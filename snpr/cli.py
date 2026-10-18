from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from .approve import (
    AppliedRecord,
    AppliedStore,
    ApproveDecision,
    AutoApprover,
    AutoParkApprover,
    CLIApprover,
    Decision,
    ParkingApprover,
)
from .cover_letter import PROFILE_CAPSULE, StaticLetterWriter
from .letter_guard import LetterGuard, extract_numbers
from .pipeline import Candidate, SnePipeline
from .scoring import VacancyScorer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APPLIED_STORE = PROJECT_ROOT / "data" / "applied.jsonl"
DEFAULT_SEEN_STORE = PROJECT_ROOT / "data" / "seen.jsonl"
DEFAULT_DEFERRED_STORE = PROJECT_ROOT / "data" / "deferred.jsonl"
DEFAULT_PARKED_STORE = PROJECT_ROOT / "data" / "parked.jsonl"
DEFAULT_SCORES_STORE = PROJECT_ROOT / "data" / "scores.jsonl"
DEFAULT_NANNY_STORE = PROJECT_ROOT / "data" / "nanny.jsonl"
DEFAULT_RAISE_MARKER = PROJECT_ROOT / "data" / "last_raise.txt"
DEFAULT_REPLIED_STORE = PROJECT_ROOT / "data" / "replied.jsonl"
DEFAULT_BLACKLIST_FILE = PROJECT_ROOT / "data" / "blacklist.txt"
DOTENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_URL_PREFIXES = {"github.com/example-user/", "habr.com/ru/articles/"}


def load_dotenv(path: Path = DOTENV_PATH) -> None:
    """Загрузить .env в os.environ. Реальные переменные приоритетнее."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snpr",
        description="Снпрер-конвейер: пре-фильтр → скоринг → конверт → approve",
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("config.toml")),
        help="TOML-конфиг пре-фильтра",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Прогон конвейера по вакансиям из JSON")
    scan.add_argument("--input", required=True, help="JSON-файл со списком вакансий")
    scan.add_argument(
        "--descriptions",
        default=None,
        help="JSON-файл {vacancy_id: description} для дорогой фазы фильтра",
    )
    scan.add_argument(
        "--mode",
        choices=["always", "threshold", "off"],
        default="always",
        help="always — подтверждение каждого пуска; threshold — автопилот "
        "выше порога; off — полный автопилот всех готовых писем",
    )
    scan.add_argument(
        "--score-threshold", type=int, default=75, help="Порог автопилота"
    )
    scan.add_argument("--limit", type=int, default=0, help="Максимум вакансий на прогон")
    scan.add_argument("--dry-run", action="store_true", help="Ничего не фиксировать")
    scan.add_argument(
        "--applied-store",
        default=str(DEFAULT_APPLIED_STORE),
        help="JSONL-история пусков",
    )
    scan.add_argument(
        "--ai",
        action="store_true",
        help="Включить LLM-скоринг (письмо статическое, LLM не нужно)",
    )
    scan.add_argument(
        "--ai-base-url", default=os.environ.get("SNE_AI_BASE_URL", "")
    )
    scan.add_argument(
        "--ai-model",
        default=os.environ.get("SNE_AI_MODEL", ""),
    )

    sne = sub.add_parser(
        "sne",
        help="Живой поллинг hh: новые вакансии → конвейер → POST /negotiations",
    )
    sne.add_argument(
        "--hours",
        type=int,
        default=0,
        help="Глубина поиска в часах (0 — из config [search].hours)",
    )
    sne.add_argument(
        "--queries",
        nargs="*",
        default=None,
        help="Запросы поиска (по умолчанию config [search].queries)",
    )
    sne.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Потолок пула новых вакансий на прогон (0 — из config; дёшево, "
        "гейты по полям выдачи)",
    )
    sne.add_argument(
        "--max-scored",
        type=int,
        default=0,
        help="Бюджет LLM-скоринга на прогон (0 — без потолка, из config)",
    )
    sne.add_argument(
        "--scoring-workers",
        type=int,
        default=0,
        help="Параллельных LLM-вызовов в волне скоринга (0 — из config "
        "[scoring].workers)",
    )
    sne.add_argument(
        "--deferred-store",
        default=str(DEFAULT_DEFERRED_STORE),
        help="JSONL-очередь вакансий, которым не хватило бюджета скоринга",
    )
    sne.add_argument(
        "--resume-id",
        default="",
        help="ID резюме для отклика (по умолчанию первое на аккаунте)",
    )
    sne.add_argument(
        "--mode",
        choices=["always", "threshold", "off", "park"],
        default="always",
        help="always — подтверждение каждого пуска; threshold — автопилот "
        "выше порога; off — полный автопилот всех готовых писем; "
        "park — для планировщика: готовые в data/parked.jsonl, решение "
        "потом через `snpr approve`",
    )
    sne.add_argument(
        "--score-threshold", type=int, default=75, help="Порог автопилота"
    )
    sne.add_argument(
        "--parked-store",
        default=str(DEFAULT_PARKED_STORE),
        help="JSONL-парковка готовых к пуску (режим park)",
    )
    sne.add_argument(
        "--scores-store",
        default=str(DEFAULT_SCORES_STORE),
        help="JSONL-журнал чисел скоринга (аудит и калибровка порогов)",
    )
    sne.add_argument(
        "--dry-run",
        action="store_true",
        help="Поиск и конвейер живые, но без POST и записи сторов",
    )
    sne.add_argument(
        "--applied-store",
        default=str(DEFAULT_APPLIED_STORE),
        help="JSONL-история пусков",
    )
    sne.add_argument(
        "--seen-store",
        default=str(DEFAULT_SEEN_STORE),
        help="JSONL-дедуп обработанных вакансий",
    )
    sne.add_argument(
        "--ai",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="LLM-скоринг (по умолчанию включён)",
    )
    sne.add_argument(
        "--ai-base-url", default=os.environ.get("SNE_AI_BASE_URL", "")
    )
    sne.add_argument(
        "--ai-model",
        default=os.environ.get("SNE_AI_MODEL", ""),
    )

    why = sub.add_parser(
        "why",
        help="Диагностика одной вакансии: где выпала из воронки",
    )
    why.add_argument(
        "vacancy",
        help="ID вакансии или ссылка вида https://hh.ru/vacancy/<id>",
    )
    why.add_argument(
        "--queries",
        nargs="*",
        default=None,
        help="Запросы для проверки покрытия поиском (по умолчанию config)",
    )
    why.add_argument(
        "--seen-store",
        default=str(DEFAULT_SEEN_STORE),
        help="JSONL-дедуп обработанных вакансий",
    )
    why.add_argument(
        "--applied-store",
        default=str(DEFAULT_APPLIED_STORE),
        help="JSONL-история пусков",
    )
    why.add_argument(
        "--deferred-store",
        default=str(DEFAULT_DEFERRED_STORE),
        help="JSONL-очередь незакрытых вакансий",
    )

    approve = sub.add_parser(
        "approve",
        help="Решение по припаркованным: y/n/e на каждую, пуск при y",
    )
    approve.add_argument(
        "--parked-store",
        default=str(DEFAULT_PARKED_STORE),
        help="JSONL-парковка готовых к пуску",
    )
    approve.add_argument(
        "--applied-store",
        default=str(DEFAULT_APPLIED_STORE),
        help="JSONL-история пусков",
    )
    approve.add_argument(
        "--seen-store",
        default=str(DEFAULT_SEEN_STORE),
        help="JSONL-дедуп обработанных вакансий",
    )
    approve.add_argument(
        "--resume-id",
        default="",
        help="ID резюме для отклика (по умолчанию первое на аккаунте)",
    )
    approve.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать карточки без POST /negotiations и записи сторов",
    )
    job = sub.add_parser(
        "job",
        help="Управление джобой планировщика: status/on/off/run/log",
    )
    job.add_argument(
        "action",
        choices=["status", "on", "off", "run", "log"],
        nargs="?",
        default="status",
        help="status — состояние; on/off — вкл/выкл; run — такт сейчас; "
        "log — хвост лога",
    )
    job.add_argument(
        "-n",
        "--lines",
        type=int,
        default=60,
        help="Строк хвоста лога (только для log)",
    )

    daemon = sub.add_parser(
        "daemon",
        help="Кроссплатформенный тикер: такты без планировщика ОС "
        "(Linux/macOS/Docker/tmux; Windows-джоба остаётся)",
    )
    daemon.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Секунд между тиками (0 — из config [daemon], дефолт 60)",
    )
    daemon.add_argument(
        "--once",
        action="store_true",
        help="Один такт и выход (стиль systemd-таймера/крона)",
    )

    nanny = sub.add_parser(
        "nanny",
        help="Няня откликов: отчёт о реакциях работодателей + снапшот",
    )
    nanny.add_argument(
        "--nanny-store",
        default=str(DEFAULT_NANNY_STORE),
        help="JSONL-снапшоты переговоров (диффы между прогонами)",
    )
    nanny.add_argument(
        "--status",
        default="active",
        help="Статус переговоров hh (по умолчанию active)",
    )
    nanny.add_argument(
        "--silence-days",
        type=float,
        default=None,
        help="Дней тишины до «повисло» (по умолчанию config [nanny])",
    )
    nanny.add_argument(
        "--dry-run",
        action="store_true",
        help="Отчёт без записи снапшота",
    )

    resume_raise = sub.add_parser(
        "raise",
        help="Поднять резюме: POST /resumes/{id}/publish (видимость)",
    )
    resume_raise.add_argument(
        "--resume-id",
        default="",
        help="ID резюме (по умолчанию первое на аккаунте)",
    )
    resume_raise.add_argument(
        "--force",
        action="store_true",
        help="Поднять сейчас, мимо окна часов и маркера интервала",
    )
    resume_raise.add_argument(
        "--marker",
        default=str(DEFAULT_RAISE_MARKER),
        help="Файл-маркер последнего поднятия",
    )
    resume_raise.add_argument(
        "--interval-hours",
        type=float,
        default=4.0,
        help="Минимум часов между поднятиями",
    )
    resume_raise.add_argument(
        "--from-hour", type=int, default=9, help="Окно поднятия, час от (МСК)"
    )
    resume_raise.add_argument(
        "--to-hour", type=int, default=19, help="Окно поднятия, час до (МСК)"
    )

    cleanup = sub.add_parser(
        "cleanup",
        help="Снять неактивные отклики (отказы и тишину; по умолчанию dry-run)",
    )
    cleanup.add_argument(
        "--status",
        default="active",
        help="Статус переговоров hh (по умолчанию active)",
    )
    cleanup.add_argument(
        "--older-than",
        type=float,
        default=None,
        help="Дней тишины до «неактивн» (по умолчанию config [cleanup])",
    )
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="Действительно снять (без флага — только показать список)",
    )
    cleanup.add_argument(
        "--with-decline-message",
        action="store_true",
        help="Отправить работодателю сообщение при снятии (по умолчанию тихо)",
    )
    cleanup.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Показать и оставленные отклики с причиной",
    )

    reply = sub.add_parser(
        "reply",
        help="Автоответы на вопросы в чатах hh: кнопки анкет — выбор варианта, "
        "текст — из фактов профиля (гвард, люди по умолчанию не отвечаются)",
    )
    reply.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать ответы, ничего не отправлять и не фиксировать",
    )
    reply.add_argument(
        "--confirm",
        action="store_true",
        help="Спрашивать y/n перед каждой отправкой",
    )
    reply.add_argument(
        "--answer-humans",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Отвечать ли свободным текстом людям (по умолчанию config [reply])",
    )
    reply.add_argument(
        "--max-chats",
        type=int,
        default=0,
        help="Потолок ответов за прогон (0 — из config [reply])",
    )
    reply.add_argument(
        "--period-hours",
        type=float,
        default=0.0,
        help="Отвечать только на вопросы свежее N часов (0 — из config)",
    )
    reply.add_argument(
        "--max-history",
        type=int,
        default=0,
        help="Не отвечать в чатах длиннее N сообщений (0 — из config)",
    )
    reply.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Страниц чатов за прогон (0 — из config)",
    )
    reply.add_argument(
        "--replied-store",
        default=str(DEFAULT_REPLIED_STORE),
        help="JSONL-история ответов (анти-дубли по message_id)",
    )

    blacklist = sub.add_parser(
        "blacklist",
        help="Чёрный список работодателей (data/blacklist.txt)",
    )
    blacklist.add_argument(
        "action",
        choices=["list", "add"],
        nargs="?",
        default="list",
        help="list — показать; add — добавить работодателя вакансии",
    )
    blacklist.add_argument(
        "vacancy_id",
        nargs="?",
        default="",
        help="ID вакансии или ссылка hh.ru/vacancy/<id> (для add)",
    )
    return parser


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _validate_base_url(url: str) -> str | None:
    from urllib.parse import urlsplit

    if not url:
        return "пустой base_url"
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and host in LOCAL_HOSTS:
        return None
    if not parts.scheme:
        return f"нет схемы в base_url: {url!r} (нужно https://...)"
    return (
        f"схема {parts.scheme!r} на не-localhost хосте {host!r}: "
        "ключ и письма уйдут открытым текстом"
    )


def _make_ai_client(args: argparse.Namespace):
    if not (args.ai_base_url and args.ai_model):
        return None
    error = _validate_base_url(args.ai_base_url)
    if error is not None:
        print(f"AI отключён: {error}", file=sys.stderr)
        return None
    from hh_applicant_tool.ai.openai import ChatOpenAI

    return ChatOpenAI(
        base_url=args.ai_base_url,
        model=args.ai_model,
        api_key=os.environ.get("SNE_AI_API_KEY") or None,
        temperature=0.2,
        # 17.10: deepseek-v4.1-flash (без thinking) отвечает за ~4-10 с;
        # 12000 токенов — с запасом на длинные описания вакансий
        max_completion_tokens=12000,
        timeout=240.0,
    )


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _scoring_profile(config: Mapping[str, Any]) -> str | None:
    """18.10: профиль кандидата из [scoring].profile (пусто — дефолт в коде)."""
    profile = config.get("scoring", {}).get("profile")
    return str(profile).strip() or None if profile is not None else None


def _letter_profile(config: Mapping[str, Any]) -> str | None:
    """18.10: факты кандидата из [letter].profile (гвард + LLM-конверт)."""
    profile = config.get("letter", {}).get("profile")
    return str(profile).strip() or None if profile is not None else None


def _letter_static_text(config: Mapping[str, Any]) -> str | None:
    """18.10: текст статического конверта из [letter].static_text."""
    text = config.get("letter", {}).get("static_text")
    return str(text).strip() or None if text is not None else None


def _build_scorers(
    args: argparse.Namespace,
    workers: int = 1,
    maybe_threshold: int = 50,
    target_threshold: int = 75,
    profile: str | None = None,
) -> list[VacancyScorer] | None:
    """Пул независимых скореров: у ChatOpenAI лок на весь HTTP-запрос,
    один инстанс = последовательные вызовы. N инстанцев = N параллельных."""
    if not args.ai:
        # явное --no-ai (scan: отсутствие --ai) отключает скоринг даже при
        # кредах в env: флаги/env не включают LLM сами по себе
        return None
    if not (args.ai_base_url and args.ai_model):
        print(
            "AI запрошен, но нет SNE_AI_BASE_URL/SNE_AI_MODEL "
            "(или флагов --ai-base-url/--ai-model). Прогон без скоринга.",
            file=sys.stderr,
        )
        return None
    return [
        VacancyScorer(
            _make_ai_client(args),
            maybe_threshold=maybe_threshold,
            target_threshold=target_threshold,
            profile=profile,
        )
        for _ in range(max(1, workers))
    ]


def _build_scorer(
    args: argparse.Namespace, profile: str | None = None
) -> VacancyScorer | None:
    scorers = _build_scorers(args, 1, profile=profile)
    return scorers[0] if scorers else None


def _make_approver(
    args: argparse.Namespace,
    auto_park_threshold: int = 0,
    max_age_hours: float | None = None,
) -> CLIApprover | AutoApprover | ParkingApprover | AutoParkApprover:
    if args.mode == "always":
        return CLIApprover()
    if args.mode == "threshold":
        return AutoApprover(score_threshold=args.score_threshold)
    if args.mode == "park":
        # 18.10: автопилот в парк-режиме — пуск от auto_approve_min,
        # только по свежим (<= max_age_hours), остальное в парковку.
        # 0/отсутствует = чистый парк (как было).
        if auto_park_threshold > 0:
            return AutoParkApprover(
                score_threshold=auto_park_threshold,
                max_age_hours=max_age_hours,
            )
        return ParkingApprover()
    return AutoApprover(score_threshold=0)


def _make_guard(config: Mapping[str, Any]) -> LetterGuard:
    guard_config = config.get("letter_guard", {})
    guard_prefixes = set(
        guard_config.get("allowed_url_prefixes", DEFAULT_URL_PREFIXES)
    )
    # 18.10: числа разрешаются из профиля [letter].profile (не из константы
    # кода) — другой человек правит факты о себе, гвард следует за ними
    profile = _letter_profile(config)
    return LetterGuard(
        allowed_numbers=extract_numbers(profile or PROFILE_CAPSULE),
        allowed_url_prefixes=guard_prefixes,
    )


def report_candidates(candidates: list[Candidate]) -> dict[str, int]:
    """Напечатать блоки скоринга/пре-фильтра, вернуть статистику статусов."""
    stats = {
        "filtered_out": 0,
        "unscored": 0,
        "pass": 0,
        "maybe": 0,
        "ready": 0,
        "parked": 0,
        "error": 0,
        "deferred": 0,
    }
    for candidate in candidates:
        status = candidate.status
        stats[status] = stats.get(status, 0) + 1

    if stats.get("parked"):
        print("В парковку (решение человека — `snpr approve`):")
        for candidate in candidates:
            if candidate.status != "parked":
                continue
            vacancy = candidate.vacancy
            employer = (vacancy.get("employer") or {}).get("name", "")
            print(f"  [{candidate.vacancy_id}] {vacancy.get('name', '')} — {employer}")
        print()

    if stats.get("deferred"):
        print(
            f"Отложено по бюджету скоринга: {stats['deferred']} "
            "(очередь сохранена, вернёмся в следующих прогонах):"
        )
        for candidate in candidates:
            if candidate.status != "deferred":
                continue
            vacancy = candidate.vacancy
            employer = (vacancy.get("employer") or {}).get("name", "")
            print(f"  [{candidate.vacancy_id}] {vacancy.get('name', '')} — {employer}")
        print()

    if stats.get("error"):
        print("Скоринг не удался (вернёмся к этим вакансиям в следующем прогоне):")
        for candidate in candidates:
            if candidate.status != "error":
                continue
            vacancy = candidate.vacancy
            employer = (vacancy.get("employer") or {}).get("name", "")
            print(
                f"  [{candidate.vacancy_id}] {vacancy.get('name', '')} — "
                f"{employer} | {candidate.error}"
            )
        print()

    if stats.get("unscored", 0):
        print("Прошли пре-фильтр (скоринг не подключён — без AI):")
        for candidate in candidates:
            if candidate.status == "unscored":
                vacancy = candidate.vacancy
                salary = vacancy.get("salary") or {}
                salary_parts = [
                    str(salary.get(k))
                    for k in ("from", "to")
                    if salary.get(k) is not None
                ]
                salary_line = (
                    " ".join(salary_parts[:2]) + " " + str(salary.get("currency", ""))
                    if salary_parts
                    else "—"
                ).strip()
                print(
                    f"  [{candidate.vacancy_id}] {vacancy.get('name', '')} — "
                    f"{(vacancy.get('employer') or {}).get('name', '')} | {salary_line}"
                )
        print()
    elif any(c.score is not None for c in candidates):
        print("Скоринг:")
        for candidate in candidates:
            if candidate.score is None:
                continue
            score = candidate.score
            status = (
                "→ письмо готово"
                if candidate.status == "ready"
                else "→ в парковке" if candidate.status == "parked" else "→ без письма"
            )
            reason = score.reasons[0] if score.reasons else ""
            print(
                f"  [{candidate.vacancy_id}] {score.score:3d} ({score.verdict}) "
                f"{candidate.vacancy.get('name', '')} — "
                f"{(candidate.vacancy.get('employer') or {}).get('name', '')} "
                f"{status} | {reason}"
            )
        print()
    return stats


def print_summary(
    total: int,
    stats: Mapping[str, int],
    sent: int,
    forced_human: int,
    dry_run: bool,
) -> None:
    print(
        "Итог: всего %d | отсеяно фильтром %d | без скоринга %d | pass %d | "
        "maybe %d | готово к пуску %d | пусков %d%s%s%s%s"
        % (
            total,
            stats["filtered_out"],
            stats.get("unscored", 0),
            stats.get("pass", 0),
            stats.get("maybe", 0),
            stats.get("ready", 0),
            sent,
            " (dry-run, ничего не записано)" if dry_run else "",
            f" | на ручное решение переведено {forced_human}" if forced_human else "",
            (
                f" | скоринг упал на {stats['error']} (вернёмся в след. прогоне)"
                if stats.get("error")
                else ""
            ),
            (
                f" | отложено по бюджету {stats['deferred']}"
                if stats.get("deferred")
                else ""
            ),
        )
    )


def run_scan(args: argparse.Namespace) -> int:
    from .filters import PipelineFilter

    vacancies = _load_json(args.input)
    if not isinstance(vacancies, list):
        print("Ошибка: ожидается JSON-список вакансий", file=sys.stderr)
        return 2

    descriptions: Mapping[str, str] | None = None
    if args.descriptions:
        descriptions = _load_json(args.descriptions)

    if args.limit > 0:
        vacancies = vacancies[: args.limit]

    config = _load_toml(args.config)
    prefilter = PipelineFilter(config)

    # Письмо статическое (не подгоняем под вакансию): LLM — только скоринг.
    # 18.10: текст/профиль — из config [letter] (персонализация без правки кода)
    writer = StaticLetterWriter(text=_letter_static_text(config))
    scorer = _build_scorer(args, profile=_scoring_profile(config))

    pipeline = SnePipeline(prefilter, scorer=scorer, writer=writer)
    candidates = pipeline.run(vacancies, descriptions=descriptions)

    stats = report_candidates(candidates)

    sent, forced_human, _parked = send_candidates(
        candidates,
        approver=_make_approver(args),
        guard=_make_guard(config),
        manual=CLIApprover(),
        applied_store=AppliedStore(Path(args.applied_store)),
        dry_run=args.dry_run,
    )

    print()
    print_summary(len(candidates), stats, sent, forced_human, args.dry_run)
    return 0


def _vacancy_numbers(vacancy: Mapping[str, Any]) -> set[str]:
    """Допустимые числа из структурированной вилки этой вакансии.

    Письмо легитимно цитирует вилку в вопросе («в поле 300-450К...»):
    полные значения и тысячная форма (300000 → «300К»).
    """
    numbers: set[str] = set()
    salary = vacancy.get("salary") or {}
    for key in ("from", "to"):
        value = salary.get(key)
        if value is None:
            continue
        try:
            int_value = int(value)
        except (TypeError, ValueError):
            continue
        numbers.add(str(int_value))
        if int_value >= 1000:
            numbers.add(str(int_value // 1000))
    return numbers


def _is_limit_exceeded(ex: Exception) -> bool:
    try:
        from hh_applicant_tool.api.errors import LimitExceeded
    except ImportError:
        return False
    return isinstance(ex, LimitExceeded)


def send_candidates(
    candidates: list[Candidate],
    approver: CLIApprover | AutoApprover | ParkingApprover | AutoParkApprover,
    guard: LetterGuard,
    manual: CLIApprover,
    applied_store: AppliedStore,
    dry_run: bool,
    sender: Any = None,
    parked_store: Any = None,
) -> tuple[int, int, int]:
    """Пускы: sender=None — только запись истории (офлайн-прогон),
    sender — реальный POST /negotiations перед записью.

    ParkingApprover (запланированный прогон): готовые — в парковку
    data/parked.jsonl, без пуска и отказа; решение — `snpr approve`.
    Возврат: (пусков, принудительно-ручных, припарковано).
    """
    sent = 0
    forced_human = 0
    parked = 0

    for candidate in candidates:
        if candidate.status != "ready":
            continue

        letter_text = candidate.letter.text if candidate.letter else ""
        violations = guard.check(
            letter_text, extra_numbers=_vacancy_numbers(candidate.vacancy)
        )

        active = approver
        if violations:
            print()
            print(
                f"[{candidate.vacancy_id}] ПРОВЕРКА ПИСЬМА — НАРУШЕНИЯ "
                "(возможна инъекция из вакансии):"
            )
            for violation in violations:
                print(f"  - {violation}")
            if not isinstance(approver, (CLIApprover, ParkingApprover)):
                # автопилот нарушил гвард — решение за человеком; парк-режим
                # и так отдаёт всё человеку (guard перепроверится в approve)
                print("Автопилот отключён для этой вакансии: решение за человеком.")
                active = manual
                forced_human += 1

        print()
        decision: ApproveDecision = active.decide(candidate)
        if decision.decision == Decision.PARK:
            if parked_store is not None and not dry_run:
                parked_store.append_record(
                    candidate.vacancy, candidate.score, letter_text
                )
                candidate.parked = True
                parked += 1
            continue
        if decision.decision in (Decision.SKIP, Decision.REJECT):
            continue

        letter = decision.letter_override or candidate.letter.text
        if decision.decision == Decision.EDIT and candidate.letter is not None:
            candidate.letter.text = letter

        if not dry_run and sender is not None:
            try:
                sender.send(candidate.vacancy_id, letter)
            except Exception as ex:
                if _is_limit_exceeded(ex):
                    print()
                    print(
                        "Дневной лимит откликов hh исчерпан — "
                        "оставшиеся пускы отменены."
                    )
                    return sent, forced_human, parked
                print(
                    f"  [{candidate.vacancy_id}] пуск не прошёл "
                    f"({ex}) — вакансия пропущена"
                )
                continue

        record = AppliedRecord(
            vacancy_id=candidate.vacancy_id,
            vacancy_name=str(candidate.vacancy.get("name") or ""),
            employer=str((candidate.vacancy.get("employer") or {}).get("name") or ""),
            score=candidate.score.score if candidate.score else 0,
            letter=letter,
            decision=decision.decision.value,
            dry_run=dry_run,
        )
        if not dry_run:
            applied_store.append(record)
        sent += 1

    return sent, forced_human, parked


def _load_toml(path: str) -> dict:
    import tomllib

    with open(path, "rb") as fh:
        return tomllib.load(fh)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        return run_scan(args)
    if args.command == "sne":
        from .sne import run_sne

        return run_sne(args)
    if args.command == "approve":
        from .sne import run_approve

        return run_approve(args)
    if args.command == "why":
        from .sne import run_why

        return run_why(args)
    if args.command == "job":
        from .job import run_job_command

        return run_job_command(args)
    if args.command == "daemon":
        from .daemon import run_daemon

        if args.interval == 0:
            config = _load_toml(args.config)
            args.interval = int(
                config.get("daemon", {}).get("interval_seconds", 60)
            )
        return run_daemon(args)
    if args.command == "nanny":
        from .hh_tool import SavingHHApplicantTool
        from .nanny import run_nanny

        if args.silence_days is None:
            config = _load_toml(args.config)
            args.silence_days = float(
                config.get("nanny", {}).get("silence_days", 3.0)
            )
        tool = SavingHHApplicantTool()
        return run_nanny(args, tool.api_client)
    if args.command == "raise":
        from .hh_tool import SavingHHApplicantTool
        from .resume_raise import run_raise

        tool = SavingHHApplicantTool()
        return run_raise(args, tool.api_client)
    if args.command == "cleanup":
        from .cleanup import run_cleanup
        from .hh_tool import SavingHHApplicantTool

        if args.older_than is None:
            config = _load_toml(args.config)
            args.older_than = float(
                config.get("cleanup", {}).get("older_than_days", 14.0)
            )
        tool = SavingHHApplicantTool()
        return run_cleanup(args, tool.api_client)
    if args.command == "reply":
        from .chat_reply import run_reply
        from .hh_tool import SavingHHApplicantTool

        config = _load_toml(args.config)
        reply_cfg = config.get("reply", {})
        if args.answer_humans is None:
            args.answer_humans = bool(reply_cfg.get("answer_humans", False))
        if args.max_chats <= 0:
            args.max_chats = int(reply_cfg.get("max_chats_per_run", 5) or 5)
        if args.period_hours <= 0:
            args.period_hours = float(reply_cfg.get("period_hours", 72) or 72)
        if args.max_history <= 0:
            args.max_history = int(reply_cfg.get("max_history", 20) or 20)
        if args.max_pages <= 0:
            args.max_pages = int(reply_cfg.get("max_pages", 3) or 3)
        tool = SavingHHApplicantTool()
        return run_reply(args, tool)
    if args.command == "blacklist":
        return run_blacklist(args)
    parser.print_help()
    return 1


def run_blacklist(args: argparse.Namespace) -> int:
    """CLI: snpr blacklist [list|add <vacancy_id>] — файл data/blacklist.txt.

    add тянет карточку вакансии, берёт id+имя работодателя и дописывает
    строку «id имя». Гейт подхватывает файл со следующего прогона.
    """
    from .filters import _norm_name, load_blacklist, resolve_blacklist_path

    config = _load_toml(args.config)
    path = resolve_blacklist_path(config.get("blacklist", {}).get("file"))

    if args.action == "list":
        ids, names, note = load_blacklist(path)
        if note:
            print(f"Блэклист пуст ({note}): {path}")
            return 0
        print(f"Блэклист: {len(ids)} id, {len(names)} имён — {path}")
        print(path.read_text(encoding="utf-8").rstrip())
        return 0

    if not args.vacancy_id:
        print("Ошибка: add требует ID вакансии или ссылку", file=sys.stderr)
        return 2
    raw = str(args.vacancy_id).strip().rstrip("/")
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    if not raw.isdigit():
        print(f"Ошибка: непонятный ID вакансии: {args.vacancy_id!r}", file=sys.stderr)
        return 2

    from .hh_tool import SavingHHApplicantTool

    tool = SavingHHApplicantTool()
    vacancy = tool.api_client.get(f"/vacancies/{raw}") or {}
    employer = vacancy.get("employer") or {}
    emp_id = str(employer.get("id") or "").strip()
    name = str(employer.get("name") or "").strip()
    if not emp_id and not name:
        print(f"Ошибка: у вакансии {raw} нет работодателя", file=sys.stderr)
        return 2

    ids, names, _ = load_blacklist(path)
    if (emp_id and emp_id in ids) or (name and _norm_name(name) in names):
        print(f"Уже в блэклисте: {emp_id} {name}".strip())
        return 0

    line = f"{emp_id} {name}".strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(f"Добавлено: {line} → {path}")
    print("Действует со следующего прогона снпрера.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
