# -*- coding: utf-8 -*-
"""
Recherche de tickers adaptes a la strategie du bot — SANS se servir du
resultat du backtest pour choisir (sinon on fabrique un gagnant par selection).

Protocole:
  TRAIN = 2023-10-31 -> 2025-03-31   (sert a choisir)
  TEST  = 2025-04-01 -> 2026-09-28   (18 derniers mois, sert a juger)

  1) Pool: S&P 500 + 400 + 600 (composition actuelle) + univers du bot.
  2) Filtre MECANIQUE, calcule sur TRAIN uniquement, derive des contraintes du
     bot (aucun P&L):
       - prix fin de train 5-200$ (actions entieres, ticket 520$)
       - volume median >= 100 M$/jour (spread serre)
       - TP +3.5% atteignable: plus haut >= ouverture +3.5% dans >= 15% des
         seances (sinon la porte de sortie haute est morte, cf. memoire aout)
  3) Trois facons de choisir 9 tickers, toutes avec les donnees TRAIN:
       S1 score mecanique (frequence TP atteignable), diversifie (corr < 0.7)
       S2 meilleur resultat du bot sur TRAIN (ticker seul), diversifie
       S0 univers actuel du bot (reference)
     + 100 tirages de 9 tickers au hasard dans le pool filtre (placebo).
  4) Jugement sur TEST: portefeuille 2 lignes x 520$, config live exacte
     (VIX 26 comme le .env), mediane de 20 perturbations.
  5) Correlation de rang train -> test des resultats par ticker: si rho ~ 0,
     le resultat passe d'un ticker ne predit pas son resultat futur.

Usage:
  .\\.venv\\Scripts\\python.exe scripts\\research_tickers.py download
  .\\.venv\\Scripts\\python.exe scripts\\research_tickers.py eval
"""
from __future__ import annotations

import json
import os
import pickle
import sys

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_swing as B  # noqa: E402
import research_data as RD  # noqa: E402
import research_engine as E  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "scripts", ".tickers_cache.pkl")
POOL_FILE = os.path.join(ROOT, "scripts", "research_pool.json")
NY = "America/New_York"

TRAIN = (pd.Timestamp("2023-10-31 09:30", tz=NY), pd.Timestamp("2025-03-31 16:00", tz=NY))
TEST = (pd.Timestamp("2025-04-01 09:30", tz=NY), pd.Timestamp("2026-09-28 16:00", tz=NY))
CURRENT = ["PLTR", "SMCI", "IONQ", "HOOD", "SOFI", "COIN", "RIVN", "RKLB", "CVS"]
EXTRA = ["RGTI", "UPST", "ASTS", "CHWY", "TOST", "DKNG", "CNC"]

MIN_PX, MAX_PX = 5.0, 200.0
MIN_DVOL = 100e6
MIN_TP_REACH = 0.15
TP_PCT = 3.5
MAX_CORR = 0.70
K = 9


def _split(df: pd.DataFrame, tickers: list) -> dict:
    out = {}
    for tk in tickers:
        try:
            x = df[tk] if isinstance(df.columns, pd.MultiIndex) else df
        except KeyError:
            continue
        x = x[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        if len(x) > 50:
            out[tk] = x
    return out


def download():
    with open(POOL_FILE, encoding="utf-8") as f:
        pool = json.load(f)
    tickers = sorted(set(pool) | set(CURRENT) | set(EXTRA))
    print("pool: %d tickers" % len(tickers))
    cache = {"d1": {}, "h1": {}}
    if os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            cache = pickle.load(f)
    todo = [t for t in tickers if t not in cache["d1"]]
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        df = yf.download(chunk, period="5y", interval="1d", group_by="ticker",
                         auto_adjust=False, progress=False, threads=True)
        got = _split(df, chunk)
        for tk, x in got.items():
            if x.index.tz is not None:
                x.index = x.index.tz_localize(None)
            cache["d1"][tk] = x
        print("  journalier %d/%d (+%d)" % (min(i + 100, len(todo)), len(todo), len(got)), flush=True)
    with open(CACHE, "wb") as f:
        pickle.dump(cache, f)

    screened = screen(cache["d1"])
    print("filtre mecanique: %d tickers passent" % len(screened))
    # l'univers actuel et les candidats d'aout sont toujours charges, meme
    # s'ils ne passent pas le filtre, pour servir de reference
    todo = [t for t in list(screened["ticker"]) + CURRENT + EXTRA
            if t not in cache["h1"] and t in cache["d1"]]
    for i in range(0, len(todo), 40):
        chunk = todo[i:i + 40]
        df = yf.download(chunk, period="730d", interval="1h", group_by="ticker",
                         auto_adjust=False, progress=False, threads=True)
        got = _split(df, chunk)
        for tk, x in got.items():
            x.index = (x.index.tz_localize(NY) if x.index.tz is None else x.index.tz_convert(NY))
            cache["h1"][tk] = x
        print("  horaire %d/%d (+%d)" % (min(i + 40, len(todo)), len(todo), len(got)), flush=True)
        with open(CACHE, "wb") as f:
            pickle.dump(cache, f)


def screen(d1: dict) -> pd.DataFrame:
    rows = []
    a, b = TRAIN[0].tz_localize(None).normalize(), TRAIN[1].tz_localize(None).normalize()
    for tk, d in d1.items():
        x = d[(d.index >= a) & (d.index <= b)]
        if len(x) < 250:
            continue
        px = float(x["Close"].iloc[-1])
        dvol = float((x["Close"] * x["Volume"]).median())
        reach = float((x["High"] >= x["Open"] * (1 + TP_PCT / 100)).mean())
        prev = x["Close"].shift(1)
        tr = pd.concat([x["High"] - x["Low"], (x["High"] - prev).abs(), (x["Low"] - prev).abs()], axis=1).max(axis=1)
        atr_pct = float((tr / x["Close"]).median() * 100)
        rows.append({"ticker": tk, "px": px, "dvol": dvol, "reach": reach, "atr_pct": atr_pct})
    df = pd.DataFrame(rows)
    ok = (df.px.between(MIN_PX, MAX_PX)) & (df.dvol >= MIN_DVOL) & (df.reach >= MIN_TP_REACH)
    return df[ok].sort_values("reach", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
def make_data(cache: dict, rdata: dict, tickers: list) -> dict:
    return {"h1": {t: cache["h1"][t] for t in tickers}, "d1": {t: cache["d1"][t] for t in tickers},
            "earnings": {}, "vix": rdata["d1"]["^VIX"]}


def signals(cache, rdata, tickers) -> pd.DataFrame:
    old = B.TICKERS
    B.TICKERS = list(tickers)
    B.VIX_THRESHOLD = 26.0  # valeur du .env en production
    try:
        s = B.build_signals(make_data(cache, rdata, tickers))
    finally:
        B.TICKERS = old
        B.VIX_THRESHOLD = 22.0
    s["kind"], s["prio"] = "open", 0.0
    return s


LIVE = E.Exit("live", sl_pct=2, tp_pct=3.5, trailing=True, flat_min_profit=1.2)


def per_ticker(cache, rdata, sig_all, tk, win):
    bars = E.Bars({tk: cache["h1"][tk]})
    s = sig_all[sig_all["ticker"] == tk]
    if s.empty:
        return 0.0, 0, 0.0
    m = E.metrics(E.simulate(bars, s, LIVE, max_open=1, win=win))
    return m["net"], m["n"], m["bps"]


def diversified(order: list, rets: pd.DataFrame, k: int) -> list:
    pick = []
    for tk in order:
        if tk not in rets.columns:
            continue
        if all(abs(rets[tk].corr(rets[p])) < MAX_CORR for p in pick):
            pick.append(tk)
        if len(pick) == k:
            break
    return pick


def portfolio(cache, sig_all, tickers, win, reps=20):
    bars = E.Bars({t: cache["h1"][t] for t in tickers})
    s = sig_all[sig_all["ticker"].isin(tickers)].reset_index(drop=True)
    return E.robust(bars, s, LIVE, reps=reps, win=win)


def evaluate():
    with open(CACHE, "rb") as f:
        cache = pickle.load(f)
    rdata = RD.load()
    scr = screen(cache["d1"])
    scr = scr[scr.ticker.isin(cache["h1"].keys())].reset_index(drop=True)
    # l'univers actuel est toujours evalue, meme s'il ne passe pas le filtre
    cur = [t for t in CURRENT if t in cache["h1"]]
    extra = [t for t in EXTRA if t in cache["h1"]]
    names = sorted(set(scr.ticker) | set(cur) | set(extra))
    print("=" * 110)
    print("RECHERCHE DE TICKERS — pool filtre: %d tickers | TRAIN %s -> %s | TEST %s -> %s"
          % (len(scr), TRAIN[0].date(), TRAIN[1].date(), TEST[0].date(), TEST[1].date()))
    print("=" * 110)
    print("Univers actuel dans le filtre mecanique: %s"
          % ", ".join("%s%s" % (t, "" if t in set(scr.ticker) else "(hors filtre)") for t in cur))

    sig_all = signals(cache, rdata, names)
    print("signaux (config live, VIX 26): %d sur %d tickers" % (len(sig_all), len(names)))

    rows = []
    for tk in names:
        ntr, ntn, btr = per_ticker(cache, rdata, sig_all, tk, TRAIN)
        nte, nten, bte = per_ticker(cache, rdata, sig_all, tk, TEST)
        rows.append({"ticker": tk, "train": ntr, "n_train": ntn, "bps_train": btr,
                     "test": nte, "n_test": nten, "bps_test": bte})
    pt = pd.DataFrame(rows).merge(scr, on="ticker", how="left")
    ok = pt[(pt.n_train >= 20) & (pt.n_test >= 20)]
    rho = ok["train"].rank().corr(ok["test"].rank())
    rho_b = ok["bps_train"].rank().corr(ok["bps_test"].rank())
    print("\n--- 1) Le resultat passe d'un ticker predit-il son resultat futur ? (%d tickers, >= 20 trades par moitie)" % len(ok))
    print("  Spearman net$ train vs test  : rho = %+.3f" % rho)
    print("  Spearman bps/trade train/test: rho = %+.3f" % rho_b)
    q = ok.assign(qt=pd.qcut(ok["train"].rank(method="first"), 5, labels=["Q1 pire", "Q2", "Q3", "Q4", "Q5 meilleur"]))
    print("  Resultat TEST moyen par quintile de TRAIN:")
    for lab, g in q.groupby("qt", observed=True):
        print("    %-12s train %+7.1f$  ->  test %+7.1f$   (n=%d tickers)" % (lab, g.train.mean(), g.test.mean(), len(g)))
    rr = ok["reach"].rank().corr(ok["test"].rank())
    print("  Spearman frequence TP atteignable (train) vs net$ test : rho = %+.3f" % rr)

    # rendements journaliers TRAIN pour la diversification
    a, b = TRAIN[0].tz_localize(None).normalize(), TRAIN[1].tz_localize(None).normalize()
    rets = pd.DataFrame({t: cache["d1"][t]["Close"] for t in names})
    rets = rets[(rets.index >= a) & (rets.index <= b)].pct_change()

    s1 = diversified(list(scr.ticker), rets, K)
    # seuls les tickers admis par le filtre mecanique sont selectionnables
    # (filtre sur n_train seulement: exiger n_test ferait fuiter la periode TEST)
    admis = pt[(pt.n_train >= 20) & pt.ticker.isin(set(scr.ticker))]
    s2 = diversified(list(admis.sort_values("train", ascending=False).ticker), rets, K)
    sets = [("S0 univers actuel", cur), ("S1 mecanique (TP atteignable)", s1),
            ("S2 meilleur backtest TRAIN", s2)]
    print("\n--- 2) Portefeuilles de 9 tickers juges sur les 18 derniers mois (config live, 2 x 520$, 20 perturbations)")
    print("%-32s %8s %17s %5s %6s %6s %6s | %s" % ("selection", "TEST $", "p10-p90", ">0", "n", "bps", "DD%", "tickers"))
    res = {}
    for lab, tks in sets:
        r = portfolio(cache, sig_all, tks, TEST)
        rt = portfolio(cache, sig_all, tks, TRAIN, reps=8)
        res[lab] = r["med"]
        print("%-32s %+8.0f [%+6.0f,%+6.0f] %4.0f%% %6d %+6.1f %+6.1f | %s   (train %+.0f$)"
              % (lab, r["med"], r["p10"], r["p90"], r["pos"], r["m"]["n"], r["m"]["bps"],
                 r["m"]["dd_pct"], ",".join(tks), rt["med"]))
    rng = np.random.default_rng(3)
    pool = list(scr.ticker)
    draws = []
    for k in range(100):
        tks = list(rng.choice(pool, size=K, replace=False))
        bars = E.Bars({t: cache["h1"][t] for t in tks})
        s = sig_all[sig_all["ticker"].isin(tks)].reset_index(drop=True)
        draws.append(E.metrics(E.simulate(bars, s, LIVE, seed=k, win=TEST))["net"])
    draws = np.array(draws)
    print("%-32s %+8.0f [%+6.0f,%+6.0f] %4.0f%%   (100 tirages de 9 tickers du pool filtre)"
          % ("R  9 tickers au hasard", np.median(draws), np.percentile(draws, 10),
             np.percentile(draws, 90), 100 * (draws > 0).mean()))
    for lab, v in res.items():
        print("   rang de %-30s parmi les tirages: %3.0f%%" % (lab, 100 * (draws < v).mean()))

    print("\n--- 3) Tickers du pool: meilleurs et pires sur TEST (information, PAS une selection)")
    cols = ["ticker", "px", "reach", "atr_pct", "n_train", "train", "bps_train", "n_test", "test", "bps_test"]
    show = pt[pt.n_test >= 20].sort_values("test", ascending=False)
    with pd.option_context("display.width", 160, "display.max_columns", 20):
        print(show[cols].head(15).round(2).to_string(index=False))
        print("...")
        print(show[cols].tail(8).round(2).to_string(index=False))
        print("\nUnivers actuel + candidats d'aout (retires / reserve):")
        print(pt[pt.ticker.isin(cur + extra)][cols].round(2).to_string(index=False))
    pt.to_csv(os.path.join(ROOT, "scripts", "research_tickers_results.csv"), index=False)
    print("\nDetail par ticker: scripts/research_tickers_results.csv")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "download":
        download()
    else:
        evaluate()
