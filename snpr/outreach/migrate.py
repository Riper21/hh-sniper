"""Однократная миграция данных research/outreach_mvp → data/outreach (§5.3).

Переносит 9 JSONL + companies.tsv + cache/, конвертирует алиасы из кода
research в JSON-файлы, доупsertить append-файлы (habr_matches/bridge) по
новым ключам. Сверяет счётчики с эталоном аудита (расхождение ≠ ошибка
схемы, но требует объяснения). research/ замораживается архивом.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .pipeline import RESEARCH_COUNTS, Paths
from .store import JsonlStore

# словари, снятые с кода research (habr_crawl_report.py:37-49,
# hh_managers.py:148-164) — в data/*.json (ПЛАН §3.5)
HABR_ALIAS_TO_GROUP = {
    "avito": "Авито", "avito-incubator": "Авито", "yandex": "Яндекс",
    "sber": "Сбер", "sberbank": "Сбер", "sberdevices": "Сбер",
    "mts": "МТС", "alfabank": "Альфа-Банк", "ozon": "Ozon",
    "kontur": "Контур", "cloudru": "Cloud.ru", "2gis": "2ГИС",
    "selectel": "Selectel", "tinkoff": "Т-Банк", "raif": "Raft",
    "just-ai": "Just AI", "x5": "X5 Group", "nornickel": "Норникель",
    "severstal": "Северсталь", "mega": "Мегафон", "rtk": "Ростелеком",
    "vtb": "ВТБ", "gazprombank": "Газпромбанк", "phosagro": "ФосАгро",
    "eurochem": "ЕвроХим", "t1": "Т1", "cels": "Цельс",
    "kaspersky": "Лаборатория Касперского", "mtsbank": "МТС",
    "sberhealth": "Сбер", "lamoda": "Ламода", "vk": "VK",
}

HH_EMPLOYER_HINTS = {
    "Сбер": "СБЕР", "Яндекс": "Yandex", "Авито": "Авито",
    "Альфа-Банк": "Альфа-Банк", "МТС": "МТС", "Т-Банк": "Т-Банк",
    "X5 Group": "X5 Tech", "Норникель": "Норникель",
    "Северсталь": "Северсталь", "билайн": "Билайн", "Контур": "Контур",
    "Cloud.ru": "Cloud.ru", "Ozon": "Ozon", "Магнит": "Магнит",
    "Selectel": "Selectel", "2ГИС": "2ГИС", "ВкусВилл": "ВкусВилл",
    "red_mad_robot": "Redmadrobot", "VK": "VK", "Авиасейлс": "Aviasales",
    "ФРИИ": "ФРИИ", "Лаборатория Касперского": "Лаборатория Касперского",
    "hh.ru": "HeadHunter", "ЭТП Газпромбанк": "Газпромбанк", "ВТБ": "ВТБ",
    "Т1": "Т1", "Мегафон": "МегаФон", "ФосАгро": "ФосАгро",
    "ЕвроХим": "ЕвроХим", "Дом.РФ": "Дом.РФ", "Русагро": "Русагро",
    "Цельс": "Цельс", "Совкомбанк": "Совкомбанк", "Just AI": "Just AI",
    "Raft": "Raft", "Аспирити": "Aspirity",
}

# соответствие файлов research → сторы интеграции
FILE_MAP = {
    "orgs.jsonl": "orgs",
    "people.jsonl": "people",
    "habr_crawl_pilot.jsonl": "habr_crawl",
    "habr_matches.jsonl": "habr_matches",
    "gh_enrich.jsonl": "gh_enrich",
    "bridge_habr.jsonl": "bridge",
    "career_managers.jsonl": "career",
    "jobtoo_career_sites.jsonl": "jobtoo_sites",
    "hh_managers.jsonl": "hh",
}


def run_migrate(
    source: Path, paths: Paths, *, verify: bool = True
) -> dict[str, Any]:
    """Перенос данных + алиасы + cache. Возврат: отчёт миграции."""
    report: dict[str, Any] = {"copied": {}, "aliases": {}, "cache_files": 0}
    if not source.exists():
        report["error"] = f"источник не найден: {source}"
        return report

    # 1. JSONL-сторы: копия с дедупом по ключу (append-файлы research
    #    могли содержать дубли между прогонами)
    for filename, store_name in FILE_MAP.items():
        src = source / filename
        if not src.exists():
            report["copied"][filename] = None
            continue
        store: JsonlStore = getattr(paths, store_name)
        records = [line for line in _read_lines(src)]
        written = store.upsert_many(records)
        report["copied"][filename] = {
            "source_lines": len(records),
            "stored": len(store.load()),
            "written": written,
        }

    # 2. companies.tsv — вход, копия как есть
    tsv = source / "companies.tsv"
    if tsv.exists():
        shutil.copy2(tsv, paths.companies_tsv)
        report["companies_tsv"] = str(paths.companies_tsv)

    # 3. алиасы из кода research → JSON (однократно)
    if not paths.employer_aliases.exists():
        import json

        paths.employer_aliases.write_text(
            json.dumps(HH_EMPLOYER_HINTS, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        report["aliases"]["employer"] = len(HH_EMPLOYER_HINTS)
    if not paths.habr_aliases.exists():
        import json

        paths.habr_aliases.write_text(
            json.dumps(HABR_ALIAS_TO_GROUP, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        report["aliases"]["habr"] = len(HABR_ALIAS_TO_GROUP)

    # 4. кэш research (642 файла) — перенос для повторного использования
    src_cache = source / "cache"
    if src_cache.exists():
        paths.cache.mkdir(parents=True, exist_ok=True)
        copied = 0
        for cache_file in src_cache.glob("*.json"):
            dest = paths.cache / cache_file.name
            if not dest.exists():
                shutil.copy2(cache_file, dest)
                copied += 1
        report["cache_files"] = copied

    # 5. сверка с эталоном аудита
    if verify:
        report["verification"] = verify_counts(paths)
    return report


def _read_lines(path: Path) -> list[dict[str, Any]]:
    import json

    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def verify_counts(paths: Paths) -> dict[str, Any]:
    """Сверка счётчиков сторов с эталоном аудита (§5.3.5)."""
    result: dict[str, Any] = {"matches": True, "diffs": {}}
    for filename, store_name in FILE_MAP.items():
        expected = RESEARCH_COUNTS.get(filename)
        if expected is None:
            continue
        store: JsonlStore = getattr(paths, store_name)
        actual = len(store.load())
        if actual != expected:
            result["matches"] = False
            result["diffs"][filename] = {"expected": expected, "actual": actual}
    return result


def freeze_research(source: Path) -> Path:
    """АРХИВ_ЗАМЕТКА.md в research/outreach_mvp (§5.3.6)."""
    note = source / "АРХИВ_ЗАМЕТКА.md"
    note.write_text(
        "# Архив прогона\n\n"
        "Данные мигрированы в data/outreach/ (06.11, ПЛАН_ИНТЕГРАЦИИ_OUTREACH.md).\n"
        "Живой код — snpr/outreach/. Эта папка — история итераций 03-05.11,\n"
        "документы и вердикты (АУДИТ_КАНАЛА, ПРОЕКТ_ПРОГОНА) остаются\n"
        "источником решений. Скрипты не удаляем: воспроизводимость истории.\n",
        encoding="utf-8",
    )
    return note
