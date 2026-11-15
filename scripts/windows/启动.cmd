@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0..\.."

echo ============================================
echo   知纹 (zhiwen) 指纹浏览器机群 - 启动
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 没找到 .venv，请先在项目根目录执行：
  echo    uv venv .venv
  echo    uv pip install --python .venv\Scripts\python.exe -e .\core
  pause
  exit /b 1
)

rem 令牌：优先用已存在的用户环境变量，否则提示输入（当前窗口有效）
if "%ZW_ADMIN_TOKEN%"=="" (
  set /p ZW_ADMIN_TOKEN=请设置访问令牌（留空则仅本机可用）:
)
if "%ZW_HUB_URL%"=="" (
  set /p ZW_HUB_URL=同步中心地址（例如 http://192.168.3.5:8700，留空跳过）:
)
if not "%ZW_HUB_URL%"=="" if "%ZW_HUB_TOKEN%"=="" (
  set /p ZW_HUB_TOKEN=同步中心令牌:
)
if not "%ZW_HUB_URL%"=="" if "%ZW_SYNC_PASSWORD%"=="" (
  set /p ZW_SYNC_PASSWORD=同步主密码（千万别丢）:
)

echo.
echo 正在启动…… 启动后打开 http://127.0.0.1:8730
echo 关闭本窗口即停止服务。
echo.
.venv\Scripts\python -m zw_core.main
pause
