"""Тесты сбер/авито/озон-адаптеров на фикстурах разведки 06.11 (§10)."""
from __future__ import annotations

from pathlib import Path

from snpr.outreach.career import ADAPTERS, get_adapter
from snpr.outreach.career.avito import AvitoAdapter, parse_list, parse_title
from snpr.outreach.career.ozon import OzonAdapter
from snpr.outreach.career.sber import (
    SberAdapter,
    parse_sitemap,
    parse_vacancy_card,
    slug_is_ml,
    vacancy_text,
)
from snpr.outreach.http import HttpClient
from snpr.outreach.store import JsonlStore


def make_client(tmp_path: Path, script: dict[str, str], monkeypatch) -> HttpClient:
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

    monkeypatch.setattr(
        "snpr.outreach.http.urllib.request.urlopen", fake_urlopen
    )
    return client


# ------------------------------------------------------------------ sber

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://rabota.sber.ru</loc></url>
  <url><loc>https://rabota.sber.ru/search/</loc></url>
  <url><loc>https://rabota.sber.ru/search/team-lead-llm-engineer-genai-4558159/</loc></url>
  <url><loc>https://rabota.sber.ru/search/veduschiy-specialist-po-finansovomu-4545417/</loc></url>
  <url><loc>https://rabota.sber.ru/search/python-developer-4578715/</loc></url>
  <url><loc>https://rabota.sber.ru/search/menedzher-po-4556880/</loc></url>
</urlset>"""

CARD_HTML = """<html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"vacancy":{
  "requisitionId":"x","internalId":4558159,
  "title":"Team Lead LLM engineer GenAI","company":"ПАО Сбербанк",
  "introduction":"RAG-платформа, агенты, LLM",
  "duties":"RAG, Agentic RAG, evals",
  "requirements":"LLM, Python, LangGraph",
  "specialization":"Дата-сайентист",
  "city":"г Москва","salary_min":500000,"salary_max":800000,
  "publicationDate":"2026-11-05T11:36:45.000Z"
}}}}
</script>
</body></html>"""


def test_sber_parse_sitemap():
    urls = parse_sitemap(SITEMAP_XML)
    assert len(urls) == 4  # корень и /search/ отрезаны
    assert all(u.startswith("https://rabota.sber.ru/search/") for u in urls)


def test_sber_slug_is_ml():
    assert slug_is_ml("https://rabota.sber.ru/search/python-developer-4578715/")
    assert slug_is_ml("https://rabota.sber.ru/search/team-lead-llm-engineer-genai-4558159/")
    assert slug_is_ml("https://rabota.sber.ru/search/stazher-ml-engineer-4584837/")
    assert not slug_is_ml("https://rabota.sber.ru/search/veduschiy-specialist-po-4545417/")
    assert not slug_is_ml("https://rabota.sber.ru/search/menedzher-po-4556880/")


def test_sber_parse_card():
    vacancy = parse_vacancy_card(CARD_HTML)
    assert vacancy is not None
    assert vacancy["internalId"] == 4558159
    assert vacancy["title"] == "Team Lead LLM engineer GenAI"
    text = vacancy_text(vacancy)
    assert "RAG" in text and "Python" in text


def test_sber_parse_card_no_data():
    assert parse_vacancy_card("<html>нет данных</html>") is None
    assert parse_vacancy_card('<script id="__NEXT_DATA__">{"props":{}}</script>') is None


def test_sber_run_end_to_end(tmp_path: Path, monkeypatch):
    # ML-слаги: llm/python; карточки только для них; менеджер НЕ найден
    # (mm: не ML) — отсеется глубоким фильтром.
    # python-карточка с ДРУГИМ internalId: после 06.11-фикса upsert_many
    # дедупит внутрибатчевые ключи (раньше одинаковые id писались дважды)
    card_py = CARD_HTML.replace('"internalId":4558159', '"internalId":4578715')
    card_mm = CARD_HTML.replace("LLM", "финансовому оздоровлению").replace(
        "RAG-платформа, агенты, LLM", "финансовое оздоровление клиентов"
    ).replace("RAG, Agentic RAG, evals", "финансы").replace(
        "LLM, Python, LangGraph", "финансы"
    ).replace('"internalId":4558159', '"internalId":4545417').replace(
        '"specialization":"Дата-сайентист"', '"specialization":"Финансы"'
    )
    client = make_client(
        tmp_path,
        {
            "sitemap": SITEMAP_XML,
            "team-lead-llm-engineer": CARD_HTML,
            "python-developer": card_py,
            "veduschiy-specialist": card_mm,
        },
        monkeypatch,
    )
    adapter = SberAdapter(client, request_delay=0)
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    written = adapter.run(store)
    assert written == 2  # llm + python карточки (разные internalId)
    records = store.load_map()
    assert "4558159" in records and "4578715" in records
    rec = records["4558159"]
    assert rec["platform"] == "sber-ssr"
    assert rec["vacancy_name"] == "Team Lead LLM engineer GenAI"
    assert rec["company"] == "Сбер"
    assert rec["area"] == "г Москва"
    assert rec["salary"]["to"] == 800000
    assert rec["manager_email"] == ""  # матчинговый канал — email нет
    assert rec["extra"]["specialization"] == "Дата-сайентист"


def test_sber_sitemap_unavailable(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, {}, monkeypatch)
    adapter = SberAdapter(client, request_delay=0)
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    assert adapter.run(store) == 0


# ----------------------------------------------------------------- avito

AVITO_LIST = """<html><body>
<a href="/vacancies/data-science/20872/">DS</a>
<a href="/vacancies/data-science/20868/">DS2</a>
<a href="/vacancies/prodazhi/20805/">Продажи</a>
<a href="/vacancies/prodazhi/20805/">dup</a>
</body></html>"""

AVITO_CARD = """<html><body><main>
<h1>Data Scientist в&nbsp;команду Support Automation</h1>
Команда Доверие и безопасность. Локация Москва. LLM, RAG, Python, ML-модели.
</main></body></html>"""


def test_avito_parse_list():
    urls = parse_list(AVITO_LIST)
    assert len(urls) == 3  # дубликат отрезан
    assert urls[0] == "https://career.avito.com/vacancies/data-science/20872/"


def test_avito_parse_title():
    assert parse_title(AVITO_CARD) == "Data Scientist в команду Support Automation"


def test_avito_run(tmp_path: Path, monkeypatch):
    # карточка продаж — не ML, отсеется
    # (паттерны карточек ПЕРЕД списком: мок матчит по подстроке)
    sales_card = AVITO_CARD.replace(
        "Data Scientist в&nbsp;команду Support Automation", "Менеджер по продажам"
    ).replace("LLM, RAG, Python, ML-модели", "холодные звонки, воронка")
    client = make_client(
        tmp_path,
        {
            "/20872/": AVITO_CARD,
            "/20868/": AVITO_CARD.replace("20872", "20868"),
            "/20805/": sales_card,
            "/vacancies/": AVITO_LIST,
        },
        monkeypatch,
    )
    adapter = AvitoAdapter(client, request_delay=0)
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    written = adapter.run(store)
    assert written == 2  # data-science карточки
    rec = store.load_map()["20872"]
    assert rec["platform"] == "avito-ssr"
    assert rec["company"] == "Авито"
    assert rec["vacancy_name"].startswith("Data Scientist")
    assert rec["manager_email"] == ""


# ------------------------------------------------------------------ ozon

def test_ozon_stub_closed(tmp_path: Path, monkeypatch):
    # сайт-заглушка «Скоро что-то будет» → 0, тихо
    client = make_client(
        tmp_path,
        {"career.ozon.ru": "<html><title>Скоро что-то будет</title></html>"},
        monkeypatch,
    )
    adapter = OzonAdapter(client)
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    assert adapter.run(store) == 0
    assert store.load() == []


def test_ozon_stub_alive_no_parser(tmp_path: Path, monkeypatch):
    # портал ожил (большой HTML) — честное сообщение, парсера нет
    big = "<html>" + "x" * 25000 + "</html>"
    client = make_client(tmp_path, {"career.ozon.ru": big}, monkeypatch)
    adapter = OzonAdapter(client)
    store = JsonlStore(tmp_path / "career.jsonl", "vacancy_id")
    assert adapter.run(store) == 0


# -------------------------------------------------------------- registry

def test_registry_extended():
    assert set(ADAPTERS) == {"alfa", "huntflow", "jobtoo", "sber", "avito", "ozon"}
    assert get_adapter("sber") is SberAdapter
    assert get_adapter("avito") is AvitoAdapter
    assert get_adapter("ozon") is OzonAdapter
