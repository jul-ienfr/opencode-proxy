# Installe le superviseur d'app au logon Windows (021 T002).
# Usage : powershell -ExecutionPolicy Bypass -File scripts\install_app_service.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $Root "scripts\run_app_supervisor.ps1"
if (-not (Test-Path $runner)) { throw "runner introuvable: $runner" }

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`"" `
    -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

Register-ScheduledTask `
    -TaskName "OpenCodeProxyAppSupervisor" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Maintient opencode.py (proxy free) en vie via app_supervisor.py" `
    -Force | Out-Null

Write-Output "Tâche planifiée 'OpenCodeProxyAppSupervisor' installée (au logon)."
