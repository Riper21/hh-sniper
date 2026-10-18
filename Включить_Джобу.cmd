@echo off
rem hh-snpr job ON: enable the 5-min task (registers if missing).
title hh-snpr: job ON
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"
"%PY%" -X utf8 -m snpr job on
"%PY%" -X utf8 -m snpr job status
echo.
pause
