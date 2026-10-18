"""Снятие неактивных откликов: DELETE /negotiations/active/{id}.

Неактивные = две категории (решение пользователя 18.10, авто-режима нет —
только руками по команде, по умолчанию dry-run):
- «discard» — работодатель отказал, отклик болтается в списке;
- «response» + тишина старше older_than_days — отклик повис без
  просмотра/ответа, слот занят.

Живые (созвон/приглашение/оффер) и свежие не трогаем. Транспорт подсмотрен
у базы (clear_negotiations.py): DELETE /negotiations/active/{id} с
параметром with_decline_message — тихое снятие без «прощального» сообщения.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .nanny import NegotiationReader, normalize_negotiation, state_label

KEEP_STATES = {"phone_interview", "invitation", "offer", "hired"}


def classify(
    record: Mapping[str, Any],
    now: datetime,
    older_than_days: float,
) -> str:
    """'remove' | 'keep' + причина ложится в отчёт вызывающим."""
    state = str(record.get("state") or "")
    if state in KEEP_STATES:
        return "живой статус"
    if state == "discard":
        return "remove"
    if state == "response":
        ts = float(record.get("updated_ts") or 0)
        if ts > 0:
            age_days = (now - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds() / 86400
            if age_days >= older_than_days:
                return "remove"
            return f"свежий отклик ({age_days:.0f} дн.)"
    return "не классифицирован — не трогаем"


def run_cleanup(args: argparse.Namespace, api_client: Any) -> int:
    """CLI: snpr cleanup [--apply] — показать/снять неактивные отклики."""
    reader = NegotiationReader(api_client)
    items = reader.fetch(status=args.status)
    records = [rec for rec in map(normalize_negotiation, items) if rec.get("negotiation_id")]
    now = datetime.now(timezone.utc)

    doomed: list[dict[str, Any]] = []
    for rec in records:
        verdict = classify(rec, now, float(args.older_than))
        if verdict == "remove":
            doomed.append(rec)
        elif args.verbose:
            print(f"  оставить [{rec['negotiation_id']}] {rec['vacancy_name']} — {verdict}")

    if not doomed:
        print(f"Неактивных откликов нет (просмотрено {len(records)} со статусом «{args.status}»).")
        return 0

    mode = "СНЯТЬ" if args.apply else "БЫ СНЯЛ (dry-run, добавьте --apply)"
    print(f"{mode}: {len(doomed)} откликов")
    removed = 0
    for rec in doomed:
        neg_id = str(rec["negotiation_id"])
        print(
            f"  - [{neg_id}] {rec['vacancy_name']} — {rec['employer']} "
            f"({state_label(str(rec.get('state')))})"
        )
        if not args.apply:
            continue
        try:
            api_client.delete(
                f"/negotiations/active/{neg_id}",
                {"with_decline_message": "true" if args.with_decline_message else "false"},
            )
            removed += 1
        except Exception as ex:
            print(f"    ошибка снятия: {ex} (продолжаем)")
    if args.apply:
        print(f"Снято: {removed} из {len(doomed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit("CLI-точка входа — python -m snpr cleanup")
