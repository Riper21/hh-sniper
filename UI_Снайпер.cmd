@echo off
rem hh-snpr UI: open the outreach web interface in default browser.
rem UI must be alive (sidecar is kept by the sniper tick); if not —
rem raise it quietly, then open the browser.
title hh-snpr: UI
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"

rem 1) make sure the UI is up (same sidecar logic as the job tick):
"%PY%" -X utf8 -m snpr outreach ui-check
rem 2) open the browser:
start "" "http://127.0.0.1:8765"
