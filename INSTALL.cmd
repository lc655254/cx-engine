@echo off
REM 双击运行即可完成安装。
REM
REM   Double-click this file on the target machine (26.123.4.39 / LC655254).
REM   It runs setup.ps1 with the execution policy bypassed for this one call,
REM   which is what Windows otherwise blocks by default.

setlocal
cd /d "%~dp0"

echo ============================================================
echo  cx-engine 安装
echo  目录: %CD%
echo ============================================================
echo.

where powershell >nul 2>&1
if errorlevel 1 (
    echo [问题] 找不到 powershell.exe
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
set RC=%ERRORLEVEL%

echo.
if %RC% equ 0 (
    echo ------------------------------------------------------------
    echo  安装流程结束。请按上面的提示完成登录和计划任务注册。
    echo ------------------------------------------------------------
) else (
    echo ------------------------------------------------------------
    echo  安装未通过，请看上面的红色/黄色提示。
    echo  详细说明见 SETUP.md
    echo ------------------------------------------------------------
)

echo.
pause
endlocal
exit /b %RC%
