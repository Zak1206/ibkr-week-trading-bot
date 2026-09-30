# -*- coding: utf-8 -*-
"""
Derive post-resultats (PEAD): l'anomalie a horizon de quelques jours la mieux
documentee sur actions individuelles (Ball & Brown 1968, Bernard & Thomas 1989,
Chan, Jegadeesh & Lakonishok 1996; reaction du marche: Brandt et al. 2008).
Bat-elle le hasard la ou les signaux prix/volume echouent (research_edge95.py) ?

Donnees: dates, BPA attendu/publie, surprise % (yfinance get_earnings_dates,
extraites via Anaconda car lxml manque dans le venv du bot:
scripts/research_earnings_dates.json).

Regles fixees AVANT le test:
  P0 toutes les publications (reference) -> achat a l'ouverture de la seance de reaction
  P1 BPA > consensus                    -> achat a l'ouverture de la seance de reaction
  P2 BPA > consensus ET ouverture en gap haussier -> achat apres la 1re heure (10h30)
  P3 reaction >= +5% (veille -> cloture du jour de reaction) -> achat a l'ouverture du lendemain
Seance de reaction: meme jour si publication avant 9h, sinon seance suivante.
Sortie: SL -10 / TP +25 / 5 seances (strategie semaine). Temoin: meme titre, meme
heure, +-30 seances hors +-5 autour de l'evenement. TRAIN / TEST (18 derniers mois).

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_pead.py
"""
from __future__ import annotations

import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import research_features as RF  # noqa: E402
import research_tickers as T  # noqa: E402

NY = "America/New_York"
K_RANDOM, MATCH, EXCL = 16, 30, 5


def main():
    with open(T.CACHE, "rb") as f:
        cache = pickle.load(f)
    with open(os.path.join(HERE, "research_earnings_dates.json"), encoding="utf-8") as f:
        earn = json.load(f)
    rng = np.random.default_rng(1968)
    events = {"P0 toutes publications": [], "P1 BPA > consensus": [],
              "P2 BPA > consensus + gap haussier": [], "P3 reaction >= +5%": []}
    arrays = {}
    for tk, lst in earn.items():
        if tk not in cache["h1"]:
            continue
        h = cache["h1"][tk]
        o, hi, lo, c = (h[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
        dayidx, uniq = pd.factorize(h.index.normalize())
        lod = np.r_[dayidx[1:] != dayidx[:-1], True]
        firsts = np.where(np.r_[True, dayidx[1:] != dayidx[:-1]])[0]
        slot = np.arange(len(h)) - firsts[dayidx]
        day_dates = pd.DatetimeIndex(uniq).tz_localize(None).normalize()
        arrays[tk] = ((o, hi, lo, c, dayidx, lod), slot, h.index)
        last_close = pd.Series(c).groupby(dayidx).last().values
        for e in lst:
            if e["surp"] is None:
                continue
            ts = pd.Timestamp(e["ts"])
            ts = ts.tz_convert(NY) if ts.tzinfo else ts.tz_localize(NY)
            d = ts.tz_localize(None).normalize()
            k = int(np.searchsorted(day_dates.values, d.to_datetime64(), side="left"))
            if not (ts.hour < 9 and k < len(day_dates) and day_dates[k] == d):
                k = int(np.searchsorted(day_dates.values, d.to_datetime64(), side="right"))
            if k < 1 or k + 1 >= len(firsts):
                continue
            j0 = firsts[k]                                   # 1re barre de la seance de reaction
            prev_close = last_close[k - 1]
            gap = o[j0] / prev_close - 1
            react = last_close[k] / prev_close - 1
            events["P0 toutes publications"].append((tk, j0))
            if e["surp"] > 0:
                events["P1 BPA > consensus"].append((tk, j0))
                if gap > 0 and j0 + 1 < len(o) and dayidx[j0 + 1] == k:
                    events["P2 BPA > consensus + gap haussier"].append((tk, j0 + 1))
            if react >= 0.05:
                events["P3 reaction >= +5%"].append((tk, firsts[k + 1]))

    print("=" * 118)
    print("DERIVE POST-RESULTATS contre le hasard — %d tickers, %d publications passees, sortie semaine"
          % (len(arrays), len(events["P0 toutes publications"])))
    print("=" * 118)
    print("%-36s | %-38s | %-38s | %s" % ("regle", "TRAIN: n, gain, avantage [IC95]", "TEST: n, gain, avantage [IC95]", "verdict"))
    for lab, evs in events.items():
        rows = []
        for tk, j in evs:
            arr, slot, idx = arrays[tk]
            if j >= len(idx) - 1:
                continue
            y = RF.week_trade(*arr, j)
            if np.isnan(y):
                continue
            d0 = arr[4][j]
            a = np.searchsorted(arr[4], d0 - MATCH, side="left")
            b = np.searchsorted(arr[4], d0 + MATCH, side="right")
            cand = np.arange(a, min(b, len(idx) - 1))
            cand = cand[(slot[cand] == slot[j]) & (np.abs(arr[4][cand] - d0) > EXCL)]
            if len(cand) < 4:
                continue
            ys = [RF.week_trade(*arr, int(q)) for q in rng.choice(cand, K_RANDOM, replace=True)]
            ys = [v for v in ys if not np.isnan(v)]
            if ys:
                rows.append({"ts": idx[j], "y": y, "x": y - float(np.mean(ys)),
                             "week": idx[j].to_period("W").start_time})
        df = pd.DataFrame(rows)
        cells, res = [], []
        for w0, w1 in (T.TRAIN, T.TEST):
            m = df[(df.ts >= w0) & (df.ts <= w1)] if len(df) else df
            if len(m) < 20:
                cells.append("n=%d (trop peu)" % len(m))
                res.append(None)
                continue
            g = [grp["x"].values for _, grp in m.groupby("week")]
            bs = [np.concatenate([g[p] for p in rng.integers(0, len(g), len(g))]).mean() for _ in range(2000)]
            lo_, hi_ = np.percentile(bs, [2.5, 97.5])
            cells.append("n=%3d %+5.2f%% | %+5.2f%% [%+5.2f,%+5.2f]"
                         % (len(m), 100 * m.y.mean(), 100 * m.x.mean(), 100 * lo_, 100 * hi_))
            res.append((m.x.mean(), lo_))
        if None in res:
            v = "donnees insuffisantes"
        elif res[0][1] > 0 and res[1][1] > 0:
            v = "ROBUSTE: bat le hasard sur les deux periodes"
        elif res[0][1] > 0 and res[1][0] > 0:
            v = "retenu (train significatif, test positif)"
        elif res[0][0] > 0 and res[1][0] > 0:
            v = "positif partout, non significatif"
        else:
            v = "ne bat pas le hasard"
        print("%-36s | %-38s | %-38s | %s" % (lab, cells[0], cells[1], v), flush=True)
    print("\nTemoin: %d entrees au hasard, meme titre, meme heure, +-%d seances hors +-%d." % (K_RANDOM, MATCH, EXCL))


if __name__ == "__main__":
    main()
