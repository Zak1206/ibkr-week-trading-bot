# Demarre le bot (IBKR paper). Gateway doit etre connecte.
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
& "$Root\.venv\Scripts\python.exe" main.py --env-file ".env.ibkr_paper" --no-portfolio-prompt
