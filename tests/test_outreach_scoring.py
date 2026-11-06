"""Тесты LLM-скоринга карьер-вакансий (§10)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from snpr.outreach.scoring import (
    record_description,
    record_to_vacancy,
    run_scoring,
)
from snpr.outreach.store import JsonlStore
from snpr.scoring import VacancyScorer


class FakeAI:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[str] = []

    def complete(self, message: str, **kwargs: Any) -> str:
        self.messages.append(message)
        return self.response


GOOD_JSON = (
    '{"score": 71, "verdict": "maybe", "reasons": ["RAG, агенты"], '
    '"matched": ["LLM"], "missing": ["K8s"]}'
)


def make_record(**extra: Any) -> dict[str, Any]:
    base = {
        "source": "career_site",
        "platform": "alfa-api",
        "company": "Альфа-Банк",
        "vacancy_id": "123",
        "vacancy_name": "Ведущий ML-инженер",
        "manager_email": "hr@alfabank.ru",
        "manager_name": "HR",
        "vacancy_url": "https://job.alfabank.ru/vacancies/123",
        "extra": {
            "duties": "строить RAG-конвейер",
            "requirements": "Python, LLM, LangGraph",
            "conditions": "гибрид",
        },
    }
    base.update(extra)
    return base


def test_run_scoring_writes_scored_at(tmp_path: Path):
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    store.upsert(make_record())
    scorer = VacancyScorer(FakeAI(GOOD_JSON))
    run_scoring(store, scorer)
    rec = store.load_map()["123"]
    # 06.11 фикс: момент скоринга для колонки «Когда» в UI (unix-сек)
    assert isinstance(rec.get("scored_at"), int)
    assert rec["scored_at"] > 1_700_000_000


def test_record_to_vacancy():
    rec = make_record(salary={"from": 300000, "to": 400000}, area="Москва")
    vac = record_to_vacancy(rec)
    assert vac["id"] == "123"
    assert vac["name"] == "Ведущий ML-инженер"
    assert vac["employer"]["name"] == "Альфа-Банк"
    assert vac["salary"]["to"] == 400000
    assert vac["area"]["name"] == "Москва"


def test_record_description():
    desc = record_description(make_record())
    assert "RAG" in desc and "LangGraph" in desc
    assert record_description(make_record(extra={})) == ""


def test_run_scoring_writes_store(tmp_path: Path):
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    store.upsert(make_record())
    scorer = VacancyScorer(FakeAI(GOOD_JSON))
    scored = run_scoring(store, scorer)
    assert scored == 1
    rec = store.load_map()["123"]
    assert rec["score"] == 71
    assert rec["verdict"] == "maybe"
    assert "RAG, агенты" in rec["score_reasons"]
    assert rec["score_matched"] == ["LLM"]
    assert rec["manager_email"] == "hr@alfabank.ru"  # контакт цел


def test_run_scoring_idempotent(tmp_path: Path):
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    store.upsert(make_record())
    ai = FakeAI(GOOD_JSON)
    scorer = VacancyScorer(ai)
    run_scoring(store, scorer)
    # второй прогон: уже заскорено — 0 вызовов LLM
    assert run_scoring(store, scorer) == 0
    assert len(ai.messages) == 1
    # --force перескоривает
    assert run_scoring(store, scorer, force=True) == 1
    assert len(ai.messages) == 2


def test_run_scoring_skips_no_description(tmp_path: Path):
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    store.upsert(make_record(extra={}))  # без описания
    scorer = VacancyScorer(FakeAI(GOOD_JSON))
    assert run_scoring(store, scorer) == 0


def test_run_scoring_bad_json_skipped(tmp_path: Path):
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    store.upsert(make_record())
    scorer = VacancyScorer(FakeAI("не JSON вообще"))
    assert run_scoring(store, scorer) == 0
    assert store.load_map()["123"].get("score") is None


def test_run_scoring_dry(tmp_path: Path):
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    store.upsert(make_record())
    scorer = VacancyScorer(FakeAI(GOOD_JSON))
    assert run_scoring(store, scorer, dry=True) == 0
    assert store.load_map()["123"].get("score") is None


def test_run_scoring_limit(tmp_path: Path):
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    store.upsert(make_record(vacancy_id="1"))
    store.upsert(make_record(vacancy_id="2"))
    scorer = VacancyScorer(FakeAI(GOOD_JSON))
    assert run_scoring(store, scorer, limit=1) == 1
    recs = store.load_map()
    assert sum(1 for r in recs.values() if r.get("score")) == 1
