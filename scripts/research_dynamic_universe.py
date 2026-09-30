# -*- coding: utf-8 -*-
"""
Rotation automatique de l'univers: vaut-elle mieux qu'un univers fixe ?

Chaque 1er du mois, l'univers de 8 tickers est choisi avec les SEULES donnees
anterieures (aucune anticipation), dans un pool d'environ 160 titres (volatils du
S&P 1500 + lignes des ETF thematiques + univers du bot). Strategie identique
partout: breakout semaine, 1 x 1000$, SL -10 / TP +25 / 5 seances.

  FIXE-8     univers actuel du bot, jamais change (choisi en aout 2026: biais retrospectif)
  POOL       tout le pool, aucune selection (reference "sans talent de selection")
  R1 perf    les 8 ou la strategie a le plus gagne sur 3 mois (carnet fantome)
  R2 coupe   FIXE-8, un ticker suspendu 1 mois apres 2 trades perdants d'affilee
  R3 mom     les 8 plus fortes hausses sur 6 mois (hors derniere semaine), liquides
  R4 vol     les 8 ou +3.5% intraday est le plus frequent sur 12 mois, liquides
  R5 mom+vol les 8 meilleurs momentum parmi les 30 plus volatils
"Carnet fantome" = trades que la strategie aurait faits sur chaque ticker pris seul,
comptes seulement une fois SORTIS (connus a la date de decision).

Periodes: TRAIN 2024-05 -> 2025-03 (apres 6 mois d'historique), TEST 2025-04 -> 2026-09.

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_dynamic_universe.py
"""
from __future__ import annotations

import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import backtest_swing as B  # noqa: E402
import research_data as RD  # noqa: E402
import research_engine as E  # noqa: E402
import research_tickers as T  # noqa: E402

NY = "America/New_York"
N = 8
CURRENT8 = [t for t in T.CURRENT if t != "CVS"]
TRAIN = (pd.Timestamp("2024-05-01 09:30", tz=NY), T.TRAIN[1])
TEST = T.TEST
WEEK = E.Exit("week", sl_pct=10, tp_pct=25, max_hold_tdays=5)
KW = dict(line_usd=1000, max_open=1, budget=1000)


def pool_names(cache):
    out = []
    for tk, h in cache["h1"].items():
        d = cache["d1"].get(tk)
        if d is None or len(h) < 3000 or h.index[0] > pd.Timestamp("2023-12-01", tz=NY):
            continue
        out.append(tk)
    return sorted(out)


def monthly_stats(d: pd.DataFrame, month_start: pd.Timestamp):
    """Statistiques connues la veille du 1er du mois."""
    x = d[d.index < month_start.tz_localize(None)]
    if len(x) < 260:
        return None
    last = x.iloc[-252:]
    c = x["Close"]
    return {
        "px": float(c.iloc[-1]),
        "dvol": float((last["Close"] * last["Volume"]).median()),
        "reach": float((last["High"] >= last["Open"] * 1.035).mean()),
        "mom": float(c.iloc[-6] / c.iloc[-126] - 1),
    }


def main():
    with open(T.CACHE, "rb") as f:
        cache = pickle.load(f)
    rd = RD.load()
    pool = pool_names(cache)
    print("pool: %d titres" % len(pool))
    old = B.TICKERS
    B.TICKERS, B.VIX_THRESHOLD = pool, 22.0
    try:
        sig = B.build_signals(T.make_data(cache, rd, pool))
    finally:
        B.TICKERS, B.VIX_THRESHOLD = old, 22.0
    sig = sig[sig["breakout"]].reset_index(drop=True)
    sig["kind"], sig["prio"] = "open", 0.0

    # carnet fantome: trades de chaque ticker pris seul
    shadow = {}
    for tk in pool:
        s = sig[sig.ticker == tk]
        if s.empty:
            shadow[tk] = []
            continue
        r = E.simulate(E.Bars({tk: cache["h1"][tk]}), s, WEEK, max_open=1, line_usd=1000, budget=1000)
        shadow[tk] = [(pd.Timestamp(t["exit_ts"]), t["net"]) for t in r["trades"]]

    months = pd.date_range("2024-05-01", "2026-09-01", freq="MS", tz=NY)
    uni = {k: {} for k in ("FIXE-8", "POOL", "R1 perf", "R2 coupe", "R3 mom", "R4 vol", "R5 mom+vol")}
    for m in months:
        st = {}
        for tk in pool:
            s = monthly_stats(cache["d1"][tk], m)
            if s and 5 <= s["px"] <= 600 and s["dvol"] >= 100e6:
                st[tk] = s
        liquid = list(st)
        uni["FIXE-8"][m] = set(CURRENT8)
        uni["POOL"][m] = set(pool)
        perf = {tk: sum(n for t, n in shadow[tk] if m - pd.DateOffset(months=3) <= t < m) for tk in liquid}
        uni["R1 perf"][m] = set(sorted(liquid, key=lambda t: -perf[t])[:N])
        susp = set()
        for tk in CURRENT8:
            past = [n for t, n in shadow.get(tk, []) if t < m]
            last_exit = max((t for t, n in shadow.get(tk, []) if t < m), default=None)
            if len(past) >= 2 and past[-1] < 0 and past[-2] < 0 and last_exit is not None \
                    and last_exit >= m - pd.DateOffset(months=1):
                susp.add(tk)
        uni["R2 coupe"][m] = set(CURRENT8) - susp
        uni["R3 mom"][m] = set(sorted(liquid, key=lambda t: -st[t]["mom"])[:N])
        uni["R4 vol"][m] = set(sorted(liquid, key=lambda t: -st[t]["reach"])[:N])
        vol30 = sorted(liquid, key=lambda t: -st[t]["reach"])[:30]
        uni["R5 mom+vol"][m] = set(sorted(vol30, key=lambda t: -st[t]["mom"])[:N])

    def filt(rule):
        keys = sig["ts"].dt.tz_convert(NY).dt.to_period("M").dt.start_time.dt.tz_localize(NY)
        u = uni[rule]
        keep = [tk in u.get(k, set()) for tk, k in zip(sig["ticker"], keys)]
        return sig[keep].reset_index(drop=True)

    bars = E.Bars({t: cache["h1"][t] for t in pool})
    print("=" * 108)
    print("ROTATION D'UNIVERS — univers choisi chaque mois avec les donnees passees uniquement, 1 x 1000$")
    print("=" * 108)
    print("%-12s | %9s %8s | %9s %8s %7s | %8s %s" % ("regle", "TRAIN $", "p10", "TEST $", "p10", "DD%", "rotation", "univers actuel (sept. 2026)"))
    for rule in uni:
        s = filt(rule)
        a = E.robust(bars, s, WEEK, reps=12, win=TRAIN, **KW)
        b = E.robust(bars, s, WEEK, reps=12, win=TEST, **KW)
        ks = sorted(uni[rule])
        turn = np.mean([len(uni[rule][ks[i]] - uni[rule][ks[i - 1]]) for i in range(1, len(ks))])
        last = sorted(uni[rule][ks[-1]])
        print("%-12s | %+9.0f %+8.0f | %+9.0f %+8.0f %+7.1f | %5.1f/mois %s"
              % (rule, a["med"], a["p10"], b["med"], b["p10"], b["m"]["dd_pct"], turn,
                 ",".join(last) if len(last) <= 10 else "%d titres" % len(last)), flush=True)
    print("\nrotation = tickers remplaces en moyenne chaque mois.")


if __name__ == "__main__":
    main()
