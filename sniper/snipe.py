"""Snipe: живой поллинг новых вакансий hh → конвейер → POST /negotiations.

Библиотечный режим базы (тонкий слой): SavingHHApplicantTool из hh_tool даёт
api-клиент с токеном из auth.json, автопродлением и капчей. Поиск —
GET /vacancies (order_by=publication_time, date_from=now-N) по запросам из
config.toml [search]; полная карточка — GET /vacancies/{id}. Дедуп —
seen.jsonl (прошедшие конвейер) + applied.jsonl (выстрелы). Выстрел —
POST /negotiations {resume_id, vacancy_id, message}.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .approve import AppliedStore
from .pipeline import SnipePipeline

DEFERRED_KEEP_FIELDS = (
    "id",
    "name",
    "employer",
    "salary",
    "area",
    "work_format",
    "experience",
    "published_at",
    "key_skills",
)


def _sort_by_published_at(vacancies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Свежие первыми: скорость отклика — весь смысл снайпера."""

    def stamp(vacancy: Mapping[str, Any]) -> str:
        return str(vacancy.get("published_at") or "")

    return sorted(vacancies, key=stamp, reverse=True)


def resolve_search_hours(
    args_hours: float,
    search_cfg: Mapping[str, Any],
    marker: Path,
    now: float | None = None,
) -> float:
    """Адаптивная глубина поиска под каденцию джобы 5 минут (18.10).

    Раньше окно было всегда 24 ч. При поллинге каждые 5 минут это
    пережёвывает всю суточную выдачу каждый такт. Теперь окно = время
    с прошлого УСПЕШНОГО прогона (маркер data/last_run.txt) + 6 минут
    страховки, но не меньше min_hours и не больше hours (24 ч —
    покрытие простоев ноутбука: woke up → окно раскрылось на паузу).
    Флаг --hours жёстко задаёт окно (ручной прогон). Маркера нет
    (первый прогон) → полный cap. Аварийные прогоны маркер НЕ трогают:
    следующий раскроет окно заново.
    """
    if args_hours > 0:
        return float(args_hours)
    cap = float(search_cfg.get("hours", 24) or 24)
    floor = float(search_cfg.get("min_hours", 0.25) or 0.25)
    try:
        last = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return cap
    moment = time.time() if now is None else now
    elapsed_hours = max(0.0, (moment - last) / 3600)
    return min(cap, max(floor, elapsed_hours + 0.1))


def _touch_marker(marker: Path) -> None:
    """Маркер успешного прогона (epoch-секунды). Не критичен: сдох —
    окно просто раскроется шире, ничего не теряем."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{time.time():.0f}\n", encoding="utf-8")
    except OSError:
        pass


def _fire_max_age_hours(fire_cfg: Mapping[str, Any]) -> float | None:
    """Лимит свежести автопилота. 18.10: минуты приоритетнее
    (max_age_minutes = 10), старый ключ max_age_hours — совместимость.
    0/отсутствует = гейт выключен."""
    try:
        minutes = float(fire_cfg.get("max_age_minutes", 0) or 0)
        if minutes > 0:
            return minutes / 60.0
        hours = float(fire_cfg.get("max_age_hours", 0) or 0)
    except (TypeError, ValueError):
        return None
    return hours or None


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс-владелец лока. Windows: OpenProcess+
    GetExitCodeProcess (os.kill на винде проверку не делает — убивает)."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _lock_holder_alive(path: Path) -> bool | None:
    """None — pid не читается (старый/чужой формат): решает возраст."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").strip()
        pid = int(raw.split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return _pid_alive(pid)


def _acquire_run_lock(path: Path, stale_after: float = 1800.0) -> int | None:
    """Лок одного прогона: каденция 5 мин, но прогон при всплеске живёт
    дольше такта, тики поверх скипаются. В файл пишем pid держателя:
    если процесс мёртв (жёсткое TerminateProcess — finally не отработал),
    лок снимаем СРАЗУ на следующем тике, не ждём 30-мин стейла.
    Поймано живьём 18.10 ~01:44: догоняющий инстанс планировщика убился
    ^C, finally не отработал, лок утёк — тики скипали бы до 02:14."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), flags)
    except FileExistsError:
        holder = _lock_holder_alive(path)
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return None
        if holder is not False and age <= stale_after:
            return None
        try:
            path.unlink()
        except OSError:
            return None
        try:
            fd = os.open(str(path), flags)
        except FileExistsError:
            return None
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return fd


def _release_run_lock(path: Path, fd: int) -> None:
    """Файл лока обязан удаляться: иначе каждый следующий такт будет
    видеть «прогон идёт» до 30-минутного стейла (джоба выродится в
    раз-в-полчаса)."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass

DEFAULT_QUERIES = [
    "LLM",
    "AI инженер",
    "ML инженер",
    "NLP",
    "Data Scientist",
    "Machine Learning",
]


class SeenStore:
    """Дедуп прогонов: id вакансий, уже прошедших конвейер."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def seen_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        ids: set[str] = set()
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("vacancy_id"):
                    ids.add(str(rec["vacancy_id"]))
        return ids

    def mark_seen(
        self, vacancy_id: str, name: str = "", status: str = ""
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "vacancy_id": str(vacancy_id),
            "name": name,
            "status": status,
            "ts": int(datetime.now(timezone.utc).timestamp()),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_search_params(
    query: str,
    hours: float,
    per_page: int,
    page: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    date_from = (moment - timedelta(hours=hours)).isoformat(timespec="seconds")
    return {
        "text": query,
        "page": page,
        "per_page": per_page,
        "order_by": "publication_time",
        "date_from": date_from,
    }


class DeferredStore:
    """Очередь вакансий, не закрытых в прогоне: бюджет скоринга или сбой LLM.

    Незакрытую вакансию нельзя помечать seen (иначе потеряем), а к следующему
    прогону она уже может выпасть из окна date_from — очередь сохраняет её
    между запусками. Отложенные по бюджету и упавшие по LLM лежат вместе:
    обе категории не закрыты, обе надо доскорить.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        items: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("id"):
                    items.append(rec)
        return items

    def save(self, vacancies: Iterable[Mapping[str, Any]]) -> int:
        rows = [
            {k: v for k, v in vacancy.items() if k in DEFERRED_KEEP_FIELDS}
            for vacancy in vacancies
            if vacancy.get("id")
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
        return len(rows)


class ParkedStore:
    """Готовые к выстрелу вакансии из запланированных прогонов.

    `snipe --mode park` не стреляет и не отказывает: ready-кандидат
    (вакансия + скор + письмо) ложится сюда целиком и ждёт решения
    человека — `sniper approve`. В seen не пишем: вакансия не закрыта,
    потерять нельзя.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def ids(self) -> set[str]:
        return {str(rec["vacancy"].get("id")) for rec in self.load_records()}

    def load_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                vacancy = rec.get("vacancy") if isinstance(rec, dict) else None
                if isinstance(vacancy, dict) and vacancy.get("id"):
                    records.append(rec)
        return records

    def append_record(
        self,
        vacancy: Mapping[str, Any],
        score: Any = None,
        letter: str = "",
    ) -> None:
        score_snapshot = None
        if score is not None:
            score_snapshot = {
                "score": score.score,
                "verdict": score.verdict,
                "reasons": list(score.reasons),
            }
        record = {
            "vacancy": dict(vacancy),
            "score": score_snapshot,
            "letter": str(letter or ""),
            "ts": int(datetime.now(timezone.utc).timestamp()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def remove(self, vids: Iterable[str]) -> int:
        """Убрать закрытые (выстрел/отказ) — парковка живёт до решения."""
        drop = {str(v) for v in vids}
        records = [
            rec
            for rec in self.load_records()
            if str(rec["vacancy"].get("id")) not in drop
        ]
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
        return len(drop)


class ScoresStore:
    """Журнал чисел скоринга: каждая проскоренная вакансия — строка.

    seen.jsonl хранит только вердикт-статус (pass/ready), числа в нём
    нет. Аудит «какие скоры вообще бывают» и калибровка порогов
    (maybe/target/автопилот) — по этому журналу, append-only.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def append_record(
        self, vacancy: Mapping[str, Any], score: Any, ts: int | None = None
    ) -> None:
        record = {
            "vacancy_id": str(vacancy.get("id") or ""),
            "name": vacancy.get("name", ""),
            "employer": (vacancy.get("employer") or {}).get("name", ""),
            "score": int(score.score),
            "verdict": str(score.verdict),
            "reasons": list(score.reasons)[:3],
            "ts": int(ts if ts is not None else datetime.now(timezone.utc).timestamp()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("vacancy_id"):
                    records.append(rec)
        return records

    def ids(self) -> set[str]:
        return {str(rec.get("vacancy_id")) for rec in self.load()}


def record_scores(
    scores_store: ScoresStore, candidates: Iterable[Any], ts: int | None = None
) -> int:
    """Записать в журнал все проскоренные кандидаты прогона.

    Пишется на моменте скоринга (не закрытия): pass и parked фиксируются
    с числами, deferred/error без скора не пишутся. Повторный скоринг
    той же вакансии (после очереди) — новая строка: дрейф скора виден.
    """
    count = 0
    for candidate in candidates:
        if candidate.score is None or not candidate.vacancy_id:
            continue
        scores_store.append_record(candidate.vacancy, candidate.score, ts=ts)
        count += 1
    return count


class HHVacancyProvider:
    """Поиск новых вакансий через api-клиент базы.

    Отдаёт короткие items из выдачи (/vacancies), а не полные карточки:
    дешёвые гейты (гео/вилка/грейд/вендор по работодателю) работают по полям
    выдачи — их там хватает (name, employer, salary, area, work_format,
    experience). Полная карточка нужна только выжившим, её берёт ленивый
    VacancyCardCache — на пул в сотни вакансий это десятки GET вместо сотен.
    """

    def __init__(self, api_client: Any, per_page: int = 50):
        self.api_client = api_client
        self.per_page = per_page

    def search_new(
        self,
        queries: list[str],
        hours: float,
        known_ids: set[str],
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Короткие items новых вакансий; дедуп по known_ids и внутри прогона.

        Страницы обходятся round-robin по запросам (иначе первый запрос съедает
        весь потолок), порядок — свежее первым (order_by=publication_time).
        """
        collected: list[dict[str, Any]] = []
        seen_now: set[str] = set(known_ids)

        def collect(items: Any) -> bool:
            """Добавить новые items; False — потолок достигнут."""
            for short in items if isinstance(items, list) else []:
                vid = str(short.get("id") or "")
                if not vid or vid in seen_now:
                    continue
                seen_now.add(vid)
                collected.append(short)
                if limit > 0 and len(collected) >= limit:
                    return False
            return True

        pages_left: dict[str, int] = {}
        for query in queries:
            res = self.api_client.get(
                "/vacancies", build_search_params(query, hours, self.per_page)
            )
            if not isinstance(res, dict):
                pages_left[query] = 1
                continue
            pages_left[query] = max(int(res.get("pages") or 1), 1)
            if not collect(res.get("items", [])):
                break

        page = 1
        while limit <= 0 or len(collected) < limit:
            progressed = False
            for query in queries:
                if page >= pages_left.get(query, 0):
                    continue
                res = self.api_client.get(
                    "/vacancies",
                    build_search_params(query, hours, self.per_page, page),
                )
                if not isinstance(res, dict):
                    continue
                progressed = True
                if not collect(res.get("items", [])):
                    break
            if not progressed:
                break
            page += 1

        return _sort_by_published_at(collected)


class VacancyCardCache:
    """Ленивая полная карточка: description нужен только выжившим гейтов."""

    def __init__(self, api_client: Any):
        self.api_client = api_client
        self.cards: dict[str, dict[str, Any]] = {}
        self.calls = 0

    def description(self, vacancy: Mapping[str, Any]) -> str:
        vid = str(vacancy.get("id") or "")
        card = self.cards.get(vid)
        if card is None:
            self.calls += 1
            fetched = self.api_client.get(f"/vacancies/{vid}")
            card = fetched if isinstance(fetched, dict) else {}
            self.cards[vid] = card
        # короткий item из выдачи дополняем полями карточки для промпта
        if isinstance(vacancy, dict):
            for field in ("key_skills", "branding"):
                if card.get(field):
                    vacancy[field] = card[field]
        return str(card.get("description") or "")


class HHNegotiationShooter:
    """Реальный выстрел: POST /negotiations (резюме + письмо)."""

    def __init__(self, api_client: Any, resume_id: str):
        self.api_client = api_client
        self.resume_id = resume_id

    def fire(self, vacancy_id: str, letter: str) -> None:
        res = self.api_client.post(
            "/negotiations",
            {
                "resume_id": self.resume_id,
                "vacancy_id": vacancy_id,
                "message": letter,
            },
            delay=random.uniform(1, 3),
        )
        if res != {}:
            raise RuntimeError(f"неожиданный ответ /negotiations: {res!r:.200}")


RETRY_STATUSES = {"error", "deferred"}


def mark_seen(seen_store: SeenStore, candidates: Iterable[Any]) -> int:
    """Пометить обработанные вакансии; незакрытые — нет.

    Незакрытые: error/deferred (вернутся очередью) и parked (ждут
    решения человека в data/parked.jsonl). Иначе вакансия навсегда
    выпадет из выдачи.
    """
    marked = 0
    for candidate in candidates:
        if candidate.status in RETRY_STATUSES or candidate.status == "parked":
            continue
        seen_store.mark_seen(
            candidate.vacancy_id,
            name=str(candidate.vacancy.get("name") or ""),
            status=candidate.status,
        )
        marked += 1
    return marked


def requeue_candidates(
    deferred_store: DeferredStore, candidates: Iterable[Any]
) -> int:
    """Вернуть незакрытые вакансии в очередь; закрытые из неё убрать.

    Кейс из живого прогона 17.10: LLM лежал, вакансия из очереди получила
    статус error, из очереди выпала — и жила только до выхода из 24-часового
    окна поиска. Дальше — потеря навсегда. Теперь очередь — страховка для
    обеих категорий: не хватило бюджета или скоринг упал.
    """
    return deferred_store.save(
        candidate.vacancy
        for candidate in candidates
        if candidate.status in RETRY_STATUSES
    )


def parse_vacancy_ref(ref: str) -> str:
    """ID вакансии из аргумента: голый ID или ссылка hh.ru/vacancy/<id>."""
    text = str(ref or "").strip()
    if not text:
        return ""
    if "/" in text:
        text = text.rstrip("/").rsplit("/", 1)[-1]
    text = text.split("?")[0].split("#")[0]
    return "".join(ch for ch in text if ch.isdigit())


def _seen_record(path: Path, vid: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(rec.get("vacancy_id", "")) == vid:
                return rec
    return None


def _search_coverage(
    api: Any,
    queries: list[str],
    vid: str,
    published_at: str,
    per_page: int = 100,
    max_pages: int = 5,
) -> list[str] | None:
    """Какие из запросов находят вакансию (None — не проверялось).

    Окно — от публикации вакансии (минус час), обход свежие-первыми,
    потолок max_pages на запрос: диагностика не должна гонять сотни вызовов.
    """
    try:
        published = datetime.fromisoformat(str(published_at or ""))
    except ValueError:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    date_from = (published - timedelta(hours=1)).isoformat(timespec="seconds")
    floor = published - timedelta(hours=1)
    found_by: list[str] = []
    for query in queries:
        for page in range(max_pages):
            res = api.get(
                "/vacancies",
                {
                    "text": query,
                    "page": page,
                    "per_page": per_page,
                    "order_by": "publication_time",
                    "date_from": date_from,
                },
            )
            if not isinstance(res, dict):
                break
            items = res.get("items", [])
            if not items:
                break
            if any(str(item.get("id", "")) == vid for item in items):
                found_by.append(query)
                break
            try:
                oldest = datetime.fromisoformat(
                    str(items[-1].get("published_at") or "")
                )
            except ValueError:
                continue
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            if oldest < floor:
                break
    return found_by


def explain_vacancy(
    api: Any,
    config: Mapping[str, Any],
    vid: str,
    seen_path: Path,
    applied_path: Path,
    deferred_path: Path,
    queries: list[str],
) -> list[str]:
    """Полный разбор воронки для одной вакансии: где выпала и почему.

    Точки потери по порядку: поисковые запросы → дедуп сторов → гейты →
    вердикт скоринга (pass < maybe < target — письмо только с maybe).
    Только чтение: диагностика ничего не пишет в сторы.
    """
    from .filters import PipelineFilter

    lines: list[str] = []
    card = api.get(f"/vacancies/{vid}")
    if isinstance(card, dict) and card.get("id"):
        name = str(card.get("name") or "")
        employer = str((card.get("employer") or {}).get("name") or "")
        lines.append(f"Вакансия {vid} — {name} — {employer}")
        lines.append(f"Опубликовано: {card.get('published_at') or '?'}")
        lines.append(f"Ссылка: https://hh.ru/vacancy/{vid}")
    else:
        card = None
        lines.append(f"Вакансия {vid}: карточка недоступна (закрыта/снята?)")
    lines.append("")

    seen_rec = _seen_record(Path(seen_path), vid)
    applied_ids = AppliedStore(Path(applied_path)).vacancy_ids()
    queued_ids = {
        str(item.get("id", ""))
        for item in DeferredStore(Path(deferred_path)).load()
    }
    lines.append("Сторы:")
    if seen_rec:
        ts = int(seen_rec.get("ts") or 0)
        when = (
            datetime.fromtimestamp(ts).astimezone().strftime("%d.%m %H:%M")
            if ts
            else "?"
        )
        lines.append(
            f"  seen:    да — {when}, статус {seen_rec.get('status', '?')}"
        )
    else:
        lines.append("  seen:    нет — конвейер её не завершал")
    lines.append(
        f"  applied: {'да — выстрел уже был' if vid in applied_ids else 'нет'}"
    )
    lines.append(
        "  очередь: "
        + ("да — доскорим в следующем прогоне" if vid in queued_ids else "нет")
    )
    lines.append("")

    gates_line = ""
    if card is not None:
        description = str(card.get("description") or "")
        verdict = PipelineFilter(config).check(card, description=description)
        lines.append("Гейты по свежей карточке:")
        for rule in verdict.rules:
            mark = "OK  " if rule.passed else "FAIL"
            lines.append(f"  {rule.rule:7} {mark} {rule.reason}")
        failed = sorted({r.rule for r in verdict.rules if not r.passed})
        gates_line = (
            "гейты забивают (" + ", ".join(failed) + ")"
            if failed
            else "гейты пропускают"
        )
        lines.append(f"  → {gates_line}")
        lines.append("")

    lines.append("Вердикт:")
    if vid in applied_ids:
        lines.append("  уже отозвались (applied) — повторного выстрела не будет")
    elif seen_rec:
        status = str(seen_rec.get("status") or "?")
        note = {
            "filtered_out": "гейты забили её в том прогоне",
            "pass": "скоринг дал pass — ниже порога письма (maybe)",
            "error": "скоринг упал — вернётся из очереди",
        }.get(status, "")
        if note:
            lines.append(f"  конвейер уже обработал её ({status}): {note}")
        if gates_line:
            lines.append(f"  сейчас по карточке: {gates_line}")
        lines.append(
            "  дедуп не даст ей вернуться в пул; если считаете таргетом — "
            "это спор гейтов/скоринга, а не потери"
        )
    elif vid in queued_ids:
        lines.append("  в очереди deferred — доскорим в следующих прогонах")
    elif card is None:
        lines.append("  карточка недоступна и в сторах её нет — до бота не дошла")
    else:
        coverage = _search_coverage(
            api, queries, vid, str(card.get("published_at") or "")
        )
        if "забивают" in gates_line:
            lines.append(f"  в сторах нет, но {gates_line} — упала бы на фильтрах")
        elif coverage is None:
            lines.append("  в сторах нет, гейты пропускают; дата публикации "
                         "не разобрана — покрытие поиска не проверено")
        elif coverage:
            lines.append(
                f"  в сторах нет, гейты пропускают, поиск находит "
                f"(запросы: {', '.join(coverage)}) — должна была попасть в "
                "пул: не хватило потолка max_new_per_run или прогон не "
                "случился в её окно публикации"
            )
        else:
            lines.append(
                f"  ни один из {len(queries)} запросов поиска её не находит — "
                "в потеря ДО бота: text-поиск hh не матчит, нужно расширять "
                "[search].queries"
            )
    return lines


def run_why(args: argparse.Namespace) -> int:
    from .cli import (
        DEFAULT_APPLIED_STORE,
        DEFAULT_DEFERRED_STORE,
        DEFAULT_SEEN_STORE,
        _load_toml,
    )
    from .hh_tool import SavingHHApplicantTool

    vid = parse_vacancy_ref(getattr(args, "vacancy", ""))
    if not vid:
        print(
            "Не разобрать ID вакансии: передайте ID или ссылку "
            "https://hh.ru/vacancy/<id>",
            file=sys.stderr,
        )
        return 2
    config = _load_toml(args.config)
    search_cfg = config.get("search", {})
    queries = (
        list(args.queries)
        if args.queries
        else list(search_cfg.get("queries", DEFAULT_QUERIES))
    )
    tool = SavingHHApplicantTool()
    lines = explain_vacancy(
        tool.api_client,
        config,
        vid,
        Path(args.seen_store),
        Path(args.applied_store),
        Path(args.deferred_store),
        queries,
    )
    print("\n".join(lines))
    return 0


def pick_resume_id(api_client: Any, preferred: str = "") -> str:
    """ID резюме для отклика: явно заданное или первое на аккаунте."""
    if preferred:
        return preferred
    res = api_client.get("/resumes/mine")
    items = res.get("items", []) if isinstance(res, dict) else []
    for item in items:
        if item.get("id"):
            return str(item["id"])
    raise RuntimeError(
        "На аккаунте не найдено резюме — POST /negotiations невозможен"
    )


def run_snipe(args: argparse.Namespace) -> int:
    """Обёртка: лок против параллельных прогонов (каденция 5 мин).

    Task Scheduler тики поверх работающего прогона пропускает сам
    (IgnoreNew); ручной запуск поверх джобы ловим лок-файлом.
    """
    lock_path = Path(args.seen_store).parent / "snipe.lock"
    lock = _acquire_run_lock(lock_path)
    if lock is None:
        print(
            "Другой прогон snipe ещё работает — такт пропущен "
            "(окно адаптивное, ничего не теряем)."
        )
        return 0
    try:
        return _run_snipe_locked(args)
    finally:
        _release_run_lock(lock_path, lock)


def _run_snipe_locked(args: argparse.Namespace) -> int:
    from .filters import PipelineFilter
    from .hh_tool import SavingHHApplicantTool

    from .cli import (
        DEFAULT_DEFERRED_STORE,
        DEFAULT_PARKED_STORE,
        DEFAULT_SCORES_STORE,
        _build_scorers,
        _letter_static_text,
        _load_toml,
        _make_approver,
        _make_guard,
        _scoring_profile,
        fire_candidates,
        print_summary,
        report_candidates,
    )

    config = _load_toml(args.config)
    search_cfg = config.get("search", {})
    scoring_cfg = config.get("scoring", {})
    marker = Path(args.seen_store).parent / "last_run.txt"
    hours = resolve_search_hours(args.hours, search_cfg, marker)
    queries = (
        list(args.queries)
        if args.queries
        else list(search_cfg.get("queries", DEFAULT_QUERIES))
    )
    per_page = int(search_cfg.get("per_page", 50))
    limit = (
        args.limit
        if args.limit > 0
        else int(search_cfg.get("max_new_per_run", 200))
    )
    score_budget = (
        args.max_scored
        if args.max_scored > 0
        else int(search_cfg.get("max_scored_per_run", 0))
    )
    workers = (
        args.scoring_workers
        if args.scoring_workers > 0
        else int(scoring_cfg.get("workers", 1))
    )
    if not queries:
        print("Нет запросов поиска: config [search].queries пуст", file=sys.stderr)
        return 2

    tool = SavingHHApplicantTool()
    api = tool.api_client

    seen_store = SeenStore(Path(args.seen_store))
    applied_store = AppliedStore(Path(args.applied_store))
    deferred_store = DeferredStore(Path(args.deferred_store))
    parked_store = ParkedStore(
        Path(getattr(args, "parked_store", DEFAULT_PARKED_STORE))
    )
    scores_store = ScoresStore(
        Path(getattr(args, "scores_store", DEFAULT_SCORES_STORE))
    )
    known_ids = (
        seen_store.seen_ids() | applied_store.vacancy_ids() | parked_store.ids()
    )

    print(
        f"Поиск: {len(queries)} запросов, глубина {hours:.1f} ч, пул {limit}"
        + (f", бюджет скоринга {score_budget}" if score_budget else ", скоринг без потолка")
    )
    provider = HHVacancyProvider(api, per_page=per_page)
    fresh = provider.search_new(queries, hours, known_ids, limit)

    # очередь с прошлых прогонов: незакрытые вакансии могли выпасть из окна
    # date_from, поэтому возвращаем их в пул (свежие приоритетнее — сортировка)
    queued = [
        item
        for item in deferred_store.load()
        if str(item.get("id") or "") not in known_ids
    ]
    by_id: dict[str, dict[str, Any]] = {
        str(item.get("id")): item for item in fresh if item.get("id")
    }
    for item in queued:
        vid = str(item.get("id"))
        if vid and vid not in by_id:
            by_id[vid] = item
    new_vacancies = _sort_by_published_at(list(by_id.values()))[:limit]

    print(
        f"Новых вакансий после дедупа: {len(new_vacancies)} "
        f"(из поиска {len(fresh)}, из очереди {len(queued)})"
    )
    if not new_vacancies:
        if not args.dry_run:
            # пустой такт тоже успешен: сжимаем окно до floor
            _touch_marker(marker)
        return 0

    try:
        resume_id = pick_resume_id(api, args.resume_id)
    except RuntimeError as ex:
        print(f"Ошибка: {ex}", file=sys.stderr)
        return 2
    print(f"Резюме для выстрела: {resume_id}")

    from .cover_letter import StaticLetterWriter

    scorers = _build_scorers(
        args,
        workers,
        maybe_threshold=int(scoring_cfg.get("maybe_min", 50)),
        target_threshold=int(scoring_cfg.get("target_min", 75)),
        profile=_scoring_profile(config),
    )
    pipeline = SnipePipeline(
        PipelineFilter(config),
        scorers=scorers,
        writer=StaticLetterWriter(text=_letter_static_text(config)),
        score_budget=score_budget,
    )
    cards = VacancyCardCache(api)
    from .pipeline import PipelineAbort

    def _progress(done: int, total: int) -> None:
        """Прогресс скоринга: каждая 10-я завершённая попытка и финал."""
        if done % 10 and done != total:
            return
        suffix = f"/{total}" if total > 0 else ""
        print(f"  скоринг: {done}{suffix}")

    try:
        candidates = pipeline.run(
            new_vacancies,
            descriptions=cards.description,
            on_progress=_progress,
        )
    except PipelineAbort as ex:
        print(f"Прогон прерван: {ex}", file=sys.stderr)
        return 3
    stats = report_candidates(candidates)
    if cards.calls:
        print(f"Полных карточек забрано: {cards.calls} (только для выживших)")

    shooter = None if args.dry_run else HHNegotiationShooter(api, resume_id)
    from .approve import CLIApprover

    fired, forced_human, parked = fire_candidates(
        candidates,
        approver=_make_approver(
            args,
            auto_park_threshold=int(scoring_cfg.get("auto_approve_min", 0)),
            max_age_hours=_fire_max_age_hours(config.get("fire", {})),
        ),
        guard=_make_guard(config),
        manual=CLIApprover(),
        applied_store=applied_store,
        dry_run=args.dry_run,
        shooter=shooter,
        parked_store=parked_store,
    )

    if not args.dry_run:
        recorded = record_scores(scores_store, candidates)
        queued_back = requeue_candidates(deferred_store, candidates)
        mark_seen(seen_store, candidates)
        if recorded:
            print(f"Скоры записаны в журнал: {recorded} ({scores_store.path})")
        if queued_back:
            print(
                f"В очереди {queued_back} вакансий (не хватило бюджета скоринга "
                "или упал LLM): в seen не помечены, вернёмся в следующих прогонах."
            )
        if parked:
            print(
                f"В ПАРКОВКЕ {parked} готовых к выстрелу: решение за человеком — "
                "`python -m sniper approve` (y/n/e по каждой)."
            )
        tool._persist()
        _touch_marker(marker)

    print()
    print_summary(len(candidates), stats, fired, forced_human, args.dry_run)
    return 0


def run_approve(args: argparse.Namespace) -> int:
    """Решение по припаркованным: карточка → y/n/e → выстрел/закрытие.

    Дополнение к запланированным прогонам (`snipe --mode park`, джоба
    каждые 5 минут с 18.10): джоба скорит без человека, автопилот бьёт
    сам от auto_approve_min, зона ниже копится в data/parked.jsonl —
    здесь человек вводит код: y — выстрел, n — закрыть, e — правка
    письма. EOF/закрытие окна — НЕ решение: остальное остаётся.
    """
    from .hh_tool import SavingHHApplicantTool

    from .cli import (
        DEFAULT_PARKED_STORE,
        _load_toml,
        _make_guard,
        _vacancy_numbers,
    )
    from .approve import AppliedRecord, _format_card
    from .cover_letter import LetterDraft
    from .pipeline import Candidate
    from .scoring import ScoreResult

    config = _load_toml(args.config)
    parked_store = ParkedStore(
        Path(getattr(args, "parked_store", DEFAULT_PARKED_STORE))
    )
    records = parked_store.load_records()
    if not records:
        print("В парковке пусто: готовых к выстрелу вакансий нет.")
        return 0

    tool = SavingHHApplicantTool()
    api = tool.api_client
    try:
        resume_id = pick_resume_id(api, args.resume_id)
    except RuntimeError as ex:
        print(f"Ошибка: {ex}", file=sys.stderr)
        return 2

    seen_store = SeenStore(Path(args.seen_store))
    applied_store = AppliedStore(Path(args.applied_store))
    guard = _make_guard(config)
    shooter = None if args.dry_run else HHNegotiationShooter(api, resume_id)

    print("\a")  # звуковой сигнал: пора вводить код
    print(f"В парковке {len(records)} вакансий — решение за вами.")
    closed: list[str] = []
    fired = 0

    for rec in records:
        vacancy = rec["vacancy"]
        vid = str(vacancy.get("id") or "")
        score_data = rec.get("score") if isinstance(rec.get("score"), dict) else {}
        score = (
            ScoreResult(
                vacancy_id=vid,
                score=int(score_data.get("score") or 0),
                verdict=str(score_data.get("verdict") or ""),
                reasons=[str(r) for r in score_data.get("reasons") or []],
            )
            if score_data
            else None
        )
        candidate = Candidate(
            vacancy=vacancy,
            score=score,
            letter=LetterDraft(vacancy_id=vid, text=str(rec.get("letter") or "")),
        )

        # свежая карточка: за время паузы вакансия могла уйти в архив
        card: dict[str, Any] = {}
        if not args.dry_run:
            try:
                fetched = api.get(f"/vacancies/{vid}")
                card = fetched if isinstance(fetched, dict) else {}
            except Exception as ex:  # noqa: BLE001
                print(f"[{vid}] карточка недоступна ({ex}) — остаётся в парковке")
                continue
        if card.get("archived"):
            print(f"[{vid}] {vacancy.get('name', '')} — вакансия в архиве, "
                  "выстрел невозможен, закрываю.")
            seen_store.mark_seen(
                vid, name=str(vacancy.get("name") or ""), status="archived"
            )
            closed.append(vid)
            continue

        letter_text = str(rec.get("letter") or "")
        violations = guard.check(
            letter_text, extra_numbers=_vacancy_numbers(card if card else vacancy)
        )

        print()
        print(_format_card(candidate))
        if violations:
            print("ПРОВЕРКА ПИСЬМА — НАРУШЕНИЯ (решение за вами):")
            for violation in violations:
                print(f"  - {violation}")
        try:
            answer = input(
                "Отправить? [y — да / n — нет / e — правка письма] (Enter = n): "
            ).strip().lower()
        except EOFError:
            print("\nВвод закрыт — оставшиеся вакансии ждут в парковке.")
            break

        if answer not in ("y", "д", "yes", "да", "e", "у", "edit", "правка"):
            # отказ человека = закрыть навсегда (не потерять в выдаче)
            if not args.dry_run:
                seen_store.mark_seen(
                    vid, name=str(vacancy.get("name") or ""), status="ready"
                )
                closed.append(vid)
            print("  → закрыто без выстрела.")
            continue

        letter = letter_text
        if answer in ("e", "у", "edit", "правка"):
            try:
                new_letter = input("Новый текст письма (одной строкой): ")
            except EOFError:
                print("\nВвод закрыт — оставшиеся вакансии ждут в парковке.")
                break
            letter = new_letter.strip() or letter_text
            if new_letter.strip():
                edit_violations = guard.check(
                    letter, extra_numbers=_vacancy_numbers(card if card else vacancy)
                )
                if edit_violations:
                    print("  правка НЕ принята (нарушения LetterGuard):")
                    for violation in edit_violations:
                        print(f"    - {violation}")
                    letter = letter_text

        if not args.dry_run and shooter is not None:
            try:
                shooter.fire(vid, letter)
            except Exception as ex:  # noqa: BLE001
                from hh_applicant_tool.api.errors import LimitExceeded

                if isinstance(ex, LimitExceeded):
                    print("Дневной лимит откликов hh исчерпан — стоп.")
                    break
                print(f"  [{vid}] выстрел не прошёл ({ex}) — остаётся в парковке.")
                continue

        if not args.dry_run:
            applied_store.append(
                AppliedRecord(
                    vacancy_id=vid,
                    vacancy_name=str(vacancy.get("name") or ""),
                    employer=str((vacancy.get("employer") or {}).get("name") or ""),
                    score=score.score if score else 0,
                    letter=letter,
                    decision="approve",
                    dry_run=False,
                )
            )
            seen_store.mark_seen(
                vid, name=str(vacancy.get("name") or ""), status="ready"
            )
            closed.append(vid)
        fired += 1
        print("  → ВЫСТРЕЛ ПРОИЗВЕДЁН." if not args.dry_run else "  → [dry-run] выстрел.")

    parked_store.remove(closed)
    if not args.dry_run:
        tool._persist()
    left = len(parked_store.load_records())
    print()
    print(
        f"Итог: закрыто {len(closed)} (выстрелов {fired}), "
        f"в парковке осталось {left}"
    )
    return 0
