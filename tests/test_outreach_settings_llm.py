"""Тесты LLM-черновика (06.11): JSON-контракт, fallback на эвристику."""
from __future__ import annotations

from pathlib import Path

from snpr.outreach.settings import (
    profile_from_resume,
    profile_from_resume_llm,
)

RESUME = """Иван Тестов
Желаемая должность: QA инженер
ivan@test.ru
Опыт работы — 5 лет

КЛЮЧЕВЫЕ НАВЫКИ
Selenium; Pytest; Java; Docker; Git
"""

GOOD_LLM_JSON = """{
 "scoring_profile": "- QA-инженер (AQA): Selenium/Pytest, 5 лет\\n- Грейд: middle+\\n- Формат: удалёнка",
 "letter_profile": "Факты:\\n- 5 лет в QA\\n- Selenium, Pytest",
 "role_keywords": ["qa", "aqa", "тестировщик", "selenium", "pytest"],
 "target_stack": ["pytest", "selenium", "java", "docker"],
 "title": "QA инженер",
 "email": "ivan@test.ru",
 "telegram": ""
}"""


class FakeAI:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[str] = []

    def complete(self, message: str, **kwargs) -> str:
        self.calls.append(message)
        return self.response


def test_llm_draft_parses_and_maps():
    ai = FakeAI(GOOD_LLM_JSON)
    draft = profile_from_resume_llm(RESUME, ai)
    assert draft is not None
    assert draft["title"] == "QA инженер"
    assert "middle+" in draft["scoring_profile"]
    assert draft["role_keywords"] == [
        "qa", "aqa", "тестировщик", "selenium", "pytest"
    ]
    assert draft["target_stack"] == ["pytest", "selenium", "java", "docker"]
    assert draft["email"] == "ivan@test.ru"
    assert ai.calls and "РЕЗЮМЕ" in ai.calls[0]


def test_llm_draft_bad_json_returns_none():
    ai = FakeAI("не JSON совсем")
    assert profile_from_resume_llm(RESUME, ai) is None


def test_llm_draft_ai_error_returns_none():
    class Boom:
        def complete(self, *a, **k):
            raise RuntimeError("llm down")

    assert profile_from_resume_llm(RESUME, Boom()) is None


def test_llm_draft_empty_profile_returns_none():
    ai = FakeAI('{"scoring_profile": "", "role_keywords": []}')
    assert profile_from_resume_llm(RESUME, ai) is None


def test_heuristic_fallback_improved():
    # эвристика теперь понимает «КЛЮЧЕВЫЕ НАВЫКИ» и `;`-разделители
    draft = profile_from_resume(RESUME)
    assert "selenium" in draft["stack_keywords"]
    assert "pytest" in draft["stack_keywords"]
    assert "Опыт: 5 лет" in draft["scoring_profile"]
