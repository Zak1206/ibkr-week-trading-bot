# -*- coding: utf-8 -*-
"""
Copier les achats des dirigeants (Form 4, code P): bat-on le hasard apres frais ?
Methode figee avant les donnees: docs/reports/recherche-dirigeants-2026-10-26.md.

Entrees: research_insider_filings.pkl (research_insider_events.py) et
research_insider_prices.pkl (research_insider_prices.py), caches locaux produits par
ces scripts (donc fiables pour pickle).

- entree a l'ouverture de la premiere seance APRES la date de depot;
- controle du ticker: prix de la transaction a +-20 % de la cloture Yahoo non ajustee;
- univers: cloture non ajustee > 5 $, volume moyen 20 j > 5 M$, au jour du depot;
- un evenement par titre sur 5 seances; frais IBKR (1000 $ en actions entieres) + 3 pb
  de spread (10 pb sous 20 M$ de volume);
- temoin: meme titre, 16 entrees a +-30 seances hors +-5, memes horizon et frais;
- IC95 % par bootstrap groupe par date d'entree; train 2012-2019, test 2020+;
- critere: IC95 % train > 0 ET moyenne test > 0, a 5 seances;
- version jouable: 1 ligne x 1000 $, SL -10 % / TP +25 % / sortie a 5 seances.

Usage: .venv/Scripts/python.exe -I scripts/research_insider_study.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
FILINGS = os.path.join(HERE, "research_insider_filings.pkl")
PRICES = os.path.join(HERE, "research_insider_prices.pkl")

START = pd.Timestamp("2012-01-01")
TRAIN_END, ALT_TRAIN_END = pd.Timestamp("2019-12-31"), pd.Timestamp("2021-12-31")
MIN_TRAIN_EVENTS = 200
HORIZONS = (1, 5, 20, 60)
DECISIVE_H = 5
K_RANDOM, MATCH, EXCL, DEDUP = 16, 30, 5, 5
LINE, FEE_MIN, FEE_PS = 1000.0, 0.35, 0.0035
SL_PCT, TP_PCT, HOLD = 0.10, 0.25, 5
CHECK_TOL = 0.20
N_BOOT = 2000


def yahoo_symbol(sym: str) -> str:
    return sym.replace(".", "-")


def fee(qty: float) -> float:
    return max(FEE_MIN, FEE_PS * qty)


class Series:
    """Tableaux d'un titre. Prix Yahoo ajustes des splits; raw = ajuste x splits futurs."""

    def __init__(self, df: pd.DataFrame):
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        self.idx = idx.normalize()
        self.o = df["Open"].to_numpy(float)
        self.o[self.o <= 0] = np.nan  # ouvertures a 0 dans les vieilles donnees Yahoo
        self.h = df["High"].to_numpy(float)
        self.l = df["Low"].to_numpy(float)
        self.c = df["Close"].to_numpy(float)
        sp = df["Stock Splits"].fillna(0).to_numpy(float) if "Stock Splits" in df else np.zeros(len(df))
        s = np.where(sp > 0, sp, 1.0)
        prod_from = np.cumprod(s[::-1])[::-1]          # produit des splits a partir de i
        self.after = np.r_[prod_from[1:], 1.0]          # produit des splits strictement apres i
        dv = pd.Series(self.c * df["Volume"].to_numpy(float))
        self.adv20 = dv.rolling(20, min_periods=20).mean().to_numpy()
        self.n = len(df)

    def raw(self, arr: np.ndarray, i: int) -> float:
        return float(arr[i] * self.after[i])


def trade_cost(raw_open: float, spread: float):
    if not np.isfinite(raw_open) or raw_open <= 0:
        return None
    qty = np.floor(LINE / raw_open)
    if qty < 1:
        return None
    return 2 * fee(qty) / (qty * raw_open) + spread


def horizon_ret(s: Series, j: int, h: int) -> float:
    e = j + h - 1
    return s.c[e] / s.o[j] - 1.0 if e < s.n else np.nan


# ---------------------------------------------------------------- evenements
def filing_table(filings: pd.DataFrame, px: dict, missing: set) -> pd.DataFrame:
    f = filings[filings.is_od & filings.valid_symbol & (filings.value >= 25_000) & (filings.price >= 3)
                & (filings.filing_date >= START)].copy()
    f["ysym"] = f.symbol.map(yahoo_symbol)
    rows = []
    cache = {}
    for r in f.itertuples(index=False):
        rec = {"acc": r.ACCESSION_NUMBER, "ysym": r.ysym, "filing_date": r.filing_date, "value": r.value,
               "is_ceo_cfo": r.is_ceo_cfo, "od_ciks": r.od_ciks if isinstance(r.od_ciks, tuple) else (),
               "has_data": False, "check_ok": False, "liquid": False, "j": -1}
        if r.ysym in px:
            s = cache.get(r.ysym)
            if s is None:
                s = cache[r.ysym] = Series(px[r.ysym])
            rec["has_data"] = True
            it = int(np.searchsorted(s.idx, r.tdate_max, side="right")) - 1
            if it >= 0 and (r.tdate_max - s.idx[it]).days <= 5:
                rec["check_ok"] = abs(s.raw(s.c, it) / r.price - 1.0) <= CHECK_TOL
            i_f = int(np.searchsorted(s.idx, r.filing_date, side="right")) - 1
            j = i_f + 1
            if i_f >= 19 and j < s.n:
                rec["j"] = j
                rec["adv20"] = s.adv20[i_f]
                rec["liquid"] = s.raw(s.c, i_f) > 5.0 and s.adv20[i_f] > 5e6 and np.floor(LINE / s.raw(s.o, j)) >= 1
        rows.append(rec)
    return pd.DataFrame(rows), cache


def cluster_dates(ft: pd.DataFrame) -> pd.DataFrame:
    """(b): depot qui porte a >= 2 le nombre de dirigeants distincts sur 10 jours calendaires."""
    keep = []
    for _, g in ft.groupby("ysym"):
        g = g.sort_values("filing_date")
        by_day = g.groupby("filing_date")
        days = list(by_day.groups.keys())
        for d in days:
            win = g[(g.filing_date >= d - pd.Timedelta(days=10)) & (g.filing_date < d)]
            before = {c for t in win.od_ciks for c in t}
            today = by_day.get_group(d)
            after = before | {c for t in today.od_ciks for c in t}
            if len(before) < 2 <= len(after):
                keep.append(today.index.values)
    idx = np.concatenate(keep) if keep else np.array([], dtype=int)
    return ft.loc[idx]


def events_for(ft: pd.DataFrame) -> pd.DataFrame:
    """Un evenement par (titre, seance d'entree); filtres; dedup 5 seances par titre."""
    ok = ft[ft.has_data & ft.check_ok & ft.liquid & (ft.j >= 0)]
    ev = (ok.groupby(["ysym", "j"]).agg(filing_date=("filing_date", "min"), value=("value", "sum"),
                                         adv20=("adv20", "first")).reset_index().sort_values(["ysym", "j"]))
    out, last = [], {}
    for r in ev.itertuples(index=False):
        if r.ysym in last and r.j < last[r.ysym] + DEDUP:
            continue
        last[r.ysym] = r.j
        out.append(r)
    return pd.DataFrame(out)


# ---------------------------------------------------------------- rendements
def measure(ev: pd.DataFrame, series: dict, spy: Series, rng) -> pd.DataFrame:
    spy_pos = {d: i for i, d in enumerate(spy.idx)}
    rows = []
    for r in ev.itertuples(index=False):
        s = series[r.ysym]
        j = int(r.j)
        spread = 3e-4 if r.adv20 >= 20e6 else 10e-4
        cost = trade_cost(s.raw(s.o, j), spread)
        if cost is None:
            continue
        cand = np.array([k for k in range(j - MATCH, j + MATCH + 1) if abs(k - j) > EXCL and 0 <= k < s.n])
        draws = rng.choice(cand, size=K_RANDOM, replace=True) if len(cand) >= 4 else np.array([], dtype=int)
        draw_cost = {int(k): trade_cost(s.raw(s.o, int(k)), spread) for k in set(draws.tolist())}
        rec = {"ysym": r.ysym, "date": s.idx[j], "value": r.value}
        for h in HORIZONS:
            y = horizon_ret(s, j, h)
            rec[f"y{h}"] = y - cost if np.isfinite(y) else np.nan
            ys = [horizon_ret(s, int(k), h) - draw_cost[int(k)] for k in draws
                  if draw_cost[int(k)] is not None and np.isfinite(horizon_ret(s, int(k), h))]
            rec[f"x{h}"] = rec[f"y{h}"] - float(np.mean(ys)) if len(ys) >= 4 and np.isfinite(rec[f"y{h}"]) else np.nan
            e = j + h - 1
            if e < s.n and s.idx[j] in spy_pos and s.idx[e] in spy_pos:
                rec[f"m{h}"] = rec[f"y{h}"] - (spy.c[spy_pos[s.idx[e]]] / spy.o[spy_pos[s.idx[j]]] - 1.0)
            else:
                rec[f"m{h}"] = np.nan
        rows.append(rec)
    return pd.DataFrame(rows)


def boot_ci(df: pd.DataFrame, col: str, rng):
    d = df[["date", col]].dropna()
    if len(d) < 30:
        return np.nan, np.nan, np.nan, len(d)
    groups = [g[col].to_numpy() for _, g in d.groupby("date")]
    sums = np.array([g.sum() for g in groups])
    cnts = np.array([len(g) for g in groups])
    bs = np.empty(N_BOOT)
    for b in range(N_BOOT):
        p = rng.integers(0, len(groups), len(groups))
        bs[b] = sums[p].sum() / cnts[p].sum()
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return float(d[col].mean()), float(lo), float(hi), len(d)


# ---------------------------------------------------------------- version jouable
def simulate(ev: pd.DataFrame, series: dict) -> pd.DataFrame:
    """1 ligne x 1000 $, SL -10 / TP +25 / sortie a la cloture de la 5e seance."""
    ev = ev.assign(date=[series[r.ysym].idx[int(r.j)] for r in ev.itertuples()])
    ev = ev.sort_values(["date", "value"], ascending=[True, False])
    trades, busy_until = [], pd.Timestamp.min
    for date, g in ev.groupby("date", sort=True):
        if date <= busy_until:
            continue
        r = g.iloc[0]
        s, j = series[r.ysym], int(r.j)
        raw_open = s.raw(s.o, j)
        if not np.isfinite(raw_open) or raw_open <= 0 or np.floor(LINE / raw_open) < 1:
            continue
        qty = np.floor(LINE / raw_open)
        spread = 3e-4 if r.adv20 >= 20e6 else 10e-4
        entry, sl, tp = s.o[j], s.o[j] * (1 - SL_PCT), s.o[j] * (1 + TP_PCT)
        exit_px, t, why = None, j, ""
        for t in range(j, min(j + HOLD, s.n)):
            if t > j and s.o[t] <= sl:
                exit_px, why = s.o[t], "SL gap"
            elif t > j and s.o[t] >= tp:
                exit_px, why = s.o[t], "TP gap"
            elif s.l[t] <= sl:
                exit_px, why = sl, "SL"
            elif s.h[t] >= tp:
                exit_px, why = tp, "TP"
            if exit_px is not None:
                break
        if exit_px is None:
            exit_px, why = s.c[t], "5 seances"
        invested = qty * raw_open
        pnl = invested * (exit_px / entry - 1.0) - 2 * fee(qty) - invested * spread
        trades.append({"entry": date, "exit": s.idx[t], "ysym": r.ysym, "pnl": pnl, "ret": pnl / invested,
                       "why": why})
        busy_until = s.idx[t]
    return pd.DataFrame(trades)


def monthly(trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    months = pd.period_range(start, end, freq="M")
    if trades.empty:
        return pd.Series(0.0, index=months)
    m = trades.groupby(trades.exit.dt.to_period("M")).pnl.sum()
    return m.reindex(months, fill_value=0.0)


def main() -> int:
    rng = np.random.default_rng(2610)
    filings = pd.read_pickle(FILINGS)
    store = pd.read_pickle(PRICES)
    px, missing = store["px"], store["missing"]
    ft, series = filing_table(filings, px, missing)
    spy = Series(px["SPY"])
    last_day = spy.idx[-1]

    print("=" * 110)
    print("COPIER LES ACHATS DES DIRIGEANTS (Form 4, code P) — methode figee le 26/10/2026")
    print("=" * 110)
    print(f"depots candidats (dirigeant/admin, >= 25 k$, prix tx >= 3 $, depuis 2012): {len(ft)}")
    yr = ft.assign(y=ft.filing_date.dt.year).groupby("y").agg(n=("acc", "size"), data=("has_data", "mean"),
                                                             check=("check_ok", "mean"))
    print("\nBiais de survie: part des depots dont le ticker a des prix Yahoo, et part qui passe le controle")
    print("annee   depots  prix Yahoo  controle ticker ok")
    for y, r in yr.iterrows():
        print(f"{y}  {int(r.n):7d}     {100 * r.data:5.1f} %          {100 * r.check:5.1f} %")
    hd = ft[ft.has_data]
    print(f"controle ticker (parmi ceux avec prix): {100 * hd.check_ok.mean():.1f} % ok")

    variants = {
        "(a) dirigeant/admin >= 25 k$": ft,
        "(b) achats groupes (>= 2 en 10 j)": cluster_dates(ft),
        "(c) PDG/DAF >= 100 k$": ft[ft.is_ceo_cfo & (ft.value >= 100_000)],
    }
    results = {}
    for lab, sub in variants.items():
        ev = events_for(sub)
        meas = measure(ev, series, spy, rng)
        n_train = int((meas.date <= TRAIN_END).sum())
        train_end = TRAIN_END if n_train >= MIN_TRAIN_EVENTS else ALT_TRAIN_END
        results[lab] = (ev, meas, train_end)
        print("\n" + "-" * 110)
        print(f"{lab}: {len(meas)} evenements tradables (train jusqu'au {train_end.date()}, "
              f"{int((meas.date <= train_end).sum())} evenements)")
        print("horizon | periode | n     | net moyen | avantage vs temoin [IC95]     | exces vs SPY [IC95]")
        for h in HORIZONS:
            for name, part in (("train", meas[meas.date <= train_end]), ("test ", meas[meas.date > train_end])):
                y, _, _, n = boot_ci(part, f"y{h}", rng)
                x, xlo, xhi, _ = boot_ci(part, f"x{h}", rng)
                m, mlo, mhi, _ = boot_ci(part, f"m{h}", rng)
                print(f"{h:4d} s  | {name} | {n:5d} | {100 * y:+7.2f} % | {100 * x:+6.2f} % [{100 * xlo:+5.2f}, {100 * xhi:+5.2f}]"
                      f"   | {100 * m:+6.2f} % [{100 * mlo:+5.2f}, {100 * mhi:+5.2f}]")
        tr = meas[meas.date <= train_end]
        te = meas[meas.date > train_end]
        _, lo, _, _ = boot_ci(tr, f"x{DECISIVE_H}", rng)
        te_mean = te[f"x{DECISIVE_H}"].mean()
        verdict = "ADOPTE (critere rempli)" if (lo > 0 and te_mean > 0) else "REJETE (critere non rempli)"
        print(f"critere a {DECISIVE_H} seances: IC95 train bas {100 * lo:+.2f} %, moyenne test {100 * te_mean:+.2f} % -> {verdict}")

    print("\n" + "=" * 110)
    print("VERSION JOUABLE: 1 ligne x 1000 $, SL -10 % / TP +25 % / sortie a 5 seances, frais compris")
    print("=" * 110)
    spy_m = pd.Series(spy.c, index=spy.idx).resample("ME").last().pct_change() * LINE
    for lab, (ev, meas, train_end) in results.items():
        trades = simulate(ev, series)
        print(f"\n{lab}: {len(trades)} trades, gain moyen {trades.ret.mean() * 100:+.2f} %/trade "
              f"({trades.pnl.mean():+.2f} $), gagnants {100 * (trades.pnl > 0).mean():.0f} %")
        print("periode          | $/mois  | %/mois  | mois median | pire mois | mois < 0 | trades/mois | signaux/mois | SPY $/mois")
        for name, a, b in (("complet", START, last_day), ("train", START, train_end),
                           ("test", train_end + pd.Timedelta(days=1), last_day)):
            t = trades[(trades.exit >= a) & (trades.exit <= b)]
            m = monthly(t, a, b)
            sig = meas[(meas.date >= a) & (meas.date <= b)]
            sp = spy_m[(spy_m.index >= a) & (spy_m.index <= b)].dropna()
            print(f"{name:16s} | {m.mean():+7.2f} | {m.mean() / LINE * 100:+6.2f} % | {m.median():+11.2f} | "
                  f"{m.min():+9.2f} | {100 * (m < 0).mean():6.0f} % | {len(t) / len(m):11.1f} | "
                  f"{len(sig) / len(m):12.1f} | {sp.mean():+8.2f}")
        last18 = trades[trades.exit >= last_day - pd.DateOffset(months=18)]
        m18 = monthly(last18, last_day - pd.DateOffset(months=18), last_day)
        print(f"18 derniers mois | {m18.mean():+7.2f} | {m18.mean() / LINE * 100:+6.2f} % | pire mois {m18.min():+.2f} $")
        print("sorties: " + ", ".join(f"{k} {v}" for k, v in trades.why.value_counts().items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
