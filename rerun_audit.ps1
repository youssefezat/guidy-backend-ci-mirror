# Restarts the Guidy backend on the latest code and re-runs the full
# 70-pair Guidy-vs-Google route comparison audit against it.
#
# Requires your Google Routes API key in the CURRENT PowerShell session:
#   $env:GOOGLE_ROUTES_API_KEY = "your key here"
#   powershell -ExecutionPolicy Bypass -File "C:\claude workspace\guidy-backend-main\rerun_audit.ps1"
#
# (Run it from a normal PowerShell prompt, not a fresh -File invocation,
#  or set the key inside this file's session -- env vars do not carry
#  across separate powershell.exe launches.)

$ErrorActionPreference = "Continue"
Set-Location "C:\claude workspace\guidy-backend-main"

if (-not $env:GOOGLE_ROUTES_API_KEY) {
    Write-Host "GOOGLE_ROUTES_API_KEY is not set in this session." -ForegroundColor Red
    Write-Host 'Set it first:  $env:GOOGLE_ROUTES_API_KEY = "your key here"' -ForegroundColor Yellow
    exit 1
}

Write-Host "=== Checking OSRM (docker, port 5000) ===" -ForegroundColor Cyan
try {
    $osrm = Invoke-WebRequest -Uri "http://localhost:5000/route/v1/foot/31.3806677,30.0663104;31.220952,30.0717052?overview=false" -TimeoutSec 5 -UseBasicParsing
    if ($osrm.Content -match '"code"\s*:\s*"Ok"') {
        Write-Host "  OSRM is up" -ForegroundColor Green
    } else {
        Write-Host "  OSRM answered but not with code:Ok -- results will be degraded" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  OSRM is NOT reachable on localhost:5000." -ForegroundColor Red
    Write-Host "  Start Docker Desktop, then:  docker start guidy-osrm" -ForegroundColor Yellow
    Write-Host "  Aborting -- an audit without OSRM would measure the wrong thing." -ForegroundColor Red
    exit 1
}

Write-Host "=== Restarting the backend on current code ===" -ForegroundColor Cyan
$conns = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    foreach ($procId in ($conns.OwningProcess | Select-Object -Unique)) {
        Write-Host ("  killing PID {0}" -f $procId)
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}
Start-Process -FilePath "python" -ArgumentList "main.py" -WorkingDirectory "C:\claude workspace\guidy-backend-main"

Write-Host "=== Waiting for the engine to load ===" -ForegroundColor Cyan
$ready = $false
for ($i = 1; $i -le 180; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8000/api/stations" -TimeoutSec 3 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    if ($i % 15 -eq 0) { Write-Host ("  still loading... {0}s" -f $i) }
}
if (-not $ready) {
    Write-Host "Server did not come up. Check the python window." -ForegroundColor Red
    exit 1
}
Write-Host ("  ready after ~{0}s" -f $i) -ForegroundColor Green

Write-Host "=== Archiving the previous audit cache ===" -ForegroundColor Cyan
if (Test-Path "route_comparison_cache.json") {
    Copy-Item "route_comparison_cache.json" "route_comparison_cache.previous.json" -Force
    Remove-Item "route_comparison_cache.json" -Force
    Write-Host "  old results kept as route_comparison_cache.previous.json (so the two runs can be diffed)"
} else {
    Write-Host "  no existing cache -- starting fresh"
}

Write-Host "=== Running the 70-pair audit (this takes several minutes) ===" -ForegroundColor Cyan
python route_comparison_audit.py

Write-Host ""
Write-Host "Done. Tell Claude it finished and it will read route_comparison_cache.json." -ForegroundColor Yellow
