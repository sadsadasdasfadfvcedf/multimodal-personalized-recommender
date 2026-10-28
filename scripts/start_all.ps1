# Start recommender/image service (:8010) and this web UI (:8020).
# Works with Windows PowerShell 5.1 and PowerShell 7.

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "outputs"
New-Item -ItemType Directory -Path $out -Force | Out-Null

if (-not $env:INTEGRATED_RECOMMENDER_ROOT) {
    $searchRoots = @(
        (Join-Path $env:USERPROFILE "Desktop"),
        (Join-Path $env:USERPROFILE "Documents"),
        (Join-Path $env:USERPROFILE "Documents\trae_projects"),
        (Split-Path -Parent $root)
    )
    $candidates = @()
    foreach ($base in $searchRoots) {
        if (-not (Test-Path -LiteralPath $base)) { continue }
        $candidates += Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "v146_release") } |
            ForEach-Object { $_.FullName }
    }
    $hit = $candidates | Where-Object {
        Test-Path -LiteralPath (Join-Path $_ "v146_release")
    } | Select-Object -First 1
    if ($hit) { $env:INTEGRATED_RECOMMENDER_ROOT = $hit }
}

if (-not $env:INTEGRATED_RECOMMENDER_ROOT) {
    Write-Error "Recommender root not found. Set INTEGRATED_RECOMMENDER_ROOT first."
    exit 1
}

$recsys = Join-Path $env:INTEGRATED_RECOMMENDER_ROOT "v146_release"
if (-not (Test-Path -LiteralPath $recsys)) {
    Write-Error "v146_release not found under INTEGRATED_RECOMMENDER_ROOT."
    exit 1
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    Write-Error "Python was not found in PATH."
    exit 1
}
$python = $pythonCommand.Source

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = (Join-Path $env:INTEGRATED_RECOMMENDER_ROOT ".codex_deps") + ";" + $recsys
$env:INTEGRATED_MODE = "auto"
if (-not $env:INTEGRATED_DEMO_USER) { $env:INTEGRATED_DEMO_USER = "15547" }
if (-not $env:INTEGRATED_RECSYS_URL) { $env:INTEGRATED_RECSYS_URL = "http://127.0.0.1:8010" }

function Stop-Port($port) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($connections) {
        $connections | ForEach-Object {
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 700
}

function Wait-Http($url, $attempts = 80) {
    for ($i = 0; $i -lt $attempts; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return $true
            }
        } catch {
            # Service is still starting.
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

Stop-Port 8010
Stop-Port 8020

$recsysArgs = @(
    "-m", "uvicorn", "app:app",
    "--app-dir", "webapp",
    "--host", "127.0.0.1",
    "--port", "8010"
)
$r1 = Start-Process -FilePath $python -ArgumentList $recsysArgs `
    -WorkingDirectory $recsys -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $out "_recsys.log") `
    -RedirectStandardError (Join-Path $out "_recsys.log.err")

if (-not (Wait-Http "http://127.0.0.1:8010/api/image/21181")) {
    Write-Error "Recommender service failed to start. Check outputs/_recsys.log.err."
    exit 1
}

$webArgs = @(
    "-B", "webui.py",
    "--real", "--voice",
    "--port", "8020",
    "--no-browser"
)
$r2 = Start-Process -FilePath $python -ArgumentList $webArgs `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $out "_web.log") `
    -RedirectStandardError (Join-Path $out "_web.log.err")

if (-not (Wait-Http "http://127.0.0.1:8020/api/info")) {
    Write-Error "Web UI failed to start. Check outputs/_web.log.err."
    exit 1
}

Write-Host ("recsys PID = " + $r1.Id)
Write-Host ("webui  PID = " + $r2.Id)
Write-Host "recommender = http://127.0.0.1:8010"
Write-Host "web UI      = http://127.0.0.1:8020"
