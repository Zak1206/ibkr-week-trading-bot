# -*- coding: utf-8 -*-
"""
Quels signaux, en plus du breakout, ameliorent la prediction ?

Protocole (pour ne pas fabriquer un gagnant):
  - base: signaux breakout du bot (config VIX 22), sur ~47 tickers volatils
    (pool mecanique + univers actuel + candidats d'aout), dedoublonnes au
    1er signal par ticker et par seance.
  - pour chaque signal: 15 caracteristiques connues AU MOMENT du signal
    (cloture de la barre 1h du signal; donnees journalieres de la VEILLE).
  - cible: rendement du trade "semaine" pris seul (entree ouverture suivante,
    SL -10 / TP +25 / sortie apres 5 seances, gaps respectes) + rendement
    jusqu'a la cloture du jour.
  - le SENS attendu de chaque caracteristique est declare AVANT le test
    (litterature). Une caracteristique n'est retenue que si:
        TRAIN: correlation de rang dans le sens attendu, IC95% hors de 0
        TEST : meme sens (18 derniers mois)
    IC par bootstrap en grappes par SEMAINE (les trades se chevauchent).
  - "intra-semaine" = rendement moins la moyenne des signaux de la meme
    semaine: mesure la capacite a CHOISIR entre candidats simultanes (utile
    pour classer quand 2 slots seulement), sans l'effet de marche.

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_features.py
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
import research_tickers as T  # noqa: E402

NY = "America/New_York"
SL, TP, CAP = 10.0, 25.0, 5

# nom -> (sens attendu, justification)
HYP = {
    "rvol":       (+1, "volume relatif du jour (stocks in play, Zarattini 2024)"),
    "dayret":     (+1, "rendement depuis l'ouverture (momentum intraday)"),
    "gap":        (+1, "gap d'ouverture (gap-and-go, news)"),
    "brk_atr":    (+1, "force de la cassure en ATR 1h"),
    "ret5":       (-1, "rendement 5 j (retournement court terme, Jegadeesh 1990)"),
    "ret20":      (-1, "rendement 20 j (retournement 1 mois)"),
    "ret60":      (+1, "rendement 60 j (momentum 3 mois)"),
    "dist52":     (+1, "distance au plus haut 52 sem. (George & Hwang 2004)"),
    "squeeze":    (-1, "largeur Bollinger / sa mediane 120 j (compression)"),
    "atr_ratio":  (-1, "ATR 5 j / ATR 20 j (contraction de volatilite)"),
    "trend_al":   (+1, "close > SMA20 > SMA50 (tendance alignee)"),
    "spy_day":    (+1, "SPY depuis l'ouverture (vent de marche)"),
    "spy_trend":  (+1, "SPY > SMA50 la veille"),
    "vix":        (-1, "VIX de la veille"),
    "rsi1h":      (-1, "RSI 14 en 1h (surachat)"),
}


def prev_day(bar_dates: np.ndarray, s: pd.Series) -> np.ndarray:
    s = s.dropna()
    pos = np.searchsorted(s.index.values, bar_dates, side="left") - 1
    out = np.full(len(bar_dates), np.nan)
    ok = pos >= 0
    out[ok] = s.values[pos[ok]]
    return out


def daily_features(d: pd.DataFrame) -> pd.DataFrame:
    c, h, l = d["Close"].astype(float), d["High"].astype(float), d["Low"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    sma20, sma50 = c.rolling(20).mean(), c.rolling(50).mean()
    bbw = c.rolling(20).std(ddof=0) * 4 / sma20
    return pd.DataFrame({
        "ret5": c / c.shift(5) - 1, "ret20": c / c.shift(20) - 1, "ret60": c / c.shift(60) - 1,
        "dist52": c / h.rolling(252, min_periods=120).max() - 1,
        "squeeze": bbw / bbw.rolling(120, min_periods=60).median(),
        "atr_ratio": tr.rolling(5).mean() / tr.rolling(20).mean(),
        "trend_al": ((c > sma20) & (sma20 > sma50)).astype(float).where(sma50.notna()),
    }, index=d.index)


def week_trade(o, h, l, c, dayidx, lod, j):
    entry = o[j]
    slp, tpp, d0 = entry * (1 - SL / 100), entry * (1 + TP / 100), dayidx[j]
    for k in range(j, len(o)):
        if l[k] <= slp:
            return min(slp, o[k]) / entry - 1
        if h[k] >= tpp:
            return max(tpp, o[k]) / entry - 1
        if lod[k] and dayidx[k] - d0 >= CAP:
            return c[k] / entry - 1
    return np.nan


def build(cache, rdata, names):
    old = B.TICKERS
    B.TICKERS, B.VIX_THRESHOLD = list(names), 22.0
    try:
        sig = B.build_signals(T.make_data(cache, rdata, names))
    finally:
        B.TICKERS, B.VIX_THRESHOLD = old, 22.0
    sig = sig[sig["breakout"]].copy()
    sig["day"] = sig["ts"].dt.normalize()
    sig = sig.drop_duplicates(["ticker", "day"], keep="first").reset_index(drop=True)

    spy_h = rdata["h1"]["SPY"]
    spy_open = spy_h.groupby(spy_h.index.normalize())["Open"].transform("first")
    spy_intraday = (spy_h["Close"] / spy_open - 1)
    spy_d = rdata["d1raw"]["SPY"]["Close"].astype(float)
    spy_tr = (spy_d > spy_d.rolling(50).mean()).astype(float)
    vix = rdata["d1"]["^VIX"]["Close"].astype(float)

    rows = []
    for tk, g in sig.groupby("ticker"):
        hdf = cache["h1"][tk]
        o, hh, ll, cc = (hdf[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
        vol = hdf["Volume"].values.astype(float)
        days = hdf.index.normalize()
        dayidx, _ = pd.factorize(days)
        lod = np.r_[dayidx[1:] != dayidx[:-1], True]
        slot = pd.Series(1, index=hdf.index).groupby(days).cumsum().values - 1
        cumv = pd.Series(vol).groupby(dayidx).cumsum().values
        piv = pd.DataFrame({"d": dayidx, "s": slot, "v": cumv}).pivot(index="d", columns="s", values="v")
        avg = piv.rolling(20, min_periods=10).mean().shift(1)  # seances PRECEDENTES
        day_open = pd.Series(o).groupby(dayidx).transform("first").values
        prev_close = pd.Series(cc).groupby(dayidx).last().shift(1)  # cloture de la veille
        ind = B.add_indicators(hdf)
        atr = ind["ATR_14"].values
        rsi = ind["RSI_14"].values
        rng_hi = hdf["High"].rolling(B.BREAKOUT_LOOKBACK).max().shift(1).values

        dfe = daily_features(cache["d1"][tk])
        bar_dates = hdf.index.normalize().tz_localize(None).values
        dcols = {k: prev_day(bar_dates, dfe[k]) for k in dfe.columns}
        spy_tr_v = prev_day(bar_dates, spy_tr)
        vix_v = prev_day(bar_dates, vix)

        for r in g.itertuples():
            j = r.bar
            i = j - 1
            if j >= len(o):
                continue
            a = avg.iloc[dayidx[i]].get(slot[i], np.nan) if dayidx[i] < len(avg) else np.nan
            pcv = prev_close.iloc[dayidx[i]] if dayidx[i] < len(prev_close) else np.nan
            ts_i = hdf.index[i]
            yw = week_trade(o, hh, ll, cc, dayidx, lod, j)
            last_today = j + int(np.argmax(lod[j:]))
            rec = {
                "ticker": tk, "ts": r.ts, "week": r.ts.to_period("W").start_time,
                "y_week": yw, "y_eod": cc[last_today] / o[j] - 1,
                "rvol": cumv[i] / a if a and a > 0 else np.nan,
                "dayret": cc[i] / day_open[i] - 1,
                "gap": day_open[i] / pcv - 1 if pcv and pcv > 0 else np.nan,
                "brk_atr": (cc[i] - rng_hi[i]) / atr[i] if atr[i] > 0 else np.nan,
                "spy_day": float(spy_intraday.get(ts_i, np.nan)),
                "spy_trend": spy_tr_v[i], "vix": vix_v[i], "rsi1h": rsi[i],
            }
            for k in dcols:
                rec[k] = dcols[k][i]
            rows.append(rec)
    ev = pd.DataFrame(rows)
    ev["y_week_x"] = ev["y_week"] - ev.groupby("week")["y_week"].transform("mean")
    return ev


def rho_ci(df, f, y, n=1000, seed=0):
    m = df[[f, y, "week"]].dropna()
    if len(m) < 40:
        return np.nan, np.nan, np.nan, len(m)
    rf, ry = m[f].rank().values, m[y].rank().values
    rho = np.corrcoef(rf, ry)[0, 1]
    rng = np.random.default_rng(seed)
    weeks = m["week"].values
    uw, inv = np.unique(weeks, return_inverse=True)
    groups = [np.where(inv == k)[0] for k in range(len(uw))]
    bs = []
    for _ in range(n):
        idx = np.concatenate([groups[k] for k in rng.integers(0, len(uw), len(uw))])
        bs.append(np.corrcoef(m[f].values[idx].argsort().argsort(), m[y].values[idx].argsort().argsort())[0, 1])
    bs = np.sort(bs)
    return rho, bs[int(0.025 * n)], bs[int(0.975 * n)], len(m)


def main():
    with open(T.CACHE, "rb") as f:
        cache = pickle.load(f)
    rdata = RD.load()
    scr = T.screen(cache["d1"])
    names = sorted((set(scr.ticker) | set(T.CURRENT) | set(T.EXTRA)) & set(cache["h1"]))
    ev = build(cache, rdata, names)
    ev = ev.dropna(subset=["y_week"])
    tr = ev[(ev.ts >= T.TRAIN[0]) & (ev.ts <= T.TRAIN[1])]
    te = ev[(ev.ts >= T.TEST[0]) & (ev.ts <= T.TEST[1])]
    ev.to_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "research_features_events.csv"), index=False)

    print("=" * 132)
    print("SIGNAUX CANDIDATS EN PLUS DU BREAKOUT — %d tickers, %d breakouts (1er/ticker/seance) | TRAIN n=%d | TEST n=%d"
          % (len(names), len(ev), len(tr), len(te)))
    print("=" * 132)
    print("Rendement moyen d'un trade semaine seul: TRAIN %+.2f%%  TEST %+.2f%%  (avant frais ~0.17%%)"
          % (100 * tr.y_week.mean(), 100 * te.y_week.mean()))
    for target, lab in (("y_week", "trade semaine (SL-10/TP+25/5 seances)"),
                        ("y_week_x", "trade semaine INTRA-SEMAINE (choisir entre candidats)"),
                        ("y_eod", "jusqu'a la cloture du jour")):
        print("\n--- cible: %s ---" % lab)
        print("%-10s %4s | %-26s | %-26s | %s" % ("signal", "sens", "TRAIN rho [IC95]", "TEST rho [IC95]", "verdict"))
        for f, (sgn, why) in HYP.items():
            r1, a1, b1, n1 = rho_ci(tr, f, target)
            r2, a2, b2, n2 = rho_ci(te, f, target, seed=1)
            if np.isnan(r1) or np.isnan(r2):
                verdict = "donnees insuffisantes"
            else:
                train_ok = (a1 > 0) if sgn > 0 else (b1 < 0)
                test_same = np.sign(r2) == sgn
                test_sig = ((a2 > 0) if sgn > 0 else (b2 < 0))
                if train_ok and test_sig:
                    verdict = "ROBUSTE (train+test significatifs)"
                elif train_ok and test_same:
                    verdict = "retenu (train sig., test meme sens)"
                elif train_ok:
                    verdict = "rejete: s'inverse en test"
                elif test_sig:
                    verdict = "test seul (non retenu)"
                else:
                    verdict = "-"
            print("%-10s %4s | %+.3f [%+.3f,%+.3f] n=%4d | %+.3f [%+.3f,%+.3f] n=%4d | %s"
                  % (f, "+" if sgn > 0 else "-", r1, a1, b1, n1, r2, a2, b2, n2, verdict))
    print("\nSens et justification:")
    for f, (sgn, why) in HYP.items():
        print("  %-10s %s  %s" % (f, "+" if sgn > 0 else "-", why))


if __name__ == "__main__":
    main()
