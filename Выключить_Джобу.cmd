@echo off
rem hh-snpr job OFF: disable the task (stays registered, ON brings back).
title hh-snpr: job OFF
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"
"%PY%" -X utf8 -m snpr job off
"%PY%" -X utf8 -m snpr job status
echo.
pause
