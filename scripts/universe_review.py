# -*- coding: utf-8 -*-
"""
Revue mensuelle de l'univers (a lancer ~1 fois par mois). NE MODIFIE RIEN: signale.

Seule raison validee de changer un ticker (rapport 2026-09-29, section 12): il ne
remplit plus la regle mecanique de la strategie "breakout semaine":
    prix 5-600 $, volume median >= 100 M$/jour, et le plus haut depasse l'ouverture
    de +3.5% dans >= 15% des seances (12 derniers mois). En dessous, la sortie a
    +25% en 5 seances est hors d'atteinte (cas de CVS: 3%).
Changer de tickers parce qu'ils ont recemment perdu a ete teste et REFUTE
(rotation sur performance: incoherente; coupe-circuit par ticker: -245$ sur 18 mois).

Usage: .\\.venv\\Scripts\\python.exe scripts\\universe_review.py
"""
from __future__ import annotations

import json
import os

import pandas as pd
import yfinance as yf
from dotenv import dotenv_values

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MIN_PX, MAX_PX, MIN_DVOL, MIN_REACH = 5.0, 600.0, 100e6, 0.15


def candidates():
    names = set()
    for fn in ("research_pool.json", "research_themes_members.json"):
        p = os.path.join(HERE, fn)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            names |= set(data) if isinstance(data, list) else {t for v in data.values() for t in v}
    return sorted(n for n in names if n.replace("-", "").isalpha())


def stats(tickers):
    rows = []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        df = yf.download(chunk, period="1y", interval="1d", group_by="ticker", auto_adjust=False,
                         progress=False, threads=True)
        for tk in chunk:
            try:
                x = (df[tk] if isinstance(df.columns, pd.MultiIndex) else df).dropna(subset=["Close"])
            except KeyError:
                continue
            if len(x) < 200:
                continue
            c = x["Close"]
            rows.append({"ticker": tk, "prix": float(c.iloc[-1]),
                         "vol_musd": float((c * x["Volume"]).median()) / 1e6,
                         "TP_atteignable": float((x["High"] >= x["Open"] * 1.035).mean()),
                         "perf_6m": float(c.iloc[-1] / c.iloc[-126] - 1) if len(c) > 126 else float("nan")})
    df = pd.DataFrame(rows)
    df["regle_ok"] = (df.prix.between(MIN_PX, MAX_PX) & (df["vol_musd"] >= MIN_DVOL / 1e6)
                      & (df.TP_atteignable >= MIN_REACH))
    return df


def main():
    env = {**dotenv_values(os.path.join(ROOT, ".env")), **dotenv_values(os.path.join(ROOT, ".env.ibkr_paper"))}
    current = [t.strip().upper() for t in (env.get("TICKERS_OVERRIDE") or "").split(",") if t.strip()]
    pool = sorted(set(candidates()) | set(current))
    print("Revue d'univers — %d titres analyses (12 derniers mois)" % len(pool))
    df = stats(pool)
    cur = df[df.ticker.isin(current)].sort_values("TP_atteignable", ascending=False)
    print("\n--- Univers actuel (%s) ---" % ",".join(current))
    for r in cur.itertuples():
        flag = "OK" if r.regle_ok else "A REMPLACER (ne remplit plus la regle)"
        print("  %-5s prix %7.2f  %7.0f M$/j  TP atteignable %4.0f%%  6 mois %+6.0f%%  -> %s"
              % (r.ticker, r.prix, r.vol_musd, 100 * r.TP_atteignable, 100 * r.perf_6m, flag))
    missing = set(current) - set(df.ticker)
    if missing:
        print("  donnees absentes: %s" % ",".join(sorted(missing)))
    cand = df[df.regle_ok & ~df.ticker.isin(current)].sort_values("TP_atteignable", ascending=False).head(15)
    print("\n--- Meilleurs remplacants possibles (regle OK, les plus adaptes a la sortie +25%) ---")
    for r in cand.itertuples():
        print("  %-5s prix %7.2f  %7.0f M$/j  TP atteignable %4.0f%%  6 mois %+6.0f%%"
              % (r.ticker, r.prix, r.vol_musd, 100 * r.TP_atteignable, 100 * r.perf_6m))
    n_bad = int((~cur.regle_ok).sum())
    print("\nA faire: %s" % ("rien, l'univers remplit la regle." if n_bad == 0 else
                             "%d ticker(s) a remplacer dans TICKERS_OVERRIDE (.env.ibkr_paper). Evite de "
                             "prendre plusieurs remplacants du meme secteur (ils bougent ensemble)." % n_bad))


if __name__ == "__main__":
    main()
