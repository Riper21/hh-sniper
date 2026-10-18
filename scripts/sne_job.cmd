@echo off
rem hh-snpr scheduler job (every 1 min since 18.10 ~02:00; was 5 min, 2h).
rem Rationale: hh views are not limited (only responses are) + the send age
rem gate is 10 minutes (config [send] max_age_minutes) - we must catch
rem vacancies within minutes of publication.
rem Scores without a human (--mode park): auto-send >= auto_approve_min
rem (fresh <= 10 min only), the rest accumulates in data\parked.jsonl.
rem If there is something to decide - opens the approve window (y/n/e)
rem and beeps. Does not duplicate an already-open window.
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
set "LOG=data\scheduler.log"
set "PYTHONIOENCODING=utf8"

echo ===== %date% %time% ===== >> "%LOG%"
"%PY%" -X utf8 -m snpr sne --mode park >> "%LOG%" 2>&1
set "SNE_RC=%ERRORLEVEL%"

rem anything parked? (parked.jsonl non-empty)
"%PY%" -X utf8 -c "import sys, pathlib; p = pathlib.Path('data/parked.jsonl'); sys.exit(0 if p.exists() and sum(1 for l in p.open(encoding='utf-8') if l.strip()) else 1)" >nul 2>&1
if %ERRORLEVEL%==0 (
    rem approve window already open? do not duplicate
    tasklist /FI "WINDOWTITLE eq hh-snpr*" 2>nul | find /I "cmd.exe" >nul
    if ERRORLEVEL 1 (
        start "hh-snpr: ready to send" cmd /k "cd /d %~dp0.. && "%PY%" -X utf8 -m snpr approve"
        powershell -NoProfile -Command "[console]::beep(800,300); [console]::beep(1000,400)"
    )
)
exit /b %SNE_RC%