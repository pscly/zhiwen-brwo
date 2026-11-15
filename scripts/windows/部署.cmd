@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0..\.."
set "ROOT=%CD%"

echo ============================================================
echo   知纹 zhiwen ^- 一键部署（Windows）
echo ============================================================
echo   项目目录：%ROOT%
echo.

rem ── 0. 前置检查 ───────────────────────────────────────────
where powershell >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 powershell，请确认这是 Windows 系统。
  goto :fail
)

set "PY="
for %%p in (python.exe python3.exe) do (
  if not defined PY (
    for /f "delims=" %%f in ('where %%p 2^>nul') do (
      if not defined PY set "PY=%%f"
    )
  )
)
if not defined PY (
  echo [错误] 没找到 python。请先安装 Python 3.11 或更高版本：
  echo        https://www.python.org/downloads/windows/
  echo        安装时务必勾选 "Add python.exe to PATH"。
  goto :fail
)
for /f "tokens=2" %%v in ('"%PY%" --version 2^>^&1') do set "PYVER=%%v"
echo [1/6] Python：%PY%  ^(版本 %PYVER%^)

rem ── 1. 用 uv 建虚拟环境（没有就退回 venv）─────────────────
set "UV="
for /f "delims=" %%f in ('where uv.exe 2^>nul') do if not defined UV set "UV=%%f"
if exist ".venv\Scripts\python.exe" (
  echo [2/6] 虚拟环境已存在，跳过创建
) else (
  if defined UV (
    echo [2/6] 用 uv 创建虚拟环境…
    "%UV%" venv .venv
  ) else (
    echo [2/6] 没装 uv，改用 python -m venv（会慢一些）…
    "%PY%" -m venv .venv
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo [错误] 虚拟环境创建失败。请查看上面的输出。
  goto :fail
)
set "VPY=%ROOT%\.venv\Scripts\python.exe"

rem ── 2. 安装依赖 ───────────────────────────────────────────
echo [3/6] 安装依赖（首次需要联网，可能要一两分钟）…
if defined UV (
  "%UV%" pip install --python "%VPY%" -e "%ROOT%\core" 2>nul
) else (
  "%VPY%" -m pip install --upgrade pip >nul 2>nul
  "%VPY%" -m pip install -e "%ROOT%\core"
)
if errorlevel 1 (
  echo [错误] 依赖安装失败。国内网络建议先设置镜像：
  echo        set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
  goto :fail
)
"%VPY%" -c "import zw_core" >nul 2>nul
if errorlevel 1 (
  echo [错误] 装完了但 import 不了 zw_core，请看上面的报错。
  goto :fail
)

rem ── 3. 生成配置文件 ───────────────────────────────────────
echo [4/6] 准备配置文件 .env …
if not exist ".env" (
  rem 生成两个随机串：管理令牌与同步主密码
  for /f "delims=" %%t in ('"%VPY%" -c "import secrets;print('zw_' + secrets.token_urlsafe(30))"') do set "TOK=%%t"
  for /f "delims=" %%p in ('"%VPY%" -c "import secrets;print(secrets.token_urlsafe(18))"') do set "SYNPW=%%p"
  > .env echo # 由 部署.cmd 自动生成。这个文件含密钥，已在 .gitignore 里，不要提交、不要外发。
  >> .env echo ZW_ADMIN_TOKEN=!TOK!
  >> .env echo ZW_SYNC_PASSWORD=!SYNPW!
  >> .env echo # 想连同步中心就把下面两行取消注释并填好
  >> .env echo #ZW_HUB_URL=http://192.168.3.5:8700
  >> .env echo #ZW_HUB_TOKEN=在这里填同步中心的令牌
  >> .env echo # 端口池：如果 21001 被 Hyper-V 占了，改这两行
  >> .env echo ZW_PORT_POOL_START=21001
  >> .env echo ZW_PORT_POOL_END=21100
  echo    已生成 .env（管理令牌与同步主密码都是随机生成的）
  echo    ^>^> 你的访问令牌：!TOK!
  echo    ^>^> 你的同步主密码：!SYNPW!     ^(丢了这个就解不开远端快照，请抄下来^)
) else (
  echo    .env 已存在，沿用原配置
)

rem ── 4. 找浏览器内核 ───────────────────────────────────────
echo [5/6] 查找浏览器内核…
set "ZW_ENGINE="
if exist "%LOCALAPPDATA%\zhiwen\engines" (
  for /f "delims=" %%f in ('dir /b /s "%LOCALAPPDATA%\zhiwen\engines\chrome.exe" 2^>nul') do if not defined ZW_ENGINE set "ZW_ENGINE=%%f"
)
if not defined ZW_ENGINE if exist "%PROGRAMFILES%\Google\Chrome\Application\chrome.exe" set "ZW_ENGINE=%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"
if not defined ZW_ENGINE if exist "%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe" set "ZW_ENGINE=%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"
if not defined ZW_ENGINE if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "ZW_ENGINE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not defined ZW_ENGINE if exist "%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe" set "ZW_ENGINE=%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"

if defined ZW_ENGINE (
  echo     找到内核：!ZW_ENGINE!
  findstr /b /c:"ZW_CHROME_BIN=" .env >nul 2>nul
  if errorlevel 1 >> .env echo ZW_CHROME_BIN=!ZW_ENGINE!
) else (
  echo     [注意] 没找到 Chrome/Chromium。程序仍可启动，但启动环境会报 ENGINE_NOT_FOUND。
  echo            两种补法：
  echo              1^) 装个 Chrome
  echo              2^) 下载 fingerprint-chromium 解压到 %%LOCALAPPDATA%%\zhiwen\engines\
  echo                 下载页：https://github.com/adryfish/fingerprint-chromium/releases
)

rem ── 5. 自检 + 起服务 ──────────────────────────────────────
echo [6/6] 启动服务…
echo.
echo ------------------------------------------------------------
echo   控制台地址：http://127.0.0.1:8730
echo   访问令牌就在上面（也记在 .env 里）
echo   关闭这个窗口 = 停止服务
echo ------------------------------------------------------------
echo.

rem 把 .env 里的变量读进当前进程
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
  if not "%%a"=="" if not "%%b"=="" set "%%a=%%b"
)
"%VPY%" -m zw_core.main

echo.
echo 服务已退出。
pause
goto :eof

:fail
echo.
echo 部署未完成，请按上面的提示处理后重跑本脚本。
pause
exit /b 1
