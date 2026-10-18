"""Поднятие резюме: POST /resumes/{id}/publish (бесплатный буст видимости).

hh ранжирует резюме в поиске по свежести поднятия — автоподнятие каждые
4 часа в рабочие часы — единственный «плюс к видимости», который боты
конкурентов не используют (транспорт подсмотрен у базы, update_resumes).

Правила безопасности:
- marker data/last_raise.txt: интервал между поднятиями не меньше
  interval_hours (hh сам разрешает раз в 4 ч — не дёргаем лишний раз);
- окно active_from_hour..active_to_hour по МСК: ночью не поднимаем;
- резюме поднимается только если hh ставит can_publish_or_update
  (свежее поднятие руками = флаг False = тихий пропуск);
- сбой поднятия НЕ ломает основной прогон снпрера (try/except в хуке).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

MSK = timezone(timedelta(hours=3))


def _read_marker_ts(marker: Path) -> float:
    try:
        return float(marker.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0.0


def should_raise(
    marker: Path,
    interval_hours: float,
    from_hour: int,
    to_hour: int,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Пора ли поднимать: интервал + рабочее окно по МСК."""
    moment = now or datetime.now(MSK)
    if not (from_hour <= moment.hour < to_hour):
        return False, f"вне окна {from_hour:02d}:00-{to_hour:02d}:00 МСК"
    elapsed_h = (moment.timestamp() - _read_marker_ts(marker)) / 3600
    if elapsed_h < interval_hours:
        return False, f"прошло {elapsed_h:.1f} ч < {interval_hours:.1f} ч"
    return True, "пора"


def raise_resume(
    api_client: Any,
    resume_id: str = "",
) -> tuple[bool, str]:
    """Одно поднятие: выбирает резюме и POST /resumes/{id}/publish."""
    res = api_client.get("/resumes/mine")
    items = res.get("items", []) if isinstance(res, dict) else []
    if not items:
        return False, "на аккаунте нет резюме"
    resume = None
    if resume_id:
        for item in items:
            if str(item.get("id")) == str(resume_id):
                resume = item
                break
        if resume is None:
            return False, f"резюме {resume_id} не найдено"
    else:
        resume = items[0]
    if not resume.get("can_publish_or_update"):
        return False, "hh не разрешает сейчас (недавнее поднятие)"
    rid = str(resume["id"])
    result = api_client.post(f"/resumes/{rid}/publish")
    if result != {}:
        return False, f"неожиданный ответ /publish: {result!r:.100}"
    return True, f"поднято: {resume.get('title') or rid}"


def write_marker(marker: Path, now: datetime | None = None) -> None:
    moment = now or datetime.now(MSK)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(moment.timestamp()), encoding="utf-8")


def maybe_auto_raise(
    config: Mapping[str, Any],
    api_client: Any,
    data_dir: Path,
    dry_run: bool,
    now: datetime | None = None,
) -> bool:
    """Хук из run_sne: тихое автоподнятие по правилам [raise]."""
    raise_cfg = config.get("raise", {})
    if not raise_cfg.get("enabled", False):
        return False
    marker = data_dir / "last_raise.txt"
    interval = float(raise_cfg.get("interval_hours", 4) or 4)
    from_hour = int(raise_cfg.get("active_from_hour", 9))
    to_hour = int(raise_cfg.get("active_to_hour", 19))
    due, why = should_raise(marker, interval, from_hour, to_hour, now=now)
    if not due:
        return False
    if dry_run:
        print(f"[raise] dry-run: поднятие пропущено ({why})")
        return False
    try:
        ok, message = raise_resume(api_client, str(raise_cfg.get("resume_id", "") or ""))
    except Exception as ex:
        print(f"[raise] ошибка поднятия, прогон продолжается: {ex}")
        return False
    if ok:
        write_marker(marker, now=now)
        print(f"[raise] {message}")
    else:
        # hh не даёт — маркер тоже двигаем, чтобы не долбить /resumes/mine
        # каждым тактом (ближайшая попытка через интервал)
        write_marker(marker, now=now)
        print(f"[raise] {message} (следующая попытка через {interval:.0f} ч)")
    return ok


def run_raise(args: argparse.Namespace, api_client: Any) -> int:
    """CLI: snpr raise — поднятие руками, --force мимо окна и маркера."""
    if args.force:
        ok, message = raise_resume(api_client, args.resume_id)
        print(("[raise] " if not ok else "") + message)
        return 0 if ok else 1
    marker = Path(args.marker)
    due, why = should_raise(
        marker,
        float(args.interval_hours),
        int(args.from_hour),
        int(args.to_hour),
    )
    if not due:
        print(f"[raise] пропущено: {why}")
        return 0
    ok, message = raise_resume(api_client, args.resume_id)
    if ok:
        write_marker(marker)
        print(f"[raise] {message}")
        return 0
    print(f"[raise] {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit("CLI-точка входа — python -m snpr raise")
