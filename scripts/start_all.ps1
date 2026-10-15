# 一键启动：推荐系统(:8010) + 本项目网页界面(:8020)
# 用法: pwsh -File scripts\start_all.ps1
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "outputs"
$recsys = "C:\Users\Administrator\Desktop\推荐系统\v146_release"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = "C:\Users\Administrator\Desktop\推荐系统\.codex_deps;" + $recsys
$env:INTEGRATED_MODE = "auto"
$env:INTEGRATED_DEMO_USER = "15547"
$env:INTEGRATED_DEMO_PASSWORD = "demo123"
$env:INTEGRATED_RECSYS_URL = "http://127.0.0.1:8010"

function Stop-Port($port) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($c) { $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }
    Start-Sleep 2
}
Stop-Port 8010
Stop-Port 8020

$r1 = Start-Process -FilePath "python" -ArgumentList "-m","uvicorn","app:app","--app-dir","webapp","--host","127.0.0.1","--port","8010" -WorkingDirectory $recsys -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $out "_recsys.log") -RedirectStandardError (Join-Path $out "_recsys.log.err")
Start-Sleep 6

$r2 = Start-Process -FilePath "python" -ArgumentList "-B","webui.py","--real","--voice","--port","8020","--no-browser" -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $out "_web.log") -RedirectStandardError (Join-Path $out "_web.log.err")
Start-Sleep 8

Write-Host ("recsys PID = " + $r1.Id)
Write-Host ("webui  PID = " + $r2.Id)
foreach ($p in 8010,8020) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    Write-Host ("port " + $p + " listening = " + [bool]$c)
}
