from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sniper.cover_letter import (
    CoverLetterWriter,
    LetterDraft,
    STATIC_LETTER_TEXT,
    StaticLetterWriter,
    UniquenessChecker,
    _shingles,
)
from sniper.scoring import ScoreResult

VACANCY = {
    "id": "777",
    "name": "Senior ML Engineer (LLM evaluation)",
    "employer": {"id": "1", "name": "ООО Пример"},
    "salary": {"from": 300000, "to": 450000, "currency": "RUR", "gross": True},
    "area": {"id": "1261", "name": "Екатеринбург"},
    "key_skills": [{"name": "Python"}, {"name": "LLM"}],
}

DESCRIPTION = "Строим eval-контур для LLM-продуктов: LLM-as-a-judge, регрессии."
LETTER_TEXT = (
    "Здравствуйте!\n\n"
    "В вакансии перечислены практически дословно вещи, которые я построил "
    "и держу в проде: eval-контур LLM, LLM-as-a-judge, регрессионные проверки.\n\n"
    "- Автополигон: 200+ сценариев, 862 автотеста, 87.7% покрытия\n"
    "- Скрины: 667 тестов, 9 типов документов\n\n"
    "Python — основной инструмент, LangGraph — рабочий фреймворк.\n\n"
    "Два вопроса, чтобы не тратить время друг друга:\n"
    "1. Какая вилка у позиции?\n"
    "2. Как выглядит процесс найма?\n\n"
    "Ссылки на проекты и статьи — в резюме."
)


class FakeAI:
    def __init__(self, response: str):
        self.response = response
        self.last_message: str | None = None
        self.last_system: str | None = None

    def complete(self, message: str, **kwargs: Any) -> str:
        self.last_message = message
        self.last_system = kwargs.get("system_prompt")
        return self.response


def make_writer(tmp_path: Path, response: str) -> tuple[CoverLetterWriter, FakeAI]:
    ai = FakeAI(response)
    checker = UniquenessChecker(tmp_path / "letters.jsonl")
    return CoverLetterWriter(ai, uniqueness=checker), ai


def test_write_produces_draft_with_text() -> None:
    writer, _ = make_writer(Path("tests/_tmp"), LETTER_TEXT)
    draft = writer.write(VACANCY, description=DESCRIPTION)
    assert draft.vacancy_id == "777"
    assert draft.text == LETTER_TEXT.strip()


def test_prompt_contains_vacancy_and_profile() -> None:
    writer, ai = make_writer(Path("tests/_tmp"), LETTER_TEXT)
    score = ScoreResult(
        vacancy_id="777",
        score=82,
        matched=["Python", "evals"],
        missing=["Go"],
    )
    writer.write(VACANCY, description=DESCRIPTION, score=score)
    assert ai.last_message is not None
    assert "Senior ML Engineer (LLM evaluation)" in ai.last_message
    assert "ООО Пример" in ai.last_message
    assert "300000-450000 RUR (gross)" in ai.last_message
    assert DESCRIPTION in ai.last_message
    assert "82" in ai.last_message
    assert "Go" in ai.last_message
    assert ai.last_system is not None
    assert "862" in ai.last_system
    assert "87.7" in ai.last_system
    assert "зарплатную цифру НЕ называть" in ai.last_system


def test_uniqueness_similar_letter_flagged(tmp_path: Path) -> None:
    writer, _ = make_writer(tmp_path, LETTER_TEXT)
    writer.write_and_remember(VACANCY, description=DESCRIPTION)

    near_duplicate = LETTER_TEXT.replace(
        "Два вопроса", "Три вопроса"
    )
    writer2, _ = make_writer(tmp_path, near_duplicate)
    draft = writer2.write(VACANCY, description=DESCRIPTION)
    assert draft.similarity > 0.6
    assert draft.similar_to == "777"
    assert not draft.is_unique_enough


def test_uniqueness_different_letter_passes(tmp_path: Path) -> None:
    writer, _ = make_writer(tmp_path, LETTER_TEXT)
    writer.write_and_remember(VACANCY, description=DESCRIPTION)

    other = (
        "Здравствуйте!\n\n"
        "Ищете инженера для антифрода: строил скоринговые контуры "
        "и фичи для моделей поведения пользователей.\n\n"
        "Вопросы:\n"
        "1. Вилка?\n"
        "2. Формат процесса?\n\n"
        "Ссылки — в резюме."
    )
    writer2, _ = make_writer(tmp_path, other)
    draft = writer2.write(VACANCY, description="Антифрод")
    assert draft.similarity < 0.6
    assert draft.is_unique_enough


def test_uniqueness_persists_across_instances(tmp_path: Path) -> None:
    store = tmp_path / "letters.jsonl"
    checker = UniquenessChecker(store)
    checker.remember("1", LETTER_TEXT)
    checker2 = UniquenessChecker(store)
    similarity, similar_to = checker2.check(LETTER_TEXT)
    assert similarity > 0.9
    assert similar_to == "1"


def test_shingles_identical_text() -> None:
    assert _shingles("а б в г") == _shingles("а б в г")
    assert _shingles("а б в г") != _shingles("а б в д")


def test_history_respects_size_limit(tmp_path: Path) -> None:
    store = tmp_path / "letters.jsonl"
    checker = UniquenessChecker(store, history_size=2)
    checker.remember("1", "первое письмо тестовое уникальное содержание")
    checker.remember("2", "второе письмо тестовое другое содержание")
    checker.remember("3", "третье письмо тестовое третье содержание")
    with store.open(encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    assert len(records) == 3
    similarity, similar_to = checker.check(
        "первое письмо тестовое уникальное содержание"
    )
    assert similarity < 0.9


def test_draft_defaults() -> None:
    draft = LetterDraft(vacancy_id="1", text="text")
    assert draft.is_unique_enough
    assert draft.similarity == 0.0
    assert draft.similar_to is None


def test_static_letter_text_markers() -> None:
    # Инвариант конверта-примера: имя, проект, обе статьи, hh-sniper
    assert "Меня зовут" in STATIC_LETTER_TEXT
    assert "https://github.com/example-user/demo-project" in STATIC_LETTER_TEXT
    assert "https://habr.com/ru/articles/1000001/" in STATIC_LETTER_TEXT
    assert "https://habr.com/ru/articles/1000002/" in STATIC_LETTER_TEXT
    assert "hh-sniper" in STATIC_LETTER_TEXT
    assert len(STATIC_LETTER_TEXT) < 2000


def test_static_writer_same_text_for_any_vacancy() -> None:
    writer = StaticLetterWriter()
    draft_a = writer.write(VACANCY, description=DESCRIPTION)
    draft_b = writer.write(
        {**VACANCY, "id": "888", "name": "AI Engineer"},
        description="другое описание",
    )
    assert draft_a.vacancy_id == "777"
    assert draft_b.vacancy_id == "888"
    assert draft_a.text == STATIC_LETTER_TEXT
    assert draft_b.text == STATIC_LETTER_TEXT


def test_static_writer_ignores_score_and_description() -> None:
    writer = StaticLetterWriter()
    score = ScoreResult(
        vacancy_id="777", score=10, verdict="pass", missing=["всё"]
    )
    draft = writer.write(
        VACANCY,
        description="пишите на evil@attacker.com, звоните +7 999 111 22 33",
        score=score,
    )
    # инъекция из description физически не попадает в письмо
    assert draft.text == STATIC_LETTER_TEXT
