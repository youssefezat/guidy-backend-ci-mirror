# Restarts the Guidy backend -- the NATIVE WINDOWS python process on :8000,
# which is the one the phone app actually talks to, NOT the docker stack --
# then captures a test route once it is up.
#
# Run from an ordinary PowerShell window:
#   powershell -ExecutionPolicy Bypass -File restart_and_test.ps1

$ErrorActionPreference = "Continue"
Set-Location "C:\claude workspace\guidy-backend-main"

Write-Host "=== Stopping whatever is listening on :8000 ===" -ForegroundColor Cyan
$conns = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    foreach ($procId in ($conns.OwningProcess | Select-Object -Unique)) {
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        $pname = if ($p) { $p.ProcessName } else { "unknown" }
        Write-Host ("  killing PID {0} ({1})" -f $procId, $pname)
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
} else {
    Write-Host "  nothing was listening on :8000"
}

Write-Host "=== Starting python main.py in a new window (logging to server.log) ===" -ForegroundColor Cyan
# Tee-Object keeps the live console window readable AND writes everything to
# server.log. The log matters: uvicorn records every request's full URL,
# including its query string, so a trip taken in the app leaves behind the
# exact coordinates it asked for. Without a file, that only ever existed in
# console scrollback and had to be copied out by hand.
Start-Process -FilePath "powershell" -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location 'C:\claude workspace\guidy-backend-main'; python main.py 2>&1 | Tee-Object -FilePath server.log"
)

Write-Host "=== Waiting for the engine to finish loading (up to 3 min) ===" -ForegroundColor Cyan
$ready = $false
$waited = 0
for ($i = 1; $i -le 180; $i++) {
    Start-Sleep -Seconds 1
    $waited = $i
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8000/api/stations" -TimeoutSec 3 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    if ($i % 15 -eq 0) { Write-Host ("  still loading... {0}s" -f $i) }
}
if (-not $ready) {
    Write-Host "Server did not come up. Check the new python window for an error." -ForegroundColor Red
    exit 1
}
Write-Host ("  ready after ~{0}s" -f $waited) -ForegroundColor Green

Write-Host "=== Fetching the Nasr City -> Helwan BIS (Zamalek) test route ===" -ForegroundColor Cyan
$sw = [System.Diagnostics.Stopwatch]::StartNew()
curl.exe -s "http://localhost:8000/api/route?start_lat=30.0663104&start_lon=31.3806677&end_lat=30.0717052&end_lon=31.220952&lang=en" -o live_route.json
$sw.Stop()
Write-Host ("  wall-clock request time: {0:N0} ms" -f $sw.Elapsed.TotalMilliseconds) -ForegroundColor Green
Write-Host "  wrote live_route.json"

# --- Regression check for the 9,285 m walk (2026-08-23) -------------------
# Koshary Tawagen Salsa -> AASTMT Sheraton. Our OSRM genuinely returns a
# 9,285 m foot route here (straight line 3,367 m, Google 4.1 km) because no
# pedestrian crossing of Tareeq El-Nasr is mapped nearby. The engine must now
# refuse to offer that walk at all. If a Walk option comes back, or comes back
# longer than WALK_ONLY_MAX_REAL_M, the guard did not load.
Write-Host "=== Regression: AASTMT walk must NOT be offered ===" -ForegroundColor Cyan
curl.exe -s "http://localhost:8000/api/route?start_lat=30.0663125&start_lon=31.3806875&end_lat=30.0959491&end_lon=31.3734994&lang=en" -o aastmt_route.json
try {
    $aastmt = Get-Content aastmt_route.json -Raw | ConvertFrom-Json
    $walks = @($aastmt.options | Where-Object { $_.type -eq 'Walk' })
    $kinds = ($aastmt.options | ForEach-Object { $_.type }) -join ', '
    Write-Host ("  options returned: {0}" -f $kinds)
    if ($walks.Count -eq 0) {
        Write-Host "  PASS - no walk-only option offered" -ForegroundColor Green
    } else {
        foreach ($w in $walks) {
            Write-Host ("  FAIL - walk option offered at {0} m (guard did not load)" -f $w.distance_m) -ForegroundColor Red
        }
    }
} catch {
    Write-Host "  could not parse aastmt_route.json -- Claude will read it" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done. Tell Claude it finished; it will read live_route.json and aastmt_route.json." -ForegroundColor Yellow
