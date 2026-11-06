"""Адаптер HuntFlow (*.huntflow.io, Nuxt SSR) — порт 4А.

Контакт рекрутера — email в тексте карточки вакансии.
"""
from __future__ import annotations

import re
import time
from typing import Any

from ..http import HttpClient
from ..rank import ml_vacancy_filter
from ..store import JsonlStore, touch_checked_at

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+")
VACANCY_LINK_RE = re.compile(r'href="(/vacancy/[a-z0-9\-]+)"')
RU_NAME_RE = re.compile(r"([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\s*$")

GENERIC_PREFIXES = ("info@", "support@", "sales@", "admin@")


class HuntflowAdapter:
    def __init__(
        self,
        client: HttpClient,
        *,
        request_delay: float = 0.5,
        keywords: list[str] | None = None,
    ) -> None:
        self.client = client
        self.request_delay = request_delay
        # 06.11: роль конфигурируема ([outreach].role_keywords; None = ML)
        self.keywords = [k.lower() for k in keywords] if keywords else None

    def discover(self) -> list[dict[str, str]]:
        """HuntFlow-таргеты — из справочника jobtoo (в research были в коде)."""
        return []

    def fetch_vacancies(
        self, target: dict[str, str], store: JsonlStore | None = None
    ) -> list[dict[str, Any]]:
        domain = str(target.get("career_domain") or "")
        company = str(target.get("company") or domain)
        if not domain:
            return []
        base = f"https://{domain}"
        body = self.client.get_text(f"{base}/vacancy")
        if body is None:
            print(f"  {company}: список недоступен")
            return []
        slugs = list(dict.fromkeys(VACANCY_LINK_RE.findall(body)))
        ml_slugs: list[tuple[str, str]] = []
        for slug in slugs:
            anchor = re.search(rf'href="{re.escape(slug)}"[^>]*>([^<]+)</a>', body)
            title = anchor.group(1) if anchor else ""
            if ml_vacancy_filter(
                title, keywords=self.keywords
            ) or ml_vacancy_filter(
                slug.replace("-", " "), keywords=self.keywords
            ):
                ml_slugs.append((slug, title or slug))
        print(
            f"  {company} ({domain}): {len(slugs)} вакансий, "
            f"подходящих: {len(ml_slugs)}"
        )
        results: list[dict[str, Any]] = []
        for slug, title in ml_slugs:
            card = self.client.get_text(f"{base}{slug}")
            if card is None:
                continue
            text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", card, flags=re.S)
            text = re.sub(r"<[^>]+>", " ", text)
            emails = [
                email
                for email in sorted(set(EMAIL_RE.findall(text)))
                if "huntflow" not in email.lower()
                and not email.lower().startswith(GENERIC_PREFIXES)
            ]
            if not emails:
                continue
            # имя рекрутера: текст перед email (паттерн «пиши <Имя> <email>»)
            name = ""
            for email in emails:
                idx = text.find(email)
                fragment = text[max(0, idx - 120) : idx]
                m = RU_NAME_RE.search(fragment.strip())
                if m:
                    name = m.group(1)
                    break
            results.append(
                touch_checked_at(
                    {
                        "source": "career_site",
                        "platform": "huntflow",
                        "company": company,
                        "group": target.get("group") or company,
                        "vacancy_id": slug,
                        "vacancy_name": title,
                        "manager_name": name,
                        "manager_email": emails[0],
                        "manager_phones": [],
                        "vacancy_url": f"{base}{slug}",
                        "salary": None,
                    }
                )
            )
            time.sleep(self.request_delay)
        return results

    def run(
        self,
        store: JsonlStore,
        *,
        targets: list[dict[str, str]] | None = None,
        dry: bool = False,
    ) -> int:
        results: list[dict[str, Any]] = []
        for target in targets or []:
            results.extend(self.fetch_vacancies(target, store))
        if dry or not results:
            return 0
        written = store.upsert_many(results)
        print(f"huntflow: записано {written}")
        return written
