# Démarre app_supervisor.py au logon utilisateur, sans droits admin (021 T002).
# Crée un .vbs caché dans le dossier Startup de l'utilisateur courant.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$startup = [Environment]::GetFolderPath("Startup")
$vbsPath = Join-Path $startup "OpenCodeProxyAppSupervisor.vbs"
$content = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$pythonw"" ""$Root\scripts\app_supervisor.py""", 0, False
"@
[IO.File]::WriteAllText($vbsPath, $content, [Text.Encoding]::ASCII)
Write-Output "Raccourci de démarrage créé: $vbsPath"
