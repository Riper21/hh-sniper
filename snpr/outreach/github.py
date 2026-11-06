"""GitHub-адаптер outreach: компании → орг → люди → email из коммитов.

Порт research/outreach_mvp/run.py (шаги seed/orgs/people) поверх
HttpClient/JsonlStore/text/rank. Инсайт прогона 03.11 сохранён в докстрингах:
корпоративный email живёт в метаданных коммитов org-репо, а не в публичных
профилях; login — бонус, когда есть (многие коммитят без паблик-профиля).

Схема orgs.jsonl / people.jsonl — идентична исследованию (миграция без
конверсии), рендер карточек — в report.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from .http import HttpClient
from .rank import is_robot, stack_match
from .store import JsonlStore, read_tsv, touch_checked_at
from .text import similarity, translit

GH_API = "https://api.github.com"

REPOS_PER_ORG_DEFAULT = 3
COMMITS_PER_REPO_DEFAULT = 30
MAX_PEOPLE_PER_ORG_DEFAULT = 12
ORG_SIMILAR_MIN = 0.5


def norm_name(text: str) -> str:
    """NFKD-lower alnum (порт run.py:111-113, для сравнения имён оргов)."""
    import unicodedata

    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text).lower() if ch.isalnum()
    )


def domain_from_blog(blog: str | None) -> str | None:
    """Домен из blog-org (порт run.py:383-388)."""
    blog = (blog or "").strip()
    if not blog:
        return None
    blog = blog.removeprefix("https://").removeprefix("http://").split("/")[0]
    return blog.lower().removeprefix("www.") or None


class GithubAdapter:
    """GitHub REST поверх HttpClient (кэш+троттлинг+ретраи)."""

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    def get(self, path: str) -> Any:
        url = path if path.startswith("http") else f"{GH_API}{path}"
        return self.client.get_json(url)


def search_org_candidates(
    gh: GithubAdapter, name: str, hint: str = ""
) -> list[dict[str, Any]]:
    """Поиск org: транслит + оригинал; кандидаты по похожести (run.py:298-316)."""
    queries: list[str] = []
    latin = translit(name)
    if latin:
        queries.append(quote(f"{latin} type:org"))
    queries.append(quote(f"{name} type:org"))
    seen: dict[str, dict[str, Any]] = {}
    for q in queries:
        data = gh.get(f"/search/users?q={q}&per_page=10")
        for item in (data or {}).get("items", []):
            login = str(item.get("login") or "")
            if login and login.lower() not in seen:
                seen[login.lower()] = {
                    "login": login,
                    "sim": max(
                        similarity(norm_name(login), norm_name(latin)),
                        similarity(norm_name(login), norm_name(name)),
                    ),
                }
    return sorted(seen.values(), key=lambda x: -x["sim"])


def resolve_org(
    gh: GithubAdapter,
    row: dict[str, Any],
    *,
    similar_min: float = ORG_SIMILAR_MIN,
) -> dict[str, Any]:
    """Резолв одной компании → запись orgs.jsonl (порт cmd_orgs для одной).

    Статусы: ok (hint|search) / not_found / ambiguous.
    """
    name = str(row["name"])
    hint = str(row.get("github_hint", "") or "")
    base = dict(row)
    if hint:
        org = gh.get(f"/orgs/{quote(hint)}")
        if org and org.get("login"):
            return touch_checked_at(
                {
                    **base,
                    "github_org": org["login"],
                    "org_source": "hint",
                    "org_candidates": [],
                    "website": org.get("blog") or "",
                    "email_domain": domain_from_blog(str(org.get("blog") or "")),
                    "member_count": None,
                    "repos_count": org.get("public_repos"),
                    "search_query": name,
                    "status": "ok",
                }
            )
    candidates = search_org_candidates(gh, name, hint)
    best = candidates[0] if candidates else None
    if not best:
        return touch_checked_at(
            {
                **base,
                "github_org": None,
                "org_source": "search",
                "org_candidates": [],
                "website": "",
                "email_domain": None,
                "member_count": None,
                "repos_count": None,
                "search_query": name,
                "status": "not_found",
            }
        )
    if best["sim"] >= similar_min:
        org = gh.get(f"/orgs/{quote(best['login'])}")
        if org and org.get("login"):
            return touch_checked_at(
                {
                    **base,
                    "github_org": org["login"],
                    "org_source": "search",
                    "org_candidates": [c["login"] for c in candidates[:5]],
                    "website": org.get("blog") or "",
                    "email_domain": domain_from_blog(str(org.get("blog") or "")),
                    "member_count": None,
                    "repos_count": org.get("public_repos"),
                    "search_query": name,
                    "status": "ok",
                }
            )
    return touch_checked_at(
        {
            **base,
            "github_org": None,
            "org_source": "search",
            "org_candidates": [c["login"] for c in candidates[:5]],
            "website": "",
            "email_domain": None,
            "member_count": None,
            "repos_count": None,
            "search_query": name,
            "status": "ambiguous",
        }
    )


def seed(
    orgs_store: JsonlStore, companies_tsv: Path
) -> int:
    """Шаг seed: TSV → orgs.jsonl (порт cmd_seed, включая инвалидацию по hint).

    Инвалидация: JsonlStore.upsert мержит поля поверх существующей записи,
    поэтому попнутые поля фиксируем значением None прямо в записи —
    upsert-мердж перезапишет устаревшие значения.
    """
    rows = read_tsv(companies_tsv)
    existing = orgs_store.load_map()
    count = 0
    stale_fields = (
        "github_org", "org_source", "org_candidates",
        "status", "checked_at",
    )
    for row in rows:
        name = row.get("name", "").strip()
        if not name:
            continue
        hint = row.get("github_hint", "").strip()
        base = dict(existing.get(name) or {})
        invalidated = base.get("github_hint") != hint and hint
        record = {
            **base,
            "name": name,
            "group": row.get("group", "").strip(),
            "priority": row.get("priority", "").strip(),
            "conferences": row.get("conferences", "").strip(),
            "github_hint": hint,
        }
        if invalidated:
            # hint появился/изменился → старый резолв недействителен
            for stale in stale_fields:
                record[stale] = None
        orgs_store.upsert(record)
        count += 1
    return count


def run_orgs(
    gh: GithubAdapter,
    orgs_store: JsonlStore,
    *,
    groups: tuple[str, ...] = ("A", "B"),
    limit: int = 0,
    similar_min: float = ORG_SIMILAR_MIN,
    dry: bool = False,
) -> int:
    """Шаг orgs: резолв компаний (порт cmd_orgs). Возврат: обновлено записей."""
    records = orgs_store.load()
    todo = [
        r
        for r in records
        if r.get("priority") in groups
        and r.get("status") not in ("ok", "not_found", "skipped", "manual")
    ]
    if limit:
        todo = todo[:limit]
    results: list[dict[str, Any]] = []
    for row in todo:
        try:
            results.append(resolve_org(gh, row, similar_min=similar_min))
        except Exception as ex:  # noqa: BLE001 — порт ABORT-семантики run.py
            print(f"  ABORT {row.get('name')}: {ex}")
            break
    if dry:
        return 0
    written = orgs_store.upsert_many(results)
    done = sum(1 for r in orgs_store.load() if r.get("status") == "ok")
    print(f"orgs: обновлено {written}, разрешено {done}/{len(orgs_store.load())}")
    return written


def emails_from_commits(
    gh: GithubAdapter,
    org_login: str,
    *,
    repos_limit: int = REPOS_PER_ORG_DEFAULT,
    commits_limit: int = COMMITS_PER_REPO_DEFAULT,
) -> list[dict[str, Any]]:
    """Коммиты топ-репо org → {login, name, email, repos} (порт
    _emails_from_commits, run.py:393-446). Робо-аккаунты — is_robot."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    repos = gh.get(
        f"/orgs/{quote(org_login)}/repos?sort=pushed&per_page={repos_limit}"
    )
    for repo in repos or []:
        if not isinstance(repo, dict):
            continue
        commits = gh.get(
            f"/repos/{quote(org_login)}/{quote(str(repo.get('name')))}/commits"
            f"?per_page={commits_limit}"
        )
        for commit in commits or []:
            if not isinstance(commit, dict):
                continue
            meta = commit.get("commit") or {}
            author = meta.get("author") or {}
            if not isinstance(author, dict):
                continue
            email = str(author.get("email") or "")
            cname = str(author.get("name") or "")
            gh_author = commit.get("author") or {}
            login = (
                str(gh_author.get("login") or "")
                if isinstance(gh_author, dict)
                else ""
            )
            if not email or "noreply.github.com" in email:
                continue
            if email.startswith("robot-") or "robot@" in email:
                continue
            if is_robot(login, email) or is_robot(cname):
                continue
            key = (login or cname, email)
            slot = found.setdefault(
                key, {"login": login, "name": cname, "email": email, "repos": set()}
            )
            slot["repos"].add(str(repo.get("name")))
    out: list[dict[str, Any]] = []
    for slot in found.values():
        slot["repos"] = sorted(slot["repos"])[:3]
        out.append(slot)
    return out


def dominant_commit_domain(
    commit_people: list[dict[str, Any]],
) -> str | None:
    """Доминантный домен email коммитеров = корп. домен org (run.py:449-458)."""
    counts: dict[str, int] = {}
    for person in commit_people:
        domain = str(person.get("email") or "").rsplit("@", 1)[-1].lower()
        if domain and domain not in (
            "github.com", "users.noreply.github.com", "localhost",
        ):
            counts[domain] = counts.get(domain, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


def _domain_for_group(orgs: list[dict[str, Any]], group: str) -> str | None:
    for record in orgs:
        if record.get("group") == group and record.get("email_domain"):
            return str(record["email_domain"])
    return None


def build_person(
    login_org: str,
    group: str,
    corp_domain: str | None,
    stack: list[str],
    *,
    gh_login: str,
    name: str,
    pub_email: str,
    commit_email: str,
    commit_repos: list[str] | None,
    contrib: int,
    user_repos: list[Any],
    org_repos: list[Any],
    bio: str,
    blog: str,
    location: str,
) -> dict[str, Any]:
    """Единая запись people.jsonl (порт _score_person, run.py:504-555).

    Email: корпоративный из коммитов > публичный корп > личный > прочий.
    """
    repos_for_stack = user_repos if user_repos else (org_repos or [])
    blob = f"{bio} {blog}"
    langs: dict[str, int] = {}
    for repo in repos_for_stack:
        if not isinstance(repo, dict):
            continue
        lang = str(repo.get("language") or "").lower()
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
        blob += f" {repo.get('name') or ''} {repo.get('description') or ''}"
    matched_score, matched = stack_match(blob, stack)
    top_lang = max(langs, key=lambda k: langs[k]) if langs else ""
    if top_lang and top_lang in stack:
        matched = sorted(set(matched) | {top_lang})
    stack_score = round(len(matched) / max(1, len(stack)), 2)

    is_corp = "@" in commit_email and (
        not corp_domain
        or commit_email.rsplit("@", 1)[-1].lower() == corp_domain
    )
    is_pub_corp = "@" in pub_email and (
        not corp_domain or pub_email.rsplit("@", 1)[-1].lower() == corp_domain
    )
    if is_corp:
        email, source = commit_email, "commits_corp"
    elif is_pub_corp:
        email, source = pub_email, "github_public_corp"
    elif pub_email:
        email, source = pub_email, "personal"
    elif commit_email:
        email, source = commit_email, "commits_other_domain"
    else:
        email, source = "", "none"
    if not email:
        status = "no_email"
    elif source == "personal":
        status = "personal_only"
    else:
        status = "ok"
    confidence = (
        "high"
        if source in ("commits_corp", "github_public_corp")
        else "medium"
        if source == "commits_other_domain"
        else "none"
    )
    person_key = f"{login_org}:{gh_login or name}:{email}"
    return touch_checked_at(
        {
            "person_key": person_key,
            "github_login": gh_login,
            "github_org": login_org,
            "group": group,
            "name": name,
            "email": email,
            "email_source": source,
            "email_confidence": confidence,
            "corp_domain": corp_domain,
            "email_repo": (commit_repos or [None])[0],
            "contributions_last_year": contrib,
            "repos_in_org": len(commit_repos or []),
            "top_languages": sorted(
                {
                    str(r.get("language"))
                    for r in user_repos
                    if isinstance(r, dict) and r.get("language")
                }
            )[:5],
            "stack_match": stack_score,
            "stack_matched": matched,
            "bio": bio[:200],
            "blog": blog,
            "location": location,
            "status": status,
        }
    )


def run_people(
    gh: GithubAdapter,
    orgs_store: JsonlStore,
    people_store: JsonlStore,
    *,
    stack: list[str],
    groups: tuple[str, ...] = ("A", "B"),
    limit: int = 0,
    max_people_per_org: int = MAX_PEOPLE_PER_ORG_DEFAULT,
    repos_limit: int = REPOS_PER_ORG_DEFAULT,
    commits_limit: int = COMMITS_PER_REPO_DEFAULT,
    dry: bool = False,
) -> int:
    """Шаг people: union(члены org, коммитеры без профиля) (порт cmd_people).

    Модель прогона: (а) члены org с публичным профилем; (б) коммитеры БЕЗ
    профиля — email+имя из коммитов.
    """
    orgs = [
        r
        for r in orgs_store.load()
        if r.get("status") == "ok" and r.get("priority") in groups
    ]
    if limit:
        orgs = orgs[:limit]
    stack_lower = [s.lower() for s in stack]
    results: list[dict[str, Any]] = []
    for org in orgs:
        login = str(org["github_org"])
        group = str(org["group"])
        try:
            members = gh.get(f"/orgs/{quote(login)}/members?per_page=50") or []
            commit_people = emails_from_commits(
                gh, login, repos_limit=repos_limit, commits_limit=commits_limit
            )
            corp_domain = dominant_commit_domain(commit_people) or _domain_for_group(
                orgs_store.load(), group
            )
            org_repos = (
                gh.get(f"/orgs/{quote(login)}/repos?per_page=30&sort=pushed") or []
            )
            by_login = {
                p["login"]: p for p in commit_people if p.get("login")
            }
            scored: list[dict[str, Any]] = []
            # (а) члены org с публичным профилем
            for member in members:
                if not isinstance(member, dict):
                    continue
                ulogin = str(member.get("login") or "")
                if not ulogin:
                    continue
                user = gh.get(f"/users/{quote(ulogin)}") or {}
                pub_email = str(user.get("email") or "")
                if "noreply.github.com" in pub_email:
                    pub_email = ""
                commit_rec = by_login.get(ulogin) or {}
                user_repos = (
                    gh.get(
                        f"/users/{quote(ulogin)}/repos?per_page=30&sort=pushed"
                    )
                    or []
                )
                scored.append(
                    build_person(
                        login,
                        group,
                        corp_domain,
                        stack_lower,
                        gh_login=ulogin,
                        name=str(user.get("name") or "")
                        or str(commit_rec.get("name") or ""),
                        pub_email=pub_email,
                        commit_email=str(commit_rec.get("email") or ""),
                        commit_repos=commit_rec.get("repos") or [],
                        contrib=int(user.get("contributions_last_year") or 0),
                        user_repos=user_repos,
                        org_repos=org_repos,
                        bio=str(user.get("bio") or ""),
                        blog=str(user.get("blog") or ""),
                        location=str(user.get("location") or ""),
                    )
                )
            # (б) коммитеры БЕЗ публичного профиля
            for person in commit_people:
                if person.get("login"):
                    continue
                scored.append(
                    build_person(
                        login,
                        group,
                        corp_domain,
                        stack_lower,
                        gh_login="",
                        name=str(person.get("name") or ""),
                        pub_email="",
                        commit_email=str(person.get("email") or ""),
                        commit_repos=person.get("repos") or [],
                        contrib=0,
                        user_repos=[],
                        org_repos=org_repos,
                        bio="",
                        blog="",
                        location="",
                    )
                )
            scored.sort(
                key=lambda r: (-r["stack_match"], -r["contributions_last_year"])
            )
            results.extend(scored[:max_people_per_org])
            ok_n = sum(1 for r in scored if r["status"] == "ok")
            corp_n = sum(
                1
                for r in scored
                if r["status"] == "ok" and r["email_confidence"] == "high"
            )
            print(
                f"  {org.get('name')} ({login}): {len(scored)} людей, "
                f"{ok_n} с email ({corp_n} corp, домен {corp_domain})"
            )
        except Exception as ex:  # noqa: BLE001 — ABORT-семантика порта
            print(f"  ABORT на {org.get('name')}: {ex}")
            break
    if dry:
        return 0
    written = people_store.upsert_many(results)
    print(
        f"people: обновлено {written}, всего {len(people_store.load())}"
    )
    return written
