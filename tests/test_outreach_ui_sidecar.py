"""Тесты sidecar UI: живость/перезапуск/pid-файлы, без реального spawn."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from snpr.outreach.pipeline import Paths
from snpr.outreach.ui_sidecar import (
    _ping,
    _pid_alive,
    ensure_ui_running,
)


def test_ensure_ui_disabled_returns_false(tmp_path: Path):
    # выключено в конфиге — нет sidecar, нет ошибок
    assert ensure_ui_running({}) is False
    cfg = {"outreach": {"enabled": True, "ui": {"enabled": False}}}
    assert ensure_ui_running(cfg) is False


def test_ensure_ui_no_spawn_when_start_false(tmp_path: Path, monkeypatch):
    cfg = {
        "outreach": {
            "enabled": True,
            "data_dir": str(tmp_path / "out").replace("\\", "/"),
            "ui": {"enabled": True, "host": "127.0.0.1", "port": 59999},
        }
    }
    spawned: list = []

    def fake_spawn(paths, host, port):
        spawned.append((host, port))
        return None

    monkeypatch.setattr("snpr.outreach.ui_sidecar._spawn_sidecar", fake_spawn)
    assert ensure_ui_running(cfg, start=False) is False
    assert spawned == []  # start=False — не спавним


def test_ensure_ui_spawns_when_dead(tmp_path: Path, monkeypatch):
    cfg = {
        "outreach": {
            "enabled": True,
            "data_dir": str(tmp_path / "out").replace("\\", "/"),
            "ui": {"enabled": True, "host": "127.0.0.1", "port": 59998},
        }
    }
    paths = Paths(tmp_path / "out")
    # мёртвый pid в файле
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.root / "ui.pid").write_text("999999999", encoding="utf-8")

    calls: list = []

    def fake_spawn(paths, host, port):
        calls.append((host, port))
        return 4242

    monkeypatch.setattr("snpr.outreach.ui_sidecar._spawn_sidecar", fake_spawn)
    # _ping на незанятый порт → False, spawn не блокирует: прогрев без пинга
    monkeypatch.setattr("snpr.outreach.ui_sidecar._ping", lambda h, p, t=2: True)
    assert ensure_ui_running(cfg) is True
    assert calls == [("127.0.0.1", 59998)]
    assert (paths.root / "ui.pid").read_text(encoding="utf-8") == "4242"


def test_ensure_ui_alive_no_respawn(tmp_path: Path, monkeypatch):
    cfg = {
        "outreach": {
            "enabled": True,
            "data_dir": str(tmp_path / "out").replace("\\", "/"),
            "ui": {"enabled": True, "host": "127.0.0.1", "port": 59997},
        }
    }
    paths = Paths(tmp_path / "out")
    paths.root.mkdir(parents=True, exist_ok=True)
    # живой процесс: текущий интерпретатор
    (paths.root / "ui.pid").write_text(str(os.getpid()), encoding="utf-8")

    spawned: list = []

    def fake_spawn(*args):
        spawned.append(args)
        return 1

    monkeypatch.setattr("snpr.outreach.ui_sidecar._spawn_sidecar", fake_spawn)
    monkeypatch.setattr("snpr.outreach.ui_sidecar._ping", lambda h, p, t=2: True)
    assert ensure_ui_running(cfg) is True
    assert spawned == []  # жив — не плодим


def test_ensure_ui_stale_pid_cleaned(tmp_path: Path, monkeypatch):
    cfg = {
        "outreach": {
            "enabled": True,
            "data_dir": str(tmp_path / "out").replace("\\", "/"),
            "ui": {"enabled": True, "host": "127.0.0.1", "port": 59996},
        }
    }
    paths = Paths(tmp_path / "out")
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.root / "ui.pid").write_text("999999999", encoding="utf-8")

    monkeypatch.setattr("snpr.outreach.ui_sidecar._spawn_sidecar", lambda *a: 111)
    monkeypatch.setattr("snpr.outreach.ui_sidecar._ping", lambda h, p, t=2: True)
    assert ensure_ui_running(cfg) is True
    assert (paths.root / "ui.pid").read_text(encoding="utf-8") == "111"


def test_pid_alive_current_process():
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_dead():
    assert _pid_alive(999999999) is False


def test_ping_dead_port():
    # порт без слушателя — False, без исключений
    assert _ping("127.0.0.1", 59995) is False
