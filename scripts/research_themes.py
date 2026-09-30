# -*- coding: utf-8 -*-
"""
Changer d'univers ? La strategie "breakout semaine" (1 x 1000$, SL -10 / TP +25 /
5 seances, breakout seul, VIX 22) sur des univers THEMATIQUES definis de facon
neutre: principales lignes d'ETF thematiques (yfinance funds_data.top_holdings),
pas une selection a la main.

Pour chaque theme: TRAIN (2023-10 -> 2025-03), TEST (18 derniers mois), un bot au
hasard dans le meme theme (memes sorties: ce que rapporte le theme sans signal),
et le buy & hold equipondere du theme. Puis la correlation de rang TRAIN -> TEST
entre themes: si elle est faible, choisir un theme sur son backtest est du bruit.

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_themes.py [--refresh]
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys

import numpy as np
import pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import backtest_swing as B  # noqa: E402
import research_data as RD  # noqa: E402
import research_engine as E  # noqa: E402
import research_tickers as T  # noqa: E402

NY = "America/New_York"
THEMES = {
    "cybersecurite": ["CIBR", "HACK", "BUG"],
    "mineurs crypto / blockchain": ["WGMI", "BKCH", "BITQ"],
    "semi-conducteurs": ["SMH", "SOXX"],
    "logiciel / IA": ["IGV", "AIQ"],
    "biotech": ["XBI"],
    "energie propre": ["ICLN", "TAN"],
    "spatial / defense": ["UFO", "ITA"],
    "quantique": ["QTUM"],
    "fintech": ["ARKF", "FINX"],
    "uranium / nucleaire": ["URA", "NLR"],
    "tech chinoise": ["KWEB"],
    "innovation (ARKK)": ["ARKK"],
}
CRYPTO_DIRECT = ["IBIT", "ETHA", "FBTC", "BITB"]
MAX_PER_THEME = 12
THEME_FILE = os.path.join(HERE, "research_themes_members.json")


def theme_members(refresh):
    if os.path.exists(THEME_FILE) and not refresh:
        with open(THEME_FILE, encoding="utf-8") as f:
            return json.load(f)
    out = {}
    for theme, etfs in THEMES.items():
        names = []
        for etf in etfs:
            try:
                th = yf.Ticker(etf).funds_data.top_holdings
                names += [s for s in th.index if isinstance(s, str) and s.replace("-", "").isalpha()]
            except Exception as e:  # noqa: BLE001
                print("  %s: %s" % (etf, e))
        seen = []
        for s in names:
            if s not in seen:
                seen.append(s)
        out[theme] = seen[:MAX_PER_THEME]
    out["crypto en direct (ETF spot)"] = CRYPTO_DIRECT
    with open(THEME_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    return out


def ensure_data(cache, names):
    todo = [t for t in names if t not in cache["h1"]]
    for i in range(0, len(todo), 30):
        chunk = todo[i:i + 30]
        h = yf.download(chunk, period="730d", interval="1h", group_by="ticker", auto_adjust=False,
                        progress=False, threads=True)
        d = yf.download(chunk, period="5y", interval="1d", group_by="ticker", auto_adjust=False,
                        progress=False, threads=True)
        for tk in chunk:
            try:
                hx = h[tk] if isinstance(h.columns, pd.MultiIndex) else h
                dx = d[tk] if isinstance(d.columns, pd.MultiIndex) else d
            except KeyError:
                continue
            hx = hx[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
            dx = dx[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
            if len(hx) < 300 or len(dx) < 120:
                continue
            hx.index = hx.index.tz_localize(NY) if hx.index.tz is None else hx.index.tz_convert(NY)
            dx.index = dx.index.tz_localize(None) if dx.index.tz is not None else dx.index
            cache["h1"][tk], cache["d1"][tk] = hx, dx
    return [t for t in names if t in cache["h1"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    with open(T.CACHE, "rb") as f:
        cache = pickle.load(f)
    rd = RD.load()
    members = theme_members(a.refresh)
    members = {"univers actuel (8)": [t for t in T.CURRENT if t != "CVS"], **members}
    allnames = sorted({t for v in members.values() for t in v})
    ok = set(ensure_data(cache, allnames))
    with open(T.CACHE, "wb") as f:
        pickle.dump(cache, f)

    old = B.TICKERS
    B.TICKERS, B.VIX_THRESHOLD = sorted(ok), 22.0
    try:
        sig = B.build_signals(T.make_data(cache, rd, sorted(ok)))
    finally:
        B.TICKERS, B.VIX_THRESHOLD = old, 22.0
    sig = sig[sig["breakout"]].reset_index(drop=True)
    sig["kind"], sig["prio"] = "open", 0.0
    week = E.Exit("week", sl_pct=10, tp_pct=25, max_hold_tdays=5)
    kw = dict(line_usd=1000, max_open=1, budget=1000)
    full = (T.TRAIN[0], T.TEST[1])

    print("=" * 128)
    print("STRATEGIE BREAKOUT SEMAINE PAR THEME — 1 x 1000$, frais IBKR, mediane de 12 perturbations")
    print("=" * 128)
    print("%-30s %3s | %8s %8s %8s %7s | %9s %8s | %9s %9s" % (
        "theme", "n", "TRAIN $", "TEST $", "$/mois", "DD%", "hasard T", "rang", "B&H TEST", "B&H DD%"))
    rows = []
    rng = np.random.default_rng(12)
    for theme, tks in members.items():
        tks = [t for t in tks if t in ok]
        if len(tks) < 2:
            print("%-30s pas assez de donnees" % theme)
            continue
        bars = E.Bars({t: cache["h1"][t] for t in tks})
        s = sig[sig["ticker"].isin(tks)].reset_index(drop=True)
        if s.empty:
            continue
        tr = E.robust(bars, s, week, reps=12, win=T.TRAIN, **kw)
        te = E.robust(bars, s, week, reps=12, win=T.TEST, **kw)
        fu = E.robust(bars, s, week, reps=12, win=full, **kw)
        months = (full[1] - full[0]).days / 30.44
        # bot au hasard dans le theme, meme sortie, periode TEST
        pn = []
        for k in range(30):
            prow = []
            for tk in tks:
                h = cache["h1"][tk]
                idx = np.where((h.index >= T.TEST[0]) & (h.index <= T.TEST[1]))[0]
                if len(idx) == 0:
                    continue
                for j in rng.choice(idx, size=max(1, len(idx) // 40), replace=False):
                    prow.append({"ts": h.index[j], "ticker": tk, "kind": "open", "prio": 0.0})
            ps = pd.DataFrame(prow).sort_values("ts").reset_index(drop=True)
            pn.append(sum(t["net"] for t in E.simulate(bars, ps, week, seed=k, win=T.TEST, **kw)["trades"]))
        pn = np.array(pn)
        a0, b0 = T.TEST[0].tz_localize(None).normalize(), T.TEST[1].tz_localize(None).normalize()
        px = pd.DataFrame({t: cache["d1"][t]["Close"] for t in tks})
        px = px[(px.index >= a0) & (px.index <= b0)].dropna(axis=1, how="all").ffill().dropna()
        bh_eq = (px / px.iloc[0]).mean(axis=1) * 1000
        bh = float(bh_eq.iloc[-1] - 1000)
        bh_dd = float(((bh_eq - bh_eq.cummax()) / bh_eq.cummax()).min() * 100)
        rows.append((theme, tr["med"], te["med"]))
        print("%-30s %3d | %+8.0f %+8.0f %+8.1f %+7.1f | %+9.0f %7.0f%% | %+9.0f %+8.1f   %s"
              % (theme, len(tks), tr["med"], te["med"], fu["med"] / months, fu["m"]["dd_pct"],
                 np.median(pn), 100 * (pn < te["med"]).mean(), bh, bh_dd, ",".join(tks[:8])), flush=True)
    df = pd.DataFrame(rows, columns=["theme", "train", "test"])
    rho = df["train"].rank().corr(df["test"].rank())
    print("-" * 128)
    print("Correlation de rang TRAIN -> TEST entre themes: rho = %+.2f (n=%d themes)" % (rho, len(df)))
    print("hasard T = mediane de 30 bots au hasard dans le theme (memes sorties) sur TEST; rang = % battus.")


if __name__ == "__main__":
    main()
