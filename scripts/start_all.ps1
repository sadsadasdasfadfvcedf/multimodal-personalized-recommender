# 一键启动：推荐系统(:8010) + 本项目网页界面(:8020)
# 用法: pwsh -File scripts\start_all.ps1
#
# 外部依赖一律从环境变量解析，与 config/settings.py 的口径保持一致：
#   * 不硬编码绝对路径 —— 未设置 INTEGRATED_RECOMMENDER_ROOT 时按常见位置探测；
#   * 不硬编码口令     —— 未设置 INTEGRATED_DEMO_PASSWORD 时，HTTP 模式会被自动跳过
#                         （见 config/settings.py 第 10 条「不硬编码密钥」）。
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "outputs"

# ---------- 外部推荐系统根目录：环境变量优先，其次常见位置探测 ----------
if (-not $env:INTEGRATED_RECOMMENDER_ROOT) {
    $candidates = @(
        (Join-Path (Split-Path -Parent $root) "推荐系统"),
        (Join-Path $env:USERPROFILE "Desktop\推荐系统"),
        (Join-Path $env:USERPROFILE "Documents\推荐系统"),
        (Join-Path $env:USERPROFILE "Documents\trae_projects\推荐系统")
    )
    $hit = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($hit) { $env:INTEGRATED_RECOMMENDER_ROOT = $hit }
}
if (-not $env:INTEGRATED_RECOMMENDER_ROOT) {
    Write-Error "未找到推荐系统目录。请先设置 `$env:INTEGRATED_RECOMMENDER_ROOT 指向推荐系统根目录。"
    exit 1
}
$recsys = Join-Path $env:INTEGRATED_RECOMMENDER_ROOT "v146_release"

# ---------- 演示口令：只从环境变量读取，绝不写进仓库 ----------
if (-not $env:INTEGRATED_DEMO_PASSWORD) {
    Write-Host "提示：未设置 INTEGRATED_DEMO_PASSWORD，推荐系统 HTTP 模式将被自动跳过。"
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = (Join-Path $env:INTEGRATED_RECOMMENDER_ROOT ".codex_deps") + ";" + $recsys
$env:INTEGRATED_MODE = "auto"
if (-not $env:INTEGRATED_DEMO_USER)  { $env:INTEGRATED_DEMO_USER  = "15547" }
if (-not $env:INTEGRATED_RECSYS_URL) { $env:INTEGRATED_RECSYS_URL = "http://127.0.0.1:8010" }

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