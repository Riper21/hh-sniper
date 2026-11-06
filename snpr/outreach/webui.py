"""Локальный UI outreach: http.server + одна HTML-страница (ПЛАН §7).

Философия проекта: stdlib-first, ноль новых зависимостей, один пользователь.
Bind строго 127.0.0.1 (конфиг), запись в сторы — через JsonlStore.upsert
с атомарностью tmp-replace; UI и CLI не пишут одновременно (лок-файл
по аналогии с sne.lock).

Критерий приёмки: полный цикл верификации карточки ≤ 1 мин
(ПРОЕКТ_ПРОГОНА §6.3).
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .base import stats as base_stats
from .pipeline import Paths
from .store import JsonlStore

_STATIC = Path(__file__).resolve().parent / "static" / "index.html"
_VERIFY_STATUSES = ("verified", "rejected")
_CAMPAIGN_EVENTS = ("queued", "sent", "replied", "bounced", "skip")

# сторы снайпера: scores.jsonl и scheduler.log лежат в data/ (рядом с
# data/outreach/) — UI читает их только на чтение, конвенция общая
_SNIPER_DATA = Path(__file__).resolve().parent.parent.parent / "data"
SCORES_JSONL = _SNIPER_DATA / "scores.jsonl"
SCHEDULER_LOG = _SNIPER_DATA / "scheduler.log"
MAX_TICKS_DEFAULT = 50


def _make_draft_ai():
    """LLM-клиент для черновиков из резюме (SNE_AI_* из .env).

    None — кредов нет: черновики пойдут эвристикой.
    """
    import os

    base = os.environ.get("SNE_AI_BASE_URL", "")
    model = os.environ.get("SNE_AI_MODEL", "")
    if not (base and model):
        return None
    try:
        from hh_applicant_tool.ai.openai import ChatOpenAI

        return ChatOpenAI(
            base_url=base,
            model=model,
            api_key=os.environ.get("SNE_AI_API_KEY") or None,
            temperature=0.2,
            max_completion_tokens=4000,
            timeout=120.0,
        )
    except Exception:  # noqa: BLE001 — нет тулкита/кредов
        return None


def load_scores(params: dict[str, str]) -> dict:
    """Скоры снайпера (data/scores.jsonl) + карьер-вакансии outreach
    (career_managers.jsonl со score после `snpr outreach score`).

    Фильтры: verdict (pass/maybe/target), min_score, limit (последние N).
    Каждой записи добавляем ссылку на вакансию.
    """
    records: list[dict] = []

    # 1. карьер-вакансии (с найм-менеджерами из базы контактов)
    career_store = _SNIPER_DATA / "outreach" / "career_managers.jsonl"
    if career_store.exists():
        for line in career_store.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or not rec.get("score"):
                continue
            records.append(
                {
                    "vacancy_id": rec.get("vacancy_id"),
                    "vacancy_url": rec.get("vacancy_url"),
                    "name": rec.get("vacancy_name"),
                    "employer": rec.get("company"),
                    "score": rec.get("score"),
                    "verdict": rec.get("verdict"),
                    "reasons": rec.get("score_reasons") or [],
                    "ts": rec.get("scored_at"),
                    "source": "outreach",
                    "manager": rec.get("manager_name") or rec.get(
                        "manager_email"
                    ),
                }
            )

    # 2. скоры снайпера (hh-канал)
    if SCORES_JSONL.exists():
        for line in SCORES_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if not rec.get("vacancy_url"):
                rec["vacancy_url"] = (
                    f"https://hh.ru/vacancy/{rec.get('vacancy_id')}"
                    if rec.get("vacancy_id")
                    else None
                )
            rec.setdefault("source", "hh")
            records.append(rec)

    verdict = params.get("verdict")
    if verdict:
        records = [r for r in records if r.get("verdict") == verdict]
    source = params.get("source")
    if source:
        records = [r for r in records if r.get("source") == source]
    min_score = params.get("min_score")
    if min_score:
        try:
            floor = int(min_score)
            records = [r for r in records if int(r.get("score") or 0) >= floor]
        except ValueError:
            pass
    limit = params.get("limit")
    try:
        tail_n = int(limit) if limit else 200
    except ValueError:
        tail_n = 200
    tail = records[-tail_n:]
    return {"scores": tail, "total": len(records)}


_TICK_HEADER = re.compile(r"^=====\s*(.+?)\s*=====")
_TICK_SUMMARY = re.compile(r"^Итог: (.+)$")


def load_ticks(params: dict[str, str]) -> dict:
    """Последние такты снайпера из scheduler.log: итоги «что откинули».

    Возврат: список тактов (время, итоговая строка, строки скоринга).
    """
    if not SCHEDULER_LOG.exists():
        return {"ticks": []}
    try:
        count = int(params.get("limit") or MAX_TICKS_DEFAULT)
    except ValueError:
        count = MAX_TICKS_DEFAULT
    ticks: list[dict] = []
    current: dict | None = None
    # лог пишется постоянно; читаем его целиком (3-5МБ — ок для localhost)
    text = SCHEDULER_LOG.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        header = _TICK_HEADER.match(line.strip())
        if header:
            if current is not None:
                ticks.append(current)
            current = {"at": header.group(1), "summary": "", "scoring": []}
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Итог:"):
            current["summary"] = stripped[5:].strip()
        elif stripped.startswith("[") and ")" in stripped and " → " in stripped:
            # строки вида «[139190268] 28 (pass) Название — Ozon → …»
            current["scoring"].append(stripped)
    if current is not None:
        ticks.append(current)
    # только такты с итогами (пустые такты неинтересны)
    ticks = [t for t in ticks if t["summary"] or t["scoring"]]
    return {"ticks": ticks[-count:]}




class _Lock:
    """Файловый лок записи (UI vs CLI): переиспользуем конвенцию sne.lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd = None

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = self.path.open("a", encoding="utf-8")
            import msvcrt

            msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
            return self
        except (OSError, ImportError, AttributeError):
            # не-Windows или залочено: proceed (UI однопользовательский,
            # лок — страховка от параллельного CLI-шага)
            return self

    def __exit__(self, *args):
        if self._fd is not None:
            try:
                import msvcrt

                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, ImportError):
                pass
            self._fd.close()
        return False


def make_handler(paths: Paths) -> type:
    class OutreachUIHandler(BaseHTTPRequestHandler):
        server_version = "snpr-outreach-ui/1.0"

        def log_message(self, fmt, *args):  # тихий лог в консоль сервера
            print(f"[ui] {self.address_string()} {fmt % args}")

        # ------------------------------------------------------------ helpers

        def _json(self, payload: object, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, message: str, code: int = 400) -> None:
            self._json({"error": message}, code)

        def _contact(self, key: str) -> dict | None:
            from urllib.parse import unquote

            key = unquote(key)
            for contact in paths.base.load():
                if str(contact.get("contact_key")) == key:
                    return contact
            return None

        # -------------------------------------------------------------- GET

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path
            if route in ("/", "/index.html"):
                body = _STATIC.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/api/stats":
                if not paths.base.path.exists():
                    self._json({"total": 0, "by_channel": {}, "by_role": {}, "by_source": {}, "verified": 0, "max_age_days": 0, "companies": []})
                    return
                self._json(base_stats(paths.base))
                return
            if route == "/api/contacts":
                params = {
                    k: v[0]
                    for k, v in parse_qs(parsed.query).items()
                }
                contacts = paths.base.load() if paths.base.path.exists() else []
                contacts = _filter_contacts(contacts, params)
                contacts.sort(
                    key=lambda c: (
                        str(c.get("company") or "яя"),
                        -int(c.get("confidence") == "high"),
                    )
                )
                self._json({"contacts": contacts})
                return
            match = re.fullmatch(r"/api/contacts/([^/]+)", route)
            if match:
                from urllib.parse import unquote

                contact = self._contact(unquote(match.group(1)))
                if contact is None:
                    self._error("контакт не найден", 404)
                    return
                events = paths.events.events_for(unquote(match.group(1)))
                self._json({"contact": contact, "events": events})
                return
            if route == "/api/scores":
                params = {
                    k: v[0] for k, v in parse_qs(parsed.query).items()
                }
                self._json(load_scores(params))
                return
            if route == "/api/ticks":
                params = {
                    k: v[0] for k, v in parse_qs(parsed.query).items()
                }
                self._json(load_ticks(params))
                return
            if route == "/api/settings":
                from .settings import read_settings

                self._json(read_settings())
                return
            if route == "/api/job":
                from .job_control import job_snapshot

                self._json(job_snapshot())
                return
            if route == "/api/job/open":
                # открыть UI в дефолтном браузере (кнопка из блока)
                from .job_control import open_in_browser

                self._json(open_in_browser())
                return

        # ------------------------------------------------------------- POST

        def _guard_request(self) -> bool:
            """Anti-CSRF: POST только с нашего Origin/Host.

            bind 127.0.0.1 не защищает от запроса ИЗ браузера жертвы
            (simple request text/plain без preflight) и DNS-rebinding
            (Host не валидировался). Локальный UI шлёт Origin
            http://127.0.0.1:port; curl/CLI — без Origin, но с корректным
            Host — пропускаем.
            """
            host = (self.headers.get("Host") or "").strip().lower()
            ok_hosts = {
                f"127.0.0.1:{self.server.server_address[1]}",
                f"localhost:{self.server.server_address[1]}",
            }
            if host and host not in ok_hosts:
                return False
            origin = (self.headers.get("Origin") or "").strip().lower()
            if origin and not any(
                origin == f"http://{h}" for h in ok_hosts
            ):
                return False
            return True

        def do_POST(self):  # noqa: N802
            from urllib.parse import unquote

            if not self._guard_request():
                self._error("запрос не от локального UI", 403)
                return
            parsed = urlparse(self.path)
            route = parsed.path
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._error("битый Content-Length")
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._error("битый JSON")
                return
            if route == "/api/settings":
                # мастер настройки: применение ключей в config.toml
                # (whitelist, бэкап, roundtrip — см. settings.py)
                from .settings import write_settings

                updates = {
                    k: v
                    for k, v in payload.items()
                    if not str(k).startswith("_")
                }
                result = write_settings(updates)
                if not result.get("ok"):
                    self._error(str(result.get("error")), 400)
                    return
                self._json(result)
                return
            if route == "/api/settings/resume-draft":
                # резюме (текст) → черновики профилей/стек-ключей.
                # Сначала LLM (грейд/вилка/анти-паттерны), недоступен —
                # эвристика (контакты/титул/навыки-секция).
                from .settings import profile_from_resume, profile_from_resume_llm

                resume = str(payload.get("resume") or "")
                if not resume.strip():
                    self._error("пустое резюме")
                    return
                draft = None
                engine = "heuristic"
                ai = _make_draft_ai()
                if ai is not None:
                    draft = profile_from_resume_llm(resume[:200000], ai)
                    if draft is not None:
                        engine = "llm"
                if draft is None:
                    draft = profile_from_resume(resume[:200000])
                draft["_engine"] = engine
                self._json(draft)
                return
            if route == "/api/job":
                # блок управления джобой: on/off/run (шедулер как в CLI)
                from .job_control import job_toggle

                action = str(payload.get("action") or "")
                result = job_toggle(action)
                if not result.get("ok"):
                    self._error(str(result.get("message")), 400)
                    return
                self._json(result)
                return
            match = re.fullmatch(r"/api/contacts/([^/]+)/verify", route)
            if match:
                status = str(payload.get("status") or "")
                if status not in _VERIFY_STATUSES:
                    self._error("status должен быть verified|rejected")
                    return
                self._verify(unquote(match.group(1)), status, str(payload.get("note") or ""))
                return
            match = re.fullmatch(r"/api/contacts/([^/]+)/campaign", route)
            if match:
                event = str(payload.get("event") or "")
                if event not in _CAMPAIGN_EVENTS:
                    self._error(f"event должен быть одним из {_CAMPAIGN_EVENTS}")
                    return
                self._campaign(unquote(match.group(1)), event, str(payload.get("note") or ""))
                return
            self._error("не найдено", 404)

        # ------------------------------------------------------------ actions

        def _verify(self, key: str, status: str, note: str) -> None:
            with _Lock(paths.root / "ui.lock"):
                base = JsonlStore(paths.base.path, "contact_key")
                contact = self._contact(key)
                if contact is None:
                    self._error("контакт не найден", 404)
                    return
                from datetime import datetime

                contact["verified"] = status
                contact["verified_at"] = datetime.now().isoformat(timespec="seconds")
                if note:
                    contact["verified_note"] = note
                base.upsert(contact)
            self._json({"ok": True, "contact_key": key, "verified": status})

        def _campaign(self, key: str, event: str, note: str) -> None:
            contact = self._contact(key)
            if contact is None:
                self._error("контакт не найден", 404)
                return
            record = paths.events.append(key, event, note)
            with _Lock(paths.root / "ui.lock"):
                base = JsonlStore(paths.base.path, "contact_key")
                updated = dict(contact)
                campaign = dict(updated.get("campaign") or {})
                campaign["status"] = event
                campaign["last_event_at"] = record["at"]
                updated["campaign"] = campaign
                base.upsert(updated)
            self._json({"ok": True, "event": record})

    return OutreachUIHandler


def _filter_contacts(
    contacts: list[dict], params: dict[str, str]
) -> list[dict]:
    out = contacts
    if params.get("company"):
        wanted = params["company"].lower()
        out = [c for c in out if wanted in str(c.get("company") or "").lower()]
    if params.get("channel"):
        out = [c for c in out if c.get("channel") == params["channel"]]
    if params.get("role"):
        out = [c for c in out if c.get("role") == params["role"]]
    if params.get("confidence"):
        out = [c for c in out if c.get("confidence") == params["confidence"]]
    if params.get("verified") == "1":
        out = [c for c in out if c.get("verified")]
    if params.get("stale_days"):
        from datetime import datetime

        try:
            stale = int(params["stale_days"])
            now = datetime.now()
            fresh: list[dict] = []
            for contact in out:
                checked = contact.get("checked_at")
                if not checked:
                    continue
                try:
                    age = (now - datetime.fromisoformat(str(checked))).days
                except ValueError:
                    continue
                if age > stale:
                    fresh.append(contact)
            out = fresh
        except ValueError:
            pass
    limit = params.get("limit")
    if limit:
        try:
            out = out[: int(limit)]
        except ValueError:
            pass
    return out


def serve(paths: Paths, host: str = "127.0.0.1", port: int = 8765) -> int:
    """Запуск сервера (блокирующий). Ctrl+C — выход."""
    handler = make_handler(paths)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"outreach UI: http://{host}:{port} (Ctrl+C — выход)")
    print(f"данные: {paths.root}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\noutreach UI: остановлен")
    finally:
        httpd.server_close()
    return 0
