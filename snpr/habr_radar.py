"""Хабр-Карьера радар: второй источник вакансий (фаза 2 мультиканала).

hh остаётся снайпером — полный цикл с автопуском; Хабр — радаром: тот же
LLM-скоринг профилем [scoring], а maybe/target уходят карточкой в Telegram
со ссылкой на вакансию. Отклик руками: API откликов у Хабра нет.

Канонический формат вакансии (фаза 0, свёрнутая сюда) — hh-подобный
словарь: id, name, employer.name, salary{from,to,currency}, area.name,
published_at, key_skills[]. Фильтры, скорер и сторы конвейера работают
с ним без изменений; id с префиксом «habr:» не пересекается с числовыми
id hh. Формальный Source-протокол отложен до третьего источника
(getmatch): сейчас конвейер и так source-агностичен по формату данных.

Источник — публичный RSS без антибота:
https://career.habr.com/vacancies/rss?q=<запрос> — до 50 последних
вакансий на запрос. title: «Требуется „Name" (Город, от X ₽)»;
description — HTML-сниппет: «Компания „X" ищет... Москва (Россия).
Полный рабочий день. Можно удалённо. Требуемые навыки: #middle, #REST,
...». Хэштег грейда в сниппете — дешёвый префильтр ДО LLM (аналог
[grade] на стороне hh): #intern/#junior режутся по тегу. Полной
карточки в RSS нет, страница вакансии — SPA без API: скоринг идёт по
сниппету (название + вилка + город + навыки LLM видит в промпте).

Дедуп: свой (id «habr:» в общем seen.jsonl) + кросс против hh —
(название, работодатель) нормализованные из scores.jsonl / applied /
parked: вакансия, уже отработанная снайпером hh, карточкой радара
не дублируется. Каденция: [habr].interval_minutes, маркер
data/habr_last.txt. Тихий хук в конце такта снпрера (после hh-фазы
и Telegram-итогов); сбой радара прогон снпрера не роняет.

CLI: python -m snpr habr [--dry-run] [--limit N]
"""

from __future__ import annotations

import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .cli import (
    DEFAULT_APPLIED_STORE,
    DEFAULT_PARKED_STORE,
    DEFAULT_SCORES_STORE,
    DEFAULT_SEEN_STORE,
)
from .filters import PipelineFilter
from .pipeline import SnePipeline
from .scoring import ScoreResult, _strip_html

HABR_RSS = "https://career.habr.com/vacancies/rss"
SOURCE_TAG = "habr"
DEFAULT_EXCLUDE_TAGS = ["intern", "junior", "trainee", "graduate"]
DEFAULT_INTERVAL_MINUTES = 15.0
DEFAULT_MAX_AGE_HOURS = 26.0
DEFAULT_MAX_PER_RUN = 6


@dataclass
class RadarPaths:
    """Сторы и маркер радара: в такте — боевые, в тестах — tmp."""

    seen: Path
    scores: Path
    applied: Path
    parked: Path
    marker: Path


def radar_paths_from_args(args: Any) -> RadarPaths:
    """Пути из аргументов (уважает --seen-store и прочие override)."""
    seen = Path(str(getattr(args, "seen_store", DEFAULT_SEEN_STORE)))
    return RadarPaths(
        seen=seen,
        scores=Path(str(getattr(args, "scores_store", DEFAULT_SCORES_STORE))),
        applied=Path(str(getattr(args, "applied_store", DEFAULT_APPLIED_STORE))),
        parked=Path(str(getattr(args, "parked_store", DEFAULT_PARKED_STORE))),
        marker=seen.parent / "habr_last.txt",
    )


# --- фетч фида -----------------------------------------------------------------


def _http_get(url: str, timeout: float = 30.0) -> str:
    """GET как есть (отдельная функция — точка подмены в тестах)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (snpr-habr-radar)"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _item_fields(item: ET.Element) -> dict[str, str]:
    fields: dict[str, str] = {}
    for child in item:
        tag = child.tag.split("}")[-1]
        fields[tag] = (child.text or "").strip()
    return fields


def _pubdate_iso(value: str) -> str:
    """RFC822 («Tue, 20 Oct 2026 21:18:23 +0300») → ISO с зоной."""
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ""
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


def fetch_rss_items(
    queries: Iterable[str], timeout: float = 30.0
) -> list[dict[str, str]]:
    """Позиции фида по запросам: объединение, дедуп по guid, свежие сверху.

    Часть запросов упала — работаем с остальными (RSS без антибота, но
    сеть бывает); все упали — исключение, вызовущий хук напечатает и
    живёт дальше (маркер не тронут — следующая попытка в свой срок).
    """
    merged: dict[str, dict[str, str]] = {}
    failures: list[str] = []
    for query in queries:
        url = f"{HABR_RSS}?" + urllib.parse.urlencode({"q": query})
        try:
            root = ET.fromstring(_http_get(url, timeout))
        except Exception as ex:  # noqa: BLE001
            failures.append(f"{query}: {type(ex).__name__}")
            continue
        for item in root.findall(".//item"):
            fields = _item_fields(item)
            guid = fields.get("guid") or ""
            if guid and guid not in merged:
                fields["_iso"] = _pubdate_iso(fields.get("pubDate", ""))
                merged[guid] = fields
    if failures and not merged:
        raise RuntimeError("RSS Хабра недоступен: " + "; ".join(failures))
    if failures:
        print(f"[habr] не забрались запросы: {'; '.join(failures)}")
    items = list(merged.values())
    items.sort(key=lambda f: f.get("_iso") or "", reverse=True)
    return items


# --- разбор позиции фида ---------------------------------------------------------


_NAME_RE = re.compile(r"«([^»]{2,200})»")
_MONEY_RE = re.compile(
    r"(от|до)?\s*(\d[\d\s\u00a0]{0,12}?)\s*(?:до\s+(\d[\d\s\u00a0]{0,12}?))?"
    r"\s*(₽|руб[а-яё.]*|\$|€)",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"#([A-Za-z0-9_][A-Za-z0-9_-]*)")
_AREA_RE = re.compile(r"»\.\s*([^.]{2,80})\.")
_REMOTE_RE = re.compile(r"(?i)удал[её]нн")
_RUSSIA_RE = re.compile(r"\s*\((?:Россия|Russia)\)\s*$")


def _num(raw: str | None) -> int | None:
    digits = re.sub(r"\D", "", raw or "")
    return int(digits) if digits else None


def _currency(symbol: str) -> str:
    if symbol.startswith(("₽", "руб")):
        return "RUR"
    if symbol == "$":
        return "USD"
    return "EUR"


def _parse_salary(text: str) -> dict[str, Any] | None:
    """Вилка из скобки title: «от 230 000 ₽», «от 800 до 1 120 $».

    gross = true: у Хабра не уточнено — считаем как hh (до вычета),
    тот же расчёт net в salary-гейте конвейера.
    """
    match = _MONEY_RE.search(text or "")
    if not match:
        return None
    first = _num(match.group(2))
    second = _num(match.group(3))
    currency = _currency(match.group(4))
    if match.group(1) and match.group(1).lower() == "до" and second is None:
        first, second = None, first
    if first is None and second is None:
        return None
    return {
        "from": first,
        "to": second,
        "currency": currency,
        "gross": True,
    }


def parse_title(title: str) -> tuple[str, dict[str, Any] | None]:
    """Название и вилка из title фида («Требуется „Name" (Москва, от X ₽)»)."""
    raw = (title or "").strip()
    match = _NAME_RE.search(raw)
    if match:
        name = match.group(1).strip()
    else:
        name = re.sub(r"^Требуется\s+", "", raw)
        name = name.split(" (")[0].strip() or raw
    return name, _parse_salary(raw)


def extract_tags(description: str) -> list[str]:
    """Хэштеги навыков из сниппета («Требуемые навыки: #middle, #REST...»)."""
    return [tag.lower() for tag in _TAG_RE.findall(description or "")]


def _parse_area(description: str) -> str:
    """Город из сниппета: предложение после «...вакансию „Name".»."""
    match = _AREA_RE.search(description or "")
    if not match:
        return ""
    area = _RUSSIA_RE.sub("", match.group(1).strip()).strip()
    return area[:60]


def map_item(item: Mapping[str, str]) -> dict[str, Any]:
    """Позиция фида → hh-подобный словарь конвейера.

    id с префиксом «habr:» — дедуп общий со снпрером, пересечений с
    числовыми id hh нет. key_skills — из хэштегов сниппета: скорер
    кладёт их в промпт той же строкой, что навыки hh.
    """
    name, salary = parse_title(item.get("title", ""))
    description = str(item.get("description") or "")
    guid = str(item.get("guid") or "")
    tags = extract_tags(description)
    vacancy: dict[str, Any] = {
        "id": f"{SOURCE_TAG}:{guid}",
        "name": name,
        "employer": {"id": "", "name": str(item.get("author") or "")},
        "salary": salary or {},
        "area": {"id": "", "name": _parse_area(description)},
        "published_at": _pubdate_iso(str(item.get("pubDate") or "")),
        "key_skills": [{"name": tag} for tag in tags],
        "work_format": (
            [{"id": "REMOTE"}] if _REMOTE_RE.search(description) else []
        ),
        "archived": False,
        "apply_url": str(item.get("link") or ""),
        "source": SOURCE_TAG,
    }
    return vacancy


# --- префильтры до LLM ------------------------------------------------------------


def _norm_key(vacancy: Mapping[str, Any]) -> tuple[str, str]:
    """Ключ кросс-дедупа: (название, работодатель), нормализованный."""
    return (
        str(vacancy.get("name") or "").strip().lower(),
        str((vacancy.get("employer") or {}).get("name") or "").strip().lower(),
    )


def covered_keys(paths: RadarPaths) -> set[tuple[str, str]]:
    """Вакансии, уже отработанные hh-снайпером, ключами кросс-дедупа.

    scores.jsonl — все проскоренные hh-вакансии (журнал чисел), applied
    — пуски, parked — ждут решения: всё это не должно прилетать
    хабр-карточкой второй раз.
    """
    from .sne import AppliedStore, ParkedStore, ScoresStore

    keys: set[tuple[str, str]] = set()
    for rec in ScoresStore(paths.scores).load():
        keys.add(
            (
                str(rec.get("name") or "").strip().lower(),
                str(rec.get("employer") or "").strip().lower(),
            )
        )
    keys |= AppliedStore(paths.applied).duplicate_keys()
    for rec in ParkedStore(paths.parked).load_records():
        keys.add(_norm_key(rec.get("vacancy") or {}))
    return keys


def prefilter(
    mapped: list[dict[str, Any]],
    seen_ids: set[str],
    covered: set[tuple[str, str]],
    cfg: Mapping[str, Any] | None = None,
    budget: int = DEFAULT_MAX_PER_RUN,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Дешёвые гейты до LLM: seen, кросс-дедуп, грейд-тег, свежесть, бюджет."""
    cfg = cfg or {}
    moment = now or datetime.now(timezone.utc)
    max_age_hours = float(cfg.get("max_age_hours", DEFAULT_MAX_AGE_HOURS) or 0)
    exclude_tags = {
        str(tag).strip().lower()
        for tag in cfg.get("exclude_tags", DEFAULT_EXCLUDE_TAGS)
        if str(tag).strip()
    }
    accepted: list[dict[str, Any]] = []
    stats = {"seen": 0, "кросс-дедуп": 0, "грейд": 0, "старьё": 0, "бюджет": 0}
    for vacancy in mapped:
        if vacancy["id"] in seen_ids:
            stats["seen"] += 1
            continue
        if _norm_key(vacancy) in covered:
            stats["кросс-дедуп"] += 1
            continue
        tags = {
            str(skill.get("name") or "").lower()
            for skill in vacancy.get("key_skills") or []
        }
        if tags & exclude_tags:
            stats["грейд"] += 1
            continue
        published = str(vacancy.get("published_at") or "")
        if max_age_hours > 0 and published:
            try:
                moment_pub = datetime.fromisoformat(published)
            except ValueError:
                moment_pub = None
            if moment_pub is not None:
                if moment_pub.tzinfo is None:
                    moment_pub = moment_pub.replace(tzinfo=timezone.utc)
                age_hours = (moment - moment_pub).total_seconds() / 3600
                if age_hours > max_age_hours:
                    stats["старьё"] += 1
                    continue
        if len(accepted) >= budget:
            stats["бюджет"] += 1
            continue
        accepted.append(vacancy)
    return accepted, stats


# --- карточка и прогон --------------------------------------------------------------


def format_habr_card(
    vacancy: Mapping[str, Any], score: ScoreResult
) -> str:
    """Карточка радара: скор, вердикт, причины, хэштеги, ссылка."""
    from .notify_tg import MAX_TEXT, _age_suffix, _vacancy_line

    lines = [
        f"ХАБР-РАДАР · скор {score.score} · {score.verdict} — отклик руками",
        _vacancy_line(vacancy) + _age_suffix(vacancy.get("published_at")),
    ]
    for reason in (score.reasons or [])[:3]:
        text = str(reason).strip()
        if text:
            lines.append(f"— {text[:120]}")
    tags = [
        str(skill.get("name") or "")
        for skill in (vacancy.get("key_skills") or [])[:12]
    ]
    tags = [tag for tag in tags if tag]
    if tags:
        lines.append(" ".join(f"#{tag}" for tag in tags))
    url = str(vacancy.get("apply_url") or "")
    if url:
        lines.append(url)
    return "\n".join(lines)[:MAX_TEXT]


def _push_card(
    config: Mapping[str, Any] | None,
    vacancy: Mapping[str, Any],
    score: ScoreResult,
    tg_paths: Any = None,
) -> None:
    from .notify_tg import send_message, tg_enabled

    if not tg_enabled(config):
        return
    try:
        send_message(format_habr_card(vacancy, score), None, tg_paths)
    except Exception as ex:  # noqa: BLE001
        print(f"[habr] карточка в Telegram не ушла: {ex}")


def _marker_due(marker: Path, minutes: float) -> bool:
    try:
        stamp = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return (time.time() - stamp) >= minutes * 60.0


def _touch(marker: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{time.time():.0f}\n", encoding="utf-8")


def run_radar(
    config: Mapping[str, Any] | None,
    scorers: list[Any] | None,
    paths: RadarPaths,
    tg_paths: Any = None,
    dry_run: bool = False,
    limit: int = 0,
    now: datetime | None = None,
) -> int:
    """Один прогон радара: фид → префильтры → скоринг → карточки → сторы.

    Возврат — число maybe/target. Прерванный скоринг (LLM лежит) — код 3,
    маркер и сторы не тронуты: следующий интервал начнёт заново, дедуп
    по seen не даст потерять или задвоить обработанное.
    """
    cfg: Mapping[str, Any] = {}
    if isinstance(config, Mapping) and isinstance(config.get("habr"), Mapping):
        cfg = config["habr"]
    if not scorers:
        print("[habr] скоринг не настроен — радар пропущен")
        return 0

    queries = [str(q).strip() for q in cfg.get("queries", []) if str(q).strip()]
    if not queries:
        from .sne import DEFAULT_QUERIES

        queries = list(DEFAULT_QUERIES)
    items = fetch_rss_items(queries)
    if not items:
        print("[habr] фид пуст")
        _touch(paths.marker)
        return 0

    from .sne import ScoresStore, SeenStore, mark_seen, record_scores

    mapped = [map_item(item) for item in items]
    descriptions = {
        vacancy["id"]: _strip_html(str(item.get("description") or ""))
        for vacancy, item in zip(mapped, items)
    }
    seen_ids = SeenStore(paths.seen).seen_ids()
    covered = covered_keys(paths)
    effective_limit = limit if limit and limit > 0 else int(
        cfg.get("max_per_run", DEFAULT_MAX_PER_RUN) or DEFAULT_MAX_PER_RUN
    )
    accepted, stats = prefilter(
        mapped, seen_ids, covered, cfg, budget=effective_limit, now=now
    )
    detail = ", ".join(f"{key}: {value}" for key, value in stats.items() if value)
    print(
        f"[habr] фид: {len(items)} позиций, к скорингу: {len(accepted)}"
        + (f" ({detail})" if detail else "")
    )
    if not accepted:
        _touch(paths.marker)
        return 0

    from .pipeline import PipelineAbort

    # 06.11: title-гейт выключаем для радара: хабр-фид уже префильтруется
    # своими гейтами (seen/старьё/грейд-теги/кросс-дедуп), а названия
    # Хабра («Требуется <X>») не содержат роль — гейт лишил бы радар
    # релевантных позиций (ловится тестом dry_run_writes_nothing)
    radar_config = dict(config or {})
    if isinstance(radar_config.get("title"), dict):
        radar_config["title"] = {**radar_config["title"], "enabled": False}
    else:
        radar_config["title"] = {"enabled": False}
    pipeline = SnePipeline(
        PipelineFilter(radar_config),
        scorers=list(scorers),
        writer=None,
        score_budget=effective_limit,
    )
    try:
        candidates = pipeline.run(accepted, descriptions=descriptions)
    except PipelineAbort as ex:
        print(f"[habr] скоринг прерван: {ex}", file=sys.stderr)
        return 3

    interesting = 0
    for candidate in candidates:
        score = candidate.score
        if score is None:
            continue
        vacancy = candidate.vacancy
        line = (
            f"  [habr] {score.verdict} {score.score:>3} · "
            f"{vacancy.get('name')} — "
            f"{(vacancy.get('employer') or {}).get('name')}"
        )
        url = str(vacancy.get("apply_url") or "")
        if url:
            line += f" · {url}"
        print(line)
        if score.verdict in ("maybe", "target"):
            interesting += 1
            if not dry_run:
                _push_card(config, vacancy, score, tg_paths)

    if dry_run:
        print("[habr] dry-run: сторы и маркер не тронуты")
        return interesting

    record_scores(ScoresStore(paths.scores), candidates)
    marked = mark_seen(SeenStore(paths.seen), candidates)
    _touch(paths.marker)
    print(f"[habr] закрыто в seen: {marked}, интересно: {interesting}")
    return interesting


def maybe_habr_radar(
    config: Mapping[str, Any] | None,
    scorers: list[Any] | None,
    args: Any,
    tg_paths: Any = None,
) -> int:
    """Тихий хук такта снпрера: кадровый интервал + выключатели.

    Такт идёт каждую минуту, радар — по [habr].interval_minutes; не пора
    или выключен — тихий 0 без сети. Прогон снпрера от радара не зависит.
    """
    cfg: Mapping[str, Any] = {}
    if isinstance(config, Mapping) and isinstance(config.get("habr"), Mapping):
        cfg = config["habr"]
    if not bool(cfg.get("enabled", True)):
        return 0
    if not scorers:
        return 0
    interval = float(cfg.get("interval_minutes", DEFAULT_INTERVAL_MINUTES) or 0)
    paths = radar_paths_from_args(args)
    if interval > 0 and not _marker_due(paths.marker, interval):
        return 0
    return run_radar(config, scorers, paths, tg_paths=tg_paths)


# --- CLI ---------------------------------------------------------------------------


def run_habr_command(args: Any) -> int:
    """CLI: python -m snpr habr — ручной прогон радара сейчас."""
    from .cli import _build_scorers, _load_toml, _scoring_profile
    from .notify_tg import paths_from_args as tg_paths_from_args

    config = _load_toml(str(args.config))
    cfg = config.get("habr", {}) or {}
    if not bool(cfg.get("enabled", True)):
        print("Радар выключен: [habr].enabled = false в config.toml")
        return 0
    scoring_cfg = config.get("scoring", {}) or {}
    scorers = _build_scorers(
        args,
        1,
        maybe_threshold=int(scoring_cfg.get("maybe_min", 50)),
        target_threshold=int(scoring_cfg.get("target_min", 75)),
        profile=_scoring_profile(config),
    )
    if not scorers:
        print(
            "AI не настроен (SNE_AI_BASE_URL/SNE_AI_MODEL или "
            "--ai-base-url/--ai-model) — скоринг радара невозможен",
            file=sys.stderr,
        )
        return 2
    result = run_radar(
        config,
        scorers,
        radar_paths_from_args(args),
        tg_paths=tg_paths_from_args(args),
        dry_run=bool(getattr(args, "dry_run", False)),
        limit=int(getattr(args, "limit", 0) or 0),
    )
    if result == 3:
        return 3
    return 0
