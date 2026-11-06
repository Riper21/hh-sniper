"""Тесты конфига outreach: дефолты, мердж секций, токен (ПЛАН §10)."""
from __future__ import annotations

from pathlib import Path

from snpr.outreach.config import data_dir, github_token, outreach_config


def test_defaults_when_absent():
    cfg = outreach_config({})
    assert cfg["enabled"] is False
    assert cfg["data_dir"] == "data/outreach"
    assert cfg["http"]["ssl_mode"] == "default"
    assert cfg["http"]["insecure_tls"] is False
    assert cfg["trigger"]["enabled"] is False
    assert cfg["ui"]["host"] == "127.0.0.1"
    assert cfg["github"]["token_env"] == "GITHUB_TOKEN"


def test_merge_overrides():
    raw = {
        "outreach": {
            "enabled": True,
            "data_dir": "X:/somewhere",
            "http": {"ssl_mode": "windows_store"},
            "habr": {"pages_default": 40},
            "ui": {"port": 9000},
        }
    }
    cfg = outreach_config(raw)
    assert cfg["enabled"] is True
    assert cfg["data_dir"] == "X:/somewhere"
    assert cfg["http"]["ssl_mode"] == "windows_store"
    assert cfg["http"]["cache_ttl_hours"] == 24  # дефолт сохранился
    assert cfg["habr"]["pages_default"] == 40
    assert cfg["habr"]["profile_delay"] == 0.7
    assert cfg["ui"]["port"] == 9000


def test_data_dir_relative_and_absolute(tmp_path: Path):
    assert data_dir({}, tmp_path) == tmp_path / "data" / "outreach"
    cfg = outreach_config({"outreach": {"data_dir": "out/data"}})
    assert data_dir(cfg, tmp_path) == tmp_path / "out" / "data"
    # абсолютный путь — платформонезависимо: tmp_path сам абсолютен
    cfg_abs = outreach_config({"outreach": {"data_dir": str(tmp_path / "abs")}})
    assert data_dir(cfg_abs, tmp_path) == tmp_path / "abs"


def test_github_token_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "pat-123")
    assert github_token(outreach_config({})) == "pat-123"
    cfg = outreach_config({"outreach": {"github": {"token_env": "MY_GH"}}})
    monkeypatch.setenv("MY_GH", "other")
    assert github_token(cfg) == "other"
    monkeypatch.delenv("MY_GH", raising=False)
    assert github_token(cfg) == ""
