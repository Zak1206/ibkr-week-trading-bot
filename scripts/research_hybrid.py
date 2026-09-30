# -*- coding: utf-8 -*-
"""
Hybride day trade / week trade contre la strategie semaine seule (8 tickers, 1000$).

  SEMAINE   breakout 1h, SL -10 / TP +25 / 5 seances (config actuelle)
  H1        idem, mais position coupee a la cloture du 1er jour si elle est en perte
  H1b       idem, coupee seulement si la perte depasse -3% a la 1re cloture
  H2        semaine prioritaire; capital inactif -> day trade ORB 60 min (stop au plus
            bas de la 1re heure, sortie a la cloture) avec la meme ligne de 1000$
  H3        deux moteurs separes: 500$ semaine + 500$ day trade ORB
"jours %" = part des trades ouverts et fermes la meme seance (regle PDT).

Usage: .\.venv\Scripts\python.exe scripts\research_hybrid.py
"""
import os, pickle, sys
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_swing as B, research_data as RD, research_engine as E, research_strategies as S, research_tickers as T

cache = pickle.load(open(T.CACHE, "rb")); rd = RD.load()
U = [t for t in T.CURRENT if t != "CVS"]
old = B.TICKERS; B.TICKERS, B.VIX_THRESHOLD = U, 22.0
sig = B.build_signals(T.make_data(cache, rd, U)); B.TICKERS, B.VIX_THRESHOLD = old, 22.0
week_s = sig[sig.breakout].reset_index(drop=True); week_s["kind"], week_s["prio"] = "open", 1.0
WEEK = E.Exit("week", sl_pct=10, tp_pct=25, max_hold_tdays=5)
ORBX = E.Exit("orb", use_signal_stop=True, eod=True)
orb = S.orb_signals({t: cache["h1"][t] for t in U}, U); orb["prio"] = 0.0; orb["exit_obj"] = [ORBX] * len(orb)
bars = E.Bars({t: cache["h1"][t] for t in U})
FULL = (T.TRAIN[0], T.TEST[1])

def run(sg, ex, win, reps=12, **kw):
    nets, dt, ns = [], [], []
    for k in range(reps):
        r = E.simulate(bars, sg, ex, seed=800 + k, skip_first_days=(k % 4) * 5, win=win, **kw)
        nets.append(sum(t["net"] for t in r["trades"])); ns.append(len(r["trades"]))
        dt.append(100.0 * sum(1 for t in r["trades"] if t["tdays"] == 0) / max(1, len(r["trades"])))
    return np.array(nets), float(np.median(ns)), float(np.median(dt))

cases = {
    "SEMAINE (actuel)": (week_s, WEEK, dict(line_usd=1000, max_open=1)),
    "H1 coupe perte 1er soir": (week_s, E.Exit("h1", sl_pct=10, tp_pct=25, max_hold_tdays=5, cut_first_close_pct=0.0), dict(line_usd=1000, max_open=1)),
    "H1b coupe si < -3% 1er soir": (week_s, E.Exit("h1b", sl_pct=10, tp_pct=25, max_hold_tdays=5, cut_first_close_pct=-3.0), dict(line_usd=1000, max_open=1)),
    "H2 semaine + ORB si inactif": (pd.concat([week_s, orb], ignore_index=True).sort_values("ts").reset_index(drop=True), WEEK, dict(line_usd=1000, max_open=1)),
}
print("=" * 104)
print("HYBRIDE DAY / WEEK TRADE — 8 tickers, 1000$, frais IBKR, 12 perturbations")
print("=" * 104)
print("%-30s %9s %9s %9s %8s | %7s %7s" % ("", "TRAIN $", "TEST $", "p10 TEST", "$/mois", "trades", "jours%"))
for lab, (sg, ex, kw) in cases.items():
    a, _, _ = run(sg, ex, T.TRAIN, **kw); b, _, _ = run(sg, ex, T.TEST, **kw); f, n, d = run(sg, ex, FULL, **kw)
    print("%-30s %+9.0f %+9.0f %+9.0f %+8.1f | %7.0f %6.0f%%" % (lab, np.median(a), np.median(b), np.percentile(b, 10), np.median(f) / 35, n, d))
# H3: deux moteurs de 500$ chacun (budget 500 chacun), resultats additionnes
res = {}
for lab, sg, ex in (("week", week_s, WEEK), ("orb", orb, ORBX)):
    res[lab] = [run(sg, ex, w, line_usd=500, max_open=1, budget=500) for w in (T.TRAIN, T.TEST, FULL)]
tot = [res["week"][i][0] + res["orb"][i][0] for i in range(3)]
print("%-30s %+9.0f %+9.0f %+9.0f %+8.1f | %7.0f %6.0f%%" % ("H3 500$ semaine + 500$ ORB", np.median(tot[0]), np.median(tot[1]), np.percentile(tot[1], 10), np.median(tot[2]) / 35, res["week"][2][1] + res["orb"][2][1], 100 * res["orb"][2][1] / (res["week"][2][1] + res["orb"][2][1])))
print("  dont moteur ORB seul (500$): TRAIN %+.0f  TEST %+.0f" % (np.median(res["orb"][0][0]), np.median(res["orb"][1][0])))
