"""Адаптер Ozon: career.ozon.ru (06.11, живая разведка).

ЗАКРЫТ: 307-петля анти-бота под curl/WinHTTP/headless-chrome, сайт —
заглушка «Скоро что-то будет» (портал переделывается). Адаптер-заглушка
с честной деградацией: `run()` возвращает 0 и печатает вердикт, чтобы
конвейер не падал и канал можно было включить позже одним флагом.

Проверять доступность: `python -m snpr outreach career --adapter ozon`
после анонса нового портала Ozon.
"""
from __future__ import annotations

from typing import Any

from ..http import HttpClient
from ..store import JsonlStore

BASE = "https://career.ozon.ru"
COMPANY = "Ozon"


class OzonAdapter:
    """Заглушка: канал закрыт анти-ботом (разведка 06.11)."""

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
        # лёгкая проба: вдруг портал ожил
        try:
            html = self.client.get_text(BASE)
        except Exception:  # noqa: BLE001 — тихая деградация
            html = None
        if html and "Скоро что-то будет" not in html and len(html) > 20000:
            # портал ожил — данных всё равно нет парсера, честно сообщаем
            print(
                "ozon: портал отвечает, но парсера нет — нужен реверс "
                "нового карьер-сайта (см. разведку)"
            )
            return 0
        print(
            "ozon: канал закрыт (анти-бот 307-петля, сайт-заглушка; "
            "разведка 06.11) — пропускаю"
        )
        return 0
