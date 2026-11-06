"""Текстовые утилиты outreach: esc/translit/similarity/normalize (ПЛАН §3.4).

Сводит шесть копий esc() из research-скриптов, translit и similarity из run.py.
normalize_company — новая общая функция для кросс-матчинга hh-employer ↔ база.
"""
from __future__ import annotations

import html
import re
import unicodedata

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def esc(value: str | None) -> str:
    """HTML-unescape + экранирование | и переносов для markdown-таблиц."""
    return (
        html.unescape(value or "")
        .replace("|", "\\|")
        .replace("\n", " ")
        .strip()
    )


def unescape(value: str | None) -> str:
    """HTML-unescape без экранирования (для данных)."""
    return html.unescape(value or "").strip()


def translit(text: str) -> str:
    """Кириллица → латиница (порт run.py:94-108, для GH-поиска оргов)."""
    out = []
    for ch in text.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
    return "".join(out)


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^a-z0-9]+", "", text.lower())
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def similarity(a: str, b: str) -> float:
    """Похожесть строк через 2-граммы (порт run.py:287-295)."""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


_LEGAL_NOISE = re.compile(
    r"\b(ооо|оао|пао|ао|зао|ип|нпо|нии|фгуп|групп|group|holding|holdings|"
    r"inc|ltd|llc|corp|corporation|company|co|ru|рф)\b",
    re.IGNORECASE,
)


def normalize_company(name: str | None) -> str:
    """Нормализация имени компании для кросс-канального матчинга.

    lower → ё→е → сброс юр-форм и «Group/Holding» → латиницa через translit
    двусторонне → только алфанумерика. Пример: «Авито Групп» и «avito» →
    «avito»; «Контур» и «kontur» → «kontur».
    """
    if not name:
        return ""
    text = name.lower().replace("ё", "е").strip()
    text = _LEGAL_NOISE.sub(" ", text)
    text = " ".join(text.split())
    # двусторонняя транслитерация: кириллица уводится в латиницу,
    # и латинские дубликаты сравниваются с кириллическими через общий вид
    return translit(unicodedata.normalize("NFKC", text))


def company_matches(hh_name: str | None, base_name: str | None) -> bool:
    """Матч hh-employer ↔ компании базы через нормализацию+похожесть.

    Два сигнала: точное совпадение нормализованных форм или (биграммы ≥ 0.55,
    либо одно имя целиком входит в другое как отдельное слово: «Ozon Tech» ↔
    «ozon», «Сбер» ↔ «ПАО Сбербанк» не пройдёт, а «Сбербанк» ↔ «sber» —
    через биграммы по префиксу).
    """
    a = normalize_company(hh_name)
    b = normalize_company(base_name)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True
    if similarity(a, b) >= 0.55:
        return True
    # пословный матч нормализованных форм
    wa = set(normalize_company_words(hh_name))
    wb = set(normalize_company_words(base_name))
    if len(wa) == 1 and wa <= wb:
        return True
    if len(wb) == 1 and wb <= wa:
        return True
    return False


def normalize_company_words(name: str | None) -> list[str]:
    """Слова нормализованной формы (для пословного матча)."""
    if not name:
        return []
    text = name.lower().replace("ё", "е").strip()
    text = _LEGAL_NOISE.sub(" ", text)
    return [translit(w) for w in text.split() if w]
