"""Тесты экрана ответов в чатах: сбор, гвард, кнопки, хук, CLI."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from snpr.chat_reply import (
    BOT_RECRUITER_ANSWER,
    ChatReplier,
    ChatikApi,
    PendingChat,
    RepliedStore,
    _build_ai,
    _format_history_lines,
    _parse_dt,
    _reply_pass,
    _should_reply_now,
    collect_pending,
    match_option,
    maybe_auto_reply,
    run_reply,
    sender_is_bot,
)
from snpr.letter_guard import LetterGuard

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
CAPSULE = "Опыт: 5 лет Python. Ожидания 300000 руб."


# ------------------------------------------------------------- фикстуры


class FakeTransport:
    def __init__(self, pages, chat_datas, unread=1):
        self.pages = pages
        self.chat_datas = chat_datas
        self.unread_n = unread
        self.sends: list[tuple[int, str]] = []
        self.reads: list[tuple[int, int]] = []
        self.chats_calls: list[int] = []
        self.unread_calls = 0

    def unread_count(self) -> int:
        self.unread_calls += 1
        return self.unread_n

    def chats(self, page: int) -> dict:
        self.chats_calls.append(page)
        if page < len(self.pages):
            return self.pages[page]
        return {"chats": {"items": [], "pages": 0}, "resources": {}}

    def chat_data(self, chat_id: int) -> dict:
        return self.chat_datas[chat_id]

    def send(self, chat_id: int, text: str) -> None:
        self.sends.append((chat_id, text))

    def mark_read(self, chat_id: int, message_id: int) -> None:
        self.reads.append((chat_id, message_id))


class FakeAI:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, object]] = []

    def complete(self, message, system_prompt=None, **kwargs):
        self.calls.append((message, system_prompt))
        return self.replies.pop(0)


class FakeTool:
    def __init__(self, resumes=None):
        self.resumes = resumes or []

    def get_resumes(self):
        return self.resumes


def _item(
    chat_id=1,
    message_id=10,
    text="Готовы обсудить вилку?",
    *,
    buttons=(),
    is_bot=True,
    name="ИИ-помощник",
    mine=False,
    created=NOW,
    discard=False,
    vacancy_id="777",
    employer_id=55,
    comp_from=200000,
    comp_to=300000,
):
    last = {
        "id": message_id,
        "participantId": 200 if mine else 100,
        "text": text,
        "creationTime": created.isoformat() if created else None,
        "participantDisplay": {"name": name, "isBot": is_bot},
    }
    if buttons:
        last["actions"] = {"text_buttons": [{"text": b} for b in buttons]}
    if discard:
        last["workflowTransition"] = {"applicantState": "DISCARD"}
    return {
        "id": chat_id,
        "currentParticipantId": 200,
        "lastMessage": last,
        "resources": {"VACANCY": [vacancy_id]},
    }


def _vacancy(vac_id="777", employer_id=55, comp_from=200000, comp_to=300000):
    return {
        "vacancyId": vac_id,
        "id": vac_id,
        "name": "Python-разработчик",
        "company": {"id": employer_id, "name": "ООО Ромашка"},
        "links": {"desktop": "https://hh.ru/vacancy/777"},
        "compensation": {"from": comp_from, "to": comp_to, "currency": "RUR"},
    }


def _page(items, pages=1, vacancies=None):
    return {
        "chats": {"items": items, "pages": pages},
        "resources": {"vacancies": vacancies if vacancies is not None else {}},
    }


def _msg(pid, text, mid, name="ИИ-помощник", is_bot=True):
    return {
        "id": mid,
        "participantId": pid,
        "text": text,
        "participantDisplay": {"name": name, "isBot": is_bot},
    }


def _chat_data(messages, allowed=True, current=200):
    return {
        "chat": {"currentParticipantId": current, "messages": {"items": messages}},
        "chatStates": {"writeMessageState": {"allowed": allowed}},
    }


def _pending(chat_id=1, message_id=10, question="Готовы к вилке?", **over):
    base = dict(
        options=[],
        sender_name="ИИ-помощник",
        is_bot=True,
        vacancy_id="777",
        vacancy_name="Python-разработчик",
        employer_id="55",
        employer_name="ООО Ромашка",
        vacancy_numbers={"200000"},
        created=NOW,
        is_discard=False,
    )
    base.update(over)
    return PendingChat(chat_id=chat_id, message_id=message_id, question=question, **base)


def _replier(store, ai=None, guard=None, **over):
    return ChatReplier(
        ai,
        guard or LetterGuard(allowed_numbers={"300000"}),
        store,
        capsule=CAPSULE,
        pause=0.0,
        **over,
    )


def _write_config(tmp_path: Path) -> str:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[letter]\n"
        f'profile = "{CAPSULE}"\n'
        "\n"
        "[letter_guard]\n"
        'allowed_url_prefixes = ["https://github.com/"]\n'
        "\n"
        "[blacklist]\n"
        f'file = "{(tmp_path / "missing.txt").as_posix()}"\n',
        encoding="utf-8",
    )
    return str(cfg)


def _reply_args(tmp_path: Path, **over) -> argparse.Namespace:
    base = {
        "config": _write_config(tmp_path),
        "replied_store": str(tmp_path / "replied.jsonl"),
        "answer_humans": False,
        "max_chats": 5,
        "period_hours": 72.0,
        "max_history": 20,
        "max_pages": 3,
        "dry_run": False,
        "confirm": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


# ------------------------------------------------------- мелкие функции


def test_parse_dt_variants() -> None:
    assert _parse_dt("2026-10-18T12:00:00Z") == datetime(
        2026, 10, 18, 12, 0, tzinfo=timezone.utc
    )
    assert _parse_dt("2026-10-18T12:00:00+03:00").utcoffset() == timedelta(hours=3)
    naive = _parse_dt("2026-10-18T12:00:00")
    assert naive is not None and naive.utcoffset() == timedelta(hours=3)
    assert _parse_dt("мусор") is None
    assert _parse_dt(None) is None


def test_sender_is_bot_flag_and_name() -> None:
    assert sender_is_bot({"participantDisplay": {"isBot": True, "name": "x"}})
    assert sender_is_bot({"participantDisplay": {"name": "ИИ-помощник Miractal"}})
    # «работодатель» содержит «бот» как подстроку — не должно ловиться
    assert not sender_is_bot({"participantDisplay": {"name": "Работодатель ПАО"}})
    assert not sender_is_bot({"participantDisplay": {"name": "Анна"}})


def test_match_option() -> None:
    options = ["Да, готов", "Нет"]
    assert match_option("Да, готов", options) == "Да, готов"
    assert match_option("  да, ГОТОВ ", options) == "Да, готов"
    assert match_option('- "Нет"', options) == "Нет"
    assert match_option("Может быть", options) is None


# --------------------------------------------------------------- стор


def test_replied_store_roundtrip(tmp_path: Path) -> None:
    store = RepliedStore(tmp_path / "replied.jsonl")
    assert store.last_ids() == {}
    store.append(1, 10, "options", "Да", employer="ООО Ромашка")
    store.append(1, 12, "text", "Ответ")
    store.append(2, 30, "options", "Нет")
    store.append(1, 11, "text", "старый id не перебивает максимум")
    assert store.last_ids() == {1: 12, 2: 30}


def test_replied_store_ignores_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "replied.jsonl"
    path.write_text(
        "{не json\n" + '{"chat_id": 1, "message_id": 5}\n' + "\n",
        encoding="utf-8",
    )
    assert RepliedStore(path).last_ids() == {1: 5}


# ------------------------------------------------------------ сбор чатов


def test_collect_pending_parses_fields() -> None:
    item = _item(buttons=("Да", "Нет"))
    transport = FakeTransport([_page([item], vacancies={"777": _vacancy()})], {})
    pending, skipped = collect_pending(transport, {}, now=NOW)
    assert skipped == 0
    assert len(pending) == 1
    chat = pending[0]
    assert chat.chat_id == 1 and chat.message_id == 10
    assert chat.options == ["Да", "Нет"]
    assert chat.vacancy_name == "Python-разработчик"
    assert chat.employer_name == "ООО Ромашка"
    assert chat.vacancy_numbers == {"200000", "300000"}
    assert chat.is_bot and chat.created == NOW


def test_collect_pending_skips_own_last_message() -> None:
    transport = FakeTransport([_page([_item(mine=True)])], {})
    assert collect_pending(transport, {}, now=NOW)[0] == []


def test_collect_pending_skips_bot_recruiter_answer() -> None:
    transport = FakeTransport([_page([_item(text=BOT_RECRUITER_ANSWER)])], {})
    assert collect_pending(transport, {}, now=NOW)[0] == []


def test_collect_pending_skips_old_but_period_zero_keeps() -> None:
    old = _item(created=NOW - timedelta(hours=100))
    transport = FakeTransport([_page([old])], {})
    assert collect_pending(transport, {}, period_hours=72, now=NOW)[0] == []
    assert len(collect_pending(transport, {}, period_hours=0, now=NOW)[0]) == 1


def test_collect_pending_replied_message_not_pending(tmp_path: Path) -> None:
    transport = FakeTransport([_page([_item(message_id=10)])], {})
    assert collect_pending(transport, {1: 10}, now=NOW)[0] == []
    assert len(collect_pending(transport, {1: 9}, now=NOW)[0]) == 1


def test_collect_pending_blacklist_skips_and_counts() -> None:
    by_id = _item(employer_id=55)
    by_name = _item(chat_id=2, employer_id=99)
    vacancies = {"777": _vacancy(employer_id=55), "888": _vacancy("888", 99)}
    transport = FakeTransport(
        [_page([by_id, by_name], vacancies=vacancies)], {}
    )
    pending, skipped = collect_pending(
        transport,
        {},
        blacklist_ids=frozenset({"55"}),
        blacklist_names=frozenset({"ооо ромашка"}),
        now=NOW,
    )
    assert pending == [] and skipped == 2


def test_collect_pending_max_chats_caps() -> None:
    items = [_item(chat_id=i, message_id=i * 10) for i in range(1, 6)]
    transport = FakeTransport([_page(items)], {})
    pending, _ = collect_pending(transport, {}, max_chats=2, now=NOW)
    assert [c.chat_id for c in pending] == [1, 2]


def test_collect_pending_stops_on_last_page() -> None:
    transport = FakeTransport([_page([_item()], pages=1)], {})
    collect_pending(transport, {}, max_pages=3, now=NOW)
    assert transport.chats_calls == [0]


# ------------------------------------------------------------- история


def test_format_history_lines_authors_and_trim() -> None:
    messages = [
        _msg(100, "Вопрос 1", 1),
        _msg(200, "Мой ответ", 2),
        _msg(100, "Ещё вопрос", 3, name="Анна", is_bot=False),
    ]
    text, count = _format_history_lines(_chat_data(messages))
    assert count == 3
    assert "Я:\nМой ответ" in text
    assert "Анна:\nЕщё вопрос" in text
    assert "ИИ-помощник:\nВопрос 1" in text

    long = [_msg(100 if i % 2 else 200, f"Сообщение {i}", i) for i in range(15)]
    text, count = _format_history_lines(_chat_data(long))
    assert count == 15
    assert "пропущено сообщений" in text


# ---------------------------------------------------------- ChatReplier


def test_process_options_bot_sends_exact_option(tmp_path: Path) -> None:
    store = RepliedStore(tmp_path / "replied.jsonl")
    transport = FakeTransport(
        [], {1: _chat_data([_msg(100, "Готовы?", 10)])}
    )
    ai = FakeAI(["Да"])
    replier = _replier(store, ai)
    chat = _pending(options=["Да", "Нет"])
    assert replier.process(transport, chat) == "sent"
    assert transport.sends == [(1, "Да")]
    assert transport.reads == [(1, 10)]
    assert store.last_ids() == {1: 10}


def test_process_options_mismatch_not_sent(tmp_path: Path) -> None:
    store = RepliedStore(tmp_path / "replied.jsonl")
    transport = FakeTransport([], {1: _chat_data([_msg(100, "Готовы?", 10)])})
    replier = _replier(store, FakeAI(["Может быть"]))
    assert replier.process(transport, _pending(options=["Да", "Нет"])) == "mismatch"
    assert transport.sends == [] and transport.reads == []
    assert store.last_ids() == {}


def test_process_options_without_llm_skips(tmp_path: Path) -> None:
    transport = FakeTransport([], {1: _chat_data([_msg(100, "Готовы?", 10)])})
    replier = _replier(RepliedStore(tmp_path / "r.jsonl"), ai=None)
    assert replier.process(transport, _pending(options=["Да"])) == "llm"
    assert transport.sends == []


def test_process_text_bot_sends_guarded_answer(tmp_path: Path) -> None:
    store = RepliedStore(tmp_path / "replied.jsonl")
    transport = FakeTransport([], {1: _chat_data([_msg(100, "Опыт?", 10)])})
    ai = FakeAI(["Пять лет на Python, готов обсудить детали."])
    replier = _replier(store, ai)
    assert replier.process(transport, _pending()) == "sent"
    assert transport.sends == [(1, "Пять лет на Python, готов обсудить детали.")]
    assert store.last_ids() == {1: 10}
    # капсула фактов ушла в system_prompt, вопрос — в основной промпт
    prompt, system = ai.calls[0]
    assert system is not None and "300000" in system and "только" in system.lower()
    assert "Опыт?" in prompt


def test_process_text_guard_blocks_phone(tmp_path: Path) -> None:
    store = RepliedStore(tmp_path / "replied.jsonl")
    transport = FakeTransport([], {1: _chat_data([_msg(100, "Телефон?", 10)])})
    replier = _replier(store, FakeAI(["Позвоните +7 999 123-45-67"]))
    assert replier.process(transport, _pending()) == "guard"
    assert transport.sends == [] and transport.reads == []
    assert store.last_ids() == {}


def test_process_text_guard_blocks_foreign_link_and_number(tmp_path: Path) -> None:
    guard = LetterGuard(allowed_numbers={"300000"})

    def _transport():
        return FakeTransport([], {1: _chat_data([_msg(100, "?", 10)])})

    # ссылка вне белого списка
    replier = _replier(RepliedStore(tmp_path / "r.jsonl"), FakeAI(
        ["Смотрите https://evil.example/cv"
         ]
    ), guard=guard)
    assert replier.process(_transport(), _pending()) == "guard"
    # число вне капсулы и вилки вакансии
    replier = _replier(RepliedStore(tmp_path / "r.jsonl"), FakeAI(
        ["Хочу 990000 руб."]
    ), guard=guard)
    assert replier.process(_transport(), _pending()) == "guard"
    # число вилки вакансии — легитимно
    store = RepliedStore(tmp_path / "r.jsonl")
    replier = _replier(store, FakeAI(["Уложусь в 200000."]), guard=guard)
    assert replier.process(_transport(), _pending()) == "sent"
    assert store.last_ids() == {1: 10}


def test_process_human_question_not_answered_by_default(tmp_path: Path) -> None:
    store = RepliedStore(tmp_path / "replied.jsonl")
    transport = FakeTransport([], {1: _chat_data([_msg(100, "Позовите?", 10)])})
    ai = FakeAI(["не должен вызваться"])
    replier = _replier(store, ai)
    chat = _pending(is_bot=False, sender_name="Анна")
    assert replier.process(transport, chat) == "human"
    assert ai.calls == [] and transport.sends == []
    assert store.last_ids() == {}


def test_process_answer_humans_enabled(tmp_path: Path) -> None:
    transport = FakeTransport([], {1: _chat_data([_msg(100, "Позовите?", 10)])})
    replier = _replier(
        RepliedStore(tmp_path / "r.jsonl"),
        FakeAI(["Да, готов созвониться."]),
        answer_humans=True,
    )
    chat = _pending(is_bot=False, sender_name="Анна")
    assert replier.process(transport, chat) == "sent"
    assert transport.sends == [(1, "Да, готов созвониться.")]


def test_process_discard_readonly_and_history(tmp_path: Path) -> None:
    store = RepliedStore(tmp_path / "r.jsonl")
    replier = _replier(store)
    assert replier.process(
        FakeTransport([], {1: _chat_data([])}), _pending(is_discard=True)
    ) == "discard"
    assert replier.process(
        FakeTransport([], {1: _chat_data([], allowed=False)}), _pending()
    ) == "readonly"
    long = [_msg(100 if i % 2 else 200, f"Сообщ {i}", i) for i in range(25)]
    assert replier.process(
        FakeTransport([], {1: _chat_data(long)}), _pending()
    ) == "history"
    assert store.last_ids() == {}


def test_process_dry_run_sends_nothing(tmp_path: Path, capsys) -> None:
    store = RepliedStore(tmp_path / "replied.jsonl")
    transport = FakeTransport([], {1: _chat_data([_msg(100, "Опыт?", 10)])})
    replier = _replier(store, FakeAI(["Пять лет."]), dry_run=True)
    assert replier.process(transport, _pending()) == "dry"
    assert transport.sends == [] and transport.reads == []
    assert store.last_ids() == {}
    assert "dry-run" in capsys.readouterr().out


def test_process_confirm_decline(tmp_path: Path) -> None:
    transport = FakeTransport([], {1: _chat_data([_msg(100, "Опыт?", 10)])})
    replier = _replier(
        RepliedStore(tmp_path / "r.jsonl"),
        FakeAI(["Пять лет."]),
        confirm=True,
        confirm_prompt=lambda q: "n",
    )
    assert replier.process(transport, _pending()) == "declined"
    assert transport.sends == []


def test_reply_pass_stats_and_empty(tmp_path: Path, capsys) -> None:
    store = RepliedStore(tmp_path / "replied.jsonl")
    empty = FakeTransport([_page([])], {})
    assert _reply_pass(
        empty,
        _replier(store),
        store,
        period_hours=72,
        max_pages=3,
        max_chats=5,
        blacklist_ids=frozenset(),
        blacklist_names=frozenset(),
    ) == {}
    assert "Чатов, требующих ответа, нет." in capsys.readouterr().out

    bot = _item(buttons=("Да",), chat_id=1)
    human = _item(chat_id=2, message_id=20, is_bot=False, name="Анна")
    transport = FakeTransport(
        [_page([bot, human], vacancies={"777": _vacancy()})],
        {
            1: _chat_data([_msg(100, "Готовы?", 10)]),
            2: _chat_data([_msg(100, "Позовите?", 20)]),
        },
    )
    stats = _reply_pass(
        transport,
        _replier(store, FakeAI(["Да"])),
        store,
        period_hours=72,
        max_pages=1,
        max_chats=5,
        blacklist_ids=frozenset(),
        blacklist_names=frozenset(),
    )
    assert stats == {"sent": 1, "human": 1}
    out = capsys.readouterr().out
    assert "Итого (2)" in out and "отправлено: 1" in out


# ------------------------------------------------------------ хук такта


def test_should_reply_now_marker_logic(tmp_path: Path) -> None:
    marker = tmp_path / "last_reply.txt"
    assert _should_reply_now(marker, 15, now=NOW)[0] is True
    marker.write_text(str((NOW - timedelta(minutes=1)).timestamp()), encoding="utf-8")
    assert _should_reply_now(marker, 15, now=NOW)[0] is False
    marker.write_text(str((NOW - timedelta(minutes=20)).timestamp()), encoding="utf-8")
    assert _should_reply_now(marker, 15, now=NOW)[0] is True


def _hook_config(tmp_path: Path, **reply_over) -> dict:
    reply = {
        "enabled": True,
        "interval_minutes": 15.0,
        "period_hours": 72.0,
        "max_chats_per_run": 5,
        "max_history": 20,
        "answer_humans": False,
    }
    reply.update(reply_over)
    return {
        "reply": reply,
        "letter": {"profile": CAPSULE},
        "letter_guard": {"allowed_url_prefixes": ["https://github.com/"]},
        "blacklist": {"file": str(tmp_path / "missing.txt")},
    }


def test_maybe_auto_reply_disabled_no_calls(tmp_path: Path) -> None:
    transport = FakeTransport([_page([])], {})
    config = _hook_config(tmp_path, enabled=False)
    assert maybe_auto_reply(config, FakeTool(), tmp_path, ai=FakeAI([])) is False
    assert transport.unread_calls == 0
    assert not (tmp_path / "last_reply.txt").exists()


def test_maybe_auto_reply_interval_gates(tmp_path: Path) -> None:
    marker = tmp_path / "last_reply.txt"
    marker.write_text(str((NOW - timedelta(minutes=1)).timestamp()), encoding="utf-8")
    transport = FakeTransport([_page([])], {})
    config = _hook_config(tmp_path)
    assert (
        maybe_auto_reply(
            config, FakeTool(), tmp_path, ai=FakeAI([]), transport=transport, now=NOW
        )
        is False
    )
    assert transport.unread_calls == 0


def test_maybe_auto_reply_unread_zero_touches_marker(tmp_path: Path) -> None:
    transport = FakeTransport([_page([])], {}, unread=0)
    config = _hook_config(tmp_path)
    assert (
        maybe_auto_reply(
            config, FakeTool(), tmp_path, ai=FakeAI([]), transport=transport, now=NOW
        )
        is False
    )
    marker = tmp_path / "last_reply.txt"
    assert marker.exists()
    assert abs(float(marker.read_text()) - NOW.timestamp()) < 1
    assert transport.chats_calls == []


def test_maybe_auto_reply_full_pass(tmp_path: Path, capsys) -> None:
    transport = FakeTransport(
        [_page([_item(buttons=("Да",))], vacancies={"777": _vacancy()})],
        {1: _chat_data([_msg(100, "Готовы?", 10)])},
        unread=1,
    )
    tool = FakeTool([{"salary": {"amount": 300000}}])
    config = _hook_config(tmp_path)
    assert (
        maybe_auto_reply(
            config, tool, tmp_path, ai=FakeAI(["Да"]), transport=transport, now=NOW
        )
        is True
    )
    assert transport.sends == [(1, "Да")]
    assert transport.reads == [(1, 10)]
    assert RepliedStore(tmp_path / "replied.jsonl").last_ids() == {1: 10}
    assert (tmp_path / "last_reply.txt").exists()
    assert "непрочитанных чатов: 1" in capsys.readouterr().out


def test_maybe_auto_reply_without_llm_no_marker(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    for key in ("SNE_AI_BASE_URL", "SNE_AI_MODEL", "SNE_AI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    transport = FakeTransport([_page([])], {})
    config = _hook_config(tmp_path)
    assert maybe_auto_reply(config, FakeTool(), tmp_path, transport=transport) is False
    assert "нет LLM" in capsys.readouterr().out
    assert not (tmp_path / "last_reply.txt").exists()


# ------------------------------------------------------------------ CLI


def test_run_reply_end_to_end(tmp_path: Path) -> None:
    transport = FakeTransport(
        [_page([_item(buttons=("Да",))], vacancies={"777": _vacancy()})],
        {1: _chat_data([_msg(100, "Готовы?", 10)])},
    )
    args = _reply_args(tmp_path)
    assert run_reply(args, FakeTool([{"salary": {"amount": 300000}}]),
                     transport=transport, ai=FakeAI(["Да"])) == 0
    assert transport.sends == [(1, "Да")]
    assert RepliedStore(tmp_path / "replied.jsonl").last_ids() == {1: 10}


def test_run_reply_dry_run_sends_nothing(tmp_path: Path) -> None:
    transport = FakeTransport(
        [_page([_item(buttons=("Да",))], vacancies={"777": _vacancy()})],
        {1: _chat_data([_msg(100, "Готовы?", 10)])},
    )
    args = _reply_args(tmp_path, dry_run=True)
    assert run_reply(args, FakeTool(), transport=transport, ai=FakeAI(["Да"])) == 0
    assert transport.sends == [] and transport.reads == []
    assert not (tmp_path / "replied.jsonl").exists()


def test_run_reply_transport_error_returns_1(tmp_path: Path, capsys) -> None:
    class BrokenTransport:
        def unread_count(self):
            return 1

        def chats(self, page):
            raise RuntimeError("сеть упала")

    args = _reply_args(tmp_path)
    assert run_reply(args, FakeTool(), transport=BrokenTransport(),
                     ai=FakeAI([])) == 1
    assert "Ошибка прогона ответов" in capsys.readouterr().err


def test_build_ai_without_env_returns_none(monkeypatch) -> None:
    for key in ("SNE_AI_BASE_URL", "SNE_AI_MODEL", "SNE_AI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert _build_ai() is None


# ------------------------------------------------------------ ChatikApi


class _FakeResponse:
    def __init__(self, payload, url="https://chatik.hh.ru/xhh"):
        self._payload = payload
        self.url = url

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params, headers))
        return _FakeResponse(self._match("GET", url))

    def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url, json, headers))
        return _FakeResponse(self._match("POST", url))

    def _match(self, method, url):
        for (m, frag), payload in self.routes.items():
            if m == method and frag in url:
                return payload
        raise AssertionError(f"неожидался {method} {url}")


class _FakeHHTool:
    base_url = "https://hh.ru"
    xsrf_token = "TOKEN123"

    def __init__(self, session, state=None):
        self.session = session
        self._state = state or {
            "config": {"externalMicroFrontendHosts": {"chatik": "https://chatik.hh.ru"}}
        }

    def get_initial_state(self, url):
        return self._state


def test_chatik_api_headers_params_and_send() -> None:
    session = _FakeSession(
        {
            ("GET", "dest=iframe"): {},
            ("GET", "/chatik/api/chats"): {
                "chats": {"items": [], "pages": 0},
                "resources": {},
            },
            ("GET", "/chatik/api/unread"): {"unreadCount": 3},
            ("POST", "/chatik/api/send"): {},
            ("POST", "/chatik/api/mark_read"): {},
        }
    )
    api = ChatikApi(_FakeHHTool(session))
    assert api.unread_count() == 3
    api.chats(0)
    chats_call = [c for c in session.calls if c[1].endswith("/chatik/api/chats")][0]
    assert chats_call[2]["filterUnread"] == "true"
    headers = chats_call[3]
    assert headers["X-Xsrftoken"] == "TOKEN123"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["Referer"] == "https://chatik.hh.ru/xhh"
    api.send(1, "Привет!")
    send_call = [c for c in session.calls if c[1].endswith("/chatik/api/send")][0]
    payload = send_call[2]
    assert payload["chatId"] == 1 and payload["text"] == "Привет!"
    assert len(payload["idempotencyKey"]) == 36
    api.mark_read(1, 10)
    mark_call = [c for c in session.calls if c[1].endswith("/mark_read")][0]
    assert mark_call[2] == {
        "chatId": 1,
        "messageId": 10,
        "hasUnreadDiscardMessage": False,
    }


def test_chatik_api_no_host_raises() -> None:
    api = ChatikApi(_FakeHHTool(_FakeSession({}), state={"config": {}}))
    try:
        api.unread_count()
    except RuntimeError as ex:
        assert "чатика" in str(ex)
    else:
        raise AssertionError("ожидали RuntimeError")
