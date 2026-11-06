"""Ранжирование outreach: стэк-матч, персоны, тиры Habr, ML-фильтр (ПЛАН §3.4).

Порт run.py (stack_match, _score_person, ROBOT-фильтр), habr_crawl_report.py
(тир-скор) и объединение дублированных ML-фильтров hh_managers.py:200-216 =
career_managers.py:61-71 в одну копию.
"""
from __future__ import annotations

import math
import re
from typing import Any

ROBOT_MARKERS = (
    "bot", "robot", "ci", "jenkins", "github-actions", "actions-user",
    "dependabot", "renovate", "greenkeeper", "travis", "circleci", "semantic",
    "gerrit", "build", "deploy", "hook", "svc", "service",
)

ML_QUERIES = (
    "machine learning", "ml engineer", "data science", "nlp engineer",
    "ai engineer", "llm", "computer vision", "исследователь данных",
    "машинн", "ml-инженер", "ии", "нейросет", "data scientist",
)

# формы-корни для морфологии русского: «машинного», «машинное» и т.д.
# «данных» НЕ сам по себе (06.11: «решения на основе данных» в карточках
# фин-команд ловил ложноположительно) — только в фразах-компаньонах ниже
_RU_STEM_HINTS = ("машинн", "нейросет", "искусственн")

# «данные» только рядом с ML-контекстом (инженер данных/дата-инженер/
# данные для ML), не «анализ данных для бизнеса»; формы склонения учтены
_RU_DATA_PHRASES = (
    "инженер данных", "дата-инженер", "data-инженер",
    "инженеру данных", "инженера данных",
    "данные для машинного", "обработка данных", "обработкой данных",
    "дата-саентист", "дата-сайентист", "дата-сайенс",
    "большие данные", "больших данных", "big data",
)


def _words(text: str | None) -> list[str]:
    """Токены для стэк-матча: регресс «ai в detail» — только целые слова
    (порт run.py:461-465, фикс от 03.11)."""
    return re.findall(r"[a-zа-яё0-9+#.]+", (text or "").lower())


def stack_match(haystack: str | None, needles: list[str]) -> tuple[float, list[str]]:
    """Доля needle-слов, найденных в haystack как целые токены/подстроки
    численно-точных форм. Возврат: (0..1, совпавшие)."""
    if not needles:
        return 0.0, []
    tokens = set(_words(haystack))
    blob = " ".join(tokens)
    matched = [n for n in needles if n.lower() in tokens or n.lower() in blob.split()]
    if not matched:
        # мягкий fallback: вхождение подстроки в слитный blob только для
        # многосложных термов (напр. "machine learning")
        matched = [
            n for n in needles if len(n.split()) > 1 and n.lower() in (haystack or "").lower()
        ]
    return (len(matched) / len(needles), matched)


def is_robot(login: str | None, email: str | None = None) -> bool:
    """Робо-аккаунты в коммитах (порт run.py:424-435 = habr_gh_enrich:33-35).

    06.11 P1-фикс: подстрочный матч ловил живых людей — «ci» матчит
    social/lucifer/practical, «bot» — rabbit. Теперь короткие маркеры
    (<=3 символов: ci/svc/co/bot при len==3) — только точное совпадение
    токена логина, длинные — по границе слова.
    """
    login = (login or "").lower()
    email = (email or "").lower()
    for marker in ROBOT_MARKERS:
        if len(marker) <= 3:
            # точное совпадение целого логина/префикса с разделителем
            if login == marker or login.startswith(marker + "-") or login.startswith(marker + "_"):
                return True
            local = email.split("@", 1)[0]
            if local == marker or local.startswith(marker + "-") or local.startswith(marker + "."):
                return True
        else:
            pattern = rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])"
            if re.search(pattern, login) or re.search(pattern, email):
                return True
    return False


def habr_tier_score(record: dict[str, Any]) -> int:
    """Композитный скор Habr-автора 0..110 (порт habr_crawl_report.py:84-109).

    karma(≤30, sqrt) + статьи(≤20) + свежесть(≤20) + компании(≤30) +
    ML-специальность(10).
    """
    karma = float(record.get("karma", 0) or 0)
    articles = int(record.get("article_count", 0) or 0)
    speciality = str(record.get("speciality", "") or "").lower()
    companies = record.get("habr_companies", []) or []
    fresh_days = record.get("fresh_days")
    score = min(30.0, math.sqrt(max(0.0, karma)) * 3.0)
    score += min(20, articles)
    if any("ml" in c or "машинн" in c for c in [speciality]):
        score += 10
    score += min(30, len(companies) * 15)
    if fresh_days is not None:
        try:
            score += 20 * max(0.0, 1.0 - float(fresh_days) / 365.0)
        except (TypeError, ValueError):
            pass
    return int(score)


def ml_vacancy_filter(
    name: str | None,
    description: str | None = "",
    *,
    keywords: list[str] | None = None,
) -> bool:
    """Подходит ли вакансия под роль: токен-матч названия+описания
    (объединение двух копий research: hh_managers.py:200-216 =
    career_managers.py:61-71).

    keywords: словарь роли из [outreach].role_keywords (06.11: роли
    конфигурируемы — QA/фронт/DevOps подставляют свой словарь, дефолт
    ML_QUERIES + фразы ниже). Многословные ключи — подстрокой, однословные
    — целым токеном (регресс «ai в detail»).

    06.11: стем «данных» убран (ложноположительно на «решения на основе
    данных» — карьер-карточки Авито) — «данные» ловятся только фразами
    _RU_DATA_PHRASES и парой «больш* данн*» (любой падеж). Это верно
    только для ML-роли; кастомные keywords полностью заменяют словарь.
    """
    blob = f"{name or ''} {description or ''}".lower()
    tokens = set(_words(blob))
    if keywords:
        # кастомная роль (QA/фронт/…): только словарь владельца.
        # Русские однословные ключи — подстрокой («тестировщик» ловит
        # «тестировщику/тестировщика»); латинские — целым токеном
        # (регресс «ai в detail»).
        for raw in keywords:
            query = str(raw).lower().strip()
            if not query:
                continue
            if " " in query:
                if query in blob:
                    return True
            elif not query.isascii():
                if query in blob:
                    return True
            elif query in tokens:
                return True
        return False
    queries = ML_QUERIES
    for query in queries:
        if " " in query:
            if query in blob:
                return True
        elif query in tokens:
            return True
    for stem in _RU_STEM_HINTS:
        if stem in blob:
            return True
    for phrase in _RU_DATA_PHRASES:
        if phrase in blob:
            return True
    if _BIG_DATA_RE.search(blob):
        return True
    return False


# «большими данными/больших данных/…» — любой падеж одним regex
_BIG_DATA_RE = re.compile(r"больш\w*\s+данн\w*|big\s+data", re.I)
