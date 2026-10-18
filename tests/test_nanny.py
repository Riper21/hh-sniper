"""Тесты няни откликов: чтение /negotiations, снапшоты, отчёт."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from snpr.nanny import (
    NannyStore,
    NegotiationReader,
    build_report,
    days_silent,
    normalize_negotiation,
    run_nanny,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _neg(
    neg_id: int,
    state: str = "response",
    updated_days_ago: float = 1.0,
    viewed: bool = False,
    unread: int = 0,
    has_new: bool = False,
    vacancy_id: int = 555,
    employer: str = "ООО Ромашка",
) -> dict:
    updated = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc) - timedelta(
        days=updated_days_ago
    )
    return {
        "id": neg_id,
        "state": {"id": state, "name": "whatever"},
        "created_at": "2026-10-01T10:00:00+03:00",
        "updated_at": updated.isoformat(),
        "viewed_by_opponent": viewed,
        "has_updates": False,
        "has_new_messages": has_new,
        "counters": {"messages": 4, "unread_messages": unread},
        "vacancy": {
            "id": vacancy_id,
            "name": "Senior AI инженер",
            "employer": {"id": 777, "name": employer},
            "salary": {"from": 300000, "to": 400000, "currency": "RUR"},
        },
    }


class FakeNegApi:
    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.get_calls: list[tuple[str, dict]] = []

    def get(self, endpoint, params=None):
        self.get_calls.append((endpoint, dict(params or {})))
        assert endpoint == "/negotiations"
        page = int((params or {}).get("page", 0))
        return {"items": self.pages[page], "pages": len(self.pages), "found": 99}


def test_reader_pagination() -> None:
    api = FakeNegApi([[_neg(1), _neg(2)], [_neg(3)]])
    items = NegotiationReader(api).fetch()
    assert len(items) == 3
    assert [c[1]["page"] for c in api.get_calls] == [0, 1]
    assert api.get_calls[0][1]["per_page"] == 100
    assert api.get_calls[0][1]["status"] == "active"


def test_reader_single_page_stops() -> None:
    api = FakeNegApi([[_neg(1)]])
    assert len(NegotiationReader(api).fetch()) == 1
    assert len(api.get_calls) == 1


def test_normalize_fields() -> None:
    rec = normalize_negotiation(_neg(100500, unread=2, viewed=True))
    assert rec["negotiation_id"] == "100500"
    assert rec["vacancy_id"] == "555"
    assert rec["employer"] == "ООО Ромашка"
    assert rec["state"] == "response"
    assert rec["viewed_by_opponent"] is True
    assert rec["unread"] == 2
    assert rec["messages"] == 4
    assert rec["updated_ts"] > 0
    assert rec["salary"] == {"from": 300000, "to": 400000, "currency": "RUR"}


def test_normalize_naive_datetime_treated_msk() -> None:
    item = _neg(1, updated_days_ago=0)
    item["updated_at"] = "2026-10-18T12:00:00"  # без зоны
    rec = normalize_negotiation(item)
    assert rec["updated_ts"] > 0


def test_nanny_store_roundtrip(tmp_path: Path) -> None:
    store = NannyStore(tmp_path / "nanny.jsonl")
    assert store.load() == {}
    rec = normalize_negotiation(_neg(7))
    assert store.save({"7": rec}) == 1
    loaded = store.load()
    assert loaded["7"]["negotiation_id"] == "7"


def test_nanny_store_garbage_lines(tmp_path: Path) -> None:
    path = tmp_path / "nanny.jsonl"
    path.write_text("не json\n\n[]\n", encoding="utf-8")
    assert NannyStore(path).load() == {}


def test_days_silent() -> None:
    rec = normalize_negotiation(_neg(1, updated_days_ago=5))
    assert abs(days_silent(rec, NOW) - 5.0) < 0.01
    assert days_silent({"updated_ts": 0}, NOW) == 0.0


def test_report_categories() -> None:
    current = {
        "1": normalize_negotiation(_neg(1, unread=3)),
        "2": normalize_negotiation(_neg(2, state="invitation")),
        "3": normalize_negotiation(_neg(3, viewed=True, updated_days_ago=2)),
        "4": normalize_negotiation(_neg(4, updated_days_ago=6)),
        "5": normalize_negotiation(_neg(5, state="discard")),
    }
    lines, stats = build_report(current, {}, NOW, silence_days=3.0)
    text = "\n".join(lines)
    assert stats["total"] == 5
    assert stats["replied"] == 1
    assert stats["progress"] == 1
    assert stats["viewed"] == 1
    assert stats["silent"] == 1
    assert stats["closed"] == 1
    assert stats["new"] == 5
    assert "ОТВЕТИЛИ" in text and "непрочитанных: 3" in text
    assert "ДВИЖЕНИЕ" in text and "приглашение" in text
    assert "ПРОСМОТРЕЛИ" in text
    assert "ТИШИНА" in text and "тишина 6 дн." in text
    assert "НОВЫЕ В РАДАРЕ" in text


def test_report_all_known_no_new() -> None:
    current = {"1": normalize_negotiation(_neg(1))}
    lines, stats = build_report(current, dict(current), NOW, 3.0)
    assert stats["new"] == 0
    assert "НОВЫЕ" not in "\n".join(lines)


def test_report_empty() -> None:
    lines, stats = build_report({}, {}, NOW, 3.0)
    assert stats["total"] == 0
    assert "Активных переговоров нет." in lines


def test_run_nanny_writes_snapshot(tmp_path: Path, capsys) -> None:
    api = FakeNegApi([[_neg(1), _neg(2, state="phone_interview")]])
    args = argparse.Namespace(
        nanny_store=str(tmp_path / "nanny.jsonl"),
        status="active",
        silence_days=3.0,
        dry_run=False,
    )
    assert run_nanny(args, api) == 0
    path = tmp_path / "nanny.jsonl"
    assert path.exists()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_run_nanny_dry_run_no_write(tmp_path: Path) -> None:
    api = FakeNegApi([[_neg(1)]])
    args = argparse.Namespace(
        nanny_store=str(tmp_path / "nanny.jsonl"),
        status="active",
        silence_days=3.0,
        dry_run=True,
    )
    run_nanny(args, api)
    assert not (tmp_path / "nanny.jsonl").exists()


def test_run_nanny_diff_against_previous(tmp_path: Path, capsys) -> None:
    store_path = tmp_path / "nanny.jsonl"
    api = FakeNegApi([[_neg(1, unread=1)]])
    args = argparse.Namespace(
        nanny_store=str(store_path),
        status="active",
        silence_days=3.0,
        dry_run=False,
    )
    run_nanny(args, api)
    out1 = capsys.readouterr().out
    assert "НОВЫЕ В РАДАРЕ" in out1
    api = FakeNegApi([[_neg(1, unread=1)]])
    run_nanny(args, api)
    out2 = capsys.readouterr().out
    assert "НОВЫЕ В РАДАРЕ" not in out2
    assert "ОТВЕТИЛИ" in out2
    # поздние записи поверх ранних: стор растёт, читается последняя
    assert len(store_path.read_text(encoding="utf-8").splitlines()) == 2
    assert len(NannyStore(store_path).load()) == 1


def test_run_nanny_mixed_statuses_counted(tmp_path: Path, capsys) -> None:
    api = FakeNegApi(
        [[_neg(1, state="discard"), _neg(2, state="offer"), _neg(3, unread=2)]]
    )
    args = argparse.Namespace(
        nanny_store=str(tmp_path / "n.jsonl"),
        status="active",
        silence_days=3.0,
        dry_run=True,
    )
    run_nanny(args, api)
    out = capsys.readouterr().out
    assert "Переговоров со статусом «active»: 3" in out
    assert "оффер" in out
