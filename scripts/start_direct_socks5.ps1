# Start the direct-egress SOCKS5 bridge used as the second free station.
# Idempotent: exits 0 if the port is already listening.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$port = 11084

$existing = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "direct SOCKS5 bridge already listening on 127.0.0.1:$port"
    exit 0
}

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $pythonw = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pythonw) {
    Write-Error "pythonw.exe/python.exe introuvable dans le PATH"
    exit 1
}

Start-Process -FilePath $pythonw -ArgumentList "scripts\direct_socks5.py --port $port" -WorkingDirectory $root
Start-Sleep -Seconds 2

$listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Write-Error "le pont direct n'écoute pas sur 127.0.0.1:$port"
    exit 1
}

Write-Host "direct SOCKS5 bridge listening on 127.0.0.1:$port"
