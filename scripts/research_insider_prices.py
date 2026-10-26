# -*- coding: utf-8 -*-
"""
Prix journaliers Yahoo des titres achetes par des dirigeants (etude
docs/reports/recherche-dirigeants-2026-10-26.md), plus SPY.

Pre-filtre large avant telechargement (la regle exacte, cloture > 5 $ et volume 20 j
> 5 M$, est appliquee par research_insider_study.py): depot de dirigeant ou
d'administrateur >= 25 k$, ticker valide, prix de transaction >= 3 $, depot depuis 2012.

Limite de debit Yahoo: elle ne renvoie pas toujours une erreur 429. Souvent les
tableaux reviennent vides avec « possibly delisted », et yfinance garde la reponse en
cache memoire (lru_cache de YfData.cache_get), donc un nouvel essai dans le meme
processus echoue pareil. Regles:
- apres chaque lot, si un ticker revient vide, on interroge un temoin (SPY). Si le
  temoin est vide ou si une erreur parle de debit (429, Too Many Requests, rate limit),
  tout le lot vide est remis dans la file, avec une attente croissante;
- sinon chaque vide est re-essaye SEUL (cache vide), exceptions visibles. Il n'est
  « absent » que si Yahoo repond 404 / sans fuseau / sans prix, ET si le temoin
  repond juste apres;
- --max-minutes: le script rend la main a temps (a relancer en boucle, il reprend).

Sortie: scripts/research_insider_prices.pkl (cache local, ignore par git):
  px: ticker -> DataFrame (Open, High, Low, Close, Volume ajustes des splits, Stock Splits)
  missing: ticker -> raison (absence confirmee)
  old_missing: absents de la 1re version (non fiables), pour compter les faux absents
  failed: ticker -> nb d'erreurs reseau non classees

Usage: .venv/Scripts/python.exe -I scripts/research_insider_prices.py [--max-minutes 8]
"""
from __future__ import annotations

import argparse
import os
import re
import time

import pandas as pd
import yfinance as yf
from yfinance import data as yf_data
from yfinance import shared as yf_shared

HERE = os.path.dirname(os.path.abspath(__file__))
FILINGS = os.path.join(HERE, "research_insider_filings.pkl")
OUT = os.path.join(HERE, "research_insider_prices.pkl")
START = "2011-10-01"
LAST_DAY = pd.Timestamp("2026-10-23")
FETCH_END = (pd.Timestamp.now().normalize() + pd.Timedelta(days=2)).strftime("%Y-%m-%d")  # futur: pas de cache
FIELDS = ["Open", "High", "Low", "Close", "Volume", "Stock Splits"]
THROTTLE = re.compile(r"rate.?limit|too many requests|\b429\b", re.I)
ABSENT = re.compile(r"404|no timezone|no price data|delisted|not found|YFTzMissing|YFPricesMissing", re.I)
CANARY = "SPY"

yf.config.debug.hide_exceptions = False


def yahoo_symbol(sym: str) -> str:
    return sym.replace(".", "-")


def clear_cache() -> None:
    yf_data.YfData.cache_get.cache_clear()


def clean(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df = df.set_axis(idx.normalize())
    df = df[[c for c in FIELDS if c in df.columns]].dropna(subset=["Open", "Close"])
    return df[df.index <= LAST_DAY].astype("float32")


def history(tk: str):
    """Essai seul. Retourne (df, erreur)."""
    clear_cache()
    try:
        df = yf.Ticker(tk).history(start=START, end=FETCH_END, interval="1d", auto_adjust=False,
                                   actions=True)
        return clean(df), ""
    except Exception as exc:
        return pd.DataFrame(), f"{type(exc).__name__}: {exc}"


def canary_ok() -> bool:
    clear_cache()
    try:
        return not yf.Ticker(CANARY).history(period="5d").empty
    except Exception:
        return False


def save(store: dict) -> None:
    pd.to_pickle(store, OUT + ".tmp")
    os.replace(OUT + ".tmp", OUT)


def load_store() -> dict:
    store = pd.read_pickle(OUT) if os.path.exists(OUT) else {}
    store.setdefault("px", {})
    old = store.get("missing", {})
    if isinstance(old, set):  # 1re version: absents non verifies (faux absents par limite de debit)
        store["old_missing"] = set(old)
        store["missing"] = {}
    store.setdefault("missing", {})
    store.setdefault("old_missing", set())
    store.setdefault("failed", {})
    return store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=8.0)
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()
    t0 = time.monotonic()
    budget = args.max_minutes * 60.0

    def left() -> float:
        return budget - (time.monotonic() - t0)

    d = pd.read_pickle(FILINGS)
    od = d[d.is_od & d.valid_symbol & (d.value >= 25_000) & (d.price >= 3) & (d.filing_date >= "2012-01-01")]
    wanted = {yahoo_symbol(s) for s in od.symbol} | {CANARY}
    store = load_store()
    done = set(store["px"]) | set(store["missing"])
    gave_up = {t for t, n in store["failed"].items() if n >= 5}
    queue = sorted((wanted & store["old_missing"]) - done - gave_up) + \
        sorted(wanted - store["old_missing"] - done - gave_up)
    backoff = 0.0
    print(f"{len(wanted)} tickers voulus: {len(store['px'])} avec prix, {len(store['missing'])} absents confirmes, "
          f"{len(queue)} dans la file (dont {len(set(queue) & store['old_missing'])} anciens absents a reverifier)",
          flush=True)

    while queue and left() > 60:
        if backoff:
            wait = min(backoff, left() - 45)
            if wait <= 0:
                break
            print(f"  limite de debit: pause {wait:.0f} s", flush=True)
            time.sleep(wait)
            if not canary_ok():
                backoff = min(backoff * 2, 480.0)
                continue
            backoff = 0.0
        chunk, queue = queue[:args.batch], queue[args.batch:]
        clear_cache()
        yf_shared._ERRORS.clear()
        try:
            raw = yf.download(chunk, start=START, end=FETCH_END, interval="1d", auto_adjust=False, actions=True,
                              group_by="ticker", threads=args.threads, progress=False)
        except Exception as exc:
            print(f"  lot: {type(exc).__name__} {exc}", flush=True)
            raw = pd.DataFrame()
        errors = dict(yf_shared._ERRORS)
        empty = []
        for tk in chunk:
            try:
                df = clean(raw[tk] if isinstance(raw.columns, pd.MultiIndex) else raw)
            except (KeyError, AttributeError):
                df = pd.DataFrame()
            if df.empty:
                empty.append(tk)
            else:
                store["px"][tk] = df
        throttled = any(THROTTLE.search(str(errors.get(tk, ""))) for tk in empty)
        if empty and not throttled and not canary_ok():
            throttled = True
        if throttled:
            queue += empty
            backoff = max(60.0, backoff * 2)
        else:
            maybe_absent = {}
            for i, tk in enumerate(empty):
                df, err = history(tk)
                time.sleep(0.5)
                if not df.empty:
                    store["px"][tk] = df
                elif THROTTLE.search(err):
                    queue += empty[i:]
                    backoff = max(60.0, backoff * 2)
                    break
                elif ABSENT.search(err) or not err:
                    maybe_absent[tk] = err or "vide sans erreur"
                else:
                    store["failed"][tk] = store["failed"].get(tk, 0) + 1
                    if store["failed"][tk] < 5:
                        queue.append(tk)
            if maybe_absent:
                if canary_ok():
                    store["missing"].update(maybe_absent)
                else:
                    queue += list(maybe_absent)
                    backoff = max(60.0, backoff * 2)
        save(store)
        old = store["old_missing"]
        print(f"  {len(store['px'])} avec prix, {len(store['missing'])} absents confirmes, {len(queue)} en file | "
              f"anciens absents: {len(old & set(store['px']))} faux, {len(old & set(store['missing']))} confirmes "
              f"| {(time.monotonic() - t0) / 60:.1f} min", flush=True)
        time.sleep(3)

    save(store)
    state = "FINI" if not queue else f"A RELANCER ({len(queue)} en file)"
    print(f"{state}: {len(store['px'])} avec prix, {len(store['missing'])} absents confirmes, "
          f"{sum(1 for n in store['failed'].values() if n >= 5)} abandonnes apres 5 erreurs", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
