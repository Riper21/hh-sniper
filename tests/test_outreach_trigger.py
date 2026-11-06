"""Тесты связки outreach↔снайпер: триггер, тихий хук, кросс-чек (§10)."""
from __future__ import annotations

import json
from pathlib import Path

from snpr.outreach.base import build_contacts, run_build
from snpr.outreach.pipeline import Paths
from snpr.outreach.store import JsonlStore
from snpr.outreach.trigger import (
    build_hint_card,
    find_contact_for_company,
    maybe_outreach_hint,
)


def _contact(**kwargs) -> dict:
    base = {
        "contact_key": "email:x@y.ru",
        "channel": "email",
        "value": "x@y.ru",
        "company": "Авито",
        "role": "ml_team",
        "source": "github_commits",
        "source_url": "https://github.com/i",
        "confidence": "high",
        "name": "Ivan",
        "stack_matched": ["python"],
    }
    base.update(kwargs)
    return base


def test_find_contact_hiring_manager_priority():
    contacts = [
        _contact(contact_key="email:a@a.ru", value="a@a.ru"),
        _contact(
            contact_key="email:hr@alfabank.ru",
            value="hr@alfabank.ru",
            company="Альфа-Банк",
            role="hiring_manager",
            source="career_api",
        ),
    ]
    found = find_contact_for_company(contacts, "Альфа-Банк")
    assert found is not None
    assert found["role"] == "hiring_manager"
    assert found["value"] == "hr@alfabank.ru"


def test_find_contact_fallback_ml_team():
    contacts = [_contact()]
    found = find_contact_for_company(contacts, "ООО «Авито»")
    assert found is not None
    assert found["role"] == "ml_team"


def test_find_contact_no_match():
    assert find_contact_for_company([_contact()], "Ozon") is None
    assert find_contact_for_company([], "Авито") is None


def test_build_hint_card():
    card = build_hint_card(
        "Senior ML Engineer",
        "Авито",
        61,
        _contact(name="Иван", value="ivan@avito.ru"),
    )
    assert "Senior ML Engineer" in card
    assert "ivan@avito.ru" in card
    assert "github.com/i" in card  # прозрачность источника
    assert "не отправлено" in card


def _write_base(tmp_path: Path, contacts: list[dict]) -> Paths:
    paths = Paths(tmp_path / "out")
    base = paths.base
    base.upsert_many(contacts)
    return paths


def test_maybe_outreach_hint_disabled():
    assert maybe_outreach_hint({}, [{"employer": "Авито"}]) == 0


def test_maybe_outreach_hint_enabled_sends(tmp_path: Path, monkeypatch):
    contacts = [_contact(value="i@avito.ru", contact_key="email:i@avito.ru")]
    _write_base(tmp_path, contacts)
    config = {
        "outreach": {
            "enabled": True,
            "data_dir": str(tmp_path / "out").replace("\\", "/"),
            "trigger": {"enabled": True, "notify_only": True},
        }
    }
    sent_texts: list[str] = []

    import snpr.outreach.trigger as trigger_mod

    def fake_send(text, keyboard=None, paths=None):
        sent_texts.append(text)

    monkeypatch.setattr(
        "snpr.notify_tg.send_message", fake_send
    )
    # send_message импортирован внутри функции — патчим через sys.modules-механику:
    # функция делает `from ..notify_tg import send_message`, поэтому патчим атрибут
    # модуля notify_tg до вызова
    import snpr.notify_tg as notify_mod

    monkeypatch.setattr(notify_mod, "send_message", fake_send)
    sent = maybe_outreach_hint(
        config, [{"employer": "Авито", "name": "ML Eng", "score": 61}]
    )
    assert sent == 1
    assert sent_texts and "i@avito.ru" in sent_texts[0]


def test_maybe_outreach_hint_no_base(tmp_path: Path):
    config = {
        "outreach": {
            "enabled": True,
            "data_dir": str(tmp_path / "empty").replace("\\", "/"),
            "trigger": {"enabled": True, "notify_only": True},
        }
    }
    assert maybe_outreach_hint(config, [{"employer": "X"}]) == 0


def test_maybe_outreach_hint_silent_on_send_error(tmp_path: Path, monkeypatch):
    contacts = [_contact(value="i@avito.ru", contact_key="email:i@avito.ru")]
    _write_base(tmp_path, contacts)
    config = {
        "outreach": {
            "enabled": True,
            "data_dir": str(tmp_path / "out").replace("\\", "/"),
            "trigger": {"enabled": True, "notify_only": True},
        }
    }

    def boom(*args, **kwargs):
        raise RuntimeError("tg down")

    import snpr.notify_tg as notify_mod

    monkeypatch.setattr(notify_mod, "send_message", boom)
    # сбой TG не валит хук (тихий контракт)
    assert (
        maybe_outreach_hint(
            config, [{"employer": "Авито", "name": "ML", "score": 1}]
        )
        == 0
    )


def test_maybe_outreach_hint_notify_only_false():
    config = {
        "outreach": {
            "enabled": True,
            "trigger": {"enabled": True, "notify_only": False},
        }
    }
    # автоотправки писем нет — хук молчит
    assert maybe_outreach_hint(config, [{"employer": "X"}]) == 0


def test_hook_never_breaks_tick(tmp_path: Path, monkeypatch):
    """Контракт «hook never breaks the tick»: кривая конфигурация не роняет
    хук-обёртку в sne.py (там try/except) — проверяем сам trigger тоже."""
    config = {"outreach": {"enabled": True, "trigger": {"enabled": True}}}
    # data_dir не существует → Paths создаёт, базы нет → 0
    assert maybe_outreach_hint(config, []) == 0
