from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from .scoring import ScoreResult

# 18.10: профиль-капсула живёт в config.toml [letter].profile
# (другой человек меняет факты о себе одним файлом; числа из профиля
# автоматически становятся разрешёнными в гварде письма через
# extract_numbers). Ниже — ПРИМЕР-ДЕФОЛТ для чистой установки:
# замените под себя (см. ИНСТРУКЦИЯ_ПОЛЬЗОВАТЕЛЯ.md).
PROFILE_CAPSULE = """\
Факты кандидата (использовать ТОЛЬКО их, ничего не выдумывать и не менять числа):
- Компания «Ромашка», 5 лет, инженер по автоматизации, фактическая роль \
QA-автоматизации
- Проект «Автополигон» — фреймворк регресс-тестирования: 862 автотеста, \
87.7% покрытия, 200+ сценариев, честные статусы прогона
- Проект «Скрины» — распознавание отсканированных документов в проде: \
667 тестов, 9 типов документов
- Статьи на Хабре: habr.com/ru/articles/1000001/ и \
habr.com/ru/articles/1000002/
- Стек: Python — основной инструмент, pytest, FastAPI, SQL/PostgreSQL
- Грейд senior, лид-роли берёт в рост"""

LETTER_SYSTEM_PROMPT_TEMPLATE = """\
Ты пишешь сопроводительные письма для отклика на вакансии. Пишешь от \
лица кандидата, на русском.

Стиль (строго):
- Первый абзац — крючок: одна конкретная связь между текстом ЭТОЙ вакансии \
и практикой кандидата («текст вакансии читается как описание моей текущей \
работы», «в вакансии перечислены практически дословно вещи, которые я \
построил»). Никаких «я опытный специалист с N-летним стажем».
- Второй блок — практика: буллеты с проектами из фактов, только \
релевантные вакансии, цифры обязательно.
- Третий блок — маппинг: какие требования вакансии закрыты фактом из \
практики, коротко.
- Четвёртый блок — «Два вопроса, чтобы не тратить время друг друга»: \
вилка позиции и процесс найма (или гео/формат, если в вакансии неясно). \
Каждый вопрос инициирует ответ и экономит раунд.
- Финальная строка: «Ссылки на проекты и статьи — в резюме.»
- Ноль воды, ноль превосходных степеней, ноль «я быстро учусь». \
Только факты и цифры.
- Честные пробелы признавать одним предложением без самобичевания.
- Свою зарплатную цифру НЕ называть. Не упоминать, что письмо написано \
с помощью ИИ.
- Объём 120–220 слов.

%(profile)s"""


def letter_system_prompt(profile: str | None = None) -> str:
    return LETTER_SYSTEM_PROMPT_TEMPLATE % {
        "profile": (profile or PROFILE_CAPSULE).strip()
    }


DEFAULT_LETTER_SYSTEM_PROMPT = letter_system_prompt()

DEFAULT_LETTER_PROMPT = """\
Вакансия:
Название: %(vacancy_name)s
Работодатель: %(employer_name)s
Зарплата: %(salary_line)s
Гео: %(area_line)s

Описание вакансии:
%(vacancy_description)s

Ключевые навыки вакансии: %(key_skills)s

Скоринг кандидата на эту вакансию: score %(score)s, совпадения: \
%(matched)s, пробелы: %(missing)s.

Напиши сопроводительное письмо в заданном стиле. Только текст письма, \
без markdown, без темы, без подписи-контактов."""


class CompletionClient(Protocol):
    def complete(self, message: str, **kwargs: Any) -> str: ...


@dataclass
class LetterDraft:
    vacancy_id: str
    text: str
    similarity: float = 0.0
    similar_to: str | None = None

    @property
    def is_unique_enough(self) -> bool:
        return self.similarity < 0.6


def _salary_line(salary: Mapping[str, Any] | None) -> str:
    if not salary or (
        salary.get("from") is None and salary.get("to") is None
    ):
        return "не указана"
    parts = [str(salary.get(k)) for k in ("from", "to") if salary.get(k) is not None]
    currency = salary.get("currency", "RUR") or "RUR"
    gross = "gross" if salary.get("gross", True) else "net"
    return f"{parts[0]}-{parts[-1]} {currency} ({gross})"


def _shingles(text: str, size: int = 3) -> set[tuple[str, ...]]:
    words = re.findall(r"[а-яёa-z0-9]+", text.lower())
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


class UniquenessChecker:
    def __init__(self, store_path: Path, history_size: int = 50):
        self.store_path = Path(store_path)
        self.history_size = history_size

    def _load_history(self) -> list[dict[str, str]]:
        if not self.store_path.exists():
            return []
        with self.store_path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def check(self, text: str) -> tuple[float, str | None]:
        current = _shingles(text)
        if not current:
            return 0.0, None
        best_similarity, best_id = 0.0, None
        for record in self._load_history()[-self.history_size :]:
            stored = _shingles(record.get("text", ""))
            if not stored:
                continue
            similarity = len(current & stored) / len(current | stored)
            if similarity > best_similarity:
                best_similarity, best_id = similarity, record.get("vacancy_id")
        return best_similarity, best_id

    def remember(self, vacancy_id: str, text: str) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "vacancy_id": vacancy_id,
            "text": text,
            "ts": int(time.time()),
        }
        with self.store_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class CoverLetterWriter:
    def __init__(
        self,
        ai_client: CompletionClient,
        uniqueness: UniquenessChecker | None = None,
        system_prompt: str | None = None,
        prompt: str | None = None,
        profile: str | None = None,
    ):
        self.ai = ai_client
        self.uniqueness = uniqueness
        self.system_prompt = (
            system_prompt
            if system_prompt is not None
            else letter_system_prompt(profile)
        )
        self.prompt = prompt or DEFAULT_LETTER_PROMPT

    def write(
        self,
        vacancy: Mapping[str, Any],
        description: str = "",
        score: ScoreResult | None = None,
    ) -> LetterDraft:
        vacancy_id = str(vacancy.get("id") or "")
        placeholders = {
            "vacancy_name": str(vacancy.get("name") or ""),
            "employer_name": str(
                (vacancy.get("employer") or {}).get("name") or ""
            ),
            "salary_line": _salary_line(vacancy.get("salary")),
            "area_line": str((vacancy.get("area") or {}).get("name") or ""),
            "vacancy_description": description,
            "key_skills": ", ".join(
                str(s.get("name"))
                for s in vacancy.get("key_skills") or []
                if s.get("name")
            ),
            "score": str(score.score) if score else "нет",
            "matched": ", ".join(score.matched) if score else "нет",
            "missing": ", ".join(score.missing) if score else "нет",
        }
        message = self.prompt % placeholders
        text = self.ai.complete(
            message, system_prompt=self.system_prompt
        ).strip()

        similarity, similar_to = 0.0, None
        if self.uniqueness:
            similarity, similar_to = self.uniqueness.check(text)

        return LetterDraft(
            vacancy_id=vacancy_id,
            text=text,
            similarity=similarity,
            similar_to=similar_to,
        )

    def write_and_remember(
        self,
        vacancy: Mapping[str, Any],
        description: str = "",
        score: ScoreResult | None = None,
    ) -> LetterDraft:
        draft = self.write(vacancy, description, score)
        if self.uniqueness:
            self.uniqueness.remember(draft.vacancy_id, draft.text)
        return draft


# ПРИМЕР-ДЕФОЛТ для чистой установки: замените имя, проекты и ссылки
# под себя в config.toml [letter].static_text (см. ИНСТРУКЦИЯ).
STATIC_LETTER_TEXT = """\
Добрый день!
Меня зовут Иван Примеров - ваша вакансия выглядит для меня очень интересной
Я занимаюсь автоматизацией и внедрением ИИ в прод, готовые - прикладные проекты, которые необходимы

У меня есть 2 проекта, полностью функциональные, выведенные в прод и используемые людьми
https://github.com/example-user/demo-project - Автоматическая система, краткое описание результата
https://habr.com/ru/articles/1000001/ - статья на Хабре

https://habr.com/ru/articles/1000002/ - Система автоматической обработки
7 Блоков для QA
8 Блоков для Аналитиков

От ревью аналитики, до формирования автотестов

И это письмо отправлено вам вместе с моим откликом, так же через мой проект - hh-snpr
В эпоху возможностей, давайте ими пользоваться вместе, я определенно что-то умею)"""


class StaticLetterWriter:
    """Фиксированный конверт: письмо НЕ подгоняется под вакансию.

    Стратегия 16.10: анализ (гейты + скоринг) решает, КОГО бить; текст
    одинаков. Description вакансии не попадает в генерацию письма —
    промпт-инъекция физически не может изменить текст. CoverLetterWriter
    (LLM-конверт) остаётся в коде как спящий путь.
    """

    def __init__(self, text: str = STATIC_LETTER_TEXT):
        self.text = text

    def write(
        self,
        vacancy: Mapping[str, Any],
        description: str = "",
        score: ScoreResult | None = None,
    ) -> LetterDraft:
        return LetterDraft(
            vacancy_id=str(vacancy.get("id") or ""),
            text=self.text,
        )
