"""Тесты title-гейта (06.11): вторая ступень префильтра до LLM.

Мотивация — аудит логов: 96% LLM-скоров = pass-мусор (врач, HR, SMM),
71% мусорных названий без токена роли, 15% со стоп-словом профессии.
Паттерн jobhunt: include-токены целым словом + exclude-стоп-слова +
«стаж 5+ лет» regex из названия.
"""
from __future__ import annotations

from snpr.filters import PipelineFilter


def _cfg(**title: object) -> dict:
    return {
        "floor_net_rub": 100,
        "grade": {"enabled": False},
        "vendor": {"enabled": False},
        "gov": {"enabled": False},
        "geo": {"enabled": False},
        "blacklist": {"enabled": False},
        "title": title or {"enabled": True},
    }


def _vac(name: str, salary_from: int = 300_000) -> dict:
    return {
        "id": "x1",
        "name": name,
        "employer": {"name": "E"},
        "salary": {"from": salary_from, "to": salary_from + 100_000, "currency": "RUR"},
        "area": {"id": "1"},
    }


def _title_rule(vac: dict) -> object:
    vf = PipelineFilter(_cfg())
    verdict = vf.check(vac)
    return next(r for r in verdict.rules if r.rule == "title")


# ---------------------------------------------------------------- include

def test_title_include_whole_word():
    """Короткий токен — только целым словом (регресс «ai в detail»)."""
    assert _title_rule(_vac("Senior ML Engineer")).passed
    assert _title_rule(_vac("LLM-разработчик")).passed
    assert _title_rule(_vac("AI Platform Engineer")).passed
    # 'ai' НЕ матчится в 'detail'/'maintain':
    assert not _title_rule(_vac("Detail-oriented manager")).passed


def test_title_include_long_substring():
    """Длинные/многословные токены — подстрокой."""
    assert _title_rule(_vac("Ведущий специалист по машинному обучению")).passed
    assert _title_rule(_vac("Data Scientist / Аналитик")).passed
    assert _title_rule(_vac("Инженер-тестировщик")).passed  # «инженер»


def test_title_no_token_reject():
    """71% мусора: название без единого токена роли → reject до LLM."""
    rule = _title_rule(_vac("Менеджер по работе с ключевыми клиентами"))
    assert not rule.passed
    assert "нет include-токена" in rule.reason
    rule2 = _title_rule(_vac("Коммерческий директор"))
    assert not rule2.passed


# ---------------------------------------------------------------- exclude

def test_title_stopword_profession():
    """15% мусора: стоп-слова профессий режут раньше include-поиска."""
    rule = _title_rule(_vac("Врач-стоматолог (терапия)"))
    assert not rule.passed
    assert "стоп-слово" in rule.reason
    assert not _title_rule(_vac("HR Business Partner")).passed
    assert not _title_rule(_vac("Бухгалтер 1С, удалённо")).passed
    assert not _title_rule(_vac("SMM-маркетолог")).passed


def test_title_stopword_custom_config():
    cfg = _cfg(enabled=True, exclude=["пилот"])
    vf = PipelineFilter(cfg)
    verdict = vf.check(_vac("Пилот гражданской авиации"))
    rule = next(r for r in verdict.rules if r.rule == "title")
    assert not rule.passed


# ------------------------------------------------------------- experience

def test_title_experience_regex():
    """«5+ лет» в названии: API-experience иногда врёт (jobhunt-трюк)."""
    cfg = _cfg(enabled=True, experience_max_years=4)
    vf = PipelineFilter(cfg)
    for name in (
        "Python разработчик 5+ лет",
        "Инженер от 6 лет опыта",
        "Team Lead 3–9 лет",
    ):
        verdict = vf.check(_vac(name))
        rule = next(r for r in verdict.rules if r.rule == "title")
        assert not rule.passed, name
        assert "стаж" in rule.reason
    # «от 2 лет» — не матчится (лимит 4)
    verdict2 = vf.check(_vac("Python от 2 лет"))
    rule2 = next(r for r in verdict2.rules if r.rule == "title")
    assert rule2.passed


# ---------------------------------------------------------------- control

def test_title_disabled_noop():
    cfg = _cfg(enabled=False)
    vf = PipelineFilter(cfg)
    verdict = vf.check(_vac("Врач-стоматолог"))
    assert all(r.rule != "title" for r in verdict.rules)


def test_title_empty_name_passes_to_scoring():
    rule = _title_rule({"id": "x", "name": "", "employer": {"name": "E"}})
    assert rule.passed


def test_title_custom_include_list():
    """QA-персона: include-словарь меняется конфигом (как role_keywords)."""
    cfg = _cfg(enabled=True, include=["qa", "aqa", "тестировщ", "тестирован"], exclude=[])
    vf = PipelineFilter(cfg)
    assert vf.check(_vac("QA Engineer")).passed
    assert vf.check(_vac("Инженер по тестированию")).passed  # «тестирован»
    # ML-вакансия НЕ проходит под QA-ролью (include полностью заменяет):
    verdict = vf.check(_vac("Senior ML Engineer"))
    assert not verdict.passed


def test_title_integration_pipeline_order():
    """title-гейт в общем вердикте: до salary НЕ важен, но до LLM — да."""
    vf = PipelineFilter(_cfg())
    verdict = vf.check(_vac("Врач-стоматолог"))
    assert not verdict.passed
    rules = [r.rule for r in verdict.rules]
    assert "title" in rules and "salary" in rules
