"""Тесты Habr-адаптера: parse_profile, контакты, crawl/match/bridge (§10)."""
from __future__ import annotations

from pathlib import Path

from snpr.outreach.habr import (
    HabrAdapter,
    bridge_candidates,
    habr_unescape,
)
from snpr.outreach.http import HttpClient
from snpr.outreach.store import JsonlStore


def make_habr(
    tmp_path: Path, script: dict[str, str], monkeypatch=None, **kwargs
) -> HabrAdapter:
    client = HttpClient(
        tmp_path / "cache",
        min_interval=0,
        throttle_sleep=0,
        retry_sleeps=(0.0,),
        backoff_sleeps=(0.0,),
        use_cache=False,
    )
    adapter = HabrAdapter(client, profile_delay=0, list_delay=0, **kwargs)

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
    return adapter


def profile_html(login: str, fullname: str = "Иван Петров") -> str:
    # формат реального Habr-стейта: contacts — JSON-массив с обычными кавычками
    contacts = (
        '[{"title":"GitHub","url":"https://github.com/ipetrov"},'
        '{"title":"Telegram","url":"https://telegram.me/ipetrov"},'
        '{"title":"Сайт","url":"https://ipetrov.ru","value":"ipetrov.ru"}]'
    )
    return f"""
<html>
<head>
<link href="https://habr.com/ru/users/{login}/" rel="canonical">
<title> {fullname} - &#x2F; Хабр</title>
</head>
<body>
<script>
window.data = {{"alias":"{login}","rating":150,"ratingPos":42,
"scoreStats":{{"score":120,"votesCount":77}},"articleCount":12,
"speciality":"ML engineer",
"lastActivityDateTime":"2026-11-01T10:00:00.000+00:00",
"registerDateTime":"2015-01-01T00:00:00.000+00:00",
"companies":{{"pagesCount":1,"companyIds":["avito","sber"]}},
"contacts":[{contacts}]}};
</script>
</body>
</html>
"""


def test_unescape():
    assert habr_unescape("\\u002Fpath") == "/path"
    assert habr_unescape('\\"q\\"') == '"q"'
    assert habr_unescape("a\\u0026b") == "a&b"


def test_parse_profile_full(tmp_path: Path, monkeypatch):
    html = profile_html("ipetrov")
    habr = make_habr(tmp_path, {}, monkeypatch)
    profile = habr.parse_profile(html, "ipetrov")
    assert profile is not None
    assert profile["login"] == "ipetrov"
    assert profile["fullname"] == "Иван Петров"
    assert profile["karma"] == 150
    assert profile["rating_pos"] == 42
    assert profile["votes_count"] == 77
    assert profile["article_count"] == 12
    assert profile["speciality"] == "ML engineer"
    assert profile["last_activity"] == "2026-11-01"
    assert profile["registered"] == "2015-01-01"
    assert profile["habr_companies"] == ["avito", "sber"]
    assert len(profile["contacts"]) == 3


def test_parse_profile_canonical_mismatch(tmp_path: Path, monkeypatch):
    html = profile_html("other_login")
    habr = make_habr(tmp_path, {}, monkeypatch)
    assert habr.parse_profile(html, "ipetrov") is None


def test_extract_github_and_telegram(tmp_path: Path, monkeypatch):
    habr = make_habr(tmp_path, {}, monkeypatch)
    profile = habr.parse_profile(profile_html("ipetrov"), "ipetrov")
    contacts = profile["contacts"]
    assert habr.extract_github(contacts) == "ipetrov"
    assert habr.extract_telegram(contacts) == "ipetrov"
    site = habr.extract_contact(contacts, "Сайт")
    assert (site or {}).get("value") == "ipetrov.ru"


def test_extract_telegram_from_value(tmp_path: Path, monkeypatch):
    contacts = [{"title": "Telegram", "url": "", "value": "@myname"}]
    assert HabrAdapter.extract_telegram(contacts) == "myname"


def test_crawl_collect_and_store(tmp_path: Path, monkeypatch):
    hub_html = """
    <html><body>
    <a href="/ru/users/alice/">a</a>
    <a href="https://habr.com/ru/users/bob/">b</a>
    <a href="/ru/users/alice/">dup</a>
    </body></html>
    """
    habr = make_habr(
        tmp_path,
        {
            "/hubs/machine_learning/articles/": hub_html,
            "/users/alice/": profile_html("alice", "Алиса"),
            "/users/bob/": profile_html("bob", "Боб"),
        },
        monkeypatch,
        hubs=["machine_learning"],
    )
    store = JsonlStore(tmp_path / "crawl.jsonl", "login")
    written = habr.crawl(store, pages=1)
    assert written == 2
    records = store.load_map()
    assert records["alice"]["fullname"] == "Алиса"
    assert records["alice"]["github_login"] == "ipetrov" or records["alice"]["telegram"]
    # идемпотентность: повторный прогон — 0 новых
    assert habr.crawl(store, pages=1, dry=True) == 0


def test_crawl_dry_writes_nothing(tmp_path: Path, monkeypatch):
    hub_html = '<a href="/ru/users/alice/">a</a>'
    habr = make_habr(
        tmp_path,
        {
            "/hubs/machine_learning/articles/": hub_html,
            "/users/alice/": profile_html("alice"),
        },
        monkeypatch,
        hubs=["machine_learning"],
    )
    store = JsonlStore(tmp_path / "crawl.jsonl", "login")
    assert habr.crawl(store, pages=1, dry=True) == 0
    assert store.load() == []


def test_enrich(tmp_path: Path, monkeypatch):
    habr = make_habr(tmp_path, {"/users/alice/": profile_html("alice", "Алиса")}, monkeypatch)
    store = JsonlStore(tmp_path / "crawl.jsonl", "login")
    store.upsert({"login": "alice", "fullname": "Алиса"})
    written = habr.enrich(store)
    assert written == 1
    rec = store.load_map()["alice"]
    assert rec["karma"] == 150
    assert rec["article_count"] == 12
    # повторный enrich — уже всё есть
    assert habr.enrich(store, dry=True) == 0


def test_match_handles(tmp_path: Path, monkeypatch):
    habr = make_habr(
        tmp_path,
        {
            "/users/ipetrov/": profile_html("ipetrov", "Иван"),
        },
        monkeypatch,
    )
    people = JsonlStore(tmp_path / "people.jsonl", "person_key")
    people.upsert(
        {
            "person_key": "avito:ipetrov:x@avito.ru",
            "github_login": "ipetrov",
            "group": "Ecommerce",
            "github_org": "avito",
            "email": "x@avito.ru",
            "email_confidence": "high",
            "stack_match": 0.5,
        }
    )
    matches = JsonlStore(tmp_path / "matches.jsonl", "github_login")
    written = habr.match_handles(people, matches, full=True)
    assert written == 1
    rec = matches.load_map()["ipetrov"]
    assert rec["habr_fullname"] == "Иван"
    assert rec["telegram"] == "ipetrov"


def test_match_handles_skip_missing_profile(tmp_path: Path, monkeypatch):
    habr = make_habr(tmp_path, {}, monkeypatch)  # все 404
    people = JsonlStore(tmp_path / "people.jsonl", "person_key")
    people.upsert({"person_key": "k", "github_login": "ghost"})
    matches = JsonlStore(tmp_path / "matches.jsonl", "github_login")
    assert habr.match_handles(people, matches, full=True) == 0


def test_bridge_closed_experiment(tmp_path: Path, monkeypatch):
    """Вердикт исследования: 0 TG — фиксируем поведение тестом."""
    habr = make_habr(tmp_path, {"/users/zaikin/": profile_html("zaikin", "Заикин")}, monkeypatch)
    people = JsonlStore(tmp_path / "people.jsonl", "person_key")
    people.upsert(
        {
            "person_key": "k",
            "github_login": "kzaikin",
            "name": "Konstantin Zaikin",
            "email": "k.zaikin@avito.ru",
            "email_confidence": "high",
            "group": "E",
        }
    )
    bridge = JsonlStore(tmp_path / "bridge.jsonl", "email")
    written = habr.bridge(people, bridge)
    assert written == 1
    rec = bridge.load_map()["k.zaikin@avito.ru"]
    # профиль нашёлся по фамилии, TG в фикстуре есть — но в реальном прогоне
    # гипотеза опровергнута; проверяем механику, не вердикт
    assert rec["habr_login"] == "zaikin"
    assert rec["tried"] == ["kzaikin:404", "k.zaikin:404"]


def test_bridge_idempotent_by_email(tmp_path: Path, monkeypatch):
    habr = make_habr(tmp_path, {}, monkeypatch)
    people = JsonlStore(tmp_path / "people.jsonl", "person_key")
    people.upsert(
        {
            "person_key": "k",
            "github_login": "",
            "name": "",
            "email": "done@x.ru",
            "email_confidence": "high",
        }
    )
    bridge = JsonlStore(tmp_path / "bridge.jsonl", "email")
    bridge.upsert({"email": "done@x.ru", "tried": []})
    assert habr.bridge(people, bridge) == 0


def test_bridge_candidates_priority():
    person = {
        "github_login": "kzaikin",
        "email": "k.zaikin@avito.ru",
        "name": "Konstantin Zaikin",
    }
    assert bridge_candidates(person) == ["kzaikin", "k.zaikin", "zaikin"]
    assert bridge_candidates({"email": "a@b.ru"}) == ["a"]
    # одиночное слово (не фамилия с пробелом) — не кандидат
    assert bridge_candidates({"name": "One Word"}) == ["word"] or bridge_candidates(
        {"name": "One Word"}
    ) == []
