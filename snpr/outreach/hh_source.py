"""hh-канал outreach: найм-менеджеры из открытых вакансий (порт hh_managers).

Ключевой рефакторинг безопасности (ПЛАН §4.4): research держал собственный
OAuth-refresh с захардкоженными Android client_id/client_secret и мобильной
личиной. Интеграция использует ЕДИНУЮ точку входа проекта —
snpr.hh_tool.SavingHHApplicantTool → tool.api_client — с уже решённым
багом персистентности токена. Android-креды из кода удалены.

Вердикт исследования (3А): канал слабый (контакты деперсон., 0 email,
call-center телефоны) — используется только как триггер приоритизации.

Этика: контакты берутся ТОЛЬКО из вакансий, где работодатель открыл их
до отклика (компания сама опубликовала контакт).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from .rank import ml_vacancy_filter
from .store import JsonlStore, read_tsv, touch_checked_at

AREA_RU = 113
MAX_PAGES = 5
REQUEST_DELAY = 0.4

# Алиасы hh-работодателей — в data/outreach/employer_aliases.json (ПЛАН §3.5);
# здесь только дефолт для свежего старта (миграция привнесёт полный словарь)
DEFAULT_EMPLOYER_HINTS: dict[str, str] = {}


class HHApiClient(Protocol):
    """Контракт api_client из snpr.hh_tool (для тестов — фейк)."""

    def get(self, path: str) -> dict: ...


def load_employer_aliases(path: Path) -> dict[str, str]:
    """Алиасы из JSON-файла (снятые с кода research HH_EMPLOYER_HINTS)."""
    import json

    if not path.exists():
        return dict(DEFAULT_EMPLOYER_HINTS)
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_EMPLOYER_HINTS)


def resolve_employer_id(
    api: HHApiClient, name: str, aliases: dict[str, str]
) -> int | None:
    """Поиск employer_id: максимум open_vacancies с матчем бренда
    (порт hh_managers.resolve_employer_id — отсекает фан-аккаунты/ИП)."""
    query = aliases.get(name) or name
    data = api.get(f"/employers?text={quote(query)}&per_page=20") or {}
    best: int | None = None
    best_score = -1
    for item in data.get("items", []):
        iname = str(item.get("name") or "").lower()
        ql = query.lower()
        brand_ok = ql in iname or iname in ql
        if not brand_ok:
            continue
        opens = int(item.get("open_vacancies") or 0)
        if opens > best_score:
            best_score = opens
            best = int(item["id"])
    return best


def search_vacancies(
    api: HHApiClient, employer_id: int, *, pages: int = MAX_PAGES
) -> list[dict]:
    """Все вакансии работодателя (постранично; порт hh_managers:188-199)."""
    all_items: list[dict] = []
    for page in range(pages):
        data = (
            api.get(
                f"/vacancies?employer_id={employer_id}&area={AREA_RU}"
                f"&per_page=100&page={page}"
            )
            or {}
        )
        items = data.get("items", [])
        all_items.extend(items)
        if not items or page >= data.get("pages", 1) - 1:
            break
    return all_items


def vacancy_is_ml(vacancy: dict, keywords: list[str] | None = None) -> bool:
    """Фильтр hh-вакансии под роль (snippet+name; 06.11: keywords из
    [outreach].role_keywords, None = ML-дефолт)."""
    name = str(vacancy.get("name") or "")
    snippet = vacancy.get("snippet") or {}
    if isinstance(snippet, dict):
        name += " " + str(snippet.get("requirement") or "")
        name += " " + str(snippet.get("responsibility") or "")
    return ml_vacancy_filter(name, keywords=keywords)


def extract_manager(vacancy_full: dict) -> dict | None:
    """Контакты из полной карточки: None, если работодатель их закрыл
    (этика канала: только открытое)."""
    contacts = vacancy_full.get("contacts") or {}
    if not (contacts.get("name") or contacts.get("email")):
        return None
    return {
        "manager_name": contacts.get("name") or "",
        "manager_email": contacts.get("email") or "",
        "manager_phones": [
            p.get("formatted") for p in (contacts.get("phones") or [])
            if p.get("formatted")
        ],
        "manager_activity": (vacancy_full.get("manager_activity") or {}).get(
            "last_activity_at"
        ),
    }


def build_record(
    company: dict, employer_id: int, vacancy_full: dict
) -> dict:
    manager = extract_manager(vacancy_full) or {}
    return touch_checked_at(
        {
            "vacancy_id": vacancy_full.get("id"),
            "vacancy_name": vacancy_full.get("name"),
            "company": company["name"],
            "group": company.get("group", ""),
            "priority": company.get("priority", ""),
            "employer_id": employer_id,
            "manager_name": manager.get("manager_name", ""),
            "manager_email": manager.get("manager_email", ""),
            "manager_phones": manager.get("manager_phones", []),
            "manager_activity": manager.get("manager_activity"),
            "salary": vacancy_full.get("salary"),
            "area": (vacancy_full.get("area") or {}).get("name"),
            "published_at": vacancy_full.get("published_at"),
            "created_at": vacancy_full.get("created_at"),
            "vacancy_url": vacancy_full.get("alternate_url"),
        }
    )


def run(
    api: HHApiClient,
    store: JsonlStore,
    *,
    companies_tsv: Path,
    aliases_path: Path,
    pages: int = MAX_PAGES,
    limit: int = 0,
    dry: bool = False,
    role_keywords: list[str] | None = None,
) -> int:
    """Прогон hh-канала (порт crawl). Троттлинг 0.4с между карточками."""
    aliases = load_employer_aliases(aliases_path)
    companies = read_tsv(companies_tsv)
    if limit:
        companies = companies[:limit]
    # 06.11: роль конфигурируема (keywords=None = ML-дефолт)
    keywords = [k.lower() for k in role_keywords] if role_keywords else None
    existing = store.keys()
    results: list[dict] = []
    for company in companies:
        if not company.get("name"):
            continue
        employer_id = resolve_employer_id(api, company["name"], aliases)
        if not employer_id:
            print(f"  {company['name']}: работодатель не найден")
            continue
        vacancies = search_vacancies(api, employer_id, pages=pages)
        ml = [v for v in vacancies if vacancy_is_ml(v, keywords)]
        with_contacts = 0
        for vacancy in ml:
            vid = vacancy.get("id")
            # 06.11 P2-фикс: vid из hh-JSON — int, existing — set[str]:
            # сравнение всегда False → карточки перекачивались каждый прогон
            if str(vid) in existing:
                continue
            full = api.get(f"/vacancies/{vid}")
            if not full:
                continue
            if extract_manager(full) is None:
                continue  # контакты закрыты до отклика — не трогаем
            with_contacts += 1
            results.append(build_record(company, employer_id, full))
            time.sleep(REQUEST_DELAY)
        print(
            f"  {company['name']} (hh id {employer_id}): вакансий "
            f"{len(vacancies)}, ML {len(ml)}, с contacts {with_contacts}"
        )
    if dry or not results:
        return 0
    written = store.upsert_many(results)
    print(f"hh: записано {written}")
    return written
