"""Мастер настройки через UI (06.11): персона без правки config.toml.

Редактируемые ключи — только персональные (профили/роли/пороги/поиск);
инфраструктурные секции (http/daemons/токены) UI не трогает.

Безопасность: бэкап config.toml.bak-{ts} перед каждой записью,
запись атомарна (tmp+replace), TOML-валидация roundtrip до записи.
Резюме не пишется в конфиг целиком: мастер формирует из него поля
[scoring].profile (LLM-скоринг) и [letter].profile (капсула фактов
для гварда) — по тем же конвенциям, что и ручная настройка.
"""
from __future__ import annotations

import io
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"

# какие ключи UI умеет редактировать (whitelist; остальное неприкосновенно)
EDITABLE = {
    "outreach.role_keywords": list,
    "outreach.github.target_stack": list,
    "scoring.profile": str,
    "scoring.maybe_min": int,
    "scoring.target_min": int,
    "scoring.auto_approve_min": int,
    "letter.profile": str,
    "letter.static_text": str,
    "search.queries": list,
    "send.max_age_minutes": int,
}


def read_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or DEFAULT_CONFIG
    with open(config_path, "rb") as fh:
        return tomllib.load(fh)


def _get(config: dict, dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(config: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = config
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def read_settings(path: Path | None = None) -> dict[str, Any]:
    """Снимок редактируемых ключей + статус hh для мастера."""
    config = read_config(path)
    settings = {
        key: _get(config, key) for key in EDITABLE
    }
    # hh-статус: жива ли авторизация снайпера
    import os

    auth = (
        Path(os.environ.get("APPDATA", ""))
        / "hh-applicant-tool"
        / "auth.json"
    )
    settings["_hh_authorized"] = auth.exists()
    settings["_config_path"] = str(path or DEFAULT_CONFIG)
    return settings


def write_settings(
    updates: dict[str, Any], path: Path | None = None
) -> dict[str, Any]:
    """Применить обновления к конфигу.

    - только ключи из whitelist (неизвестные — ошибка);
    - типы валидируются;
    - TOML roundtrip (tomli_w) до записи: конфиг нельзя сломать;
    - бэкап config.toml.bak-{ts} перед записью, запись атомарна.
    Возврат: {'ok': True, 'applied': [...]} или {'ok': False, 'error': …}.
    """
    config_path = Path(path or DEFAULT_CONFIG)
    unknown = [k for k in updates if k not in EDITABLE]
    if unknown:
        return {"ok": False, "error": f"неизвестные ключи: {unknown}"}
    config = read_config(config_path)
    for key, value in updates.items():
        expected = EDITABLE[key]
        if expected is list:
            if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value
            ):
                return {"ok": False, "error": f"{key}: ожидается список строк"}
            cleaned = [v.strip() for v in value if v.strip()]
            _set(config, key, cleaned)
        elif expected is int:
            try:
                _set(config, key, int(value))
            except (TypeError, ValueError):
                return {"ok": False, "error": f"{key}: ожидается число"}
        else:
            _set(config, key, str(value))
    # roundtrip: сериализуем и парсим обратно — конфиг нельзя сломать
    import tomli_w

    buf = io.BytesIO()
    tomli_w.dump(config, buf)
    text = buf.getvalue().decode("utf-8")
    try:
        restored = tomllib.loads(text)
    except tomllib.TOMLDecodeError as ex:
        return {"ok": False, "error": f"TOML не собрался: {ex}"}
    if _get(restored, "scoring.profile") != _get(config, "scoring.profile"):
        return {"ok": False, "error": "roundtrip потерял профиль"}
    # бэкап + атомарная запись
    backup = config_path.with_name(
        config_path.name + f".bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(config_path, backup)
    tmp = config_path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(config_path)
    return {"ok": True, "applied": sorted(updates), "backup": str(backup)}


def profile_from_resume(resume_text: str) -> dict[str, str]:
    """Черновики профилей из текста резюме (без LLM — эвристики).

    Мастер показывает черновик, человек правит и жмёт «Сохранить» —
    финальный текст уходит в [scoring].profile/[letter].profile как есть.
    Эвристика: контакты/титул/навыки-строки/последнее место.
    """
    import re

    text = resume_text.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    draft = {
        "scoring_profile": "",
        "letter_profile": "",
        "title": "",
        "email": "",
        "phone": "",
        "telegram": "",
        "stack_keywords": [],
    }
    if not lines:
        return draft
    # ФИО = первая непустая строка без цифр
    name = next(
        (l for l in lines[:5] if not any(c.isdigit() for c in l)), lines[0]
    )
    draft["title"] = name
    # контакты
    email_m = re.search(
        r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+", text
    )
    if email_m:
        draft["email"] = email_m.group(0)
    phone_m = re.search(r"\+7[\s(]?\d{3}[\s)]?\s?\d{3}[\s-]?\d{2}[\s-]?\d{2}", text)
    if phone_m:
        draft["phone"] = phone_m.group(0)
    tg_m = re.search(r"t\.me/([A-Za-z0-9_]{4,})", text)
    if tg_m:
        draft["telegram"] = "@" + tg_m.group(1)
    # желаемая должность: строка после «Желаемая» или с «инженер/разработчик»
    title_m = re.search(
        r"(?:Желаемая\s+должность|должность)[:\s]+(.+)", text, re.I
    )
    if title_m:
        draft["title"] = title_m.group(1).strip()[:80]
    # навыки: секции «Навыки»/«КЛЮЧЕВЫЕ НАВЫКИ»/«Skills» до конца абзаца
    skills_m = re.search(
        r"(?:клю(?:чевые)?\s*навыки|навыки|skills)\s*:?\s*\n(.+?)(?:\n\s*\n|\Z)",
        text,
        re.S | re.I,
    )
    skills = ""
    if skills_m:
        skills = " ".join(skills_m.group(1).replace(";", " ").split())[:600]
    # стек-ключи для role_keywords/target_stack: латинские токены навыков
    stack_words = sorted(
        {
            w.lower()
            for w in re.findall(r"[A-Za-z][A-Za-z+#.\-]{1,15}", skills)
            if len(w) >= 2 and w.lower() not in ("b", "c")
        }
    )[:12]
    # релокация/формат (для полноты черновика)
    reloc_m = re.search(r"Готов к переезду:\s*(.+)", text, re.I)
    reloc = " ".join(reloc_m.group(1).split())[:120] if reloc_m else ""
    exp_m = re.search(r"Опыт работы\D{0,20}(\d+\s*(?:года|лет|мес)[^\n]*)", text)
    exp = " ".join(exp_m.group(1).split())[:60] if exp_m else ""
    # черновик профиля скоринга: должность + навыки + гео
    parts = [f"- Позиция: {draft['title'] or '—'}"]
    if exp:
        parts.append(f"- Опыт: {exp}")
    if skills:
        parts.append(f"- Навыки: {skills[:400]}")
    if reloc:
        parts.append(f"- Релокация: {reloc}")
    if draft["email"]:
        parts.append(f"- Email: {draft['email']}")
    parts.append("- Грейд/формат/зарплату — допишите вручную")
    draft["scoring_profile"] = "\n".join(parts)
    # черновик капсулы письма: факты + контакты
    draft["letter_profile"] = (
        "Факты кандидата (использовать ТОЛЬКО их):\n"
        f"- {draft['title'] or '—'}\n"
        + (f"- Опыт: {exp}\n" if exp else "")
        + (f"- Навыки: {skills[:400]}\n" if skills else "")
        + (f"- Релокация: {reloc}\n" if reloc else "")
        + (f"- Email: {draft['email']}\n" if draft["email"] else "")
        + (f"- Telegram: {draft['telegram']}\n" if draft["telegram"] else "")
        + "- Остальные факты — допишите вручную (проекты, цифры, артефакты)"
    )
    draft["stack_keywords"] = stack_words
    return draft


DRAFT_SYSTEM_PROMPT = """\
Ты — помощник по трудоустройству. Из текста резюме составь НАСТРОЙКИ \
джоба поиска вакансий. Верни строго JSON без markdown:
{
 "scoring_profile": "профиль для LLM-скоринга вакансий: буллеты с ролью, \
грейдом, стеком, опытом, форматом работы, гео, зарплатной вилкой (если \
есть) и анти-паттернами (какие вакансии НЕ рассматривать)",
 "letter_profile": "капсула фактов для сопроводительного письма: ТОЛЬКО \
проверяемые факты с числами (проекты, сроки, метрики), не выдумывать",
 "role_keywords": ["6-10 ключевых слов роли для фильтра вакансий: \
латиницей и по-русски"],
 "target_stack": ["5-8 технологий для поиска коллег по GitHub-коммитам"],
 "title": "короткий титул должности",
 "email": "email из резюме или пусто",
 "telegram": "telegram из резюме или пусто"
}
Пиши по-русски. Числа и названия компаний — только из резюме."""


def profile_from_resume_llm(
    resume_text: str, ai_client: Any
) -> dict[str, Any] | None:
    """Черновики через LLM (умеет то, что эвристика не может: грейд,
    вилку, анти-паттерны, чистку стека).

    Возврат None — LLM недоступен/битый JSON: вызывающий код откатывается
    на эвристику profile_from_resume.
    """
    import json as json_mod

    from ..scoring import _extract_json  # noqa: PLC2701 — общий экстрактор

    try:
        raw = ai_client.complete(
            f"РЕЗЮМЕ:\n{resume_text[:30000]}\n\nСоставь настройки JSON.",
            system_prompt=DRAFT_SYSTEM_PROMPT,
        )
        data = _extract_json(raw)
    except Exception:  # noqa: BLE001 — LLM/JSON любой сбой → эвристика
        return None
    result: dict[str, Any] = {
        "scoring_profile": str(data.get("scoring_profile") or "").strip(),
        "letter_profile": str(data.get("letter_profile") or "").strip(),
        "role_keywords": [
            str(k).lower().strip()
            for k in (data.get("role_keywords") or [])
            if str(k).strip()
        ][:12],
        "target_stack": [
            str(k).lower().strip()
            for k in (data.get("target_stack") or [])
            if str(k).strip()
        ][:10],
        "title": str(data.get("title") or "").strip()[:80],
        "email": str(data.get("email") or "").strip(),
        "telegram": str(data.get("telegram") or "").strip(),
        "stack_keywords": [],
        "phone": "",
    }
    result["stack_keywords"] = result["role_keywords"]
    if not result["scoring_profile"]:
        return None
    return result
