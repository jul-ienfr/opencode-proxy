# Lanceur du superviseur d'app OpenCodeProxy (021 T002).
# Enveloppe mince autour de scripts/app_supervisor.py pour une tâche
# planifiée au logon. Aucune fenêtre : pythonw + WindowStyle Hidden.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$log = Join-Path $Root "logs\app-supervisor.log"
& $pythonw -u (Join-Path $PSScriptRoot "app_supervisor.py") *>> $log
exit $LASTEXITCODE
