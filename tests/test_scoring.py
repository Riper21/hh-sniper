from __future__ import annotations

import json
from typing import Any

import pytest

from snpr.scoring import (
    LLMUnavailable,
    ScoreResult,
    VacancyScorer,
    _extract_json,
    _strip_html,
    _work_format_line,
)

VACANCY = {
    "id": "777",
    "name": "Senior ML Engineer (LLM evaluation)",
    "employer": {"id": "1", "name": "ООО Пример"},
    "salary": {"from": 300000, "to": 450000, "currency": "RUR", "gross": True},
    "area": {"id": "1261", "name": "Екатеринбург"},
    "work_format": [{"id": "HYBRID"}],
    "experience": {"id": "between3And6"},
    "key_skills": [{"name": "Python"}, {"name": "LLM"}],
}


class FakeAI:
    def __init__(self, response: str):
        self.response = response
        self.last_message: str | None = None
        self.last_system: str | None = None

    def complete(self, message: str, **kwargs: Any) -> str:
        self.last_message = message
        self.last_system = kwargs.get("system_prompt")
        return self.response


class FlakyAI(FakeAI):
    """Первые failures попыток роняют с OpenAIError, дальше — ответ."""

    def __init__(self, response: str, failures: int, error: Exception):
        super().__init__(response)
        self.failures = failures
        self.error = error
        self.calls = 0

    def complete(self, message: str, **kwargs: Any) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return super().complete(message, **kwargs)


TARGET_JSON = json.dumps(
    {
        "score": 82,
        "verdict": "target",
        "reasons": ["evals в проде, якорная функция"],
        "matched": ["Python", "LLM"],
        "missing": ["Go"],
    },
    ensure_ascii=False,
)


def test_score_parses_plain_json() -> None:
    ai = FakeAI(TARGET_JSON)
    scorer = VacancyScorer(ai)
    result = scorer.score(VACANCY, description="Описание вакансии")
    assert result.parse_ok
    assert result.score == 82
    assert result.verdict == "target"
    assert result.is_target
    assert "Python" in result.matched


def test_score_parses_fenced_json() -> None:
    ai = FakeAI(f"Вот оценка:\n```json\n{TARGET_JSON}\n```\nСпасибо.")
    scorer = VacancyScorer(ai)
    result = scorer.score(VACANCY)
    assert result.parse_ok
    assert result.score == 82


def test_score_bad_json_marks_parse_fail() -> None:
    ai = FakeAI("Определённо подходящая вакансия, ставлю 90!")
    scorer = VacancyScorer(ai)
    result = scorer.score(VACANCY)
    assert not result.parse_ok
    assert result.verdict == "pass"


def test_score_invalid_verdict_falls_back_to_threshold() -> None:
    payload = json.dumps({"score": 60, "verdict": "отлично"})
    scorer = VacancyScorer(FakeAI(payload))
    result = scorer.score(VACANCY)
    assert result.verdict == "maybe"


def test_score_derives_verdict_when_missing() -> None:
    payload = json.dumps({"score": 90})
    scorer = VacancyScorer(FakeAI(payload))
    result = scorer.score(VACANCY)
    assert result.verdict == "target"


def test_score_custom_maybe_threshold_45() -> None:
    """18.10: порог письма 50 → 45 — граница переключается с конфигом."""
    scorer = VacancyScorer(FakeAI(json.dumps({"score": 45})), maybe_threshold=45)
    assert scorer.score(VACANCY).verdict == "maybe"

    scorer44 = VacancyScorer(FakeAI(json.dumps({"score": 44})), maybe_threshold=45)
    assert scorer44.score(VACANCY).verdict == "pass"

    # дефолт не тронут: 45 ниже стандартного порога 50
    scorer_default = VacancyScorer(FakeAI(json.dumps({"score": 45})))
    assert scorer_default.score(VACANCY).verdict == "pass"


def test_prompt_contains_vacancy_fields() -> None:
    ai = FakeAI(TARGET_JSON)
    scorer = VacancyScorer(ai)
    scorer.score(VACANCY, description="Текст про evals и агентов")
    assert ai.last_message is not None
    assert "Senior ML Engineer (LLM evaluation)" in ai.last_message
    assert "ООО Пример" in ai.last_message
    assert "300000-450000 RUR (gross)" in ai.last_message
    assert "Текст про evals и агентов" in ai.last_message
    assert "Формат работы: гибрид" in ai.last_message
    assert ai.last_system is not None
    assert "Senior/Lead" in ai.last_system


def test_work_format_line_variants() -> None:
    assert _work_format_line({"work_format": [{"id": "REMOTE"}]}) == "удалёнка"
    assert _work_format_line({"work_format": [{"id": "OFFICE"}]}) == "офис"
    assert (
        _work_format_line(
            {"work_format": [{"id": "HYBRID"}, {"id": "HYBRID"}]}
        )
        == "гибрид"
    )
    assert _work_format_line({"work_format": []}) == "не указан"
    assert _work_format_line({}) == "не указан"
    assert _work_format_line({"work_format": "remote"}) == "удалёнка"


def test_prompt_remote_format_labeled() -> None:
    ai = FakeAI(TARGET_JSON)
    VacancyScorer(ai).score(
        {**VACANCY, "work_format": [{"id": "REMOTE"}]}
    )
    assert ai.last_message is not None
    assert "Формат работы: удалёнка" in ai.last_message


def test_extract_json_from_noise() -> None:
    text = f"Разбор:\n{TARGET_JSON}\nКомментарий после."
    data = _extract_json(text)
    assert data["score"] == 82


def test_scorer_result_defaults() -> None:
    result = ScoreResult(vacancy_id="1")
    assert not result.is_target
    assert result.score == 0
    assert result.reasons == []


def test_strip_html_shrinks_prompt_payload() -> None:
    """HH отдаёт описание HTML-кодом; шлюз 502-ит на тяжёлых запросах."""
    raw = "<p>Нужны <b>Python</b> &amp; Go</p><ul><li>evals</li></ul>"
    cleaned = _strip_html(raw)
    assert "<" not in cleaned and ">" not in cleaned
    assert "Python & Go" in cleaned
    assert "evals" in cleaned
    assert len(cleaned) < len(raw)
    assert _strip_html("") == ""
    assert _strip_html("обычный текст") == "обычный текст"


def test_prompt_has_html_stripped_description() -> None:
    ai = FakeAI(TARGET_JSON)
    VacancyScorer(ai).score(VACANCY, description="<p>Опыт с LLM</p>")
    assert ai.last_message is not None
    assert "Опыт с LLM" in ai.last_message
    assert "<p>" not in ai.last_message


def test_score_retries_transient_llm_error() -> None:
    """Живой кейс 16.10: 502 от шлюза валил весь прогон — ретраим."""
    ai = FlakyAI(TARGET_JSON, failures=1, error=RuntimeError("502 Bad Gateway"))
    scorer = VacancyScorer(ai, retries=1, retry_backoff=0.0)
    result = scorer.score(VACANCY)
    assert ai.calls == 2
    assert result.score == 82


def test_score_raises_llm_unavailable_after_retries() -> None:
    ai = FlakyAI(TARGET_JSON, failures=9, error=RuntimeError("502 Bad Gateway"))
    scorer = VacancyScorer(ai, retries=1, retry_backoff=0.0)
    with pytest.raises(LLMUnavailable) as exc:
        scorer.score(VACANCY)
    assert "2 попыток" in str(exc.value)
    assert ai.calls == 2


def test_score_no_retry_by_default_still_retries_once() -> None:
    """Дефолт = одна попытка ретрая: единичный сбой шлюза не роняет прогон."""
    ai = FlakyAI(TARGET_JSON, failures=1, error=RuntimeError("503"))
    result = VacancyScorer(ai, retry_backoff=0.0).score(VACANCY)
    assert result.verdict == "target"
