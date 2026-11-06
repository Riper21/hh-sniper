"""Тесты CLI outreach (парсер, статус, migrate), report, base (§10)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from snpr.outreach.base import build_contacts, run_build, stats
from snpr.outreach.cli import build_parser, run_cli
from snpr.outreach.migrate import (
    HABR_ALIAS_TO_GROUP,
    run_migrate,
    verify_counts,
)
from snpr.outreach.pipeline import Paths
from snpr.outreach.report import (
    render_cards,
    render_channel_report,
    render_full_base,
    render_github_report,
    render_habr_report,
)
from snpr.outreach.store import JsonlStore


@pytest.fixture()
def research_tree(tmp_path: Path) -> Path:
    """Мини-копия research/outreach_mvp с данными."""
    src = tmp_path / "research"
    src.mkdir()
    files = {
        "orgs.jsonl": [
            {
                "name": "Авито",
                "status": "ok",
                "github_org": "avito",
                "group": "Ecommerce",
                "priority": "A",
            }
        ],
        "people.jsonl": [
            {
                "person_key": "avito:i:i@avito.ru",
                "github_login": "i",
                "email": "i@avito.ru",
                "status": "ok",
                "email_confidence": "high",
                "group": "Ecommerce",
            }
        ],
        "habr_crawl_pilot.jsonl": [{"login": "ml_guy", "telegram": "mlguy"}],
        "habr_matches.jsonl": [{"github_login": "i", "habr_login": "i"}],
        "bridge_habr.jsonl": [{"email": "i@avito.ru", "telegram": None}],
        "career_managers.jsonl": [
            {
                "vacancy_id": "1",
                "manager_email": "hr@alfabank.ru",
                "manager_name": "HR",
                "company": "Альфа-Банк",
                "vacancy_url": "https://job.alfabank.ru/vacancies/1",
            }
        ],
        "jobtoo_career_sites.jsonl": [
            {"company": "Альфа-Банк", "career_domain": "job.alfabank.ru"}
        ],
        "hh_managers.jsonl": [
            {"vacancy_id": "9", "company": "Сбер", "manager_name": "Ольга"}
        ],
    }
    for name, records in files.items():
        (src / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
    (src / "companies.tsv").write_text(
        "name\tgroup\tpriority\tconferences\tgithub_hint\n"
        "Авито\tEcommerce\tA\taij\tavito\n",
        encoding="utf-8",
    )
    cache = src / "cache"
    cache.mkdir()
    (cache / "abc.json").write_text('{"data": 1, "ts": "2026-11-01T00:00:00"}', encoding="utf-8")
    return src


# ------------------------------------------------------------------ migrate

def test_migrate_copies_and_aliases(tmp_path: Path, research_tree: Path):
    from snpr.outreach.migrate import HH_EMPLOYER_HINTS

    paths = Paths(tmp_path / "out")
    report = run_migrate(research_tree, paths, verify=False)
    assert report["copied"]["orgs.jsonl"]["stored"] == 1
    assert paths.orgs.load_map()["Авито"]["github_org"] == "avito"
    assert report["aliases"]["employer"] == len(HH_EMPLOYER_HINTS)
    assert report["aliases"]["habr"] == len(HABR_ALIAS_TO_GROUP)
    assert paths.employer_aliases.exists()
    assert paths.habr_aliases.exists()
    assert report["cache_files"] == 1
    assert (paths.cache / "abc.json").exists()
    assert paths.companies_tsv.exists()


def test_migrate_idempotent(tmp_path: Path, research_tree: Path):
    paths = Paths(tmp_path / "out")
    run_migrate(research_tree, paths, verify=False)
    report2 = run_migrate(research_tree, paths, verify=False)
    # повторный прогон: ничего нового (кэш уже скопирован, сторы — upsert 0)
    assert report2["cache_files"] == 0
    assert report2["copied"]["orgs.jsonl"]["written"] == 0


def test_migrate_missing_source(tmp_path: Path):
    paths = Paths(tmp_path / "out")
    report = run_migrate(tmp_path / "nope", paths, verify=False)
    assert "error" in report


def test_verify_counts(tmp_path: Path, research_tree: Path):
    paths = Paths(tmp_path / "out")
    run_migrate(research_tree, paths, verify=False)
    result = verify_counts(paths)
    assert result["matches"] is False  # мини-фикстура ≠ эталон аудита
    assert result["diffs"]["orgs.jsonl"]["expected"] == 36


# --------------------------------------------------------------------- base

def test_build_contacts_all_channels(tmp_path: Path, research_tree: Path):
    paths = Paths(tmp_path / "out")
    run_migrate(research_tree, paths, verify=False)
    contacts = build_contacts(paths.people, paths.career, paths.hh, paths.habr_crawl)
    by_key = {c["contact_key"]: c for c in contacts}
    assert "email:i@avito.ru" in by_key  # ml_team из GitHub
    assert by_key["email:i@avito.ru"]["role"] == "ml_team"
    assert "email:hr@alfabank.ru" in by_key  # hiring_manager из career
    assert by_key["email:hr@alfabank.ru"]["role"] == "hiring_manager"
    assert "telegram:@mlguy" in by_key  # expert из Habr
    assert by_key["telegram:@mlguy"]["role"] == "expert"


def test_run_build_and_stats(tmp_path: Path, research_tree: Path):
    paths = Paths(tmp_path / "out")
    run_migrate(research_tree, paths, verify=False)
    written = run_build(
        paths.people, paths.career, paths.hh, paths.habr_crawl, paths.base
    )
    assert written >= 3
    summary = stats(paths.base)
    assert summary["total"] == len(paths.base.load())
    assert summary["by_channel"].get("email", 0) >= 2
    assert summary["by_channel"].get("telegram") == 1
    assert summary["by_role"]["hiring_manager"] >= 1
    assert summary["by_role"]["ml_team"] == 1
    assert summary["by_role"]["expert"] == 1


def test_run_build_dry(tmp_path: Path, research_tree: Path):
    paths = Paths(tmp_path / "out")
    run_migrate(research_tree, paths, verify=False)
    assert (
        run_build(
            paths.people, paths.career, paths.hh, paths.habr_crawl, paths.base, dry=True
        )
        == 0
    )
    assert paths.base.load() == []


# ------------------------------------------------------------------- report

def test_render_cards_and_report(tmp_path: Path, research_tree: Path):
    paths = Paths(tmp_path / "out")
    run_migrate(research_tree, paths, verify=False)
    cards = render_cards(paths.orgs, paths.people, paths.reports / "cards.md")
    text = cards.read_text(encoding="utf-8")
    assert "Авито" in text
    assert "i@avito.ru" in text
    assert "github.com" in text  # ссылка на источник (прозрачность)
    report = render_github_report(paths.orgs, paths.people, paths.reports / "report.md")
    rtext = report.read_text(encoding="utf-8")
    assert "Q1" in rtext and "Q2" in rtext


def test_render_channel_report(tmp_path: Path, research_tree: Path):
    paths = Paths(tmp_path / "out")
    run_migrate(research_tree, paths, verify=False)
    out = render_channel_report(
        paths.hh, paths.reports / "hh.md", "HH-канал", "тест"
    )
    text = out.read_text(encoding="utf-8")
    assert "Сбер" in text
    assert "Ольга" in text


def test_render_full_base(tmp_path: Path, research_tree: Path):
    paths = Paths(tmp_path / "out")
    run_migrate(research_tree, paths, verify=False)
    contacts = build_contacts(paths.people, paths.career, paths.hh, paths.habr_crawl)
    out = render_full_base(contacts, paths.reports / "base.md")
    text = out.read_text(encoding="utf-8")
    assert "Найм-менеджеры" in text
    assert "ML-команда" in text
    assert "Эксперты" in text


def test_render_habr_report(tmp_path: Path, research_tree: Path):
    paths = Paths(tmp_path / "out")
    run_migrate(research_tree, paths, verify=False)
    out = render_habr_report(
        paths.habr_crawl, paths.reports / "habr.md", aliases={"avito": "Авито"}
    )
    text = out.read_text(encoding="utf-8")
    assert "ml_guy" in text


# ---------------------------------------------------------------------- CLI

def test_cli_parser_registers_steps():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    built = build_parser(sub)
    assert built is not None
    args = parser.parse_args(["outreach", "status"])
    assert args.outreach_step == "status"


def test_cli_status(tmp_path: Path, research_tree: Path, monkeypatch, capsys):
    paths = Paths(tmp_path / "out")
    run_migrate(research_tree, paths, verify=False)
    run_build(paths.people, paths.career, paths.hh, paths.habr_crawl, paths.base)

    config = tmp_path / "config.toml"
    config.write_text(
        f'[outreach]\ndata_dir = "{str(paths.root).replace(chr(92), "/")}"\n',
        encoding="utf-8",
    )
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_parser(sub)
    args = parser.parse_args(["outreach", "status"])
    args.config = str(config)
    rc = run_cli(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "outreach" in out
    assert "orgs" in out
    assert "БАЗА" in out


def test_cli_seed(tmp_path: Path, monkeypatch, capsys):
    import argparse

    # data_dir через собственный config: outreach.data_dir = абсолютный tmp
    config = tmp_path / "config.toml"
    config.write_text(
        f'[outreach]\ndata_dir = "{str(tmp_path).replace(chr(92), "/")}/data/outreach"\n',
        encoding="utf-8",
    )
    (tmp_path / "data" / "outreach").mkdir(parents=True, exist_ok=True)
    tsv = tmp_path / "data" / "outreach" / "companies.tsv"
    tsv.write_text(
        "name\tgroup\tpriority\tconferences\tgithub_hint\nАвито\tE\tA\taij\tavito\n",
        encoding="utf-8",
    )
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_parser(sub)
    args = parser.parse_args(["outreach", "seed"])
    args.config = str(config)
    rc = run_cli(args)
    assert rc == 0
    store = JsonlStore(tmp_path / "data" / "outreach" / "orgs.jsonl", "name")
    assert "Авито" in store.keys()
