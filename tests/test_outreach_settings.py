"""Тесты мастера настройки (06.11): whitelist, бэкап, roundtrip, черновики."""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from snpr.outreach.settings import (
    EDITABLE,
    profile_from_resume,
    read_settings,
    write_settings,
)


@pytest.fixture()
def config_copy(tmp_path: Path) -> Path:
    """Копия боевого конфига — тестируем запись без риска."""
    src = Path(__file__).parent.parent / "snpr" / "config.toml"
    dst = tmp_path / "config.toml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


RESUME = """Удюрминский Антон Витальевич
+7 (999) 5673604
riper21riper@gmail.com
Telegram: https://t.me/riper21
Желаемая должность: AI Platform Engineer / LLM Systems Engineer

Навыки
Python; FastAPI; Pytest; LangGraph; LangChain; LLM; RAG; Docker; Git
"""


def test_read_settings_snapshot(config_copy: Path):
    s = read_settings(config_copy)
    assert "scoring.profile" in s
    assert "outreach.role_keywords" in s
    assert isinstance(s["scoring.maybe_min"], int)
    assert s["_hh_authorized"] in (True, False)


def test_write_settings_applies_and_backs_up(config_copy: Path):
    result = write_settings(
        {"outreach.role_keywords": ["qa", "aqa"], "scoring.maybe_min": 40},
        path=config_copy,
    )
    assert result["ok"] is True
    assert "outreach.role_keywords" in result["applied"]
    # бэкап создан
    backups = list(config_copy.parent.glob("config.toml.bak-*"))
    assert backups
    # применено и конфиг валиден
    with open(config_copy, "rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg["outreach"]["role_keywords"] == ["qa", "aqa"]
    assert cfg["scoring"]["maybe_min"] == 40
    # инфраструктурные секции не тронуты
    assert "habr" in cfg


def test_write_settings_rejects_unknown_keys(config_copy: Path):
    result = write_settings({"habr.interval_minutes": 1}, path=config_copy)
    assert result["ok"] is False
    assert "неизвестные" in result["error"]


def test_write_settings_rejects_bad_type(config_copy: Path):
    result = write_settings({"scoring.maybe_min": "не число"}, path=config_copy)
    assert result["ok"] is False
    result2 = write_settings(
        {"outreach.role_keywords": "не список"}, path=config_copy
    )
    assert result2["ok"] is False
    # конфиг не сломан
    with open(config_copy, "rb") as fh:
        tomllib.load(fh)


def test_write_settings_multiline_profile_roundtrip(config_copy: Path):
    profile = "- строка 1\n- строка 2 с \"кавычками\"\n- числа: 862"
    result = write_settings({"scoring.profile": profile}, path=config_copy)
    assert result["ok"] is True
    with open(config_copy, "rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg["scoring"]["profile"] == profile


def test_profile_from_resume_draft():
    draft = profile_from_resume(RESUME)
    assert draft["email"] == "riper21riper@gmail.com"
    assert draft["telegram"] == "@riper21"
    assert "LLM Systems" in draft["title"] or "Engineer" in draft["title"]
    assert "Python" in draft["scoring_profile"]
    assert "факты" in draft["letter_profile"].lower()
    # стек-ключи — латиница из навыков
    assert "python" in draft["stack_keywords"]
    assert "pytest" in draft["stack_keywords"]


def test_profile_from_resume_empty():
    draft = profile_from_resume("")
    assert draft["scoring_profile"] == ""
    assert draft["stack_keywords"] == []
