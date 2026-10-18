from __future__ import annotations

import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from sniper.filters import PipelineFilter

FIXTURES = Path(__file__).parent / "fixtures" / "vacancies.json"
NOW = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)


def _gates_on() -> dict:
    """Живой config.toml со всеми гейтами явно включёнными.

    С 17.10 config.toml держит vendor/grade/gov/geo выключенными (решение
    пользователя: fit решает LLM-скоринг). Тесты логики гейтов обязаны
    проверять её независимо от текущего положения рубильников.
    """
    with (Path(__file__).parent.parent / "sniper" / "config.toml").open(
        "rb"
    ) as fh:
        cfg = tomllib.load(fh)
    for section in ("grade", "vendor", "gov", "geo"):
        cfg.setdefault(section, {})["enabled"] = True
    return cfg


GATES_ON = _gates_on()

VENDOR_DESCRIPTION = (
    "Мы аутсорс-команда, разрабатываем решения под ключ для наших заказчиков "
    "из финансового сектора."
)
GOV_DESCRIPTION = (
    "Работа в рамках государственного заказа по 44-ФЗ, сопровождение "
    "информационных систем федерального ведомства."
)


def load_vacancies() -> list[dict]:
    with FIXTURES.open(encoding="utf-8") as fh:
        return json.load(fh)


def by_id(vacancies: list[dict], vid: str) -> dict:
    return next(v for v in vacancies if v["id"] == vid)


def test_v01_pass_clean_senior_hybrid_ekb() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v01"), now=NOW)
    assert verdict.passed, verdict.reject_reasons


def test_v02_reject_junior_grade() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v02"), now=NOW)
    assert not verdict.passed
    assert any(r.rule == "grade" for r in verdict.rules if not r.passed)


def test_v03_reject_internship() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v03"), now=NOW)
    assert not verdict.passed
    assert any(r.rule == "grade" for r in verdict.rules if not r.passed)


def test_v04_reject_no_experience() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v04"), now=NOW)
    assert not verdict.passed
    reasons = [r.rule for r in verdict.rules if not r.passed]
    assert "grade" in reasons


def test_v05_reject_vendor_employer() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v05"), now=NOW)
    assert not verdict.passed
    assert any(r.rule == "vendor" for r in verdict.rules if not r.passed)


def test_v06_reject_vendor_description() -> None:
    vf = PipelineFilter(GATES_ON)
    vacancy = by_id(load_vacancies(), "v06")
    verdict = vf.check(vacancy, description=VENDOR_DESCRIPTION, now=NOW)
    assert not verdict.passed
    assert any(r.rule == "vendor" for r in verdict.rules if not r.passed)


def test_v06_pass_without_description() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v06"), now=NOW)
    assert verdict.passed, verdict.reject_reasons


def test_v07_reject_gov_employer() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v07"), now=NOW)
    assert not verdict.passed
    assert any(r.rule == "gov" for r in verdict.rules if not r.passed)


def test_v08_reject_gov_rzd() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v08"), now=NOW)
    assert not verdict.passed
    assert any(r.rule == "gov" for r in verdict.rules if not r.passed)


def test_v09_reject_salary_floor() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v09"), now=NOW)
    assert not verdict.passed
    assert any(r.rule == "salary" for r in verdict.rules if not r.passed)


def test_v10_pass_borderline_top_salary() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v10"), now=NOW)
    assert verdict.passed, verdict.reject_reasons


def test_v11_reject_geo_moscow_office() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v11"), now=NOW)
    assert not verdict.passed
    assert any(r.rule == "geo" for r in verdict.rules if not r.passed)


def test_v12_pass_usd_remote() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v12"), now=NOW)
    assert verdict.passed, verdict.reject_reasons


def test_v13_reject_middle_grade() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v13"), now=NOW)
    assert not verdict.passed
    assert any(r.rule == "grade" for r in verdict.rules if not r.passed)


def test_v14_pass_without_freshness_and_reject_with() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v14"), now=NOW)
    assert verdict.passed, verdict.reject_reasons

    vf_fresh = PipelineFilter({**GATES_ON, "max_age_hours": 48})
    verdict_fresh = vf_fresh.check(by_id(load_vacancies(), "v14"), now=NOW)
    assert not verdict_fresh.passed
    assert any(
        r.rule == "freshness" for r in verdict_fresh.rules if not r.passed
    )


def test_v15_reject_vendor_lce() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v15"), now=NOW)
    assert not verdict.passed
    assert any(r.rule == "vendor" for r in verdict.rules if not r.passed)


def test_v16_reject_gov_description_fzu() -> None:
    vf = PipelineFilter(GATES_ON)
    vacancy = by_id(load_vacancies(), "v16")
    verdict = vf.check(vacancy, description=GOV_DESCRIPTION, now=NOW)
    assert not verdict.passed
    assert any(r.rule == "gov" for r in verdict.rules if not r.passed)


def test_v16_reject_gov_university() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v16"), now=NOW)
    assert not verdict.passed
    assert any(r.rule == "gov" for r in verdict.rules if not r.passed)


def test_salary_pass_when_no_salary_stated() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v16"), now=NOW)
    salary_rules = [r for r in verdict.rules if r.rule == "salary"]
    assert salary_rules[0].passed


def _salary_rule(vacancy: dict):
    verdict = PipelineFilter(GATES_ON).check(vacancy, now=NOW)
    return next(r for r in verdict.rules if r.rule == "salary")


BASE_VACANCY = {"id": "t-salary", "name": "Senior ML Engineer"}


def test_salary_hard_floor_200k_net() -> None:
    # 150-200К gross ≈ 174К net: ниже рубильника 200К — отклон без скоринга
    rule = _salary_rule(
        {
            **BASE_VACANCY,
            "salary": {"from": 150000, "to": 200000, "currency": "RUR", "gross": True},
        }
    )
    assert not rule.passed


def test_salary_200_400_not_blocked_goes_to_scoring() -> None:
    # 200-350К gross ≈ 174-305К net: верх выше рубильника — не блок,
    # «разбираемся по месту» (скоринг видит ориентир 400К)
    rule = _salary_rule(
        {
            **BASE_VACANCY,
            "salary": {"from": 200000, "to": 350000, "currency": "RUR", "gross": True},
        }
    )
    assert rule.passed


def test_salary_borderline_top_just_above_floor() -> None:
    # верх 230К gross ≈ 200.1К net — едва выше рубильника, проходит
    rule = _salary_rule(
        {
            **BASE_VACANCY,
            "salary": {"from": 150000, "to": 230000, "currency": "RUR", "gross": True},
        }
    )
    assert rule.passed


def test_salary_no_top_low_from_still_allowed() -> None:
    # только «от 180К», верха нет: диапазон открыт вверх — не блок
    # (рубильник для from-only: ниже 50% от floor)
    rule = _salary_rule(
        {
            **BASE_VACANCY,
            "salary": {"from": 180000, "currency": "RUR", "gross": True},
        }
    )
    assert rule.passed


def test_verdict_reports_reject_reasons() -> None:
    vf = PipelineFilter(GATES_ON)
    verdict = vf.check(by_id(load_vacancies(), "v09"), now=NOW)
    assert verdict.reject_reasons
    assert all(isinstance(x, str) for x in verdict.reject_reasons)


def test_gates_disabled_only_salary_left() -> None:
    """17.10: vendor/grade/gov/geo выключены — fit решает LLM-скоринг.

    Москва-офис (v11) проходит: правило одно — зарплатный рубильник.
    """
    cfg = {**GATES_ON}
    for section in ("grade", "vendor", "gov", "geo"):
        cfg[section] = {**cfg[section], "enabled": False}
    vf = PipelineFilter(cfg)
    verdict = vf.check(by_id(load_vacancies(), "v11"), now=NOW)
    assert verdict.passed, verdict.reject_reasons
    assert [r.rule for r in verdict.rules] == ["salary"]


def test_gates_enabled_by_default() -> None:
    """Секция без ключа enabled = гейт работает (обратная совместимость)."""
    vf = PipelineFilter(
        {"grade": {"exclude_experience": ["between1And3"]}}
    )
    verdict = vf.check(
        {"id": "x", "name": "AI", "experience": {"id": "between1And3"}},
        now=NOW,
    )
    assert any(
        r.rule == "grade" and not r.passed for r in verdict.rules
    )


def test_config_toml_has_user_gates_off() -> None:
    """Живой конфиг: решение 17.10 — четыре гейта сняты, зарплата осталась."""
    with (Path(__file__).parent.parent / "sniper" / "config.toml").open(
        "rb"
    ) as fh:
        cfg = tomllib.load(fh)
    for section in ("grade", "vendor", "gov", "geo"):
        assert cfg[section]["enabled"] is False, section
    assert "enabled" not in cfg  # зарплатный рубильник — не выключается
