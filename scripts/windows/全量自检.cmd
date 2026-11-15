@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0..\.."
set "ROOT=%CD%"
set "VPY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%VPY%" (
  echo [错误] 没有 .venv。请先双击 scripts\windows\部署.cmd
  pause
  exit /b 1
)
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if not "%%a"=="" if not "%%b"=="" set "%%a=%%b"
  )
)

echo ============================================================
echo   知纹 zhiwen ^- 全量自检（会真启动浏览器，约 2 分钟）
echo ============================================================
echo   前提：服务已在另一个窗口跑着（scripts\windows\启动.cmd）
echo.
pause

set "ZW_CORE=http://127.0.0.1:8730"
"%VPY%" scripts\e2e-check.py

echo.
echo ============================================================
echo   单元测试（不依赖服务，只测代码逻辑）
echo ============================================================
"%VPY%" -m pytest tests -q
echo.
pause
