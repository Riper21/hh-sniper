"""CLI outreach-модуля: подкоманды `python -m snpr outreach …` (§5.1)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from .config import data_dir, github_token, outreach_config
from .pipeline import Paths, outreach_status, prune_cache


def _paths(args: argparse.Namespace) -> tuple[Paths, dict[str, Any]]:
    config = _load_config(args)
    cfg = outreach_config(config)
    return Paths(data_dir(cfg, _project_root())), cfg


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _load_config(args: argparse.Namespace) -> dict[str, Any]:
    import tomllib

    # SUPPRESS-семантика: атрибута config может не быть (наследуем
    # от родительского парсера, если был), дефолт — snpr/config.toml
    path = getattr(args, "config", None) or (
        getattr(args, "outreach_config", None)
        or (_project_root() / "snpr" / "config.toml")
    )
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _out(args: argparse.Namespace, message: str) -> None:
    if not getattr(args, "quiet", False):
        print(message)


def cmd_seed(args: argparse.Namespace) -> int:
    from .github import seed

    paths, _cfg = _paths(args)
    count = seed(paths.orgs, paths.companies_tsv)
    _out(args, f"seed: {count} компаний")
    return 0


def cmd_orgs(args: argparse.Namespace) -> int:
    from .github import GithubAdapter, run_orgs
    from .http import HttpClient

    paths, cfg = _paths(args)
    http_cfg = cfg.get("http", {})
    gh_cfg = cfg.get("github", {})
    client = HttpClient(
        paths.cache,
        token=github_token(cfg) or None,
        ttl_hours=float(http_cfg.get("cache_ttl_hours", 24)),
        ssl_mode=str(http_cfg.get("ssl_mode", "default")),
        insecure_tls=bool(http_cfg.get("insecure_tls", False)),
        min_interval=float(gh_cfg.get("min_interval", 1.2)),
    )
    written = run_orgs(
        GithubAdapter(client),
        paths.orgs,
        groups=tuple(args.groups or ("A", "B")),
        limit=args.limit,
        similar_min=float(gh_cfg.get("org_similar_min", 0.5)),
        dry=args.dry,
    )
    _out(args, f"orgs: обновлено {written}")
    return 0


def cmd_people(args: argparse.Namespace) -> int:
    from .github import GithubAdapter, run_people
    from .http import HttpClient

    paths, cfg = _paths(args)
    http_cfg = cfg.get("http", {})
    gh_cfg = cfg.get("github", {})
    client = HttpClient(
        paths.cache,
        token=github_token(cfg) or None,
        ttl_hours=float(http_cfg.get("cache_ttl_hours", 24)),
        ssl_mode=str(http_cfg.get("ssl_mode", "default")),
        insecure_tls=bool(http_cfg.get("insecure_tls", False)),
        min_interval=float(gh_cfg.get("min_interval", 1.2)),
    )
    stack = _target_stack(cfg)
    written = run_people(
        GithubAdapter(client),
        paths.orgs,
        paths.people,
        stack=stack,
        groups=tuple(args.groups or ("A", "B")),
        limit=args.limit,
        dry=args.dry,
    )
    _out(args, f"people: обновлено {written}")
    return 0


def _target_stack(cfg: dict[str, Any]) -> list[str]:
    stack = cfg.get("github", {}).get("target_stack")
    if stack:
        return [str(s).lower() for s in stack]
    return ["python", "llm", "ml", "ai", "rag", "nlp", "pytorch"]


def cmd_habr(args: argparse.Namespace) -> int:
    from .habr import HabrAdapter
    from .http import HttpClient

    paths, cfg = _paths(args)
    http_cfg = cfg.get("http", {})
    habr_cfg = cfg.get("habr", {})
    client = HttpClient(
        paths.cache,
        ttl_hours=float(http_cfg.get("cache_ttl_hours", 24)),
        ssl_mode=str(http_cfg.get("ssl_mode", "default")),
        retry_sleeps=(5.0, 15.0, 45.0),
        backoff_sleeps=(15.0, 45.0, 90.0),
    )
    adapter = HabrAdapter(
        client,
        profile_delay=float(habr_cfg.get("profile_delay", 0.7)),
        list_delay=float(habr_cfg.get("list_delay", 0.8)),
        hubs=list(habr_cfg.get("hubs", ["machine_learning", "artificial_intelligence"])),
    )
    if args.enrich:
        written = adapter.enrich(paths.habr_crawl, dry=args.dry)
        _out(args, f"habr enrich: обновлено {written}")
    else:
        written = adapter.crawl(
            paths.habr_crawl, pages=args.pages or int(habr_cfg.get("pages_default", 5)),
            dry=args.dry,
        )
        _out(args, f"habr crawl: записано {written}")
    return 0


def cmd_match(args: argparse.Namespace) -> int:
    from .habr import HabrAdapter
    from .http import HttpClient

    paths, cfg = _paths(args)
    http_cfg = cfg.get("http", {})
    client = HttpClient(
        paths.cache,
        ttl_hours=float(http_cfg.get("cache_ttl_hours", 24)),
        ssl_mode=str(http_cfg.get("ssl_mode", "default")),
        retry_sleeps=(5.0, 15.0, 45.0),
    )
    adapter = HabrAdapter(client, profile_delay=0, list_delay=0)
    written = adapter.match_handles(
        paths.people,
        paths.habr_matches,
        limit=args.limit,
        full=args.full,
        dry=args.dry,
    )
    _out(args, f"match: записано {written}")
    return 0


def cmd_bridge(args: argparse.Namespace) -> int:
    from .habr import HabrAdapter
    from .http import HttpClient

    paths, cfg = _paths(args)
    http_cfg = cfg.get("http", {})
    client = HttpClient(
        paths.cache,
        ttl_hours=float(http_cfg.get("cache_ttl_hours", 24)),
        ssl_mode=str(http_cfg.get("ssl_mode", "default")),
        retry_sleeps=(5.0, 15.0, 45.0),
    )
    adapter = HabrAdapter(client, profile_delay=0, list_delay=0)
    written = adapter.bridge(paths.people, paths.bridge, dry=args.dry)
    _out(args, f"bridge (закрытый эксперимент 3Б): записано {written}")
    return 0


def cmd_hh(args: argparse.Namespace) -> int:
    from . import hh_source
    from .hh_tool_api import get_hh_api

    paths, cfg = _paths(args)
    if not cfg.get("hh", {}).get("enabled", True):
        _out(args, "hh-канал выключен в конфиге ([outreach.hh].enabled=false)")
        return 0
    api = get_hh_api()
    if api is None:
        print("hh API недоступен: нет авторизации снайпера (snpr.hh_tool)", file=sys.stderr)
        return 2
    written = hh_source.run(
        api,
        paths.hh,
        companies_tsv=paths.companies_tsv,
        aliases_path=paths.employer_aliases,
        pages=args.pages,
        limit=args.limit,
        dry=args.dry,
        role_keywords=[
            str(k).lower()
            for k in (cfg.get("role_keywords") or [])
            if str(k).strip()
        ]
        or None,
    )
    _out(args, f"hh: записано {written}")
    return 0


def cmd_career(args: argparse.Namespace) -> int:
    from .career import get_adapter
    from .http import HttpClient

    paths, cfg = _paths(args)
    http_cfg = cfg.get("http", {})
    client = HttpClient(
        paths.cache,
        ttl_hours=float(http_cfg.get("cache_ttl_hours", 24)),
        ssl_mode=str(http_cfg.get("ssl_mode", "default")),
        insecure_tls=bool(http_cfg.get("insecure_tls", False)),
    )
    # 06.11: роль конфигурируема — словарь ключей фильтра вакансий.
    # Пусто = ML-дефолт (зашитый словарь rank.py). Пресеты в ИНСТРУКЦИИ §8a.
    keywords = [
        str(k).lower()
        for k in (cfg.get("role_keywords") or [])
        if str(k).strip()
    ] or None
    total = 0
    adapters = list(
        cfg.get("career", {}).get(
            "adapters",
            ["alfa", "huntflow", "jobtoo", "sber", "avito", "ozon"],
        )
    )
    if args.adapter:
        adapters = [args.adapter]
    for name in adapters:
        adapter_cls = get_adapter(name)
        # jobtoo — справочник доменов, не зависит от роли
        adapter = (
            adapter_cls(client)
            if name == "jobtoo"
            else adapter_cls(client, keywords=keywords)
        )
        if name == "huntflow":
            # таргеты: пересечение справочника jobtoo с companies.tsv
            targets = _huntflow_targets(paths)
            written = adapter.run(paths.career, targets=targets, dry=args.dry)
        else:
            written = adapter.run(paths.career, dry=args.dry)
        _out(args, f"career/{name}: {written}")
        if name == "jobtoo":
            total += 0  # jobtoo пишет в отдельный справочник
        else:
            total += written
    if keywords:
        _out(args, f"роль: {', '.join(keywords[:8])}")
    return 0


def _huntflow_targets(paths: Paths) -> list[dict[str, str]]:
    """Таргеты HuntFlow: домены справочника jobtoo, чьи компании есть
    в companies.tsv (замена захардкоженным HUNTFLOW_TARGETS research)."""
    from .store import read_tsv
    from .text import normalize_company

    companies = read_tsv(paths.companies_tsv)
    wanted = {normalize_company(c.get("name")): c for c in companies if c.get("name")}
    sites = paths.jobtoo_sites.load()
    targets: list[dict[str, str]] = []
    for site in sites:
        domain = str(site.get("career_domain") or "")
        if "huntflow" not in domain:
            continue
        company = str(site.get("company") or "")
        match = wanted.get(normalize_company(company))
        if match:
            targets.append(
                {
                    "company": match["name"],
                    "group": match.get("group") or match["name"],
                    "career_domain": domain,
                }
            )
    return targets


def cmd_base(args: argparse.Namespace) -> int:
    from .base import run_build

    paths, _cfg = _paths(args)
    written = run_build(
        paths.people,
        paths.career,
        paths.hh,
        paths.habr_crawl,
        paths.base,
        dry=args.dry,
    )
    _out(args, f"base: обновлено {written}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from . import report

    paths, _cfg = _paths(args)
    reports = paths.reports
    which = args.which or "all"
    written: list[Path] = []
    if which in ("all", "cards"):
        written.append(report.render_cards(paths.orgs, paths.people, reports / "cards.md"))
    if which in ("all", "github"):
        written.append(
            report.render_github_report(paths.orgs, paths.people, reports / "report.md")
        )
    if which in ("all", "hh"):
        written.append(
            report.render_channel_report(
                paths.hh,
                reports / "НАЙМ_МЕНЕДЖЕРЫ.md",
                "Нанимающие менеджеры ML/AI-вакансий (hh.ru)",
                "Контакты из ОТКРЫТЫХ вакансий (этика канала).",
            )
        )
    if which in ("all", "career"):
        written.append(
            report.render_channel_report(
                paths.career,
                reports / "КАРЬЕРНЫЕ_САЙТЫ_МЕНЕДЖЕРЫ.md",
                "Нанимающие менеджеры (карьер-сайты)",
                "Alfa API + HuntFlow SSR (4А — «прямой прорыв»).",
            )
        )
    if which in ("all", "habr"):
        import json

        aliases = {}
        if paths.habr_aliases.exists():
            aliases = json.loads(paths.habr_aliases.read_text(encoding="utf-8"))
        written.append(
            report.render_habr_report(
                paths.habr_crawl, reports / "КАУЛ_ИТОГ.md", aliases=aliases
            )
        )
    if which in ("all", "base"):
        from .base import build_contacts

        contacts = build_contacts(paths.people, paths.career, paths.hh, paths.habr_crawl)
        written.append(
            report.render_full_base(contacts, reports / "ВСЯ_БАЗА_КОНТАКТОВ.md")
        )
    for path in written:
        _out(args, f"отчёт: {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    status = outreach_status(_load_config(args))
    print(f"outreach: {'ВКЛ' if status['enabled'] else 'выкл'}, данные: {status['data_dir']}")
    for name, info in status["stores"].items():
        last = info["last_checked"] or "—"
        print(f"  {name}: {info['records']} записей (последняя проверка {last})")
    if "base" in status:
        base = status["base"]
        print(
            f"  БАЗА: {base['total']} контактов "
            f"(email {base['by_channel'].get('email', 0)}, "
            f"tg {base['by_channel'].get('telegram', 0)}), "
            f"верифицировано {base['verified']}, возраст ≤ {base['max_age_days']} д"
        )
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from .migrate import freeze_research, run_migrate

    paths, _cfg = _paths(args)
    # 06.11: миграция выполнена, research/ архивирован в trash/.
    # Команда осталась для воспроизводимости: источник только явный --source.
    source = Path(args.source) if args.source else None
    if source is None or not source.exists():
        _out(args, "миграция уже выполнена 06.11; research/ архивирован.")
        _out(args, "для повторной миграции укажите --source <путь к outreach_mvp>")
        return 0
    report = run_migrate(source, paths)
    for filename, info in report.get("copied", {}).items():
        _out(args, f"  {filename}: {info}")
    _out(args, f"  кэш: {report.get('cache_files', 0)} файлов")
    verification = report.get("verification", {})
    if verification.get("matches"):
        _out(args, "  сверка с эталоном аудита: СОВПАДЕНИЕ")
    else:
        print(f"  сверка: РАСХОЖДЕНИЯ {verification.get('diffs')}", file=sys.stderr)
        return 1
    if args.freeze:
        note = freeze_research(source)
        _out(args, f"  архив-заметка: {note}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    paths, cfg = _paths(args)
    removed = prune_cache(paths, cfg)
    _out(args, f"prune: удалено {removed} файлов кэша")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """LLM-скоринг карьер-вакансий (06.11): тот же скорер и профиль, что
    у снайпера, результат — в career-стор + UI."""
    from .scoring import VacancyScorer

    paths, cfg = _paths(args)
    ai_base = os.environ.get("SNE_AI_BASE_URL", "")
    ai_model = os.environ.get("SNE_AI_MODEL", "")
    if not (ai_base and ai_model):
        print(
            "AI недоступен: нет SNE_AI_BASE_URL/SNE_AI_MODEL в .env",
            file=sys.stderr,
        )
        return 2
    from ..cli import _make_ai_client, _scoring_profile
    import argparse as _argparse

    fake_args = _argparse.Namespace(
        ai_base_url=ai_base, ai_model=ai_model
    )
    ai = _make_ai_client(fake_args)
    if ai is None:
        print("AI-клиент не собрался", file=sys.stderr)
        return 2
    config = _load_config(args)
    scorer = VacancyScorer(
        ai,
        maybe_threshold=int(
            (config.get("scoring", {}) or {}).get("maybe_min", 45)
        ),
        target_threshold=int(
            (config.get("scoring", {}) or {}).get("target_min", 75)
        ),
        profile=_scoring_profile(config),
    )
    from . import scoring as outreach_scoring

    scored = outreach_scoring.run_scoring(
        paths.career,
        scorer,
        limit=args.limit,
        force=args.force,
        dry=args.dry,
    )
    _out(args, f"score: заскорено {scored}")
    return 0


def cmd_ui_check(args: argparse.Namespace) -> int:
    """UI_Снайпер.cmd: тихо убедиться, что sidecar UI жив (без браузера).

    Двойной клик по ярлыку: этот шаг поднимает UI, если он упал,
    затем cmd открывает браузер через start.
    """
    # ensure_ui_running ждёт RAW-конфиг {"outreach": {...}} (внутри сам
    # мерджит outreach_config) — передаём исходный, не смерженный
    from .ui_sidecar import ensure_ui_running

    alive = ensure_ui_running(_load_config(args))
    if alive:
        print("UI жив: http://127.0.0.1:8765")
    else:
        print(
            "UI не поднялся: проверь флаги [outreach].enabled / "
            "[outreach.ui].enabled в snpr/config.toml"
        )
        return 1
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .webui import serve

    paths, cfg = _paths(args)
    ui_cfg = cfg.get("ui", {})
    return serve(
        paths,
        host=str(ui_cfg.get("host", "127.0.0.1")),
        port=int(ui_cfg.get("port", 8765)),
    )


def build_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Регистрация `snpr outreach` в основном CLI (паттерн cli.py:66+)."""
    parser = sub.add_parser(
        "outreach", help="Прямые контакты целевых компаний (модуль outreach)"
    )
    # 06.11 P2-фикс (затенение --config): корневой snpr --config и outreach
    # --config перетирались default=None степов — работал только флаг ПОСЛЕ
    # имени степа. Теперь степы наследуют (SUPPRESS), а outreach-родитель
    # берёт корневой default при отсутствии своего флага.
    root_config = getattr(sub, "_root_config_default", None)
    parser.add_argument(
        "--config", default=root_config, help="путь к config.toml"
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    steps = parser.add_subparsers(dest="outreach_step", required=True)

    def add_step(name: str, help_text: str) -> argparse.ArgumentParser:
        step = steps.add_parser(name, help=help_text)
        step.add_argument("--config", default=argparse.SUPPRESS)
        step.add_argument("-q", "--quiet", action="store_true")
        step.add_argument("--dry", action="store_true")
        step.add_argument("--limit", type=int, default=0)
        return step

    seed_p = add_step("seed", "companies.tsv → orgs.jsonl")
    seed_p.set_defaults(func=cmd_seed)

    orgs_p = add_step("orgs", "резолв GitHub-оргов")
    orgs_p.add_argument("--group", dest="groups", action="append", default=None)
    orgs_p.set_defaults(func=cmd_orgs)

    people_p = add_step("people", "люди и email из коммитов")
    people_p.add_argument("--group", dest="groups", action="append", default=None)
    people_p.set_defaults(func=cmd_people)

    habr_p = add_step("habr", "краул Habr-хабов")
    habr_p.add_argument("--pages", type=int, default=0)
    habr_p.add_argument("--enrich", action="store_true")
    habr_p.set_defaults(func=cmd_habr)

    match_p = add_step("match", "хэндл-матч GH↔Habr")
    match_p.add_argument("--full", action="store_true")
    match_p.set_defaults(func=cmd_match)

    bridge_p = add_step("bridge", "мост email→Habr→TG (закрытый эксперимент)")
    bridge_p.set_defaults(func=cmd_bridge)

    hh_p = add_step("hh", "найм-менеджеры из hh-вакансий")
    hh_p.add_argument("--pages", type=int, default=5)
    hh_p.set_defaults(func=cmd_hh)

    career_p = add_step("career", "карьер-сайты (alfa/huntflow/jobtoo)")
    career_p.add_argument("--adapter", default=None)
    career_p.set_defaults(func=cmd_career)

    base_p = add_step("base", "сборка outreach.jsonl (единая база)")
    base_p.set_defaults(func=cmd_base)

    score_p = add_step("score", "LLM-скоринг карьер-вакансий (профиль снайпера)")
    score_p.add_argument("--force", action="store_true", help="перескорить всё")
    score_p.set_defaults(func=cmd_score)

    report_p = add_step("report", "рендер MD-отчётов")
    report_p.add_argument(
        "--which", dest="which", default="all",
        choices=["all", "cards", "github", "habr", "hh", "career", "base"],
    )
    report_p.set_defaults(func=cmd_report)

    status_p = add_step("status", "офлайн-сводка по данным")
    status_p.set_defaults(func=cmd_status)

    migrate_p = add_step("migrate", "перенос данных research/outreach_mvp")
    migrate_p.add_argument("--source", default=None)
    migrate_p.add_argument("--freeze", action="store_true")
    migrate_p.set_defaults(func=cmd_migrate)

    prune_p = add_step("prune", "прунинг кэша")
    prune_p.set_defaults(func=cmd_prune)

    ui_p = add_step("ui", "локальный UI (127.0.0.1)")
    ui_p.set_defaults(func=cmd_ui)

    ui_check_p = add_step(
        "ui-check", "тихо проверить/поднять sidecar UI (для ярлыка)"
    )
    ui_check_p.set_defaults(func=cmd_ui_check)

    return parser


def run_cli(args: argparse.Namespace) -> int:
    """Точка входа из основного CLI (dispatch в cli.py)."""
    if getattr(args, "func", None):
        return args.func(args)
    return 0
