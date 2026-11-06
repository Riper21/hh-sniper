"""Тесты HttpClient: кэш/TTL/негатив/прун, троттлинг, retry (ПЛАН §10)."""
from __future__ import annotations

import json
import time
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from snpr.outreach.http import HttpClient, HttpError


def make_client(tmp_path: Path, **kwargs) -> HttpClient:
    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("throttle_sleep", 0)
    kwargs.setdefault("retry_sleeps", (0.0,))
    kwargs.setdefault("backoff_sleeps", (0.0,))
    return HttpClient(tmp_path / "cache", **kwargs)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeHandler:
    """monkeypatch-цель для urlopen: сценарии по URL-префиксу."""

    calls: list[str] = []
    script: dict[str, list] = {}

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.script = {}


def patch_urlopen(monkeypatch, script: dict[str, list], handler=None):
    FakeHandler.reset()
    FakeHandler.script = script

    def fake_urlopen(req, timeout=None, context=None):
        url = req.full_url
        FakeHandler.calls.append(url)
        scenario = FakeHandler.script.get(url)
        if scenario is None:
            # fallback по префиксу
            for prefix, sc in FakeHandler.script.items():
                if url.startswith(prefix):
                    scenario = sc
                    break
        if scenario is None:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        action = scenario.pop(0) if scenario else None
        if isinstance(action, Exception):
            raise action
        if isinstance(action, int):
            raise urllib.error.HTTPError(url, action, "err", {}, None)
        return FakeResponse(action if isinstance(action, bytes) else action.encode())

    monkeypatch.setattr(
        "snpr.outreach.http.urllib.request.urlopen", fake_urlopen
    )
    return fake_urlopen


BODY = json.dumps({"ok": True, "value": 42}).encode()


def test_get_json_cache_hit_no_second_call(tmp_path, monkeypatch):
    patch_urlopen(monkeypatch, {"https://api/x": [BODY]})
    client = make_client(tmp_path)
    first = client.get_json("https://api/x")
    assert first["value"] == 42
    second = client.get_json("https://api/x")
    assert second == first
    assert FakeHandler.calls.count("https://api/x") == 1


def test_get_json_404_negative_cache(tmp_path, monkeypatch):
    patch_urlopen(monkeypatch, {"https://api/miss": [404]})
    client = make_client(tmp_path)
    assert client.get_json("https://api/miss") is None
    assert client.get_json("https://api/miss") is None  # из негативного кэша
    assert FakeHandler.calls.count("https://api/miss") == 1


def test_cache_ttl_expiry(tmp_path, monkeypatch):
    patch_urlopen(monkeypatch, {"https://api/x": [BODY, BODY]})
    client = make_client(tmp_path, ttl_hours=24)
    assert client.get_json("https://api/x")["value"] == 42
    # протухляем кэш вручную
    path = client._cache_path("https://api/x")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["ts"] = (datetime.now() - timedelta(hours=25)).isoformat()
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert client.get_json("https://api/x")["value"] == 42
    assert FakeHandler.calls.count("https://api/x") == 2


def test_retry_on_5xx_then_success(tmp_path, monkeypatch):
    patch_urlopen(
        monkeypatch, {"https://api/flaky": [502, 503, BODY]}
    )
    client = make_client(tmp_path)
    assert client.get_json("https://api/flaky")["value"] == 42
    assert len(FakeHandler.calls) == 3


def test_retry_exhausted_raises(tmp_path, monkeypatch):
    err = urllib.error.URLError("boom")
    patch_urlopen(monkeypatch, {"https://api/dead": [err, err, err]})
    client = make_client(tmp_path)
    with pytest.raises(HttpError):
        client.get_json("https://api/dead")


def test_404_not_retried(tmp_path, monkeypatch):
    patch_urlopen(monkeypatch, {"https://api/one": [404]})
    client = make_client(tmp_path)
    assert client.get_json("https://api/one") is None
    assert len(FakeHandler.calls) == 1


def test_backoff_on_429_then_success(tmp_path, monkeypatch):
    patch_urlopen(monkeypatch, {"https://api/rate": [429, BODY]})
    client = make_client(tmp_path)
    assert client.get_json("https://api/rate")["value"] == 42
    assert len(FakeHandler.calls) == 2


def test_get_text_html(tmp_path, monkeypatch):
    html = b"<html><body>hello</body></html>"
    patch_urlopen(monkeypatch, {"https://site/page": [html]})
    client = make_client(tmp_path)
    assert client.get_text("https://site/page") == "<html><body>hello</body></html>"


def test_post_json_no_cache(tmp_path, monkeypatch):
    responses = []
    patch_urlopen(monkeypatch, {})
    def fake_urlopen(req, timeout=None, context=None):
        FakeHandler.calls.append(req.full_url)
        responses.append(req.data)
        return FakeResponse(b'{"done": true}')

    monkeypatch.setattr(
        "snpr.outreach.http.urllib.request.urlopen", fake_urlopen
    )
    client = make_client(tmp_path)
    assert client.post_json("https://api/echo", {"a": 1}) == {"done": True}
    assert client.post_json("https://api/echo", {"a": 1}) == {"done": True}
    assert len(FakeHandler.calls) == 2  # без кэша — оба раза в сеть
    assert json.loads(responses[0]) == {"a": 1}


def test_prune_removes_oldest(tmp_path, monkeypatch):
    patch_urlopen(monkeypatch, {})
    client = make_client(tmp_path, use_cache=True)
    for i in range(5):
        p = client.cache_dir / f"f{i}.json"
        p.write_text(json.dumps({"data": i, "ts": "2000-01-01T00:00:00"}), encoding="utf-8")
        time.sleep(0.01)
    removed = client.prune(max_files=2)
    assert removed == 3
    remaining = sorted(p.name for p in client.cache_dir.glob("*.json"))
    assert remaining == ["f3.json", "f4.json"]  # старейшие удалены


def test_token_bearer_header(tmp_path, monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen.update(dict(req.headers))
        return FakeResponse(b"{}")

    monkeypatch.setattr(
        "snpr.outreach.http.urllib.request.urlopen", fake_urlopen
    )
    client = make_client(tmp_path, token="tok123", use_cache=False)
    client.get_json("https://api/whoami")
    assert seen.get("Authorization") == "Bearer tok123"


def test_insecure_tls_selects_unverified(tmp_path):
    client = make_client(tmp_path, insecure_tls=True)
    assert client._ssl_mode == "unverified"
    assert client._ctx_custom is not None
    assert client._ctx_custom.verify_mode.value == 0  # ssl.CERT_NONE


def test_unknown_ssl_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        make_client(tmp_path, ssl_mode="bogus")


def test_throttle_sleeps_between_calls(tmp_path, monkeypatch):
    patch_urlopen(monkeypatch, {})
    client = make_client(
        tmp_path,
        min_interval=0.05,
        throttle_every=2,
        throttle_sleep=0.01,
        use_cache=False,
    )
    t0 = time.monotonic()
    for _ in range(2):
        try:
            client.get_json("https://api/t")
        except urllib.error.HTTPError:
            pass
    assert time.monotonic() - t0 >= 0.05
