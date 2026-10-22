# -*- coding: utf-8 -*-
"""
Univers de secours quand les 8 tickers sont baissiers (idee de Zak, 21/10/2026).

Regles FIGEES AVANT le premier lancement (aucun reglage apres coup):
  - Coeur: les 8 tickers du bot, toujours scannes, prioritaires (prio 1).
  - Bascule: le jour D, si au moins K des 8 avaient une tendance journaliere <= 0
    a la cloture de la veille (meme score -2..+2 que le filtre MTF du bot),
    les signaux du pool de secours sont acceptes ce jour-la (prio 0).
  - Pool de secours: pool de recherche hors coeur, filtre le 1er de chaque mois
    avec les seules donnees passees par la regle mecanique de universe_review.py:
    prix 5-600 $, volume median >= 100 M$/jour, +3.5% intraday dans >= 15% des
    seances sur 12 mois.
  - Strategie identique partout (signaux breakout semaine, VIX 22), sortie du
    profil paper actuel: SL -10 / TP +25 / 5 seances seulement si en gain,
    1 ligne x 1000 $ fixe (sans paliers, pour comparer les univers entre eux).

Variantes:
  FIXE-8       les 8 seuls (reference)
  SECOURS K=6  regle principale
  SECOURS K=5  sensibilite au seuil
  SECOURS K=7  sensibilite au seuil
  K=6 liquide  pool sans le filtre de volatilite (prix + volume seulement)
  TOUJOURS     pool de secours ouvert tous les jours (la condition sert-elle ?)

Adoption seulement si: K=6 bat FIXE-8 en TRAIN et en TEST (mediane de 12
perturbations), K=5 et K=7 aussi, p10 TEST de K=6 >= p10 TEST de FIXE-8, et
chute max pas pire de plus de 10 points.

Periodes: TRAIN 2024-05 -> 2025-03, TEST 2025-04 -> 2026-09 (comme section 12).

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_fallback_universe.py
"""
from __future__ import annotations

import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import backtest_swing as B  # noqa: E402
import research_data as RD  # noqa: E402
import research_dynamic_universe as DU  # noqa: E402
import research_engine as E  # noqa: E402
import research_tickers as T  # noqa: E402

NY = "America/New_York"
CORE = DU.CURRENT8
EXIT = E.Exit("week", sl_pct=10, tp_pct=25, max_hold_tdays=5, max_hold_only_if_profit=True)
KW = dict(line_usd=1000, max_open=1, budget=1000)
REPS = 12


def core_bearish_count(cache) -> pd.Series:
    """Nombre de tickers du coeur avec tendance journaliere <= 0, par date de cloture."""
    sc = pd.concat({tk: B.daily_trend_score(cache["d1"][tk]).dropna() for tk in CORE}, axis=1).sort_index()
    sc = sc.ffill()
    return (sc <= 0).sum(axis=1)


def main():
    with open(T.CACHE, "rb") as f:
        cache = pickle.load(f)
    rd = RD.load()
    pool = DU.pool_names(cache)
    missing = set(CORE) - set(pool)
    assert not missing, "coeur absent du pool: %s" % missing
    print("pool: %d titres (dont %d du coeur)" % (len(pool), len(CORE)))

    old = B.TICKERS
    B.TICKERS, B.VIX_THRESHOLD = pool, 22.0
    try:
        sig = B.build_signals(T.make_data(cache, rd, pool))
    finally:
        B.TICKERS, B.VIX_THRESHOLD = old, 22.0
    sig = sig[sig["breakout"]].reset_index(drop=True)
    sig["kind"] = "open"

    # date de la barre du SIGNAL (entree = barre suivante), comme le bot au scan
    sig_day = np.array([cache["h1"][tk].index[int(b) - 1].tz_convert(NY).normalize().tz_localize(None)
                        for tk, b in zip(sig["ticker"], sig["bar"])])
    bear = core_bearish_count(cache)
    pos = np.searchsorted(bear.index.values, sig_day.astype("datetime64[ns]"), side="left") - 1
    sig["bear"] = np.where(pos >= 0, bear.values[np.clip(pos, 0, None)], 0)
    sig["month"] = [pd.Timestamp(d).to_period("M").start_time.tz_localize(NY) for d in sig_day]

    months = pd.date_range("2024-05-01", "2026-09-01", freq="MS", tz=NY)
    vol_pool, liq_pool = {}, {}
    for m in months:
        v, l_ = set(), set()
        for tk in pool:
            if tk in CORE:
                continue
            s = DU.monthly_stats(cache["d1"][tk], m)
            if s and 5 <= s["px"] <= 600 and s["dvol"] >= 100e6:
                l_.add(tk)
                if s["reach"] >= 0.15:
                    v.add(tk)
        vol_pool[m], liq_pool[m] = v, l_
    print("pool de secours (regle mecanique): %.0f titres/mois en moyenne, %.0f sans le filtre de volatilite"
          % (np.mean([len(x) for x in vol_pool.values()]), np.mean([len(x) for x in liq_pool.values()])))

    def build(k, fallback):
        is_core = sig["ticker"].isin(CORE)
        in_fb = np.array([tk in fallback.get(m, set()) for tk, m in zip(sig["ticker"], sig["month"])])
        on = np.ones(len(sig), dtype=bool) if k is None else (sig["bear"].values >= k)
        s = sig[is_core.values | (in_fb & on)].copy()
        s["prio"] = np.where(s["ticker"].isin(CORE), 1.0, 0.0)
        return s.reset_index(drop=True)

    variants = {
        "FIXE-8": sig[sig["ticker"].isin(CORE)].assign(prio=1.0).reset_index(drop=True),
        "SECOURS K=6": build(6, vol_pool),
        "SECOURS K=5": build(5, vol_pool),
        "SECOURS K=7": build(7, vol_pool),
        "K=6 liquide": build(6, liq_pool),
        "TOUJOURS": build(None, vol_pool),
    }

    def share_on(k, win):
        d = bear[(bear.index >= win[0].tz_localize(None).normalize()) & (bear.index <= win[1].tz_localize(None))]
        return 100.0 * float((d >= k).mean())

    print("seances ou le coeur est baissier (>= K sur 8 a <= 0): "
          + "  ".join("K=%d TRAIN %.0f%% TEST %.0f%%" % (k, share_on(k, DU.TRAIN), share_on(k, DU.TEST))
                      for k in (5, 6, 7)))

    bars = E.Bars({t: cache["h1"][t] for t in pool})
    print("=" * 118)
    print("UNIVERS DE SECOURS — 1 x 1000 $, SL -10 / TP +25 / 5 seances si en gain, mediane de %d perturbations" % REPS)
    print("=" * 118)
    print("%-12s | %8s %7s %6s | %8s %7s %6s %4s | %s"
          % ("variante", "TRAIN $", "p10", "DD%", "TEST $", "p10", "DD%", "n", "trades du pool de secours (TEST, run median)"))
    out = {}
    for name, s in variants.items():
        a = E.robust(bars, s, EXIT, reps=REPS, win=DU.TRAIN, **KW)
        b = E.robust(bars, s, EXIT, reps=REPS, win=DU.TEST, **KW)
        out[name] = (a, b)
        fb = [t for t in b["res"]["trades"] if t["ticker"] not in CORE]
        if fb:
            nets = np.array([t["net"] for t in fb])
            fb_txt = "%3d trades, %+6.0f $, %2.0f%% gagnants, %+5.0f bps/trade" % (
                len(fb), nets.sum(), 100 * (nets > 0).mean(),
                1e4 * np.mean([t["net"] / t["cost"] for t in fb]))
        else:
            fb_txt = "-"
        print("%-12s | %+8.0f %+7.0f %+6.1f | %+8.0f %+7.0f %+6.1f %4d | %s"
              % (name, a["med"], a["p10"], a["m"]["dd_pct"], b["med"], b["p10"], b["m"]["dd_pct"],
                 b["m"]["n"], fb_txt), flush=True)

    ref_a, ref_b = out["FIXE-8"]
    beats = {k: out[k][0]["med"] > ref_a["med"] and out[k][1]["med"] > ref_b["med"]
             for k in ("SECOURS K=5", "SECOURS K=6", "SECOURS K=7")}
    k6a, k6b = out["SECOURS K=6"]
    ok = (all(beats.values()) and k6b["p10"] >= ref_b["p10"]
          and k6b["m"]["dd_pct"] >= ref_b["m"]["dd_pct"] - 10
          and k6a["m"]["dd_pct"] >= ref_a["m"]["dd_pct"] - 10)
    print("\nbat FIXE-8 sur les deux periodes: " + ", ".join("%s %s" % (k, "oui" if v else "non") for k, v in beats.items()))
    print("VERDICT (criteres figes): %s" % ("ADOPTABLE" if ok else "REJETE"))

    # Diagnostic (ajoute apres le verdict, ne le change pas): avec 1 ligne, un trade
    # de secours occupe la place. Quels trades de FIXE-8 sont perdus pendant ce temps ?
    print("\nPourquoi (TEST, un seul run sans perturbation):")
    ref = E.simulate(bars, variants["FIXE-8"], EXIT, seed=100, win=DU.TEST, **KW)["trades"]
    print("  FIXE-8       coeur %2d trades %+6.0f $" % (len(ref), sum(t["net"] for t in ref)))
    for name in ("SECOURS K=5", "SECOURS K=6", "SECOURS K=7", "TOUJOURS"):
        tr = E.simulate(bars, variants[name], EXIT, seed=100, win=DU.TEST, **KW)["trades"]
        core = [t for t in tr if t["ticker"] in CORE]
        fb = [t for t in tr if t["ticker"] not in CORE]
        held = [(t["entry_ts"], t["exit_ts"]) for t in fb]
        lost = [t for t in ref if any(a <= t["entry_ts"] <= b for a, b in held)]
        print("  %-12s coeur %2d trades %+6.0f $ | secours %2d trades %+6.0f $ | trades FIXE-8 rates "
              "(ligne prise par le secours): %2d, %+6.0f $"
              % (name, len(core), sum(t["net"] for t in core), len(fb), sum(t["net"] for t in fb),
                 len(lost), sum(t["net"] for t in lost)))


if __name__ == "__main__":
    main()
