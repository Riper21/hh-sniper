"""Прототип адаптера карьер-сайта (ПЛАН §4.3)."""
from __future__ import annotations

from typing import Any, Protocol

from ..store import JsonlStore


class CareerAdapter(Protocol):
    """Единый контракт карьер-канала: discover → fetch_vacancies → run.

    ManagerRecord (схема career_managers.jsonl исследования, без изменений):
    source, platform, company, group, vacancy_id, vacancy_name,
    manager_name, manager_email, manager_phones[], vacancy_url, salary.
    """

    def discover(self) -> list[dict[str, str]]:
        """Домены/таргеты канала: [{company, career_domain}, …]."""
        ...

    def fetch_vacancies(
        self, target: dict[str, str], store: JsonlStore
    ) -> list[dict[str, Any]]:
        """ML-вакансии таргета → записи с контактами менеджера."""
        ...

    def run(
        self,
        store: JsonlStore,
        *,
        targets: list[dict[str, str]] | None = None,
        dry: bool = False,
    ) -> int:
        """Прогон канала: discover (если таргеты не даны) → fetch → upsert."""
        ...
