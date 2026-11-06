"""Habr-адаптер outreach: краул хабов, хэндл-матч, закрытый мост (ПЛАН §4.2).

Порт habr_crawl.py + habr_match.py + habr_bridge.py в один модуль поверх
HttpClient: ОДНА копия парсера (CONTACTS_RE/parse_profile/extract_telegram —
было 2 копии в research), лестницы пауз — в конфиге клиента.

Эмпирика прогонов: habr работает со СТАНДАРТНЫМ ssl-контекстом
(противоположность GitHub) — ssl_mode="default".

Мост (3Б) — закрытый эксперимент: гипотеза email→Habr→TG опровергнута
(0 TG из 53), сохранён для воспроизводимости вердикта.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .http import HttpClient
from .store import JsonlStore, touch_checked_at

HABR_BASE = "https://habr.com"
DEFAULT_HUBS = ("machine_learning", "artificial_intelligence")

CONTACTS_RE = re.compile(r'"contacts":\[(.*?)\]')
CONTACT_ITEM_RE = re.compile(
    r'\{"title":"([^"]+)","url":"((?:[^"\\]|\\.)*)"(?:,"value":"((?:[^"\\]|\\.)*)")?'
)
CANONICAL_RE = re.compile(
    r'<link href="https://habr\.com/ru/users/([a-zA-Z0-9_\-\.]+)/" rel="canonical"'
)
TITLE_RE = re.compile(r"<title>\s*([^<]+?)\s*-\s*&#x2F;\s*Хабр</title>", re.S)
AUTHOR_RE = re.compile(r'href="(?:https://habr\.com)?/ru/users/([a-zA-Z0-9_\-\.]+)/"')

ENRICH_FIELDS = (
    "karma", "rating_pos", "votes_count", "article_count",
    "speciality", "last_activity", "registered", "habr_companies",
)


def habr_unescape(value: str) -> str:
    """JSON-эскейпы в HTML-стейте Habr (порт habr_crawl.unescape)."""
    return (
        value.replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace('\\"', '"')
        .replace("\\u0026", "&")
    )


class HabrAdapter:
    """Habr-профили поверх HttpClient (get_text + кэш + ретраи)."""

    def __init__(
        self,
        client: HttpClient,
        *,
        profile_delay: float = 0.7,
        list_delay: float = 0.8,
        hubs: list[str] | None = None,
    ) -> None:
        self.client = client
        self.profile_delay = profile_delay
        self.list_delay = list_delay
        self.hubs = tuple(hubs or DEFAULT_HUBS)

    # ------------------------------------------------------------- fetching

    def profile_html(self, login: str) -> str | None:
        """HTML профиля или None (404/редирект)."""
        return self.client.get_text(f"{HABR_BASE}/ru/users/{quote(login)}/")

    def hub_page(self, hub: str, page: int) -> str | None:
        url = f"{HABR_BASE}/ru/hubs/{hub}/articles/"
        if page > 1:
            url += f"page{page}/"
        return self.client.get_text(url)

    # -------------------------------------------------------------- parsing

    def parse_profile(self, html: str, login: str) -> dict[str, Any] | None:
        """Профиль из HTML: canonical-валидация (порт habr_crawl:152-203)."""
        canonical = CANONICAL_RE.search(html)
        if not canonical or canonical.group(1) != login:
            return None
        profile: dict[str, Any] = {"login": login}
        title = TITLE_RE.search(html)
        if title:
            profile["fullname"] = title.group(1).strip()
        contacts_match = CONTACTS_RE.search(html)
        contacts: list[dict[str, Any]] = []
        if contacts_match and contacts_match.group(1).strip():
            for item in CONTACT_ITEM_RE.finditer(contacts_match.group(1)):
                contacts.append(
                    {
                        "title": item.group(1),
                        "url": habr_unescape(item.group(2) or ""),
                        "value": habr_unescape(item.group(3) or ""),
                    }
                )
        profile["contacts"] = contacts

        def num(pattern: str) -> int | None:
            m = re.search(pattern, html)
            return int(m.group(1)) if m else None

        karma = num(r'"rating":(-?\d+)')
        if karma is not None:
            profile["karma"] = karma
        rating_pos = num(r'"ratingPos":(\d+)')
        if rating_pos is not None:
            profile["rating_pos"] = rating_pos
        m = re.search(r'"scoreStats":\{"score":(-?\d+),"votesCount":(\d+)\}', html)
        if m:
            profile["votes_count"] = int(m.group(2))
        articles = num(r'"articleCount":(\d+)')
        if articles is not None:
            profile["article_count"] = articles
        m = re.search(r'"speciality":"((?:[^"\\]|\\.)*)"', html)
        if m and m.group(1).strip():
            profile["speciality"] = habr_unescape(m.group(1))
        m = re.search(r'"lastActivityDateTime":"([^"]+)"', html)
        if m:
            profile["last_activity"] = m.group(1)[:10]
        m = re.search(r'"registerDateTime":"([^"]+)"', html)
        if m:
            profile["registered"] = m.group(1)[:10]
        m = re.search(r'"companies":\{"pagesCount":\d+,"companyIds":\[([^\]]*)\]', html)
        if m and m.group(1).strip():
            ids = re.findall(r'"([^"]+)"', m.group(1))
            if ids:
                profile["habr_companies"] = ids
        return profile

    @staticmethod
    def extract_contact(contacts: list[dict], title: str) -> dict | None:
        for contact in contacts:
            if contact.get("title") == title:
                return contact
        return None

    @staticmethod
    def extract_github(contacts: list[dict]) -> str | None:
        contact = HabrAdapter.extract_contact(contacts, "GitHub")
        if not contact:
            return None
        m = re.search(r"github\.com/([A-Za-z0-9_\-]+)", contact.get("url") or "")
        return m.group(1) if m else None

    @staticmethod
    def extract_telegram(contacts: list[dict]) -> str | None:
        contact = HabrAdapter.extract_contact(contacts, "Telegram")
        if not contact:
            return None
        value = contact.get("value") or ""
        if value:
            return value.lstrip("@")
        m = re.search(r"telegram\.me/([A-Za-z0-9_]{4,})", contact.get("url") or "")
        return m.group(1) if m else None

    # ------------------------------------------------------------ crawl API

    def collect_authors(self, pages: int) -> list[str]:
        """Логины авторов со страниц хабов (порт habr_crawl:119-149)."""
        authors: list[str] = []
        for hub in self.hubs:
            for page in range(1, pages + 1):
                html = self.hub_page(hub, page)
                if html is None:
                    print(f"  список {hub} стр.{page}: 404, стоп по хабу")
                    break
                found = set(AUTHOR_RE.findall(html))
                authors.extend(found)
                print(f"  {hub} стр.{page}: +{len(found)} авторов")
                time.sleep(self.list_delay)
        seen: set[str] = set()
        unique: list[str] = []
        for author in authors:
            if author not in seen:
                seen.add(author)
                unique.append(author)
        return unique

    def crawl(
        self,
        store: JsonlStore,
        *,
        pages: int = 5,
        dry: bool = False,
    ) -> int:
        """Краул хабов → записи в стор (порт main habr_crawl). Возврат: записей."""
        authors = self.collect_authors(pages)
        done = store.keys()
        todo = [a for a in authors if a not in done]
        print(f"авторов к проверке: {len(todo)} (уже было {len(done)})")
        results: list[dict[str, Any]] = []
        for login in todo:
            html = self.profile_html(login)
            if html is None:
                continue
            profile = self.parse_profile(html, login)
            if not profile:
                continue
            contacts = profile.get("contacts") or []
            github = self.extract_github(contacts)
            telegram = self.extract_telegram(contacts)
            site = self.extract_contact(contacts, "Сайт")
            results.append(
                touch_checked_at(
                    {
                        "login": login,
                        "fullname": profile.get("fullname"),
                        "contacts": contacts,
                        "github_login": github,
                        "telegram": telegram,
                        "site": (site or {}).get("url"),
                        "habr_url": f"{HABR_BASE}/ru/users/{login}/",
                        "hubs": list(self.hubs),
                    }
                )
            )
            time.sleep(self.profile_delay)
        if dry or not results:
            return 0
        written = store.upsert_many(results)
        print(f"crawl: записано {written}, всего {len(store.load())}")
        return written

    def enrich(self, store: JsonlStore, *, dry: bool = False) -> int:
        """Дозаполнение enrich-полей существующих записей (порт
        habr_crawl.enrich_records)."""
        records = store.load()
        todo = [r for r in records if not all(f in r for f in ENRICH_FIELDS)]
        print(f"enrich: {len(records)} записей, к обходу {len(todo)}")
        results: list[dict[str, Any]] = []
        for record in todo:
            login = str(record["login"])
            html = self.profile_html(login)
            if html is None:
                continue
            profile = self.parse_profile(html, login)
            if not profile:
                continue
            merged = dict(record)
            for field in ENRICH_FIELDS:
                if field in profile:
                    merged[field] = profile[field]
            results.append(touch_checked_at(merged))
            time.sleep(self.profile_delay)
        if dry or not results:
            return 0
        written = store.upsert_many(results)
        print(f"enrich: обновлено {written}")
        return written

    # ------------------------------------------------------------ match API

    def match_handles(
        self,
        people_store: JsonlStore,
        matches_store: JsonlStore,
        *,
        limit: int = 0,
        full: bool = False,
        dry: bool = False,
    ) -> int:
        """Хэндл-матч GH↔Habr (порт habr_match.main). Ключ — github_login."""
        people = people_store.load()
        if full:
            people = [p for p in people if p.get("github_login")]
            checked = matches_store.keys()
            todo = [p for p in people if str(p["github_login"]) not in checked]
        else:
            people = people[: limit or 20]
            todo = people
        print(f"match: к проверке {len(todo)} логинов")
        results: list[dict[str, Any]] = []
        for person in todo:
            login = str(person.get("github_login") or "")
            if not login:
                continue
            html = self.profile_html(login)
            if html is None:
                continue
            profile = self.parse_profile(html, login)
            if not profile:
                continue
            telegram = self.extract_telegram(profile.get("contacts") or [])
            results.append(
                touch_checked_at(
                    {
                        "github_login": login,
                        "group": person.get("group"),
                        "github_org": person.get("github_org"),
                        "email": person.get("email"),
                        "email_confidence": person.get("email_confidence"),
                        "habr_login": profile["login"],
                        "habr_fullname": profile.get("fullname"),
                        "habr_contacts": profile.get("contacts"),
                        "telegram": telegram,
                        "habr_url": f"{HABR_BASE}/ru/users/{login}/",
                        "stack_match": person.get("stack_match"),
                    }
                )
            )
            time.sleep(self.profile_delay)
        if dry or not results:
            return 0
        written = matches_store.upsert_many(results)
        print(f"match: записано {written}")
        return written

    # ------------------------------------------------------------ bridge API

    def bridge(
        self,
        people_store: JsonlStore,
        bridge_store: JsonlStore,
        *,
        dry: bool = False,
    ) -> int:
        """Мост email→Habr→TG — ЗАКРЫТЫЙ эксперимент (порт habr_bridge).

        Гипотеза опровергнута эмпирически (0 TG из 53, АУДИТ_КАНАЛА.md):
        сохраняем шаг для воспроизводимости вердикта.
        """
        high = [
            r
            for r in people_store.load()
            if r.get("email_confidence") == "high" and r.get("email")
        ]
        done = bridge_store.keys()
        print(f"мост: {len(high)} high-персон, {len(done)} уже проверено")
        results: list[dict[str, Any]] = []
        for person in high:
            key = str(person.get("email"))
            if key in done:
                continue
            matched_login = None
            profile: dict[str, Any] | None = None
            tried: list[str] = []
            for candidate in bridge_candidates(person):
                html = self.profile_html(candidate)
                if html is None:
                    tried.append(f"{candidate}:404")
                    continue
                parsed = self.parse_profile(html, candidate)
                if parsed:
                    matched_login = candidate
                    profile = parsed
                    break
                tried.append(f"{candidate}:redir")
            telegram = (
                self.extract_telegram((profile or {}).get("contacts") or [])
                if profile
                else None
            )
            results.append(
                touch_checked_at(
                    {
                        "email": key,
                        "name": person.get("name"),
                        "group": person.get("group"),
                        "github_login": person.get("github_login"),
                        "tried": tried,
                        "habr_login": matched_login,
                        "habr_fullname": (profile or {}).get("fullname"),
                        "telegram": telegram,
                        "karma": (profile or {}).get("karma"),
                        "article_count": (profile or {}).get("article_count"),
                        "habr_companies": (profile or {}).get("habr_companies"),
                    }
                )
            )
        if dry or not results:
            return 0
        written = bridge_store.upsert_many(results)
        print(
            f"мост: записано {written}, "
            f"с TG: {sum(1 for r in results if r.get('telegram'))}"
        )
        return written


def bridge_candidates(person: dict[str, Any]) -> list[str]:
    """Кандидаты habr-логина для персоны (порт habr_bridge.candidates)."""
    out: list[str] = []
    if person.get("github_login"):
        out.append(str(person["github_login"]))
    email = str(person.get("email") or "")
    prefix = email.split("@")[0] if "@" in email else ""
    if prefix and prefix not in out:
        out.append(prefix)
    name = str(person.get("name") or "")
    if " " in name:
        last = name.split()[-1].lower()
        if last not in out:
            out.append(last)
    return out
