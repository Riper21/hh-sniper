@echo off
rem hh-snpr job RUN: send one tick NOW, wait, show the log tail.
title hh-snpr: job run now
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"
"%PY%" -X utf8 -m snpr job run
"%PY%" -X utf8 -c "print('\u0422\u0430\u043a\u0442 \u043f\u043e\u0448\u0451\u043b. \u0416\u0434\u0443 ~35 \u0441\u0435\u043a\u0443\u043d\u0434, \u043f\u043e\u0442\u043e\u043c \u043f\u043e\u043a\u0430\u0436\u0443 \u0445\u0432\u043e\u0441\u0442 \u043b\u043e\u0433\u0430...')"
timeout /t 35 /nobreak >nul
"%PY%" -X utf8 -m snpr job log -n 25
echo.
pause
