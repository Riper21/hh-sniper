@echo off
rem hh-snpr job STATUS: state, cadence, last/next run, marker, shots.
title hh-snpr: job status
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"
"%PY%" -X utf8 -m snpr job status
echo.
pause
