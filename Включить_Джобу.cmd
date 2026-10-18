@echo off
rem hh-sniper job ON: enable the 5-min task (registers if missing).
title hh-sniper: job ON
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"
"%PY%" -X utf8 -m sniper job on
"%PY%" -X utf8 -m sniper job status
echo.
pause
