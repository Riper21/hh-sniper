@echo off
rem hh-sniper job STATUS: state, cadence, last/next run, marker, shots.
title hh-sniper: job status
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"
"%PY%" -X utf8 -m sniper job status
echo.
pause
