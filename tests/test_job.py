"""Тесты управления джобой планировщика (sniper/job.py, 18.10)."""

from __future__ import annotations

from pathlib import Path

import sniper.job as job


def test_file_count(tmp_path: Path) -> None:
    missing = tmp_path / "no.jsonl"
    assert job._file_count(missing) == 0
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    assert job._file_count(empty) == 0
    filled = tmp_path / "filled.jsonl"
    filled.write_text('{"a":1}\n{"b":2}\n\n{"c":3}\n', encoding="utf-8")
    assert job._file_count(filled) == 3


def test_marker_age_minutes(tmp_path: Path, monkeypatch) -> None:
    import time

    monkeypatch.setattr(job, "MARKER_PATH", tmp_path / "last_run.txt")
    # нет файла — None
    assert job._marker_age_minutes() is None
    # мусор — None
    (tmp_path / "last_run.txt").write_text("мусор", encoding="utf-8")
    assert job._marker_age_minutes() is None
    # свежий маркер — ~0 мин
    (tmp_path / "last_run.txt").write_text(
        f"{time.time():.0f}", encoding="utf-8"
    )
    age = job._marker_age_minutes()
    assert age is not None and age < 1.0


def test_job_log_tail(tmp_path: Path, monkeypatch, capsys) -> None:
    log = tmp_path / "scheduler.log"
    log.write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")
    monkeypatch.setattr(job, "LOG_PATH", log)
    assert job.job_log(lines=3) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["line8", "line9", "line10"]
    # лога нет — код 1
    monkeypatch.setattr(job, "LOG_PATH", tmp_path / "missing.log")
    assert job.job_log() == 1


def test_job_status_no_task(monkeypatch, capsys) -> None:
    monkeypatch.setattr(job, "_task_status", lambda: None)
    assert job.job_status() == 1
    assert "НЕ зарегистрирована" in capsys.readouterr().out


def test_job_status_full(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(
        job,
        "_task_status",
        lambda: {
            "state": "Ready",
            "interval": "PT5M",
            "last": "18.10.2026 1:30:00",
            "next": "18.10.2026 1:35:00",
            "result": 0,
            "missed": 0,
        },
    )
    monkeypatch.setattr(job, "MARKER_PATH", tmp_path / "last_run.txt")
    monkeypatch.setattr(job, "LOCK_PATH", tmp_path / "snipe.lock")
    monkeypatch.setattr(job, "LOG_PATH", tmp_path / "scheduler.log")
    assert job.job_status() == 0
    out = capsys.readouterr().out
    assert "Ready" in out and "PT5M" in out
    assert "успешных прогонов ещё не было" in out


def test_job_on_registers_when_missing(monkeypatch, capsys) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_schtasks(*flags: str) -> tuple[int, str]:
        calls.append(flags)
        return 1, "задача не найдена"

    monkeypatch.setattr(job, "_schtasks", fake_schtasks)
    monkeypatch.setattr(job, "_register_via_ps1", lambda: True)
    monkeypatch.setattr(
        job,
        "_task_status",
        lambda: {"state": "Ready", "interval": "PT5M", "next": "скоро"},
    )
    assert job.job_on() == 0
    assert calls == [("/change", "/tn", "hh-sniper", "/enable")]
    assert "ВКЛЮЧЕНА" in capsys.readouterr().out


def test_job_on_when_registered(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_schtasks(*flags: str) -> tuple[int, str]:
        calls.append(flags)
        return 0, "УСПЕШНО"

    monkeypatch.setattr(job, "_schtasks", fake_schtasks)
    monkeypatch.setattr(job, "_register_via_ps1", lambda: False)
    monkeypatch.setattr(job, "_task_status", lambda: None)
    assert job.job_on() == 0
    assert calls == [("/change", "/tn", "hh-sniper", "/enable")]


def test_job_off_calls_disable(monkeypatch, capsys) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_schtasks(*flags: str) -> tuple[int, str]:
        calls.append(flags)
        return 0, "УСПЕШНО"

    monkeypatch.setattr(job, "_schtasks", fake_schtasks)
    assert job.job_off() == 0
    assert calls == [("/change", "/tn", "hh-sniper", "/disable")]
    assert "ВЫКЛЮЧЕНА" in capsys.readouterr().out


def test_job_run_calls_run(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_schtasks(*flags: str) -> tuple[int, str]:
        calls.append(flags)
        return 0, ""

    monkeypatch.setattr(job, "_schtasks", fake_schtasks)
    assert job.job_run() == 0
    assert calls == [("/run", "/tn", "hh-sniper")]


def test_run_job_command_dispatch(monkeypatch) -> None:
    import argparse

    hit: list[str] = []
    monkeypatch.setattr(job, "job_on", lambda: hit.append("on") or 0)
    monkeypatch.setattr(job, "job_log", lambda lines: hit.append(f"log{lines}") or 0)
    monkeypatch.setattr(job, "job_status", lambda: hit.append("status") or 0)

    args = argparse.Namespace(action="on", lines=30)
    assert job.run_job_command(args) == 0
    args = argparse.Namespace(action="log", lines=30)
    assert job.run_job_command(args) == 0
    assert hit[-1] == "log30"
    args = argparse.Namespace(action=None, lines=60)
    assert job.run_job_command(args) == 0
    assert hit[-1] == "status"
