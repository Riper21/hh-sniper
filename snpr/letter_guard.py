from __future__ import annotations

import re

MAX_LETTER_LENGTH = 2000

# Ссылки: явные http(s)://, www., и голые домены (habr.com/..., t.me/...)
URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)\S+"
    r"|\b[a-z0-9][a-z0-9-]{0,30}\.(?:ru|com|io|ai|org|net|dev|me|tech|рф)\b\S*"
)
EMAIL_RE = re.compile(
    r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b"
)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)[\s(-]?\d{3}[\s)-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)"
)
TG_RE = re.compile(r"(?<![а-яёa-z0-9_])@[a-zа-яё0-9_]{3,}(?![а-яёa-z0-9_])")
NUM_RE = re.compile(r"\d+")


def extract_numbers(text: str) -> set[str]:
    return set(NUM_RE.findall(text))


def _normalize_url(url: str) -> str:
    value = url.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value


class LetterGuard:
    """Пост-валидация письма перед пуском.

    Description вакансии — атакующий текст на входе LLM: письмо (в т.ч.
    статическое, после ручной правки) проверяется на следы инъекции
    (чужие ссылки, контакты, числа вне капсулы). Свои ссылки (github,
    habr) — через белый список префиксов. Нарушения не блокируют пуск,
    а переводят его на ручное решение.
    """

    def __init__(
        self,
        allowed_numbers: set[str] | None = None,
        allowed_url_prefixes: set[str] | None = None,
        max_length: int = MAX_LETTER_LENGTH,
    ):
        self.allowed_numbers = allowed_numbers or set()
        self.allowed_url_prefixes = {
            _normalize_url(prefix).rstrip("/") + "/"
            for prefix in (allowed_url_prefixes or set())
        }
        self.max_length = max_length

    def _url_violation(self, url: str) -> str | None:
        normalized = _normalize_url(url)
        for prefix in self.allowed_url_prefixes:
            if normalized.startswith(prefix):
                return None
        return f"ссылка вне белого списка: {url[:60]}"

    def check(
        self, text: str, extra_numbers: set[str] | None = None
    ) -> list[str]:
        """extra_numbers — допустимые числа этой вакансии (вилка).

        Числа из структурированного поля salary (не свободный текст) легитимны
        в вопросах про вилку: «в поле 300-450К gross, в тексте net — что верно?».
        Числа из description вакансии в whitelist НЕ попадают.
        """
        if not text:
            return []
        allowed = self.allowed_numbers | (extra_numbers or set())
        violations: list[str] = []
        if len(text) > self.max_length:
            violations.append(
                f"объём {len(text)} знаков > {self.max_length}"
            )
        allowed_url_spans: list[tuple[int, int]] = []
        for match in URL_RE.finditer(text):
            violation = self._url_violation(match.group(0))
            if violation:
                violations.append(violation)
            else:
                allowed_url_spans.append((match.start(), match.end()))
        for match in EMAIL_RE.finditer(text):
            violations.append(f"email: {match.group(0)[:60]}")
        for match in PHONE_RE.finditer(text):
            violations.append(f"телефон: {match.group(0)[:60]}")
        for match in TG_RE.finditer(text):
            fragment = match.group(0)
            if "@" in fragment and EMAIL_RE.search(fragment) is None:
                violations.append(f"контакт @: {fragment[:60]}")
        for match in NUM_RE.finditer(text):
            token = match.group(0)
            if len(token) < 2 or token in allowed:
                continue
            # числа внутри разрешённых ссылок (github.com/example-user) — ок
            if any(
                start <= match.start() < end for start, end in allowed_url_spans
            ):
                continue
            violations.append(f"число вне капсулы: {token}")
        return violations
