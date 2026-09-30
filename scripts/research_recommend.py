# -*- coding: utf-8 -*-
"""
Changements structurels candidats, contre la config REELLEMENT en production
(.env: VIX 26), sur la fenetre commune et ses deux moities.

Chaque changement a une justification a priori, pas un balayage:
  - breakout seul : le chemin "structure" (82% des signaux) a un excess nul a
    tous les horizons (research_signal_edge.py); le flag existe deja
    (BUY_SNIPER_BREAKOUT_REQUIRED=1)
  - VIX 22        : valeur par defaut de main.py; le harnais de septembre
    supposait 22 alors que le .env est a 26
  - 1 ligne 1000$ : le plancher de 0.35$/ordre coute 13.5 bps aller-retour a
    520$, 7 bps a 1000$ (arithmetique, pas un reglage)
  - sortie semaine: SL-10/TP+25, 5 seances max (plateau etabli en septembre)

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_recommend.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_swing as B  # noqa: E402
import research_engine as E  # noqa: E402
import research_strategies as S  # noqa: E402

REPS = 20


def main():
    data = B.load_data()
    bars = E.Bars(data["h1"])
    s26 = S.bot_signals(data, 26.0)
    s22 = S.bot_signals(data, 22.0)
    b26 = s26[s26["breakout"]].reset_index(drop=True)
    b22 = s22[s22["breakout"]].reset_index(drop=True)
    live = E.Exit("live", sl_pct=2, tp_pct=3.5, trailing=True, flat_min_profit=1.2)
    week = E.Exit("week", sl_pct=10, tp_pct=25, max_hold_tdays=5)
    one = {"line_usd": 1000.0, "max_open": 1}
    rows = [
        ("0  PRODUCTION (VIX 26, 2x520)", s26, live, {}),
        ("1  VIX 22", s22, live, {}),
        ("2  breakout seul (VIX 26)", b26, live, {}),
        ("3  breakout seul + VIX 22", b22, live, {}),
        ("4  breakout + VIX 22, 1 ligne 1000$", b22, live, one),
        ("5  config 0 en 1 ligne 1000$", s26, live, one),
        ("6  breakout + VIX 22, sortie semaine", b22, week, {}),
    ]
    W = S.WIN
    mid = W[0] + (W[1] - W[0]) / 2
    print("=" * 128)
    print("CHANGEMENTS STRUCTURELS — %s -> %s (moitie: %s), mediane de %d perturbations, frais IBKR + 3 bps"
          % (W[0].date(), W[1].date(), mid.date(), REPS))
    print("=" * 128)
    print("%-38s %8s %15s %4s %5s %6s %6s %6s | %8s %8s %4s | %5s"
          % ("variante", "total $", "p10-p90", ">0", "n", "bps", "DD%", "Shrp", "1re moi", "18 mois", ">0", "%jour"))
    print("-" * 128)
    for lab, sg, ex, kw in rows:
        full = E.robust(bars, sg, ex, reps=REPS, win=W, **kw)
        h1 = E.robust(bars, sg, ex, reps=REPS // 2, win=(W[0], mid), **kw)
        h2 = E.robust(bars, sg, ex, reps=REPS, win=(mid, W[1]), **kw)
        tr = full["res"]["trades"]
        same_day = 100.0 * sum(1 for t in tr if t["tdays"] == 0) / max(1, len(tr))
        m = full["m"]
        print("%-38s %+8.0f [%+6.0f,%+6.0f] %3.0f%% %5d %+6.1f %+6.1f %6.2f | %+8.0f %+8.0f %3.0f%% | %4.0f%%"
              % (lab, full["med"], full["p10"], full["p90"], full["pos"], m["n"], m["bps"],
                 m["dd_pct"], m["sharpe"], h1["med"], h2["med"], h2["pos"], same_day), flush=True)
    print("\n%jour = part des trades ouverts et fermes la meme seance (= day trades au sens PDT).")


if __name__ == "__main__":
    main()
