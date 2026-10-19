"""Демон hh-snpr: кроссплатформенный тикер — такты без планировщика ОС.

Windows-джоба (Task Scheduler → scripts/sne_job.cmd) остаётся рабочим
путём; демон — тот же тик на Linux/macOS/Docker/VPS/tmux (этап 1 плана
кроссплатформенности 18.10). Каждый тик — subprocess
`python -m snpr sne --mode park`: свежий процесс на такт (длинный цикл
без утечек памяти, сбой тика не роняет демон), stdout/stderr — в
data/scheduler.log тем же форматом, что sne_job.cmd (===== дата =====).
Дублей между тиками нет: прогон сам держит data/sne.lock (pid-проверка
в sne) — демон + джоба одновременно просто скипаются друг по другу.
data/daemon.lock — против ВТОРОГО демона (pid жив → отказ старта).

После тика: парковка непуста → показать approve. Windows: как
sne_job.cmd (анти-дубль окна по pid-локу data/approve.lock + beep;
19.10 — поиск по заголовку не видел окно в Windows Terminal).
Не-Windows: BEL + подсказка в stdout — approve открывают руками
(tmux/SSH); авто-окно терминала — этап 2. systemd/launchd-юниты и
Docker — этапы 2-3.

CLI: python -m snpr daemon [--interval N] [--once] [--config ...]
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .cli import DEFAULT_PARKED_STORE, PROJECT_ROOT
from .job import LOG_PATH

DAEMON_LOCK_PATH = PROJECT_ROOT / "data" / "daemon.lock"

# Пол интервала: руками можно опечататься в ноль — hh не должен получать
# шторм прогонов. Каденция ниже 5 с смысла не имеет и с живым конвейером.
_MIN_INTERVAL = 5.0


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _tick_command(args: argparse.Namespace) -> list[str]:
    """Команда такта: тот же вызов, что гонит sne_job.cmd (--mode park).

    --config — корневой аргумент cli, идёт ДО подкоманды. Отсутствует
    (тестовый namespace) — не передаём, sne возьмёт свой дефолт.
    """
    cmd = [sys.executable, "-X", "utf8", "-m", "snpr"]
    config = str(getattr(args, "config", "") or "")
    if config:
        cmd += ["--config", config]
    cmd += ["sne", "--mode", "park"]
    return cmd


def _tick(cmd: list[str], log_path: Path) -> int:
    """Один такт: subprocess, вывод дописывается в лог (как sne_job.cmd).

    Возврат — rc прогона (сбой тика НЕ роняет демон, rc просто попадает
    в сводку). Не смог запуститься вовсе — 127 + причина в лог.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"===== {_stamp()} =====\n")
            # шапку — на диск ДО спавна: потомок пишет в тот же fd мимо
            # нашего буфера, без flush она всплыла бы только после тика
            # (убитый по таймауту тик 18.10 22:41 потерял шапку — искали
            # «зависание», а тик работал в волне скоринга)
            fh.flush()
            proc = subprocess.run(
                cmd,
                stdout=fh,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                env=env,
                check=False,
            )
            return proc.returncode
    except OSError as exc:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"тик не стартовал: {exc}\n")
        return 127


def _parked_nonempty(parked: Path) -> bool:
    """Проверка как в sne_job.cmd: файл есть и есть непустые строки."""
    if not parked.exists():
        return False
    try:
        with parked.open(encoding="utf-8") as fh:
            return any(line.strip() for line in fh)
    except OSError:
        return False


def _approve_running() -> bool:
    """Окно approve живо? pid-лок data/approve.lock (не заголовок окна:
    19.10 — Windows Terminal хостит окно в WindowsTerminal.exe,
    tasklist по WINDOWTITLE+cmd.exe его не находил, окна плодились)."""
    from .sne import approve_lock_alive

    return approve_lock_alive()


def _open_approve() -> None:
    """Win: новое окно `snpr approve` + beep (как sne_job.cmd, тот же
    заголовок — анти-дубль работает и против окна джобы). Не-Windows:
    BEL + подсказка: юзер в tmux/SSH сам открывает approve."""
    if sys.platform == "win32":
        inner = (
            f'cd /d "{PROJECT_ROOT}" && '
            f'"{sys.executable}" -X utf8 -m snpr approve'
        )
        subprocess.Popen(
            f'start "hh-snpr: ready to send" cmd /k "{inner}"',
            shell=True,
        )
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "[console]::beep(800,300); [console]::beep(1000,400)",
            ],
        )
        return
    print("\a", end="", flush=True)
    print("есть на решение: python -m snpr approve", flush=True)


def _maybe_open_approve(parked: Path) -> bool:
    """Парковка непуста → показать approve (окно не дублируем)."""
    if not _parked_nonempty(parked):
        return False
    if _approve_running():
        return False
    _open_approve()
    return True


def _wait(delay: float, is_stopped: Callable[[], bool]) -> None:
    """Спим остаток такта кусками по 0.2 с: Ctrl-C/SIGTERM ловится сразу,
    не через минуту."""
    deadline = time.monotonic() + max(0.0, delay)
    while not is_stopped() and time.monotonic() < deadline:
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def run_daemon(args: argparse.Namespace) -> int:
    """Тело демона: лок демона → цикл тиков → graceful стоп.

    interval приходит из cli (config [daemon].interval_seconds, дефолт
    60); здесь только пол от опечаток. Тик-сбой не завершает цикл.
    """
    from .sne import _acquire_run_lock, _release_run_lock

    interval = max(_MIN_INTERVAL, float(args.interval))
    log_path = LOG_PATH
    parked = Path(DEFAULT_PARKED_STORE)

    lock_fd = _acquire_run_lock(DAEMON_LOCK_PATH, stale_after=1800.0)
    if lock_fd is None:
        print(
            "демон уже запущен (data/daemon.lock жив) — второй не нужен: "
            "тики скипались бы друг по другу (sne.lock)"
        )
        return 1

    cmd = _tick_command(args)

    stop: dict[str, bool] = {"flag": False}

    def _on_signal(signum: int, frame: object) -> None:
        stop["flag"] = True

    saved_handlers: list[tuple[int, object]] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            saved_handlers.append((sig, signal.signal(sig, _on_signal)))
        except (ValueError, OSError):
            pass

    print(
        f"[{_stamp()}] демон: тик каждые {interval:.0f} с, лог {log_path} "
        f"(стоп: Ctrl-C)",
        flush=True,
    )
    rc = 0
    try:
        while not stop["flag"]:
            started = time.monotonic()
            rc = _tick(cmd, log_path)
            opened = _maybe_open_approve(parked)
            note = " → approve" if opened else ""
            print(f"[{_stamp()}] тик rc={rc}{note}", flush=True)
            if getattr(args, "once", False):
                return rc
            _wait(interval - (time.monotonic() - started), lambda: stop["flag"])
    except KeyboardInterrupt:
        pass
    finally:
        # сигналы вернуть: перехват SIGINT не должен пережить демона
        for sig, handler in saved_handlers:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        _release_run_lock(DAEMON_LOCK_PATH, lock_fd)
    print(f"[{_stamp()}] демон остановлен", flush=True)
    return rc
