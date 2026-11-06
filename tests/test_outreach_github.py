"""Тесты GitHub-адаптера: резолв оргов, email из коммитов, персоны (§10)."""
from __future__ import annotations

from pathlib import Path

from snpr.outreach.github import (
    GithubAdapter,
    build_person,
    dominant_commit_domain,
    domain_from_blog,
    emails_from_commits,
    norm_name,
    resolve_org,
    run_orgs,
    run_people,
    seed,
)
from snpr.outreach.http import HttpClient
from snpr.outreach.store import JsonlStore


def make_gh(
    tmp_path: Path, script: dict[str, list], monkeypatch=None
) -> GithubAdapter:
    client = HttpClient(
        tmp_path / "cache",
        min_interval=0,
        throttle_sleep=0,
        retry_sleeps=(0.0,),
        backoff_sleeps=(0.0,),
        use_cache=False,
    )
    adapter = GithubAdapter(client)

    import urllib.error

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        import json as json_mod

        url = req.full_url
        for pattern, actions in script.items():
            if pattern in url:
                action = actions.pop(0) if actions else None
                if action is None:
                    return FakeResponse(b"{}")
                if isinstance(action, Exception):
                    raise action
                if isinstance(action, int):
                    raise urllib.error.HTTPError(url, action, "e", {}, None)
                if isinstance(action, (dict, list)):
                    body = json_mod.dumps(action).encode()
                elif isinstance(action, bytes):
                    body = action
                else:
                    body = action.encode()
                return FakeResponse(body)
        return FakeResponse(b"{}")

    assert monkeypatch is not None, "передайте monkeypatch (анти-утечка)"
    monkeypatch.setattr(
        "snpr.outreach.http.urllib.request.urlopen", fake_urlopen
    )
    return adapter


def org_payload(login: str, blog: str = "") -> dict:
    return {"login": login, "blog": blog, "public_repos": 57}


def search_payload(*logins: str) -> dict:
    return {"items": [{"login": l} for l in logins]}


def test_norm_name():
    # порт 1:1 run.py: NFKD-lower-alnum, юр-формы НЕ сбрасываются
    assert norm_name("ООО «Контур»") == "оооконтур"
    assert norm_name("Avito-tech") == "avitotech"


def test_domain_from_blog():
    assert domain_from_blog("https://avito.tech/blog") == "avito.tech"
    assert domain_from_blog("http://www.kontur.ru") == "kontur.ru"
    assert domain_from_blog("") is None
    assert domain_from_blog(None) is None


def test_resolve_org_by_hint(tmp_path: Path, monkeypatch):
    gh = make_gh(tmp_path, {"/orgs/avito-tech": [org_payload("avito-tech", "https://avito.io")]}, monkeypatch)
    row = {"name": "Авито", "group": "Ecommerce", "priority": "A", "github_hint": "avito-tech"}
    rec = resolve_org(gh, row)
    assert rec["status"] == "ok"
    assert rec["org_source"] == "hint"
    assert rec["github_org"] == "avito-tech"
    assert rec["email_domain"] == "avito.io"
    assert rec["checked_at"]


def test_resolve_org_not_found(tmp_path: Path, monkeypatch):
    gh = make_gh(
        tmp_path,
        {"/search/users": [search_payload()], "/orgs/": [{}]},
        monkeypatch,
    )
    row = {"name": "Несуществующая", "group": "X", "priority": "B", "github_hint": ""}
    rec = resolve_org(gh, row)
    assert rec["status"] == "not_found"
    assert rec["github_org"] is None


def test_resolve_org_ambiguous_below_threshold(tmp_path: Path, monkeypatch):
    gh = make_gh(
        tmp_path,
        {
            "/search/users": [search_payload("some-unrelated-org")],
            "/orgs/some-unrelated-org": [org_payload("some-unrelated-org")],
        },
        monkeypatch,
    )
    row = {"name": "Авито", "group": "E", "priority": "A", "github_hint": ""}
    rec = resolve_org(gh, row, similar_min=0.9)
    assert rec["status"] == "ambiguous"
    assert rec["org_candidates"] == ["some-unrelated-org"]


def test_resolve_org_search_match(tmp_path: Path, monkeypatch):
    gh = make_gh(
        tmp_path,
        {
            "/search/users": [search_payload("avito")],
            "/orgs/avito": [org_payload("avito", "https://avito.ru")],
        },
        monkeypatch,
    )
    row = {"name": "avito", "group": "E", "priority": "A", "github_hint": ""}
    rec = resolve_org(gh, row)
    assert rec["status"] == "ok"
    assert rec["org_source"] == "search"


def test_seed_and_invalidation(tmp_path: Path, monkeypatch):
    orgs = JsonlStore(tmp_path / "orgs.jsonl", "name")
    orgs.upsert(
        {
            "name": "Авито",
            "github_hint": "old-hint",
            "github_org": "resolved-old",
            "status": "ok",
        }
    )
    tsv = tmp_path / "companies.tsv"
    tsv.write_text(
        "name\tgroup\tpriority\tconferences\tgithub_hint\n"
        "Авито\tEcommerce\tA\taij\tavito-tech\n"
        "Яндекс\tIT\tA\tdf\tyandex\n",
        encoding="utf-8",
    )
    count = seed(orgs, tsv)
    assert count == 2
    records = orgs.load_map()
    # hint изменился → резолв инвалидирован (None — upsert-мердж затёр)
    assert records["Авито"]["github_org"] is None
    assert records["Авито"]["status"] is None
    assert records["Авито"]["github_hint"] == "avito-tech"
    assert records["Яндекс"]["github_hint"] == "yandex"


def test_run_orgs_dry_writes_nothing(tmp_path: Path, monkeypatch):
    gh = make_gh(tmp_path, {"/orgs/avito-tech": [org_payload("avito-tech")]}, monkeypatch)
    orgs = JsonlStore(tmp_path / "orgs.jsonl", "name")
    orgs.upsert({"name": "Авито", "group": "E", "priority": "A", "github_hint": "avito-tech"})
    assert run_orgs(gh, orgs, dry=True) == 0
    rec = orgs.load_map()["Авито"]
    assert "github_org" not in rec  # dry — ничего не записано


def test_run_orgs_full(tmp_path: Path, monkeypatch):
    gh = make_gh(tmp_path, {"/orgs/avito-tech": [org_payload("avito-tech")]}, monkeypatch)
    orgs = JsonlStore(tmp_path / "orgs.jsonl", "name")
    orgs.upsert({"name": "Авито", "group": "E", "priority": "A", "github_hint": "avito-tech"})
    written = run_orgs(gh, orgs, groups=("A",))
    assert written == 1
    assert orgs.load_map()["Авито"]["github_org"] == "avito-tech"


def test_emails_from_commits_collects_and_filters_robots(tmp_path: Path, monkeypatch):
    def commit(name: str, email: str, login: str = "") -> dict:
        return {
            "commit": {"author": {"name": name, "email": email}},
            "author": {"login": login} if login else None,
        }

    repo_commits = [
        commit("Ivan Petrov", "ipetrov@avito.ru", "ipetrov"),
        commit("Ivan Petrov", "ipetrov@avito.ru", "ipetrov"),  # дедуп
        commit("Anon Dev", "anon@avito.ru"),  # без login
        commit("CI Bot", "ci-bot@avito.ru", "ci-bot"),  # робот
        {"commit": {"author": {"name": "X", "email": "x@noreply.github.com"}}},
    ]
    gh = make_gh(
        tmp_path,
        {
            "/repos/": [repo_commits, []],
            "/orgs/avito/repos": [
                [{"name": "backend"}, {"name": "frontend"}]
            ],
        },
        monkeypatch,
    )
    people = emails_from_commits(gh, "avito")
    emails = {p["email"] for p in people}
    assert emails == {"ipetrov@avito.ru", "anon@avito.ru"}
    by_email = {p["email"]: p for p in people}
    assert by_email["ipetrov@avito.ru"]["login"] == "ipetrov"
    assert by_email["anon@avito.ru"]["login"] == ""


def test_dominant_commit_domain():
    people = [
        {"email": "a@avito.ru"},
        {"email": "b@avito.ru"},
        {"email": "c@gmail.com"},
    ]
    assert dominant_commit_domain(people) == "avito.ru"
    assert dominant_commit_domain([]) is None


def test_build_person_commits_corp_high():
    rec = build_person(
        "avito",
        "Ecommerce",
        "avito.ru",
        ["python", "llm"],
        gh_login="ipetrov",
        name="Ivan Petrov",
        pub_email="ivan@gmail.com",
        commit_email="ipetrov@avito.ru",
        commit_repos=["backend"],
        contrib=120,
        user_repos=[{"name": "ml", "language": "Python", "description": "llm tools"}],
        org_repos=[],
        bio="ml engineer",
        blog="",
        location="Moscow",
    )
    assert rec["email"] == "ipetrov@avito.ru"
    assert rec["email_source"] == "commits_corp"
    assert rec["email_confidence"] == "high"
    assert rec["status"] == "ok"
    assert rec["stack_matched"] and "python" in rec["stack_matched"]
    assert rec["person_key"] == "avito:ipetrov:ipetrov@avito.ru"


def test_build_person_personal_only():
    # семантика research: corp_domain=None → «домен неизвестен», любой
    # публичный email трактуется как github_public_corp (run.py:517-522);
    # личный статус возможен только при известном corp_domain
    rec = build_person(
        "avito", "E", "avito.ru", [],
        gh_login="x", name="X", pub_email="x@gmail.com",
        commit_email="", commit_repos=[], contrib=0,
        user_repos=[], org_repos=[], bio="", blog="", location="",
    )
    assert rec["email_source"] == "personal"
    assert rec["email_confidence"] == "none"
    assert rec["status"] == "personal_only"
    # а при corp_domain=None тот же email — github_public_corp (порт 1:1)
    rec2 = build_person(
        "avito", "E", None, [],
        gh_login="x", name="X", pub_email="x@gmail.com",
        commit_email="", commit_repos=[], contrib=0,
        user_repos=[], org_repos=[], bio="", blog="", location="",
    )
    assert rec2["email_source"] == "github_public_corp"


def test_build_person_no_email():
    rec = build_person(
        "avito", "E", None, [],
        gh_login="", name="N", pub_email="", commit_email="",
        commit_repos=[], contrib=0, user_repos=[], org_repos=[],
        bio="", blog="", location="",
    )
    assert rec["status"] == "no_email"
    assert rec["email_source"] == "none"


def test_run_people_end_to_end(tmp_path: Path, monkeypatch):
    member = {"login": "ipetrov"}
    user = {
        "login": "ipetrov", "name": "Ivan Petrov", "email": "ipetrov@avito.ru",
        "bio": "ml", "blog": "", "location": "Moscow",
        "contributions_last_year": 100,
    }
    commits = [
        {
            "commit": {"author": {"name": "Anon", "email": "anon@avito.ru"}},
            "author": None,
        }
    ]
    gh = make_gh(
        tmp_path,
        {
            "/orgs/avito/members": [[member]],
            "/repos/avito/backend/commits": [commits],
            "/orgs/avito/repos": [[{"name": "backend", "language": "Python"}]],
            "/users/ipetrov": [user],
            "/users/ipetrov/repos": [[{"name": "ml", "language": "Python"}]],
        },
        monkeypatch,
    )
    orgs = JsonlStore(tmp_path / "orgs.jsonl", "name")
    orgs.upsert(
        {
            "name": "Авито", "group": "Ecommerce", "priority": "A",
            "github_org": "avito", "status": "ok",
        }
    )
    people = JsonlStore(tmp_path / "people.jsonl", "person_key")
    written = run_people(gh, orgs, people, stack=["python"], groups=("A",))
    assert written == 2
    records = {r["github_login"]: r for r in people.load()}
    assert "ipetrov" in records
    anon = [r for r in people.load() if not r["github_login"]]
    assert len(anon) == 1
    assert anon[0]["email"] == "anon@avito.ru"
    assert anon[0]["email_source"] == "commits_corp"
