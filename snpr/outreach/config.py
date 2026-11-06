"""Чтение секции [outreach] из config.toml (ПЛАН §3.5).

Дефолты консервативны: модуль выключен, TLS верифицирован, данные в
data/outreach/ (gitignored — политика «данные под замком»).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "data_dir": "data/outreach",
    # 06.11: роль конфигурируема — словарь ключей для фильтра вакансий.
    # Пусто = ML-дефолт (ML_QUERIES + рус. фразы в rank.py).
    # Пресеты см. ПЛАН_ИНТЕГРАЦИИ/ИНСТРУКЦИЯ §8a:
    #   QA: ["qa", "aqa", "тестировщик", "тестирование", "quality",
    #        "selenium", "pytest", "playwright", "автотест"]
    #   фронт: ["frontend", "фронтенд", "react", "vue", "angular", ...]
    "role_keywords": [],
}

HTTP_DEFAULTS: dict[str, Any] = {
    "cache_ttl_hours": 24,
    "negative_ttl_hours": 24,
    "ssl_mode": "default",
    "insecure_tls": False,
    "cache_max_files": 5000,
}

GITHUB_DEFAULTS: dict[str, Any] = {
    "token_env": "GITHUB_TOKEN",
    "min_interval": 1.2,
    "org_similar_min": 0.5,
}

HABR_DEFAULTS: dict[str, Any] = {
    "pages_default": 5,
    "profile_delay": 0.7,
    "list_delay": 0.8,
    "hubs": ["machine_learning", "artificial_intelligence"],
}

HH_DEFAULTS: dict[str, Any] = {
    "enabled": True,
}

CAREER_DEFAULTS: dict[str, Any] = {
    "adapters": ["alfa", "huntflow", "jobtoo", "sber", "avito", "ozon"],
}

TRIGGER_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "notify_only": True,
}

UI_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "host": "127.0.0.1",
    "port": 8765,
}


def outreach_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Секция [outreach] с дефолтами. config — весь config.toml."""
    section = dict((config or {}).get("outreach", {}) or {})
    merged = {**DEFAULTS}
    for key in ("enabled", "data_dir", "role_keywords"):
        if key in section:
            merged[key] = section[key]
    for sub, defaults in (
        ("http", HTTP_DEFAULTS),
        ("github", GITHUB_DEFAULTS),
        ("habr", HABR_DEFAULTS),
        ("hh", HH_DEFAULTS),
        ("career", CAREER_DEFAULTS),
        ("trigger", TRIGGER_DEFAULTS),
        ("ui", UI_DEFAULTS),
    ):
        merged[sub] = {**defaults, **(section.get(sub, {}) or {})}
    return merged


def data_dir(config: dict[str, Any] | None, project_root: Path | None = None) -> Path:
    """Каталог данных outreach: data_dir из конфига относительно корня.

    Принимает либо сырой config.toml, либо уже смерженный outreach_config().
    """
    raw_value: Any
    if config and "http" in config and "data_dir" in config:
        # уже смерженный вид из outreach_config()
        raw_value = config["data_dir"]
    else:
        raw_value = outreach_config(config)["data_dir"]
    raw = Path(str(raw_value))
    if raw.is_absolute():
        return raw
    root = project_root or Path.cwd()
    return root / raw


def github_token(cfg: dict[str, Any]) -> str:
    """PAT из env (переменная задаётся token_env)."""
    env_name = str(cfg.get("github", {}).get("token_env", "GITHUB_TOKEN"))
    return os.environ.get(env_name, "")
