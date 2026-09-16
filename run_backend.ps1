# Guidy Backend launcher.
# Detects your machine's real LAN IPv4 address (skipping loopback/virtual
# adapters), prints exactly what to put in secrets.properties, and starts
# the server. Meant to be run via run_backend.bat (double-click), not
# directly -- see that file for why.

$ErrorActionPreference = "Stop"

# Always run from the folder this script lives in, regardless of where
# it was launched from.
Set-Location -Path $PSScriptRoot

Write-Host ""
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host "  Guidy Backend" -ForegroundColor Cyan
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host ""

# Find a real LAN IPv4 address -- filters out loopback (127.x), link-local
# (169.254.x, used when there's no real network), and common virtual
# adapter names (VPNs, Hyper-V, WSL) that would give a useless address to
# put in secrets.properties.
$ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -notlike "127.*" -and
        $_.IPAddress -notlike "169.254.*" -and
        $_.InterfaceAlias -notmatch "Loopback|vEthernet|Virtual|WSL|VPN"
    } |
    Select-Object -First 1 -ExpandProperty IPAddress

if (-not $ip) {
    Write-Host "Could not auto-detect a LAN IPv4 address." -ForegroundColor Yellow
    Write-Host "Run 'ipconfig' manually and look for your Wi-Fi/Ethernet adapter's IPv4 Address." -ForegroundColor Yellow
    $ip = "YOUR_IP_HERE"
} else {
    Write-Host "Detected LAN IP: $ip" -ForegroundColor Green
}

Write-Host ""
Write-Host "Your phone (on the same Wi-Fi) can reach this backend at:" -ForegroundColor White
Write-Host "  http://${ip}:8000/api/health" -ForegroundColor White
Write-Host ""
Write-Host "Make sure secrets.properties (in guidy-app-main) has:" -ForegroundColor White
Write-Host "  API_BASE_URL=http://${ip}:8000/api" -ForegroundColor White
Write-Host ""
Write-Host "First time on this machine? You may need to allow the port through" -ForegroundColor DarkGray
Write-Host "Windows Firewall (run as Administrator, once):" -ForegroundColor DarkGray
Write-Host '  New-NetFirewallRule -DisplayName "Guidy Backend" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow' -ForegroundColor DarkGray
Write-Host ""
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host "Starting server... (press Ctrl+C to stop)" -ForegroundColor Cyan
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host ""

python main.py

Write-Host ""
Write-Host "Server stopped." -ForegroundColor Yellow
Read-Host "Press Enter to close this window"
