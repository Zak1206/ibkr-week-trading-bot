# -*- coding: utf-8 -*-
"""
Le "vrai test" de Zak: sur la periode ou le bot a tourne en paper (fin mai -> fin
aout 2026), que donnent
  - le paper REEL (ancienne config, journal),
  - l'ancienne config en BACKTEST (sert a calibrer: le backtest retrouve-t-il le reel ?),
  - la nouvelle config "breakout semaine" en backtest,
  - un bot au hasard (memes sorties que la nouvelle config) et le buy & hold ?

Deux lectures: periode complete, et entrees limitees aux JOURS OU LE BOT TOURNAIT
(evenements du journal), pour ne pas creditor la nouvelle config de trades pris
pendant que le bot etait eteint.

Ancienne config reconstituee par morceaux depuis les config_snapshot du journal:
  jusqu'au 02/08: 12 tickers, 3 x 300$, SL -2 / TP +4, flat >= 0.8%
  03/08 -> 09/08: 9 tickers, 2 x 520$, SL -2 / TP +4, flat >= 1.2% (a partir du 07/08)
  a partir du 10/08: TP +3.5%
  (approximation: l'univers reel de fin mai/juin etait plus large, UNH/DIS/XOM...)

Usage: .\\.venv\\Scripts\\python.exe scripts\\compare_paper_period.py
"""
from __future__ import annotations

import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import backtest_swing as B  # noqa: E402
import research_data as RD  # noqa: E402
import research_engine as E  # noqa: E402
import research_tickers as T  # noqa: E402

NY = "America/New_York"
P0 = pd.Timestamp("2026-05-26 09:30", tz=NY)
P1 = pd.Timestamp("2026-08-27 16:00", tz=NY)
NEW_U = ["PLTR", "SMCI", "IONQ", "HOOD", "SOFI", "COIN", "RIVN", "RKLB"]
OLD_U12 = NEW_U + ["CVS", "RGTI", "UPST", "ASTS"]
OLD_U9 = NEW_U + ["CVS"]
FEE_PER_ORDER_EST = 0.40  # IBKR tiered min 0.35$ + frais de bourse


def journal():
    rows = [json.loads(l) for l in open(os.path.join(ROOT, "trades_journal.jsonl"), encoding="utf-8") if l.strip()]
    ev = pd.DataFrame(rows)
    ev["ts"] = pd.to_datetime(ev["ts_utc"]).dt.tz_convert(NY)
    act = ev[ev.event.isin(["signal_buy", "confirm_buy", "ibkr_order_filled", "ibkr_order_failed",
                            "signal_sell", "confirm_sell", "swap_executed", "session_start", "config_snapshot"])]
    active = set(act.ts.dt.date)
    tc = ev[(ev.event == "trade_closed") & (ev.get("void").isna() if "void" in ev else True)]
    return tc, active


def signals(cache, rd, tickers, vix):
    old = B.TICKERS
    B.TICKERS, B.VIX_THRESHOLD = list(tickers), vix
    try:
        s = B.build_signals(T.make_data(cache, rd, tickers))
    finally:
        B.TICKERS, B.VIX_THRESHOLD = old, 22.0
    s["kind"], s["prio"] = "open", 0.0
    return s


def only_active(sig, active):
    return sig[sig["ts"].dt.date.isin(active)].reset_index(drop=True)


def dist(bars, sig, ex, win, reps=16, **kw):
    nets, ns = [], []
    for k in range(reps):
        r = E.simulate(bars, sig, ex, seed=600 + k, win=win, **kw)
        nets.append(sum(t["net"] for t in r["trades"]))
        ns.append(len(r["trades"]))
    nets = np.sort(nets)
    return float(np.median(nets)), float(np.percentile(nets, 10)), float(np.percentile(nets, 90)), int(np.median(ns))


def main():
    with open(T.CACHE, "rb") as f:
        cache = pickle.load(f)
    rd = RD.load()
    tc, active = journal()
    gross = float(tc.pnl_usd.sum())
    fees = 2 * FEE_PER_ORDER_EST * len(tc)

    # --- ancienne config, par morceaux
    s12 = signals(cache, rd, OLD_U12, 26.0)
    s9 = signals(cache, rd, OLD_U9, 26.0)
    b12 = E.Bars({t: cache["h1"][t] for t in OLD_U12})
    b9 = E.Bars({t: cache["h1"][t] for t in OLD_U9})
    w_a = (P0, pd.Timestamp("2026-08-02 16:00", tz=NY))
    w_b = (pd.Timestamp("2026-08-03 09:30", tz=NY), pd.Timestamp("2026-08-09 16:00", tz=NY))
    w_c = (pd.Timestamp("2026-08-10 09:30", tz=NY), P1)
    ex_a = E.Exit("old a", sl_pct=2, tp_pct=4.0, trailing=True, flat_min_profit=0.8)
    ex_b = E.Exit("old b", sl_pct=2, tp_pct=4.0, trailing=True, flat_min_profit=1.2)
    ex_c = E.Exit("old c", sl_pct=2, tp_pct=3.5, trailing=True, flat_min_profit=1.2)

    def old_cfg(act_only):
        f = (lambda s: only_active(s, active)) if act_only else (lambda s: s)
        parts = [dist(b12, f(s12), ex_a, w_a, budget=1000, line_usd=300, max_open=3),
                 dist(b9, f(s9), ex_b, w_b, budget=1000, line_usd=520, max_open=2),
                 dist(b9, f(s9), ex_c, w_c, budget=1000, line_usd=520, max_open=2)]
        return tuple(sum(p[i] for p in parts) for i in range(4))

    # --- nouvelle config
    sn = signals(cache, rd, NEW_U, 22.0)
    sn = sn[sn["breakout"]].reset_index(drop=True)
    bn = E.Bars({t: cache["h1"][t] for t in NEW_U})
    week = E.Exit("week", sl_pct=10, tp_pct=25, max_hold_tdays=5)
    win = (P0, P1)

    def new_cfg(act_only):
        s = only_active(sn, active) if act_only else sn
        return dist(bn, s, week, win, budget=1000, line_usd=1000, max_open=1)

    def placebo(act_only, reps=60):
        rng = np.random.default_rng(21)
        nets = []
        for k in range(reps):
            rows = []
            for tk in NEW_U:
                h = cache["h1"][tk]
                ok = (h.index >= P0) & (h.index <= P1)
                if act_only:
                    ok &= pd.Series(h.index.date, index=h.index).isin(active).values
                idx = np.where(ok)[0]
                for j in rng.choice(idx, size=max(1, len(idx) // 40), replace=False):
                    rows.append({"ts": h.index[j], "ticker": tk, "kind": "open", "prio": 0.0})
            ps = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
            r = E.simulate(bn, ps, week, seed=k, win=win, budget=1000, line_usd=1000, max_open=1)
            nets.append(sum(t["net"] for t in r["trades"]))
        return np.array(nets)

    def bh(tks):
        a, b = P0.tz_localize(None).normalize(), P1.tz_localize(None).normalize()
        px = pd.DataFrame({t: (rd["d1"][t]["Close"] if t in rd["d1"] else cache["d1"][t]["Close"]) for t in tks})
        px = px[(px.index >= a) & (px.index <= b)].dropna()
        return float(((px.iloc[-1] / px.iloc[0]).mean() - 1) * 1000)

    print("=" * 104)
    print("PERIODE DU PAPER: %s -> %s (%d jours ou le bot a tourne)" % (P0.date(), P1.date(), len(active)))
    print("=" * 104)
    print("%-52s %9s %9s %9s %7s" % ("", "net $", "p10", "p90", "trades"))
    print("%-52s %+9.0f %9s %9s %7d" % ("PAPER REEL, ancienne config (brut journal)", gross, "", "", len(tc)))
    print("%-52s %+9.0f %9s %9s %7d" % ("PAPER REEL, net de frais estimes (%.2f$/ordre)" % FEE_PER_ORDER_EST,
                                          gross - fees, "", "", len(tc)))
    for act_only, lab in ((True, "jours actifs"), (False, "periode complete")):
        o = old_cfg(act_only)
        n = new_cfg(act_only)
        pl = placebo(act_only)
        print("--- %s ---" % lab)
        print("%-52s %+9.0f %+9.0f %+9.0f %7d" % ("ancienne config en BACKTEST (calibration)", *o))
        print("%-52s %+9.0f %+9.0f %+9.0f %7d" % ("NOUVELLE config breakout semaine (1 x 1000$)", *n))
        print("%-52s %+9.0f %+9.0f %+9.0f" % ("bot au hasard, memes sorties (60 tirages)", np.median(pl),
                                              np.percentile(pl, 10), np.percentile(pl, 90)))
        print("   rang de la nouvelle config parmi les bots au hasard: %.0f%%" % (100 * (pl < n[0]).mean()))
    print("--- buy & hold sur la periode (1000$) ---")
    for lab, tks in (("SPY", ["SPY"]), ("QQQ", ["QQQ"]), ("les 8 actions", NEW_U)):
        print("%-52s %+9.0f" % ("buy & hold " + lab, bh(tks)))


if __name__ == "__main__":
    main()
