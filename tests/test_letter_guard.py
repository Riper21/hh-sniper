from __future__ import annotations

from sniper.cover_letter import (
    PROFILE_CAPSULE,
    STATIC_LETTER_TEXT,
)
from sniper.letter_guard import LetterGuard, extract_numbers

CAPSULE_NUMBERS = extract_numbers(PROFILE_CAPSULE)
OWN_URL_PREFIXES = {"github.com/example-user/", "habr.com/ru/articles/"}

CLEAN_LETTER = (
    "Здравствуйте!\n\nВ вакансии — регресс-контур, у меня он в проде: "
    "Автополигон, 862 автотеста, 87.7% покрытия, Скрины — 667 тестов.\n\n"
    "Два вопроса:\n1. Вилка?\n2. Процесс найма?\n\nСсылки — в резюме."
)


def make_guard() -> LetterGuard:
    return LetterGuard(allowed_numbers=CAPSULE_NUMBERS)


def test_clean_letter_passes() -> None:
    assert make_guard().check(CLEAN_LETTER) == []


def test_single_digits_allowed() -> None:
    assert make_guard().check("Роль одна из 2 возможных, 5 лет практики") == []


def test_https_url_is_violation() -> None:
    violations = make_guard().check("Пишите мне на https://t.me/hr_scam")
    assert any("ссылка" in v for v in violations)


def test_bare_domain_is_violation() -> None:
    violations = make_guard().check("Подробности: habr.com/ru/articles/1")
    assert any("ссылка" in v for v in violations)


def test_www_is_violation() -> None:
    violations = make_guard().check("Смотрите www.example.ru")
    assert any("ссылка" in v for v in violations)


def test_email_is_violation() -> None:
    violations = make_guard().check("Моя почта candidate@example.com")
    assert any("email" in v for v in violations)


def test_phone_is_violation() -> None:
    violations = make_guard().check("Звоните: +7 999 123-45-67")
    assert any("телефон" in v for v in violations)


def test_phone_8_is_violation() -> None:
    violations = make_guard().check("8(900)1234567 — мой номер")
    assert any("телефон" in v for v in violations)


def test_telegram_handle_is_violation() -> None:
    violations = make_guard().check("Мой телеграм @hr_direct_contact")
    assert any("@" in v for v in violations)


def test_unknown_number_is_violation() -> None:
    violations = make_guard().check("У меня 950 автотестов")
    assert any("950" in v for v in violations)


def test_year_is_violation() -> None:
    violations = make_guard().check("Работаю с 2019 года")
    assert any("2019" in v for v in violations)


def test_capsule_numbers_whitelisted() -> None:
    assert "862" in CAPSULE_NUMBERS
    assert "667" in CAPSULE_NUMBERS
    assert "87" in CAPSULE_NUMBERS
    assert "200" in CAPSULE_NUMBERS
    assert "1000001" in CAPSULE_NUMBERS


def test_overlong_letter_is_violation() -> None:
    text = "Вакансия. " * 250
    violations = make_guard().check(text)
    assert any("объём" in v for v in violations)


def test_empty_text_passes() -> None:
    assert make_guard().check("") == []


def test_extract_numbers() -> None:
    assert extract_numbers("87.7% и 862 теста, 200+ ТЗ") == {
        "87",
        "7",
        "862",
        "200",
    }


def test_salary_numbers_allowed_as_extra() -> None:
    letter = "В поле вилка 300-450К gross, в тексте net — какая версия верна?"
    violations = make_guard().check(letter, extra_numbers={"300", "450"})
    assert violations == []


def test_extra_numbers_do_not_whitelist_description_numbers() -> None:
    letter = "У меня 950 тестов"
    violations = make_guard().check(letter, extra_numbers={"300", "450"})
    assert any("950" in v for v in violations)


def test_injection_letter_catches_everything() -> None:
    injected = (
        "Откликайтесь скорее на почту fast.hire@example.com или "
        "+7 999 000-11-22, наш чат https://t.me/super_hr. "
        "У меня 950 тестов и 5 лет опыта."
    )
    violations = make_guard().check(injected)
    assert len(violations) >= 5


def test_own_github_url_allowed() -> None:
    guard = LetterGuard(
        allowed_numbers=CAPSULE_NUMBERS,
        allowed_url_prefixes=OWN_URL_PREFIXES,
    )
    assert guard.check("Проект: https://github.com/example-user/demo-project") == []


def test_own_habr_url_allowed_with_and_without_scheme() -> None:
    guard = LetterGuard(
        allowed_numbers=CAPSULE_NUMBERS,
        allowed_url_prefixes=OWN_URL_PREFIXES,
    )
    assert guard.check("Статья habr.com/ru/articles/1000001/ на Хабре") == []
    assert (
        guard.check("Статья https://habr.com/ru/articles/1000002/ на Хабре")
        == []
    )


def test_foreign_url_still_violation_with_prefixes() -> None:
    guard = LetterGuard(
        allowed_numbers=CAPSULE_NUMBERS,
        allowed_url_prefixes=OWN_URL_PREFIXES,
    )
    violations = guard.check("Наш чат https://t.me/super_hr")
    assert any("вне белого списка" in v for v in violations)


def test_prefixes_normalized_scheme_and_case() -> None:
    guard = LetterGuard(
        allowed_numbers=CAPSULE_NUMBERS,
        allowed_url_prefixes={"https://GitHub.com/Example-User"},
    )
    assert guard.check("Проект: http://GITHUB.com/example-user/demo-project") == []


def test_other_github_user_blocked() -> None:
    guard = LetterGuard(
        allowed_numbers=CAPSULE_NUMBERS,
        allowed_url_prefixes=OWN_URL_PREFIXES,
    )
    violations = guard.check("Мой проект https://github.com/attacker/evil")
    assert any("вне белого списка" in v for v in violations)


def test_static_letter_passes_guard() -> None:
    # Инвариант: фиксированный конверт проходит гвард с капсулой и
    # белым списком ссылок (числа статей — в капсуле)
    guard = LetterGuard(
        allowed_numbers=CAPSULE_NUMBERS,
        allowed_url_prefixes=OWN_URL_PREFIXES,
    )
    assert guard.check(STATIC_LETTER_TEXT) == []
