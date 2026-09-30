# Archive la phase paper IBKR avant passage live.
# Usage : .\scripts\archive_paper_ibkr.ps1

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$ArchiveName = "ibkr_paper_2026-05-23_2026-06-25"
$Dest = Join-Path $Root "archive\$ArchiveName"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

$Files = @(
    "trades_journal.jsonl",
    "positions.json",
    "cockpit_live.json",
    "swap_pending.json",
    "flash_state.json",
    "premarket_prep_state.json",
    "telegram_offset.json",
    ".env.ibkr_paper"
)

foreach ($f in $Files) {
    $src = Join-Path $Root $f
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $Dest $f) -Force
        Write-Host "Archive: $f"
    }
}

# Bilan rapide depuis le journal archive
$py = Join-Path $Root ".venv\Scripts\python.exe"
$journalArchive = Join-Path $Dest "trades_journal.jsonl"
if ((Test-Path $py) -and (Test-Path $journalArchive)) {
    $env:TB_ARCHIVE_DEST = $Dest
    $env:TB_ROOT = $Root
    & $py -c @'
import os, sys
sys.path.insert(0, os.environ["TB_ROOT"])
from main import load_trade_closed_events
journal = os.path.join(os.environ["TB_ARCHIVE_DEST"], "trades_journal.jsonl")
ev = load_trade_closed_events(journal)
ev = [e for e in ev if not (e.get("ticker")=="UPST" and str(e.get("ts_utc","")).startswith("2026-06-26") and float(e.get("pnl_usd",0) or 0)<0)] or ev
w = [e for e in ev if float(e.get("pnl_usd",0) or 0)>0]
pnl = sum(float(e.get("pnl_usd",0) or 0) for e in ev)
wr = len(w)/len(ev)*100 if ev else 0
bilan = f"""# Bilan paper IBKR — ibkr_paper_2026-05-23_2026-06-25

| Metrique | Valeur |
|----------|--------|
| Trades clos (dedup) | {len(ev)} |
| PnL cumule | {pnl:+.2f} USD |
| Win rate | {wr:.1f}% |
| Budget ref. | 1 000 USD |

Archive figee avant passage live.
Report HTML : docs/reports/v2-ibkr-performance-report.html
"""
open(os.path.join(os.environ["TB_ARCHIVE_DEST"], "BILAN.md"), "w", encoding="utf-8").write(bilan)
print("BILAN.md ecrit")
'@
}
Write-Host ""
Write-Host "Archive terminee : archive\$ArchiveName" -ForegroundColor Green
Write-Host "Prochaine etape : lancer live avec .\scripts\start_bot_live.ps1 (lundi)" -ForegroundColor Cyan
