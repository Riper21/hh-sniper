"""Блок управления джобой в UI (06.11): on/off/run/status из браузера.

Переиспользует snpr.job (Task Scheduler) — те же операции, что у
`python -m snpr job status|on|off|run`, но через /api/job/*.
Безопасность: действия локальные (127.0.0.1), флаги ASCII в schtasks.

Не-Windows: job.py честно направляет на systemd/launchd — блок в UI
показывает тот же вердикт (статус-артефакты data/ платформенно-нейтральны).
"""
from __future__ import annotations

import subprocess
from typing import Any

from ..job import (
    TASK_NAME,
    _marker_age_minutes,
    _register_via_ps1,
    _schtasks,
    _task_status,
)

def job_snapshot() -> dict[str, Any]:
    """Снимок статуса для UI: задача + живые артефакты тикера."""
    data = _task_status()
    snapshot: dict[str, Any] = {
        "registered": data is not None,
        "state": str((data or {}).get("state") or "?"),
        "interval": str((data or {}).get("interval") or "?"),
        "last_run": str((data or {}).get("last") or ""),
        "next_run": str((data or {}).get("next") or ""),
        "last_result": (data or {}).get("result"),
        "missed": (data or {}).get("missed"),
        "is_windows": True,
    }
    import sys

    snapshot["is_windows"] = sys.platform == "win32"
    age = _marker_age_minutes()
    snapshot["last_success_min"] = round(age, 1) if age is not None else None
    from ..job import LOCK_PATH

    snapshot["running_now"] = LOCK_PATH.exists()
    return snapshot


def job_toggle(action: str) -> dict[str, Any]:
    """on|off|run через schtasks (как python -m snpr job …)."""
    if action not in ("on", "off", "run"):
        return {"ok": False, "error": f"неизвестное действие: {action}"}
    if action == "run":
        rc, out = _schtasks("/run", "/tn", TASK_NAME)
        return {
            "ok": rc == 0,
            "message": "такт запущен вручную (скрыто)"
            if rc == 0
            else f"не удалось: {out}",
        }
    flags = ("/enable",) if action == "on" else ("/disable",)
    rc, out = _schtasks("/change", "/tn", TASK_NAME, *flags)
    if rc != 0 and action == "on":
        # нет задачи — регистрируем (как job_on у CLI)
        if _register_via_ps1():
            rc, out = 0, ""
        else:
            rc, out = 2, out
    return {
        "ok": rc == 0,
        "message": f"джоба {'ВКЛЮЧЕНА' if action == 'on' else 'ВЫКЛЮЧЕНА'}"
        if rc == 0
        else f"не удалось: {out}",
    }


def open_in_browser(url: str = "http://127.0.0.1:8765") -> dict[str, Any]:
    """Открыть дефолтный браузер на UI (кнопка «Открыть UI»)."""
    import webbrowser

    try:
        opened = webbrowser.open(url)
        return {"ok": opened, "message": f"браузер открыт: {url}"}
    except Exception as ex:  # noqa: BLE001 — тихо
        return {"ok": False, "message": f"не удалось: {ex}"}
