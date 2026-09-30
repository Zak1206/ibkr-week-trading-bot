# -*- coding: utf-8 -*-
"""
Les nouvelles sources ameliorent-elles la prediction du trade "breakout semaine" ?
Meme protocole que research_features.py (sens attendu declare AVANT, TRAIN puis
TEST, IC bootstrap par semaine), sur les breakouts de ~42 tickers volatils.

Sources (scripts/research_sources_data.py):
  IBKR  iv_chg5  variation 5 j de la vol implicite            sens -  (hausse de la peur)
        iv_hv    vol implicite - vol realisee                   sens -  (Bali & Hovakimian 2009)
        iv_rank  rang de la vol implicite sur 1 an              sens -
  FINRA sv_ratio  part du volume vendu a decouvert, veille      sens -  (Boehmer, Jones & Zhang 2008)
        sv_ratio5 moyenne 5 j                                   sens -
        sv_z      ecart a sa norme 60 j                         sens -
  IBKR  up_recent  relevement de recommandation <= 5 j          sens +  (Womack 1996)
  (Briefing.com)   down_recent abaissement <= 10 j              sens -
  BTC   btc_24h  BTC sur 24 h (titres lies a la crypto)         sens +
        btc_5d   BTC sur 5 j                                    sens +
Toutes les valeurs sont celles de la VEILLE du signal (ou anterieures a l'heure du
signal pour les news et le BTC). Les actions d'analystes ne remontent qu'a nov. 2024:
pour elles, TRAIN/TEST = deux moities de leur propre periode.

En plus: le relevement d'analyste comme ENTREE a part entiere, contre le hasard
(meme titre, meme heure, +-30 seances hors +-5, sortie semaine).

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_sources.py
"""
from __future__ import annotations

import os
import pickle
import re
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import research_data as RD  # noqa: E402
import research_features as RF  # noqa: E402
import research_tickers as T  # noqa: E402

NY = "America/New_York"
CRYPTO = {"COIN", "HOOD", "MARA", "CLSK", "RIOT", "MSTR", "HUT", "CIFR", "IREN", "WULF", "BITF", "CORZ",
          "BTBT", "HIVE", "SOFI"}
HYP = {"iv_chg5": -1, "iv_hv": -1, "iv_rank": -1, "sv_ratio": -1, "sv_ratio5": -1, "sv_z": -1,
       "up_recent": +1, "down_recent": -1, "btc_24h": +1, "btc_5d": +1}
UP = re.compile(r"\bupgraded\b", re.I)
DOWN = re.compile(r"\bdowngraded\b", re.I)


def prev_val(series: pd.Series, dates: pd.Series) -> np.ndarray:
    s = series.dropna().sort_index()
    idx = s.index.tz_localize(None) if getattr(s.index, "tz", None) is not None else s.index
    pos = np.searchsorted(idx.values, dates.values.astype("datetime64[ns]"), side="left") - 1
    out = np.full(len(dates), np.nan)
    ok = pos >= 0
    out[ok] = s.values[pos[ok]]
    return out


def analyst_events(src):
    rows = []
    for tk, lst in src.get("analyst", {}).items():
        for t, head in lst:
            ts = pd.Timestamp(t)
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
            kind = "up" if UP.search(head) else ("down" if DOWN.search(head) else None)
            if kind:
                rows.append({"ticker": tk, "ts": ts.tz_convert(NY), "kind": kind})
    return pd.DataFrame(rows)


def add_features(ev, src):
    ev = ev.copy()
    ev["sigday"] = ev["ts"].dt.tz_convert(NY).dt.tz_localize(None).dt.normalize()
    fin = src.get("finra")
    for f in HYP:
        ev[f] = np.nan
    an = analyst_events(src)
    btc = src.get("btc_h1")
    btc_c = btc["Close"].astype(float) if btc is not None else None
    for tk, g in ev.groupby("ticker"):
        i = g.index
        d = g["sigday"]
        if tk in src.get("iv", {}):
            iv = src["iv"][tk]
            ivv = prev_val(iv, d)
            iv5 = prev_val(iv.shift(5), d)
            ev.loc[i, "iv_chg5"] = ivv / iv5 - 1
            lo, hi = iv.rolling(252, min_periods=120).min(), iv.rolling(252, min_periods=120).max()
            ev.loc[i, "iv_rank"] = (ivv - prev_val(lo, d)) / (prev_val(hi, d) - prev_val(lo, d))
            if tk in src.get("hv", {}):
                ev.loc[i, "iv_hv"] = ivv - prev_val(src["hv"][tk], d)
        if fin is not None:
            x = fin[fin["Symbol"] == tk].set_index("Date").sort_index()
            if len(x):
                r = x["ShortVolume"] / x["TotalVolume"]
                ev.loc[i, "sv_ratio"] = prev_val(r, d)
                ev.loc[i, "sv_ratio5"] = prev_val(r.rolling(5).mean(), d)
                ev.loc[i, "sv_z"] = prev_val((r - r.rolling(60).mean()) / r.rolling(60).std(), d)
        if len(an):
            a = an[an["ticker"] == tk]
            first = an["ts"].min()
            for k in i:
                ts = ev.at[k, "ts"] - pd.Timedelta(hours=1)   # heure du signal (cloture de la barre)
                if ts < first:
                    continue
                aa = a[a["ts"] <= ts]
                ev.at[k, "up_recent"] = float(((aa["kind"] == "up") & (aa["ts"] >= ts - pd.Timedelta(days=5))).any())
                ev.at[k, "down_recent"] = float(((aa["kind"] == "down") & (aa["ts"] >= ts - pd.Timedelta(days=10))).any())
        if btc_c is not None and tk in CRYPTO:
            for k in i:
                ts = ev.at[k, "ts"] - pd.Timedelta(hours=1)
                b = btc_c[btc_c.index <= ts]
                if len(b) > 130:
                    ev.at[k, "btc_24h"] = b.iloc[-1] / b.iloc[-25] - 1
                    ev.at[k, "btc_5d"] = b.iloc[-1] / b.iloc[-121] - 1
    return ev


def verdict_line(ev, f, sgn, w_tr, w_te, target="y_week"):
    tr = ev[(ev.ts >= w_tr[0]) & (ev.ts <= w_tr[1])]
    te = ev[(ev.ts >= w_te[0]) & (ev.ts <= w_te[1])]
    r1, a1, b1, n1 = RF.rho_ci(tr, f, target)
    r2, a2, b2, n2 = RF.rho_ci(te, f, target, seed=1)
    if np.isnan(r1) or np.isnan(r2):
        v = "donnees insuffisantes"
    else:
        tok = (a1 > 0) if sgn > 0 else (b1 < 0)
        tsig = (a2 > 0) if sgn > 0 else (b2 < 0)
        v = ("ROBUSTE" if tok and tsig else "retenu" if tok and np.sign(r2) == sgn
             else "rejete: s'inverse" if tok else "test seul" if tsig else "-")
    print("%-12s %2s | %+.3f [%+.3f,%+.3f] n=%4s | %+.3f [%+.3f,%+.3f] n=%4s | %s"
          % (f, "+" if sgn > 0 else "-", r1, a1, b1, n1, r2, a2, b2, n2, v))


def upgrade_entry_test(cache, src):
    an = analyst_events(src)
    an = an[an["kind"] == "up"]
    rng = np.random.default_rng(1996)
    rows = []
    for r in an.itertuples():
        if r.ticker not in cache["h1"]:
            continue
        h = cache["h1"][r.ticker]
        # 1re barre qui OUVRE apres la publication (avant 9h30 -> ouverture du jour)
        j = int(np.searchsorted(h.index.values, r.ts.tz_convert(NY).to_datetime64(), side="right"))
        if j >= len(h) - 1:
            continue
        o, hi, lo, c = (h[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
        dayidx, _ = pd.factorize(h.index.normalize())
        lod = np.r_[dayidx[1:] != dayidx[:-1], True]
        firsts = np.where(np.r_[True, dayidx[1:] != dayidx[:-1]])[0]
        slot = np.arange(len(h)) - firsts[dayidx]
        arr = (o, hi, lo, c, dayidx, lod)
        y = RF.week_trade(*arr, j)
        if np.isnan(y):
            continue
        d0 = dayidx[j]
        a = np.searchsorted(dayidx, d0 - 30, "left")
        b = np.searchsorted(dayidx, d0 + 30, "right")
        cand = np.arange(a, min(b, len(h) - 1))
        cand = cand[(slot[cand] == slot[j]) & (np.abs(dayidx[cand] - d0) > 5)]
        if len(cand) < 4:
            continue
        ys = [RF.week_trade(*arr, int(q)) for q in rng.choice(cand, 16, replace=True)]
        ys = [v for v in ys if not np.isnan(v)]
        if ys:
            rows.append({"ts": h.index[j], "y": y, "x": y - np.mean(ys), "week": h.index[j].to_period("W").start_time})
    df = pd.DataFrame(rows).sort_values("ts")
    if df.empty:
        print("aucun relevement exploitable")
        return
    mid = df.ts.iloc[0] + (df.ts.iloc[-1] - df.ts.iloc[0]) / 2
    print("\n--- Relevement d'analyste comme ENTREE (sortie semaine) contre le hasard ---")
    for lab, m in (("1re moitie", df[df.ts < mid]), ("2e moitie", df[df.ts >= mid]), ("total", df)):
        g = [grp["x"].values for _, grp in m.groupby("week")]
        bs = [np.concatenate([g[p] for p in rng.integers(0, len(g), len(g))]).mean() for _ in range(2000)]
        lo_, hi_ = np.percentile(bs, [2.5, 97.5])
        print("  %-10s n=%3d  gain moyen %+5.2f%%  avantage vs hasard %+5.2f%% [%+5.2f, %+5.2f]"
              % (lab, len(m), 100 * m.y.mean(), 100 * m.x.mean(), 100 * lo_, 100 * hi_))


def main():
    with open(T.CACHE, "rb") as f:
        cache = pickle.load(f)
    with open(os.path.join(HERE, ".sources_cache.pkl"), "rb") as f:
        src = pickle.load(f)
    rdata = RD.load()
    scr = T.screen(cache["d1"])
    names = sorted((set(scr.ticker) | set(T.CURRENT) | set(T.EXTRA)) & set(cache["h1"]) & set(src.get("iv", {})))
    ev = RF.build(cache, rdata, names).dropna(subset=["y_week"])
    ev = add_features(ev, src)
    an = analyst_events(src)
    a0 = an["ts"].min() if len(an) else T.TEST[0]
    amid = a0 + (T.TEST[1] - a0) / 2
    print("=" * 112)
    print("NOUVELLES SOURCES — %d breakouts sur %d tickers (trade semaine seul)" % (len(ev), len(names)))
    print("=" * 112)
    print("%-12s %2s | %-28s | %-28s | %s" % ("signal", "", "TRAIN rho [IC95] n", "TEST rho [IC95] n", "verdict"))
    for f, sgn in HYP.items():
        if f in ("up_recent", "down_recent"):
            verdict_line(ev, f, sgn, (a0, amid), (amid, T.TEST[1]))
        else:
            verdict_line(ev, f, sgn, T.TRAIN, T.TEST)
    print("(analystes: TRAIN/TEST = %s -> %s / %s -> %s)" % (a0.date(), amid.date(), amid.date(), T.TEST[1].date()))
    for f in ("up_recent", "down_recent"):
        m = ev[ev[f].notna()]
        if len(m):
            print("  %-11s: %3d breakouts concernes, gain moyen %+.2f%% contre %+.2f%% sans"
                  % (f, int(m[f].sum()), 100 * m[m[f] == 1].y_week.mean(), 100 * m[m[f] == 0].y_week.mean()))
    upgrade_entry_test(cache, src)


if __name__ == "__main__":
    main()
