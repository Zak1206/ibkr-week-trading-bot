# -*- coding: utf-8 -*-
"""
ETF inverse quand les 8 tickers sont baissiers (idee de Zak, 26/10/2026).

Regles FIGEES AVANT le premier calcul, ecrites dans
docs/reports/recherche-etf-inverse-2026-10-26.md (section 1). En bref:
  - Strategie longue = FIXE-8 de la section 15 (research_fallback_universe):
    breakout semaine des 8, VIX 22, SL -10 / TP +25 / 5 seances si en gain,
    1 ligne x 1000 $ fixe, frais IBKR + 3 bps.
  - Regime baissier le jour D: >= K des 8 avec tendance journaliere <= 0 a la
    cloture de D-1. K=6 teste, K=5 et K=7 en controle de sensibilite.
  - ETF inverse (PSQ, SH, SQQQ): achete a l'ouverture de 9h30 d'une seance
    baissiere si la ligne est libre et qu'aucun long n'entre a cette barre;
    vendu a l'ouverture de la premiere seance non baissiere, ou au moment ou un
    long entre (le long est prioritaire et garde exactement ses trades).
    Pas de stop. Meme taille, memes frais, memes 3 bps que les longs.
  - Prix: journaliers yfinance ajustes. Vente en cours de seance: cloture
    ajustee de la veille x (1 - L x variation de l'indice depuis la cloture
    de la veille), indice QQQ ou SPY en barres 1h (ETF remis a zero chaque jour).
  - 12 perturbations appariees (memes graines que E.robust).

Methode: la strategie longue est simulee par research_engine (non modifie),
puis rejouee barre par barre avec l'ETF inverse dans les trous. Comme le long
est prioritaire, ses entrees et sorties ne changent pas; seul le cash (donc la
taille des lignes) peut changer. Le rejeu sans inverse doit retrouver le moteur
au centime (controle affiche).

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_inverse_etf.py
"""
from __future__ import annotations

import math
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
import research_dynamic_universe as DU  # noqa: E402
import research_engine as E  # noqa: E402
import research_tickers as T  # noqa: E402
from research_fallback_universe import CORE, EXIT, KW, REPS, core_bearish_count  # noqa: E402

NY = "America/New_York"
ETF_CACHE = os.path.join(HERE, ".inverse_etf_cache.pkl")
# variante -> (ETF, indice suivi, levier inverse)
VARIANTS = {"PSQ": ("PSQ", "QQQ", 1.0), "SH": ("SH", "SPY", 1.0), "SQQQ": ("SQQQ", "QQQ", 3.0)}
KS = (6, 5, 7)
HS = 3.0 / 2.0 / 10000.0   # demi-spread, comme le moteur


def load_etf_daily() -> dict:
    # cache local ecrit par ce script (meme usage que les autres caches de recherche)
    if os.path.exists(ETF_CACHE):
        with open(ETF_CACHE, "rb") as f:
            return pickle.load(f)
    out = {}
    for tk in ("PSQ", "SH", "SQQQ", "QQQ", "SPY"):
        d = yf.download(tk, start="2024-01-01", end="2026-10-01", interval="1d",
                        progress=False, auto_adjust=True)
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d.index = d.index.tz_localize(None) if d.index.tz is not None else d.index
        out[tk] = d
    with open(ETF_CACHE, "wb") as f:
        pickle.dump(out, f)
    return out


def window_ts(bars: E.Bars, win, skip_days: int) -> list:
    """Memes barres que research_engine.simulate (fenetre puis depart decale)."""
    ts = [t for t in bars.all_ts if win[0] <= t <= win[1]]
    if skip_days > 0 and ts:
        cut = ts[0] + pd.Timedelta(days=skip_days)
        ts = [t for t in ts if t >= cut]
    return ts


class InversePricer:
    """Prix de l'ETF inverse a l'ouverture d'une seance, en cours de seance, et a la cloture."""

    def __init__(self, etf_d: pd.DataFrame, idx_h1: pd.DataFrame, lev: float):
        self.d = etf_d
        self.lev = lev
        self.days = etf_d.index
        self.idx_open = {t: float(v) for t, v in idx_h1["Open"].items()}
        day = idx_h1.index.normalize().tz_localize(None)
        last = idx_h1.groupby(day)["Close"].last()
        self.idx_close = {pd.Timestamp(k): float(v) for k, v in last.items()}

    def _prev_day(self, day: pd.Timestamp) -> pd.Timestamp:
        i = self.days.searchsorted(day, side="left") - 1
        return self.days[i]

    def open_(self, day: pd.Timestamp) -> float:
        return float(self.d.at[day, "Open"])

    def close(self, day: pd.Timestamp) -> float:
        return float(self.d.at[day, "Close"])

    def intraday(self, t: pd.Timestamp) -> float:
        day = t.tz_convert(NY).normalize().tz_localize(None)
        if t.tz_convert(NY).strftime("%H:%M") == "09:30":
            return self.open_(day)
        prev = self._prev_day(day)
        move = self.idx_open[t] / self.idx_close[prev] - 1.0
        return float(self.d.at[prev, "Close"]) * (1.0 - self.lev * move)


def replay(bars, longs, ts_list, regime_day, pricer, budget=1000.0, line_usd=1000.0):
    """
    Rejoue les trades longs du moteur et place l'ETF inverse dans les trous.
    pricer=None: pas d'inverse (controle: doit retrouver le moteur).
    Retourne (trades longs, trades inverses, equity par seance, stats).
    """
    entries, exits = {}, {}
    for k, t in enumerate(longs):
        entries.setdefault(t["entry_ts"], []).append(k)
        exits.setdefault(t["exit_ts"], []).append(k)
    cash = budget
    long_open = None          # (index du trade, qty, cost)
    inv = None                # dict(qty, entry, cost, entry_ts)
    long_out, inv_out, equity = [], [], []
    lost = 0
    n_regime = n_regime_free = 0

    def buy_inv(px, t):
        nonlocal cash, inv
        entry = px * (1.0 + HS)
        line = min(line_usd, cash - E.FEE_MIN)
        qty = math.floor(line / entry) if line >= 50 else 0
        if qty < 1:
            return
        cost = entry * qty + E.fees(qty)
        if cost > cash:
            return
        cash -= cost
        inv = {"qty": qty, "entry": entry, "cost": cost, "entry_ts": t}

    def sell_inv(px, t, reason):
        nonlocal cash, inv
        proceeds = px * (1.0 - HS) * inv["qty"] - E.fees(inv["qty"])
        cash += proceeds
        inv_out.append({"entry_ts": inv["entry_ts"], "exit_ts": t, "net": proceeds - inv["cost"],
                        "cost": inv["cost"], "ret_pct": (px / inv["entry"] - 1.0) * 100.0, "reason": reason})
        inv = None

    last_day = None
    for t in ts_list:
        day = t.tz_convert(NY).normalize().tz_localize(None)
        first_bar = day != last_day
        if first_bar:
            reg = regime_day(day)
            if reg:
                n_regime += 1
                if long_open is None:
                    n_regime_free += 1
            if pricer is not None:
                if inv is not None and not reg:
                    sell_inv(pricer.open_(day), t, "FIN_REGIME")
                if inv is None and reg and long_open is None and t not in entries:
                    buy_inv(pricer.open_(day), t)
        # 1) entrees longues (le moteur traite les entrees avant les sorties)
        for k in entries.get(t, []):
            tr = longs[k]
            if inv is not None:
                sell_inv(pricer.intraday(t), t, "LONG")
            line = min(line_usd, cash - E.FEE_MIN)
            qty = math.floor(line / tr["entry"]) if line >= 50 else 0
            cost = tr["entry"] * qty + E.fees(qty) if qty >= 1 else float("inf")
            if qty < 1 or cost > cash:
                lost += 1
                continue
            cash -= cost
            long_open = (k, qty, cost)
        # 2) sorties longues
        for k in exits.get(t, []):
            if long_open is None or long_open[0] != k:
                continue
            tr = longs[k]
            _, qty, cost = long_open
            proceeds = tr["exit"] * (1.0 - HS) * qty - E.fees(qty)
            cash += proceeds
            long_out.append({**tr, "qty": qty, "cost": cost, "net": proceeds - cost})
            long_open = None
        # 3) equity de fin de barre (la derniere de la seance est gardee)
        mv = 0.0
        if long_open is not None:
            tk = longs[long_open[0]]["ticker"]
            i = bars.pos[tk].get(t)
            mv += (bars.c[tk][i] if i is not None else longs[long_open[0]]["entry"]) * long_open[1]
        if inv is not None:
            mv += inv["qty"] * pricer.close(day)
        if equity and equity[-1][0] == day:
            equity[-1] = (day, cash + mv)
        else:
            equity.append((day, cash + mv))
        last_day = day
    if inv is not None:
        sell_inv(pricer.close(last_day), ts_list[-1], "END")
    stats = {"lost": lost, "regime_days": n_regime, "regime_free": n_regime_free}
    return long_out, inv_out, equity, stats


def dd_pct(equity) -> float:
    eq = pd.Series([e for _, e in equity], dtype=float)
    return float(((eq - eq.cummax()) / eq.cummax()).min() * 100.0) if len(eq) > 2 else 0.0


def regime_fn(bear: pd.Series, k: int):
    idx = bear.index.values
    vals = bear.values

    def f(day: pd.Timestamp) -> bool:
        p = np.searchsorted(idx, np.datetime64(day), side="left") - 1
        return bool(p >= 0 and vals[p] >= k)
    return f


def regime_periods(days: pd.DatetimeIndex, f) -> list:
    """(premiere seance du regime, premiere seance hors regime) sur des seances consecutives."""
    out, start = [], None
    for d in days:
        r = f(d)
        if r and start is None:
            start = d
        elif not r and start is not None:
            out.append((start, d))
            start = None
    return out


def main():
    with open(T.CACHE, "rb") as fh:
        cache = pickle.load(fh)
    rd = RD.load()
    etf = load_etf_daily()

    old = B.TICKERS
    B.TICKERS, B.VIX_THRESHOLD = list(CORE), 22.0
    try:
        sig = B.build_signals(T.make_data(cache, rd, list(CORE)))
    finally:
        B.TICKERS, B.VIX_THRESHOLD = old, 22.0
    sig = sig[sig["breakout"]].reset_index(drop=True)
    sig["kind"], sig["prio"] = "open", 1.0
    bars = E.Bars({t: cache["h1"][t] for t in CORE})
    bear = core_bearish_count(cache)
    pricers = {v: InversePricer(etf[e], rd["h1"][ix], lev) for v, (e, ix, lev) in VARIANTS.items()}
    wins = {"TRAIN": DU.TRAIN, "TEST": DU.TEST}

    # --- 1) strategie seule, par perturbation, et controle du rejeu ---
    base = {}
    max_gap = 0.0
    for wn, win in wins.items():
        runs = []
        for k in range(REPS):
            skip = (k % 5) * 4
            r = E.simulate(bars, sig, EXIT, seed=100 + k, skip_first_days=skip, win=win, **KW)
            m = E.metrics(r, KW["budget"])
            ts_list = window_ts(bars, win, skip)
            lo, _, eq, st = replay(bars, r["trades"], ts_list, lambda d: False, None)
            gap = abs(sum(t["net"] for t in lo) - m["net"]) + abs(dd_pct(eq) - m["dd_pct"])
            max_gap = max(max_gap, gap)
            runs.append({"net": m["net"], "dd": m["dd_pct"], "trades": r["trades"], "ts": ts_list,
                         "n": m["n"]})
        base[wn] = runs
        ref = E.robust(bars, sig, EXIT, reps=REPS, win=win, **KW)
        print("%s strategie seule: mediane %+.0f $ (robust %+.0f $), DD %.1f %%"
              % (wn, sorted(x["net"] for x in runs)[REPS // 2], ref["med"], ref["m"]["dd_pct"]))
    print("controle du rejeu sans inverse: ecart max %.6f (net $ + DD points) -> %s"
          % (max_gap, "OK" if max_gap < 1e-6 else "ECART"))

    # --- 2) strategie + ETF inverse ---
    def med_run(runs):
        s = sorted(runs, key=lambda x: x["net"])
        return s[len(s) // 2], s[int(0.1 * (len(s) - 1))]

    print("\n" + "=" * 128)
    print("STRATEGIE + ETF INVERSE — 1 x 1000 $, mediane de %d perturbations appariees" % REPS)
    print("=" * 128)
    print("%-5s %-3s | %8s %8s %6s %6s %5s | %8s %8s %6s %6s %5s | %s"
          % ("ETF", "K", "TRAIN $", "seule $", "DD%", "DDs%", "%pos", "TEST $", "seule $", "DD%", "DDs%", "%pos",
             "inverse (TEST, run median) | longs perdus"))
    verdict = {}
    for v in VARIANTS:
        for kk in KS:
            f = regime_fn(bear, kk)
            row = {}
            for wn in wins:
                runs = []
                for k, b in enumerate(base[wn]):
                    lo, io, eq, st = replay(bars, b["trades"], b["ts"], f, pricers[v])
                    net = sum(t["net"] for t in lo) + sum(t["net"] for t in io)
                    runs.append({"net": net, "dd": dd_pct(eq), "delta": net - b["net"], "inv": io,
                                 "lost": b["n"] - len(lo), "st": st, "base_dd": b["dd"]})
                med, p10 = med_run(runs)
                bmed = sorted(base[wn], key=lambda x: x["net"])[REPS // 2]
                row[wn] = {"med": med["net"], "p10": p10["net"], "dd": med["dd"], "bmed": bmed["net"],
                           "bdd": bmed["dd"], "pos": 100.0 * np.mean([r["delta"] > 0 for r in runs]),
                           "lost": max(r["lost"] for r in runs), "inv": med["inv"], "st": med["st"]}
            tr, te = row["TRAIN"], row["TEST"]
            inv = te["inv"]
            nets = np.array([x["net"] for x in inv]) if inv else np.zeros(0)
            inv_txt = ("%3d trades, %+6.0f $, %2.0f%% gagnants" % (len(nets), nets.sum(), 100 * (nets > 0).mean())
                       if len(nets) else "-")
            print("%-5s %-3d | %+8.0f %+8.0f %6.1f %6.1f %5.0f | %+8.0f %+8.0f %6.1f %6.1f %5.0f | %s | %d"
                  % (v, kk, tr["med"], tr["bmed"], tr["dd"], tr["bdd"], tr["pos"],
                     te["med"], te["bmed"], te["dd"], te["bdd"], te["pos"], inv_txt, max(tr["lost"], te["lost"])),
                  flush=True)
            ok = all(row[w]["med"] > row[w]["bmed"] and row[w]["pos"] >= 80.0
                     and row[w]["dd"] >= row[w]["bdd"] - 5.0 and row[w]["lost"] == 0 for w in wins)
            verdict[(v, kk)] = (ok, row)

    print("\nCriteres (K=6): gain median > seule en TRAIN et TEST, >= 80 %% des perturbations appariees > 0,"
          " DD pas pire de plus de 5 points, 0 long perdu")
    for v in VARIANTS:
        ok, row = verdict[(v, 6)]
        sens = [verdict[(v, kk)][1]["TEST"]["med"] > verdict[(v, kk)][1]["TEST"]["bmed"] for kk in (5, 7)]
        label = "ADOPTABLE" if ok and any(sens) else ("NON CONCLUANT (voisins)" if ok else "REJETE")
        print("  %-5s K=6: %s" % (v, label))

    # --- 3) diagnostic: l'inverse seul pendant chaque periode baissiere ---
    print("\nDiagnostic: ETF inverse et indice seuls, de l'ouverture qui suit la confirmation"
          " a l'ouverture qui suit la fin du regime (toute la fenetre TRAIN+TEST)")
    days = etf["PSQ"].loc[DU.TRAIN[0].tz_localize(None).normalize():DU.TEST[1].tz_localize(None)].index
    for kk in KS:
        f = regime_fn(bear, kk)
        per = regime_periods(days, f)
        share = 100.0 * np.mean([f(d) for d in days])
        line = "K=%d: %2d periodes, %3.0f%% des seances, duree mediane %4.1f seances" % (
            kk, len(per), share, np.median([days.get_loc(b) - days.get_loc(a) for a, b in per]) if per else 0)
        print(line)
        for tk in ("PSQ", "SH", "SQQQ", "QQQ", "SPY"):
            r = np.array([etf[tk].at[b, "Open"] / etf[tk].at[a, "Open"] - 1.0 for a, b in per]) * 100.0
            if len(r):
                print("    %-5s moyenne %+6.2f %%, mediane %+6.2f %%, %3.0f%% des periodes gagnantes, cumul %+7.1f %%"
                      % (tk, r.mean(), np.median(r), 100 * (r > 0).mean(), 100 * (np.prod(1 + r / 100) - 1)))
    for wn in wins:
        st = verdict[("PSQ", 6)][1][wn]["st"]
        print("%s, K=6, run median: %d seances baissieres, dont %d avec la ligne libre a 9h30"
              % (wn, st["regime_days"], st["regime_free"]))


if __name__ == "__main__":
    main()
