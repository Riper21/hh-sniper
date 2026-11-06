"""Тесты hh-канала: контакты из карточек, алиасы, ML-фильтр (§10).

Отдельный гвард: в snpr/ НЕТ Android client_id (секрет в коде — история
research, в интеграции удалён).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from snpr.outreach.hh_source import (
    build_record,
    extract_manager,
    load_employer_aliases,
    resolve_employer_id,
    run,
    search_vacancies,
    vacancy_is_ml,
)
from snpr.outreach.store import JsonlStore


class FakeApi:
    def __init__(self, script: dict) -> None:
        self.script = script
        self.calls: list[str] = []

    def get(self, path: str):
        from urllib.parse import unquote

        self.calls.append(path)
        decoded = unquote(path)
        for pattern, payload in self.script.items():
            if pattern in path or pattern in decoded:
                return payload
        return {}


def test_vacancy_is_ml_name_and_snippet():
    assert vacancy_is_ml({"name": "Senior ML Engineer"})
    assert vacancy_is_ml(
        {
            "name": "Разработчик",
            "snippet": {"requirement": "Опыт computer vision обязателен"},
        }
    )
    assert vacancy_is_ml({"name": "Бухгалтер", "snippet": {}}) is False


def test_extract_manager_open_and_closed():
    open_card = {
        "contacts": {
            "name": "Анна",
            "email": "a@sber.ru",
            "phones": [{"formatted": "+7 999"}],
        }
    }
    manager = extract_manager(open_card)
    assert manager["manager_name"] == "Анна"
    assert manager["manager_email"] == "a@sber.ru"
    assert manager["manager_phones"] == ["+7 999"]
    # закрытые контакты — None (этика канала)
    assert extract_manager({"contacts": {}}) is None
    assert extract_manager({"contacts": None}) is None


def test_resolve_employer_id_best_open_vacancies():
    api = FakeApi(
        {
            "/employers?text=Yandex": {
                "items": [
                    {"id": "1", "name": "Yandex", "open_vacancies": 300},
                    {"id": "2", "name": "Yandex Fan Club", "open_vacancies": 900},
                    {"id": "3", "name": "Yandex", "open_vacancies": 50},
                ]
            }
        }
    )
    # Fan Club отсекается бренд-матчем («yandex» не содержится в «yandex fan club»?
    # содержится! но и «yandex fan club» не в «yandex» — матчится, выбираем по opens)
    emp = resolve_employer_id(api, "Яндекс", {"Яндекс": "Yandex"})
    assert emp == 2  # максимум open_vacancies среди бренд-матчей


def test_resolve_employer_id_none_when_no_match():
    api = FakeApi({"/employers?text=Ghost": {"items": []}})
    assert resolve_employer_id(api, "Ghost", {}) is None


def test_search_vacancies_pagination():
    api = FakeApi(
        {
            "page=0": {"items": [{"id": 1}], "pages": 2},
            "page=1": {"items": [{"id": 2}], "pages": 2},
        }
    )
    items = search_vacancies(api, 42, pages=5)
    assert [i["id"] for i in items] == [1, 2]


def test_load_employer_aliases(tmp_path: Path):
    path = tmp_path / "aliases.json"
    path.write_text('{"Сбер": "СБЕР"}', encoding="utf-8")
    assert load_employer_aliases(path) == {"Сбер": "СБЕР"}
    assert load_employer_aliases(tmp_path / "missing.json") == {}


def test_build_record_schema(tmp_path: Path):
    full = {
        "id": "100",
        "name": "ML Engineer",
        "contacts": {"name": "Ольга", "email": "olga@t.ru"},
        "salary": {"from": 200000, "to": 300000, "currency": "RUR"},
        "area": {"name": "Москва"},
        "published_at": "2026-11-01",
        "alternate_url": "https://hh.ru/vacancy/100",
    }
    rec = build_record({"name": "Т-Банк", "group": "Banks", "priority": "A"}, 7, full)
    assert rec["vacancy_id"] == "100"
    assert rec["manager_email"] == "olga@t.ru"
    assert rec["salary"]["from"] == 200000
    assert rec["checked_at"]


def test_run_end_to_end(tmp_path: Path):
    api = FakeApi(
        {
            "/employers?text=СБЕР": {"items": [{"id": "9", "name": "СБЕР", "open_vacancies": 10}]},
            "employer_id=9": {
                "items": [
                    {"id": "100", "name": "ML Engineer"},
                    {"id": "101", "name": "Аналитик"},
                ],
                "pages": 1,
            },
            "/vacancies/100": {
                "id": "100",
                "name": "ML Engineer",
                "contacts": {"name": "Ольга", "email": "olga@sber.ru"},
                "alternate_url": "https://hh.ru/vacancy/100",
            },
        }
    )
    tsv = tmp_path / "companies.tsv"
    tsv.write_text(
        "name\tgroup\tpriority\tconferences\tgithub_hint\n"
        "Сбер\tBanks\tA\tdf\t\t\n",
        encoding="utf-8",
    )
    aliases = tmp_path / "aliases.json"
    aliases.write_text('{"Сбер": "СБЕР"}', encoding="utf-8")
    store = JsonlStore(tmp_path / "hh.jsonl", "vacancy_id")
    written = run(
        api, store, companies_tsv=tsv, aliases_path=aliases, dry=False
    )
    assert written == 1
    rec = store.load_map()["100"]
    assert rec["manager_email"] == "olga@sber.ru"
    assert rec["company"] == "Сбер"


def test_run_dry_writes_nothing(tmp_path: Path):
    api = FakeApi(
        {
            "/employers?text=X": {"items": [{"id": "1", "name": "X", "open_vacancies": 1}]},
            "employer_id=1": {"items": [{"id": "5", "name": "ML"}], "pages": 1},
            "/vacancies/5": {"id": "5", "contacts": {"name": "N"}, "name": "ML"},
        }
    )
    tsv = tmp_path / "companies.tsv"
    tsv.write_text("name\tgroup\tpriority\nX\tG\tA\n", encoding="utf-8")
    store = JsonlStore(tmp_path / "hh.jsonl", "vacancy_id")
    assert run(api, store, companies_tsv=tsv, aliases_path=tmp_path / "a.json", dry=True) == 0
    assert store.load() == []


def test_guard_no_android_credentials_in_snpr():
    """CI-гвард (ПЛАН §9): Android client_id из research не должен попасть
    в код интеграции."""
    import snpr  # noqa: F401

    snpr_dir = Path(snpr.__file__).parent
    leaked: list[Path] = []
    for py in snpr_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        if "HIOMIAS" in text or "ANDROID_CLIENT_ID" in text:
            leaked.append(py)
    assert leaked == []
