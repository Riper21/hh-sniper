"""Telegram-доставка: карточки итогов такта + кнопки решений из кармана.

Фаза 1 мультиканального плана (20.10, ПЛАН_РАЗВИТИЯ_МУЛЬТИКАНАЛ.md):
бот — канал доставки к человеку, не новый источник вакансий.

- итоги такта уходят в чат сразу: автопуски (скор >= auto_approve_min)
  — информационной карточкой, свежая парковка (maybe 45-54) — карточкой
  с кнопками «Пустить» / «Пропустить»;
- нажатие исполняется тем же кодом, что `snpr approve`: свежая карточка
  вакансии, архив, дубль объявления, LetterGuard, дневной лимит hh,
  история пусков и seen-дедуп;
- отдельных процессов нет: и отправка, и разбор живут в такте
  `sne --mode park` (в начале — накопившиеся нажатия, в конце — карточки
  и мгновенный добор решений; offset-файл не даёт разобрать апдейт
  дважды, сами апдейты копятся на серверах Telegram до суток).

Включение: бот в @BotFather → токен TG_BOT_TOKEN в .env → /start боту
(чат привязывается к первому написавшему, прочие игнорируются). Нет
токена — бот молча выключен, снпрер работает как раньше. Общий
выключатель — [telegram].enabled в config.toml.

CLI: python -m snpr tg status|test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .cli import (
    DEFAULT_APPLIED_STORE,
    DEFAULT_PARKED_STORE,
    DEFAULT_SEEN_STORE,
)

TG_API = "https://api.telegram.org"
DATA_DIR = Path(DEFAULT_PARKED_STORE).parent
CHAT_FILE = DATA_DIR / "tg_chat.json"
OFFSET_FILE = DATA_DIR / "tg_offset.txt"
MAX_TEXT = 4000  # лимит Telegram — 4096, запас на служебное
MAX_UPDATES = 50


@dataclass
class TgPaths:
    """Пути бота: в такте — боевые сторы, в тестах — tmp."""

    chat: Path
    offset: Path
    parked: Path
    seen: Path
    applied: Path


def default_paths() -> TgPaths:
    return TgPaths(
        chat=CHAT_FILE,
        offset=OFFSET_FILE,
        parked=Path(DEFAULT_PARKED_STORE),
        seen=Path(DEFAULT_SEEN_STORE),
        applied=Path(DEFAULT_APPLIED_STORE),
    )


def paths_from_args(args: argparse.Namespace) -> TgPaths:
    """Пути из аргументов такта (уважает --parked-store и прочие override)."""
    seen = Path(str(getattr(args, "seen_store", DEFAULT_SEEN_STORE)))
    return TgPaths(
        chat=seen.parent / "tg_chat.json",
        offset=seen.parent / "tg_offset.txt",
        parked=Path(str(getattr(args, "parked_store", DEFAULT_PARKED_STORE))),
        seen=seen,
        applied=Path(str(getattr(args, "applied_store", DEFAULT_APPLIED_STORE))),
    )


# --- включённость и привязка -------------------------------------------------


def bot_token() -> str:
    return str(os.environ.get("TG_BOT_TOKEN") or "").strip()


def _cfg_flag(
    config: Mapping[str, Any] | None, key: str, default: bool
) -> bool:
    if not config:
        return default
    section = config.get("telegram")
    if not isinstance(section, Mapping):
        return default
    value = section.get(key)
    return bool(value) if value is not None else default


def tg_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Бот включён: токен в .env + [telegram].enabled (дефолт true)."""
    if not bot_token():
        return False
    return _cfg_flag(config, "enabled", True)


def bound_chat_id(paths: TgPaths | None = None) -> int | None:
    """Привязанный чат (первый /start); None — бот ещё не привязан."""
    path = (paths or default_paths()).chat
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    chat_id = data.get("chat_id")
    return int(chat_id) if chat_id else None


def _save_chat(paths: TgPaths, chat_id: int, name: str = "") -> None:
    paths.chat.parent.mkdir(parents=True, exist_ok=True)
    paths.chat.write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "name": name,
                "ts": int(time.time()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# --- Bot API ------------------------------------------------------------------


def _tg_call(method: str, payload: Mapping[str, Any], timeout: float = 25.0) -> Any:
    """POST JSON к Bot API; не-ok ответ — исключение (тихий хук ловит)."""
    token = bot_token()
    if not token:
        raise RuntimeError("TG_BOT_TOKEN не задан")
    request = urllib.request.Request(
        f"{TG_API}/bot{token}/{method}",
        data=json.dumps(dict(payload)).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        detail = ""
        try:
            detail = str(
                json.loads(ex.read().decode("utf-8")).get("description") or ""
            )
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"TG {method}: HTTP {ex.code} {detail}".strip()) from ex
    if not isinstance(body, dict) or not body.get("ok"):
        raise RuntimeError(f"TG {method}: {body.get('description')}")
    return body.get("result")


def _answer(callback_query_id: str, text: str = "") -> None:
    """Ответ на нажатие: всплывашка в Telegram. Не критичен для решения."""
    if not callback_query_id:
        return
    try:
        _tg_call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text[:200]},
        )
    except Exception:  # noqa: BLE001
        pass


def _edit_message(message: Mapping[str, Any], text: str) -> None:
    """Поправить карточку после решения (статус вместо кнопок)."""
    if not message:
        return
    try:
        _tg_call(
            "editMessageText",
            {
                "chat_id": (message.get("chat") or {}).get("id"),
                "message_id": message.get("message_id"),
                "text": text[:MAX_TEXT],
            },
        )
    except Exception:  # noqa: BLE001
        pass


# --- карточки -----------------------------------------------------------------


def _salary_line(vacancy: Mapping[str, Any]) -> str:
    salary = vacancy.get("salary") or {}
    if not isinstance(salary, Mapping):
        return ""
    values = [salary.get(k) for k in ("from", "to") if salary.get(k) is not None]
    if not values:
        return ""
    currency = str(salary.get("currency") or "").strip()
    if len(values) == 2:
        return f"{values[0]}-{values[1]} {currency}".strip()
    return f"{values[0]} {currency}".strip()


def _vacancy_line(vacancy: Mapping[str, Any]) -> str:
    """«Название — Работодатель · вилка · город» для карточек и правок."""
    name = str(vacancy.get("name") or "")
    employer = str((vacancy.get("employer") or {}).get("name") or "")
    bits = [f"{name} — {employer}".strip(" —")]
    salary = _salary_line(vacancy)
    if salary:
        bits.append(salary)
    area = str((vacancy.get("area") or {}).get("name") or "")
    if area:
        bits.append(area)
    return " · ".join(bits)


def _age_suffix(published_at: object) -> str:
    from .approve import vacancy_age_hours

    age = vacancy_age_hours(published_at)
    if age is None:
        return ""
    if age < 1.0:
        return f" · {max(1, int(age * 60))} мин назад"
    return f" · {age:.0f} ч назад"


def format_parked_card(record: Mapping[str, Any]) -> str:
    """Карточка maybe-кандидата: скор, вилка, причины, ссылка, возраст."""
    vacancy = record.get("vacancy") or {}
    vid = str(vacancy.get("id") or "")
    score = record.get("score") if isinstance(record.get("score"), dict) else {}
    lines = [f"ПАРКОВКА · скор {score.get('score', '—')} · решение за вами"]
    lines.append(_vacancy_line(vacancy) + _age_suffix(vacancy.get("published_at")))
    for reason in (score.get("reasons") or [])[:3]:
        text = str(reason).strip()
        if text:
            lines.append(f"— {text[:120]}")
    if vid:
        lines.append(f"https://hh.ru/vacancy/{vid}")
    return "\n".join(lines)[:MAX_TEXT]


def format_applied_card(row: Mapping[str, Any]) -> str:
    """Карточка автопуска (информационная, без кнопок)."""
    vid = str(row.get("vacancy_id") or "")
    lines = [
        f"АВТОПУСК · скор {row.get('score', '—')}",
        f"{row.get('vacancy_name', '')} — {row.get('employer', '')}".strip(" —"),
    ]
    if vid:
        lines.append(f"https://hh.ru/vacancy/{vid}")
    return "\n".join(lines)[:MAX_TEXT]


def _decision_keyboard(vid: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Пустить", "callback_data": f"go:{vid}"},
                {"text": "Пропустить", "callback_data": f"no:{vid}"},
            ]
        ]
    }


# --- отправка -----------------------------------------------------------------


def send_message(
    text: str,
    keyboard: Mapping[str, Any] | None = None,
    paths: TgPaths | None = None,
) -> None:
    """Сообщение в привязанный чат; не привязан — молча (ждёт /start)."""
    chat = bound_chat_id(paths)
    if chat is None:
        return
    payload: dict[str, Any] = {"chat_id": chat, "text": text[:MAX_TEXT]}
    if keyboard:
        payload["reply_markup"] = dict(keyboard)
    _tg_call("sendMessage", payload)


def _applied_rows_after(paths: TgPaths, before: set[str]) -> list[dict[str, Any]]:
    """Строки applied-истории с id, которых не было до такта (diff)."""
    rows: list[dict[str, Any]] = []
    if not paths.applied.exists():
        return rows
    with paths.applied.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and str(row.get("vacancy_id") or "") not in before:
                rows.append(row)
    return rows


def notify_run_results(
    config: Mapping[str, Any] | None,
    applied_before: set[str],
    parked_before: set[str],
    paths: TgPaths | None = None,
) -> int:
    """Пуши итогов такта: автопуски + свежая парковка с кнопками.

    «Новые» считаются diff-ом сторов до/после пусковой фазы такта
    (id пусков и id парковки). Возврат — число отправленных карточек.
    """
    if not tg_enabled(config):
        return 0
    paths = paths or default_paths()
    from .sne import ParkedStore

    sent = 0
    if _cfg_flag(config, "notify_parked", True):
        records = ParkedStore(paths.parked).load_records()
        fresh = [
            record
            for record in records
            if str((record.get("vacancy") or {}).get("id") or "")
            not in parked_before
        ]
        for record in fresh:
            vid = str((record.get("vacancy") or {}).get("id") or "")
            send_message(
                format_parked_card(record), _decision_keyboard(vid), paths
            )
            sent += 1
    if _cfg_flag(config, "notify_applied", True):
        for row in _applied_rows_after(paths, applied_before):
            send_message(format_applied_card(row), None, paths)
            sent += 1
    return sent


# --- разбор нажатий ------------------------------------------------------------


def process_updates(
    config: Mapping[str, Any] | None = None,
    paths: TgPaths | None = None,
) -> int:
    """Разобрать накопившиеся апдейты бота (getUpdates без ожидания).

    /start от первого человека привязывает чат; кнопки «Пустить/
    Пропустить» исполняются кодом `snpr approve`. Возврат — число
    обработанных событий (0 — бот выключен или событий нет).
    """
    if not tg_enabled(config):
        return 0
    paths = paths or default_paths()
    offset = _load_offset(paths)
    updates = _tg_call(
        "getUpdates",
        {
            "offset": offset + 1,
            "timeout": 0,
            "limit": MAX_UPDATES,
            "allowed_updates": ["message", "callback_query"],
        },
        timeout=20.0,
    )
    if not isinstance(updates, list) or not updates:
        return 0
    handled = 0
    for update in updates:
        if isinstance(update, dict):
            offset = max(offset, int(update.get("update_id") or 0))
            handled += _handle_update(update, config, paths)
    _save_offset(paths, offset)
    return handled


def _load_offset(paths: TgPaths) -> int:
    try:
        return int(paths.offset.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _save_offset(paths: TgPaths, value: int) -> None:
    paths.offset.parent.mkdir(parents=True, exist_ok=True)
    paths.offset.write_text(f"{value}\n", encoding="utf-8")


def _handle_update(
    update: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    paths: TgPaths,
) -> int:
    message = update.get("message")
    if isinstance(message, dict) and str(message.get("text") or "").strip() == "/start":
        return _handle_start(message, paths)
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        return _handle_callback(callback, config, paths)
    return 0


def _handle_start(message: Mapping[str, Any], paths: TgPaths) -> int:
    """Первый /start привязывает чат навсегда; чужие — мимо."""
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return 0
    current = bound_chat_id(paths)
    if current is not None and current != int(chat_id):
        return 0
    who = str((message.get("from") or {}).get("first_name") or "")
    if current == int(chat_id):
        send_message("Бот уже привязан к этому чату — жду кнопок.", paths=paths)
        return 1
    _save_chat(paths, int(chat_id), who)
    greeting = "hh-snpr: чат привязан. Карточки вакансий и кнопки решений будут приходить сюда."
    if who:
        greeting = f"{greeting} Привет, {who}."
    send_message(greeting, paths=paths)
    return 1


def _parse_callback(data: str) -> tuple[str, str] | None:
    action, _, vid = str(data or "").partition(":")
    if action in ("go", "no") and vid.isdigit():
        return action, vid
    return None


def _handle_callback(
    callback: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    paths: TgPaths,
) -> int:
    parsed = _parse_callback(str(callback.get("data") or ""))
    cb_id = str(callback.get("id") or "")
    owner = bound_chat_id(paths)
    sender = int((callback.get("from") or {}).get("id") or 0)
    if owner is not None and sender and sender != owner:
        _answer(cb_id, "Не ваш бот")
        return 0
    if parsed is None:
        _answer(cb_id, "Кнопка не распознана")
        return 0
    action, vid = parsed
    message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    if action == "no":
        return _tg_skip(vid, cb_id, message, paths)
    return _tg_approve(vid, cb_id, message, config, paths)


def _find_parked(vid: str, paths: TgPaths) -> dict[str, Any] | None:
    from .sne import ParkedStore

    for record in ParkedStore(paths.parked).load_records():
        if str((record.get("vacancy") or {}).get("id") or "") == vid:
            return record
    return None


def _close_parked(
    paths: TgPaths, vid: str, vacancy: Mapping[str, Any], status: str
) -> None:
    """Закрыть паркованную вакансию: seen + убрать из парковки."""
    from .sne import ParkedStore, SeenStore

    SeenStore(paths.seen).mark_seen(
        vid, name=str(vacancy.get("name") or ""), status=status
    )
    ParkedStore(paths.parked).remove([vid])


def _tg_skip(
    vid: str, cb_id: str, message: Mapping[str, Any], paths: TgPaths
) -> int:
    """«Пропустить»: закрыть вакансию (seen ready) и убрать из парковки."""
    record = _find_parked(vid, paths)
    if record is None:
        _answer(cb_id, "Уже решена ранее")
        _edit_message(message, "Закрыта ранее (не в парковке)")
        return 0
    vacancy = record.get("vacancy") or {}
    _close_parked(paths, vid, vacancy, status="ready")
    _answer(cb_id, "Пропущено")
    _edit_message(message, f"Пропущено · {_vacancy_line(vacancy)}")
    return 1


def _tg_approve(
    vid: str,
    cb_id: str,
    message: Mapping[str, Any],
    config: Mapping[str, Any] | None,
    paths: TgPaths,
) -> int:
    """«Пустить»: тот же путь, что `snpr approve` — карточка, архив,
    дубль, LetterGuard, лимит hh, история пусков."""
    from .sne import approve_lock_alive

    if approve_lock_alive():
        # окно approve занято тем же решением — не дублируем
        _answer(cb_id, "Окно approve открыто — решите там")
        return 0
    record = _find_parked(vid, paths)
    if record is None:
        _answer(cb_id, "Уже решена ранее")
        _edit_message(message, "Закрыта ранее (не в парковке)")
        return 0
    vacancy = record.get("vacancy") or {}
    letter = str(record.get("letter") or "")

    from .hh_tool import SavingHHApplicantTool

    tool = SavingHHApplicantTool()
    api = tool.api_client
    try:
        card = api.get(f"/vacancies/{vid}")
        card = card if isinstance(card, dict) else {}
    except Exception as ex:  # noqa: BLE001
        _answer(cb_id, f"Карточка недоступна: {ex}"[:200])
        return 0
    if card.get("archived"):
        _close_parked(paths, vid, vacancy, status="archived")
        _answer(cb_id, "Вакансия ушла в архив — закрыто")
        _edit_message(message, f"В архиве · {_vacancy_line(vacancy)}")
        return 1

    from .approve import AppliedRecord, AppliedStore
    from .cli import _make_guard, _vacancy_dup_key, _vacancy_numbers

    applied_store = AppliedStore(paths.applied)
    fresh_vacancy = card if card else vacancy
    if _vacancy_dup_key(fresh_vacancy) in applied_store.duplicate_keys():
        _close_parked(paths, vid, vacancy, status="duplicate")
        _answer(cb_id, "Дубль объявления: отклик уже был — закрыто")
        _edit_message(message, f"Дубль · {_vacancy_line(vacancy)}")
        return 1

    violations = _make_guard(config).check(
        letter, extra_numbers=_vacancy_numbers(fresh_vacancy)
    )
    if violations:
        # письмо не проходит гвард: остаётся в парковке, человек решит
        _answer(cb_id, f"LetterGuard: {violations[0]}"[:200])
        return 0

    from .sne import HHNegotiationSender, pick_resume_id

    try:
        resume_id = pick_resume_id(api, "")
    except RuntimeError as ex:
        _answer(cb_id, f"Резюме не найдено: {ex}"[:200])
        return 0
    try:
        HHNegotiationSender(api, resume_id).send(vid, letter)
    except Exception as ex:  # noqa: BLE001
        from hh_applicant_tool.api.errors import LimitExceeded

        if isinstance(ex, LimitExceeded):
            _answer(cb_id, "Дневной лимит откликов hh исчерпан")
        else:
            _answer(cb_id, f"Пуск не прошёл: {ex}"[:200])
        return 0

    score = record.get("score") if isinstance(record.get("score"), dict) else {}
    applied_store.append(
        AppliedRecord(
            vacancy_id=vid,
            vacancy_name=str(vacancy.get("name") or ""),
            employer=str((vacancy.get("employer") or {}).get("name") or ""),
            score=int(score.get("score") or 0),
            letter=letter,
            decision="tg-approve",
            dry_run=False,
        )
    )
    _close_parked(paths, vid, vacancy, status="ready")
    tool._persist()
    _answer(cb_id, "Пуск произведён")
    _edit_message(
        message,
        f"ПУСК ПРОИЗВЁДЁН · скор {score.get('score', '—')}\n"
        f"{_vacancy_line(vacancy)}",
    )
    return 1


# --- CLI ------------------------------------------------------------------------


def run_tg_command(args: argparse.Namespace) -> int:
    """CLI: snpr tg status|test — токен, бот, привязка, пробная карточка."""
    from .cli import _load_toml

    config = _load_toml(str(getattr(args, "config", "") or ""))
    token = bot_token()
    if not token:
        print("TG_BOT_TOKEN не задан: добавьте строку TG_BOT_TOKEN=... в .env")
        print("Бот заводится в @BotFather (2 минуты): /newbot → имя → username.")
        return 1
    if not tg_enabled(config):
        print("Бот выключен: [telegram].enabled = false в config.toml")
        return 1
    try:
        me = _tg_call("getMe", {})
    except Exception as ex:  # noqa: BLE001
        print(f"Telegram не ответил: {ex}", file=sys.stderr)
        return 2
    username = me.get("username") if isinstance(me, dict) else None
    print(f"Бот: @{username or '?'}")
    chat = bound_chat_id()
    if chat is None:
        print("Чат не привязан: отправьте боту /start с вашего Telegram.")
    else:
        print(f"Чат привязан: {chat}")
    if str(getattr(args, "action", "status") or "status") == "test":
        if chat is None:
            print("test: сначала /start от вас боту — привязать чат.")
            return 1
        send_message("hh-snpr: пробная карточка — бот жив, доставка работает.")
        print("Пробное сообщение отправлено.")
    return 0
