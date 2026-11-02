@echo off
REM Resume the cx-engine batch after a logon IF nothing is running.
REM
REM Why this exists: the machine was rebooted by accident twice on 2026-10-18
REM (18:20 and 20:04) and each reboot silently ended the batch for hours.
REM This task fires 2 minutes after logon; it only asks the main task to start
REM when no python process is alive, and the main task's IgnoreNew setting makes
REM a duplicate start a no-op anyway. schedule_resume.py keeps its own 07:00-23:00
REM window guard, so an off-hours logon starts nothing.

tasklist /FI "IMAGENAME eq python.exe" 2>nul | find /I "python.exe" >nul
if errorlevel 1 (
  schtasks /Run /TN "cx-engine-daily" >nul 2>&1
)
exit /b 0
