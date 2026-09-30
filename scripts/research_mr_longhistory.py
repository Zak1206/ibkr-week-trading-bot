# -*- coding: utf-8 -*-
"""
RSI(2) Connors sur 25 ans de donnees journalieres: l'edge tient-il hors du
marche haussier 2023-2026 ?

Regles publiees, non modifiees: close > SMA200 et RSI(2) < 10 -> achat;
sortie des que close > SMA5, ou au plus tard apres 5 seances. Pas de stop.
2 lignes x 520$, 1000$ non compose, frais IBKR + 3 bps.

Deux executions encadrent la realite du bot (qui decide a 15h30 et passe
un ordre MOC, cf. research_strategies.py):
  ideal  : decision ET execution au close du jour (legere anticipation)
  prudent: entree a l'ouverture du lendemain, sortie a l'ouverture qui suit
           le drapeau (aucune anticipation, mais on rate le rebond d'ouverture)

Placebo: memes sorties, meme nombre d'entrees par ETF, dates au hasard parmi
les jours ou close > SMA200. Isole l'apport du RSI(2) par rapport a la simple
derive haussiere d'un ETF en tendance.

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_mr_longhistory.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import research_data as RD  # noqa: E402
import research_engine as E  # noqa: E402

ETF = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY",
       "XLP", "XLU", "XLB", "SMH"]
BOT = ["PLTR", "SMCI", "IONQ", "HOOD", "SOFI", "COIN", "RIVN", "RKLB", "CVS"]
END = pd.Timestamp("2026-09-04")

PERIODS = [
    ("2001-2007", "2001-01-01", "2007-12-31"),
    ("2008-2009 crise", "2008-01-01", "2009-12-31"),
    ("2010-2019", "2010-01-01", "2019-12-31"),
    ("2020-2022 covid+bear", "2020-01-01", "2022-12-31"),
    ("2023-10 -> 2026-09", "2023-10-31", "2026-09-04"),
]


def indicators(d: pd.DataFrame) -> pd.DataFrame:
    c = d["Close"].astype(float)
    delta = c.diff()
    ag = delta.clip(lower=0).ewm(alpha=0.5, adjust=False, min_periods=2).mean()
    al = (-delta.clip(upper=0)).ewm(alpha=0.5, adjust=False, min_periods=2).mean()
    rsi = 100 - 100 / (1 + ag / al)
    out = pd.DataFrame({"rsi": rsi.where(al > 0, 100.0),
                        "sma200": c.rolling(200).mean(),
                        "sma5": c.rolling(5).mean(), "c": c}, index=d.index)
    return out


def build(d1: dict, tickers: list, mode: str, start: str):
    rows, flags, trend = [], {}, {}
    h = {}
    for tk in tickers:
        d = d1[tk]
        d = d[d.index <= END]
        ind = indicators(d)
        entry = (ind.c > ind.sma200) & (ind.rsi < 10) & ind.sma200.notna()
        flags[tk] = (ind.c > ind.sma5).values
        trend[tk] = ((ind.c > ind.sma200) & ind.sma200.notna()).values
        h[tk] = d
        idx = np.where(entry.values & (d.index >= start))[0]
        for i in idx:
            if mode == "ideal":
                rows.append({"ts": d.index[i], "ticker": tk, "kind": "close",
                             "prio": -float(ind.rsi.iloc[i])})
            elif i + 1 < len(d):
                rows.append({"ts": d.index[i + 1], "ticker": tk, "kind": "open",
                             "prio": -float(ind.rsi.iloc[i])})
    sig = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    return sig, flags, trend, h


def placebo(counts, t_first, trend, h, mode, rng):
    """Autant d'entrees que de trades REELLEMENT executes par ETF (pas de
    signaux: les signaux consecutifs d'une meme baisse ne font qu'un trade)."""
    rows = []
    for tk, k in counts.items():
        idx = h[tk].index
        pool = np.where(trend[tk] & (idx >= t_first))[0]
        pool = pool[pool + 1 < len(idx)]
        pick = rng.choice(pool, size=min(k, len(pool)), replace=False)
        for i in pick:
            if mode == "ideal":
                rows.append({"ts": idx[i], "ticker": tk, "kind": "close", "prio": 0.0})
            else:
                rows.append({"ts": idx[i + 1], "ticker": tk, "kind": "open", "prio": 0.0})
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def run(label, d1, tickers, start, periods, n_placebo=40):
    print("=" * 112)
    print(label)
    print("=" * 112)
    for mode in ("ideal", "prudent"):
        sig, flags, trend, h = build(d1, tickers, mode, start)
        bars = E.Bars(h)
        ex = E.Exit("mr", exit_flag=True, flag_next_open=(mode == "prudent"), max_hold_tdays=5)
        print("\n--- execution %s (%d signaux) ---" % (mode, len(sig)))
        print("%-22s %6s %9s %8s %7s %6s %7s %7s %6s"
              % ("periode", "n", "net $", "$/an", "bps/tr", "t", "gagn%", "DD%", "Shrp"))
        for name, a, b in periods:
            win = (pd.Timestamp(a), pd.Timestamp(b))
            r = E.simulate(bars, sig, ex, win=win, exit_flags=flags)
            m = E.metrics(r)
            yrs = max(0.1, (win[1] - win[0]).days / 365.25)
            print("%-22s %6d %+9.0f %+8.0f %+7.1f %6.2f %6.1f%% %+6.1f %6.2f"
                  % (name, m["n"], m["net"], m["net"] / yrs, m["bps"], m["t"], m["win"],
                     m["dd_pct"], m["sharpe"]))
        # placebo sur toute la periode
        win = (pd.Timestamp(periods[0][1]), pd.Timestamp(periods[-1][2]))
        real_res = E.simulate(bars, sig, ex, win=win, exit_flags=flags)
        real = E.metrics(real_res)
        counts = pd.Series([t["ticker"] for t in real_res["trades"]]).value_counts().to_dict()
        rng = np.random.default_rng(7)
        pn, pb = [], []
        for _ in range(n_placebo):
            ps = placebo(counts, max(sig["ts"].min(), win[0]), trend, h, mode, rng)
            pm = E.metrics(E.simulate(bars, ps, ex, win=win, exit_flags=flags))
            pn.append(pm["net"])
            pb.append(pm["bps"])
        pn, pb = np.array(pn), np.array(pb)
        print("%-22s %6d %+9.0f   bps/tr %+6.1f   | placebo: net median %+.0f [p5 %+.0f, p95 %+.0f], bps %+.1f -> rang %.0f%%"
              % ("TOTAL", real["n"], real["net"], real["bps"], np.median(pn),
                 np.percentile(pn, 5), np.percentile(pn, 95), np.median(pb),
                 100.0 * (pn < real["net"]).mean()))
    print()


def main():
    rd = RD.load()
    run("RSI(2) CONNORS — 14 ETF, 2001 -> 2026 (regles publiees, aucun reglage)",
        rd["d1"], ETF, "2001-01-01", PERIODS)
    run("RSI(2) CONNORS — univers du bot (journalier ajuste, depuis les IPO)",
        rd["d1"], BOT, "2021-01-01",
        [("2022 bear", "2022-01-01", "2022-12-31"),
         ("2023-01 -> 2023-10", "2023-01-01", "2023-10-30"),
         ("2023-10 -> 2026-09", "2023-10-31", "2026-09-04")])


if __name__ == "__main__":
    main()
