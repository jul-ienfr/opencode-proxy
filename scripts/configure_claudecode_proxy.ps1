<#
.SYNOPSIS
  Configure Claude Code sur CETTE machine pour consommer OpenCodeProxy via Tailscale.
  À lancer sur la machine cliente (desktop-tk459fh), en utilisateur normal — pas admin.
  Indépendant de l'installation OpenSSH/DISM en cours.

.PARAMETER ProxyBase
  URL du proxy (IP Tailscale de la machine serveur + port).
.PARAMETER Model
  Modèle par défaut. S'il n'est pas servi, bascule automatique sur le premier modèle free.
#>
param(
  [string]$ProxyBase = 'http://100.90.85.128:4000',
  [string]$Model     = 'deepseek-v4.1-flash-free'
)
$ErrorActionPreference = 'Stop'
# PowerShell 5.1 négocie TLS 1.0/1.1 par défaut ; claude.ai et github.com
# exigent TLS 1.2 → sans cette ligne, tout téléchargement HTTPS est coupé.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# Le proxy est en client_auth.mode=none : la clé est ignorée, mais Claude Code exige une valeur non vide.
$ApiKey = 'opencode-proxy'

function Step([string]$n, [string]$t) { Write-Host "`n── $n/4 $t ──" -ForegroundColor Cyan }

# ── 1. Connectivité Tailscale → proxy ────────────────────────────────────────
Step '1' 'Connectivité vers le proxy'
try {
  $h = Invoke-RestMethod -Uri "$ProxyBase/health" -TimeoutSec 10
  Write-Host "health: $($h.status) ($($h.status_reasons -join '; '))"
} catch {
  Write-Host "ERREUR : proxy injoignable sur $ProxyBase — Tailscale est-il connecté sur cette machine ?" -ForegroundColor Red
  exit 1
}
$models = Invoke-RestMethod -Uri "$ProxyBase/v1/models" -TimeoutSec 15
$list = if ($models.data) { $models.data } else { $models.models }
$ids = $list | ForEach-Object { $_.id }
Write-Host "$($ids.Count) modèles servis par le proxy."
if ($ids -notcontains $Model) {
  $free = $list | Where-Object { $_.tier -eq 'free' } | ForEach-Object { $_.id }
  if ($free) {
    $Model = $free[0]
    Write-Host "Modèle demandé absent — bascule sur $Model" -ForegroundColor Yellow
  } else {
    Write-Host "Aucun modèle free listé ; je garde $Model." -ForegroundColor Yellow
  }
}
Write-Host "Modèle retenu : $Model"

# ── 2. Claude Code ───────────────────────────────────────────────────────────
Step '2' 'Claude Code'
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
  Write-Host "Déjà installé : $(& claude --version 2>$null)"
} else {
  Write-Host "Installation (installeur natif)…"
  try {
    Invoke-RestMethod -Uri 'https://claude.ai/install.ps1' | Invoke-Expression
  } catch {
    Write-Host "Installeur natif KO — repli sur npm." -ForegroundColor Yellow
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
      Write-Host "npm absent également : installez Node.js LTS puis relancez ce script." -ForegroundColor Red
      exit 1
    }
    npm install -g @anthropic-ai/claude-code
  }
}

# ── 3. Variables d'environnement persistantes (scope utilisateur) ────────────
Step '3' "Variables d'environnement (persistantes, scope utilisateur)"
$envVars = [ordered]@{
  ANTHROPIC_BASE_URL             = $ProxyBase
  ANTHROPIC_API_KEY              = $ApiKey
  ANTHROPIC_MODEL                = $Model
  ANTHROPIC_DEFAULT_SONNET_MODEL = $Model
  ANTHROPIC_DEFAULT_HAIKU_MODEL  = $Model
  ANTHROPIC_DEFAULT_OPUS_MODEL   = $Model
}
foreach ($kv in $envVars.GetEnumerator()) {
  setx $kv.Key $kv.Value | Out-Null
  Write-Host ("  {0} = {1}" -f $kv.Key, $kv.Value)
}

# ── 4. Validation par un appel réel (format Anthropic Messages) ──────────────
Step '4' 'Appel réel de validation'
$body = @{
  model      = $Model
  max_tokens = 64
  messages   = @(@{ role = 'user'; content = 'Réponds exactement : PROXY OK' })
} | ConvertTo-Json -Depth 5
try {
  $resp = Invoke-RestMethod -Uri "$ProxyBase/v1/messages" -Method Post -TimeoutSec 120 `
    -Headers @{ 'x-api-key' = $ApiKey; 'anthropic-version' = '2023-06-01' } `
    -ContentType 'application/json' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
  Write-Host "Réponse du modèle : $($resp.content[0].text)" -ForegroundColor Green
} catch {
  Write-Host "L'appel de validation a échoué : $_" -ForegroundColor Red
  Write-Host "(Les variables sont posées quand même — réessayez après quelques secondes.)"
}

Write-Host "`nTerminé. Ouvrez un NOUVEAU terminal (setx ne touche pas la session courante) puis lancez : claude" -ForegroundColor Green
