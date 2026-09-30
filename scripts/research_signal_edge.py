# -*- coding: utf-8 -*-
"""
Diagnostic: les signaux d'ENTREE du bot predisent-ils quelque chose ?

Deux tests independants de toute regle de sortie:

1) Etude d'evenement. Pour chaque signal (entree a l'ouverture de la barre
   suivante, comme dans backtest_swing), rendement futur a 1h, 3h, fin de
   seance, ~1j, ~2j, ~5j. On le compare au rendement INCONDITIONNEL du meme
   ticker a la meme heure d'entree (sinon on confond "le signal marche" avec
   "HOOD a fait +1100%" ou "la barre de 9h30 bouge beaucoup").
   IC95% par bootstrap en grappes par JOUR (les signaux d'une meme seance ne
   sont pas independants).

2) Placebo. Memes sorties (config live, variante semaine), memes tickers,
   meme nombre de signaux par ticker et par heure, mais dates tirees au
   hasard. Si le vrai signal ne sort pas du nuage des placebos, le resultat
   vient du marche et de la sortie, pas de l'entree.

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_signal_edge.py [--draws 100]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_swing as B  # noqa: E402

HORIZONS = [("1h", 1), ("3h", 3), ("~1j", 7), ("~2j", 14), ("~5j", 35)]


def bar_frame(data: dict, tk: str) -> pd.DataFrame:
    h = data["h1"][tk]
    ind = B.add_indicators(h)
    df = pd.DataFrame({
        "open": h["Open"].astype(float).values,
        "close": h["Close"].astype(float).values,
        "hour": h.index.hour,
        "day": h.index.normalize().tz_localize(None),
    }, index=h.index)
    df["valid"] = ~(ind["RSI_14"].isna() | ind["MACDs_12_26_9"].isna()).values
    n = len(df)
    op = df["open"].values
    cl = df["close"].values
    for lab, k in HORIZONS:
        out = np.full(n, np.nan)
        end = np.arange(n) + k - 1
        ok = end < n
        out[ok] = cl[end[ok]] / op[ok] - 1.0
        df["r_" + lab] = out
    # jusqu'a la cloture de la seance d'entree
    last_idx = df.groupby("day").cumcount(ascending=False).values
    end = np.arange(n) + last_idx
    df["r_eod"] = cl[end] / op - 1.0
    return df


def cluster_boot(x: np.ndarray, groups: np.ndarray, n: int = 3000,
                 seed: int = 0) -> tuple:
    rng = np.random.default_rng(seed)
    ug, inv = np.unique(groups, return_inverse=True)
    sums = np.bincount(inv, weights=x, minlength=len(ug))
    cnts = np.bincount(inv, minlength=len(ug)).astype(float)
    draws = rng.integers(0, len(ug), size=(n, len(ug)))
    means = sums[draws].sum(axis=1) / cnts[draws].sum(axis=1)
    means.sort()
    return float(x.mean()), float(means[int(0.025 * n)]), float(means[int(0.975 * n)])


def event_study(data: dict, sig: pd.DataFrame) -> None:
    cols = ["r_" + l for l, _ in HORIZONS] + ["r_eod"]
    labels = [l for l, _ in HORIZONS] + ["fin seance"]
    rows = []
    base_all = []
    for tk in B.TICKERS:
        df = bar_frame(data, tk)
        v = df[df["valid"]]
        base = v.groupby("hour")[cols].mean()
        base_all.append(v[cols + ["hour"]].assign(ticker=tk))
        s = sig[sig["ticker"] == tk]
        for r in s.itertuples():
            j = r.bar
            row = df.iloc[j]
            rec = {"ticker": tk, "day": row["day"], "path": "breakout" if r.breakout else "structure",
                   "conf": r.conf, "mtf": r.mtf}
            for c in cols:
                rec[c] = row[c]
                rec["x" + c] = row[c] - base.loc[row["hour"], c]
            rows.append(rec)
    ev = pd.DataFrame(rows)

    print("=" * 104)
    print("1) ETUDE D'EVENEMENT — rendement apres signal, en points de base (1 bp = 0.01%)")
    print("=" * 104)
    print("excess = rendement apres signal - rendement moyen du meme ticker a la meme heure.")
    print("Couts aller-retour a battre sur une ligne de 520$: ~13.5 bps de frais + 3 bps de spread = ~17 bps.\n")

    def block(title: str, e: pd.DataFrame) -> None:
        print("--- %s (n=%d signaux, %d seances) ---" % (title, len(e), e["day"].nunique()))
        print("%-12s %10s %10s %26s" % ("horizon", "brut bps", "excess bps", "IC95% excess (grappes/jour)"))
        for c, lab in zip(cols, labels):
            m = e[["x" + c, c, "day"]].dropna()
            if len(m) < 20:
                continue
            mu, lo, hi = cluster_boot(m["x" + c].values * 1e4, m["day"].values.astype("int64"))
            flag = "  <- significatif" if (lo > 0 or hi < 0) else ""
            print("%-12s %+10.1f %+10.1f        [%+7.1f , %+7.1f]%s"
                  % (lab, m[c].mean() * 1e4, mu, lo, hi, flag))
        print()

    block("TOUS LES SIGNAUX", ev)
    block("chemin BREAKOUT", ev[ev["path"] == "breakout"])
    block("chemin STRUCTURE (sans breakout)", ev[ev["path"] == "structure"])
    block("conf >= 80", ev[ev["conf"] >= 80])
    block("conf < 80", ev[ev["conf"] < 80])

    # le score de confiance classe-t-il les signaux ? correlation de rang
    print("--- Le score de confiance ordonne-t-il les rendements ? (Spearman conf vs excess) ---")
    for c, lab in zip(cols, labels):
        m = ev[["conf", "x" + c]].dropna()
        rho = m["conf"].rank().corr(m["x" + c].rank())
        print("  %-12s rho = %+.3f   (n=%d)" % (lab, rho, len(m)))
    print()

    ball = pd.concat(base_all)
    print("--- Reference: rendement moyen INCONDITIONNEL (toutes barres, 9 tickers) ---")
    for c, lab in zip(cols, labels):
        print("  %-12s %+7.1f bps" % (lab, ball[c].mean() * 1e4))
    print()


class PlaceboPools:
    """Barres eligibles par (ticker, heure), calculees une fois."""

    def __init__(self, data: dict, trend_only: bool):
        self.data = data
        self.pools = {}
        for tk in B.TICKERS:
            h = data["h1"][tk]
            ind = B.add_indicators(h)
            valid = ~(ind["RSI_14"].isna() | ind["MACDs_12_26_9"].isna()).values
            if trend_only:
                ts_day = B.daily_trend_score(data["d1"][tk]).dropna()
                bar_dates = h.index.normalize().tz_localize(None).values
                pos = np.searchsorted(ts_day.index.values, bar_dates, side="left") - 1
                ht = np.where(pos >= 0, ts_day.values[np.clip(pos, 0, None)], np.nan)
                valid &= (ht >= 1)
            valid[0] = False
            hours = h.index.hour.values
            self.pools[tk] = {hr: np.where(valid & (hours == hr))[0] for hr in np.unique(hours)}

    def draw(self, sig: pd.DataFrame, rng: np.random.Generator, frac: float) -> pd.DataFrame:
        """frac * (nb de signaux reels) par (ticker, heure), dates au hasard."""
        rows = []
        for (tk, hr), k in sig.groupby([sig["ticker"], sig["ts"].dt.hour]).size().items():
            pool = self.pools[tk].get(hr, np.zeros(0, dtype=int))
            k = int(round(k * frac))
            if len(pool) == 0 or k == 0:
                continue
            h = self.data["h1"][tk]
            for j in rng.choice(pool, size=min(k, len(pool)), replace=False):
                rows.append({"ts": h.index[j], "ticker": tk, "bar": int(j), "kind": "open",
                             "prio": 0.0})
        return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def placebo_test(data: dict, sig: pd.DataFrame, draws: int) -> None:
    """Placebo apparie sur le nombre de TRADES executes, pas de signaux.

    Les vrais signaux arrivent en grappes (plusieurs barres de suite sur le
    meme ticker) et une grappe ne fait qu'un trade; des dates tirees au hasard
    sont dispersees et font chacune un trade. A nombre de signaux egal, le
    placebo paierait donc 2-3x plus de frais: on reduit sa densite jusqu'a
    obtenir le meme nombre de trades que le vrai signal.
    """
    import research_engine as E
    import research_strategies as S

    bars = E.Bars(data["h1"])
    s_all = sig.copy()
    s_all["kind"], s_all["prio"] = "open", 0.0
    s_bko = s_all[s_all["breakout"]].reset_index(drop=True)
    live = E.Exit("live", sl_pct=2, tp_pct=3.5, trailing=True, flat_min_profit=1.2)
    week = E.Exit("week", sl_pct=10, tp_pct=25, max_hold_tdays=5)
    cases = [("signaux actuels + sortie live", s_all, live),
             ("signaux actuels + sortie semaine", s_all, week),
             ("breakout seul + sortie live", s_bko, live),
             ("breakout seul + sortie semaine", s_bko, week)]
    win = S.WIN

    print("=" * 128)
    print("2) PLACEBO APPARIE — memes sorties, meme nb de TRADES, entrees au hasard (%d tirages), fenetre %s -> %s"
          % (draws, win[0].date(), win[1].date()))
    print("=" * 128)
    print("A: dates uniformes (meme repartition par ticker et par heure).")
    print("B: idem, seulement les jours ou la tendance journaliere >= 1 (ce qu'apporte l'intraday AU-DELA du filtre MTF).\n")
    print("%-34s %8s %5s %7s | %-38s | %-38s"
          % ("cas", "VRAI $", "n", "bps/tr", "placebo A: $ med [p5,p95] rang / bps", "placebo B: $ med [p5,p95] rang / bps"))
    print("-" * 128)
    rng = np.random.default_rng(42)
    pools = {False: PlaceboPools(data, False), True: PlaceboPools(data, True)}
    for name, sg, ex in cases:
        reals = [E.metrics(E.simulate(bars, sg, ex, seed=500 + k, win=win)) for k in range(10)]
        r_net = float(np.median([m["net"] for m in reals]))
        r_n = float(np.median([m["n"] for m in reals]))
        r_bps = float(np.median([m["bps"] for m in reals]))
        cells = []
        for trend_only in (False, True):
            # calibration de la densite pour egaler le nombre de trades
            best = None
            for frac in (1.0, 0.7, 0.5, 0.35, 0.25, 0.18, 0.12, 0.08):
                ns = [E.metrics(E.simulate(bars, pools[trend_only].draw(sg, rng, frac), ex,
                                           seed=k, win=win))["n"] for k in range(2)]
                gap = abs(np.mean(ns) - r_n)
                if best is None or gap < best[0]:
                    best = (gap, frac, np.mean(ns))
            frac = best[1]
            nets, bps = [], []
            for k in range(draws):
                m = E.metrics(E.simulate(bars, pools[trend_only].draw(sg, rng, frac), ex,
                                         seed=k, win=win))
                nets.append(m["net"])
                bps.append(m["bps"])
            nets, bps = np.array(nets), np.array(bps)
            cells.append("%+6.0f [%+5.0f,%+5.0f] %3.0f%% / %+5.1f n~%d"
                         % (np.median(nets), np.percentile(nets, 5), np.percentile(nets, 95),
                            100.0 * (nets < r_net).mean(), np.median(bps), best[2]))
        print("%-34s %+8.0f %5.0f %+7.1f | %-38s | %-38s" % (name, r_net, r_n, r_bps, cells[0], cells[1]),
              flush=True)
    print("\nrang = % des placebos qui font MOINS bien que le vrai signal (>= 95% : l'entree a un edge).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=60)
    ap.add_argument("--skip-placebo", action="store_true")
    ap.add_argument("--skip-event", action="store_true")
    args = ap.parse_args()
    data = B.load_data()
    sig = B.build_signals(data)
    if not args.skip_event:
        event_study(data, sig)
    if not args.skip_placebo:
        placebo_test(data, sig, args.draws)


if __name__ == "__main__":
    main()
