# Demarre le bot en mode LIVE IBKR.
# Pre-requis : IB Gateway en mode LIVE, port API 4001, connecte.
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "=== MODE LIVE IBKR ===" -ForegroundColor Red
Write-Host "Gateway doit etre en LIVE (port 4001), pas paper (4002)." -ForegroundColor Yellow
$confirm = Read-Host "Confirmer lancement LIVE ? (oui/non)"
if ($confirm -ne "oui") {
    Write-Host "Annule."
    exit 1
}

& "$Root\.venv\Scripts\python.exe" main.py --env-file ".env.ibkr_live" --no-portfolio-prompt
