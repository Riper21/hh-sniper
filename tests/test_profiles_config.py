"""Тесты персонализации через config (18.10 ~12:40): профиль скоринга,
профиль-капсула и текст письма живут в config.toml, другой человек
меняет их без правки кода. Дефолты в коде — фоллбек."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomllib

from sniper.cli import _letter_profile, _letter_static_text, _scoring_profile
from sniper.cover_letter import (
    PROFILE_CAPSULE,
    STATIC_LETTER_TEXT,
    letter_system_prompt,
)
from sniper.letter_guard import extract_numbers
from sniper.scoring import DEFAULT_SCORING_PROFILE, VacancyScorer

CONFIG = (
    Path(__file__).parent.parent / "sniper" / "config.toml"
)


def _cfg() -> dict[str, Any]:
    with CONFIG.open("rb") as fh:
        return tomllib.load(fh)


def test_config_profiles_present_and_pinned() -> None:
    """Копии в конфиге байт-в-байт равны дефолтам кода (нет дрейфа)."""
    cfg = _cfg()
    assert cfg["scoring"]["profile"].strip() == DEFAULT_SCORING_PROFILE.strip()
    assert cfg["letter"]["profile"].strip() == PROFILE_CAPSULE.strip()
    assert (
        cfg["letter"]["static_text"].strip() == STATIC_LETTER_TEXT.strip()
    )


def test_config_profile_helpers() -> None:
    cfg = _cfg()
    assert _scoring_profile(cfg) == cfg["scoring"]["profile"].strip()
    assert _letter_profile(cfg) == cfg["letter"]["profile"].strip()
    assert _letter_static_text(cfg) == cfg["letter"]["static_text"].strip()
    # нет ключей — None (дефолты в коде)
    assert _scoring_profile({}) is None
    assert _letter_profile({}) is None
    assert _letter_static_text({}) is None
    # пустая строка — тоже None
    assert _scoring_profile({"scoring": {"profile": "  "}}) is None


class _FakeAI:
    def complete(self, message: str, **kwargs: Any) -> str:
        return "{}"


def test_scorer_profile_overrides_system_prompt() -> None:
    """Чужой профиль попадает в системный промпт, дефолт — не теряется."""
    custom = "- Middle QA, ручное тестирование, Москва"
    scorer = VacancyScorer(_FakeAI(), profile=custom)  # type: ignore[arg-type]
    assert custom in scorer.system_prompt
    assert "Senior/Lead AI-инженер" not in scorer.system_prompt

    default = VacancyScorer(_FakeAI())  # type: ignore[arg-type]
    assert "Senior/Lead" in default.system_prompt
    assert '{"score": <int 0-100>' in default.system_prompt  # контракт цел


def test_letter_system_prompt_profile() -> None:
    custom = "- Middle QA из Нижнего"
    assert custom in letter_system_prompt(custom)
    assert "Ромашка" in letter_system_prompt(None)


def test_guard_numbers_follow_config_profile() -> None:
    """Числа гварда берутся из [letter].profile: сменил факты — сменились
    разрешённые числа (999 не из дефолтного профиля)."""
    cfg = _cfg()
    assert extract_numbers(_letter_profile(cfg)) == extract_numbers(
        PROFILE_CAPSULE
    )
    custom = "- 15 лет опыта, 3 команды"
    assert extract_numbers(custom) == {"15", "3"}
