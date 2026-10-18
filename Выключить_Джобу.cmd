@echo off
rem hh-sniper job OFF: disable the task (stays registered, ON brings back).
title hh-sniper: job OFF
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"
"%PY%" -X utf8 -m sniper job off
"%PY%" -X utf8 -m sniper job status
echo.
pause
