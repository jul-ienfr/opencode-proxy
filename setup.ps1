# OpenCode Proxy - Installation automatique
# Installe Python deps + OpenVPN automatiquement

$ErrorActionPreference = "Continue"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  OpenCode Proxy - Installation auto" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# --- Python ---
Write-Host "[1/4] Verification Python..." -ForegroundColor Yellow
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[ERREUR] Python non installe" -ForegroundColor Red
    Write-Host "Telechargez: https://www.python.org/downloads/"
    exit 1
}
Write-Host "[OK] Python detecte" -ForegroundColor Green

# --- Dependencies Python ---
Write-Host "[2/4] Installation dependances Python..." -ForegroundColor Yellow
pip install -r requirements.txt --quiet 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERREUR] Echec pip install" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] Dependances Python installees" -ForegroundColor Green

# --- OpenVPN ---
Write-Host "[3/4] Verification OpenVPN..." -ForegroundColor Yellow
$openvpn = Get-Command openvpn -ErrorAction SilentlyContinue
if (-not $openvpn) {
    # Chercher dans les emplacements courants
    $paths = @(
        "C:\Program Files\OpenVPN\bin\openvpn.exe",
        "C:\Program Files (x86)\OpenVPN\bin\openvpn.exe"
    )
    $found = $false
    foreach ($p in $paths) {
        if (Test-Path $p) {
            $openvpn = $p
            $found = $true
            break
        }
    }
    if (-not $found) {
        Write-Host "OpenVPN non installe. Installation via winget..." -ForegroundColor Yellow
        try {
            winget install OpenVPNTechnologies.OpenVPN --accept-package-agreements --accept-source-agreements --silent 2>&1 | Out-Null
            # Rechercher apres installation
            foreach ($p in $paths) {
                if (Test-Path $p) {
                    $openvpn = $p
                    $found = $true
                    break
                }
            }
            if ($found) {
                Write-Host "[OK] OpenVPN installe" -ForegroundColor Green
            } else {
                Write-Host "[ERREUR] OpenVPN installe mais introuvable" -ForegroundColor Red
                Write-Host "Redemarrez PowerShell et relancez setup.ps1" -ForegroundColor Yellow
            }
        } catch {
            Write-Host "[ERREUR] Impossible d'installer OpenVPN: $_" -ForegroundColor Red
            Write-Host "Installez manuellement: winget install OpenVPNTechnologies.OpenVPN" -ForegroundColor Yellow
        }
    } else {
        Write-Host "[OK] OpenVPN detecte" -ForegroundColor Green
    }
} else {
    Write-Host "[OK] OpenVPN detecte" -ForegroundColor Green
}

# --- Config ---
Write-Host "[4/4] Preparation configuration..." -ForegroundColor Yellow
if (-not (Test-Path .env)) {
    if (Test-Path .env.example) {
        Copy-Item .env.example .env
        Write-Host "[OK] Fichier .env cree" -ForegroundColor Green
    }
}
if (-not (Test-Path logs)) { New-Item -ItemType Directory logs | Out-Null }
if (-not (Test-Path vpn_configs)) { New-Item -ItemType Directory vpn_configs | Out-Null }
Write-Host "[OK] Configuration prete" -ForegroundColor Green

# --- Lancement ---
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Lancement du proxy..." -ForegroundColor Cyan
Write-Host "  Dashboard: http://localhost:8082" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
python opencode.py
