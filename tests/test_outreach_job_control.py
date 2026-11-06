"""Тесты блока управления джобой (06.11): snapshot/toggle/маппинг."""
from __future__ import annotations

from pathlib import Path

import pytest

from snpr.outreach import job_control


def test_job_toggle_rejects_unknown(monkeypatch):
    result = job_control.job_toggle("explode")
    assert result["ok"] is False
    assert "неизвестное" in result["error"]


def test_job_toggle_calls_schtasks(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        job_control, "_schtasks", lambda *flags: (0, "") or calls.append(flags)
    )
    # исправный мок: _schtasks возвращает (0, "")
    def fake_schtasks(*flags):
        calls.append(flags)
        return (0, "")

    monkeypatch.setattr(job_control, "_schtasks", fake_schtasks)
    result = job_control.job_toggle("on")
    assert result["ok"] is True
    assert "ВКЛЮЧЕНА" in result["message"]
    assert ("/change", "/tn", "hh-snpr", "/enable") in calls

    calls.clear()
    result_off = job_control.job_toggle("off")
    assert result_off["ok"] is True
    assert "ВЫКЛЮЧЕНА" in result_off["message"]
    assert ("/change", "/tn", "hh-snpr", "/disable") in calls

    calls.clear()
    result_run = job_control.job_toggle("run")
    assert result_run["ok"] is True
    assert ("/run", "/tn", "hh-snpr") in calls


def test_job_toggle_on_registers_when_missing(monkeypatch):
    """Нет задачи → on регистрирует через register_task.ps1 (как CLI)."""
    def fake_schtasks(*flags):
        # первая попытка change — задача не найдена (код 1)
        return (1, "задача не найдена")

    registered: list[bool] = []

    def fake_register():
        registered.append(True)
        return True

    monkeypatch.setattr(job_control, "_schtasks", fake_schtasks)
    monkeypatch.setattr(job_control, "_register_via_ps1", fake_register)
    result = job_control.job_toggle("on")
    assert result["ok"] is True
    assert registered


def test_job_snapshot_shape(monkeypatch):
    fake_status = {
        "state": "Ready",
        "interval": "PT1M",
        "last": "11/06/2026 11:18",
        "next": "11/06/2026 11:19",
        "result": 0,
        "missed": 0,
    }
    monkeypatch.setattr(job_control, "_task_status", lambda: fake_status)
    monkeypatch.setattr(
        job_control, "_marker_age_minutes", lambda: 12.3
    )
    from snpr.job import LOCK_PATH

    had_lock = LOCK_PATH.exists()
    try:
        if not had_lock:
            LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOCK_PATH.write_text("0 99999", encoding="utf-8")
        snap = job_control.job_snapshot()
        assert snap["registered"] is True
        assert snap["state"] == "Ready"
        assert snap["last_success_min"] == 12.3
        assert snap["running_now"] is True
    finally:
        if not had_lock and LOCK_PATH.exists():
            LOCK_PATH.unlink()


def test_job_snapshot_unregistered(monkeypatch):
    monkeypatch.setattr(job_control, "_task_status", lambda: None)
    snap = job_control.job_snapshot()
    assert snap["registered"] is False
    assert snap["state"] == "?"


def test_open_in_browser(monkeypatch):
    opened: list[str] = []

    class FakeBrowser:
        def open(self, url):
            opened.append(url)
            return True

    import webbrowser

    monkeypatch.setattr(webbrowser, "open", FakeBrowser().open)
    result = job_control.open_in_browser()
    assert result["ok"] is True
    assert opened == ["http://127.0.0.1:8765"]
