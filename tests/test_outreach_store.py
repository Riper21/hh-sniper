"""Тесты сторов: upsert-атомарность, ключи, campaign-log, TSV (ПЛАН §10)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from snpr.outreach.store import (
    CampaignLog,
    JsonlStore,
    load_jsonl,
    read_tsv,
    touch_checked_at,
)


def test_upsert_insert_then_update(tmp_path: Path):
    store = JsonlStore(tmp_path / "s.jsonl", "name")
    assert store.upsert({"name": "Avito", "email": "a@x"}) is True
    assert store.upsert({"name": "Avito", "email": "b@x"}) is True
    recs = store.load()
    assert len(recs) == 1
    assert recs[0]["email"] == "b@x"


def test_upsert_merges_fields(tmp_path: Path):
    store = JsonlStore(tmp_path / "s.jsonl", "k")
    store.upsert({"k": "x", "a": 1, "b": 2})
    store.upsert({"k": "x", "b": 3})
    rec = store.load()[0]
    assert rec["a"] == 1 and rec["b"] == 3


def test_upsert_missing_key_raises(tmp_path: Path):
    store = JsonlStore(tmp_path / "s.jsonl", "name")
    with pytest.raises(KeyError):
        store.upsert({"email": "x"})


def test_keys_and_load_map(tmp_path: Path):
    store = JsonlStore(tmp_path / "s.jsonl", "login")
    store.upsert_many([{"login": "a"}, {"login": "b"}, {"login": "a", "v": 1}])
    assert store.keys() == {"a", "b"}
    assert store.load_map()["a"]["v"] == 1


def test_upsert_many_counts_only_changes(tmp_path: Path):
    store = JsonlStore(tmp_path / "s.jsonl", "k")
    store.upsert_many([{"k": "1"}, {"k": "2"}])
    changed = store.upsert_many([{"k": "1"}, {"k": "3"}])
    assert changed == 1
    assert store.keys() == {"1", "2", "3"}


def test_broken_lines_skipped(tmp_path: Path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        '{"k": "a"}\nnot-json\n{"k": "b"}\n\n', encoding="utf-8"
    )
    store = JsonlStore(path, "k")
    assert store.keys() == {"a", "b"}


def test_atomic_no_tmp_left(tmp_path: Path):
    store = JsonlStore(tmp_path / "s.jsonl", "k")
    store.upsert({"k": "x"})
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_campaign_log_append_and_filter(tmp_path: Path):
    log = CampaignLog(tmp_path / "events.jsonl")
    log.append("email:a@x", "queued", "первый контакт")
    log.append("email:b@x", "sent")
    log.append("email:a@x", "replied", "ответил")
    events = log.events_for("email:a@x")
    assert [e["event"] for e in events] == ["queued", "replied"]
    assert events[0]["note"] == "первый контакт"
    assert events[1]["at"] >= events[0]["at"]


def test_touch_checked_at(tmp_path: Path):
    rec = touch_checked_at({"name": "x"})
    assert "checked_at" in rec
    import datetime as dt

    assert dt.datetime.fromisoformat(rec["checked_at"])


def test_load_jsonl_missing_and_ok(tmp_path: Path):
    assert load_jsonl(tmp_path / "nope.jsonl") == []
    path = tmp_path / "d.jsonl"
    path.write_text('{"a": 1}\nbroken\n{"b": 2}\n', encoding="utf-8")
    assert len(load_jsonl(path)) == 2


def test_read_tsv(tmp_path: Path):
    path = tmp_path / "companies.tsv"
    path.write_text(
        "name\tgroup\tpriority\tconferences\tgithub_hint\n"
        "Авито\tEcommerce\tA\taij;df\tavito-tech\n",
        encoding="utf-8",
    )
    rows = read_tsv(path)
    assert rows[0]["name"] == "Авито"
    assert rows[0]["github_hint"] == "avito-tech"
    assert rows[0]["priority"] == "A"
