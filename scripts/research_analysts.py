# -*- coding: utf-8 -*-
"""
Actions d'analystes (yfinance upgrades_downgrades, historique depuis ~2021, gratuit):
changements de recommandation ET revisions d'objectif de cours. Ameliorent-elles
le trade "breakout semaine" ? Meme protocole que research_features.py.

  down10    abaissement de recommandation <= 10 j        sens -  (Womack 1996)
  up5       relevement de recommandation <= 5 j          sens +
  pt_net10  (objectifs releves - abaisses) sur 10 j      sens +  (Brav & Lehavy 2003)
  pt_chg10  variation moyenne des objectifs sur 10 j     sens +
  pt_gap    objectif moyen (90 j) / prix - 1             sens +  (Da & Schaumburg 2011)
Seuls les evenements publies AVANT l'heure du signal sont utilises.

Puis, si un signal passe: effet en portefeuille (1 x 1000$, sortie semaine) du
filtre correspondant sur TRAIN et TEST.

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_analysts.py
"""
from __future__ import annotations

import os
import pickle
import sys

import numpy as np
import pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import research_data as RD  # noqa: E402
import research_features as RF  # noqa: E402
import research_tickers as T  # noqa: E402

CACHE = os.path.join(HERE, ".analysts_cache.pkl")
HYP = {"down10": -1, "up5": +1, "pt_net10": +1, "pt_chg10": +1, "pt_gap": +1}


def load(names):
    if os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            out = pickle.load(f)
    else:
        out = {}
    for tk in names:
        if tk in out:
            continue
        try:
            ud = yf.Ticker(tk).upgrades_downgrades
            if ud is not None and len(ud):
                ud = ud.copy()
                ud.index = pd.to_datetime(ud.index).tz_localize("UTC")
                out[tk] = ud.sort_index()
        except Exception:
            pass
    with open(CACHE, "wb") as f:
        pickle.dump(out, f)
    return out


def features(ev, ana, cache):
    ev = ev.copy()
    for f in HYP:
        ev[f] = np.nan
    for tk, g in ev.groupby("ticker"):
        ud = ana.get(tk)
        if ud is None:
            continue
        first = ud.index.min()
        act = ud["Action"].astype(str).str.lower()
        pta = ud["priceTargetAction"].astype(str).str.lower()
        cur = pd.to_numeric(ud["currentPriceTarget"], errors="coerce")
        pri = pd.to_numeric(ud["priorPriceTarget"], errors="coerce")
        h = cache["h1"][tk]
        for k in g.index:
            t_sig = (ev.at[k, "ts"] - pd.Timedelta(hours=1)).tz_convert("UTC")   # cloture de la barre signal
            if t_sig < first + pd.Timedelta(days=90):
                continue
            m = ud.index <= t_sig
            w10 = m & (ud.index >= t_sig - pd.Timedelta(days=10))
            w5 = m & (ud.index >= t_sig - pd.Timedelta(days=5))
            w90 = m & (ud.index >= t_sig - pd.Timedelta(days=90))
            ev.at[k, "down10"] = float((act[w10] == "down").any())
            ev.at[k, "up5"] = float((act[w5] == "up").any())
            ev.at[k, "pt_net10"] = float((pta[w10] == "raises").sum() - (pta[w10] == "lowers").sum())
            chg = (cur[w10] / pri[w10] - 1).replace([np.inf, -np.inf], np.nan).dropna()
            ev.at[k, "pt_chg10"] = float(chg.mean()) if len(chg) else 0.0
            tgt = cur[w90].dropna()
            px_i = h.index.searchsorted(ev.at[k, "ts"]) - 1
            if len(tgt) and px_i >= 0:
                ev.at[k, "pt_gap"] = float(tgt.mean() / h["Close"].iloc[px_i] - 1)
    return ev


def cluster_diff(m, f):
    """Ecart de gain moyen (evenement vs reste) et IC95% bootstrap par semaine."""
    rng = np.random.default_rng(0)
    g = [grp for _, grp in m.groupby("week")]
    diffs = []
    for _ in range(2000):
        s = pd.concat([g[p] for p in rng.integers(0, len(g), len(g))])
        a, b = s[s[f] == 1].y_week, s[s[f] == 0].y_week
        if len(a) and len(b):
            diffs.append(a.mean() - b.mean())
    a, b = m[m[f] == 1].y_week, m[m[f] == 0].y_week
    return a.mean() - b.mean(), np.percentile(diffs, 2.5), np.percentile(diffs, 97.5), len(a)


def main():
    with open(T.CACHE, "rb") as f:
        cache = pickle.load(f)
    rd = RD.load()
    scr = T.screen(cache["d1"])
    # meme echantillon de 42 tickers que research_features / research_sources
    with open(os.path.join(HERE, ".sources_cache.pkl"), "rb") as f:
        ref = set(pickle.load(f)["iv"])
    names = sorted((set(scr.ticker) | set(T.CURRENT) | set(T.EXTRA)) & set(cache["h1"]) & ref)
    ana = load(names)
    ev = RF.build(cache, rd, names).dropna(subset=["y_week"])
    ev = features(ev, ana, cache)
    print("=" * 112)
    print("ACTIONS D'ANALYSTES (yfinance) — %d breakouts, %d tickers avec historique d'analystes"
          % (len(ev), len(ana)))
    print("=" * 112)
    print("%-10s %2s | %-28s | %-28s | %s" % ("signal", "", "TRAIN rho [IC95] n", "TEST rho [IC95] n", "verdict"))
    tr = ev[(ev.ts >= T.TRAIN[0]) & (ev.ts <= T.TRAIN[1])]
    te = ev[(ev.ts >= T.TEST[0]) & (ev.ts <= T.TEST[1])]
    for f, sgn in HYP.items():
        r1, a1, b1, n1 = RF.rho_ci(tr, f, "y_week")
        r2, a2, b2, n2 = RF.rho_ci(te, f, "y_week", seed=1)
        tok = (a1 > 0) if sgn > 0 else (b1 < 0)
        tsig = (a2 > 0) if sgn > 0 else (b2 < 0)
        v = ("ROBUSTE" if tok and tsig else "retenu" if tok and np.sign(r2) == sgn else
             "rejete: s'inverse" if tok else "test seul" if tsig else "-")
        print("%-10s %2s | %+.3f [%+.3f,%+.3f] n=%4d | %+.3f [%+.3f,%+.3f] n=%4d | %s"
              % (f, "+" if sgn > 0 else "-", r1, a1, b1, n1, r2, a2, b2, n2, v))
    print("\nEvenements binaires: gain moyen du trade semaine avec l'evenement - sans (IC95% par semaine)")
    for f in ("down10", "up5"):
        for lab, m in (("TRAIN", tr), ("TEST", te)):
            m = m.dropna(subset=[f])
            d, lo, hi, n = cluster_diff(m, f)
            print("  %-7s %-5s n=%3d  ecart %+6.2f%%  [%+6.2f, %+6.2f]" % (f, lab, n, 100 * d, 100 * lo, 100 * hi))
    ev.to_csv(os.path.join(HERE, "research_analysts_events.csv"), index=False)


if __name__ == "__main__":
    main()
