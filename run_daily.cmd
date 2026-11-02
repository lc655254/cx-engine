@echo off
REM Daily entry point for the cx-engine batch.
REM
REM Why a wrapper instead of pointing the task straight at python:
REM   * the scheduled task's stdout is discarded, so a run left no trace of what the
REM     supervisor decided (budget gate, preflight result, which phase it reached)
REM   * this redirects everything to logs\scheduler.log, and rotates it once
REM
REM chcp 65001 matters: cmd's %DATE%/%TIME% expand to locale text, which on a Chinese
REM system is GBK bytes. Python writes UTF-8. Mixing the two into one file produced
REM   UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd0
REM and made the log unreadable. Switching the console to UTF-8 first keeps it uniform.

setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if exist "logs\scheduler.log" (
    if exist "logs\scheduler.prev.log" del /q "logs\scheduler.prev.log"
    move /y "logs\scheduler.log" "logs\scheduler.prev.log" >nul 2>&1
)

echo ============================================================ >> "logs\scheduler.log"
echo [%DATE% %TIME%] cx-engine daily run starting >> "logs\scheduler.log"
echo ============================================================ >> "logs\scheduler.log"

".venv\Scripts\python.exe" -X utf8 -c "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace')" >nul 2>&1
".venv\Scripts\python.exe" -X utf8 schedule_resume.py >> "logs\scheduler.log" 2>&1

echo [%DATE% %TIME%] cx-engine daily run finished >> "logs\scheduler.log"
endlocal
