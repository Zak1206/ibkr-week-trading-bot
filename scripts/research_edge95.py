# -*- coding: utf-8 -*-
"""
Objectif de Zak: que l'entree du bot batte un bot au hasard avec >= 95% de
confiance. Ce script mesure l'avantage REEL de chaque type d'entree, sans
bidouillage:

  - chaque entree est comparee a des entrees tirees au hasard dans le MEME titre
    et la MEME semaine (+-5 seances), avec la MEME sortie (SL -10 / TP +25 /
    5 seances). La difference = ce qu'apporte le signal, net du marche et du titre;
  - IC95% par bootstrap en grappes par semaine; TRAIN et TEST (18 derniers mois)
    separes; 42 tickers volatils (~milliers d'evenements = test puissant);
  - regles fixees AVANT le test, tirees de la litterature, aucun reglage:
      E0 breakout 1h du bot (reference)
      E1 choc avec news -> continuation (Chan 2003; Gervais, Kaniel & Mingelgrin 2001):
         jour a >= +4% sur volume >= 2.5x la moyenne 20 j -> achat a l'ouverture suivante
      E2 baisse sans news -> retournement (Chan 2003): jour a <= -4% sur volume
         <= 1.2x la moyenne, titre au-dessus de sa SMA50 -> achat a l'ouverture suivante
      E3 retournement court terme (Jegadeesh 1990; Connors): RSI(2) < 10 et
         close > SMA200 -> achat a l'ouverture suivante
      E4 plus haut 52 semaines (George & Hwang 2004): cloture au-dessus du plus
         haut des 252 j precedents, volume >= 1.5x -> achat a l'ouverture suivante
  - une entree n'est retenue que si: TRAIN IC95% > 0 ET TEST moyenne > 0.

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_edge95.py
"""
from __future__ import annotations

import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_swing as B  # noqa: E402
import research_data as RD  # noqa: E402
import research_features as RF  # noqa: E402
import research_tickers as T  # noqa: E402

K_RANDOM = 16         # entrees aleatoires appariees par evenement
MATCH_SESSIONS = 30   # temoin: +- 30 seances autour de l'entree...
EXCLUDE_SESSIONS = 5  # ...SAUF les +- 5 seances autour de l'evenement. Un voisinage proche
                      # est contamine: les signaux sont definis par le mouvement qui les
                      # precede (un breakout suit une hausse), donc des entrees tirees juste
                      # avant le signal capturent ce mouvement et faussent la comparaison
                      # (bug trouve le 2026-09-29: -3.3% breakout / +3.2% RSI2, artefacts).


def day_first_bar(h: pd.DataFrame):
    days = h.index.normalize()
    codes, uniq = pd.factorize(days)
    first = np.r_[True, codes[1:] != codes[:-1]]
    return codes, np.where(first)[0], uniq


def daily_events(d: pd.DataFrame):
    c, v = d["Close"].astype(float), d["Volume"].astype(float)
    ret = c / c.shift(1) - 1
    vavg = v.rolling(20).mean().shift(1)   # moyenne des jours PRECEDENTS
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    delta = c.diff()
    ag = delta.clip(lower=0).ewm(alpha=0.5, adjust=False, min_periods=2).mean()
    al = (-delta.clip(upper=0)).ewm(alpha=0.5, adjust=False, min_periods=2).mean()
    rsi2 = (100 - 100 / (1 + ag / al)).where(al > 0, 100.0)
    hi252 = d["High"].astype(float).rolling(252, min_periods=200).max().shift(1)
    return {
        "E1 news -> continuation": (ret >= 0.04) & (v >= 2.5 * vavg),
        "E2 baisse sans news -> rebond": (ret <= -0.04) & (v <= 1.2 * vavg) & (c > sma50),
        "E3 RSI2<10 en tendance": (rsi2 < 10) & (c > sma200),
        "E4 plus haut 52 semaines": (c > hi252) & (v >= 1.5 * vavg),
    }


def build_events(cache, rdata, names):
    """Barre d'ENTREE (indice 1h) par evenement, pour E0..E4."""
    out = {}
    old = B.TICKERS
    B.TICKERS, B.VIX_THRESHOLD = list(names), 22.0
    try:
        sig = B.build_signals(T.make_data(cache, rdata, names))
    finally:
        B.TICKERS, B.VIX_THRESHOLD = old, 22.0
    sig = sig[sig["breakout"]].copy()
    sig["day"] = sig["ts"].dt.normalize()
    sig = sig.drop_duplicates(["ticker", "day"])
    out["E0 breakout 1h (bot)"] = [(r.ticker, int(r.bar)) for r in sig.itertuples()]
    for tk in names:
        h, d = cache["h1"][tk], cache["d1"][tk]
        codes, firsts, uniq = day_first_bar(h)
        day_of = pd.DatetimeIndex(uniq).tz_localize(None)
        # jour de bourse suivant dans les donnees 1h
        for lab, flag in daily_events(d).items():
            dates = flag[flag.fillna(False)].index
            dates = dates[dates >= day_of[0]]   # evenements couverts par les donnees 1h
            pos = np.searchsorted(day_of.values, dates.values, side="right")  # 1er jour 1h > date
            ok = pos < len(firsts)
            out.setdefault(lab, []).extend((tk, int(firsts[p])) for p in pos[ok])
    return out


def trade_ret(arr, j):
    o, h, l, c, dayidx, lod = arr
    return RF.week_trade(o, h, l, c, dayidx, lod, j)


def main():
    with open(T.CACHE, "rb") as f:
        cache = pickle.load(f)
    rdata = RD.load()
    scr = T.screen(cache["d1"])
    names = sorted((set(scr.ticker) | set(T.CURRENT) | set(T.EXTRA)) & set(cache["h1"]))
    arrays = {}
    for tk in names:
        h = cache["h1"][tk]
        dayidx, _ = pd.factorize(h.index.normalize())
        lod = np.r_[dayidx[1:] != dayidx[:-1], True]
        firsts = np.where(np.r_[True, dayidx[1:] != dayidx[:-1]])[0]
        slot = np.arange(len(h)) - firsts[dayidx]   # rang de la barre dans sa seance
        arrays[tk] = ((h["Open"].values.astype(float), h["High"].values.astype(float),
                       h["Low"].values.astype(float), h["Close"].values.astype(float), dayidx, lod),
                      slot, h.index)
    events = build_events(cache, rdata, names)
    rng = np.random.default_rng(95)

    print("=" * 118)
    print("AVANTAGE DE L'ENTREE CONTRE LE HASARD — meme titre, meme periode (+-30 seances hors +-5), meme sortie (SL-10/TP+25/5 seances), %d tickers"
          % len(names))
    print("=" * 118)
    print("%-32s | %-40s | %-40s | %s" % ("entree", "TRAIN: n, gain moyen, avantage [IC95]",
                                          "TEST 18 mois: n, gain, avantage [IC95]", "verdict"))
    for lab, evs in events.items():
        rows = []
        for tk, j in evs:
            arr, slot, idx = arrays[tk]
            if j >= len(idx) - 1:
                continue
            y = trade_ret(arr, j)
            if np.isnan(y):
                continue
            d0 = arr[4][j]
            # temoin: meme titre, meme heure, +-30 seances hors voisinage de l'evenement
            lo = np.searchsorted(arr[4], d0 - MATCH_SESSIONS, side="left")
            hi = np.searchsorted(arr[4], d0 + MATCH_SESSIONS, side="right")
            cand = np.arange(lo, min(hi, len(idx) - 1))
            cand = cand[(slot[cand] == slot[j]) & (np.abs(arr[4][cand] - d0) > EXCLUDE_SESSIONS)]
            if len(cand) < 4:
                continue
            ys = [trade_ret(arr, int(k)) for k in rng.choice(cand, size=K_RANDOM, replace=True)]
            ys = [v for v in ys if not np.isnan(v)]
            if not ys:
                continue
            rows.append({"ts": idx[j], "y": y, "x": y - float(np.mean(ys)),
                         "week": idx[j].to_period("W").start_time})
        df = pd.DataFrame(rows)
        cells = []
        verdict_ok = []
        for w0, w1 in (T.TRAIN, T.TEST):
            m = df[(df.ts >= w0) & (df.ts <= w1)]
            if len(m) < 30:
                cells.append("n=%d (trop peu)" % len(m))
                verdict_ok.append(None)
                continue
            g = [grp["x"].values for _, grp in m.groupby("week")]
            bs = []
            for _ in range(2000):
                pick = rng.integers(0, len(g), len(g))
                bs.append(np.concatenate([g[p] for p in pick]).mean())
            lo_, hi_ = np.percentile(bs, [2.5, 97.5])
            cells.append("n=%4d %+5.2f%% | %+5.2f%% [%+5.2f,%+5.2f]" % (len(m), 100 * m.y.mean(), 100 * m.x.mean(),
                                                                        100 * lo_, 100 * hi_))
            verdict_ok.append((m.x.mean(), lo_, hi_))
        tr_, te_ = verdict_ok
        if tr_ is None or te_ is None:
            verdict = "donnees insuffisantes"
        elif tr_[1] > 0 and te_[1] > 0:
            verdict = "ROBUSTE: bat le hasard sur les deux periodes"
        elif tr_[1] > 0 and te_[0] > 0:
            verdict = "retenu (train significatif, test positif)"
        elif te_[1] > 0:
            verdict = "test seul significatif"
        else:
            verdict = "ne bat pas le hasard"
        print("%-32s | %-40s | %-40s | %s" % (lab, cells[0], cells[1], verdict), flush=True)
    print("\nAvantage = gain du trade - gain moyen de %d entrees au hasard (meme titre, +-%d seances, meme sortie)."
          % (K_RANDOM, MATCH_SESSIONS))
    print("Pour qu'un portefeuille de ~170 trades batte le hasard a 95%%, il faut un avantage d'environ +1.2%% par trade.")


if __name__ == "__main__":
    main()
