@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
if "%ZW_ADMIN_TOKEN%"=="" set /p ZW_ADMIN_TOKEN=访问令牌:
.venv\Scripts\python -m zw_core.cli doctor
echo.
pause
