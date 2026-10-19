from __future__ import annotations

import json
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from typing import Any

import pytest

from snpr.approve import (
    ApproveDecision,
    AutoApprover,
    CLIApprover,
    Decision,
)
from snpr.cover_letter import LetterDraft, StaticLetterWriter
from snpr.filters import PipelineFilter
from snpr.pipeline import Candidate, PipelineAbort, SnePipeline
from snpr.scoring import ScoreResult, VacancyScorer

# гейты в тестах всегда включены: решение 17.10 (config: enabled=false)
# не должно отключать логику, которую эти тесты проверяют
from test_filters import GATES_ON

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "vacancies.json"

TARGET_JSON = json.dumps(
    {
        "score": 88,
        "verdict": "target",
        "reasons": ["evals в проде"],
        "matched": ["Python", "LLM"],
        "missing": [],
    },
    ensure_ascii=False,
)
PASS_JSON = json.dumps(
    {
        "score": 20,
        "verdict": "pass",
        "reasons": ["поддержка легаси"],
        "matched": [],
        "missing": ["evals"],
    },
    ensure_ascii=False,
)
LETTER = (
    "Здравствуйте!\n\nВ вакансии — eval-контур, у меня он в проде: "
    "МАЯК, 862 автотеста.\n\nДва вопроса:\n1. Вилка?\n2. Процесс найма?\n\n"
    "Ссылки — в резюме."
)


class FakeAI:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, message: str, **kwargs: Any) -> str:
        with self._lock:
            self.calls += 1
        return self.response


def load_vacancies() -> list[dict]:
    with FIXTURES.open(encoding="utf-8") as fh:
        return json.load(fh)


def by_id(vid: str) -> dict:
    return next(v for v in load_vacancies() if v["id"] == vid)


def make_pipeline(letter_dir: Path) -> SnePipeline:
    scorer_ai = FakeAI(TARGET_JSON)
    return SnePipeline(
        PipelineFilter(GATES_ON),
        scorer=VacancyScorer(scorer_ai),
        writer=StaticLetterWriter(),
    )


def test_pipeline_filters_out_vendor() -> None:
    pipeline = SnePipeline(PipelineFilter(GATES_ON))
    candidates = pipeline.run([by_id("v05")])
    assert candidates[0].status == "filtered_out"
    assert candidates[0].reject_reasons


def test_pipeline_scores_and_writes_letter() -> None:
    pipeline = make_pipeline(Path("tests/_tmp"))
    candidates = pipeline.run([by_id("v01")])
    candidate = candidates[0]
    assert candidate.status == "ready"
    assert candidate.score is not None
    assert candidate.score.verdict == "target"
    assert candidate.letter is not None
    assert "ScanReader" in candidate.letter.text
    assert "hh-sniper" in candidate.letter.text


def test_pipeline_pass_verdict_gets_no_letter() -> None:
    scorer_ai = FakeAI(PASS_JSON)
    pipeline = SnePipeline(
        PipelineFilter(GATES_ON), scorer=VacancyScorer(scorer_ai), writer=StaticLetterWriter()
    )
    candidates = pipeline.run([by_id("v01")])
    assert candidates[0].score is not None
    assert candidates[0].score.verdict == "pass"
    assert candidates[0].letter is None
    assert candidates[0].status == "pass"


def test_pipeline_without_ai_marks_unscored() -> None:
    pipeline = SnePipeline(PipelineFilter(GATES_ON))
    candidates = pipeline.run([by_id("v01")])
    assert candidates[0].status == "unscored"
    assert candidates[0].score is None


class DeadAI:
    """Эндпоинт LLM неактивн: все попытки падают."""

    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    def complete(self, message: str, **kwargs: Any) -> str:
        self.calls += 1
        raise self.error


def test_pipeline_marks_error_status_on_llm_failure() -> None:
    scorer = VacancyScorer(
        DeadAI(RuntimeError("502 Bad Gateway")), retries=0, retry_backoff=0.0
    )
    pipeline = SnePipeline(
        PipelineFilter(GATES_ON),
        scorer=scorer,
        writer=StaticLetterWriter(),
        llm_error_abort=99,
    )
    candidates = pipeline.run([by_id("v01"), by_id("v01")])
    assert [c.status for c in candidates] == ["error", "error"]
    assert all(c.letter is None for c in candidates)
    assert "LLM недоступен" in candidates[0].error


def test_pipeline_aborts_after_consecutive_llm_errors() -> None:
    from snpr.pipeline import PipelineAbort

    scorer = VacancyScorer(
        DeadAI(RuntimeError("503")), retries=0, retry_backoff=0.0
    )
    pipeline = SnePipeline(
        PipelineFilter(GATES_ON), scorer=scorer, llm_error_abort=3
    )
    with pytest.raises(PipelineAbort) as exc:
        pipeline.run([by_id("v01")] * 3)
    assert "3 раз подряд" in str(exc.value)


def test_pipeline_error_resets_counter_between_successes() -> None:
    """Ошибка, успех, ошибка — не прерывают прогон (счётчик не накапливает)."""

    class AlternatingAI:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, message: str, **kwargs: Any) -> str:
            self.calls += 1
            if self.calls % 2:
                raise RuntimeError("502")
            return TARGET_JSON

    pipeline = SnePipeline(
        PipelineFilter(GATES_ON),
        scorer=VacancyScorer(
            AlternatingAI(), retries=0, retry_backoff=0.0
        ),
        llm_error_abort=2,
    )
    candidates = pipeline.run([by_id("v01")] * 4)
    assert [c.status for c in candidates] == [
        "error",
        "target",
        "error",
        "target",
    ]


def test_pipeline_score_budget_defers_tail() -> None:
    """Бюджет скоринга: хвост пула откладывается, а не теряется и не гейтится зря."""
    scorer_ai = FakeAI(TARGET_JSON)
    pipeline = SnePipeline(
        PipelineFilter(GATES_ON),
        scorer=VacancyScorer(scorer_ai),
        writer=StaticLetterWriter(),
        score_budget=2,
    )
    fetched: list[str] = []

    def source(vac: Any) -> str:
        fetched.append(str(vac["id"]))
        return "Опыт с LLM-агентами"

    candidates = pipeline.run(
        [by_id("v01")] * 4, descriptions=source
    )
    assert [c.status for c in candidates] == [
        "ready",
        "ready",
        "deferred",
        "deferred",
    ]
    assert scorer_ai.calls == 2
    # отложенные всё равно прошли дешёвые гейты (статус не filtered_out)
    assert candidates[2].reject_reasons == []
    # 17.10: отложенные не тянут полные карточки — карточки берут только
    # те, кто реально идёт в скоринг (было 106 карточек при 25 проскоренных)
    assert len(fetched) == 2


def test_pipeline_dedupes_second_phase_filter() -> None:
    pipeline = SnePipeline(PipelineFilter(GATES_ON))
    descriptions = {"v06": "Работаем по аутсорс-модели с заказчиками"}
    candidates = pipeline.run([by_id("v06")], descriptions=descriptions)
    assert candidates[0].status == "filtered_out"
    assert any("vendor" in r for r in candidates[0].reject_reasons)


def test_pipeline_parallel_scoring_waves() -> None:
    """17.10: скоринг волнами по N скореров — бюджет, порядок, ленивые
    карточки и прогресс сохраняют семантику последовательного прогона."""
    fake = FakeAI(TARGET_JSON)
    scorers = [VacancyScorer(fake) for _ in range(4)]
    progress: list[tuple[int, int]] = []
    pipeline = SnePipeline(
        PipelineFilter(GATES_ON),
        scorers=scorers,
        writer=StaticLetterWriter(),
        score_budget=6,
    )
    fetched: list[str] = []

    def source(vac: Any) -> str:
        fetched.append(str(vac["id"]))
        return "LLM-агенты и evals в проде"

    candidates = pipeline.run(
        [by_id("v01")] * 10,
        descriptions=source,
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert [c.status for c in candidates] == ["ready"] * 6 + ["deferred"] * 4
    assert fake.calls == 6
    assert len(fetched) == 6
    assert progress and progress[-1] == (6, 6)


def test_pipeline_parallel_abort_on_consecutive_errors() -> None:
    """Волна из 4 воркеров против неактивного эндпоинта: 4 провала подряд
    (в порядке завершения) — PipelineAbort, сторы остаются нетронутыми."""
    dead = DeadAI(RuntimeError("503"))
    scorers = [
        VacancyScorer(dead, retries=0, retry_backoff=0.0) for _ in range(4)
    ]
    pipeline = SnePipeline(
        PipelineFilter(GATES_ON),
        scorers=scorers,
        llm_error_abort=4,
        score_budget=10,
    )
    with pytest.raises(PipelineAbort):
        pipeline.run([by_id("v01")] * 8)


def test_config_toml_batch_scoring_settings() -> None:
    """17.10: бюджет 100 и параллельный скоринг — из живого конфига."""
    with (Path(__file__).parent.parent / "snpr" / "config.toml").open(
        "rb"
    ) as fh:
        cfg = tomllib.load(fh)
    assert cfg["search"]["max_scored_per_run"] == 100
    # 19.10: медленный ключ — 4 ловили 429 на разборке очереди-горы,
    # снижено до 2 (+ fast-fail на rate limit в scoring.py)
    assert cfg["scoring"]["workers"] == 2


def test_config_toml_maybe_threshold_45() -> None:
    """18.10: порог письма 45 из живого конфига (решение пользователя)."""
    import argparse

    from snpr.cli import _build_scorers

    with (Path(__file__).parent.parent / "snpr" / "config.toml").open(
        "rb"
    ) as fh:
        cfg = tomllib.load(fh)
    scoring = cfg["scoring"]
    assert scoring["maybe_min"] == 45
    assert scoring["target_min"] == 75

    args = argparse.Namespace(
        ai=True,
        ai_base_url="https://gateway.invalid/v1",
        ai_model="test-model",
    )
    scorers = _build_scorers(
        args,
        2,
        maybe_threshold=int(scoring["maybe_min"]),
        target_threshold=int(scoring["target_min"]),
    )
    assert scorers is not None and len(scorers) == 2
    assert {s.maybe_threshold for s in scorers} == {45}
    assert {s.target_threshold for s in scorers} == {75}


def test_make_approver_park_auto_threshold() -> None:
    """18.10: park-режим + auto_approve_min > 0 = гибрид-автопилот."""
    import argparse

    from snpr.approve import AutoParkApprover, ParkingApprover
    from snpr.cli import _make_approver

    with (Path(__file__).parent.parent / "snpr" / "config.toml").open(
        "rb"
    ) as fh:
        cfg = tomllib.load(fh)
    assert cfg["scoring"]["auto_approve_min"] == 55
    assert cfg["send"]["max_age_minutes"] == 10

    args = argparse.Namespace(mode="park", score_threshold=75)
    hybrid = _make_approver(args, auto_park_threshold=55, max_age_hours=5.0)
    assert isinstance(hybrid, AutoParkApprover)
    assert hybrid.score_threshold == 55
    assert hybrid.max_age_hours == 5.0

    # 0/отсутствует — чистый парк, как было до 18.10
    assert isinstance(_make_approver(args), ParkingApprover)
    assert isinstance(_make_approver(args, auto_park_threshold=0), ParkingApprover)


def test_send_max_age_hours_minutes_priority() -> None:
    """18.10: max_age_minutes приоритетнее, часы — совместимость, 0 = выкл."""
    from snpr.sne import _send_max_age_hours

    assert _send_max_age_hours({"max_age_minutes": 10}) == 10 / 60
    assert _send_max_age_hours({"max_age_minutes": 10, "max_age_hours": 5}) == (
        10 / 60
    )
    assert _send_max_age_hours({"max_age_hours": 5.0}) == 5.0
    assert _send_max_age_hours({}) is None
    assert _send_max_age_hours({"max_age_minutes": 0, "max_age_hours": 0}) is None
    assert _send_max_age_hours({"max_age_minutes": "мусор"}) is None


def test_pipeline_description_source_per_vacancy() -> None:
    """Каждой вакансии — СВОЁ описание из DescriptionSource.

    Живой баг 17.10 (волны): замыкание по переменной цикла ловило последнюю
    вакансию — весь пул получил описание последней (Rambler Data Engineer),
    1 карточка вместо ~60, вердикты галлюцинировали «это Data Engineer»
    на юристах и схемотехниках.
    """
    descriptions = {
        "v01": "Опыт с LLM-агентами и evals в проде",
        "v06": "Работаем по аутсорс-модели с заказчиками",
    }
    calls: list[str] = []

    def source(vac: Any) -> str:
        vid = str(vac["id"])
        calls.append(vid)
        return descriptions[vid]

    pipeline = SnePipeline(PipelineFilter(GATES_ON))
    candidates = pipeline.run(
        [by_id("v01"), by_id("v06"), by_id("v01")], descriptions=source
    )
    assert calls == ["v01", "v06", "v01"]
    # v01 (нормальное описание) жив, v06 забракован vendor-ключевиком из СВОЕГО
    # описания — не из чужого (скорера нет — v01 остаётся unscored)
    assert [c.status for c in candidates] == [
        "unscored",
        "filtered_out",
        "unscored",
    ]


def test_cli_approver_yes() -> None:
    answers = iter(["y"])
    approver = CLIApprover(prompt=lambda _: next(answers))
    candidate = Candidate(
        vacancy=by_id("v01"),
        score=ScoreResult(vacancy_id="v01", score=88, verdict="target"),
        letter=LetterDraft(vacancy_id="v01", text=LETTER),
    )
    decision = approver.decide(candidate)
    assert decision.decision == Decision.APPROVE


def test_cli_approver_eof_means_reject() -> None:
    """Пайп-режим ('n' | snpr): EOF на промпте = отказ, не крэш.

    Живой кейс 17.10: второй ready-кандидат съел EOF, прогон упал в
    send_candidates — до mark_seen, потеряны ~60 проскоренных вакансий.
    """

    def eof_prompt(_: str) -> str:
        raise EOFError

    approver = CLIApprover(prompt=eof_prompt)
    candidate = Candidate(
        vacancy=by_id("v01"),
        score=ScoreResult(vacancy_id="v01", score=88, verdict="target"),
        letter=LetterDraft(vacancy_id="v01", text=LETTER),
    )
    decision = approver.decide(candidate)
    assert decision.decision == Decision.REJECT


def test_cli_approver_edit() -> None:
    answers = iter(["e", "Новый текст письма"])
    approver = CLIApprover(prompt=lambda _: next(answers))
    candidate = Candidate(
        vacancy=by_id("v01"),
        score=ScoreResult(vacancy_id="v01", score=88, verdict="target"),
        letter=LetterDraft(vacancy_id="v01", text=LETTER),
    )
    decision = approver.decide(candidate)
    assert decision.decision == Decision.EDIT
    assert decision.letter_override == "Новый текст письма"


def test_cli_approver_reject_on_empty_answer() -> None:
    approver = CLIApprover(prompt=lambda _: "")
    candidate = Candidate(
        vacancy=by_id("v01"),
        score=ScoreResult(vacancy_id="v01", score=88, verdict="target"),
        letter=LetterDraft(vacancy_id="v01", text=LETTER),
    )
    decision = approver.decide(candidate)
    assert decision.decision == Decision.REJECT


def test_auto_approver_threshold() -> None:
    candidate = Candidate(
        vacancy=by_id("v01"),
        score=ScoreResult(vacancy_id="v01", score=80, verdict="target"),
        letter=LetterDraft(vacancy_id="v01", text=LETTER),
    )
    assert AutoApprover(score_threshold=75).decide(candidate).decision == (
        Decision.APPROVE
    )
    assert AutoApprover(score_threshold=90).decide(candidate).decision == (
        Decision.SKIP
    )


def test_auto_approver_requires_unique_letter() -> None:
    candidate = Candidate(
        vacancy=by_id("v01"),
        score=ScoreResult(vacancy_id="v01", score=80, verdict="target"),
        letter=LetterDraft(
            vacancy_id="v01", text=LETTER, similarity=0.9, similar_to="v02"
        ),
    )
    decision = AutoApprover(score_threshold=75).decide(candidate)
    assert decision.decision == Decision.SKIP


def test_cli_scan_dry_run_mode_off() -> None:
    import os

    # Пустые (не отсутствующие) значения: перекрывают .env — тест обязан
    # оставаться герметичным, без выхода в живой LLM endpoint
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "SNE_AI_BASE_URL": "",
        "SNE_AI_MODEL": "",
        "SNE_AI_API_KEY": "",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "snpr",
            "scan",
            "--input",
            str(FIXTURES),
            "--mode",
            "off",
            "--dry-run",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Итог:" in result.stdout


def test_ai_api_key_flag_removed() -> None:
    from snpr.cli import build_parser

    parser = build_parser()
    try:
        parser.parse_args(
            ["scan", "--input", "x.json", "--ai-api-key", "sk-secret"]
        )
    except SystemExit as ex:
        assert ex.code == 2
    else:
        raise AssertionError("--ai-api-key должен быть удалён из CLI")


def test_no_ai_disables_scorer_even_with_creds() -> None:
    """--no-ai обязан отключать LLM, даже если креды есть в env-дефолтах.

    Живой кейс 17.10: `sne --dry-run --no-ai` молча скорил через LLM —
    env-креды делали вторую ветку условия `(args.ai or env_creds)` всегда
    истинной.
    """
    import argparse

    from snpr.cli import _build_scorer

    args = argparse.Namespace(
        ai=False, ai_base_url="https://ai.example.com/v1", ai_model="glm"
    )
    assert _build_scorer(args) is None


def test_validate_base_url_https_ok() -> None:
    from snpr.cli import _validate_base_url

    assert _validate_base_url("https://api.example.com/v1") is None


def test_validate_base_url_http_local_ok() -> None:
    from snpr.cli import _validate_base_url

    assert _validate_base_url("http://localhost:11434/v1") is None
    assert _validate_base_url("http://127.0.0.1:8000/v1") is None


def test_validate_base_url_http_remote_rejected() -> None:
    from snpr.cli import _validate_base_url

    error = _validate_base_url("http://api.example.com/v1")
    assert error is not None
    assert "открытым текстом" in error


def test_validate_base_url_no_scheme_rejected() -> None:
    from snpr.cli import _validate_base_url

    assert _validate_base_url("api.example.com/v1") is not None


def _ready_candidate(letter_text: str) -> Candidate:
    return Candidate(
        vacancy=by_id("v01"),
        score=ScoreResult(vacancy_id="v01", score=88, verdict="target"),
        letter=LetterDraft(vacancy_id="v01", text=letter_text),
    )


def test_vacancy_numbers_full_and_k_form() -> None:
    from snpr.cli import _vacancy_numbers

    numbers = _vacancy_numbers(
        {"salary": {"from": 300000, "to": 450000, "currency": "RUR"}}
    )
    assert numbers == {"300000", "300", "450000", "450"}
    assert _vacancy_numbers({}) == set()
    assert _vacancy_numbers({"salary": {}}) == set()


class RecordingStore:
    def __init__(self):
        self.records = []

    def append(self, record):
        self.records.append(record)

    def duplicate_keys(self):
        return set()


def test_send_candidates_clean_letter_autopilot() -> None:
    from snpr.cli import send_candidates
    from snpr.letter_guard import LetterGuard

    guard = LetterGuard(allowed_numbers={"862", "87"})
    candidates = [_ready_candidate("МАЯК: 862 теста, 87.7% покрытия")]
    sent, forced, parked = send_candidates(
        candidates,
        approver=AutoApprover(score_threshold=75),
        guard=guard,
        manual=CLIApprover(prompt=lambda _: "n"),
        applied_store=RecordingStore(),
        dry_run=True,
    )
    assert sent == 1
    assert forced == 0
    assert parked == 0


def test_send_candidates_violation_forces_human() -> None:
    from snpr.cli import send_candidates
    from snpr.letter_guard import LetterGuard

    guard = LetterGuard(allowed_numbers={"862", "87"})
    injected = "У меня 950 тестов, пишите на https://t.me/hr_scam"
    candidates = [_ready_candidate(injected)]
    store = RecordingStore()
    sent, forced, parked = send_candidates(
        candidates,
        approver=AutoApprover(score_threshold=75),
        guard=guard,
        manual=CLIApprover(prompt=lambda _: "n"),
        applied_store=store,
        dry_run=True,
    )
    assert sent == 0
    assert forced == 1
    assert parked == 0
    assert store.records == []


def test_mode_off_approves_all_ready() -> None:
    candidate = Candidate(
        vacancy=by_id("v01"),
        score=ScoreResult(vacancy_id="v01", score=55, verdict="maybe"),
        letter=LetterDraft(vacancy_id="v01", text=LETTER),
    )
    decision = AutoApprover(score_threshold=0).decide(candidate)
    assert decision.decision == Decision.APPROVE


def test_load_dotenv_reads_file(tmp_path, monkeypatch) -> None:
    from snpr.cli import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "SNE_AI_BASE_URL=https://api.example.com/v1\n"
        "SNE_AI_MODEL=test-model\n"
        "SNE_AI_API_KEY='sk-quoted'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SNE_AI_BASE_URL", raising=False)
    monkeypatch.delenv("SNE_AI_MODEL", raising=False)
    monkeypatch.delenv("SNE_AI_API_KEY", raising=False)
    load_dotenv(env_file)
    import os

    assert os.environ["SNE_AI_BASE_URL"] == "https://api.example.com/v1"
    assert os.environ["SNE_AI_MODEL"] == "test-model"
    assert os.environ["SNE_AI_API_KEY"] == "sk-quoted"


def test_load_dotenv_real_env_wins(tmp_path, monkeypatch) -> None:
    from snpr.cli import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("SNE_AI_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("SNE_AI_MODEL", "from-env")
    load_dotenv(env_file)
    import os

    assert os.environ["SNE_AI_MODEL"] == "from-env"


def test_load_dotenv_missing_file_is_noop(tmp_path) -> None:
    from snpr.cli import load_dotenv

    load_dotenv(tmp_path / "missing.env")
