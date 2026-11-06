"""Тесты конфигурируемой роли (06.11): role_keywords во всех фильтрах."""
from __future__ import annotations

import pytest

from snpr.outreach.config import outreach_config
from snpr.outreach.career.sber import ROLE_SLUG_HINTS, slug_is_ml
from snpr.outreach.rank import ml_vacancy_filter

QA_KEYWORDS = [
    "qa", "aqa", "тестировщик", "тестирование", "quality",
    "selenium", "pytest", "playwright", "автотест",
]


def test_filter_default_ml_unchanged():
    assert ml_vacancy_filter("Senior ML Engineer")
    assert ml_vacancy_filter("Инженер по машинному обучению")
    assert not ml_vacancy_filter("Менеджер по продажам")


def test_filter_custom_role_qa():
    kw = QA_KEYWORDS
    assert ml_vacancy_filter("QA Fullstack java", keywords=kw)
    # «тестировщик» — подстрока: ловит «тестировщику/тестировщика»,
    # но «эксперт по ТЕСТИРОВАНИЮ» — другой корень: в пресете нужен
    # и «тестирован» (морфология: -щик / -ание / -ания)
    assert ml_vacancy_filter("Инженер-тестировщик", keywords=kw)
    kw_full = kw + ["тестирован"]
    assert ml_vacancy_filter(
        "Старший эксперт по тестированию", keywords=kw_full
    )
    assert ml_vacancy_filter("Инженер по автоматизации (Selenium)", keywords=kw)
    # ML-вакансии НЕ проходят под QA-ролью — роль полностью заменяет словарь
    assert not ml_vacancy_filter("Senior ML Engineer", keywords=kw)
    assert not ml_vacancy_filter("LLM-разработчик", keywords=kw)
    # и менеджеры
    assert not ml_vacancy_filter("Менеджер по продажам", keywords=kw)


def test_filter_ml_passes_when_no_keywords():
    # keywords=None/[] — ML-дефолт
    assert ml_vacancy_filter("LLM-разработчик", keywords=None)
    assert ml_vacancy_filter("LLM-разработчик", keywords=[])


def test_filter_custom_description_match():
    kw = ["selenium", "pytest"]
    # название не QA, но описание содержит маркер
    assert ml_vacancy_filter("Инженер", "пишем автотесты на pytest", keywords=kw)
    assert not ml_vacancy_filter("Инженер", "пишем бэкенд", keywords=kw)


def test_slug_hints_role_presets():
    # пресет qa расширяется в слаг-хинты
    assert "qa" in ROLE_SLUG_HINTS
    assert slug_is_ml(
        "https://rabota.sber.ru/search/qa-inzhener-4583653/",
        keywords=["qa"],
    )
    assert slug_is_ml(
        "https://rabota.sber.ru/search/aqa-engineer-finstroy-ai-4583768/",
        keywords=["qa"],
    )
    # ML-слаг НЕ матчится под QA
    assert not slug_is_ml(
        "https://rabota.sber.ru/search/python-developer-4578715/",
        keywords=["qa"],
    )
    # без keywords — ML-дефолт как раньше
    assert slug_is_ml("https://rabota.sber.ru/search/python-developer-4578715/")
    assert not slug_is_ml("https://rabota.sber.ru/search/qa-inzhener-4583653/")


def test_slug_hints_latin_keyword_direct():
    # латинский keyword матчит слаг напрямую
    assert slug_is_ml(
        "https://rabota.sber.ru/search/react-razrabotchik-123/",
        keywords=["frontend"],
    )


def test_config_role_keywords_default_empty():
    cfg = outreach_config({})
    assert cfg["role_keywords"] == []
    cfg2 = outreach_config({"outreach": {"role_keywords": ["qa", "aqa"]}})
    assert cfg2["role_keywords"] == ["qa", "aqa"]
