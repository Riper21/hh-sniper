"""Тесты блэклиста работодателей: файл, гейт пре-фильтра, CLI add/list."""

from __future__ import annotations

import argparse
from pathlib import Path

from snpr.cli import run_blacklist
from snpr.filters import (
    DEFAULT_BLACKLIST_PATH,
    PipelineFilter,
    _norm_name,
    load_blacklist,
    resolve_blacklist_path,
)


def _cfg(blacklist_file: str | None = None, enabled: bool = True) -> dict:
    cfg: dict = {
        "floor_net_rub": 100000,
        "grade": {"enabled": False},
        "vendor": {"enabled": False},
        "gov": {"enabled": False},
        "geo": {"enabled": False},
    }
    if blacklist_file is not None:
        cfg["blacklist"] = {"file": blacklist_file, "enabled": enabled}
    return cfg


def _vac(employer_id: str = "", employer_name: str = "") -> dict:
    return {
        "id": "V1",
        "name": "Senior AI инженер",
        "employer": {"id": employer_id, "name": employer_name},
        "salary": {},
    }


def test_norm_name_collapses_and_lowercases() -> None:
    assert _norm_name("  ООО   Ромашка ") == "ооо ромашка"
    assert _norm_name('«Ромашка»') == "ромашка"


def test_load_blacklist_parse(tmp_path: Path) -> None:
    path = tmp_path / "blacklist.txt"
    path.write_text(
        "# комментарий\n"
        "123\n"
        "ООО Ромашка\n"
        "456 Ромашка Плюс\n"
        "\n"
        "   # отступ\n",
        encoding="utf-8",
    )
    ids, names, note = load_blacklist(path)
    assert ids == {"123", "456"}
    assert names == {"ооо ромашка", "ромашка плюс"}
    assert note == ""


def test_load_blacklist_missing_file(tmp_path: Path) -> None:
    ids, names, note = load_blacklist(tmp_path / "nope.txt")
    assert ids == set() and names == set()
    assert note == "файла нет"


def test_load_blacklist_none_path() -> None:
    ids, names, note = load_blacklist(None)
    assert ids == set() and note == "файл не указан"


def test_load_blacklist_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.txt"
    path.write_bytes("﻿789".encode("utf-8"))
    ids, names, _ = load_blacklist(path)
    # BOM приклеивается к первому токену, isdigit ломается → строка уходит
    # в имена: не матч по id, но и не падение
    assert ids == set()
    assert any("789" in name for name in names)


def test_resolve_blacklist_path() -> None:
    assert resolve_blacklist_path(None) == DEFAULT_BLACKLIST_PATH
    assert resolve_blacklist_path("") == DEFAULT_BLACKLIST_PATH
    abs_path = Path("C:/some/abs/blacklist.txt")
    assert resolve_blacklist_path(str(abs_path)) == abs_path
    rel = resolve_blacklist_path("data/blacklist.txt")
    assert rel == DEFAULT_BLACKLIST_PATH
    assert not rel.is_absolute() or rel.exists() is False


def test_gate_rejects_by_employer_id(tmp_path: Path) -> None:
    path = tmp_path / "blacklist.txt"
    path.write_text("123\n", encoding="utf-8")
    verdict = PipelineFilter(_cfg(str(path))).check(_vac("123", "ООО Ромашка"))
    assert not verdict.passed
    reasons = verdict.reject_reasons
    assert any(r.startswith("blacklist:") for r in reasons)
    assert "id 123" in reasons[0]


def test_gate_rejects_by_employer_name(tmp_path: Path) -> None:
    path = tmp_path / "blacklist.txt"
    path.write_text("ООО Ромашка\n", encoding="utf-8")
    verdict = PipelineFilter(_cfg(str(path))).check(_vac("999", "  ооо РОМАШКА  "))
    assert not verdict.passed
    assert "blacklist:" in verdict.reject_reasons[0]


def test_gate_rejects_by_name_from_id_line(tmp_path: Path) -> None:
    path = tmp_path / "blacklist.txt"
    path.write_text("123 Ромашка Плюс\n", encoding="utf-8")
    verdict = PipelineFilter(_cfg(str(path))).check(_vac("777", "Ромашка плюс"))
    assert not verdict.passed


def test_gate_passes_unrelated(tmp_path: Path) -> None:
    path = tmp_path / "blacklist.txt"
    path.write_text("123\nООО Ромашка\n", encoding="utf-8")
    verdict = PipelineFilter(_cfg(str(path))).check(_vac("999", "ООо Лютик"))
    assert verdict.passed, verdict.reject_reasons


def test_gate_no_file_is_noop(tmp_path: Path) -> None:
    verdict = PipelineFilter(_cfg(str(tmp_path / "nope.txt"))).check(
        _vac("123", "ООО Ромашка")
    )
    assert verdict.passed
    assert all(v.rule != "blacklist" for v in verdict.rules)


def test_gate_no_section_is_noop() -> None:
    verdict = PipelineFilter(_cfg()).check(_vac("123", "ООО Ромашка"))
    assert verdict.passed
    assert all(v.rule != "blacklist" for v in verdict.rules)


def test_gate_disabled_section_ignored(tmp_path: Path) -> None:
    path = tmp_path / "blacklist.txt"
    path.write_text("123\n", encoding="utf-8")
    verdict = PipelineFilter(_cfg(str(path), enabled=False)).check(
        _vac("123", "ООО Ромашка")
    )
    assert verdict.passed
    assert all(v.rule != "blacklist" for v in verdict.rules)


def test_gate_empty_file_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "blacklist.txt"
    path.write_text("# только комментарии\n", encoding="utf-8")
    verdict = PipelineFilter(_cfg(str(path))).check(_vac("123", "ООО Ромашка"))
    assert verdict.passed


# ---- CLI: snpr blacklist list/add ----


class FakeVacApi:
    def __init__(self, vacancy: dict):
        self.vacancy = vacancy
        self.get_calls: list[str] = []

    def get(self, endpoint, params=None):
        self.get_calls.append(endpoint)
        return self.vacancy


class FakeTool:
    def __init__(self, vacancy: dict):
        self.api_client = FakeVacApi(vacancy)


def _write_config(tmp_path: Path, blacklist_path: Path) -> str:
    config_path = tmp_path / "config.toml"
    quoted = str(blacklist_path).replace("\\", "/")
    config_path.write_text(
        f"[blacklist]\nfile = '{quoted}'\n", encoding="utf-8"
    )
    return str(config_path)


def _run_blacklist_args(action: str, config: str, vacancy_id: str = "") -> argparse.Namespace:
    return argparse.Namespace(action=action, vacancy_id=vacancy_id, config=config)


def test_blacklist_cli_list_empty(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path, tmp_path / "empty.txt")
    assert run_blacklist(_run_blacklist_args("list", config)) == 0
    assert "Блэклист пуст" in capsys.readouterr().out


def test_blacklist_cli_list_shows_file(tmp_path: Path, capsys) -> None:
    path = tmp_path / "blacklist.txt"
    path.write_text("123 ООО Ромашка\n", encoding="utf-8")
    config = _write_config(tmp_path, path)
    assert run_blacklist(_run_blacklist_args("list", config)) == 0
    out = capsys.readouterr().out
    assert "1 id" in out and "ООО Ромашка" in out


def test_blacklist_cli_add_from_id(tmp_path: Path, capsys, monkeypatch) -> None:
    path = tmp_path / "blacklist.txt"
    config = _write_config(tmp_path, path)
    vacancy = {
        "id": "555",
        "employer": {"id": "123", "name": "ООО Ромашка"},
    }
    monkeypatch.setattr(
        "snpr.hh_tool.SavingHHApplicantTool",
        lambda: FakeTool(vacancy),
    )
    assert run_blacklist(_run_blacklist_args("add", config, "555")) == 0
    assert path.read_text(encoding="utf-8") == "123 ООО Ромашка\n"
    out = capsys.readouterr().out
    assert "Добавлено: 123 ООО Ромашка" in out


def test_blacklist_cli_add_from_url(tmp_path: Path, capsys, monkeypatch) -> None:
    path = tmp_path / "blacklist.txt"
    config = _write_config(tmp_path, path)
    vacancy = {"id": "555", "employer": {"id": "77", "name": "Лютик"}}
    monkeypatch.setattr(
        "snpr.hh_tool.SavingHHApplicantTool",
        lambda: FakeTool(vacancy),
    )
    assert run_blacklist(
        _run_blacklist_args("add", config, "https://hh.ru/vacancy/555")
    ) == 0
    assert path.read_text(encoding="utf-8") == "77 Лютик\n"


def test_blacklist_cli_add_dedup(tmp_path: Path, capsys, monkeypatch) -> None:
    path = tmp_path / "blacklist.txt"
    path.write_text("123 ООО Ромашка\n", encoding="utf-8")
    config = _write_config(tmp_path, path)
    vacancy = {"id": "555", "employer": {"id": "123", "name": "ООО Ромашка"}}
    monkeypatch.setattr(
        "snpr.hh_tool.SavingHHApplicantTool",
        lambda: FakeTool(vacancy),
    )
    assert run_blacklist(_run_blacklist_args("add", config, "555")) == 0
    assert path.read_text(encoding="utf-8").count("123") == 1
    assert "Уже в блэклисте" in capsys.readouterr().out


def test_blacklist_cli_add_without_vacancy_id(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path, tmp_path / "blacklist.txt")
    assert run_blacklist(_run_blacklist_args("add", config)) == 2
    assert "требует ID" in capsys.readouterr().err


def test_blacklist_cli_add_bad_vacancy_id(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path, tmp_path / "blacklist.txt")
    assert run_blacklist(_run_blacklist_args("add", config, "abc")) == 2
