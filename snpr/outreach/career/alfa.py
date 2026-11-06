"""Адаптер Alfa-Bank: открытый JSON API job.alfabank.ru (порт 4А).

«Прямой прорыв» исследования: карточка вакансии содержит contactFullName/
contactEmail/contactPhone — найм-менеджер без скрейпинга.
"""
from __future__ import annotations

import time
from typing import Any

from ..http import HttpClient
from ..rank import ml_vacancy_filter
from ..store import JsonlStore, touch_checked_at

API_BASE = "https://job.alfabank.ru/api/vacancies"
REFERER = "https://job.alfabank.ru/vacancies"
PAGE_SIZE = 100
MAX_PAGES = 12
COMPANY = "Альфа-Банк"


class AlfaAdapter:
    def __init__(
        self,
        client: HttpClient,
        *,
        request_delay: float = 0.4,
        page_delay: float = 0.5,
        keywords: list[str] | None = None,
    ) -> None:
        self.client = client
        self.request_delay = request_delay
        self.page_delay = page_delay
        # 06.11: роль конфигурируема ([outreach].role_keywords; None = ML)
        self.keywords = [k.lower() for k in keywords] if keywords else None

    def discover(self) -> list[dict[str, str]]:
        return [{"company": COMPANY, "career_domain": "job.alfabank.ru"}]

    def fetch_vacancies(
        self, target: dict[str, str], store: JsonlStore | None = None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        ml_vacancies: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(MAX_PAGES):
            data = self.client.get_json(
                f"{API_BASE}?take={PAGE_SIZE}&skip={page * PAGE_SIZE}",
                headers={"Referer": REFERER},
            )
            if not isinstance(data, dict):
                print(f"  альфа: стр.{page} — нет данных, стоп")
                break
            total = data.get("total")
            for vacancy in data.get("items", []):
                if ml_vacancy_filter(
                    str(vacancy.get("name") or ""), keywords=self.keywords
                ):
                    ml_vacancies.append(vacancy)
            if (page + 1) * PAGE_SIZE >= (total or 0):
                break
            time.sleep(self.page_delay)
        print(f"  альфа: {total} вакансий, подходящих: {len(ml_vacancies)}")
        for vacancy in ml_vacancies:
            vid = vacancy.get("id")
            full = self.client.get_json(
                f"{API_BASE}/{vid}", headers={"Referer": REFERER}
            )
            if not isinstance(full, dict):
                continue
            record = touch_checked_at(
                {
                    "source": "career_site",
                    "platform": "alfa-api",
                    "company": COMPANY,
                    "group": target.get("group") or COMPANY,
                    "vacancy_id": str(vid),
                    "vacancy_name": full.get("name") or vacancy.get("name"),
                    "manager_name": str(full.get("contactFullName") or "").strip(),
                    "manager_email": str(full.get("contactEmail") or "").strip(),
                    "manager_phones": [str(full.get("contactPhone") or "").strip()]
                    if full.get("contactPhone")
                    else [],
                    "vacancy_url": f"https://job.alfabank.ru/vacancies/{vid}",
                    "salary": None,
                    # 06.11: полное описание для LLM-скоринга (outreach score)
                    "extra": {
                        "duties": str(full.get("duties") or ""),
                        "requirements": str(full.get("requirements") or ""),
                        "conditions": str(full.get("conditions") or ""),
                        "experience": str(full.get("experience") or ""),
                        "city": str(full.get("city") or ""),
                    },
                }
            )
            if record["manager_name"] or record["manager_email"]:
                results.append(record)
            time.sleep(self.request_delay)
        return results

    def run(
        self,
        store: JsonlStore,
        *,
        targets: list[dict[str, str]] | None = None,
        dry: bool = False,
    ) -> int:
        target = (targets or [{}])[0]
        results = self.fetch_vacancies(target, store)
        if dry or not results:
            return 0
        written = store.upsert_many(results)
        print(f"alfa: записано {written}")
        return written
