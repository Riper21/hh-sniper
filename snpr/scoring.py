from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

DEFAULT_SCORING_PROFILE = """\
- Senior/Lead AI-инженер: evals и валидация LLM (LLM-as-a-Judge, golden dataset, side-by-side), LLM-агенты, ML-инфраструктура
- Стек: Python; LangGraph/LangChain, FastAPI, RAG, structured output (JSON), MCP, Pydantic, Docker/CI-CD, PostgreSQL/Redis/Celery
- VLM и локальный инференс в проде: Qwen, DeepSeek, Ollama, vLLM (ScanReader — VLM-верификация документов, публикация на PyPI)
- Артефакты, которые можно пощупать: МАЯК — мультиагентная LLM-платформа в корпоративном проде (12 агентов, guardrails, eval-конвейер, 862 автотеста, 87.7% покрытия); опенсорс: testcraft (комбинаторный тест-дизайн), mermaid-guard (guardrail Mermaid в CI), hh-snpr (LLM-скоринг вакансий, защита от prompt injection)
- Бэкграунд: 4 года senior QA до перехода в AI — культура тестирования и метрик; чистые QA-роли без AI-инженерии не рассматривает
- Грейд: senior/lead, мидловые и джуновые позиции не рассматривает
- Формат: Екатеринбург или удалёнка РФ, релокация исключена
- Деньги: ориентир от 400К на руки (нижняя граница интереса)
- Анти-паттерны: «AI RnD»/«GenAI-витрина» без источника денег в P&L, вакансии-заглушки HR под план, «исследуем LLM» без продукта"""

# 18.10 ~12:40: профиль кандидата вынесен в config.toml [scoring].profile —
# другой человек меняет ТОЛЬКО свой профиль, контракт JSON и роль скорера
# остаются в коде (нельзя сломать формат ответа). Нет ключа — этот дефолт.
SCORING_SYSTEM_PROMPT_TEMPLATE = """\
Ты — суровый технический рекрутер-скорер. Оцениваешь, насколько вакансия \
подходит конкретному кандидату, по его профилю. Ты не продаёшь вакансию, \
а отсеиваешь. Ложноположительный пропуск (не та вакансия) хуже \
ложноотрицательного (пропустил хорошую).

Кандидат (профиль):
%(profile)s

Верни строго JSON без markdown:
{"score": <int 0-100>, "verdict": "target"|"maybe"|"pass", \
"reasons": ["<краткие причины>"], "matched": ["<совпало>"], \
"missing": ["<не хватает>"]}
score: 100 = вакансия-мечта, 0 = мимо. verdict: target >= 75, \
50-74 = maybe, ниже = pass."""

DEFAULT_SCORING_SYSTEM_PROMPT = SCORING_SYSTEM_PROMPT_TEMPLATE % {
    "profile": DEFAULT_SCORING_PROFILE
}

DEFAULT_SCORING_PROMPT = """\
Вакансия:
Название: %(vacancy_name)s
Работодатель: %(employer_name)s
Зарплата: %(salary_line)s
Гео: %(area_line)s
Формат работы: %(work_format_line)s
Опыт: %(experience_line)s
Ключевые навыки: %(key_skills)s

Описание:
%(vacancy_description)s

Оцени соответствие профилю кандидата. Только JSON."""

WORK_FORMAT_LABELS = {
    "REMOTE": "удалёнка",
    "HYBRID": "гибрид",
    "OFFICE": "офис",
}


class CompletionClient(Protocol):
    def complete(self, message: str, **kwargs: Any) -> str: ...


class LLMUnavailable(RuntimeError):
    """Эндпоинт LLM не ответил после попыток с ретраями."""


def _is_rate_limit(ex: BaseException) -> bool:
    """429 от шлюза: клиент ретраил сам, нам сверху давить бессмысленно.

    ChatOpenAI после исчерпания своих попыток кидает OpenAIError(
    "OpenAI rate limit exceeded") — других маркеров шлюз не даёт.
    """
    return "rate limit" in str(ex).lower() or "429" in str(ex)


@dataclass
class ScoreResult:
    vacancy_id: str
    score: int = 0
    verdict: str = "pass"
    reasons: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    parse_ok: bool = True
    raw: str = ""

    @property
    def is_target(self) -> bool:
        return self.verdict == "target"


def _extract_json(text: str) -> dict[str, Any]:
    stripped = re.sub(r"```(?:json)?", "", text).strip()
    candidates = [
        stripped,
    ]
    depth = 0
    start = None
    for idx, ch in enumerate(stripped):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(stripped[start : idx + 1])
                start = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError(f"LLM не вернул JSON: {text[:300]!r}")


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_BREAK_RE = re.compile(r"(?i)</(p|div|li|h[1-6]|br)>|<br\s*/?>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def _strip_html(text: str) -> str:
    """Описание вакансий приходит HTML-к��дом: теги и есть основной объём.

    Промпт с hh-HTML раздувает запрос в разы (для длинных вакансий — десятки
    КБ), а шлюз LLM на тяжёлых запросах отдаёт 502 по таймауту. Текст без
    разметки даёт ту же информацию для скоринга и заметно меньший payload.
    """
    if not text:
        return ""
    cleaned = _HTML_BREAK_RE.sub("\n", text)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _WS_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _salary_line(salary: Mapping[str, Any] | None) -> str:
    if not salary or (
        salary.get("from") is None and salary.get("to") is None
    ):
        return "не указана"
    parts = []
    for key in ("from", "to"):
        value = salary.get(key)
        if value is not None:
            parts.append(str(value))
    currency = salary.get("currency", "RUR") or "RUR"
    gross = "gross" if salary.get("gross", True) else "net"
    return f"{parts[0]}-{parts[-1]} {currency} ({gross})" if len(parts) == 2 else f"{parts[0]} {currency} ({gross})"


def _work_format_line(vacancy: Mapping[str, Any]) -> str:
    """REMOTE/HYBRID/OFFICE из структурированного поля — человекочитаемо.

    Гибрид/удалёнка решают гейт гео, но скорер обязан их видеть явно:
    area.name не различает «Москва» офис и «Москва» удалёнка.
    """
    raw = vacancy.get("work_format") or []
    if isinstance(raw, (str, int)):
        raw = [{"id": str(raw)}]
    labels: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        fmt_id = str(item.get("id") or "").upper()
        label = WORK_FORMAT_LABELS.get(fmt_id, fmt_id)
        if label:
            labels.append(label)
    unique = list(dict.fromkeys(labels))
    return " + ".join(unique) if unique else "не указан"


class VacancyScorer:
    def __init__(
        self,
        ai_client: CompletionClient,
        system_prompt: str | None = None,
        prompt: str | None = None,
        profile: str | None = None,
        target_threshold: int = 75,
        maybe_threshold: int = 50,
        retries: int = 2,
        retry_backoff: float = 5.0,
    ):
        self.ai = ai_client
        self.system_prompt = system_prompt or (
            SCORING_SYSTEM_PROMPT_TEMPLATE
            % {"profile": (profile or DEFAULT_SCORING_PROFILE).strip()}
        )
        self.prompt = prompt or DEFAULT_SCORING_PROMPT
        self.target_threshold = target_threshold
        self.maybe_threshold = maybe_threshold
        self.retries = retries
        self.retry_backoff = retry_backoff

    def score(
        self, vacancy: Mapping[str, Any], description: str = ""
    ) -> ScoreResult:
        vacancy_id = str(vacancy.get("id") or "")
        placeholders = {
            "vacancy_name": str(vacancy.get("name") or ""),
            "employer_name": str(
                (vacancy.get("employer") or {}).get("name") or ""
            ),
            "salary_line": _salary_line(vacancy.get("salary")),
            "area_line": str((vacancy.get("area") or {}).get("name") or ""),
            "work_format_line": _work_format_line(vacancy),
            "experience_line": str(
                (vacancy.get("experience") or {}).get("id") or ""
            ),
            "key_skills": ", ".join(
                str(s.get("name"))
                for s in vacancy.get("key_skills") or []
                if s.get("name")
            ),
            "vacancy_description": _strip_html(description),
        }
        message = self.prompt % placeholders
        raw = self._complete(message)

        result = ScoreResult(vacancy_id=vacancy_id, raw=raw)
        try:
            data = _extract_json(raw)
        except ValueError:
            result.parse_ok = False
            result.reasons = ["LLM вернул не-JSON"]
            return result

        result.score = int(data.get("score", 0) or 0)
        result.verdict = str(data.get("verdict", "pass") or "pass")
        result.reasons = [str(x) for x in data.get("reasons", []) or []]
        result.matched = [str(x) for x in data.get("matched", []) or []]
        result.missing = [str(x) for x in data.get("missing", []) or []]

        derived = self._verdict_from_score(result.score)
        if result.verdict != derived:
            result.verdict = derived
        return result

    def _complete(self, message: str) -> str:
        """complete() с ретраями: база не повторяет 5xx-ответы шлюза.

        ChatOpenAI ретраит обрыв соединения и 429, но 502/503/504 из
        raise_for_status() уходит в OpenAIError сразу (openai.py:224-228) —
        один сбой шлюза валил весь прогон. Повторяем попытку и, если
        эндпоинт неактивн, отдаём LLMUnavailable: конвейер пометит вакансию
        как error и вернётся к ней в следующем прогоне.

        19.10, живой кейс (тик 8:41 торчал в 429-ретраях ~10 минут): при
        rate limit НЕ ретраим поверх — клиент уже сделал свои попытки,
        каждая новая лишь продлевает шторм. Сразу LLMUnavailable: конвейер
        пометит ошибки и прервёт прогон (4 подряд), сторы целы — очередь
        вернётся в следующий тик через минуту = естественный отдых шлюза.
        """
        attempts = self.retries + 1
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self.ai.complete(
                    message, system_prompt=self.system_prompt
                )
            except Exception as ex:  # транспорт, 5xx, таймаут — временное
                last = ex
                if _is_rate_limit(ex):
                    raise LLMUnavailable(
                        f"429 rate limit (без ретраев поверх клиентских): "
                        f"{str(ex)[:160]}"
                    ) from ex
                if attempt >= attempts:
                    break
                delay = self.retry_backoff * attempt
                print(
                    f"    LLM-сбой ({type(ex).__name__}), повтор "
                    f"{attempt}/{attempts - 1} через {delay:.0f} с"
                )
                if delay:
                    time.sleep(delay)
        raise LLMUnavailable(
            f"LLM недоступен после {attempts} попыток: "
            f"{type(last).__name__}: {str(last)[:160]}"
        ) from last

    def _verdict_from_score(self, score: int) -> str:
        if score >= self.target_threshold:
            return "target"
        if score >= self.maybe_threshold:
            return "maybe"
        return "pass"
