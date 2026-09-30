# -*- coding: utf-8 -*-
"""
Collecte des sources de donnees candidates (cache: scripts/.sources_cache.pkl).

  FINRA  : volume quotidien de ventes a decouvert (Reg SHO, gratuit, depuis 2009)
           https://cdn.finra.org/equity/regsho/daily/CNMSshvolAAAAMMJJ.txt
  IBKR   : volatilite implicite (OPTION_IMPLIED_VOLATILITY) et historique
           (HISTORICAL_VOLATILITY) quotidiennes sur 3 ans + actions d'analystes
           Briefing.com (BRFUPDN) via reqHistoricalNews. Connexion LECTURE SEULE,
           client ID 77 (le bot utilise 8). Necessite l'IB Gateway ouvert.
  yfinance: BTC-USD en 1h (730 j) et en journalier.

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_sources_data.py [--skip-finra] [--skip-ibkr]
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import pickle
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import research_tickers as T  # noqa: E402

CACHE = os.path.join(HERE, ".sources_cache.pkl")
START, END = dt.date(2023, 9, 1), dt.date(2026, 9, 28)


def tickers():
    with open(T.CACHE, "rb") as f:
        cache = pickle.load(f)
    return sorted(set(cache["h1"]) | {"SPY", "QQQ"})


def finra(names):
    days = pd.bdate_range(START, END)
    keep = set(names)

    def one(d):
        url = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol%s.txt" % d.strftime("%Y%m%d")
        try:
            r = requests.get(url, timeout=30)
            if r.status_code != 200:
                return None
            df = pd.read_csv(io.StringIO(r.text), sep="|")
            df = df[df["Symbol"].isin(keep)]
            return df[["Date", "Symbol", "ShortVolume", "TotalVolume"]]
        except Exception:
            return None

    with ThreadPoolExecutor(8) as ex:
        parts = [p for p in ex.map(one, days) if p is not None and len(p)]
    out = pd.concat(parts)
    out["Date"] = pd.to_datetime(out["Date"].astype(str), format="%Y%m%d")
    print("FINRA: %d jours, %d lignes" % (out["Date"].nunique(), len(out)))
    return out


def ibkr(names):
    from ib_insync import IB, Stock, util
    ib = IB()
    ib.connect("127.0.0.1", 4002, clientId=77, readonly=True, timeout=20)
    iv, hv, news = {}, {}, {}
    for tk in names:
        c = Stock(tk, "SMART", "USD")
        try:
            ib.qualifyContracts(c)
        except Exception:
            continue
        for what, dest in (("OPTION_IMPLIED_VOLATILITY", iv), ("HISTORICAL_VOLATILITY", hv)):
            try:
                bars = ib.reqHistoricalData(c, endDateTime="", durationStr="3 Y", barSizeSetting="1 day",
                                            whatToShow=what, useRTH=True, formatDate=1)
                df = util.df(bars)
                if df is not None and len(df):
                    dest[tk] = df.set_index(pd.to_datetime(df["date"]))["close"].astype(float)
            except Exception as e:  # noqa: BLE001
                print("  %s %s: %s" % (tk, what, e))
            ib.sleep(2.0)   # rythme des requetes historiques IBKR
        rows, end = [], ""
        for _ in range(6):
            try:
                h = ib.reqHistoricalNews(c.conId, "BRFUPDN", "", end, 300)
            except Exception:
                break
            if not h:
                break
            rows += [(x.time, x.headline) for x in h]
            if len(h) < 300:
                break
            end = (min(x.time for x in h) - dt.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            ib.sleep(1.0)
        news[tk] = rows
        print("  IBKR %-5s IV %4d  HV %4d  analystes %3d" % (tk, len(iv.get(tk, [])), len(hv.get(tk, [])), len(rows)),
              flush=True)
    ib.disconnect()
    return iv, hv, news


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-finra", action="store_true")
    ap.add_argument("--skip-ibkr", action="store_true")
    a = ap.parse_args()
    names = tickers()
    out = {}
    if os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            out = pickle.load(f)
    if not a.skip_ibkr:
        out["iv"], out["hv"], out["analyst"] = ibkr(names)
    btc = yf.download("BTC-USD", period="730d", interval="1h", progress=False, auto_adjust=False)
    if isinstance(btc.columns, pd.MultiIndex):
        btc = btc.droplevel(1, axis=1)
    out["btc_h1"] = btc.tz_convert("America/New_York") if btc.index.tz is not None else btc
    if not a.skip_finra:
        out["finra"] = finra(names)
    with open(CACHE, "wb") as f:
        pickle.dump(out, f)
    print("cache:", CACHE, {k: (len(v) if hasattr(v, "__len__") else "") for k, v in out.items()})


if __name__ == "__main__":
    main()
