"""Тесты поднятия резюме: окно, интервал, POST /publish, хук снпрера."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from snpr.resume_raise import (
    MSK,
    maybe_auto_raise,
    raise_resume,
    run_raise,
    should_raise,
    write_marker,
)


class FakeResumesApi:
    def __init__(
        self,
        resumes: list[dict] | None = None,
        post_result: object = None,
        raise_on_get: Exception | None = None,
    ):
        self.resumes = resumes if resumes is not None else []
        self.post_result = post_result if post_result is not None else {}
        self.raise_on_get = raise_on_get
        self.post_calls: list[str] = []
        self.get_calls: list[str] = []

    def get(self, endpoint, params=None):
        self.get_calls.append(endpoint)
        if self.raise_on_get:
            raise self.raise_on_get
        if endpoint == "/resumes/mine":
            return {"items": self.resumes}
        raise AssertionError(f"unexpected GET {endpoint}")

    def post(self, endpoint, params=None):
        self.post_calls.append(endpoint)
        return self.post_result


def _resume(rid: str, can: bool = True) -> dict:
    return {"id": rid, "title": f"Резюме {rid}", "can_publish_or_update": can}


def _at(hour: int, day: int = 18) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=MSK)


def test_should_raise_window() -> None:
    marker = Path("C:/nonexistent-raise-marker.txt")
    assert should_raise(marker, 4, 9, 19, now=_at(8))[0] is False
    assert should_raise(marker, 4, 9, 19, now=_at(19))[0] is False
    assert should_raise(marker, 4, 9, 19, now=_at(9))[0] is True
    assert should_raise(marker, 4, 9, 19, now=_at(18))[0] is True


def test_should_raise_interval(tmp_path: Path) -> None:
    marker = tmp_path / "last_raise.txt"
    now = _at(12)
    write_marker(marker, now=now)
    ok, why = should_raise(marker, 4, 9, 19, now=now + timedelta(hours=1))
    assert ok is False and "1.0 ч" in why
    ok, why = should_raise(marker, 4, 9, 19, now=now + timedelta(hours=5))
    assert ok is True


def test_should_raise_garbage_marker(tmp_path: Path) -> None:
    marker = tmp_path / "last_raise.txt"
    marker.write_text("мусор", encoding="utf-8")
    assert should_raise(marker, 4, 9, 19, now=_at(12))[0] is True


def test_raise_resume_no_resumes() -> None:
    api = FakeResumesApi(resumes=[])
    ok, message = raise_resume(api)
    assert ok is False and "нет резюме" in message


def test_raise_resume_not_allowed() -> None:
    api = FakeResumesApi(resumes=[_resume("R1", can=False)])
    ok, _ = raise_resume(api)
    assert ok is False
    assert api.post_calls == []


def test_raise_resume_success() -> None:
    api = FakeResumesApi(resumes=[_resume("R1")])
    ok, message = raise_resume(api)
    assert ok is True and "Резюме R1" in message
    assert api.post_calls == ["/resumes/R1/publish"]


def test_raise_resume_by_id() -> None:
    api = FakeResumesApi(resumes=[_resume("R1"), _resume("R2")])
    ok, message = raise_resume(api, resume_id="R2")
    assert ok is True
    assert api.post_calls == ["/resumes/R2/publish"]


def test_raise_resume_unknown_id() -> None:
    api = FakeResumesApi(resumes=[_resume("R1")])
    ok, message = raise_resume(api, resume_id="NOPE")
    assert ok is False and "не найдено" in message


def test_raise_resume_unexpected_answer() -> None:
    api = FakeResumesApi(resumes=[_resume("R1")], post_result={"status": "x"})
    ok, message = raise_resume(api)
    assert ok is False and "неожиданный ответ" in message


def test_maybe_auto_raise_disabled() -> None:
    api = FakeResumesApi()
    assert maybe_auto_raise({"raise": {"enabled": False}}, api, Path("x"), False) is False
    assert api.get_calls == []


def test_maybe_auto_raise_dry_run(tmp_path: Path) -> None:
    api = FakeResumesApi(resumes=[_resume("R1")])
    cfg = {"raise": {"enabled": True}}
    assert maybe_auto_raise(cfg, api, tmp_path, True, now=_at(12)) is False
    assert api.get_calls == []
    assert not (tmp_path / "last_raise.txt").exists()


def test_maybe_auto_raise_success(tmp_path: Path, capsys) -> None:
    api = FakeResumesApi(resumes=[_resume("R1")])
    cfg = {"raise": {"enabled": True, "interval_hours": 4}}
    assert maybe_auto_raise(cfg, api, tmp_path, False, now=_at(12)) is True
    assert (tmp_path / "last_raise.txt").exists()
    assert "[raise] поднято" in capsys.readouterr().out


def test_maybe_auto_raise_marker_blocks_repeat(tmp_path: Path) -> None:
    api = FakeResumesApi(resumes=[_resume("R1")])
    cfg = {"raise": {"enabled": True, "interval_hours": 4}}
    maybe_auto_raise(cfg, api, tmp_path, False, now=_at(12))
    assert maybe_auto_raise(cfg, api, tmp_path, False, now=_at(13)) is False
    assert len(api.post_calls) == 1


def test_maybe_auto_raise_api_error_no_marker(tmp_path: Path, capsys) -> None:
    api = FakeResumesApi(raise_on_get=RuntimeError("сеть упала"))
    cfg = {"raise": {"enabled": True}}
    assert maybe_auto_raise(cfg, api, tmp_path, False, now=_at(12)) is False
    assert not (tmp_path / "last_raise.txt").exists()
    assert "прогон продолжается" in capsys.readouterr().out


def test_maybe_auto_raise_not_allowed_writes_marker(tmp_path: Path, capsys) -> None:
    api = FakeResumesApi(resumes=[_resume("R1", can=False)])
    cfg = {"raise": {"enabled": True, "interval_hours": 4}}
    assert maybe_auto_raise(cfg, api, tmp_path, False, now=_at(12)) is False
    assert (tmp_path / "last_raise.txt").exists()
    assert "hh не разрешает" in capsys.readouterr().out


def test_maybe_auto_raise_out_of_window(tmp_path: Path) -> None:
    api = FakeResumesApi(resumes=[_resume("R1")])
    now = datetime(2026, 10, 18, 3, 0, tzinfo=MSK)
    cfg = {"raise": {"enabled": True}}
    assert maybe_auto_raise(cfg, api, tmp_path, False, now=now) is False
    assert api.get_calls == []


def test_run_raise_force() -> None:
    api = FakeResumesApi(resumes=[_resume("R1")])
    args = argparse.Namespace(
        force=True, resume_id="", marker="x", interval_hours=4.0,
        from_hour=9, to_hour=19,
    )
    assert run_raise(args, api) == 0
    assert api.post_calls == ["/resumes/R1/publish"]


def test_run_raise_respects_interval(tmp_path: Path, capsys) -> None:
    marker = tmp_path / "last_raise.txt"
    write_marker(marker, now=datetime.now(MSK))
    args = argparse.Namespace(
        force=False, resume_id="", marker=str(marker), interval_hours=4.0,
        from_hour=9, to_hour=19,
    )
    api = FakeResumesApi(resumes=[_resume("R1")])
    assert run_raise(args, api) == 0
    assert api.post_calls == []
    assert "пропущено" in capsys.readouterr().out
