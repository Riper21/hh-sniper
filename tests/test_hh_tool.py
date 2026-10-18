"""Тесты обёртки snpr.hh_tool (баг базы: токен теряется при успехе)."""

import pytest

from snpr import hh_tool


class RecordingTool:
    """Фейковый тул: фиксирует вызовы методов сохранения."""

    def __init__(self, *, token_result=False, token_error=False):
        self.token_result = token_result
        self.token_error = token_error
        self.calls = []

    def save_token(self):
        self.calls.append("token")
        if self.token_error:
            raise RuntimeError("token boom")
        return self.token_result

    def save_cookies(self):
        self.calls.append("cookies")

    def _save_config_if_needed(self):
        self.calls.append("config")


def test_persist_calls_all_savers():
    tool = RecordingTool(token_result=True)
    hh_tool.SavingHHApplicantTool._persist(tool)
    assert tool.calls == ["token", "cookies", "config"]


def test_persist_survives_token_failure():
    tool = RecordingTool(token_error=True)
    hh_tool.SavingHHApplicantTool._persist(tool)
    assert tool.calls == ["token", "cookies", "config"]


def test_run_persists_after_early_success(monkeypatch):
    # Суть бага базы: run() возвращает успех ДО блока сохранения —
    # обёртка обязана сохранить и после раннего выхода.
    monkeypatch.setattr(
        hh_tool.HHApplicantTool, "run", lambda self, argv=None: 0
    )
    calls = []
    monkeypatch.setattr(
        hh_tool.SavingHHApplicantTool,
        "_persist",
        lambda self: calls.append("persist"),
    )
    tool = hh_tool.SavingHHApplicantTool()
    assert tool.run(["whatever"]) == 0
    assert calls == ["persist"]


def test_run_persists_on_exception(monkeypatch):
    def boom(self, argv=None):
        raise RuntimeError("operation failed")

    monkeypatch.setattr(hh_tool.HHApplicantTool, "run", boom)
    calls = []
    monkeypatch.setattr(
        hh_tool.SavingHHApplicantTool,
        "_persist",
        lambda self: calls.append("persist"),
    )
    tool = hh_tool.SavingHHApplicantTool()
    with pytest.raises(RuntimeError):
        tool.run()
    assert calls == ["persist"]


def test_build_auth_argv():
    assert hh_tool.build_auth_argv(["a@b.ru"]) == [
        "--captcha-manual",
        "authorize",
        "a@b.ru",
    ]
    assert hh_tool.build_auth_argv([]) == ["--captcha-manual", "authorize"]


def test_main_runs_authorize_via_wrapper(monkeypatch):
    captured = {}

    class FakeTool:
        def run(self, argv):
            captured["argv"] = argv
            return 3

    monkeypatch.setattr(hh_tool, "SavingHHApplicantTool", FakeTool)
    assert hh_tool.main(["x@y.ru"]) == 3
    assert captured["argv"] == ["--captcha-manual", "authorize", "x@y.ru"]
