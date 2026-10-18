"""Управление джобой hh-sniper (Task Scheduler) + живой статус + хвост лога.

Джоба — задача `hh-sniper`: каждые 5 минут скрыто гонит snipe_job.cmd
(wscript: консоль не мигает). Управление — schtasks (флаги ASCII),
статус — PowerShell-JSON (значения не локализованы: Ready/PT5M).
`status` добавляет живые артефакты прогона: маркер последнего успешного
такта (data/last_run.txt), лок (data/snipe.lock), парковка, выстрелы.

CLI: python -m sniper job status|on|off|run|log [-n 60]
Клик-обёртка: scripts\job.cmd (без аргументов = status).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .cli import DEFAULT_APPLIED_STORE, DEFAULT_PARKED_STORE, PROJECT_ROOT

TASK_NAME = "hh-sniper"
LOG_PATH = PROJECT_ROOT / "data" / "scheduler.log"
MARKER_PATH = PROJECT_ROOT / "data" / "last_run.txt"
LOCK_PATH = PROJECT_ROOT / "data" / "snipe.lock"
REGISTER_SCRIPT = PROJECT_ROOT / "scripts" / "register_task.ps1"

# Значения намеренно не локализованы (Ready/PT5M/цифры): парсим без мучений
# с кодировками, локализованный schtasks-вывод не трогаем вообще.
_STATUS_PS = (
    "$ErrorActionPreference='Stop';"
    "try{"
    "$t=Get-ScheduledTask -TaskName 'hh-sniper';"
    "$i=Get-ScheduledTaskInfo -TaskName 'hh-sniper';"
    "[pscustomobject]@{"
    "state=[string]$t.State;"
    "interval=[string]$t.Triggers[0].Repetition.Interval;"
    "last=[string]$i.LastRunTime;"
    "next=[string]$i.NextRunTime;"
    "result=[int]$i.LastTaskResult;"
    "missed=[int]$i.NumberOfMissedRuns"
    "} | ConvertTo-Json -Compress"
    "}catch{'null'}"
)


def _schtasks(*flags: str) -> tuple[int, str]:
    """schtasks с флагами; возврат (код, текст). Вывод декодируем cp866."""
    proc = subprocess.run(
        ["schtasks", *flags], capture_output=True, check=False
    )
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    return proc.returncode, raw.decode("cp866", errors="replace").strip()


def _task_status() -> dict[str, Any] | None:
    """None — задача не зарегистрирована (или PowerShell не справился)."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _STATUS_PS],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw or raw == "null":
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _register_via_ps1() -> bool:
    """Зарегистрировать задачу scripts/register_task.ps1 (каждые 5 мин)."""
    if not REGISTER_SCRIPT.exists():
        print(f"Не найден {REGISTER_SCRIPT}", file=sys.stderr)
        return False
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REGISTER_SCRIPT),
        ],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("cp866", errors="replace")
        print(f"Регистрация не удалась: {err}", file=sys.stderr)
        return False
    return True


def _file_count(path: Path) -> int:
    """Строк-записей JSONL (пустой/нет файла — 0)."""
    try:
        with path.open(encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def _marker_age_minutes() -> float | None:
    """Сколько минут назад закрылся последний УСПЕШНЫЙ такт."""
    try:
        ts = float(MARKER_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return max(0.0, (time.time() - ts) / 60)


def _log_size() -> str:
    try:
        size = LOG_PATH.stat().st_size
    except OSError:
        return "нет файла"
    for unit in ("Б", "КБ", "МБ"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} МБ"


def job_status() -> int:
    data = _task_status()
    if data is None:
        print("Задача hh-sniper НЕ зарегистрирована.")
        print("Включить и зарегистрировать: python -m sniper job on")
        return 1
    state = str(data.get("state") or "?")
    interval = str(data.get("interval") or "?")
    print(f"Джоба hh-sniper: {state}, каденция {interval}")
    print(
        f"  последний запуск: {data.get('last') or '?'} "
        f"(код {data.get('result')}; 0 = ок), "
        f"пропущено тиков: {data.get('missed')}"
    )
    print(f"  следующий запуск: {data.get('next') or '?'}")
    if state.lower().startswith("dis"):
        print("  ДЖОБА ВЫКЛЮЧЕНА — включить: python -m sniper job on")
    age = _marker_age_minutes()
    if age is None:
        print("  успешных прогонов ещё не было (маркера нет)")
    else:
        print(f"  последний успешный прогон: {age:.0f} мин назад")
    print(f"  прогон идёт прямо сейчас: {'да' if LOCK_PATH.exists() else 'нет'}")
    print(f"  в парковке на approve: {_file_count(DEFAULT_PARKED_STORE)}")
    print(f"  выстрелов всего: {_file_count(DEFAULT_APPLIED_STORE)}")
    print(f"  лог: {LOG_PATH} ({_log_size()}); хвост: python -m sniper job log")
    return 0


def job_on() -> int:
    rc, out = _schtasks("/change", "/tn", TASK_NAME, "/enable")
    if rc != 0:
        # нет задачи (или слетела) — регистрируем заново тем же скриптом
        print("Задача не найдена — регистрирую через register_task.ps1...")
        if not _register_via_ps1():
            print(f"schtasks сказал: {out}", file=sys.stderr)
            return 2
    data = _task_status()
    if data is not None:
        print(
            f"Джоба ВКЛЮЧЕНА: {data.get('state')} {data.get('interval')}, "
            f"следующий запуск {data.get('next') or '?'}"
        )
    else:
        print("Джоба включена (статус получить не удалось).")
    return 0


def job_off() -> int:
    rc, out = _schtasks("/change", "/tn", TASK_NAME, "/disable")
    if rc != 0:
        print(f"Не удалось выключить: {out}", file=sys.stderr)
        return 2
    print("Джоба ВЫКЛЮЧЕНА (задача осталась в планировщике).")
    print("Включить обратно: python -m sniper job on")
    print("Идущий прогон доработает; уже отправленные отклики не отзываются.")
    return 0


def job_run() -> int:
    rc, out = _schtasks("/run", "/tn", TASK_NAME)
    if rc != 0:
        print(f"Не удалось запустить такт: {out}", file=sys.stderr)
        return 2
    print("Такт запущен вручную (скрыто, как обычная джоба).")
    print("Хвост лога: python -m sniper job log -n 30")
    return 0


def job_log(lines: int = 60) -> int:
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(f"Лога нет: {LOG_PATH}")
        return 1
    all_lines = text.splitlines()
    tail = all_lines[-lines:] if lines > 0 else all_lines
    print("\n".join(tail))
    return 0


def run_job_command(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "status") or "status")
    if action == "on":
        return job_on()
    if action == "off":
        return job_off()
    if action == "run":
        return job_run()
    if action == "log":
        return job_log(int(getattr(args, "lines", 60) or 60))
    return job_status()
