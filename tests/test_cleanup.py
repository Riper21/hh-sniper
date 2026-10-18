"""Тесты снятия неактивных откликов: классификация, dry-run, --apply."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from snpr.cleanup import classify, run_cleanup
from snpr.nanny import normalize_negotiation

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _rec(
    neg_id: int,
    state: str = "response",
    updated_days_ago: float = 1.0,
) -> dict:
    updated = NOW - timedelta(days=updated_days_ago)
    return normalize_negotiation(
        {
            "id": neg_id,
            "state": {"id": state, "name": "x"},
            "updated_at": updated.isoformat(),
            "counters": {},
            "vacancy": {
                "id": neg_id * 10,
                "name": f"Вакансия {neg_id}",
                "employer": {"id": neg_id * 100, "name": f"Работодатель {neg_id}"},
            },
        }
    )


def _item(neg_id: int, state: str, updated_days_ago: float) -> dict:
    updated = NOW - timedelta(days=updated_days_ago)
    return {
        "id": neg_id,
        "state": {"id": state, "name": "x"},
        "updated_at": updated.isoformat(),
        "viewed_by_opponent": False,
        "counters": {"messages": 0, "unread_messages": 0},
        "vacancy": {
            "id": neg_id * 10,
            "name": f"Вакансия {neg_id}",
            "employer": {"id": neg_id * 100, "name": f"Работодатель {neg_id}"},
        },
    }


class FakeCleanupApi:
    def __init__(self, items: list[dict], fail_ids: set[int] | None = None):
        self.items = items
        self.fail_ids = fail_ids or set()
        self.get_calls: list[str] = []
        self.delete_calls: list[tuple[str, dict]] = []

    def get(self, endpoint, params=None):
        self.get_calls.append(endpoint)
        if endpoint == "/negotiations":
            return {"items": self.items, "pages": 1, "found": len(self.items)}
        raise AssertionError(f"unexpected GET {endpoint}")

    def delete(self, endpoint, params=None):
        self.delete_calls.append((endpoint, dict(params or {})))
        neg_id = int(endpoint.rsplit("/", 1)[-1])
        if neg_id in self.fail_ids:
            raise RuntimeError("hh стал капризным")
        return {}


def _args(**over) -> argparse.Namespace:
    base = {
        "status": "active",
        "older_than": 14.0,
        "apply": False,
        "with_decline_message": False,
        "verbose": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_classify_discard_is_dead() -> None:
    assert classify(_rec(1, "discard"), NOW, 14.0) == "remove"


def test_classify_response_old_is_dead() -> None:
    assert classify(_rec(1, "response", 20), NOW, 14.0) == "remove"


def test_classify_response_fresh_kept() -> None:
    assert "свежий" in classify(_rec(1, "response", 2), NOW, 14.0)


def test_classify_progress_states_kept() -> None:
    for state in ("phone_interview", "invitation", "offer", "hired"):
        assert "живой" in classify(_rec(1, state, 60), NOW, 14.0), state


def test_classify_unknown_state_kept() -> None:
    assert "не трогаем" in classify(_rec(1, "teleportation"), NOW, 14.0)


def test_run_cleanup_dry_run_shows_without_delete(capsys) -> None:
    api = FakeCleanupApi(
        [
            _item(1, "discard", 1),
            _item(2, "response", 20),
            _item(3, "invitation", 30),
        ]
    )
    assert run_cleanup(_args(), api) == 0
    out = capsys.readouterr().out
    assert "БЫ СНЯЛ" in out and "--apply" in out
    assert api.delete_calls == []
    assert "Вакансия 1" in out and "Вакансия 2" in out
    assert "Вакансия 3" not in out


def test_run_cleanup_apply_deletes(capsys) -> None:
    api = FakeCleanupApi(
        [_item(1, "discard", 1), _item(2, "response", 20), _item(3, "response", 2)]
    )
    assert run_cleanup(_args(apply=True), api) == 0
    assert [c[0] for c in api.delete_calls] == [
        "/negotiations/active/1",
        "/negotiations/active/2",
    ]
    assert all(c[1] == {"with_decline_message": "false"} for c in api.delete_calls)
    out = capsys.readouterr().out
    assert "Снято: 2 из 2" in out


def test_run_cleanup_decline_message_flag(capsys) -> None:
    api = FakeCleanupApi([_item(1, "discard", 1)])
    run_cleanup(_args(apply=True, with_decline_message=True), api)
    assert api.delete_calls[0][1] == {"with_decline_message": "true"}


def test_run_cleanup_delete_error_continues(capsys) -> None:
    api = FakeCleanupApi(
        [_item(1, "discard", 1), _item(2, "discard", 1)], fail_ids={1}
    )
    assert run_cleanup(_args(apply=True), api) == 0
    assert len(api.delete_calls) == 2
    out = capsys.readouterr().out
    assert "ошибка снятия" in out and "Снято: 1 из 2" in out


def test_run_cleanup_nothing_dead(capsys) -> None:
    api = FakeCleanupApi([_item(1, "response", 1), _item(2, "invitation", 1)])
    assert run_cleanup(_args(), api) == 0
    assert "Неактивных откликов нет" in capsys.readouterr().out
    assert api.delete_calls == []


def test_run_cleanup_verbose_lists_kept(capsys) -> None:
    api = FakeCleanupApi([_item(1, "response", 1), _item(2, "invitation", 30)])
    run_cleanup(_args(verbose=True), api)
    out = capsys.readouterr().out
    assert "оставить [2]" in out and "живой статус" in out
    assert "оставить [1]" in out and "свежий" in out
