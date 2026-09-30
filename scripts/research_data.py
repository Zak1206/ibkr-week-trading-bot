# -*- coding: utf-8 -*-
"""
Donnees supplementaires pour la recherche (cache separe de .backtest_cache.pkl,
qui reste la reference de la config live).

  h1 : ETF liquides en 1h, meme profondeur que l'univers du bot (~3 ans)
  d1 : journalier sur l'historique complet (ETF + univers du bot), pour tester
       les strategies journalieres sur plusieurs regimes (2008, 2020, 2022).

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_data.py [--refresh]
"""
from __future__ import annotations

import argparse
import os
import pickle

import pandas as pd
import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "scripts", ".research_cache.pkl")

BOT = ["PLTR", "SMCI", "IONQ", "HOOD", "SOFI", "COIN", "RIVN", "RKLB", "CVS"]
ETF = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY",
       "XLP", "XLU", "XLB", "SMH", "TLT", "GLD"]


def _flat(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(1, axis=1)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    return df.dropna(subset=["Close"])


def load(refresh: bool = False) -> dict:
    if os.path.exists(CACHE) and not refresh:
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    out = {"h1": {}, "d1": {}}
    for tk in ETF:
        h = _flat(yf.download(tk, period="730d", interval="1h",
                              progress=False, auto_adjust=False))
        h.index = (h.index.tz_localize("America/New_York") if h.index.tz is None
                   else h.index.tz_convert("America/New_York"))
        out["h1"][tk] = h
    for tk in ETF + BOT + ["^VIX"]:
        # auto_adjust=True: dividendes reinvestis dans l'historique long,
        # sinon un ETF a 3% de rendement parait perdre 3%/an.
        d = _flat(yf.download(tk, period="max", interval="1d",
                              progress=False, auto_adjust=True))
        if d.index.tz is not None:
            d.index = d.index.tz_localize(None)
        out["d1"][tk] = d
    # Journalier NON ajuste, meme echelle de prix que les barres 1h: sert a
    # calculer SMA/RSI avec le prix intraday du jour comme dernier point.
    out["d1raw"] = {}
    for tk in ETF:
        d = _flat(yf.download(tk, period="5y", interval="1d",
                              progress=False, auto_adjust=False))
        if d.index.tz is not None:
            d.index = d.index.tz_localize(None)
        out["d1raw"][tk] = d
    with open(CACHE, "wb") as f:
        pickle.dump(out, f)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    d = load(refresh=a.refresh)
    for k in ("h1", "d1", "d1raw"):
        for tk, df in d[k].items():
            print("%s %-5s %6d  %s -> %s" % (k, tk, len(df), df.index[0], df.index[-1]))
