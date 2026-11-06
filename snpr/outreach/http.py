"""Единый HTTP-клиент outreach-модуля.

Порт эталонного класса GH из research/outreach_mvp/run.py (кэш sha1(url)[:16]
+ TTL 24 ч + негативный кэш 404 + лестницы повторов), обобщённый на все каналы.
Сводит три копии _ssl_context и пять копий retry-лестниц исследования в одну
точку (ПЛАН_ИНТЕГРАЦИИ_OUTREACH.md §3.2).

SSL-режимы:
- "default"       — стандартный контекст (эмпирика прогонов: habr работает так);
- "windows_store" — доверие корп. MITM-CA из хранилища Windows с чередованием
                    контекстов при SSLError (обход из run.py, осознанный режим);
- "unverified"    — без верификации; только через явный конфиг insecure_tls
                    (в research был включён вшитым в career_managers.py — баг).
"""
from __future__ import annotations

import hashlib
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

DEFAULT_RETRY_SLEEPS = (5.0, 15.0, 45.0)
DEFAULT_BACKOFF_SLEEPS = (30.0, 60.0, 90.0)


def _default_ssl_context() -> ssl.SSLContext:
    """Стандартный контекст: системные CA, верификация включена."""
    return ssl.create_default_context()


def _windows_store_ssl_context() -> ssl.SSLContext | None:
    """Контекст с доверием корп. MITM-CA из хранилища Windows (порт run.py).

    06.11 фикс: протухшие промежуточные (LE R3/ISRG X2) в Windows-сторе
    ломали верификацию свежих цепочек — OpenSSL выбирал expired-путь
    («certificate has expired» на валидном leaf jobtoo.ru). Теперь в сторе
    грузятся ТОЛЬКО действующие по датам серты.
    Возвращает None вне Windows или при пустом хранилище — вызывающий код
    откатывается на чередование со стандартным контекстом.
    """
    if sys.platform != "win32" or not hasattr(ssl, "enum_certificates"):
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        loaded = 0
        now = _x509_now()
        for store in ("ROOT", "CA"):
            try:
                for der, enc_type, _store in ssl.enum_certificates(store):
                    if enc_type != "x509_asn":
                        continue
                    if _cert_expired(der, now):
                        continue  # протухшие промежуточные не подсовываем
                    try:
                        ctx.load_verify_locations(cadata=_pem_from_der(der))
                        loaded += 1
                    except ssl.SSLError:
                        pass
            except OSError:
                continue
        if not loaded:
            return None
        return ctx
    except Exception:  # noqa: BLE001 — осознанный откат на дефолт
        return None


def _x509_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _cert_expired(der: bytes, now) -> bool:
    """Даты действия DER-серта; битый серт считаем протухшим (skip)."""
    try:
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        return cert.not_valid_after_utc < now
    except Exception:  # noqa: BLE001 — cryptography нет или серт битый
        return True


def _pem_from_der(der: bytes) -> str:
    """DER → PEM (для load_verify_locations cadata)."""
    import base64

    b64 = base64.encodebytes(der).decode("ascii").strip()
    lines = "\n".join(b64.splitlines())
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n"


def _unverified_ssl_context() -> ssl.SSLContext:
    """Неверифицированный контекст — ТОЛЬКО через явный конфиг insecure_tls."""
    return ssl._create_unverified_context()  # noqa: SLA001 — инкапсулировано


class HttpError(RuntimeError):
    """Неустранимая ошибка HTTP после всех повторов."""


class HttpClient:
    """Кэширующий, троттлящий, ретрящийся GET/POST клиент (порт GH из run.py).

    Кэш: cache_dir/<sha1(url)[:16]>.json, формат {"data": …, "ts": ISO}.
    404 кэшируется как {"data": null} — негативный кэш.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        token: str | None = None,
        auth_header: str | None = "Authorization: Bearer {token}",
        extra_headers: dict[str, str] | None = None,
        user_agent: str = USER_AGENT,
        ttl_hours: float = 24.0,
        negative_ttl_hours: float = 24.0,
        min_interval: float = 0.5,
        throttle_every: int = 3,
        throttle_sleep: float = 1.2,
        retry_sleeps: tuple[float, ...] = DEFAULT_RETRY_SLEEPS,
        backoff_sleeps: tuple[float, ...] = DEFAULT_BACKOFF_SLEEPS,
        ssl_mode: str = "default",
        insecure_tls: bool = False,
        connection_close: bool = False,
        use_cache: bool = True,
    ) -> None:
        if insecure_tls:
            ssl_mode = "unverified"
        self.cache_dir = Path(cache_dir)
        self.token = token
        self._auth_header = auth_header
        self._extra_headers = dict(extra_headers or {})
        self.user_agent = user_agent
        self.ttl = timedelta(hours=ttl_hours)
        self.negative_ttl = timedelta(hours=negative_ttl_hours)
        self.min_interval = min_interval
        self.throttle_every = max(1, throttle_every)
        self.throttle_sleep = throttle_sleep
        self.retry_sleeps = tuple(retry_sleeps)
        self.backoff_sleeps = tuple(backoff_sleeps)
        self.connection_close = connection_close
        self.use_cache = use_cache
        self._calls_since_throttle = 0
        self._last_call_ts = 0.0
        self._ssl_mode = ssl_mode
        self._ctx_custom = self._init_context(ssl_mode)
        self._ctx_default = _default_ssl_context()
        if use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ ctx

    def _init_context(self, mode: str) -> ssl.SSLContext | None:
        if mode == "unverified":
            return _unverified_ssl_context()
        if mode == "windows_store":
            return _windows_store_ssl_context()
        if mode != "default":
            raise ValueError(f"неизвестный ssl_mode: {mode!r}")
        return None

    def _contexts(self) -> list[ssl.SSLContext]:
        """Контексты для чередования: кастомный первым (если есть)."""
        if self._ctx_custom is not None:
            return [self._ctx_custom, self._ctx_default]
        return [self._ctx_default]

    # ---------------------------------------------------------------- cache

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    def _cache_get(self, url: str) -> Any | None | bool:
        """Возврат: значение (hit), None (hit-негатив: 404), False (промах).

        06.11 P0-фикс: мигрированный кэш research пишет aware-timestamps
        («+00:00»), интеграция — naive; вычитание naive-aware роняло
        TypeError ПЕРВЫЙ ЖЕ сетевой прогон после migrate (краш пробивался
        сквозь except: ловились только JSONDecodeError/OSError). Теперь
        ts нормализуется к naive, битые записи = промах.
        """
        if not self.use_cache:
            return False
        path = self._cache_path(url)
        if not path.exists():
            return False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(raw.get("ts", "2000-01-01T00:00:00"))
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            return False
        if ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        ttl = self.negative_ttl if raw.get("data") is None else self.ttl
        if datetime.now() - ts > ttl:
            return False
        return raw.get("data")

    def _cache_put(self, url: str, data: Any) -> None:
        if not self.use_cache:
            return
        path = self._cache_path(url)
        payload = {"data": data, "ts": datetime.now().isoformat(timespec="seconds")}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(path)

    def prune(self, max_files: int = 5000) -> int:
        """Удаление старейших файлов кэша сверх max_files. Возврат: удалено."""
        if not self.cache_dir.exists():
            return 0
        files = sorted(
            self.cache_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for path in files[max_files:]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    # ------------------------------------------------------------- requests

    def _headers(self, more: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent}
        if self.token and self._auth_header:
            headers[self._auth_header.split(":", 1)[0]] = (
                self._auth_header.format(token=self.token).split(" ", 1)[-1]
                if " " in self._auth_header
                else self.token
            )
        headers.update(self._extra_headers)
        if self.connection_close:
            headers["Connection"] = "close"
        if more:
            headers.update(more)
        return headers

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self.min_interval - (now - self._last_call_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_call_ts = time.monotonic()
        self._calls_since_throttle += 1
        if self._calls_since_throttle >= self.throttle_every:
            self._calls_since_throttle = 0
            if self.throttle_sleep > 0:
                time.sleep(self.throttle_sleep)

    def _open(self, url: str, headers: dict[str, str], data: bytes | None):
        req = urllib.request.Request(url, headers=headers, data=data)
        last_error: Exception | None = None
        contexts = self._contexts()
        attempt_ctx = 0
        sleeps: list[float] = list(self.retry_sleeps)
        # ретраи по лестнице retry_sleeps; 403/429 досыпают backoff-лестницу
        # в очередь (порт run.py: 429/403 — грубые сигналы бана, ждём дольше);
        # на SSLError — чередуем контексты (MITM-режим)
        max_attempts = len(self.retry_sleeps) + len(self.backoff_sleeps) + 1
        attempt = 0
        while True:
            self._throttle()
            try:
                return urllib.request.urlopen(
                    req, timeout=30, context=contexts[attempt_ctx % len(contexts)]
                )
            except urllib.error.HTTPError as ex:
                if ex.code == 404:
                    raise
                if ex.code in (403, 429):
                    sleeps.extend(self.backoff_sleeps)
                    sleeps = sleeps[: max(0, max_attempts - attempt)]
                last_error = ex
            except (urllib.error.URLError, ssl.SSLError, OSError) as ex:
                if isinstance(getattr(ex, "args", [None])[0], ssl.SSLError) or isinstance(
                    ex, ssl.SSLError
                ):
                    attempt_ctx += 1
                last_error = ex
            attempt += 1
            if attempt >= max_attempts:
                break
            pause = sleeps.pop(0) if sleeps else self.retry_sleeps[-1]
            if pause > 0:
                time.sleep(pause)
        raise HttpError(f"{url}: исчерпаны повторы ({last_error})")

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        ttl: timedelta | None = None,
    ) -> Any:
        """GET → JSON. 404 кэшируется как None (негативный кэш)."""
        cached = self._cache_get(url)
        if cached is not False:
            return cached
        try:
            resp = self._open(url, self._headers(headers), None)
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                self._cache_put(url, None)
                return None
            raise
        with resp:
            data = json.loads(resp.read().decode("utf-8"))
        self._cache_put(url, data)
        return data

    def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        ttl: timedelta | None = None,
    ) -> str | None:
        """GET → текст (HTML). 404 → None с негативным кэшем."""
        cached = self._cache_get(url)
        if cached is not False:
            return cached
        try:
            resp = self._open(url, self._headers(headers), None)
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                self._cache_put(url, None)
                return None
            raise
        with resp:
            text = resp.read().decode("utf-8", errors="replace")
        self._cache_put(url, text)
        return text

    def post_json(
        self,
        url: str,
        payload: dict[str, Any] | None,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """POST JSON (без кэша). Возврат: распарсенный JSON ответа."""
        body = json.dumps(payload or {}).encode("utf-8")
        all_headers = self._headers(headers)
        all_headers.setdefault("Content-Type", "application/json")
        resp = self._open(url, all_headers, body)
        with resp:
            return json.loads(resp.read().decode("utf-8"))
