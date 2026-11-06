"""Адаптер Сбера: rabota.sber.ru (06.11, живая разведка).

Канал: /sitemap (2500 вакансий) → ML-фильтр по слагам → SSR-карточки
/search/{slug}-{id}/ с полными данными (__NEXT_DATA__.vacancy:
title/company/introduction/duties/requirements/city/salary).

Email найм-менеджера: НЕТ (отклик через сайт) — канал служит
матчинг-источником для ТРИГГЕРА (вакансии компании ↔ база контактов),
как hh (вердикт 3А): «триггер приоритизации».

Разведка 06.11: hr.sberbank.ru/api-web/cs/api/1/public/* — служебный
Alfresco-CMS (медиа/локали), вакансий там нет; SPA-поиск требует сессию
браузера. Sitemap+SSR — стабильный легальный путь.
"""
from __future__ import annotations

import re
import time
from typing import Any

from ..http import HttpClient
from ..rank import ml_vacancy_filter
from ..store import JsonlStore, touch_checked_at
from ..text import unescape

BASE = "https://rabota.sber.ru"
SITEMAP = f"{BASE}/sitemap"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)
VACANCY_LINK_RE = re.compile(r"<loc>(https://rabota\.sber\.ru/search/[^<]+)</loc>")
COMPANY = "Сбер"
GROUP = "Сбер"

# слаги ML-вакансий из sitemap: machine-learning/ml-/python/ai/... —
# первый дешёвый фильтр ДО загрузки карточек (экономия запросов)
ML_SLUG_HINTS = (
    "machine-learning", "ml-", "-ml", "python", "ai-", "-ai",
    "iskusstvenn", "neuro", "llm", "data-sci", "chatgpt", "gigachat",
)


def parse_sitemap(xml: str) -> list[str]:
    """URL вакансий из sitemap (кроме корня /search/)."""
    urls = VACANCY_LINK_RE.findall(xml)
    return [u for u in urls if u.rstrip("/") != f"{BASE}/search/"]


# роли → слаг-хинты (для [outreach].role_keywords; None = ML-дефолт)
ROLE_SLUG_HINTS: dict[str, tuple[str, ...]] = {
    "qa": ("qa", "test", "testirovshchik", "kachestv", "avtotest", "aqa"),
    "frontend": ("frontend", "front", "react", "vue", "angular"),
    "devops": ("devops", "sre", "kubernetes", "infrastruktur"),
    "backend": ("backend", "python", "java", "net-"),
}


def _slug_hints(keywords: list[str] | None) -> tuple[str, ...]:
    """Слаг-хинты под роль: кастомные keywords, пресет роли или ML."""
    if not keywords:
        return ML_SLUG_HINTS
    hints: list[str] = []
    for keyword in keywords:
        kw = keyword.lower().strip()
        if kw in ROLE_SLUG_HINTS:
            hints.extend(ROLE_SLUG_HINTS[kw])
        else:
            # латинский keyword матчит слаг напрямую; русский —
            # первые 5 символов корня (транслит-слаги длинные)
            hints.append(kw if kw.isascii() else kw[:5])
    return tuple(h for h in hints if h)


def slug_is_ml(url: str, keywords: list[str] | None = None) -> bool:
    """URL-слаг под роль (первый дешёвый фильтр)."""
    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    return any(hint in slug for hint in _slug_hints(keywords))


def parse_vacancy_card(html: str) -> dict[str, Any] | None:
    """__NEXT_DATA__ → vacancy (SSR-карточка Сбера)."""
    import json

    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    vacancy = (data.get("props", {}).get("pageProps", {}) or {}).get("vacancy")
    return vacancy if isinstance(vacancy, dict) else None


def vacancy_text(vacancy: dict[str, Any]) -> str:
    """Полный текст карточки для ML-фильтра."""
    parts = [
        str(vacancy.get("title") or ""),
        str(vacancy.get("introduction") or ""),
        str(vacancy.get("duties") or ""),
        str(vacancy.get("requirements") or ""),
        str(vacancy.get("specialization") or ""),
    ]
    return unescape(" ".join(parts))


class SberAdapter:
    def __init__(
        self,
        client: HttpClient,
        *,
        request_delay: float = 0.7,
        keywords: list[str] | None = None,
    ) -> None:
        self.client = client
        self.request_delay = request_delay
        # 06.11: роль конфигурируема ([outreach].role_keywords; None = ML)
        self.keywords = [k.lower() for k in keywords] if keywords else None

    def discover(self) -> list[dict[str, str]]:
        return [{"company": COMPANY, "career_domain": "rabota.sber.ru"}]

    def fetch_vacancies(
        self, target: dict[str, str], store: JsonlStore | None = None
    ) -> list[dict[str, Any]]:
        """Sitemap → слаги роли → карточки. Записи СХЕМЫ career_managers.jsonl
        (manager_name/email пустые — канал матчинговый, см. докстринг)."""
        xml = self.client.get_text(SITEMAP)
        if not xml:
            print("  сбер: sitemap недоступен")
            return []
        urls = parse_sitemap(xml)
        candidates = [u for u in urls if slug_is_ml(u, keywords=self.keywords)]
        print(
            f"  сбер: sitemap {len(urls)} вакансий, слагов роли {len(candidates)}"
        )
        results: list[dict[str, Any]] = []
        for url in candidates:
            html = self.client.get_text(url)
            if html is None:
                continue
            vacancy = parse_vacancy_card(html)
            if not vacancy:
                continue
            text = vacancy_text(vacancy)
            if not ml_vacancy_filter(
                str(vacancy.get("title") or ""), text, keywords=self.keywords
            ):
                continue  # слаг соврал — глубокая проверка отсеяла
            internal_id = str(vacancy.get("internalId") or "")
            if not internal_id:
                continue
            results.append(
                touch_checked_at(
                    {
                        "source": "career_site",
                        "platform": "sber-ssr",
                        "company": COMPANY,
                        "group": target.get("group") or GROUP,
                        "vacancy_id": internal_id,
                        "vacancy_name": vacancy.get("title"),
                        "manager_name": "",
                        "manager_email": "",
                        "manager_phones": [],
                        "vacancy_url": url,
                        "salary": _salary(vacancy),
                        "area": vacancy.get("city"),
                        "extra": {
                            "specialization": vacancy.get("specialization"),
                            "publication_date": vacancy.get("publicationDate"),
                            "description": text[:2000],
                        },
                    }
                )
            )
            time.sleep(self.request_delay)
        print(f"  сбер: ML-вакансий подтверждено {len(results)}")
        return results

    def run(
        self,
        store: JsonlStore,
        *,
        targets: list[dict[str, str]] | None = None,
        dry: bool = False,
    ) -> int:
        results = self.fetch_vacancies(targets[0] if targets else {})
        if dry or not results:
            return 0
        written = store.upsert_many(results)
        print(f"sber: записано {written}")
        return written


def _salary(vacancy: dict[str, Any]) -> dict[str, Any] | None:
    lo = vacancy.get("salary_min")
    hi = vacancy.get("salary_max")
    if not lo and not hi:
        return None
    return {
        "from": lo,
        "to": hi,
        "currency": "RUR",
    }
