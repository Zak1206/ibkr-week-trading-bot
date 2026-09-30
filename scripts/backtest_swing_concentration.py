# -*- coding: utf-8 -*-
"""
Le gain des variantes swing tient-il a quelques trades ?

Meme question que sur les donnees paper (ou 3 trades faisaient 87% du
resultat). Pour chaque variante: part des 3/5 meilleurs trades, et resultat
une fois ces trades retires.

Usage: .\\.venv\\Scripts\\python.exe scripts\\backtest_swing_concentration.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_swing import (  # noqa: E402
    BUDGET_USD, LIVE_RR_MULT, LIVE_STOP_PCT, Variant,
    build_signals, load_data, simulate,
)

VARIANTS = [
    Variant("CONTROLE live (-2/+3.5, flat, trail)", LIVE_STOP_PCT,
            LIVE_STOP_PCT * LIVE_RR_MULT, flat=True, trailing=True),
    Variant("Swing A  -6/+15  cap 10j", 6, 15, max_hold_days=10),
    Variant("Swing B -10/+25  cap 10j", 10, 25, max_hold_days=10),
    Variant("Swing B -10/+25  sans cap", 10, 25),
    Variant("Swing C -15/+50  cap 10j", 15, 50, max_hold_days=10),
]


def main():
    data = load_data()
    sig = build_signals(data)

    print("=" * 104)
    print("CONCENTRATION DU RESULTAT — le gain tient-il a quelques trades ?")
    print("=" * 104)
    print("%-38s %5s %10s %9s %9s %11s %11s"
          % ("variante", "n", "net $", "top3 %", "top5 %",
             "sans top3", "sans top5"))
    print("-" * 104)

    for v in VARIANTS:
        res = simulate(sig, data, v)
        nets = sorted((t["net"] for t in res["trades"]), reverse=True)
        if not nets:
            continue
        tot = sum(nets)
        top3 = sum(nets[:3])
        top5 = sum(nets[:5])
        p3 = 100.0 * top3 / tot if tot != 0 else 0.0
        p5 = 100.0 * top5 / tot if tot != 0 else 0.0
        print("%-38s %5d %+10.1f %8.0f%% %8.0f%% %+11.1f %+11.1f"
              % (v.name, len(nets), tot, p3, p5, tot - top3, tot - top5))

    print("\nLecture: si 'sans top5' est proche de zero ou negatif, la variante")
    print("ne gagne pas grace a sa regle de sortie mais grace a quelques coups.")
    print("Rappel des donnees paper reelles: 3 trades = 87%% du resultat, et")
    print("sans les 5 meilleurs -> -15.52$ sur 63 trades.")


if __name__ == "__main__":
    main()
