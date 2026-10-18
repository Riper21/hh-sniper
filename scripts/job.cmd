@echo off
rem hh-sniper job control wrapper (18.10).
rem Usage: job.cmd [status^|on^|off^|run^|log] [-n N]
rem No arguments = status. Log tail default 60 lines.
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"
if "%~1"=="" (
    "%PY%" -X utf8 -m sniper job status
) else (
    "%PY%" -X utf8 -m sniper job %*
)
