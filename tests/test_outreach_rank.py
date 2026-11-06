"""Тесты ранжирования: стэк-матч (регресс «ai в detail»), персоны, тиры
Habr, ML-фильтр, робо-детект (ПЛАН §10)."""
from __future__ import annotations

from snpr.outreach.rank import (
    habr_tier_score,
    is_robot,
    ml_vacancy_filter,
        stack_match,
)


def test_stack_match_regression_ai_in_detail():
    """Регресс исследования 03.11: 'ai' НЕ должно матчиться в 'detail'."""
    score, matched = stack_match("detailed design document", ["ai", "llm"])
    assert "ai" not in matched
    assert score == 0.0


def test_stack_match_whole_words():
    score, matched = stack_match("python ml llm engineer", ["python", "llm", "rag"])
    assert "python" in matched and "llm" in matched
    assert "rag" not in matched
    assert abs(score - 2 / 3) < 1e-9


def test_stack_match_multiterm():
    score, matched = stack_match(
        "Senior machine learning platform", ["machine learning", "nlp"]
    )
    assert "machine learning" in matched


def test_stack_match_empty():
    assert stack_match("text", []) == (0.0, [])
    assert stack_match(None, ["ai"]) == (0.0, [])


def test_is_robot():
    assert is_robot("github-actions[bot]")
    assert is_robot("jenkins-ci")
    assert is_robot("svc-deploy")
    assert is_robot("ivan", "ivan@avito.ru") is False
    assert is_robot(None) is False


def test_habr_tier_score_caps():
    rec = {
        "karma": 1000.0,
        "article_count": 100,
        "speciality": "ml engineer",
        "habr_companies": ["avito", "sber"],
        "fresh_days": 0,
    }
    score = habr_tier_score(rec)
    assert 0 < score <= 110


def test_habr_tier_score_empty():
    assert habr_tier_score({}) == 0


def test_ml_vacancy_filter():
    assert ml_vacancy_filter("Senior ML Engineer", "")
    assert ml_vacancy_filter("Разработчик LLM-агентов", "")
    assert ml_vacancy_filter("Инженер машинного обучения", "")
    assert ml_vacancy_filter("Веб-разработчик", "django react") is False


def test_ml_vacancy_filter_in_description():
    assert ml_vacancy_filter("Python developer", "стек: computer vision, torch")
