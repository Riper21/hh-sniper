"""Рендер MD-отчётов outreach из сторов (ПЛАН §5.2).

Порт cmd_render (cards.md/report.md из run.py) и канальных отчётов research
(НАЙМ_МЕНЕДЖЕРЫ, КАРЬЕРНЫЕ_САЙТЫ, КАУЛ_ИТОГ, ВСЯ_БАЗА). Числа считаются
из сторов — не хардкодом (в research были зашиты в career_report.py:54-55).

Выход: data/outreach/reports/*.md (приватно, data/ в gitignore).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .rank import habr_tier_score
from .store import JsonlStore
from .text import esc


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def render_cards(
    orgs_store: JsonlStore, people_store: JsonlStore, out_path: Path
) -> Path:
    """Карточки «компания → лучший кандидат + запасные» (порт cmd_render).

    Требование прозрачности (ПРОЕКТ_ПРОГОНА §3.5): ссылка на источник email
    для каждой персоны — без неё письмо выглядит как скрейпинг-спам.
    """
    orgs = orgs_store.load_map()
    people = people_store.load()
    lines = ["# Карточки outreach (итог прогона)", "", f"Сгенерировано: {_now()}", ""]
    by_group: dict[str, list[dict]] = {}
    for person in people:
        by_group.setdefault(str(person.get("group")), []).append(person)
    for name, org in sorted(orgs.items(), key=lambda kv: str(kv[1].get("priority"))):
        if org.get("status") != "ok":
            continue
        group_people = by_group.get(str(org.get("group")), [])
        ok_people = [p for p in group_people if p.get("status") == "ok"]
        lines.append(
            f"## {esc(name)} ({esc(str(org.get('github_org')))}), "
            f"приоритет {esc(str(org.get('priority')))}"
        )
        lines.append("")
        if not ok_people:
            lines.append("*Email не найден ни у одного из проверенных членов org.*")
            lines.append("")
            continue
        best = ok_people[0]
        lines.append(
            f"**Лучший кандидат: {esc(str(best.get('name') or best.get('github_login')))}** "
            f"(github: {esc(str(best.get('github_login') or '—'))}, "
            f"stack_match {best.get('stack_match')})"
        )
        lines.append(
            f"- Email: `{esc(str(best.get('email') or ''))}` "
            f"({esc(str(best.get('email_source') or ''))}, "
            f"{esc(str(best.get('email_confidence') or ''))})"
        )
        lines.append(
            f"- Языки: {', '.join(best.get('top_languages') or []) or '—'}; "
            f"контрибуции за год: {best.get('contributions_last_year')}"
        )
        if best.get("stack_matched"):
            lines.append(f"- Матч стека: {', '.join(best['stack_matched'])}")
        if best.get("email_repo"):
            lines.append(f"- Email найден в репо: {esc(str(best['email_repo']))}")
        github_login = str(best.get("github_login") or "")
        lines.append(
            f"- [github.com/{github_login}](https://github.com/{github_login}) | "
            f"[org](https://github.com/{org.get('github_org')})"
        )
        lines.append("")
        if len(ok_people) > 1:
            lines.append("**Запасные:**")
            for person in ok_people[1:4]:
                lines.append(
                    f"- {esc(str(person.get('name') or person.get('github_login')))} "
                    f"({person.get('stack_match')}, "
                    f"{esc(str(person.get('email_source') or ''))}/"
                    f"{esc(str(person.get('email_confidence') or ''))}) — "
                    f"`{esc(str(person.get('email') or ''))}`"
                )
            lines.append("")
        no_email = [p for p in group_people if p.get("status") == "no_email"]
        lines.append(
            f"**Не найдено email:** {len(no_email)} из {len(group_people)} "
            f"проверенных (см. people.jsonl)"
        )
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def render_github_report(
    orgs_store: JsonlStore,
    people_store: JsonlStore,
    out_path: Path,
    *,
    groups: tuple[str, ...] = ("A", "B"),
) -> Path:
    """report.md: метрики по 4 вопросам ПРОЕКТ_ПРОГОНА §1 (порт run.py:697+)."""
    orgs = orgs_store.load()
    people = people_store.load()
    scoped = [r for r in orgs if r.get("priority") in groups]
    resolved = [r for r in scoped if r.get("status") == "ok"]
    by_source: dict[str, int] = {}
    for record in resolved:
        source = str(record.get("org_source"))
        by_source[source] = by_source.get(source, 0) + 1
    ambiguous = [r for r in scoped if r.get("status") == "ambiguous"]
    not_found = [r for r in scoped if r.get("status") == "not_found"]
    ok_people = [p for p in people if p.get("status") == "ok"]
    high = [p for p in ok_people if p.get("email_confidence") == "high"]
    groups_with_high = sorted({str(p.get("group")) for p in high})
    lines = [
        "# Отчёт прогона (метрики по вопросам ПРОЕКТ_ПРОГОНА.md §1)",
        "",
        f"Сгенерировано: {_now()}",
        "",
        "## Q1: разрешение компании → GitHub org",
        f"- компаний в прогоне (группы {', '.join(groups)}): {len(scoped)}",
        f"- разрешено: {len(resolved)}"
        + (
            f" ({round(100 * len(resolved) / len(scoped))}%)"
            if scoped
            else ""
        ),
        f"- из них по источникам: {by_source}",
        f"- ambiguous: {len(ambiguous)} — {[r['name'] for r in ambiguous]}",
        f"- not_found: {len(not_found)} — {[r['name'] for r in not_found]}",
        "",
        "## Q2: персоны с корп. email",
        f"- проверено людей: {len(people)}",
        f"- с email (статус ok): {len(ok_people)}",
        f"- high confidence: {len(high)} в группах: {groups_with_high}",
        "",
        "## Q3: готовность карточек",
        f"- компаний с ready-кандидатом (email ok): {len(groups_with_high)}",
        "",
        "## Q4: систематические неудачи",
        "- ambiguous: ручной github_hint в companies.tsv",
        "- not_found: нет GitHub-присутствия — исключить из канала",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def render_channel_report(
    store: JsonlStore, out_path: Path, title: str, source_note: str
) -> Path:
    """Канальные таблицы hh/career (порт render hh_managers/career_report)."""
    records = store.load()
    with_email = [r for r in records if r.get("manager_email")]
    with_name = [r for r in records if r.get("manager_name")]
    lines = [
        f"# {title}",
        "",
        f"Сгенерировано: {_now()}  ",
        f"{source_note}",
        "",
        "## Сводка",
        "",
        "| Метрика | Значение |",
        "|---|---:|",
        f"| Записей | {len(records)} |",
        f"| Имя менеджера | {len(with_name)} |",
        f"| Email менеджера | {len(with_email)} |",
        f"| Компаний покрыто | {len({r.get('company') for r in records})} |",
        "",
    ]
    by_company: dict[str, list[dict]] = {}
    for record in records:
        by_company.setdefault(str(record.get("company")), []).append(record)
    for company, items in sorted(by_company.items()):
        lines.append(f"## {esc(company)}")
        lines.append("")
        lines.append(
            "| Вакансия | Менеджер | Email | Телефон | Ссылка |"
        )
        lines.append("|---|---|---|---|---|")
        for record in sorted(
            items, key=lambda r: str(r.get("published_at") or ""), reverse=True
        ):
            phones = ", ".join(record.get("manager_phones") or []) or "—"
            lines.append(
                f"| {esc(str(record.get('vacancy_name') or ''))[:60]} "
                f"| {esc(str(record.get('manager_name') or '—'))} "
                f"| `{esc(str(record.get('manager_email') or '—'))}` "
                f"| {esc(phones)} "
                f"| [вакансия]({esc(str(record.get('vacancy_url') or ''))}) |"
            )
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def render_full_base(
    contacts: list[dict[str, Any]],
    out_path: Path,
    *,
    companies: dict[str, dict] | None = None,
) -> Path:
    """Единая база: компания → найм-менеджер → ML-команда → эксперты
    (порт final_report.py, числа из данных)."""
    lines = [
        "# Вся база контактов (агрегат всех каналов)",
        "",
        f"Сгенерировано: {_now()}",
        "",
    ]
    by_company: dict[str, dict[str, list[dict]]] = {}
    for contact in contacts:
        company = str(contact.get("company") or "—")
        role = str(contact.get("role") or "expert")
        by_company.setdefault(company, {}).setdefault(role, []).append(contact)
    for company in sorted(by_company):
        roles = by_company[company]
        lines.append(f"## {esc(company)}")
        lines.append("")
        for role, label in (
            ("hiring_manager", "Найм-менеджеры (email)"),
            ("ml_team", "ML-команда (email из коммитов)"),
            ("expert", "Эксперты (TG)"),
        ):
            items = roles.get(role) or []
            if not items:
                continue
            lines.append(f"**{label} ({len(items)}):**")
            for contact in items:
                value = contact.get("value") or ""
                name = contact.get("name") or "—"
                source = contact.get("source") or ""
                lines.append(
                    f"- {esc(str(name))} — `{esc(str(value))}` ({esc(str(source))})"
                )
            lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def render_habr_report(
    crawl_store: JsonlStore, out_path: Path, *, aliases: dict[str, str] | None = None
) -> Path:
    """КАУЛ_ИТОГ: тиры авторов Habr (порт habr_crawl_report, скор — общий
    rank.habr_tier_score; алиасы компаний — из JSON, не из кода)."""
    records = crawl_store.load()
    aliases = aliases or {}
    with_tg = [r for r in records if r.get("telegram")]
    with_github = [r for r in records if r.get("github_login")]
    lines = [
        "# Итог краула Habr-хабов",
        "",
        f"Сгенерировано: {_now()}",
        "",
        "| Метрика | Значение |",
        "|---|---:|",
        f"| Авторов | {len(records)} |",
        f"| С Telegram | {len(with_tg)} |",
        f"| С GitHub | {len(with_github)} |",
        f"| Имя известно | {sum(1 for r in records if r.get('fullname'))} |",
        "",
        "## Топ авторов (композитный скор)",
        "",
        "| Автор | Карма | Статьи | TG | Компании | Скор |",
        "|---|---:|---:|---|---|---:|",
    ]
    for record in sorted(
        records, key=lambda r: habr_tier_score(r), reverse=True
    )[:30]:
        companies = ",".join(
            aliases.get(str(c), str(c)) for c in (record.get("habr_companies") or [])[:3]
        )
        lines.append(
            f"| [{esc(str(record.get('login')))}]"
            f"(https://habr.com/ru/users/{record.get('login')}/) "
            f"| {record.get('karma', '—')} "
            f"| {record.get('article_count', '—')} "
            f"| {('@' + str(record['telegram'])) if record.get('telegram') else '—'} "
            f"| {esc(companies)} "
            f"| {habr_tier_score(record)} |"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
