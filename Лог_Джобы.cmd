@echo off
rem hh-sniper job LOG: tail of data\scheduler.log (default 60 lines).
title hh-sniper: job log
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"
"%PY%" -X utf8 -m sniper job log -n 60
echo.
pause
