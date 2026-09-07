#Requires -Version 5.1
# [plan-perf-fiabilite-6stations Lot 0] Gate d'intégration LOCALE unique.
# Décision 2026-09-06 : aucune CI distante (pas de workflow GitHub Actions).
# Exécute dans l'ordre, arrêt au premier échec :
#   1. ruff check
#   2. mypy
#   3. pytest -k "not docker" --cov=. --cov-fail-under=45   (palier Lot 3,
#      mesuré 49.9 — voir plan-perf-fiabilite-6stations)
#   4. python scripts/bench_perf.py --json --fail-threshold 20
#   5. pip-audit -r requirements.txt   (scopé aux dépendances du PROJET :
#      `pip-audit` nu audite tout l'interpréteur système — 266 vulns
#      pré-existantes hors périmètre : torch, litellm, crawl4ai… que le
#      proxy n'importe jamais. Voir docs/tuning.md § gate.)
#   6. gitleaks detect   (SKIP avec avertissement si le binaire est absent)
#   7. docker compose config   (validation syntaxique, ne requiert pas le daemon)
#
# Usage :  powershell -ExecutionPolicy Bypass -File scripts/gate.ps1
#          (depuis la racine du dépôt ; Windows PowerShell 5.1 suffit, cf. #Requires)
# Exit 0 si tout passe, 1 sinon. La sortie de chaque étape est affichée telle quelle.

$ErrorActionPreference = "Continue"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)  # racine du repo

$script:failed = $false

function Step([string]$name, [scriptblock]$cmd) {
    Write-Output ""
    Write-Output "==== [$name] ===="
    & $cmd | ForEach-Object { $_.ToString() }
    $code = $LASTEXITCODE
    if (-not $?) { $code = 1 }
    if ($code -ne 0) {
        Write-Output "    -> $name FAILED (exit $code)"
        $script:failed = $true
    } else {
        Write-Output "    -> $name OK"
    }
}

Step "ruff"   { ruff check . }
if ($script:failed) { exit 1 }
Step "mypy"   { mypy . }
if ($script:failed) { exit 1 }
Step "pytest" { python -m pytest -k "not docker" --cov=. --cov-fail-under=45 }
if ($script:failed) { exit 1 }
Step "bench"  { python scripts/bench_perf.py --json --fail-threshold 20 }
if ($script:failed) { exit 1 }
Step "pip-audit" { pip-audit -r requirements.txt }
if ($script:failed) { exit 1 }

# gitleaks est optionnel : absent = skip avec avertissement explicite.
$gitleaks = Get-Command gitleaks -ErrorAction SilentlyContinue
if ($gitleaks) {
    Step "gitleaks" { gitleaks detect --redact }
    if ($script:failed) { exit 1 }
} else {
    Write-Output "==== [gitleaks] SKIP (binaire absent - installer https://github.com/gitleaks/gitleaks pour l'activer)"
}

Step "compose" { docker compose config --quiet }
if ($script:failed) { exit 1 }

Write-Output ""
Write-Output "GATE OK"
exit 0
