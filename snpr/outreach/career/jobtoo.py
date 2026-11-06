"""Справочник карьер-доменов из бандлов JobToo (порт jobtoo_extract.py).

Эмпирика 06.11 (живая проба): jobtoo переупаковал фронт — новый entry-чанк
справочник (массив [название, домен]) больше НЕ содержит; данные лежат в
СТАРЫХ чанках (entry-* прошлых релизов живы годами) и в __common-*.
Поэтому дискаври сканирует ВСЕ чанки главной и все известные шаблоны,
пока не найдёт справочник; при полном провале — «директория недоступна».
"""
from __future__ import annotations

import re
from typing import Any

from ..http import HttpClient
from ..store import JsonlStore

JOBTOO_HOME = "https://jobtoo.ru"
# известные источники справочника (живой замер 06.11: старый entry 5.6МБ
# и __common 2.7МБ оба содержат полный массив 707 пар)
KNOWN_BUNDLE_URLS = (
    "https://jobtoo.ru/_expo/static/js/web/"
    "entry-a3190a93b2a8077237e0be88acd04eb5.js",
)
PAIRS_RE = re.compile(
    r'\["((?:[^"\\]|\\.)+)","((?:[a-z0-9\-]+\.)+[a-z]{2,6})"\]'
)
SCRIPT_SRC_RE = re.compile(r'src="([^"]+\.js)"')
# порог «это справочник, а не случайные пары»: в настоящем бандле 700+ пар
MIN_PAIRS = 100


def _unesc(value: str) -> str:
    if "\\u" in value or "\\x" in value:
        try:
            return value.encode().decode("unicode_escape", errors="replace")
        except Exception:  # noqa: BLE001 — осознанный откат
            return value
    return value


def parse_bundle(js: str) -> list[dict[str, str]]:
    """Пары ["Название","домен"] из JS-бандла (порт jobtoo_extract:30-44)."""
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, domain in PAIRS_RE.findall(js):
        name = _unesc(name).strip(" ·").strip()
        if not name or domain in seen:
            continue
        seen.add(domain)
        records.append({"company": name, "career_domain": domain})
    return records


def _looks_like_directory(js: str) -> bool:
    return len(PAIRS_RE.findall(js)) >= MIN_PAIRS


def discover_bundle_url(client: HttpClient) -> str:
    """Легаси-API: URL первого кандидата. Новый код зовёт discover_directory."""
    candidates = _candidate_urls(client)
    return candidates[0] if candidates else KNOWN_BUNDLE_URLS[0]


def _candidate_urls(client: HttpClient) -> list[str]:
    """URL чанков-кандидатов: все script src главной + известные легаси."""
    urls: list[str] = []
    html = client.get_text(JOBTOO_HOME)
    if html:
        for src in SCRIPT_SRC_RE.findall(html):
            urls.append(src if src.startswith("http") else f"{JOBTOO_HOME}{src}")
    # легаси-чанки — крупные, качаются последними (только если главная
    # не отдала ничего со справочником)
    for known in KNOWN_BUNDLE_URLS:
        if known not in urls:
            urls.append(known)
    return urls


def discover_directory(client: HttpClient) -> tuple[str | None, list[dict[str, str]]]:
    """Ищет справочник по чанкам: (url с данными, записи) или (None, [])."""
    saved = client.use_cache
    client.use_cache = False
    try:
        for url in _candidate_urls(client):
            js = client.get_text(url)
            if not js:
                continue
            records = parse_bundle(js)
            if len(records) >= MIN_PAIRS:
                return url, records
    finally:
        client.use_cache = saved
    return None, []


class JobtooAdapter:
    def __init__(self, client: HttpClient) -> None:
        self.client = client

    def discover(self) -> list[dict[str, str]]:
        return []

    def fetch_vacancies(
        self, target: dict[str, str], store: JsonlStore | None = None
    ) -> list[dict[str, Any]]:
        return []

    def run(
        self,
        store: JsonlStore,
        *,
        targets: list[dict[str, str]] | None = None,
        dry: bool = False,
    ) -> int:
        """Ищет справочник в чанках jobtoo → career_sites.jsonl (рерайт).

        Возврат: число записей в справочнике (0 = директория недоступна).
        """
        url, records = discover_directory(self.client)
        if not records:
            print("jobtoo: директория недоступна (справочник не найден в чанках)")
            return 0
        print(f"jobtoo: справочник в {url.rsplit('/', 1)[-1]}, компаний {len(records)}")
        if dry:
            return 0
        # полный рерайт справочника — upsert_many не удаляет пропавших;
        # это осознанно: домены из прошлых выпусков остаются валидными
        store.upsert_many(records)
        return len(records)
