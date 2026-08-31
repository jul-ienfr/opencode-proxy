# rotate_db.ps1 -- rotation DB/log pour opencode-proxy (port Windows de
# scripts/rotate_db.sh, sans dependance externe : sqlite3 via Python).
# [plan 30/08 Lot B1] Outil MANUEL (one-shot). La rotation hebdo automatique
# est IN-APP (portable, suit le projet) : opencode.py _db_maintenance_loop,
# dimanche 03:00 = checkpoint TRUNCATE + purge > database.weekly_purge_days
# (defaut 90) + VACUUM. Ce script sert a forcer un passage manuel si besoin.
# Usage : powershell -NoProfile -File scripts/rotate_db.ps1 [-DryRun] [-Vacuum]
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Vacuum,
    [int]$RetentionDays = 30,
    [int]$BakRetentionDays = 7
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$db = Join-Path $root 'logs\requests.db'

function Log([string]$msg) {
    Write-Output "[$((Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK'))] $msg"
}

if (-not (Test-Path $db)) {
    Log "DB not found at $db -- nothing to do"
    exit 0
}

Log ("DB size before: {0:N1} MB" -f ((Get-Item $db).Length / 1MB))

$pyCode = @"
import sqlite3, sys
db, retention, vacuum = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1"
conn = sqlite3.connect(db, timeout=30)
conn.execute("PRAGMA busy_timeout=30000")
try:
    for table in ("requests", "free_model_usage"):
        try:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE timestamp < "
                f"datetime('now', '-{retention} days')"
            )
            n = conn.total_changes
            print(f"[rotate] {table}: deleted old rows (total changes {n})")
        except sqlite3.Error as exc:
            print(f"[rotate] {table}: skip ({exc})")
    conn.commit()
    print(f"remaining requests: {conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0]}")
    if vacuum:
        print("VACUUM...")
        conn.execute("VACUUM")
        print("VACUUM done")
finally:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
"@

$pyFile = Join-Path $env:TEMP ("rotate_db_" + [guid]::NewGuid().ToString('N') + ".py")
Set-Content -Path $pyFile -Value $pyCode -Encoding ASCII

if ($DryRun) {
    Log "[dry-run] would DELETE rows older than $RetentionDays days$(if ($Vacuum) { ' + VACUUM' })"
    python $pyFile $db $RetentionDays 0
} else {
    python $pyFile $db $RetentionDays ($(if ($Vacuum) { 1 } else { 0 }))
}
Remove-Item $pyFile -Force -ErrorAction SilentlyContinue

Log ("DB size after: {0:N1} MB" -f ((Get-Item $db).Length / 1MB))

# Old .bak cleanup (> BakRetentionDays)
$cutoff = (Get-Date).AddDays(-$BakRetentionDays)
$deleted = 0
Get-ChildItem (Join-Path $root 'logs') -Filter 'requests.db.bak-*' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    ForEach-Object {
        if ($DryRun) { Log "[dry-run] would delete $($_.Name)" }
        else { Remove-Item $_.FullName -Force; $deleted++ }
    }
Log "Deleted $deleted old .bak files (> $BakRetentionDays days)"