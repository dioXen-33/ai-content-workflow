# =============================================================================
#  Lancement de l'outil derriere un tunnel Cloudflare.
#
#  Usage :  .\run_tunnel.ps1
#
#  Fait TOUT en une commande, tu ne touches jamais au .env a la main :
#    1. demarre un tunnel Cloudflare (URL HTTPS publique, sans port ni domaine)
#    2. recupere l'URL et l'ecrit dans PUBLIC_BASE_URL du .env (et l'affiche)
#    3. lance l'outil au premier plan -> les logs defilent a l'ecran
#
#  Ctrl+C arrete l'outil ET ferme le tunnel proprement.
# =============================================================================

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# --- Localiser cloudflared ---------------------------------------------------
$cloudflared =
    if (Test-Path "C:\tools\cloudflared.exe") { "C:\tools\cloudflared.exe" }
    elseif (Get-Command cloudflared -ErrorAction SilentlyContinue) { "cloudflared" }
    else { $null }

if (-not $cloudflared) {
    Write-Host "cloudflared introuvable dans C:\tools. Installe-le d'abord." -ForegroundColor Red
    exit 1
}

$envPath = Join-Path $root ".env"
if (-not (Test-Path $envPath)) {
    Write-Host ".env introuvable dans $root" -ForegroundColor Red
    exit 1
}

# --- 1. Demarrer le tunnel et capturer l'URL publique ------------------------
$stdout = Join-Path $env:TEMP "wfia-cf-out.txt"
$stderr = Join-Path $env:TEMP "wfia-cf-err.txt"
Remove-Item $stdout, $stderr -ErrorAction SilentlyContinue

Write-Host "[1/3] Demarrage du tunnel Cloudflare..." -ForegroundColor Cyan
$tunnel = Start-Process $cloudflared `
    -ArgumentList "tunnel", "--url", "http://127.0.0.1:8000" `
    -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr

$publicUrl = $null
foreach ($i in 1..60) {
    Start-Sleep -Seconds 1
    $text = ""
    if (Test-Path $stderr) { $text += (Get-Content $stderr -Raw -ErrorAction SilentlyContinue) }
    if (Test-Path $stdout) { $text += (Get-Content $stdout -Raw -ErrorAction SilentlyContinue) }
    $m = [regex]::Match($text, "https://[-a-z0-9]+\.trycloudflare\.com")
    if ($m.Success) { $publicUrl = $m.Value; break }
    if ($tunnel.HasExited) { break }
}

if (-not $publicUrl) {
    Write-Host "Impossible de recuperer l'URL du tunnel. Sortie de cloudflared :" -ForegroundColor Red
    if (Test-Path $stderr) { Get-Content $stderr -Tail 25 }
    if (-not $tunnel.HasExited) { $tunnel | Stop-Process -Force }
    exit 1
}

# --- 2. Injecter l'URL dans le .env, puis le relire pour preuve --------------
Write-Host "[2/3] Ecriture de l'URL dans le .env..." -ForegroundColor Cyan
$lines = Get-Content $envPath
if ($lines -match '^PUBLIC_BASE_URL=') {
    $lines = $lines -replace '^PUBLIC_BASE_URL=.*', "PUBLIC_BASE_URL=$publicUrl"
} else {
    $lines += "PUBLIC_BASE_URL=$publicUrl"
}
[System.IO.File]::WriteAllLines($envPath, $lines)

$check = (Get-Content $envPath | Select-String '^PUBLIC_BASE_URL=').Line
Write-Host ""
Write-Host "===============================================================" -ForegroundColor Green
Write-Host "  URL publique : $publicUrl" -ForegroundColor Green
Write-Host "  .env         : $check" -ForegroundColor Green
Write-Host "===============================================================" -ForegroundColor Green
Write-Host ""

# --- 3. Lancer l'outil au premier plan (logs visibles) -----------------------
Write-Host "[3/3] Lancement de l'outil (Ctrl+C pour arreter)..." -ForegroundColor Cyan
Write-Host ""
try {
    & (Join-Path $root ".venv\Scripts\python.exe") run.py
}
finally {
    Write-Host ""
    Write-Host "Arret du tunnel Cloudflare..." -ForegroundColor Yellow
    if ($tunnel -and -not $tunnel.HasExited) { $tunnel | Stop-Process -Force }
}
