"""Няня откликов: чтение переговоров hh и отчёт о реакциях работодателей.

Источник — официальный GET /negotiations (status=active, постранично):
каждый item уже содержит state (id+name), viewed_by_opponent («работодатель
просмотрел»), updated_at, counters (сообщения/непрочитанные), вакансию и
работодателя. Дополнительных запросов на сообщения не делаем — для отчёта
этого достаточно, чат остаётся на сайте hh (решение пользователя 18.10:
респондим вручную, автоматных ответов не строим).

Снапшоты пишутся в data/nanny.jsonl: nanny сравнивает текущее состояние с
прошлым прогоном и показывает только ДВИЖЕНИЕ — новые отклики, ответы,
просмотры, изменения статусов и тишину по дням.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

# Живые статусы переговоров hh (id из API): что-то произошло — работодатель
# двигается. Всё, что не response и не discard, считаем прогрессом.
PROGRESS_STATES = {
    "phone_interview",
    "invitation",
    "offer",
    "hired",
}
STATE_LABELS = {
    "response": "отклик отправлен",
    "viewed": "просмотрено",
    "phone_interview": "созвон",
    "invitation": "приглашение",
    "offer": "оффер",
    "hired": "нанят",
    "discard": "отказ",
}
MSK = timezone(timedelta(hours=3))


def state_label(state_id: str) -> str:
    return STATE_LABELS.get(state_id, state_id or "?")


def parse_api_dt(value: object) -> datetime | None:
    """Дата из API hh (ISO с зоной); наивная строка трактуется как +03:00."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MSK)
    return moment


class NegotiationReader:
    """Постраничное чтение /negotiations: active или другой статус."""

    def __init__(self, api_client: Any, per_page: int = 100):
        self.api_client = api_client
        self.per_page = per_page

    def fetch(self, status: str = "active") -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            res = self.api_client.get(
                "/negotiations",
                {"status": status, "page": page, "per_page": self.per_page},
            )
            if not isinstance(res, dict):
                break
            chunk = res.get("items")
            if not isinstance(chunk, list) or not chunk:
                break
            items.extend(item for item in chunk if isinstance(item, dict))
            if page + 1 >= int(res.get("pages") or 0):
                break
            page += 1
        return items


def normalize_negotiation(item: Mapping[str, Any]) -> dict[str, Any]:
    """Компактная запись для стора: только то, что нужно няне и отчёту."""
    vacancy = item.get("vacancy") or {}
    employer = vacancy.get("employer") or {}
    counters = item.get("counters") or {}
    state = (item.get("state") or {}).get("id") or ""
    updated = parse_api_dt(item.get("updated_at"))
    return {
        "negotiation_id": str(item.get("id") or ""),
        "vacancy_id": str(vacancy.get("id") or ""),
        "vacancy_name": str(vacancy.get("name") or ""),
        "employer": str(employer.get("name") or ""),
        "salary": vacancy.get("salary"),
        "state": state,
        "viewed_by_opponent": bool(item.get("viewed_by_opponent")),
        "has_new_messages": bool(item.get("has_new_messages")),
        "messages": int(counters.get("messages") or 0),
        "unread": int(counters.get("unread_messages") or 0),
        "updated_at": str(item.get("updated_at") or ""),
        "updated_ts": int(updated.timestamp()) if updated else 0,
    }


class NannyStore:
    """Снапшоты переговоров: data/nanny.jsonl, ключ — negotiation_id."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        records: dict[str, dict[str, Any]] = {}
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("negotiation_id"):
                    # поздние записи перезаписывают ранние
                    records[str(rec["negotiation_id"])] = rec
        return records

    def save(self, records: Mapping[str, Mapping[str, Any]]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for rec in records.values():
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return len(records)


def days_silent(record: Mapping[str, Any], now: datetime) -> float:
    """Сколько дней с последнего движения по переговору (0 — неизвестно)."""
    ts = float(record.get("updated_ts") or 0)
    if ts <= 0:
        return 0.0
    return (now - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds() / 86400


def build_report(
    current: Mapping[str, Mapping[str, Any]],
    previous: Mapping[str, Mapping[str, Any]],
    now: datetime,
    silence_days: float,
) -> tuple[list[str], dict[str, int]]:
    """Отчёт няни: строки по категориям + счётчики для сводки."""
    lines: list[str] = []
    stats = {"total": 0, "replied": 0, "progress": 0, "viewed": 0, "silent": 0, "new": 0, "closed": 0}

    def card(rec: Mapping[str, Any]) -> str:
        salary = rec.get("salary") or {}
        parts = []
        for key in ("from", "to"):
            value = salary.get(key)
            if value is not None:
                parts.append(f"{int(value) // 1000}К")
        money = "-".join(parts) + " " + str(salary.get("currency", "")) if parts else ""
        head = f"[{rec.get('negotiation_id')}] {rec.get('vacancy_name', '')} — {rec.get('employer', '')}"
        return f"{head} {money}".strip()

    replied: list[tuple[int, Mapping[str, Any]]] = []
    progress: list[Mapping[str, Any]] = []
    viewed: list[Mapping[str, Any]] = []
    silent: list[tuple[float, Mapping[str, Any]]] = []
    fresh: list[Mapping[str, Any]] = []

    for rec in current.values():
        stats["total"] += 1
        state = str(rec.get("state") or "")
        prev = previous.get(str(rec.get("negotiation_id")))
        if prev is None:
            fresh.append(rec)
            stats["new"] += 1
        unread = int(rec.get("unread") or 0)
        if unread > 0 or bool(rec.get("has_new_messages")):
            replied.append((unread, rec))
            stats["replied"] += 1
            continue
        if state in PROGRESS_STATES:
            progress.append(rec)
            stats["progress"] += 1
            continue
        if state == "discard":
            stats["closed"] += 1
            continue
        if state == "response":
            if bool(rec.get("viewed_by_opponent")):
                viewed.append(rec)
                stats["viewed"] += 1
            else:
                silence = days_silent(rec, now)
                if silence >= silence_days:
                    silent.append((silence, rec))
                    stats["silent"] += 1

    if replied:
        lines.append("ОТВЕТИЛИ (непрочитанные сообщения работодателя — смотреть на hh):")
        for unread, rec in sorted(replied, key=lambda pair: -pair[0]):
            lines.append(f"  * {card(rec)} — {state_label(str(rec.get('state')))}, непрочитанных: {unread}")
    if progress:
        lines.append("ДВИЖЕНИЕ (статус дальше отклика — созвон/приглашение/оффер):")
        for rec in progress:
            lines.append(f"  * {card(rec)} — {state_label(str(rec.get('state')))}")
    if viewed:
        lines.append("ПРОСМОТРЕЛИ (резюме видели, ответа нет):")
        for rec in viewed:
            lines.append(f"  * {card(rec)} — просмотрено {days_silent(rec, now):.0f} дн. назад")
    if silent:
        lines.append(f"ТИШИНА (не просмотрено и без движения ≥ {silence_days:.0f} дн.):")
        for silence, rec in sorted(silent, key=lambda pair: -pair[0]):
            lines.append(f"  * {card(rec)} — тишина {silence:.0f} дн.")
    if fresh:
        lines.append(f"НОВЫЕ В РАДАРЕ (не было в прошлом отчёте): {len(fresh)}")
    if not lines:
        lines.append("Активных переговоров нет.")
    stats["silent_listed"] = len(silent)
    return lines, stats


def run_nanny(args: argparse.Namespace, api_client: Any) -> int:
    """CLI: snpr nanny — снять снапшот переговоров и показать отчёт."""
    store = NannyStore(Path(args.nanny_store))
    previous = store.load()
    reader = NegotiationReader(api_client)
    items = reader.fetch(status=args.status)
    current = {rec["negotiation_id"]: rec for rec in map(normalize_negotiation, items) if rec["negotiation_id"]}

    now = datetime.now(timezone.utc)
    lines, stats = build_report(current, previous, now, float(args.silence_days))
    print(f"Переговоров со статусом «{args.status}»: {stats['total']}")
    for line in lines:
        print(line)

    if not args.dry_run:
        written = store.save(current)
        print(f"Снапшот записан: {written} записей → {store.path}")
    return 0


if __name__ == "__main__":
    sys.exit("CLI-точка входа — python -m snpr nanny")
