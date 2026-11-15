@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0..\.."
set "ROOT=%CD%"
set "VPY=%ROOT%\.venv\Scripts\python.exe"

echo ============================================================
echo   知纹 zhiwen ^- 自检（Windows）
echo ============================================================
echo.

if not exist "%VPY%" (
  echo [错误] 没有 .venv。请先双击 scripts\windows\部署.cmd
  pause
  exit /b 1
)

rem 读 .env
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if not "%%a"=="" if not "%%b"=="" set "%%a=%%b"
  )
) else (
  echo [注意] 没有 .env，将只做离线自检（不连服务、不连同步中心）。
)

echo --- 1. 版本与依赖 -------------------------------------------
"%VPY%" -c "import zw_core, sys; print('  zw-core', zw_core.__version__, ' on', sys.version.split()[0])"
if errorlevel 1 goto :broken

echo.
echo --- 2. 内核 ------------------------------------------------
if defined ZW_CHROME_BIN (
  if exist "%ZW_CHROME_BIN%" (
    echo   已指定：%ZW_CHROME_BIN%
    for /f "delims=" %%v in ('"%ZW_CHROME_BIN%" --version 2^>^&1') do echo   版本：%%v
  ) else (
    echo   [错误] ZW_CHROME_BIN 指向的文件不存在：%ZW_CHROME_BIN%
  )
) else (
  echo   未指定 ZW_CHROME_BIN，程序会自己找
)

echo.
echo --- 3. 端口池是否被系统保留段吃掉 --------------------------
if defined ZW_PORT_POOL_START (
  echo   程序使用的端口池：%ZW_PORT_POOL_START%-%ZW_PORT_POOL_END%
) else (
  echo   程序使用的端口池：21001-21100 ^(默认^)
)
echo   Windows 为 Hyper-V/WSL 保留的端口段：
powershell -NoProfile -Command "^
  $r = netsh int ipv4 show excludedportrange protocol=tcp; ^
  $r | Select-String -Pattern '\d+' | ForEach-Object { '    ' + $_.Line.Trim() }"

echo.
echo --- 4. 服务连通性 ------------------------------------------
powershell -NoProfile -Command "^
  try { ^
    $h = @{}; if ('%ZW_ADMIN_TOKEN%' -ne '') { $h['Authorization'] = 'Bearer %ZW_ADMIN_TOKEN%' }; ^
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8730/healthz' -TimeoutSec 5; ^
    Write-Host '  服务在跑：' $r.data.version ^
  } catch { Write-Host '  服务没起来（正常，如果你还没启动）。启动方式：scripts\windows\启动.cmd' }"

echo.
echo --- 5. 在线自检（需要服务在跑）-----------------------------
powershell -NoProfile -Command "^
  try { ^
    $h = @{}; if ('%ZW_ADMIN_TOKEN%' -ne '') { $h['Authorization'] = 'Bearer %ZW_ADMIN_TOKEN%' }; ^
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8730/api/v1/maintenance/doctor' -Method Post -Headers $h -TimeoutSec 20; ^
    foreach ($c in $r.data.checks) { ^
      $mark = if ($c.ok) { '[OK]  ' } else { '[!!]  ' }; ^
      $d = if ($c.'详情' -is [string]) { $c.'详情' } else { $c.'详情' | ConvertTo-Json -Compress -Depth 3 }; ^
      Write-Host ('  ' + $mark + $c.'项' + '  ' + $d) ^
    }; ^
    Write-Host ''; ^
    if ($r.data.all_ok) { Write-Host '  全部通过' } else { Write-Host '  有未通过项，见上' } ^
  } catch { Write-Host '  取不到自检结果：' $_.Exception.Message }"

echo.
echo --- 6. 完整的端到端自检（会真开浏览器，约 2 分钟）---------
echo   要跑就执行： scripts\windows\全量自检.cmd
echo.
pause
goto :eof

:broken
echo.
echo [错误] 依赖不完整，请重跑 scripts\windows\部署.cmd
pause
exit /b 1
