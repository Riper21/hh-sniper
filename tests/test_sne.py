"""Тесты Этапа 5: поллинг, дедуп-сторы, пуск POST /negotiations."""

import json
import os
from pathlib import Path

from snpr.approve import AppliedRecord, AppliedStore
from snpr.cli import send_candidates
from snpr.pipeline import Candidate
from snpr.sne import (
    DeferredStore,
    HHNegotiationSender,
    HHVacancyProvider,
    SeenStore,
    VacancyCardCache,
    build_search_params,
    pick_resume_id,
)


def _make_vacancy(
    vid: str, name: str = "AI инженер", salary_to: int | None = 450000
) -> dict:
    return {
        "id": vid,
        "name": name,
        "employer": {"name": "ОК Компани"},
        "salary": (
            {"from": 300000, "to": salary_to, "currency": "RUR"}
            if salary_to
            else None
        ),
        "area": {"id": "40", "name": "Удалённая работа"},
        "published_at": "2026-10-16T12:00:00+03:00",
        "description": "LLM evals, агенты, Python.",
        "key_skills": [{"name": "Python"}],
    }


class FakeApiClient:
    """api_client базы: записи вызовов GET/POST с подставными ответами."""

    def __init__(
        self, search_items=None, resumes=None, post_result=None,
        search_pages=None, cards=None
    ):
        self.get_calls: list = []
        self.post_calls: list = []
        self.search_pages = (
            search_pages
            if search_pages is not None
            else [search_items or []]
        )
        self.resumes = resumes
        self.post_result = post_result if post_result is not None else {}
        self.cards = cards if cards is not None else {}

    def get(self, endpoint, params=None):
        self.get_calls.append((endpoint, params))
        if endpoint == "/vacancies":
            page = int((params or {}).get("page", 0))
            items = self.search_pages[page] if page < len(self.search_pages) else []
            return {
                "found": sum(len(chunk) for chunk in self.search_pages),
                "pages": len(self.search_pages),
                "items": items,
            }
        if endpoint == "/resumes/mine":
            return {"items": self.resumes or []}
        vid = endpoint.rsplit("/", 1)[-1]
        return self.cards.get(vid) or _make_vacancy(vid)

    def post(self, endpoint, params=None, delay=None):
        self.post_calls.append((endpoint, params, delay))
        return self.post_result


def test_seen_store_roundtrip(tmp_path):
    store = SeenStore(tmp_path / "seen.jsonl")
    assert store.seen_ids() == set()
    store.mark_seen("111", name="AI", status="ready")
    store.mark_seen("222", name="ML", status="filtered_out")
    assert store.seen_ids() == {"111", "222"}
    first = (tmp_path / "seen.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first)["status"] == "ready"


def test_seen_store_ignores_garbage(tmp_path):
    path = tmp_path / "seen.jsonl"
    path.write_text("не json\n\n{id: 'x'}\n", encoding="utf-8")
    assert SeenStore(path).seen_ids() == set()


def test_applied_store_vacancy_ids(tmp_path):
    store = AppliedStore(tmp_path / "applied.jsonl")
    assert store.vacancy_ids() == set()
    assert store.duplicate_keys() == set()
    store.append(
        AppliedRecord("v1", "AI", "OK", 80, "letter", "approve", dry_run=False)
    )
    with (tmp_path / "applied.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("мусорная строка\n")
    assert store.vacancy_ids() == {"v1"}
    # дедуп-ключи: нормализация регистра/пробелов, мусор игнорируется
    assert store.duplicate_keys() == {("ai", "ok")}
    store.append(
        AppliedRecord("v2", "  AI ", "oK ", 75, "letter", "approve", dry_run=False)
    )
    assert store.duplicate_keys() == {("ai", "ok")}


def test_build_search_params():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    params = build_search_params("LLM", 24, 50, now=now)
    assert params["text"] == "LLM"
    assert params["per_page"] == 50
    assert params["page"] == 0
    assert params["order_by"] == "publication_time"
    assert params["date_from"] == "2026-10-15T12:00:00+00:00"


def test_build_search_params_fractional_hours():
    """18.10: окно адаптивное — доли часа (5-минутная каденция)."""
    from datetime import datetime, timezone

    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    params = build_search_params("LLM", 0.25, 50, now=now)
    assert params["date_from"] == "2026-10-16T11:45:00+00:00"


def test_resolve_search_hours_adaptive(tmp_path):
    """Окно = elapsed + 6 мин, с полом floor/cap; --hours жёстче."""
    import time as time_mod

    from snpr.sne import resolve_search_hours

    cfg = {"hours": 24, "min_hours": 0.25}
    now = time_mod.time()
    marker = tmp_path / "last_run.txt"

    # нет маркера (первый прогон) — полный cap
    assert resolve_search_hours(0, cfg, marker) == 24.0
    # мусор в маркере — тоже cap
    marker.write_text("не число", encoding="utf-8")
    assert resolve_search_hours(0, cfg, marker) == 24.0

    # 3 минуты назад → floor (0.25) — окно чуть шире каденции
    marker.write_text(f"{now - 180:.0f}", encoding="utf-8")
    assert resolve_search_hours(0, cfg, marker, now=now) == 0.25

    # 2 часа паузы → окно раскрылось на паузу (+6 мин)
    marker.write_text(f"{now - 7200:.0f}", encoding="utf-8")
    got = resolve_search_hours(0, cfg, marker, now=now)
    assert abs(got - 2.1) < 0.01

    # пауза больше cap → не шире 24
    marker.write_text(f"{now - 3 * 86400:.0f}", encoding="utf-8")
    assert resolve_search_hours(0, cfg, marker, now=now) == 24.0

    # ручной флаг жёстко задаёт окно
    assert resolve_search_hours(6, cfg, marker, now=now) == 6.0


def test_resolve_search_hours_catchup_cap(tmp_path):
    """catchup_hours: после простоя не догоняем гору — только свежее.

    19.10, запрос: включение джобы не должно пережёвывать накопленное
    за выключенное состояние (429 на медленном ключе LLM).
    """
    import time as time_mod

    from snpr.sne import resolve_search_hours

    cfg = {"hours": 24, "min_hours": 0.1, "catchup_hours": 0.1}
    now = time_mod.time()
    marker = tmp_path / "last_run.txt"

    # пауза 10 часов → окно НЕ раскрылось на паузу, а прижато к catchup
    marker.write_text(f"{now - 10 * 3600:.0f}", encoding="utf-8")
    assert resolve_search_hours(0, cfg, marker, now=now) == 0.1

    # пауза 3 минуты → как обычно (окно ≈ каденция, catchup не мешает)
    marker.write_text(f"{now - 180:.0f}", encoding="utf-8")
    assert resolve_search_hours(0, cfg, marker, now=now) == 0.1

    # нет маркера (первый прогон) → catchup, а не полный 24-часовой cap
    marker.unlink()
    assert resolve_search_hours(0, cfg, marker) == 0.1

    # catchup уже часов (жёсткий прогон руками) → dogон до него, не до 24
    cfg2 = {"hours": 24, "min_hours": 0.1, "catchup_hours": 2.0}
    marker.write_text(f"{now - 10 * 3600:.0f}", encoding="utf-8")
    assert resolve_search_hours(0, cfg2, marker, now=now) == 2.0

    # --hours сильнее catchup: ручной прогон всегда жёсткий
    assert resolve_search_hours(6, cfg, marker, now=now) == 6.0

    # catchup НИЖЕ floor → верх берёт cap (floor не раздувает окно)
    cfg3 = {"hours": 24, "min_hours": 0.25, "catchup_hours": 0.1}
    marker.write_text(f"{now - 10 * 3600:.0f}", encoding="utf-8")
    assert resolve_search_hours(0, cfg3, marker, now=now) == 0.1

    # catchup = 0 (выкл) → прежнее поведение: полный догон паузы
    cfg4 = {"hours": 24, "min_hours": 0.1}
    marker.write_text(f"{now - 7200:.0f}", encoding="utf-8")
    got = resolve_search_hours(0, cfg4, marker, now=now)
    assert abs(got - 2.1) < 0.01


def test_run_lock_single_and_stale(tmp_path):
    """Лок: параллельный прогон не стартует, релиз УДАЛЯЕТ файл,
    зависший — снимается. (18.10, поймано живьём: релиз без unlink
    выродил бы 5-минутную джобу в раз-в-полчаса.)"""
    import os
    import time as time_mod

    from snpr.sne import _acquire_run_lock, _release_run_lock

    lock = tmp_path / "sne.lock"
    fd = _acquire_run_lock(lock)
    assert fd is not None
    # второй прогон поверх — отказ
    assert _acquire_run_lock(lock) is None
    # релиз удаляет файл — следующий стартует свободно
    _release_run_lock(lock, fd)
    assert not lock.exists()
    fd = _acquire_run_lock(lock)
    assert fd is not None

    # зависший лок (35 мин) — снимается
    _release_run_lock(lock, fd)
    fd = _acquire_run_lock(lock)
    assert fd is not None
    _release_run_lock(lock, fd)
    fd = _acquire_run_lock(lock)
    assert fd is not None

    # ...и то же для старого формата без pid: только возраст
    _release_run_lock(lock, fd)
    lock.write_text("", encoding="utf-8")
    old = time_mod.time() - 2100
    lock.touch()
    os.utime(lock, (old, old))
    fd2 = _acquire_run_lock(lock)
    assert fd2 is not None
    _release_run_lock(lock, fd2)
    assert not lock.exists()


def test_run_lock_pid_liveness(tmp_path):
    """Неактивный держатель лока — снятие мгновенно, не 30-мин стейл.
    Живой — блокирует. Старый формат без pid — решает возраст.
    (18.10 ~01:44: инстанс остановили ^C, finally не отработал, лок утёк.)"""
    import os
    import subprocess
    import sys

    from snpr.sne import _acquire_run_lock, _release_run_lock

    lock = tmp_path / "sne.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    # pid процесса, который уже завершился: лок свежий по возрасту,
    # но держателя нет — забираем сразу
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
    )
    dead_pid = int(proc.stdout.strip())
    lock.write_text(f"{dead_pid}\n", encoding="utf-8")
    fd = _acquire_run_lock(lock)
    assert fd is not None
    _release_run_lock(lock, fd)

    # живой держатель (сам тест-процесс) — блокирует, пока свежо
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert _acquire_run_lock(lock) is None
    # но не дольше стейла
    fd = _acquire_run_lock(lock, stale_after=-1)
    assert fd is not None
    _release_run_lock(lock, fd)

    # старый формат (пустой файл) — только возраст, как раньше
    lock.write_text("", encoding="utf-8")
    assert _acquire_run_lock(lock) is None
    lock.unlink()


def test_provider_search_new_dedup_and_limit():
    shorts = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    api = FakeApiClient(search_items=shorts)
    provider = HHVacancyProvider(api, per_page=50)
    result = provider.search_new(["LLM"], 24, known_ids={"2"}, limit=0)
    assert [v["id"] for v in result] == ["1", "3"]
    assert provider.search_new(["LLM"], 24, known_ids={"1", "2", "3"}) == []


def test_provider_does_not_fetch_full_cards():
    """Пул в сотни вакансий не должен стоить сотни GET карточек."""
    shorts = [{"id": str(i)} for i in range(10)]
    api = FakeApiClient(search_items=shorts)
    provider = HHVacancyProvider(api, per_page=50)
    result = provider.search_new(["LLM"], 24, known_ids=set(), limit=0)
    assert len(result) == 10
    assert all(call[0] == "/vacancies" for call in api.get_calls)


def test_provider_search_new_limit():
    shorts = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    api = FakeApiClient(search_items=shorts)
    provider = HHVacancyProvider(api, per_page=50)
    result = provider.search_new(["LLM"], 24, known_ids=set(), limit=2)
    assert len(result) == 2


def test_provider_paginates_pages():
    """Страницы обходятся, а не берётся только первая (объём дня > per_page)."""
    api = FakeApiClient(search_pages=[[{"id": "1"}, {"id": "2"}], [{"id": "3"}]])
    provider = HHVacancyProvider(api, per_page=2)
    result = provider.search_new(["LLM"], 24, known_ids=set(), limit=0)
    assert {v["id"] for v in result} == {"1", "2", "3"}
    pages_requested = [
        params.get("page") for endpoint, params in api.get_calls if endpoint == "/vacancies"
    ]
    assert pages_requested == [0, 1]


def test_provider_paginates_round_robin_between_queries():
    """Первый запрос не должен съедать весь потолок пула."""
    api = FakeApiClient(
        search_pages=[[{"id": "a1"}], [{"id": "a2"}]]
    )
    provider = HHVacancyProvider(api, per_page=1)
    result = provider.search_new(["A", "B"], 24, known_ids=set(), limit=0)
    assert [v["id"] for v in result] == ["a1", "a2"]


def test_provider_sorts_fresh_first():
    shorts = [
        {"id": "old", "published_at": "2026-10-15T08:00:00+03:00"},
        {"id": "new", "published_at": "2026-10-16T20:00:00+03:00"},
        {"id": "mid", "published_at": "2026-10-16T09:00:00+03:00"},
    ]
    api = FakeApiClient(search_items=shorts)
    provider = HHVacancyProvider(api, per_page=50)
    result = provider.search_new(["LLM"], 24, known_ids=set(), limit=0)
    assert [v["id"] for v in result] == ["new", "mid", "old"]


def test_card_cache_fetches_once_and_enriches():
    """Полная карточка нужна только выжившим и берётся один раз."""
    api = FakeApiClient()
    cache = VacancyCardCache(api)
    vacancy = {"id": "777", "name": "AI инженер"}
    first = cache.description(vacancy)
    second = cache.description(vacancy)
    assert cache.calls == 1
    assert "LLM evals" in first
    assert second == first
    # карточка дополняет короткий item из выдачи для промпта скоринга
    assert vacancy["key_skills"] == [{"name": "Python"}]


def test_deferred_store_roundtrip(tmp_path):
    store = DeferredStore(tmp_path / "deferred.jsonl")
    assert store.load() == []
    kept = store.save(
        [
            {
                "id": "111",
                "name": "AI",
                "employer": {"name": "X"},
                "area": {"id": "1261"},
                "published_at": "2026-10-16T12:00:00+03:00",
                "snippet": "не нужно",
                "contacts": [{"email": "x@y"}],
            }
        ]
    )
    assert kept == 1
    loaded = DeferredStore(tmp_path / "deferred.jsonl").load()
    assert loaded[0]["id"] == "111"
    # лишние поля не сохраняем: очередь не должна протекать контактами
    assert "contacts" not in loaded[0]
    assert "snippet" not in loaded[0]


def test_deferred_store_ignores_garbage(tmp_path):
    path = tmp_path / "deferred.jsonl"
    path.write_text("не json\n\n[1,2]\n", encoding="utf-8")
    assert DeferredStore(path).load() == []


def test_sender_posts_negotiation():
    api = FakeApiClient()
    sender = HHNegotiationSender(api, resume_id="RES-1")
    sender.send("123", "текст письма")
    endpoint, params, delay = api.post_calls[0]
    assert endpoint == "/negotiations"
    assert params == {
        "resume_id": "RES-1",
        "vacancy_id": "123",
        "message": "текст письма",
    }
    assert delay is not None and 1 <= delay <= 3


def test_sender_raises_on_unexpected_response():
    api = FakeApiClient(post_result={"error": "oops"})
    sender = HHNegotiationSender(api, resume_id="RES-1")
    try:
        sender.send("123", "letter")
    except RuntimeError as ex:
        assert "неожиданный ответ" in str(ex)
    else:
        raise AssertionError("ожидали RuntimeError")


def test_pick_resume_id():
    api = FakeApiClient(resumes=[{"id": "R-9", "title": "AI инженер"}])
    assert pick_resume_id(api) == "R-9"
    assert pick_resume_id(api, preferred="R-1") == "R-1"
    try:
        pick_resume_id(FakeApiClient(resumes=[]))
    except RuntimeError:
        pass
    else:
        raise AssertionError("ожидали RuntimeError при пустом списке резюме")


class _RecordingSender:
    def __init__(self, error: Exception | None = None):
        self.sent: list[tuple[str, str]] = []
        self.error = error

    def send(self, vacancy_id: str, letter: str) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append((vacancy_id, letter))


def _ready(vid: str) -> Candidate:
    from snpr.cover_letter import LetterDraft
    from snpr.scoring import ScoreResult

    return Candidate(
        vacancy=_make_vacancy(vid),
        score=ScoreResult(vacancy_id=vid, score=88, verdict="target"),
        letter=LetterDraft(vacancy_id=vid, text="Письмо без чисел и ссылок."),
    )


def _send(candidate, sender, store) -> tuple[int, int]:
    from snpr.approve import AutoApprover, Decision
    from snpr.letter_guard import LetterGuard

    class _YesApprover(AutoApprover):
        def decide(self, cand):
            from snpr.approve import ApproveDecision

            return ApproveDecision(Decision.APPROVE)

    return send_candidates(
        [candidate],
        approver=_YesApprover(),
        guard=LetterGuard(allowed_numbers=set(), allowed_url_prefixes=set()),
        manual=_YesApprover(),
        applied_store=store,
        dry_run=False,
        sender=sender,
    )


def test_send_candidates_with_sender_success(tmp_path):
    candidate = _ready("777")
    store = AppliedStore(tmp_path / "applied.jsonl")
    sender = _RecordingSender()

    sent, _, _ = _send(candidate, sender, store)

    assert sent == 1
    assert sender.sent[0][0] == "777"
    assert "777" in store.vacancy_ids()


def test_send_candidates_duplicate_vacancy_sends_best_once(tmp_path, capsys):
    """19.10: Grand trade — два одинаковых объявления (разные id) в
    одном прогоне: уходит один отклик лучшему по скору, второй
    закрывается duplicate и не попадает в applied."""
    from snpr.approve import AutoApprover, Decision
    from snpr.letter_guard import LetterGuard
    from snpr.scoring import ScoreResult

    lo = _ready("111")  # score 88 из _ready
    hi = _ready("222")
    # «два одинаковых объявления»: название и работодатель совпадают
    for cand in (lo, hi):
        cand.vacancy["name"] = "Руководитель направления ИИ"
        cand.vacancy["employer"] = {"id": "e1", "name": "Grand trade"}
    lo.score = ScoreResult(vacancy_id="111", score=55, verdict="maybe")
    hi.score = ScoreResult(vacancy_id="222", score=58, verdict="maybe")

    store = AppliedStore(tmp_path / "applied.jsonl")
    sender = _RecordingSender()

    class _YesApprover(AutoApprover):
        def decide(self, cand):
            from snpr.approve import ApproveDecision

            return ApproveDecision(Decision.APPROVE)

    sent, _, _ = send_candidates(
        [lo, hi],
        approver=_YesApprover(),
        guard=LetterGuard(allowed_numbers=set(), allowed_url_prefixes=set()),
        manual=_YesApprover(),
        applied_store=store,
        dry_run=False,
        sender=sender,
    )

    assert sent == 1
    # ушла ЛУЧШАЯ по скору (58), не первая по списку (55)
    assert sender.sent == [("222", "Письмо без чисел и ссылок.")]
    assert store.vacancy_ids() == {"222"}
    assert hi.status == "ready"
    assert lo.status == "duplicate"
    assert "ДУБЛЬ объявления" in capsys.readouterr().out


def test_send_candidates_duplicate_in_applied_history_skipped(tmp_path):
    """Дубль уже откликнутой раньше (история applied) — не отправляется
    даже автопилотом с высоким скором."""
    from snpr.approve import AppliedRecord, AutoApprover, Decision
    from snpr.letter_guard import LetterGuard

    store = AppliedStore(tmp_path / "applied.jsonl")
    store.append(
        AppliedRecord(
            vacancy_id="999",
            vacancy_name="Руководитель направления ИИ",
            employer="Grand trade",
            score=58,
            letter="x",
            decision="approve",
            dry_run=False,
        )
    )

    cand = _ready("111")
    cand.vacancy["name"] = "Руководитель направления ИИ"  # тот же ключ
    cand.vacancy["employer"] = {"id": "e1", "name": "Grand Trade"}  # регистр!

    sender = _RecordingSender()

    class _YesApprover(AutoApprover):
        def decide(self, c):
            from snpr.approve import ApproveDecision

            return ApproveDecision(Decision.APPROVE)

    sent, _, _ = send_candidates(
        [cand],
        approver=_YesApprover(),
        guard=LetterGuard(allowed_numbers=set(), allowed_url_prefixes=set()),
        manual=_YesApprover(),
        applied_store=store,
        dry_run=False,
        sender=sender,
    )

    assert sent == 0
    assert sender.sent == []
    assert cand.status == "duplicate"
    assert store.vacancy_ids() == {"999"}  # новой записи нет


def test_send_candidates_sender_failure_no_record(tmp_path, capsys):
    candidate = _ready("777")
    store = AppliedStore(tmp_path / "applied.jsonl")
    sender = _RecordingSender(error=RuntimeError("сеть упала"))

    sent, _, _ = _send(candidate, sender, store)

    assert sent == 0
    assert store.vacancy_ids() == set()
    assert "пуск не прошёл" in capsys.readouterr().out


def test_send_candidates_limit_exceeded_stops(tmp_path, capsys):
    from hh_applicant_tool.api.errors import LimitExceeded

    candidate = _ready("777")
    store = AppliedStore(tmp_path / "applied.jsonl")
    sender = _RecordingSender(error=LimitExceeded(None, {"errors": []}))

    sent, _, _ = _send(candidate, sender, store)

    assert sent == 0
    out = capsys.readouterr().out
    assert "лимит откликов" in out.lower()


def test_mark_seen_skips_error_candidates(tmp_path):
    """Вакансия с упавшим скорингом не должна навсегда выпасть из выдачи."""
    from snpr.sne import mark_seen

    seen = SeenStore(tmp_path / "seen.jsonl")
    ok = _ready("111")
    failed = _ready("222")
    failed.error = "LLM недоступен после 2 попыток: RuntimeError: 502"

    marked = mark_seen(seen, [ok, failed])

    assert marked == 1
    assert seen.seen_ids() == {"111"}


def test_mark_seen_skips_deferred_candidates(tmp_path):
    """Отложенные по бюджету не теряем — они живут в очереди deferred."""
    from snpr.sne import mark_seen

    seen = SeenStore(tmp_path / "seen.jsonl")
    scored = _ready("111")
    deferred = _ready("222")
    deferred.deferred = True

    marked = mark_seen(seen, [scored, deferred])

    assert marked == 1
    assert seen.seen_ids() == {"111"}


def test_requeue_candidates_keeps_error_and_deferred(tmp_path):
    """Кейс живого прогона 17.10: error из очереди выпал и жил только
    до края 24-часового окна поиска — дальше потеря навсегда."""
    from snpr.sne import requeue_candidates

    store = DeferredStore(tmp_path / "deferred.jsonl")
    done = _ready("111")
    failed = _ready("222")
    failed.error = "LLM недоступен после 3 попыток: 503"
    later = _ready("333")
    later.deferred = True

    queued = requeue_candidates(store, [done, failed, later])

    assert queued == 2
    assert [item["id"] for item in store.load()] == ["222", "333"]
    # закрытая вакансия из очереди убрана — она уже в seen
    assert "111" not in {item["id"] for item in store.load()}


# --- why: диагностика воронки для одной вакансии ---

_WHY_CFG = {
    "floor_net_rub": 200000,
    "grade": {"exclude_pattern": "junior|middle", "exclude_experience": ["between1And3"]},
    # непустые паттерны: пустой regex в PipelineFilter матчит всё подряд
    "vendor": {"employers": "Luxoft|EPAM", "keywords": "аутсорс|аутстафф"},
    "gov": {"employers": "РЖД|Ростех", "keywords": "госзаказ|44-ФЗ"},
    "geo": {"remote_ok": True, "areas": ["1261"]},
}


def test_parse_vacancy_ref():
    from snpr.sne import parse_vacancy_ref

    assert parse_vacancy_ref("136375819") == "136375819"
    assert parse_vacancy_ref("https://hh.ru/vacancy/136375819") == "136375819"
    assert (
        parse_vacancy_ref("https://hh.ru/vacancy/136375819?query=LLM")
        == "136375819"
    )
    assert parse_vacancy_ref("hh.ru/vacancy/136375819/") == "136375819"
    assert parse_vacancy_ref("") == ""
    assert parse_vacancy_ref("без цифр") == ""


def test_explain_vacancy_seen_filtered_out(tmp_path):
    """Уже обработанная гейтами вакансия: статус в seen + вердикт гейтов."""
    from snpr.sne import explain_vacancy

    seen = SeenStore(tmp_path / "seen.jsonl")
    seen.mark_seen("777", name="AI", status="filtered_out")
    lines = explain_vacancy(
        FakeApiClient(),
        _WHY_CFG,
        "777",
        tmp_path / "seen.jsonl",
        tmp_path / "applied.jsonl",
        tmp_path / "deferred.jsonl",
        ["LLM"],
    )
    text = "\n".join(lines)
    assert "seen:    да" in text
    assert "filtered_out" in text
    # фикстура без work_format и area 40 → гео-гейт забивает
    assert "geo" in text and "FAIL" in text
    assert "конвейер уже обработал" in text


def test_explain_vacancy_query_miss(tmp_path):
    """Гейты пропускают, но поиск её не находит — потеря ДО бота."""
    from snpr.sne import explain_vacancy

    vacancy = _make_vacancy("555")
    vacancy["work_format"] = [{"id": "REMOTE"}]
    api = FakeApiClient(cards={"555": vacancy})  # поиск пуст
    lines = explain_vacancy(
        api,
        _WHY_CFG,
        "555",
        tmp_path / "seen.jsonl",
        tmp_path / "applied.jsonl",
        tmp_path / "deferred.jsonl",
        ["LLM", "AI инженер"],
    )
    text = "\n".join(lines)
    assert "seen:    нет" in text
    assert "гейты пропускают" in text
    assert "ни один из 2 запросов" in text
    assert "ДО бота" in text


def test_explain_vacancy_coverage_found(tmp_path):
    """Гейты пропускают и поиск находит — должна была попасть в пул."""
    from snpr.sne import explain_vacancy

    vacancy = _make_vacancy("555")
    vacancy["work_format"] = [{"id": "REMOTE"}]
    api = FakeApiClient(search_items=[{"id": "555"}], cards={"555": vacancy})
    lines = explain_vacancy(
        api,
        _WHY_CFG,
        "555",
        tmp_path / "seen.jsonl",
        tmp_path / "applied.jsonl",
        tmp_path / "deferred.jsonl",
        ["LLM"],
    )
    text = "\n".join(lines)
    assert "должна была попасть в пул" in text
    assert "LLM" in text


# --- паркинг: запланированные прогоны без человека (17.10) ---


def _scored(vid: str, points: int = 88):
    from snpr.scoring import ScoreResult

    return ScoreResult(
        vacancy_id=vid, score=points, verdict="target", reasons=["стек совпал"]
    )


def test_send_candidates_park_mode(tmp_path):
    """Планировщик: ready не пускает и не отказывает — ложится в парковку.

    Ни seen, ни очередь deferred: вакансия живёт в data/parked.jsonl до
    решения человека (`snpr approve`). Пайп 'n' раньше такое закрыл бы.
    """
    from snpr.approve import CLIApprover, ParkingApprover
    from snpr.letter_guard import LetterGuard
    from snpr.sne import ParkedStore, mark_seen, requeue_candidates

    candidate = _ready("777")
    sender = _RecordingSender()
    parked = ParkedStore(tmp_path / "parked.jsonl")

    sent, forced, parked_n = send_candidates(
        [candidate],
        approver=ParkingApprover(),
        guard=LetterGuard(allowed_numbers=set(), allowed_url_prefixes=set()),
        manual=CLIApprover(prompt=lambda _: "n"),
        applied_store=AppliedStore(tmp_path / "applied.jsonl"),
        dry_run=False,
        sender=sender,
        parked_store=parked,
    )

    assert (sent, forced, parked_n) == (0, 0, 1)
    assert sender.sent == []
    records = parked.load_records()
    assert len(records) == 1
    assert str(records[0]["vacancy"]["id"]) == "777"
    assert records[0]["score"]["score"] == 88
    assert records[0]["score"]["verdict"] == "target"
    assert "Письмо" in records[0]["letter"]

    # вакансия не закрыта: seen и deferred её не забирают
    assert candidate.status == "parked"
    seen = SeenStore(tmp_path / "seen.jsonl")
    assert mark_seen(seen, [candidate]) == 0
    assert seen.seen_ids() == set()
    assert requeue_candidates(DeferredStore(tmp_path / "deferred.jsonl"), [candidate]) == 0


def test_parked_store_roundtrip(tmp_path):
    """ids() для дедупа пула + remove() закрытых."""
    from snpr.sne import ParkedStore

    parked = ParkedStore(tmp_path / "parked.jsonl")
    parked.append_record(_make_vacancy("777"), _scored("777"), "текст письма")
    parked.append_record(_make_vacancy("888"), _scored("888", 55), "другое письмо")

    assert parked.ids() == {"777", "888"}
    assert len(parked.load_records()) == 2

    removed = parked.remove(["777"])
    assert removed == 1
    assert parked.ids() == {"888"}


def test_send_candidates_auto_park_threshold(tmp_path):
    """18.10: автопилот в парк-режиме — от 55 пускает сам, 45-54 паркует.

    Решение пользователя: рынок не наполнен >70% сходством, бьём по всем
    от 45; автопуск — от 55 без спроса. Гвард чист (проверен ДО
    решения), письмо уникально (static = similarity 0).
    """
    from snpr.approve import AutoParkApprover, CLIApprover
    from snpr.cover_letter import LetterDraft
    from snpr.letter_guard import LetterGuard
    from snpr.sne import ParkedStore

    def _auto_ready(vid: str, points: int, name: str) -> Candidate:
        return Candidate(
            vacancy=_make_vacancy(vid, name=name),
            score=_scored(vid, points),
            letter=LetterDraft(vacancy_id=vid, text="Письмо без чисел и ссылок."),
        )

    # имена разные: одинаковые (название, работодатель) — это дубль
    # объявления (19.10), он закрывается, а не паркуется
    hot = _auto_ready("585", 58, "AI инженер")  # >= 55 — пуск без человека
    warm = _auto_ready("484", 48, "ML инженер")  # 45-54 — парковка на approve
    sender = _RecordingSender()
    parked = ParkedStore(tmp_path / "parked.jsonl")

    sent, forced, parked_n = send_candidates(
        [hot, warm],
        approver=AutoParkApprover(score_threshold=55),
        guard=LetterGuard(allowed_numbers=set(), allowed_url_prefixes=set()),
        manual=CLIApprover(prompt=lambda _: "n"),
        applied_store=AppliedStore(tmp_path / "applied.jsonl"),
        dry_run=False,
        sender=sender,
        parked_store=parked,
    )

    assert (sent, forced, parked_n) == (1, 0, 1)
    assert sender.sent == [("585", "Письмо без чисел и ссылок.")]
    assert [str(r["vacancy"]["id"]) for r in parked.load_records()] == ["484"]
    assert AppliedStore(tmp_path / "applied.jsonl").vacancy_ids() == {"585"}
    assert hot.status == "ready"
    assert warm.status == "parked"


def test_auto_park_approver_edges():
    """Границы гибрида: без письма/скора/уникальности — парковка, не SKIP."""
    from snpr.approve import AutoParkApprover, Decision
    from snpr.cover_letter import LetterDraft

    def _check(candidate: Candidate) -> str:
        return AutoParkApprover(score_threshold=55).decide(candidate).decision.value

    assert _check(_ready("777")) == "approve"  # 88 >= 55
    no_letter = Candidate(vacancy=_make_vacancy("111"), score=_scored("111", 70))
    assert _check(no_letter) == "park"
    no_score = Candidate(
        vacancy=_make_vacancy("222"),
        letter=LetterDraft(vacancy_id="222", text="Письмо."),
    )
    assert _check(no_score) == "park"
    not_unique = Candidate(
        vacancy=_make_vacancy("333"),
        score=_scored("333", 70),
        letter=LetterDraft(vacancy_id="333", text="Письмо.", similarity=0.9),
    )
    assert _check(not_unique) == "park"
    # требование уникальности снято — скор решает
    loose = AutoParkApprover(score_threshold=55, require_unique=False)
    assert loose.decide(not_unique).decision.value == "approve"


def test_vacancy_age_hours_timezone_safe():
    """18.10: published_at в +03:00 (сервер hh) vs локальные часы (+05) —
    возраст это разность МОМЕНТОВ, пояс не влияет. Наивная строка = +03:00."""
    from datetime import datetime, timedelta, timezone

    from snpr.approve import vacancy_age_hours

    now = datetime.now(timezone.utc)
    msk = timezone(timedelta(hours=3))
    yekt = timezone(timedelta(hours=5))

    # одна и та же секунда, записанная в разных поясах
    pub_msk = (now - timedelta(hours=5)).astimezone(msk).isoformat()
    pub_yekt = (now - timedelta(hours=5)).astimezone(yekt).isoformat()
    assert abs(vacancy_age_hours(pub_msk, now=now) - 5.0) < 0.01
    assert abs(vacancy_age_hours(pub_yekt, now=now) - 5.0) < 0.01

    # без смещения — трактуем как московское серверное
    pub_naive = (
        (now - timedelta(hours=2)).astimezone(msk).replace(tzinfo=None).isoformat()
    )
    assert abs(vacancy_age_hours(pub_naive, now=now) - 2.0) < 0.01

    assert vacancy_age_hours(None) is None
    assert vacancy_age_hours("не дата") is None
    # будущее (дрейф часов) — не отрицательный возраст
    assert vacancy_age_hours((now + timedelta(minutes=1)).isoformat(), now=now) == 0.0


def test_auto_park_approver_age_gate():
    """18.10: скор >= 55, но вакансии 6 ч — парковка; 3 ч — пуск."""
    from datetime import datetime, timedelta, timezone

    from snpr.approve import AutoParkApprover
    from snpr.cover_letter import LetterDraft

    def _aged(vid: str, age_hours: float) -> Candidate:
        pub = (
            datetime.now(timezone.utc) - timedelta(hours=age_hours)
        ).isoformat()
        vacancy = _make_vacancy(vid)
        vacancy["published_at"] = pub
        return Candidate(
            vacancy=vacancy,
            score=_scored(vid, 70),
            letter=LetterDraft(vacancy_id=vid, text="Письмо без чисел и ссылок."),
        )

    approver = AutoParkApprover(score_threshold=55, max_age_hours=5.0)
    assert approver.decide(_aged("fresh", 3.0)).decision.value == "approve"
    assert approver.decide(_aged("stale", 6.0)).decision.value == "park"

    # возраст неизвестен — автопилот не пускает
    no_age = _make_vacancy("noage")
    no_age.pop("published_at", None)
    unknown = Candidate(
        vacancy=no_age,
        score=_scored("noage", 70),
        letter=LetterDraft(vacancy_id="noage", text="Письмо."),
    )
    assert approver.decide(unknown).decision.value == "park"

    # лимит не задан — гейта нет (обратная совместимость)
    assert (
        AutoParkApprover(score_threshold=55).decide(_aged("any", 48.0)).decision.value
        == "approve"
    )


def test_scores_store_roundtrip(tmp_path):
    """Журнал чисел: load/ids, причины урезаны до 3, без скора не пишем."""
    from snpr.scoring import ScoreResult
    from snpr.sne import ScoresStore, record_scores

    store = ScoresStore(tmp_path / "scores.jsonl")
    store.append_record(_make_vacancy("777"), _scored("777", 79), ts=1000)
    low = ScoreResult(
        vacancy_id="888", score=55, verdict="maybe", reasons=["мимо домена"]
    )
    store.append_record(_make_vacancy("888"), low, ts=2000)

    rows = store.load()
    assert [r["vacancy_id"] for r in rows] == ["777", "888"]
    assert rows[0]["score"] == 79 and rows[0]["verdict"] == "target"
    assert rows[0]["ts"] == 1000
    assert rows[1]["score"] == 55 and rows[1]["verdict"] == "maybe"
    assert store.ids() == {"777", "888"}

    scored = [
        Candidate(vacancy=_make_vacancy("111"), score=_scored("111", 15)),
        Candidate(vacancy=_make_vacancy("222")),  # без скора — мимо
    ]
    assert record_scores(store, scored, ts=3000) == 1
    assert store.ids() == {"777", "888", "111"}


def _run_approve_with(
    tmp_path, monkeypatch, answers, cards=None
):
    """run_approve на фейковом API; answers — ответы на промпты."""
    import argparse

    import snpr.hh_tool as hh_tool_mod
    from snpr.sne import ParkedStore, run_approve

    api = FakeApiClient(
        resumes=[{"id": "R-9", "title": "AI инженер"}],
        post_result={},
        cards=cards if cards is not None else {},
    )

    class _FakeTool:
        api_client = api

        def _persist(self):
            pass

    monkeypatch.setattr(
        hh_tool_mod, "SavingHHApplicantTool", lambda: _FakeTool()
    )

    stream = iter(answers)

    def _input(_prompt: str) -> str:
        try:
            return next(stream)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", _input)

    args = argparse.Namespace(
        config=str(Path(__file__).resolve().parent.parent / "snpr" / "config.toml"),
        parked_store=str(tmp_path / "parked.jsonl"),
        applied_store=str(tmp_path / "applied.jsonl"),
        seen_store=str(tmp_path / "seen.jsonl"),
        resume_id="",
        dry_run=False,
    )
    parked = ParkedStore(tmp_path / "parked.jsonl")
    # 888 — другое название: иначе дедуп объявлений (19.10) закроет её
    # как дубль 777 (одинаковые name+employer) без вопроса
    parked.append_record(_make_vacancy("777"), _scored("777"), "Письмо без чисел.")
    parked.append_record(
        _make_vacancy("888", name="ML инженер"), _scored("888", 55), "Письмо два."
    )
    code = run_approve(args)
    return code, api, parked


def test_run_approve_send_and_decline(tmp_path, monkeypatch, capsys):
    """y — пуск + история; n — закрыто без пуска; парковка пустеет."""
    code, api, parked = _run_approve_with(tmp_path, monkeypatch, ["y", "n"])

    assert code == 0
    # один POST /negotiations: резюме + вакансия + письмо
    assert len(api.post_calls) == 1
    endpoint, params, _delay = api.post_calls[0]
    assert endpoint == "/negotiations"
    assert params["resume_id"] == "R-9"
    assert params["vacancy_id"] == "777"
    assert params["message"] == "Письмо без чисел."
    # обе закрыты: 777 пуском, 888 отказом
    applied = AppliedStore(tmp_path / "applied.jsonl")
    assert applied.vacancy_ids() == {"777"}
    seen = SeenStore(tmp_path / "seen.jsonl")
    assert seen.seen_ids() == {"777", "888"}
    assert parked.ids() == set()
    out = capsys.readouterr().out
    assert "ПУСК ПРОИЗВЕДЁН" in out
    assert "закрыто без пуска" in out


def test_run_approve_eof_keeps_parked(tmp_path, monkeypatch, capsys):
    """EOF (окно закрыли) — НЕ решение: вакансия остаётся в парковке."""
    code, api, parked = _run_approve_with(tmp_path, monkeypatch, [])

    assert code == 0
    assert api.post_calls == []
    assert seen_ids_of(tmp_path) == set()
    assert parked.ids() == {"777", "888"}
    assert "ждут в парковке" in capsys.readouterr().out


def seen_ids_of(tmp_path):
    return SeenStore(tmp_path / "seen.jsonl").seen_ids()


def test_run_approve_archived_closes_silently(tmp_path, monkeypatch, capsys):
    """За паузу вакансия ушла в архив — закрываем без промпта и пуска."""
    archived = _make_vacancy("888")
    archived["archived"] = True
    cards = {
        "777": _make_vacancy("777"),
        "888": archived,
    }
    code, api, parked = _run_approve_with(
        tmp_path, monkeypatch, ["y"], cards=cards
    )

    assert code == 0
    assert [c[1]["vacancy_id"] for c in api.post_calls] == ["777"]
    assert seen_ids_of(tmp_path) == {"777", "888"}
    assert parked.ids() == set()
    assert "архив" in capsys.readouterr().out


def test_run_approve_duplicate_closed_without_send(tmp_path, monkeypatch, capsys):
    """19.10: дубль уже откликнутого объявления в парковке — закрывается
    без вопроса и без отправки (двойной отклик рекрутеру — спам)."""
    from snpr.approve import AppliedRecord

    applied = AppliedStore(tmp_path / "applied.jsonl")
    applied.append(
        AppliedRecord(
            vacancy_id="999",
            vacancy_name="AI инженер",  # тот же ключ, что у 777
            employer="ОК Компани",
            score=80,
            letter="x",
            decision="approve",
            dry_run=False,
        )
    )

    code, api, parked = _run_approve_with(tmp_path, monkeypatch, ["n"])

    assert code == 0
    # 777 — дубль: закрыта БЕЗ вопроса (первый 'n' достался 888, не 777)
    assert api.post_calls == []  # ничего не отправлено
    assert seen_ids_of(tmp_path) == {"777", "888"}
    assert parked.ids() == set()
    assert AppliedStore(tmp_path / "applied.jsonl").vacancy_ids() == {"999"}
    out = capsys.readouterr().out
    assert "ДУБЛЬ объявления" in out


def test_run_approve_send_failure_keeps_parked(tmp_path, monkeypatch, capsys):
    """Пуск не прошёл (сеть/лимит) — вакансия не закрывается, ждёт."""
    import snpr.sne as sne_mod

    real_sender = sne_mod.HHNegotiationSender

    class _FailingSender(real_sender):
        def send(self, vacancy_id, letter):
            raise RuntimeError("шлюз лёг")

    monkeypatch.setattr(sne_mod, "HHNegotiationSender", _FailingSender)
    code, api, parked = _run_approve_with(tmp_path, monkeypatch, ["y", "n"])
    assert code == 0
    assert seen_ids_of(tmp_path) == {"888"}  # только отказ 'n'
    assert parked.ids() == {"777"}  # неудавшийся пуск ждёт ретрая
    assert "пуск не прошёл" in capsys.readouterr().out


class TestApproveLock:
    """19.10: pid-лок окна approve вместо поиска окна по заголовку
    (Windows Terminal хостит окно в WindowsTerminal.exe — tasklist
    WINDOWTITLE+cmd.exe его не находил, окна плодились)."""

    def test_free_then_taken_then_released(self, tmp_path):
        import snpr.sne as sne_mod

        lock = tmp_path / "approve.lock"
        fd = sne_mod._acquire_approve_lock(lock)
        assert fd is not None
        try:
            # живой держатель (свой pid) — занято безусловно
            assert sne_mod._acquire_approve_lock(lock) is None
        finally:
            sne_mod._release_run_lock(lock, fd)
        assert not lock.exists()
        assert sne_mod._acquire_approve_lock(lock) is not None

    def test_dead_holder_stolen_immediately(self, tmp_path, monkeypatch):
        # окно закрыли крестиком: pid мёртв — лок снимаем, не ждём стейла
        import snpr.sne as sne_mod

        lock = tmp_path / "approve.lock"
        lock.write_text("12345\n", encoding="utf-8")
        monkeypatch.setattr(sne_mod, "_pid_alive", lambda pid: False)
        fd = sne_mod._acquire_approve_lock(lock)
        assert fd is not None
        sne_mod._release_run_lock(lock, fd)

    def test_garbage_lock_stolen(self, tmp_path, monkeypatch):
        # битый файл (не наш формат) — снимаем сразу
        import snpr.sne as sne_mod

        lock = tmp_path / "approve.lock"
        lock.write_text("мусор", encoding="utf-8")
        monkeypatch.setattr(sne_mod, "_pid_alive", lambda pid: True)
        fd = sne_mod._acquire_approve_lock(lock)
        assert fd is not None
        sne_mod._release_run_lock(lock, fd)

    def test_lock_alive_default_path(self, tmp_path, monkeypatch):
        import snpr.sne as sne_mod

        lock = tmp_path / "approve.lock"
        monkeypatch.setattr(
            "snpr.cli.DEFAULT_PARKED_STORE", str(tmp_path / "parked.jsonl")
        )
        assert sne_mod.approve_lock_alive() is False
        lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
        assert sne_mod.approve_lock_alive() is True


def test_run_approve_second_window_refused(tmp_path, monkeypatch, capsys):
    """Живой approve-лок — второе окно не спрашивает вакансии вовсе.

    Сценарий 19.10: джоба плодила окна каждую минуту, каждое вставало
    на «Отправить?» одной и той же вакансии.
    """
    lock = tmp_path / "approve.lock"
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    code, api, parked = _run_approve_with(tmp_path, monkeypatch, [])
    assert code == 0
    out = capsys.readouterr().out
    assert "уже открыто" in out
    assert "В парковке" not in out  # вопросов не было
    assert parked.ids() == {"777", "888"}  # ничего не решено
