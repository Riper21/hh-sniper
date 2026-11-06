"""Тесты career-адаптеров: alfa/huntflow/jobtoo на фикстурах (§10)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from snpr.outreach.career import ADAPTERS, get_adapter
from snpr.outreach.career.alfa import AlfaAdapter
from snpr.outreach.career.huntflow import HuntflowAdapter
from snpr.outreach.career.jobtoo import (
    JobtooAdapter,
    discover_bundle_url,
    parse_bundle,
)
from snpr.outreach.http import HttpClient
from snpr.outreach.store import JsonlStore


def make_client(
    tmp_path: Path, script: dict[str, str], monkeypatch=None
) -> HttpClient:
    """Мок-сеть: ключ = подстрока URL, первое совпадение по порядку вставки
    (специфичные паттерны — ПЕРЕД общими).

    monkeypatch обязателен: прямой присбор urlopen утекает в соседние
    тест-файлы (webui поднимает живой локальный сервер).
    """
    client = HttpClient(
        tmp_path / "cache",
        min_interval=0,
        throttle_sleep=0,
        retry_sleeps=(0.0,),
        backoff_sleeps=(0.0,),
        use_cache=False,
    )

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import urllib.error

    def fake_urlopen(req, timeout=None, context=None):
        url = req.full_url
        for pattern, body in script.items():
            if pattern in url:
                return FakeResponse(body.encode())
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)

    assert monkeypatch is not None, "передайте monkeypatch (анти-утечка)"
    monkeypatch.setattr(
        "snpr.outreach.http.urllib.request.urlopen", fake_urlopen
    )
    return client


# ------------------------------------------------------------------ alfa

def alfa_list(names: list[str]) -> dict:
    return {
        "total": len(names),
        "items": [{"id": f"v{i}", "name": name} for i, name in enumerate(names)],
    }


def alfa_card(vid: str, name: str) -> dict:
    return {
        "name": name,
        "contactFullName": "Мария Иванова",
        "contactEmail": "m.ivanova@alfabank.ru",
        "contactPhone": "+7 999 111-22-33",
    }


def test_alfa_fetch_ml_with_contacts(tmp_path: Path, monkeypatch):
    client = make_client(
        tmp_path,
        {
            "api/vacancies?take=100&skip=0": json.dumps(
                alfa_list(
                    [
                        "Senior ML Engineer",
                        "Бухгалтер",  # не ML — фильтруется
                        "LLM-разработчик",
                    ]
                )
            ),
            "api/vacancies/v0": json.dumps(alfa_card("v0", "Senior ML Engineer")),
            "api/vacancies/v2": json.dumps(alfa_card("v2", "LLM-разработчик")),
        },
        monkeypatch,
    )
    adapter = AlfaAdapter(client, request_delay=0, page_delay=0)
    results = adapter.fetch_vacancies({})
    assert len(results) == 2
    rec = results[0]
    assert rec["platform"] == "alfa-api"
    assert rec["manager_name"] == "Мария Иванова"
    assert rec["manager_email"] == "m.ivanova@alfabank.ru"
    assert rec["manager_phones"] == ["+7 999 111-22-33"]
    assert rec["vacancy_id"] == "v0"
    assert rec["checked_at"]


def test_alfa_run_upserts_store(tmp_path: Path, monkeypatch):
    client = make_client(
        tmp_path,
        {
            "api/vacancies?take=100&skip=0": json.dumps(alfa_list(["ML Engineer"])),
            "api/vacancies/v0": json.dumps(alfa_card("v0", "ML Engineer")),
        },
        monkeypatch,
    )
    adapter = AlfaAdapter(client, request_delay=0, page_delay=0)
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    written = adapter.run(store)
    assert written == 1
    # повтор — идемпотентно
    assert adapter.run(store) == 0


def test_alfa_skips_vacancies_without_contacts(tmp_path: Path, monkeypatch):
    client = make_client(
        tmp_path,
        {
            "api/vacancies?take=100&skip=0": json.dumps(alfa_list(["ML Eng"])),
            "api/vacancies/v0": json.dumps({"name": "ML Eng"}),
        },
        monkeypatch,
    )
    adapter = AlfaAdapter(client, request_delay=0, page_delay=0)
    assert adapter.fetch_vacancies({}) == []


# -------------------------------------------------------------- huntflow

HUNTFLOW_LIST = """
<html><body>
<a href="/vacancy/ml-engineer">ML Engineer</a>
<a href="/vacancy/accountant">Accountant</a>
<a href="/vacancy/ml-engineer">dup</a>
</body></html>
"""

HUNTFLOW_CARD = """
<html><body><script>var x=1;</script>
<div>Пиши на почту рекрутеру Мария Иванова m.ivanova@sber2b.ru</div>
<div>Вопросы на support@huntflow.io</div>
</body></html>
"""


def test_huntflow_extract_email_and_name(tmp_path: Path, monkeypatch):
    client = make_client(
        tmp_path,
        {
            # специфичные паттерны карточек ПЕРЕД общим паттерном списка
            "/vacancy/ml-engineer": HUNTFLOW_CARD,
            "/vacancy/accountant": "<html>нет email</html>",
            "/vacancy": HUNTFLOW_LIST,
        },
        monkeypatch,
    )
    adapter = HuntflowAdapter(client, request_delay=0)
    results = adapter.fetch_vacancies({"career_domain": "sber2b.huntflow.io"})
    assert len(results) == 1
    rec = results[0]
    assert rec["platform"] == "huntflow"
    assert rec["manager_email"] == "m.ivanova@sber2b.ru"
    assert rec["manager_name"] == "Мария Иванова"
    assert rec["vacancy_id"] == "/vacancy/ml-engineer"
    assert rec["vacancy_url"] == "https://sber2b.huntflow.io/vacancy/ml-engineer"


def test_huntflow_generic_emails_filtered(tmp_path: Path, monkeypatch):
    card = "<html><body>info@x.ru и sales@x.ru только</body></html>"
    client = make_client(
        tmp_path,
        {
            "/vacancy/ml-engineer": card,
            "/vacancy": HUNTFLOW_LIST,
        },
        monkeypatch,
    )
    adapter = HuntflowAdapter(client, request_delay=0)
    assert adapter.fetch_vacancies({"career_domain": "d.io"}) == []


def test_huntflow_empty_domain(tmp_path: Path, monkeypatch):
    adapter = HuntflowAdapter(
        make_client(tmp_path, {}, monkeypatch), request_delay=0
    )
    assert adapter.fetch_vacancies({}) == []


# ---------------------------------------------------------------- jobtoo

JOBTOO_JS = (
    'var companies = [["Альфа-Банк","job.alfabank.ru"],'
    '["Сбер","sber2b.huntflow.io"],'
    '["Альфа-Банк","job.alfabank.ru"],'
    '["Ozon","career.ozon.ru"]];'
)


def test_parse_bundle():
    records = parse_bundle(JOBTOO_JS)
    assert len(records) == 3  # дубликат домена исключён
    assert records[0] == {"company": "Альфа-Банк", "career_domain": "job.alfabank.ru"}
    assert records[2] == {"company": "Ozon", "career_domain": "career.ozon.ru"}


def test_parse_bundle_unicode_escape():
    js = '[["\u0422\u0411\u0430\u043d\u043a","job.tbank.ru"]]'
    records = parse_bundle(js)
    assert records[0]["company"] == "ТБанк"


def test_discover_bundle_url_from_home(tmp_path: Path, monkeypatch):
    home = '<html><script src="/_expo/static/js/web/entry-new123.js"></script></html>'
    client = make_client(tmp_path, {"jobtoo.ru": home}, monkeypatch)
    assert discover_bundle_url(client) == "https://jobtoo.ru/_expo/static/js/web/entry-new123.js"


def test_discover_bundle_url_fallback(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, {}, monkeypatch)  # 404 на главную
    from snpr.outreach.career.jobtoo import KNOWN_BUNDLE_URLS

    assert discover_bundle_url(client) == KNOWN_BUNDLE_URLS[0]


def test_jobtoo_run(tmp_path: Path, monkeypatch):
    # свежий релиз jobtoo: новый entry НЕ несёт справочник (0 пар),
    # справочник живёт в легаси-чанке — дискаври находит его перебором
    legacy_js = "var companies = [%s];" % ",".join(
        f'["Компания {i}","c{i}.example.ru"]' for i in range(120)
    ) + '[["Альфа-Банк","job.alfabank.ru"],["Сбер","sber2b.huntflow.io"]]'
    client = make_client(
        tmp_path,
        {
            # маленькие чанки главной ПЕРВЫМИ (без справочника)
            "/js/entry-new.js": "runtime only",
            "/js/index.js": "routes",
            # легаси-чанк со справочником (последним — как в KNOWN_BUNDLE_URLS)
            "entry-a3190a93": legacy_js,
            "jobtoo.ru": (
                '<script src="/js/entry-new.js"></script>'
                '<script src="/js/index.js"></script>'
            ),
        },
        monkeypatch,
    )
    adapter = JobtooAdapter(client)
    store = JsonlStore(tmp_path / "sites.jsonl", "career_domain")
    count = adapter.run(store)
    assert count == 122  # 120 синтетики + 2 известных
    assert store.load_map()["job.alfabank.ru"]["company"] == "Альфа-Банк"


def test_jobtoo_run_directory_in_current_bundle(tmp_path: Path, monkeypatch):
    # сценарий «справочник в актуальном чанке» (как до переупаковки jobtoo)
    js = "var companies = [%s];" % ",".join(
        f'["Компания {i}","c{i}.example.ru"]' for i in range(120)
    )
    client = make_client(
        tmp_path,
        {
            "/js/entry-x.js": js,
            "jobtoo.ru": '<script src="/js/entry-x.js"></script>',
        },
        monkeypatch,
    )
    adapter = JobtooAdapter(client)
    store = JsonlStore(tmp_path / "sites.jsonl", "career_domain")
    assert adapter.run(store) == 120


def test_jobtoo_unavailable(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, {}, monkeypatch)
    adapter = JobtooAdapter(client)
    store = JsonlStore(tmp_path / "sites.jsonl", "career_domain")
    assert adapter.run(store) == 0
    assert store.load() == []


# ---------------------------------------------------------------- registry

def test_registry():
    assert set(ADAPTERS) == {
        "alfa", "huntflow", "jobtoo", "sber", "avito", "ozon"
    }
    assert get_adapter("alfa") is AlfaAdapter
    with pytest.raises(KeyError):
        get_adapter("nope")
