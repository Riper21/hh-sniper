"""CI-гварды репо (ПЛАН §9): дубли, секреты, анти-паттерны — регрессия
дублей блокируется тестами.

Проверки (все — «не появись снова»):
- в snpr/ нет Android-кредов hh (HIOMIAS);
- в snpr/ нет sys.path.insert (модули — обычные пакеты);
- нет ssl._create_unverified_context вне http.py (инкапсуляция);
- в snpr/outreach одна копия esc/unesc-парсера Habr (CONTACTS_RE);
- research-скрипты не импортируются из snpr/ (изоляция данных).
"""
from __future__ import annotations

from pathlib import Path

import snpr

SNPR_DIR = Path(snpr.__file__).parent


def _py_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def test_no_android_credentials():
    leaked = [
        p
        for p in _py_files(SNPR_DIR)
        if "HIOMIAS" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert leaked == [], f"Android-креды hh в коде: {leaked}"


def test_no_sys_path_hacks():
    offenders = [
        p
        for p in _py_files(SNPR_DIR)
        if "sys.path.insert" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == [], f"sys.path.insert в snpr/: {offenders}"


def test_unverified_ssl_only_in_http_module():
    offenders = [
        p
        for p in _py_files(SNPR_DIR)
        if "_create_unverified" in p.read_text(encoding="utf-8", errors="replace")
        and p.name != "http.py"
    ]
    assert offenders == [], f"unverified SSL вне http.py: {offenders}"


def test_habr_parser_single_copy():
    copies = [
        p
        for p in _py_files(SNPR_DIR)
        if "CONTACTS_RE" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert copies == [SNPR_DIR / "outreach" / "habr.py"], (
        f"парсер Habr должен жить только в outreach/habr.py: {copies}"
    )


def test_snpr_does_not_import_research():
    """Изоляция: snpr/ не импортирует research-скрипты (миграция данных —
    только пути по умолчанию в cli.py/migrate.py, не импорты)."""
    import re

    pattern = re.compile(
        r"^\s*(from|import)\s+(research|run|habr_crawl|habr_match|habr_bridge|"
        r"habr_gh_enrich|hh_managers|career_managers|jobtoo_extract|final_report)\b",
        re.M,
    )
    offenders = [
        p
        for p in _py_files(SNPR_DIR)
        if pattern.search(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert offenders == [], f"импорт research-скриптов из snpr/: {offenders}"


def test_esc_single_definition():
    """esc() определён только в outreach/text.py (в research было 6 копий);
    перечисление имён импорта разрешено."""
    import re

    pattern = re.compile(r"^def esc\(", re.M)
    definitions = [
        p
        for p in _py_files(SNPR_DIR)
        if pattern.search(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert definitions == [SNPR_DIR / "outreach" / "text.py"], (
        f"def esc() должен жить только в outreach/text.py: {definitions}"
    )
