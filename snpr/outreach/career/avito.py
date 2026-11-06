"""Адаптер Авито: career.avito.com (06.11, живая разведка).

Канал: /vacancies/ (SSR HTML, ~60 вакансий по отделам) → карточки
/vacancies/{department}/{id}/ (полнотекст SSR). ML-фильтр по тексту
(data-science и смежные).

Email найм-менеджера: НЕТ (отклик через форму сайта) — канал
матчинговый, как сбер/hh.

Особенность: TLS нестабилен (таймауты handshake — анти-бот), нужен
клиент с ретраями (HttpClient уже умеет).
"""
from __future__ import annotations

import re
import time
from typing import Any

from ..http import HttpClient
from ..rank import ml_vacancy_filter
from ..store import JsonlStore, touch_checked_at
from ..text import unescape

BASE = "https://career.avito.com"
LIST_URL = f"{BASE}/vacancies/"
VACANCY_LINK_RE = re.compile(r'href="(/vacancies/[a-z0-9\-]+/\d+/)"')
TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
TAGS_RE = re.compile(r"<script.*?</script>|<style.*?</style>", re.S)
MAIN_RE = re.compile(r"<main[^>]*>(.*?)</main>", re.S)
ARTICLE_RE = re.compile(r"<article[^>]*>(.*?)</article>", re.S)
COMPANY = "Авито"
GROUP = "Авито"


def parse_list(html: str) -> list[str]:
    """Ссылки вакансий из списка (полный URL)."""
    return [f"{BASE}{path}" for path in dict.fromkeys(VACANCY_LINK_RE.findall(html))]


def card_html_body(html: str) -> str:
    """Тело карточки БЕЗ навигации/футера (живой замер 06.11: полный HTML
    содержит меню направлений «data science…» — ML-фильтр ловил мусор
    в карточках менеджеров). Порядок: main > article > весь html."""
    for pattern in (MAIN_RE, ARTICLE_RE):
        match = pattern.search(html)
        if match:
            return match.group(1)
    return html


def html_to_text(html: str) -> str:
    text = TAGS_RE.sub(" ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text))


def card_text(html: str) -> str:
    return html_to_text(card_html_body(html))


def parse_title(html: str) -> str:
    match = TITLE_RE.search(html)
    if not match:
        return ""
    return html_to_text(match.group(1)).strip()


class AvitoAdapter:
    def __init__(
        self,
        client: HttpClient,
        *,
        request_delay: float = 0.8,
        keywords: list[str] | None = None,
    ) -> None:
        self.client = client
        self.request_delay = request_delay
        # 06.11: роль конфигурируема ([outreach].role_keywords; None = ML)
        self.keywords = [k.lower() for k in keywords] if keywords else None

    def discover(self) -> list[dict[str, str]]:
        return [{"company": COMPANY, "career_domain": "career.avito.com"}]

    def fetch_vacancies(
        self, target: dict[str, str], store: JsonlStore | None = None
    ) -> list[dict[str, Any]]:
        body = self.client.get_text(LIST_URL)
        if not body:
            print("  авито: список недоступен")
            return []
        urls = parse_list(body)
        print(f"  авито: вакансий в списке {len(urls)}")
        results: list[dict[str, Any]] = []
        for url in urls:
            card = self.client.get_text(url)
            if card is None:
                continue
            title = parse_title(card)
            # текст для фильтра: заголовок + начало тела карточки.
            # Полное тело с формой отклика содержит «обработка персональных
            # ДАННЫХ» — жадный стем «данных» ловил все 44 карточки (06.11).
            card_body = card_text(card)
            filter_text = f"{title} {card_body[:600]}"
            if not ml_vacancy_filter(
                title, filter_text, keywords=self.keywords
            ):
                continue
            match = re.search(r"/(\d+)/?$", url)
            vacancy_id = match.group(1) if match else url
            results.append(
                touch_checked_at(
                    {
                        "source": "career_site",
                        "platform": "avito-ssr",
                        "company": COMPANY,
                        "group": target.get("group") or GROUP,
                        "vacancy_id": vacancy_id,
                        "vacancy_name": title or url,
                        "manager_name": "",
                        "manager_email": "",
                        "manager_phones": [],
                        "vacancy_url": url,
                        "salary": None,
                        "extra": {"description": card_body[:2000]},
                    }
                )
            )
            time.sleep(self.request_delay)
        print(f"  авито: ML-вакансий {len(results)}")
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
        print(f"avito: записано {written}")
        return written
