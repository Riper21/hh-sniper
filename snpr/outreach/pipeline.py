"""Оркестратор шагов outreach + пути сторов (ПЛАН §3.1/§5).

Все сторы в data/outreach/ (gitignored, «данные под замком»):
orgs/people (GitHub), habr_crawl/habr_matches/bridge (Habr),
career_managers/jobtoo_career_sites (карьер-сайты), hh_managers (hh),
outreach (единая база), events (CampaignLog).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import github_token, outreach_config
from .store import CampaignLog, JsonlStore

# ожидаемые счётчики research-прогонов (миграция сверяет, ПЛАН §5.3)
RESEARCH_COUNTS = {
    "orgs.jsonl": 36,
    "people.jsonl": 100,
    "habr_crawl_pilot.jsonl": 893,
    "gh_enrich.jsonl": 45,
    "hh_managers.jsonl": 38,
    "career_managers.jsonl": 19,
    "jobtoo_career_sites.jsonl": 707,
    "bridge_habr.jsonl": 53,
    "habr_matches.jsonl": 35,
}


class Paths:
    """Централизованные пути сторов/отчётов outreach."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.cache = root / "cache"
        self.reports = root / "reports"

    # справочники каналов
    @property
    def orgs(self) -> JsonlStore:
        return JsonlStore(self.root / "orgs.jsonl", "name")

    @property
    def people(self) -> JsonlStore:
        return JsonlStore(self.root / "people.jsonl", "person_key")

    @property
    def habr_crawl(self) -> JsonlStore:
        return JsonlStore(self.root / "habr_crawl_pilot.jsonl", "login")

    @property
    def habr_matches(self) -> JsonlStore:
        return JsonlStore(self.root / "habr_matches.jsonl", "github_login")

    @property
    def gh_enrich(self) -> JsonlStore:
        return JsonlStore(self.root / "gh_enrich.jsonl", "github_login")

    @property
    def bridge(self) -> JsonlStore:
        return JsonlStore(self.root / "bridge_habr.jsonl", "email")

    @property
    def career(self) -> JsonlStore:
        return JsonlStore(self.root / "career_managers.jsonl", "vacancy_id")

    @property
    def jobtoo_sites(self) -> JsonlStore:
        return JsonlStore(self.root / "jobtoo_career_sites.jsonl", "career_domain")

    @property
    def hh(self) -> JsonlStore:
        return JsonlStore(self.root / "hh_managers.jsonl", "vacancy_id")

    # единая база + кампания
    @property
    def base(self) -> JsonlStore:
        return JsonlStore(self.root / "outreach.jsonl", "contact_key")

    @property
    def events(self) -> CampaignLog:
        return CampaignLog(self.root / "events.jsonl")

    # вход
    @property
    def companies_tsv(self) -> Path:
        return self.root / "companies.tsv"

    @property
    def employer_aliases(self) -> Path:
        return self.root / "employer_aliases.json"

    @property
    def habr_aliases(self) -> Path:
        return self.root / "habr_aliases.json"


def make_http(paths: Paths, cfg: dict[str, Any]) -> "object":
    """HttpClient из [outreach.http] + токен GitHub из env."""
    from .http import HttpClient

    http_cfg = cfg.get("http", {})
    gh_cfg = cfg.get("github", {})
    return HttpClient(
        paths.cache,
        token=github_token(cfg) or None,
        ttl_hours=float(http_cfg.get("cache_ttl_hours", 24)),
        negative_ttl_hours=float(http_cfg.get("negative_ttl_hours", 24)),
        min_interval=float(gh_cfg.get("min_interval", 1.2)),
        ssl_mode=str(http_cfg.get("ssl_mode", "default")),
        insecure_tls=bool(http_cfg.get("insecure_tls", False)),
    )


def prune_cache(paths: Paths, cfg: dict[str, Any]) -> int:
    """Прунинг кэша (в research 642 файла росли вечно; ПЛАН §3.2)."""
    from .http import HttpClient

    http_cfg = cfg.get("http", {})
    client = HttpClient(
        paths.cache,
        ttl_hours=0,
        min_interval=0,
        throttle_sleep=0,
    )
    return client.prune(max_files=int(http_cfg.get("cache_max_files", 5000)))


def outreach_status(config: dict[str, Any] | None) -> dict[str, Any]:
    """Оффлайн-сводка: счётчики сторов, свежесть, конфиг (CLI status)."""
    cfg = outreach_config(config)
    from .base import stats
    from .config import data_dir

    paths = Paths(data_dir(cfg))
    out: dict[str, Any] = {
        "enabled": cfg.get("enabled"),
        "data_dir": str(paths.root),
        "stores": {},
    }
    for name, store in (
        ("orgs", paths.orgs),
        ("people", paths.people),
        ("habr_crawl", paths.habr_crawl),
        ("habr_matches", paths.habr_matches),
        ("gh_enrich", paths.gh_enrich),
        ("bridge", paths.bridge),
        ("career", paths.career),
        ("jobtoo_sites", paths.jobtoo_sites),
        ("hh", paths.hh),
    ):
        records = store.load()
        checked = [
            r.get("checked_at")
            for r in records
            if r.get("checked_at")
        ]
        out["stores"][name] = {
            "records": len(records),
            "last_checked": max(checked) if checked else None,
        }
    if paths.base.path.exists():
        out["base"] = stats(paths.base)
    return out
