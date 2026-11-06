"""Telegram-доставка: карточки, привязка чата, кнопки, offset-файл.

Живой Telegram API не трогается: _tg_call подменяется рекордером
вызовов, сторы — tmp-пути через TgPaths (как в такте, только в tmp).
"""

import argparse
import json

from snpr import notify_tg
from snpr.notify_tg import TgPaths

CHAT_ID = 424242


def _paths(tmp_path):
    return TgPaths(
        chat=tmp_path / "tg_chat.json",
        offset=tmp_path / "tg_offset.txt",
        parked=tmp_path / "parked.jsonl",
        seen=tmp_path / "seen.jsonl",
        applied=tmp_path / "applied.jsonl",
    )


def _recorder(updates=None):
    """Фейк Bot API: фиксирует вызовы, getUpdates отдает заготовку."""
    calls: list[tuple[str, dict]] = []

    def fake(method, payload, timeout=25.0):
        calls.append((method, dict(payload)))
        if method == "getUpdates":
            return list(updates or [])
        return {}

    fake.calls = calls
    return fake


def _bind(paths: TgPaths, chat_id: int = CHAT_ID) -> None:
    paths.chat.parent.mkdir(parents=True, exist_ok=True)
    paths.chat.write_text(
        json.dumps({"chat_id": chat_id, "name": "test", "ts": 1}),
        encoding="utf-8",
    )


def _start_update(chat_id: int, update_id: int = 5) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "text": "/start",
            "chat": {"id": chat_id},
            "from": {"first_name": "Тест"},
        },
    }


def _callback_update(
    data: str, from_id: int, update_id: int = 7, message_id: int = 77
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": from_id},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": CHAT_ID}},
        },
    }


def _write_parked(path, vid="123", name="ML инженер", employer="СБЕР") -> dict:
    record = {
        "vacancy": {
            "id": vid,
            "name": name,
            "employer": {"name": employer},
            "salary": {"from": 300000, "to": 450000, "currency": "RUR"},
            "area": {"name": "Москва"},
            "published_at": "2026-10-21T10:00:00+03:00",
        },
        "score": {
            "score": 48,
            "verdict": "maybe",
            "reasons": ["стек совпадает", "грейд ок"],
        },
        "letter": "письмо",
        "ts": 1,
    }
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def _sent_texts(fake) -> list[str]:
    return [
        payload["text"]
        for method, payload in fake.calls
        if method == "sendMessage"
    ]


class TestEnabled:
    def test_no_token_disabled(self, monkeypatch):
        monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
        assert notify_tg.tg_enabled(None) is False

    def test_token_enabled(self, monkeypatch):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        assert notify_tg.tg_enabled(None) is True

    def test_config_kill_switch(self, monkeypatch):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        assert notify_tg.tg_enabled({"telegram": {"enabled": False}}) is False


class TestStart:
    def test_first_start_binds_and_greets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        fake = _recorder([_start_update(CHAT_ID)])
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        assert notify_tg.process_updates(None, paths) == 1
        data = json.loads(paths.chat.read_text(encoding="utf-8"))
        assert data["chat_id"] == CHAT_ID
        texts = _sent_texts(fake)
        assert len(texts) == 1
        assert "привязан" in texts[0]
        assert paths.offset.read_text(encoding="utf-8").strip() == "5"

    def test_stranger_after_binding_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        _bind(paths)
        fake = _recorder([_start_update(999)])
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        assert notify_tg.process_updates(None, paths) == 0
        assert _sent_texts(fake) == []
        data = json.loads(paths.chat.read_text(encoding="utf-8"))
        assert data["chat_id"] == CHAT_ID

    def test_same_chat_restarted_replies(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        _bind(paths)
        fake = _recorder([_start_update(CHAT_ID)])
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        assert notify_tg.process_updates(None, paths) == 1
        assert "уже привязан" in _sent_texts(fake)[0]


class TestCards:
    def test_parked_card_fields(self):
        record = _write_parked_standalone()
        text = notify_tg.format_parked_card(record)
        assert "ПАРКОВКА" in text and "48" in text
        assert "ML инженер" in text and "СБЕР" in text
        assert "300000-450000" in text
        assert "Москва" in text
        assert "стек совпадает" in text
        assert "https://hh.ru/vacancy/123" in text

    def test_applied_card_fields(self):
        row = {
            "vacancy_id": "777",
            "vacancy_name": "Senior LLM Engineer",
            "employer": "СБЕР",
            "score": 62,
        }
        text = notify_tg.format_applied_card(row)
        assert "АВТОПУСК" in text and "62" in text
        assert "Senior LLM Engineer" in text and "СБЕР" in text
        assert "https://hh.ru/vacancy/777" in text

    def test_card_truncated_to_limit(self):
        record = _write_parked_standalone(name="X" * 6000)
        assert len(notify_tg.format_parked_card(record)) <= notify_tg.MAX_TEXT


def _write_parked_standalone(vid="123", name="ML инженер") -> dict:
    return {
        "vacancy": {
            "id": vid,
            "name": name,
            "employer": {"name": "СБЕР"},
            "salary": {"from": 300000, "to": 450000, "currency": "RUR"},
            "area": {"name": "Москва"},
            "published_at": "2026-10-21T10:00:00+03:00",
        },
        "score": {"score": 48, "verdict": "maybe", "reasons": ["стек совпадает"]},
        "letter": "письмо",
    }


class TestCallbackParse:
    def test_go_and_no(self):
        assert notify_tg._parse_callback("go:123") == ("go", "123")
        assert notify_tg._parse_callback("no:12") == ("no", "12")

    def test_junk_rejected(self):
        assert notify_tg._parse_callback("go:abc") is None
        assert notify_tg._parse_callback("junk") is None
        assert notify_tg._parse_callback("") is None


class TestNotifyRunResults:
    def test_new_parked_gets_buttons(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        _bind(paths)
        _write_parked(paths.parked, vid="123")
        fake = _recorder()
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        sent = notify_tg.notify_run_results(None, set(), {"999"}, paths)
        assert sent == 1
        payload = fake.calls[0][1]
        assert payload["chat_id"] == CHAT_ID
        assert "ПАРКОВКА" in payload["text"]
        buttons = [
            button["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        assert "go:123" in buttons and "no:123" in buttons

    def test_old_parked_not_resent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        _bind(paths)
        _write_parked(paths.parked, vid="123")
        fake = _recorder()
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        assert notify_tg.notify_run_results(None, set(), {"123"}, paths) == 0
        assert _sent_texts(fake) == []

    def test_new_applied_card(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        _bind(paths)
        row = {
            "vacancy_id": "777",
            "vacancy_name": "Senior LLM Engineer",
            "employer": "СБЕР",
            "score": 62,
        }
        paths.applied.write_text(
            json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        fake = _recorder()
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        assert notify_tg.notify_run_results(None, set(), set(), paths) == 1
        assert "АВТОПУСК" in _sent_texts(fake)[0]

    def test_no_token_no_sends(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
        paths = _paths(tmp_path)
        _bind(paths)
        _write_parked(paths.parked, vid="123")
        fake = _recorder()
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        assert notify_tg.notify_run_results(None, set(), set(), paths) == 0
        assert fake.calls == []

    def test_notify_applied_off(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        _bind(paths)
        row = {"vacancy_id": "777", "vacancy_name": "N", "employer": "E", "score": 1}
        paths.applied.write_text(
            json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        fake = _recorder()
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        config = {"telegram": {"notify_applied": False}}
        assert notify_tg.notify_run_results(config, set(), set(), paths) == 0
        assert _sent_texts(fake) == []


class TestSkipCallback:
    def test_skip_closes_and_edits(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        _bind(paths)
        _write_parked(paths.parked, vid="123")
        fake = _recorder([_callback_update("no:123", CHAT_ID)])
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        assert notify_tg.process_updates(None, paths) == 1
        # парковка опустела, вакансия закрыта в seen
        assert paths.parked.read_text(encoding="utf-8").strip() == ""
        seen_text = paths.seen.read_text(encoding="utf-8")
        assert '"123"' in seen_text and '"ready"' in seen_text
        answers = [
            payload.get("text", "")
            for method, payload in fake.calls
            if method == "answerCallbackQuery"
        ]
        assert "Пропущено" in answers
        edits = [
            payload.get("text", "")
            for method, payload in fake.calls
            if method == "editMessageText"
        ]
        assert any(text.startswith("Пропущено") for text in edits)

    def test_stranger_callback_denied(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        _bind(paths)
        _write_parked(paths.parked, vid="123")
        fake = _recorder([_callback_update("no:123", 999)])
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        assert notify_tg.process_updates(None, paths) == 0
        answers = [
            payload.get("text", "")
            for method, payload in fake.calls
            if method == "answerCallbackQuery"
        ]
        assert "Не ваш бот" in answers
        assert '"id": "123"' in paths.parked.read_text(encoding="utf-8")

    def test_callback_unknown_vacancy(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        _bind(paths)
        fake = _recorder([_callback_update("no:123", CHAT_ID)])
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        assert notify_tg.process_updates(None, paths) == 0
        answers = [
            payload.get("text", "")
            for method, payload in fake.calls
            if method == "answerCallbackQuery"
        ]
        assert any("Уже решена" in answer for answer in answers)


class TestProcessUpdates:
    def test_disabled_no_calls(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
        paths = _paths(tmp_path)
        fake = _recorder([_start_update(CHAT_ID)])
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        assert notify_tg.process_updates(None, paths) == 0
        assert fake.calls == []

    def test_offset_takes_max(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TG_BOT_TOKEN", "test-token")
        paths = _paths(tmp_path)
        _bind(paths)
        updates = [
            _callback_update("no:123", CHAT_ID, update_id=5),
            _start_update(CHAT_ID, update_id=9),
        ]
        fake = _recorder(updates)
        monkeypatch.setattr(notify_tg, "_tg_call", fake)
        notify_tg.process_updates(None, paths)
        assert paths.offset.read_text(encoding="utf-8").strip() == "9"


class TestPathsFromArgs:
    def test_respects_store_overrides(self, tmp_path):
        args = argparse.Namespace(
            seen_store=str(tmp_path / "seen.jsonl"),
            parked_store=str(tmp_path / "parked.jsonl"),
            applied_store=str(tmp_path / "applied.jsonl"),
        )
        paths = notify_tg.paths_from_args(args)
        assert paths.seen == tmp_path / "seen.jsonl"
        assert paths.parked == tmp_path / "parked.jsonl"
        assert paths.applied == tmp_path / "applied.jsonl"
        assert paths.chat == tmp_path / "tg_chat.json"
        assert paths.offset == tmp_path / "tg_offset.txt"
