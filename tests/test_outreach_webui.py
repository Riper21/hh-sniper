"""Тесты UI: HTTP API, verify-запись, localhost-only (§10)."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from snpr.outreach.pipeline import Paths
from snpr.outreach.store import JsonlStore
from snpr.outreach.webui import make_handler, serve


@pytest.fixture()
def ui_paths(tmp_path: Path) -> Paths:
    paths = Paths(tmp_path / "out")
    paths.base.upsert(
        {
            "contact_key": "email:i@avito.ru",
            "channel": "email",
            "value": "i@avito.ru",
            "company": "Авито",
            "name": "Ivan",
            "role": "ml_team",
            "source": "github_commits",
            "source_url": "https://github.com/i",
            "confidence": "high",
            "stack_matched": ["python"],
        }
    )
    paths.base.upsert(
        {
            "contact_key": "telegram:@mlguy",
            "channel": "telegram",
            "value": "@mlguy",
            "company": None,
            "name": "ML Guy",
            "role": "expert",
            "source": "habr_crawl",
            "confidence": "medium",
            "stack_matched": [],
        }
    )
    return paths


def start_server(paths: Paths, port: int = 0):
    from http.server import ThreadingHTTPServer

    handler = make_handler(paths)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    return httpd


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_index_html_served(ui_paths: Paths):
    httpd = start_server(ui_paths)
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % httpd.server_address[1], timeout=5) as resp:
            body = resp.read().decode("utf-8")
            assert "Outreach" in body
            assert resp.headers["Content-Type"].startswith("text/html")
    finally:
        httpd.shutdown()


def test_stats_endpoint(ui_paths: Paths):
    httpd = start_server(ui_paths)
    try:
        data = get_json("http://127.0.0.1:%d/api/stats" % httpd.server_address[1])
        assert data["total"] == 2
        assert data["by_channel"]["email"] == 1
        assert data["by_channel"]["telegram"] == 1
        assert data["by_role"]["ml_team"] == 1
    finally:
        httpd.shutdown()


def test_contacts_filtering(ui_paths: Paths):
    httpd = start_server(ui_paths)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        all_contacts = get_json(base + "/api/contacts")["contacts"]
        assert len(all_contacts) == 2
        only_email = get_json(base + "/api/contacts?channel=email")["contacts"]
        assert len(only_email) == 1
        assert only_email[0]["company"] == "Авито"
        only_hm = get_json(base + "/api/contacts?role=hiring_manager")["contacts"]
        assert only_hm == []
    finally:
        httpd.shutdown()


def test_contact_detail_with_events(ui_paths: Paths):
    httpd = start_server(ui_paths)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        data = get_json(base + "/api/contacts/email%3Ai%40avito.ru")
        assert data["contact"]["name"] == "Ivan"
        assert "events" in data
    finally:
        httpd.shutdown()


def test_verify_writes_store(ui_paths: Paths):
    httpd = start_server(ui_paths)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        result = post_json(
            base + "/api/contacts/email%3Ai%40avito.ru/verify",
            {"status": "verified", "note": "проверил профиль"},
        )
        assert result["ok"] is True
        contact = ui_paths.base.load_map()["email:i@avito.ru"]
        assert contact["verified"] == "verified"
        assert contact["verified_note"] == "проверил профиль"
        assert contact["verified_at"]
    finally:
        httpd.shutdown()


def test_verify_rejects_bad_status(ui_paths: Paths):
    httpd = start_server(ui_paths)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        with pytest.raises(Exception):
            post_json(
                base + "/api/contacts/email%3Ai%40avito.ru/verify",
                {"status": "bogus"},
            )
    finally:
        httpd.shutdown()


def test_campaign_event_logged(ui_paths: Paths):
    httpd = start_server(ui_paths)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        result = post_json(
            base + "/api/contacts/email%3Ai%40avito.ru/campaign",
            {"event": "sent", "note": "письмо от 06.11"},
        )
        assert result["event"]["event"] == "sent"
        events = ui_paths.events.events_for("email:i@avito.ru")
        assert events[-1]["note"] == "письмо от 06.11"
        contact = ui_paths.base.load_map()["email:i@avito.ru"]
        assert contact["campaign"]["status"] == "sent"
    finally:
        httpd.shutdown()


def test_404_unknown_contact(ui_paths: Paths):
    httpd = start_server(ui_paths)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        with pytest.raises(Exception):
            get_json(base + "/api/contacts/email:ghost@nowhere.ru")
    finally:
        httpd.shutdown()


def test_server_binds_localhost_only(ui_paths: Paths):
    httpd = start_server(ui_paths)
    try:
        host, port = httpd.server_address[:2]
        assert host == "127.0.0.1"
    finally:
        httpd.shutdown()


def test_empty_base_stats(ui_paths: Paths, tmp_path: Path):
    empty = Paths(tmp_path / "empty")
    httpd = start_server(empty)
    try:
        data = get_json("http://127.0.0.1:%d/api/stats" % httpd.server_address[1])
        assert data["total"] == 0
        contacts = get_json(
            "http://127.0.0.1:%d/api/contacts" % httpd.server_address[1]
        )["contacts"]
        assert contacts == []
    finally:
        httpd.shutdown()


# ------------------------------------------------------------- scores/ticks

@pytest.fixture()
def isolated_score_sources(tmp_path: Path, monkeypatch):
    """Мок обоих источников /api/scores: hh-журнал + career-стор."""
    import snpr.outreach.webui as webui_mod

    scores_file = tmp_path / "scores.jsonl"
    career_file = tmp_path / "outreach" / "career_managers.jsonl"
    career_file.parent.mkdir(parents=True)
    # career-стор НЕ существует по умолчанию (записи не заскорены)
    monkeypatch.setattr(webui_mod, "SCORES_JSONL", scores_file)
    # путь career-стора вычисляется от _SNIPER_DATA — мокаем константу
    monkeypatch.setattr(webui_mod, "_SNIPER_DATA", tmp_path)
    return scores_file, career_file


def test_scores_endpoint(
    ui_paths: Paths, tmp_path: Path, monkeypatch, isolated_score_sources
):
    scores_file, _career = isolated_score_sources
    rows = [
        {
            "vacancy_id": "1",
            "name": "ML Engineer",
            "employer": "Ozon",
            "score": 80,
            "verdict": "target",
            "reasons": ["подходит"],
            "ts": 1793945130,
        },
        {
            "vacancy_id": "2",
            "name": "Бухгалтер",
            "employer": "X",
            "score": 10,
            "verdict": "pass",
            "reasons": ["не ML"],
            "ts": 1793945200,
        },
    ]
    scores_file.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    import snpr.outreach.webui as webui_mod

    data = webui_mod.load_scores({})
    assert data["total"] == 2
    assert data["scores"][-1]["vacancy_url"] == "https://hh.ru/vacancy/2"

    only_pass = webui_mod.load_scores({"verdict": "pass"})
    assert only_pass["total"] == 1
    assert only_pass["scores"][0]["verdict"] == "pass"

    high = webui_mod.load_scores({"min_score": "50"})
    assert high["total"] == 1
    assert high["scores"][0]["score"] == 80

    limited = webui_mod.load_scores({"limit": "1"})
    assert limited["total"] == 2
    assert len(limited["scores"]) == 1


def test_scores_merged_with_career(
    tmp_path: Path, monkeypatch, isolated_score_sources
):
    """06.11: /api/scores объединяет hh-журнал снайпера и outreach-скоры
    карьер-вакансий (source=outreach, найм-менеджер в записи)."""
    scores_file, career_file = isolated_score_sources
    scores_file.write_text(
        json.dumps(
            {
                "vacancy_id": "7",
                "name": "hh вакансия",
                "employer": "Yandex",
                "score": 60,
                "verdict": "maybe",
                "reasons": ["ok"],
                "ts": 1793946000,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    career_file.write_text(
        json.dumps(
            {
                "vacancy_id": "39598",
                "platform": "alfa-api",
                "company": "Альфа-Банк",
                "vacancy_name": "Data Science",
                "manager_name": "Мария",
                "manager_email": "m@alfabank.ru",
                "vacancy_url": "https://job.alfabank.ru/vacancies/39598",
                "score": 45,
                "verdict": "maybe",
                "score_reasons": ["RAG"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    import snpr.outreach.webui as webui_mod

    data = webui_mod.load_scores({})
    assert data["total"] == 2
    by_src = {r.get("source"): r for r in data["scores"]}
    assert set(by_src) == {"hh", "outreach"}
    assert by_src["outreach"]["manager"] == "Мария"
    assert by_src["outreach"]["reasons"] == ["RAG"]
    # фильтр по источнику
    only_outreach = webui_mod.load_scores({"source": "outreach"})
    assert only_outreach["total"] == 1
    assert only_outreach["scores"][0]["vacancy_id"] == "39598"
    # карьер-запись БЕЗ скора не попадает
    career_file.write_text(
        json.dumps(
            {"vacancy_id": "1", "vacancy_name": "без скора", "company": "X"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    assert webui_mod.load_scores({"source": "outreach"})["total"] == 0


def test_scores_missing_file(tmp_path: Path, monkeypatch):
    import snpr.outreach.webui as webui_mod

    monkeypatch.setattr(webui_mod, "SCORES_JSONL", tmp_path / "nope.jsonl")
    monkeypatch.setattr(webui_mod, "_SNIPER_DATA", tmp_path)
    data = webui_mod.load_scores({})
    assert data == {"scores": [], "total": 0}


def test_ticks_endpoint(ui_paths: Paths, tmp_path: Path, monkeypatch):
    import snpr.outreach.webui as webui_mod

    log = tmp_path / "scheduler.log"
    log.write_text(
        "===== 06.11.2026 11:05:17,68 ===== \n"
        "Поиск: 6 запросов, глубина 0.1 ч, пул 300, бюджет скоринга 100\n"
        "Новых вакансий после дедупа: 1 (из поиска 1, из очереди 0)\n"
        "Скоринг:\n"
        "  [139190268]  28 (pass) Ведущий менеджер — Ozon → без письма | Вакансия — PM\n"
        "Полных карточек забрано: 1 (только для выживших)\n"
        "Скоры записаны в журнал: 1\n"
        "Итог: всего 1 | отсеяно фильтром 0 | без скоринга 0 | pass 1 | maybe 0 | готово к пуску 0 | пусков 0\n"
        "===== 06.11.2026 11:06:17,68 ===== \n"
        "Поиск: 6 запросов\n"
        "Итог: всего 2 | отсеяно фильтром 2 | pass 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui_mod, "SCHEDULER_LOG", log)
    data = webui_mod.load_ticks({})
    assert len(data["ticks"]) == 2
    first = data["ticks"][0]
    assert first["at"] == "06.11.2026 11:05:17,68"
    assert "pass 1" in first["summary"]
    assert len(first["scoring"]) == 1
    assert "139190268" in first["scoring"][0]
    # лимит
    limited = webui_mod.load_ticks({"limit": "1"})
    assert len(limited["ticks"]) == 1
    assert limited["ticks"][0]["at"] == "06.11.2026 11:06:17,68"


def test_ticks_missing_file(tmp_path: Path, monkeypatch):
    import snpr.outreach.webui as webui_mod

    monkeypatch.setattr(webui_mod, "SCHEDULER_LOG", tmp_path / "nope.log")
    assert webui_mod.load_ticks({}) == {"ticks": []}


def test_scores_and_ticks_via_http(ui_paths: Paths, tmp_path: Path, monkeypatch):
    import snpr.outreach.webui as webui_mod

    scores_file = tmp_path / "scores.jsonl"
    scores_file.write_text(
        json.dumps(
            {
                "vacancy_id": "9",
                "name": "LLM Dev",
                "employer": "Yandex",
                "score": 60,
                "verdict": "maybe",
                "reasons": ["stack"],
                "ts": 1793946000,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    log = tmp_path / "scheduler.log"
    log.write_text(
        "===== 06.11.2026 12:00:00,00 ===== \nИтог: всего 1 | pass 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui_mod, "SCORES_JSONL", scores_file)
    monkeypatch.setattr(webui_mod, "SCHEDULER_LOG", log)
    # изолируем career-источник (не существует в tmp)
    monkeypatch.setattr(webui_mod, "_SNIPER_DATA", tmp_path)
    httpd = start_server(ui_paths)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        data = get_json(base + "/api/scores?verdict=maybe")
        assert data["total"] == 1
        assert data["scores"][0]["vacancy_url"] == "https://hh.ru/vacancy/9"
        ticks = get_json(base + "/api/ticks")
        assert ticks["ticks"][0]["summary"].startswith("всего 1")
    finally:
        httpd.shutdown()
