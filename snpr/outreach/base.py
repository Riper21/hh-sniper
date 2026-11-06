"""Сборка единой контактной базы outreach.jsonl (ПЛАН §6.1).

Контракт ПРОЕКТ_ПРОГОНА.md:190-192: outreach.jsonl, сверка с applied/scores,
целевая вакансия как триггер. Здесь — сборка базы из сторов каналов;
сверка и триггер — trigger.py; ручная верификация/кампания — webui.py.

Роли (вердикты АУДИТ_КАНАЛА): hiring_manager (career/hh), ml_team
(GitHub-коммиты), expert (Habr-краул). Письма НЕ отправляются —
campaign-статусы заполняются вручную из UI/CLI.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .store import JsonlStore, touch_checked_at


def _contact_key(channel: str, value: str) -> str:
    return f"{channel}:{value}"


def build_contacts(
    people_store: JsonlStore,
    career_store: JsonlStore,
    hh_store: JsonlStore,
    crawl_store: JsonlStore,
) -> list[dict[str, Any]]:
    """Все каналы → единый список контактов (дедуп по contact_key,
    merge полей при дубле)."""
    by_key: dict[str, dict[str, Any]] = {}

    def add(record: dict[str, Any]) -> None:
        key = str(record["contact_key"])
        existing = by_key.get(key)
        if existing:
            existing.update(
                {k: v for k, v in record.items() if v not in (None, "", [])}
            )
        else:
            by_key[key] = record

    # GitHub-коммиты: ML-команда (email, только ok-статус)
    for person in people_store.load():
        if person.get("status") != "ok" or not person.get("email"):
            continue
        add(
            touch_checked_at(
                {
                    "contact_key": _contact_key("email", str(person["email"])),
                    "channel": "email",
                    "value": str(person["email"]),
                    "company": _company_of_group(str(person.get("group"))),
                    "group": person.get("group"),
                    "name": person.get("name") or person.get("github_login"),
                    "role": "ml_team",
                    "source": "github_commits",
                    "source_url": f"https://github.com/{person.get('github_login')}"
                    if person.get("github_login")
                    else None,
                    "confidence": person.get("email_confidence", "none"),
                    "stack_matched": person.get("stack_matched") or [],
                    "checked_at": person.get("checked_at"),
                }
            )
        )

    # Карьер-сайты: найм-менеджеры (email)
    for record in career_store.load():
        if not record.get("manager_email"):
            continue
        add(
            touch_checked_at(
                {
                    "contact_key": _contact_key(
                        "email", str(record["manager_email"])
                    ),
                    "channel": "email",
                    "value": str(record["manager_email"]),
                    "company": record.get("company"),
                    "group": record.get("group"),
                    "name": record.get("manager_name"),
                    "role": "hiring_manager",
                    "source": "career_api",
                    "source_url": record.get("vacancy_url"),
                    "confidence": "high",
                    "stack_matched": [],
                    "checked_at": record.get("checked_at"),
                }
            )
        )

    # hh: найм-менеджеры (email, если открыт; телефон — отдельный канал)
    for record in hh_store.load():
        if record.get("manager_email"):
            add(
                touch_checked_at(
                    {
                        "contact_key": _contact_key(
                            "email", str(record["manager_email"])
                        ),
                        "channel": "email",
                        "value": str(record["manager_email"]),
                        "company": record.get("company"),
                        "group": record.get("group"),
                        "name": record.get("manager_name"),
                        "role": "hiring_manager",
                        "source": "hh_vacancy",
                        "source_url": record.get("vacancy_url"),
                        "confidence": "medium",
                        "stack_matched": [],
                        "checked_at": record.get("checked_at"),
                    }
                )
            )

    # Habr-краул: эксперты (Telegram)
    for record in crawl_store.load():
        if not record.get("telegram"):
            continue
        add(
            touch_checked_at(
                {
                    "contact_key": _contact_key(
                        "telegram", f"@{record['telegram']}"
                    ),
                    "channel": "telegram",
                    "value": f"@{record['telegram']}",
                    "company": None,
                    "group": None,
                    "name": record.get("fullname") or record.get("login"),
                    "role": "expert",
                    "source": "habr_crawl",
                    "source_url": record.get("habr_url"),
                    "confidence": "medium",
                    "stack_matched": [],
                    "checked_at": record.get("checked_at"),
                }
            )
        )

    return list(by_key.values())


def _company_of_group(group: str) -> str | None:
    """Группа people.jsonl = компании companies.tsv; имя компании
    совпадает с группой в исследовании (см. cards.md)."""
    return group or None


def run_build(
    people_store: JsonlStore,
    career_store: JsonlStore,
    hh_store: JsonlStore,
    crawl_store: JsonlStore,
    base_store: JsonlStore,
    *,
    dry: bool = False,
) -> int:
    """Сборка + upsert базы. Ручные поля (verified, campaign) не затираются:
    JsonlStore.merge оставляет их, новые данные обновляют провенанс."""
    contacts = build_contacts(people_store, career_store, hh_store, crawl_store)
    print(
        f"base: контактов {len(contacts)} "
        f"(email: {sum(1 for c in contacts if c['channel'] == 'email')}, "
        f"telegram: {sum(1 for c in contacts if c['channel'] == 'telegram')})"
    )
    if dry:
        return 0
    written = base_store.upsert_many(contacts)
    print(f"base: обновлено {written}, всего {len(base_store.load())}")
    return written


def stats(base_store: JsonlStore) -> dict[str, Any]:
    """Оффлайн-сводка для status/UI: счётчики, свежесть, верификация."""
    contacts = base_store.load()
    by_channel: dict[str, int] = {}
    by_role: dict[str, int] = {}
    by_source: dict[str, int] = {}
    verified = 0
    stale_days: list[int] = []
    now = datetime.now()
    for contact in contacts:
        channel = str(contact.get("channel"))
        by_channel[channel] = by_channel.get(channel, 0) + 1
        role = str(contact.get("role"))
        by_role[role] = by_role.get(role, 0) + 1
        source = str(contact.get("source"))
        by_source[source] = by_source.get(source, 0) + 1
        if contact.get("verified"):
            verified += 1
        checked = contact.get("checked_at")
        if checked:
            try:
                age = (now - datetime.fromisoformat(str(checked))).days
                stale_days.append(age)
            except ValueError:
                pass
    return {
        "total": len(contacts),
        "by_channel": by_channel,
        "by_role": by_role,
        "by_source": by_source,
        "verified": verified,
        "max_age_days": max(stale_days) if stale_days else 0,
        "companies": sorted(
            {str(c.get("company")) for c in contacts if c.get("company")}
        ),
    }
