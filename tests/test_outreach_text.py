"""Тесты текстовых утилит: esc/translit/similarity/normalize (ПЛАН §10)."""
from __future__ import annotations

from snpr.outreach.text import (
    company_matches,
    esc,
    normalize_company,
    similarity,
    translit,
    unescape,
)


def test_esc_unescapes_and_escapes_pipe():
    assert esc("A &amp; B") == "A & B"
    assert esc("a|b") == "a\\|b"
    assert esc("line1\nline2") == "line1 line2"
    assert esc(None) == ""
    assert esc("  x  ") == "x"


def test_unescape():
    assert unescape("&lt;b&gt;") == "<b>"
    assert unescape(None) == ""


def test_translit():
    assert translit("Авито") == "avito"
    assert translit("Машинное обучение") == "mashinnoeobuchenie"
    assert translit("Yandex") == "yandex"


def test_similarity_identical_and_unrelated():
    assert similarity("avito", "avito") == 1.0
    assert similarity("avito", "ozon") == 0.0


def test_similarity_cyrillic_latin_same_word():
    # «контур» и «kontur» дают разные биграммы до нормализации,
    # но после normalize_company — совпадают (тест ниже)
    assert similarity("контур", "kontur") < 1.0


def test_normalize_company_drops_legal_forms():
    assert normalize_company("ООО «Авито»") == "avito"
    assert normalize_company("Авито Групп") == "avito"
    assert normalize_company("avito") == "avito"
    assert normalize_company("Контур") == normalize_company("kontur")
    assert normalize_company("") == ""
    assert normalize_company(None) == ""


def test_normalize_yandex_sber():
    assert normalize_company("Яндекс") == "yandeks"
    assert normalize_company("Сбер") == "sber"
    assert normalize_company("ПАО Сбербанк") != ""


def test_company_matches_cross_language():
    assert company_matches("ООО «Контур»", "kontur") is True
    assert company_matches("Авито", "avito") is True
    assert company_matches("Ozon Tech", "ozon") is True
    assert company_matches("Some Random LLC", "avito") is False
