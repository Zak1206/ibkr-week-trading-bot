#!/usr/bin/env python3
"""Check-up rapide: positions, prix live vs entree, config critique."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
extra = os.getenv("ENV_FILE", "").strip() or os.getenv("ENV_FILE_ENVVAR", "")
if os.getenv("ENV_FILE_ENVVAR"):
    load_dotenv(ROOT / os.getenv("ENV_FILE_ENVVAR", ""), override=True)


def main() -> None:
    from main import (
        get_reference_price_usd,
        load_positions,
        normalize_position_state,
        price_suspect_vs_entry,
    )

    pos_path = os.getenv("POSITION_FILE", "positions.json")
    positions = load_positions(str(ROOT / pos_path))
    open_pos = [
        (tk, normalize_position_state(st))
        for tk, st in positions.items()
        if normalize_position_state(st).get("in_position")
    ]
    print(f"=== Check-up bot ({len(open_pos)} positions ouvertes) ===\n")
    issues = 0
    for tk, st in open_pos:
        entry = float(st.get("entry_price_usd", 0) or 0)
        live = get_reference_price_usd(tk)
        tp = st.get("take_profit")
        print(f"{tk}: entry={entry:.2f} | live={live} | TP={tp} | conf={st.get('entry_confidence')}")
        if live and entry > 0:
            pnl = (live - entry) / entry * 100
            print(f"    P/L latent ~{pnl:+.2f}%")
            if price_suspect_vs_entry(live, entry):
                print("    [!] Prix live suspect vs entree (verifier Gateway/Yahoo)")
                issues += 1
        elif entry > 0:
            print("    [!] Prix live indisponible")
            issues += 1
    print(f"\nSCAN_PARALLEL_ENABLED={os.getenv('SCAN_PARALLEL_ENABLED', '?')}")
    print(f"SCAN_INTERVAL_MIN={os.getenv('SCAN_INTERVAL_MIN', '?')}")
    print(f"REFERENCE_PRICE_IBKR_FIRST={os.getenv('REFERENCE_PRICE_IBKR_FIRST', '?')}")
    print(f"\nProblemes detectes: {issues}")
    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
