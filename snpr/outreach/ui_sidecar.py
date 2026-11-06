"""Sidecar-менеджер UI outreach: фоновый процесс рядом с джобой (§7.2).

UI — блокирующий serve_forever, тик снайпера — минутный и завершающийся:
их нельзя смешивать. Вместо этого тик (тихий хук ensure_ui_running)
убеждается, что sidecar-процесс жив, и поднимает его при падении.

Живость — по pid-файлу + ping /api/stats: порт может быть занят чужим
процессом, поэтому «pid жив И порт отвечает» = здоров, иначе перезапуск.
Один владельный лок: data/outreach/ui.lock (файл-флаг), повторный тик
не плодит процессы.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.error import URLError

from .config import data_dir, outreach_config
from .pipeline import Paths


def _ui_proc_exe() -> str:
    return sys.executable or "python"


def _spawn_sidecar(paths: Paths, host: str, port: int) -> int | None:
    """Поднять detached-процесс `python -m snpr outreach ui`.

    Возврат: pid или None (spawn не удался — тихо, тик не задет).
    """
    try:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        proc = subprocess.Popen(
            [_ui_proc_exe(), "-X", "utf8", "-m", "snpr", "outreach", "ui"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            # корень проекта (каталог с пакетом snpr/), НЕ data_dir:
            # абсолютный data_dir вне проекта ломал cwd-хак parent.parent
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            creationflags=creationflags,
            env=env,
            close_fds=True,
        )
        return proc.pid
    except Exception:  # noqa: BLE001 — тихий sidecar
        return None


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, AttributeError, ValueError):
        return False


def _ping(host: str, port: int, timeout: float = 2.0) -> bool:
    """UI отвечает на /api/stats? (живой HTTP, не только порт открыт)."""
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"http://{host}:{port}/api/stats", timeout=timeout
        ) as resp:
            return resp.status == 200
    except (URLError, OSError, ValueError):
        return False


def ensure_ui_running(
    config: Mapping[str, Any] | None,
    *,
    start: bool = True,
) -> bool:
    """Тихий хук для тика снайпера: UI жив? нет — поднять sidecar.

    Условия: [outreach].enabled И [outreach.ui].enabled. Возврат: жив ли UI
    на момент выхода (до прогрева — True, если spawn прошёл).
    """
    cfg = outreach_config(dict(config or {}))
    if not (cfg.get("enabled") and cfg.get("ui", {}).get("enabled")):
        return False
    host = str(cfg.get("ui", {}).get("host", "127.0.0.1"))
    port = int(cfg.get("ui", {}).get("port", 8765))
    paths = Paths(data_dir(cfg))
    pidfile = paths.root / "ui.pid"

    pid: int | None = None
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None

    # жив: pid жив И HTTP отвечает
    if pid and _pid_alive(pid) and _ping(host, port):
        return True
    # pid жив, но порт не отвечает — зависший процесс: снять и пересоздать
    if pid and _pid_alive(pid) and not _ping(host, port):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            pass
        pidfile.unlink(missing_ok=True)
    elif pid and not _pid_alive(pid):
        pidfile.unlink(missing_ok=True)

    if not start:
        return False
    # 06.11 P1-фикс: лок вокруг check→spawn→write (докстринг обещал,
    # кода не было: два тика подряд спавнили два sidecar, pid-файл
    # перезаписывался, первый процесс становился сиротой)
    from .webui import _Lock

    with _Lock(paths.root / "ui.lock"):
        # повторная проверка под локом: конкурентный тик мог поднять
        if pidfile.exists():
            try:
                pid2 = int(pidfile.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid2 = None
            if pid2 and _pid_alive(pid2) and _ping(host, port):
                return True
        new_pid = _spawn_sidecar(paths, host, port)
        if new_pid is None:
            return False
        pidfile.write_text(str(new_pid), encoding="utf-8")
    # прогрев: до 5с ждём первый ответ (не блокируем тик надолго)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _ping(host, port):
            return True
        time.sleep(0.5)
    # 06.11 P1-фикс: честный ответ — процесс мог умереть сразу после
    # spawn; «UI жив» в логе тика не должен врать
    return _ping(host, port)
