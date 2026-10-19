"""Демон: такт-subprocess, парковка→approve, лок демона, цикл/стоп.

Всё платформенно-опасное (окна/звук) мокается; живой тик `sne --mode
park` сюда не входит — это домен смоука и обкатки.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import pytest

from snpr import daemon
from snpr.sne import _acquire_run_lock, _release_run_lock


def _args(**kw) -> argparse.Namespace:
    base = {"interval": 1, "once": False, "config": ""}
    base.update(kw)
    return argparse.Namespace(**base)


class TestTickCommand:
    def test_same_invocation_as_job_cmd(self):
        cmd = daemon._tick_command(_args(config="cfg.toml"))
        assert cmd[:5] == [sys.executable, "-X", "utf8", "-m", "snpr"]
        assert cmd[cmd.index("--config") + 1] == "cfg.toml"
        assert cmd[cmd.index("sne") + 1 :] == ["--mode", "park"]

    def test_no_config_omitted(self):
        cmd = daemon._tick_command(_args(config=""))
        assert "--config" not in cmd
        assert "sne" in cmd and "park" in cmd


class TestTick:
    def test_ok_writes_header_and_output(self, tmp_path):
        log = tmp_path / "scheduler.log"
        rc = daemon._tick([sys.executable, "-c", "print('tick-ok')"], log)
        text = log.read_text(encoding="utf-8")
        assert rc == 0
        assert "=====" in text
        assert "tick-ok" in text

    def test_propagates_rc(self, tmp_path):
        log = tmp_path / "scheduler.log"
        rc = daemon._tick(
            [sys.executable, "-c", "import sys; sys.exit(3)"], log
        )
        assert rc == 3
        assert "=====" in log.read_text(encoding="utf-8")

    def test_spawn_failure_is_127_not_crash(self, tmp_path):
        log = tmp_path / "scheduler.log"
        rc = daemon._tick(["no-such-executable-xyz"], log)
        text = log.read_text(encoding="utf-8")
        assert rc == 127
        assert "=====" in text

    def test_header_on_disk_before_child_runs(self, tmp_path):
        # потомок читает лог ДО своего выхода: шапка обязана уже лежать
        # на диске (найдено живьём 18.10: буфер родителя прятал шапку
        # до конца тика — убитый тик терял её целиком)
        log = tmp_path / "scheduler.log"
        child = (
            "import pathlib, sys; "
            "text = pathlib.Path(sys.argv[1]).read_text("
            "encoding='utf-8'); "
            "sys.exit(0 if '=====' in text else 5)"
        )
        rc = daemon._tick([sys.executable, "-c", child, str(log)], log)
        assert rc == 0


class TestParked:
    def test_missing_file(self, tmp_path):
        assert not daemon._parked_nonempty(tmp_path / "parked.jsonl")

    def test_empty_lines_only(self, tmp_path):
        p = tmp_path / "parked.jsonl"
        p.write_text("\n \n", encoding="utf-8")
        assert not daemon._parked_nonempty(p)

    def test_has_rows(self, tmp_path):
        p = tmp_path / "parked.jsonl"
        p.write_text('{"id": 1}\n', encoding="utf-8")
        assert daemon._parked_nonempty(p)


class TestMaybeApprove:
    def test_no_parked_no_call(self, tmp_path, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr(daemon, "_open_approve", lambda: calls.append(1))
        assert not daemon._maybe_open_approve(tmp_path / "parked.jsonl")
        assert calls == []

    def test_parked_opens(self, tmp_path, monkeypatch):
        p = tmp_path / "parked.jsonl"
        p.write_text('{"id": 1}\n', encoding="utf-8")
        monkeypatch.setattr(daemon, "_approve_running", lambda: False)
        calls: list[int] = []
        monkeypatch.setattr(daemon, "_open_approve", lambda: calls.append(1))
        assert daemon._maybe_open_approve(p)
        assert calls == [1]

    def test_window_already_open_no_dup(self, tmp_path, monkeypatch):
        p = tmp_path / "parked.jsonl"
        p.write_text('{"id": 1}\n', encoding="utf-8")
        monkeypatch.setattr(daemon, "_approve_running", lambda: True)
        calls: list[int] = []
        monkeypatch.setattr(daemon, "_open_approve", lambda: calls.append(1))
        assert not daemon._maybe_open_approve(p)
        assert calls == []


class TestDaemonLock:
    def test_second_acquire_refused_then_released(self, tmp_path):
        lock = tmp_path / "daemon.lock"
        first = _acquire_run_lock(lock, stale_after=1800.0)
        assert first is not None
        try:
            assert _acquire_run_lock(lock, stale_after=1800.0) is None
        finally:
            _release_run_lock(lock, first)
        again = _acquire_run_lock(lock, stale_after=1800.0)
        assert again is not None
        _release_run_lock(lock, again)
        assert not lock.exists()


class TestWait:
    def test_returns_when_stopped(self):
        states = iter([False, True])
        start = time.monotonic()
        daemon._wait(5.0, lambda: next(states))
        assert time.monotonic() - start < 1.0


class TestUnixApproveSignal:
    def test_open_approve_bells_and_hints(self, monkeypatch, capsys):
        # этап 2: сигнал «есть на approve» без окна — BEL консоли
        monkeypatch.setattr(sys, "platform", "linux")
        daemon._open_approve()
        out = capsys.readouterr().out
        assert "\a" in out
        assert "python -m snpr approve" in out

    def test_no_lock_not_running(self, monkeypatch, tmp_path):
        # 19.10: анти-дубль — pid-лок approve, не заголовок окна; без лока
        # ( approve закрыт) — не running
        monkeypatch.setattr(
            "snpr.sne._approve_lock_path", lambda *a, **k: tmp_path / "approve.lock"
        )
        assert daemon._approve_running() is False

    def test_live_lock_running(self, monkeypatch, tmp_path):
        # живой pid в локе (свой процесс) — approve работает, окно не дублируем
        lock = tmp_path / "approve.lock"
        lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
        monkeypatch.setattr(
            "snpr.sne._approve_lock_path", lambda *a, **k: lock
        )
        assert daemon._approve_running() is True


class TestRunDaemon:
    def _patch_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon, "LOG_PATH", tmp_path / "scheduler.log")
        monkeypatch.setattr(
            daemon, "DEFAULT_PARKED_STORE", str(tmp_path / "parked.jsonl")
        )
        monkeypatch.setattr(
            daemon, "DAEMON_LOCK_PATH", tmp_path / "daemon.lock"
        )

    def test_once_runs_single_tick_and_releases_lock(
        self, monkeypatch, tmp_path
    ):
        self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(
            daemon,
            "_tick_command",
            lambda args: [sys.executable, "-c", "print('daemon-once')"],
        )
        monkeypatch.setattr(daemon, "_open_approve", lambda: None)
        rc = daemon.run_daemon(_args(once=True, interval=60))
        assert rc == 0
        text = (tmp_path / "scheduler.log").read_text(encoding="utf-8")
        assert "daemon-once" in text
        assert not (tmp_path / "daemon.lock").exists()

    def test_once_returns_tick_rc(self, monkeypatch, tmp_path):
        self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(
            daemon,
            "_tick_command",
            lambda args: [sys.executable, "-c", "import sys; sys.exit(4)"],
        )
        monkeypatch.setattr(daemon, "_open_approve", lambda: None)
        assert daemon.run_daemon(_args(once=True)) == 4

    def test_loop_ticks_until_interrupt(self, monkeypatch, tmp_path):
        self._patch_paths(monkeypatch, tmp_path)
        counter = tmp_path / "count.txt"
        monkeypatch.setattr(
            daemon,
            "_tick_command",
            lambda args: [
                sys.executable,
                "-c",
                "import pathlib, sys; "
                "p = pathlib.Path(sys.argv[1]); "
                "n = (int(p.read_text()) if p.exists() else 0) + 1; "
                "p.write_text(str(n))",
                str(counter),
            ],
        )
        monkeypatch.setattr(daemon, "_open_approve", lambda: None)
        waits = {"n": 0}

        def fake_wait(delay, is_stopped):
            waits["n"] += 1
            if waits["n"] >= 3:
                raise KeyboardInterrupt

        monkeypatch.setattr(daemon, "_wait", fake_wait)
        rc = daemon.run_daemon(_args(interval=1))
        assert rc == 0
        assert counter.read_text(encoding="utf-8") == "3"
        assert not (tmp_path / "daemon.lock").exists()

    def test_second_daemon_refused(self, monkeypatch, tmp_path):
        self._patch_paths(monkeypatch, tmp_path)
        # чужой «живой» держатель: свой pid в лок-файле
        lock = tmp_path / "daemon.lock"
        lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
        monkeypatch.setattr(
            daemon,
            "_tick_command",
            lambda args: pytest.fail("тик не должен был стартовать"),
        )
        assert daemon.run_daemon(_args(once=True)) == 1
        assert lock.exists()

    def test_interval_floored(self, monkeypatch, tmp_path):
        self._patch_paths(monkeypatch, tmp_path)
        seen: dict[str, float] = {}

        def spy_wait(delay, is_stopped):
            seen["delay"] = delay
            raise KeyboardInterrupt

        monkeypatch.setattr(daemon, "_wait", spy_wait)
        monkeypatch.setattr(
            daemon,
            "_tick_command",
            lambda args: [sys.executable, "-c", "pass"],
        )
        monkeypatch.setattr(daemon, "_open_approve", lambda: None)
        daemon.run_daemon(_args(interval=1))
        assert seen["delay"] >= daemon._MIN_INTERVAL - 1.0
