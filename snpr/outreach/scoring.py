"""LLM-скоринг карьер-вакансий outreach (06.11, запрос владельца).

Переиспользует VacancyScorer снайпера: карьер-запись (career_managers.jsonl)
конвертируется в hh-подобный dict (name/employer/salary/area) + описание
из extra.description (Sber/Avito уже пишут) или карта Alfa (duties/
requirements/description — дотягиваем адаптером).

Скор пишется в ТОТ ЖЕ стор (поля score/verdict/reasons/matched/missing +
scored_at) — апсерт мержит, ручные поля не затираются. Идемпотентность:
--force для перескоринга, иначе пропускаем уже скоренные.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..scoring import VacancyScorer
from .store import JsonlStore


def record_to_vacancy(record: Mapping[str, Any]) -> dict[str, Any]:
    """Карьер-запись → hh-подобный dict для VacancyScorer.score()."""
    salary = record.get("salary") or {}
    return {
        "id": record.get("vacancy_id"),
        "name": record.get("vacancy_name"),
        "employer": {"name": record.get("company")},
        "salary": {
            "from": salary.get("from"),
            "to": salary.get("to"),
            "currency": salary.get("currency", "RUR"),
        }
        if salary
        else None,
        "area": {"name": record.get("area")},
        "experience": {},
        "key_skills": [],
    }


def record_description(record: Mapping[str, Any]) -> str:
    """Описание из записи: extra.description (Sber/Avito) или собранное."""
    extra = record.get("extra") or {}
    parts = [
        str(extra.get("description") or ""),
        str(extra.get("duties") or ""),
        str(extra.get("requirements") or ""),
        str(extra.get("conditions") or ""),
    ]
    return " ".join(p for p in parts if p).strip()


def run_scoring(
    store: JsonlStore,
    scorer: VacancyScorer,
    *,
    limit: int = 0,
    force: bool = False,
    dry: bool = False,
) -> int:
    """Прогнать LLM-скоринг по записям карьер-канала.

    Пропускает записи: без описания (скорить нечего — Alfa до дотяжки),
    уже скоренные (score есть) без --force. Возврат: сколько заскорено.
    """
    records = store.load()
    no_description = sum(1 for r in records if not record_description(r))
    todo = [
        r
        for r in records
        if (force or not r.get("score")) and record_description(r)
    ]
    if limit:
        todo = todo[:limit]
    print(
        f"outreach score: записей {len(records)}, к скорингу {len(todo)} "
        f"(без описания пропущено: {no_description})"
    )
    scored = 0
    for record in todo:
        result = scorer.score(
            record_to_vacancy(record), record_description(record)
        )
        if not result.parse_ok:
            print(f"  [{record.get('vacancy_id')}] LLM не вернул JSON — пропуск")
            continue
        updated = dict(record)
        updated["score"] = result.score
        updated["verdict"] = result.verdict
        updated["score_reasons"] = result.reasons
        updated["score_matched"] = result.matched
        updated["score_missing"] = result.missing
        # 06.11 фикс: момент скоринга — для колонки «Когда» в UI
        # (unix-секунды, как ts в scores.jsonl снайпера)
        from datetime import datetime, timezone

        updated["scored_at"] = int(
            datetime.now(timezone.utc).timestamp()
        )
        if not dry:
            store.upsert(updated)
        scored += 1
        print(
            f"  [{record.get('vacancy_id')}] {result.score} ({result.verdict}) "
            f"{str(record.get('vacancy_name'))[:50]}"
        )
    if dry:
        return 0
    return scored
