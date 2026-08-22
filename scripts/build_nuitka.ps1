# Build Nuitka binaire ultra-rapide (Windows + Linux)
# Prérequis: pip install nuitka ordered-set zstandard
# Usage: .\scripts\build_nuitka.ps1  -> dist/opencode.exe (Windows) ou dist/opencode.bin (Linux)
param(
    [string]$Python = "python",
    [switch]$Onefile
)
$ErrorActionPreference = "Stop"
Write-Host "Building opencode with Nuitka..."

$args = @(
    "--standalone"
    "--python-flag=no_site"
    "--python-flag=no_warnings"
    "--include-package=config"
    "--include-package=dashboard"
    "--include-package=vpn_manager"
    "--include-package=free_ip_pool"
    "--include-data-dir=static=static"
    "--include-data-file=config.yaml=config.yaml"
    "--enable-plugin=anti-bloat"
    "--lto=yes"
    "--jobs=4"
    "opencode.py"
)
if ($Onefile) { $args += "--onefile" }

& $Python -m nuitka @args
if ($LASTEXITCODE -ne 0) { throw "Nuitka build failed" }
Write-Host "Done: dist/opencode.exe"
Write-Host "Bench vs CPython: .\dist\opencode.exe --help"
