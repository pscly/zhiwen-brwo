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

rem 读 .env（这是唯一的配置来源；不要在脚本里再硬编码密钥）
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if not "%%a"=="" if not "%%b"=="" set "%%a=%%b"
  )
) else (
  echo [错误] 没有 .env。请先双击 scripts\windows\部署.cmd 生成它。
  pause
  exit /b 1
)

echo ============================================================
echo   知纹 zhiwen ^- 启动
echo ============================================================
echo   控制台：http://127.0.0.1:8730
echo   数据目录：%LOCALAPPDATA%\zhiwen    ^(可用 .env 里的 ZW_ROOT 改^)
echo   关闭本窗口即停止服务。
echo ============================================================
echo.

"%VPY%" -m zw_core.main
echo.
echo 服务已退出（退出码 %errorlevel%）。
echo 如果是异常退出，日志在：%LOCALAPPDATA%\zhiwen\logs\
pause
