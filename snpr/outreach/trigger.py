"""Связка outreach ↔ снайпер (ПЛАН §6): тихий хук «целевая вакансия →
подсказка контакта».

Контракт ПРОЕКТ_ПРОГОНА.md:190-192 — «outreach.jsonl, сверка с applied/
scores, целевая вакансия как триггер письма». Письма НЕ отправляются
(решение зафиксировано): только TG-карточка-подсказка с прямым контактом
найм-менеджера и ссылкой на источник (требование прозрачности).

Дешевизна: матч по готовой таблице алиасов, ноль сети, ноль LLM —
тихий хук не может задеть прогон (обёрнут try/except в sne.py).
"""
from __future__ import annotations

import argparse
from typing import Any, Mapping

from .config import data_dir, outreach_config
from .pipeline import Paths
from .text import company_matches


def find_contact_for_company(
    contacts: list[dict[str, Any]], hh_employer: str
) -> dict[str, Any] | None:
    """Лучший контакт для hh-работодателя: hiring_manager > ml_team.

    Роль hiring_manager (карьер-сайты) приоритетнее — вердикт 4А.
    """
    best: dict[str, Any] | None = None
    for contact in contacts:
        company = str(contact.get("company") or "")
        if not company or not company_matches(hh_employer, company):
            continue
        if contact.get("role") != "hiring_manager":
            continue
        if not contact.get("value"):
            continue
        if best is None or contact.get("confidence") == "high":
            best = contact
    if best is not None:
        return best
    for contact in contacts:
        company = str(contact.get("company") or "")
        if not company or not company_matches(hh_employer, company):
            continue
        if contact.get("role") == "ml_team" and contact.get("value"):
            return contact
    return None


def build_hint_card(
    vacancy_name: str,
    employer_name: str,
    score: Any,
    contact: Mapping[str, Any],
) -> str:
    """TG-карточка-подсказка (notify_only)."""
    source_url = str(contact.get("source_url") or "")
    lines = [
        "🎯 Outreach: у этой компании есть прямой контакт",
        "",
        f"Вакансия: {vacancy_name}",
        f"Компания: {employer_name} (score {score})",
        "",
        f"Контакт: {contact.get('name') or '—'} — {contact.get('role')}",
        f"Канал: {contact.get('channel')} → {contact.get('value')}",
        f"Источник: {contact.get('source')}"
        + (f" ({source_url})" if source_url else ""),
    ]
    if contact.get("confidence"):
        lines.append(f"Достоверность: {contact['confidence']}")
    lines.append("")
    lines.append("Письмо не отправлено автоматически — решение за тобой.")
    return "\n".join(lines)[:4000]


def maybe_outreach_hint(
    config: Mapping[str, Any] | None,
    applied_rows: list[dict[str, Any]],
    tg_paths: Any = None,
    args: argparse.Namespace | None = None,
) -> int:
    """Тихий хук конца такта: новые отклики → подсказки контактов.

    Вызывается из run_sne (шаблон maybe_habr_radar, sne.py:1185-1190).
    Возврат: число отправленных подсказок (0 — базы нет/выключено/нет TG).
    """
    cfg = outreach_config(dict(config or {}))
    trigger_cfg = cfg.get("trigger", {})
    if not (cfg.get("enabled") and trigger_cfg.get("enabled")):
        return 0
    if not trigger_cfg.get("notify_only", True):
        return 0  # автоотправки писем нет и не будет без отдельного решения
    paths = Paths(data_dir(cfg))
    if not paths.base.path.exists():
        return 0
    contacts = paths.base.load()
    if not contacts:
        return 0
    sent = 0
    from ..notify_tg import send_message

    for row in applied_rows:
        employer = str(row.get("employer") or row.get("employer_name") or "")
        if not employer:
            continue
        contact = find_contact_for_company(contacts, employer)
        if contact is None:
            continue
        text = build_hint_card(
            str(row.get("name") or row.get("vacancy_name") or ""),
            employer,
            row.get("score", "—"),
            contact,
        )
        try:
            send_message(text, paths=tg_paths)
            sent += 1
        except Exception:  # noqa: BLE001 — тихий хук
            continue
    return sent
