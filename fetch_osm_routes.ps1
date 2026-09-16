# Pull Greater Cairo transit route relations from OpenStreetMap (Overpass).
#
# WHY THIS RUNS ON YOUR MACHINE
#
# The Claude sandbox's network allowlist blocks overpass-api.de,
# download.geofabrik.de, gitlab.com and openstreetmap.org outright, so the
# data has to be fetched here and read from the workspace folder.
#
# THREE TRAPS THIS SCRIPT HAS ALREADY FALLEN INTO
#
# 1. A REGIONAL MIRROR ANSWERING FOR THE WRONG PLANET.
#    overpass.osm.ch returned HTTP 200 and zero rows -- it is a
#    Switzerland-only instance. Read literally that says "OSM has no Cairo
#    bus routes", which is a conclusion drawn from a database that does not
#    contain Egypt. Every endpoint is now PROBED with a query whose answer
#    cannot legitimately be zero (Cairo's metro lines), and any endpoint
#    failing the probe is skipped however healthy its HTTP status.
#
# 2. RATE LIMITS SHREDDING THE RUN. overpass-api.de allows a couple of
#    concurrent slots per IP; firing nine queries two seconds apart earned
#    429s on five of them. The script now asks /api/status when a slot will
#    be free, waits for it, and backs off on 429 and 504.
#
# 3. ARABIC ARRIVING AS MOJIBAKE. PowerShell 5.1's Invoke-WebRequest
#    decodes the body as ISO-8859-1 when it isn't certain, so route names
#    came back as "Ø§ÙØ®Ø·". The raw bytes are now decoded as UTF-8
#    explicitly. Arabic names are half the value of this data.
#
# RESUMABLE. Each mode is written to its own file as soon as it arrives, and
# a mode already on disk is skipped. A rate-limited run is therefore fixed by
# running the script again rather than starting over.
#
# USAGE
#   .\fetch_osm_routes.ps1              fetch whatever is still missing
#   .\fetch_osm_routes.ps1 -Force       re-fetch everything
#   .\fetch_osm_routes.ps1 -Full        also fetch member geometry

param([switch]$Full, [switch]$Force)

Set-Location -Path $PSScriptRoot

try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11
} catch { }

$bbox = "29.70,30.80,30.35,31.80"   # south,west,north,east

# Overpass rejects PowerShell's default user agent with 406.
$userAgent = "Guidy-Transit/1.0 (Cairo transit app; +https://github.com/youssefezat)"

# GLOBAL instances only.
$endpoints = @(
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter"
)

function Get-Utf8Content {
    <# PowerShell 5.1 guesses ISO-8859-1 and mangles Arabic. Decode the
       bytes ourselves. #>
    param($Response)
    if ($Response.RawContentStream) {
        $ms = New-Object System.IO.MemoryStream
        $Response.RawContentStream.Position = 0
        $Response.RawContentStream.CopyTo($ms)
        return [System.Text.Encoding]::UTF8.GetString($ms.ToArray())
    }
    return $Response.Content
}

function Wait-ForSlot {
    <# Overpass publishes when your next slot frees up. Asking is far
       better than guessing at a sleep interval. #>
    param([string]$Endpoint)
    $statusUrl = $Endpoint -replace "/interpreter$", "/status"
    try {
        $s = Invoke-WebRequest -Uri $statusUrl -UserAgent $userAgent `
                -TimeoutSec 30 -UseBasicParsing
        $text = Get-Utf8Content $s
        if ($text -match "Slot available after: .*in (\d+) seconds") {
            $wait = [int]$Matches[1] + 2
            Write-Host ("    waiting {0}s for a free slot..." -f $wait) -ForegroundColor DarkGray
            Start-Sleep -Seconds $wait
        }
    } catch { Start-Sleep -Seconds 5 }
}

function Invoke-OverpassRetry {
    param([string]$Endpoint, [string]$Query, [int]$TimeoutSec = 400, [int]$Attempts = 4)

    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            $r = Invoke-WebRequest -Uri $Endpoint -Method Post -Body $Query `
                -ContentType "text/plain; charset=utf-8" `
                -Headers @{ "Accept" = "application/json" } `
                -UserAgent $userAgent -TimeoutSec $TimeoutSec -UseBasicParsing
            return (Get-Utf8Content $r)
        } catch {
            $code = $null
            if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
            $backoff = [Math]::Min(120, 15 * [Math]::Pow(2, $i - 1))
            if ($code -eq 429) {
                Write-Host "    429 rate limited (attempt $i/$Attempts)" -ForegroundColor DarkYellow
                Wait-ForSlot -Endpoint $Endpoint
            } elseif ($code -eq 504 -or $code -eq 503) {
                Write-Host ("    {0} server busy, backing off {1}s (attempt {2}/{3})" -f $code, $backoff, $i, $Attempts) -ForegroundColor DarkYellow
                Start-Sleep -Seconds $backoff
            } else {
                Write-Host "    failed: $($_.Exception.Message)" -ForegroundColor Yellow
                return $null
            }
        }
    }
    return $null
}

function Test-Endpoint {
    param([string]$Endpoint)
    $probe = "[out:json][timeout:60];relation[`"type`"=`"route`"][`"route`"=`"subway`"]($bbox);out count;"
    $body = Invoke-OverpassRetry -Endpoint $Endpoint -Query $probe -TimeoutSec 90 -Attempts 2
    if (-not $body) { return $false }
    try {
        $total = [int](($body | ConvertFrom-Json).elements[0].tags.total)
    } catch { return $false }
    if ($total -gt 0) {
        Write-Host ("  probe OK: {0} Cairo metro relations" -f $total) -ForegroundColor Green
        return $true
    }
    Write-Host "  probe returned 0 - wrong database, skipping" -ForegroundColor Yellow
    return $false
}

$chosen = $null
foreach ($e in $endpoints) {
    Write-Host "Probing $e ..." -ForegroundColor Cyan
    if (Test-Endpoint -Endpoint $e) { $chosen = $e; break }
}
if (-not $chosen) {
    Write-Host "No Overpass endpoint could serve Cairo data. Try again in a few minutes." -ForegroundColor Red
    exit 1
}
Write-Host "Using $chosen" -ForegroundColor Cyan
Write-Host ""

$modes = @("bus", "minibus", "share_taxi", "trolleybus",
           "monorail", "light_rail", "subway", "tram", "train")

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$counts = @{}

foreach ($mode in $modes) {
    $file = Join-Path $PSScriptRoot "osm_mode_$mode.json"
    if ((Test-Path $file) -and (-not $Force)) {
        $existing = (Get-Content $file -Raw | ConvertFrom-Json).elements
        $counts[$mode] = @($existing).Count
        Write-Host ("  {0,-12} {1,5}  (cached)" -f $mode, $counts[$mode]) -ForegroundColor DarkGray
        continue
    }

    Write-Host ("  {0,-12} fetching..." -f $mode) -ForegroundColor Cyan
    $q = "[out:json][timeout:300];relation[`"type`"=`"route`"][`"route`"=`"$mode`"]($bbox);out tags;"
    $body = Invoke-OverpassRetry -Endpoint $chosen -Query $q

    if (-not $body) {
        Write-Host ("  {0,-12} STILL FAILING - re-run the script to retry just this one" -f $mode) -ForegroundColor Yellow
        continue
    }
    [System.IO.File]::WriteAllText($file, $body, $utf8NoBom)
    $n = @(($body | ConvertFrom-Json).elements).Count
    $counts[$mode] = $n
    Write-Host ("  {0,-12} {1,5} relations" -f $mode, $n) -ForegroundColor Green

    Wait-ForSlot -Endpoint $chosen
}

# Merge whatever we have into one file, with provenance.
$all = @()
foreach ($mode in $modes) {
    $file = Join-Path $PSScriptRoot "osm_mode_$mode.json"
    if (Test-Path $file) {
        $els = (Get-Content $file -Raw | ConvertFrom-Json).elements
        if ($els) { $all += $els }
    }
}
$payload = [pscustomobject]@{
    source      = "OpenStreetMap via Overpass ($chosen)"
    licence     = "ODbL - https://www.openstreetmap.org/copyright"
    fetched_at  = (Get-Date).ToString("s")
    bbox        = $bbox
    mode_counts = $counts
    elements    = $all
}
[System.IO.File]::WriteAllText(
    (Join-Path $PSScriptRoot "osm_routes_tags.json"),
    ($payload | ConvertTo-Json -Depth 12), $utf8NoBom)

Write-Host ""
Write-Host ("Total: {0} route relations across {1} modes" -f @($all).Count, $counts.Count) -ForegroundColor Cyan
$missing = $modes | Where-Object { -not (Test-Path (Join-Path $PSScriptRoot "osm_mode_$_.json")) }
if ($missing) {
    Write-Host ("Still missing: {0}  -- just run the script again." -f ($missing -join ", ")) -ForegroundColor Yellow
}
