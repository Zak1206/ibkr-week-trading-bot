# -*- coding: utf-8 -*-
"""
Comparaison de strategies COURTES (day trading -> 1 semaine max) contre la
config live, sur la MEME fenetre, le meme capital (1000$, 2 lignes x 520$),
les memes frais IBKR et le meme spread.

Aucun parametre n'est optimise ici. Chaque strategie est prise telle que
publiee (ou telle qu'elle tourne en live), et jugee sur la mediane de 20
perturbations + les deux moities de la fenetre.

  A  config live            signaux actuels, SL-2/TP+3.5, trail, flat>=1.2%
  B  breakout seul          meme sortie, seulement le chemin breakout
                            (flag existant BUY_SNIPER_BREAKOUT_REQUIRED=1)
  C  breakout day trade     breakout seul, SL-2%, sortie forcee a la cloture
  D  sortie semaine         signaux actuels, SL-10/TP+25, 5 seances max
  R  + regime               idem, uniquement si SPY > SMA200 la veille
  O  ORB 60 min             "stocks in play" (Zarattini, Barbon & Aziz 2024)
                            adapte en 1h: 1re heure haussiere, volume relatif
                            >= 1, stop d'achat au plus haut, stop au plus bas,
                            sortie a la cloture. Top 2 par volume relatif.
  M  RSI(2) Connors         close > SMA200 et RSI2 < 10 -> achat MOC (prix de
                            15h30 comme proxy), sortie close > SMA5 ou 5 seances

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_strategies.py [--reps 20]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_swing as B  # noqa: E402
import research_data as RD  # noqa: E402
import research_engine as E  # noqa: E402

NY = "America/New_York"
WIN = (pd.Timestamp("2023-10-31 09:30", tz=NY), pd.Timestamp("2026-09-04 16:00", tz=NY))
BOT = B.TICKERS
ETF_MR = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY",
          "XLP", "XLU", "XLB", "SMH"]


# ---------------------------------------------------------------------------
def prev_day_values(bar_index: pd.DatetimeIndex, daily: pd.Series) -> np.ndarray:
    """Valeur du dernier jour CLOTURE strictement avant la date de chaque barre."""
    bar_dates = bar_index.tz_convert(NY).normalize().tz_localize(None).values
    pos = np.searchsorted(daily.index.values, bar_dates, side="left") - 1
    out = np.full(len(bar_dates), np.nan)
    ok = pos >= 0
    out[ok] = daily.values[pos[ok]]
    return out


def spy_regime(rdata: dict) -> pd.Series:
    c = rdata["d1raw"]["SPY"]["Close"].astype(float)
    sma = c.rolling(200).mean()
    return ((c > sma) & sma.notna()).astype(float)


def bot_signals(bdata: dict, vix: float = 22.0) -> pd.DataFrame:
    B.VIX_THRESHOLD = vix
    s = B.build_signals(bdata)
    B.VIX_THRESHOLD = 22.0
    s["kind"] = "open"
    s["prio"] = 0.0
    return s


def with_regime(sig: pd.DataFrame, h1: dict, reg: pd.Series) -> pd.DataFrame:
    keep = np.zeros(len(sig), dtype=bool)
    for tk in sig["ticker"].unique():
        m = (sig["ticker"] == tk).values
        idx = pd.DatetimeIndex(sig.loc[m, "ts"])
        keep[m] = prev_day_values(idx, reg) > 0.5
    return sig[keep].reset_index(drop=True)


def orb_signals(h1: dict, tickers: list, rvol_min: float = 1.0,
                lookback: int = 14) -> pd.DataFrame:
    rows = []
    for tk in tickers:
        h = h1[tk]
        days = h.index.normalize()
        first = ~pd.Series(days).duplicated().values
        is930 = (h.index.hour == 9) & (h.index.minute == 30)
        idx = np.where(first & is930)[0]
        vol = h["Volume"].values.astype(float)[idx]
        avg = pd.Series(vol).rolling(lookback).mean().shift(1).values  # jours PRECEDENTS
        o, hi, lo, c = (h[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
        for k, i in enumerate(idx):
            if i + 1 >= len(h) or days[i + 1] != days[i]:
                continue
            if not (avg[k] > 0):
                continue
            rv = vol[k] / avg[k]
            if c[i] <= o[i] or rv < rvol_min:
                continue
            rows.append({"ts": h.index[i + 1], "ticker": tk, "bar": i + 1, "kind": "stop",
                         "level": hi[i] + 0.01, "stop_px": lo[i], "prio": rv,
                         "or_range_pct": (hi[i] / lo[i] - 1) * 100})
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def rsi2_signals(h1: dict, d1: dict, tickers: list):
    """Connors RSI(2). Le prix de 15h30 (ouverture de la derniere barre 1h)
    remplace le close du jour dans RSI2/SMA200/SMA5: la decision est donc prise
    AVANT la cloture, et l'ordre part au close (MOC, limite NYSE 15h50)."""
    rows, flags = [], {}
    for tk in tickers:
        h = h1[tk]
        d = d1[tk]["Close"].astype(float)
        delta = d.diff()
        ag = delta.clip(lower=0).ewm(alpha=0.5, adjust=False, min_periods=2).mean()
        al = (-delta.clip(upper=0)).ewm(alpha=0.5, adjust=False, min_periods=2).mean()
        s199 = d.rolling(199).sum()
        s4 = d.rolling(4).sum()
        idx = np.where((h.index.hour == 15) & (h.index.minute == 30))[0]
        bar_dates = h.index[idx].normalize().tz_localize(None).values
        pos = np.searchsorted(d.index.values, bar_dates, side="left") - 1
        ok = pos >= 0
        idx, pos = idx[ok], pos[ok]
        proxy = h["Open"].values.astype(float)[idx]
        ch = proxy - d.values[pos]
        agp = 0.5 * np.maximum(ch, 0) + 0.5 * ag.values[pos]
        alp = 0.5 * np.maximum(-ch, 0) + 0.5 * al.values[pos]
        with np.errstate(divide="ignore", invalid="ignore"):
            rsi = np.where(alp > 0, 100 - 100 / (1 + agp / alp), 100.0)
        sma200 = (s199.values[pos] + proxy) / 200.0
        sma5 = (s4.values[pos] + proxy) / 5.0
        entry = (proxy > sma200) & (rsi < 10) & ~np.isnan(sma200)
        fl = np.zeros(len(h), dtype=bool)
        fl[idx[proxy > sma5]] = True
        flags[tk] = fl
        for i, r in zip(idx[entry], rsi[entry]):
            rows.append({"ts": h.index[i], "ticker": tk, "bar": int(i), "kind": "close",
                         "prio": -float(r)})
    sig = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True) if rows else pd.DataFrame()
    return sig, flags


# ---------------------------------------------------------------------------
def overnight_screen(bdata, rdata):
    print("=" * 100)
    print("SCREEN 1 — effet overnight: acheter au close (MOC), revendre a l'ouverture (MOO)")
    print("=" * 100)
    print("Cout aller-retour: 2 x 0.35$ + 3 bps = ~16.5 bps sur 520$, ~10 bps sur 1000$.")
    print("%-6s %12s %12s %12s   %s" % ("", "overnight", "intraday", "jours", "fenetre"))
    for tk in ["SPY", "QQQ", "IWM"] + BOT:
        d = rdata["d1"].get(tk)
        for lab, lo in (("fenetre", "2023-10-31"), ("depuis 2005", "2005-01-01")):
            if lab == "depuis 2005" and tk in BOT:
                continue
            x = d[(d.index >= lo) & (d.index <= "2026-09-04")]
            on = (x["Open"] / x["Close"].shift(1) - 1).dropna()
            intr = (x["Close"] / x["Open"] - 1)
            print("%-6s %+10.1f bps %+10.1f bps %8d     %s" % (tk, on.mean() * 1e4, intr.mean() * 1e4, len(on), lab))
    print()


def intraday_momentum_screen(bdata, rdata, h1):
    print("=" * 100)
    print("SCREEN 2 — momentum intraday (Gao, Han, Li & Zhou 2018): la 1re heure predit-elle la derniere ?")
    print("=" * 100)
    print("rendement 15h30->16h00 selon le signe du rendement veille close -> 10h30.")
    print("%-6s %14s %14s %8s" % ("", "si 1re h > 0", "si 1re h < 0", "jours"))
    for tk in ["SPY", "QQQ", "IWM"] + BOT:
        h = h1[tk]
        h = h[(h.index >= WIN[0]) & (h.index <= WIN[1])]
        days = h.index.normalize()
        g = pd.DataFrame({"o": h["Open"].values, "c": h["Close"].values,
                          "hr": h.index.hour, "mn": h.index.minute}, index=days)
        first = g[(g.hr == 9) & (g.mn == 30)]["c"]
        last = g[(g.hr == 15) & (g.mn == 30)]
        prev_close = g.groupby(level=0)["c"].last().shift(1)
        df = pd.DataFrame({"r1": first / prev_close.reindex(first.index) - 1})
        df["rl"] = (last["c"] / last["o"] - 1).reindex(df.index)
        df = df.dropna()
        up, dn = df[df.r1 > 0].rl, df[df.r1 < 0].rl
        print("%-6s %+10.1f bps %+10.1f bps %8d" % (tk, up.mean() * 1e4, dn.mean() * 1e4, len(df)))
    print()


# ---------------------------------------------------------------------------
def run_row(label, bars, sig, ex, reps, exit_flags=None, **kw):
    t = time.time()
    mid = WIN[0] + (WIN[1] - WIN[0]) / 2
    full = E.robust(bars, sig, ex, reps=reps, win=WIN, exit_flags=exit_flags, **kw)
    h1r = E.robust(bars, sig, ex, reps=max(6, reps // 2), win=(WIN[0], mid), exit_flags=exit_flags, **kw)
    h2r = E.robust(bars, sig, ex, reps=max(6, reps // 2), win=(mid, WIN[1]), exit_flags=exit_flags, **kw)
    m = full["m"]
    weeks = (WIN[1] - WIN[0]).days / 7.0
    print("%-34s %+8.0f [%+6.0f,%+6.0f] %4.0f%% | %5d %5.1f %6.1f %+6.1f %5.2f %6.1f %5.2f %4.1f | %+7.0f %+7.0f  (%.0fs)"
          % (label, full["med"], full["p10"], full["p90"], full["pos"],
             m["n"], m["n"] / weeks, m["win"], m["bps"], m["t"], m["dd_pct"], m["sharpe"],
             m["tdays"], h1r["med"], h2r["med"], time.time() - t), flush=True)
    return {"label": label, "full": full, "h1": h1r["med"], "h2": h2r["med"]}


def buy_hold(rdata, bdata):
    out = {}
    for tk in ["SPY", "QQQ"]:
        d = rdata["d1"][tk]
        x = d[(d.index >= "2023-10-31") & (d.index <= "2026-09-04")]["Close"]
        out[tk] = 100 * (x.iloc[-1] / x.iloc[0] - 1)
    vals = []
    for tk in BOT:
        h = bdata["h1"][tk]
        x = h[(h.index >= WIN[0]) & (h.index <= WIN[1])]["Close"]
        vals.append(x.iloc[-1] / x.iloc[0])
    out["panier9"] = 100 * (np.mean(vals) - 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    bdata = B.load_data()
    rdata = RD.load()
    h1 = dict(bdata["h1"])
    for tk in ETF_MR:
        h1[tk] = rdata["h1"][tk]
    d1 = dict(bdata["d1"])
    for tk in ETF_MR:
        d1[tk] = rdata["d1raw"][tk]

    overnight_screen(bdata, rdata)
    intraday_momentum_screen(bdata, rdata, h1)

    bars_bot = E.Bars({tk: h1[tk] for tk in BOT})
    bars_etf = E.Bars({tk: h1[tk] for tk in ETF_MR})
    reg = spy_regime(rdata)

    s_all = bot_signals(bdata, 22.0)
    s_all26 = bot_signals(bdata, 26.0)
    s_bko = s_all[s_all["breakout"]].reset_index(drop=True)
    s_orb = orb_signals(h1, BOT)
    s_mr, fl_mr = rsi2_signals(h1, d1, BOT)
    s_mre, fl_mre = rsi2_signals(h1, d1, ETF_MR)
    print("signaux: bot=%d  bot(VIX26)=%d  breakout=%d  ORB=%d  RSI2 bot=%d  RSI2 ETF=%d"
          % (len(s_all), len(s_all26), len(s_bko), len(s_orb), len(s_mr), len(s_mre)))

    live = E.Exit("live", sl_pct=2, tp_pct=3.5, trailing=True, flat_min_profit=1.2)
    day = E.Exit("day", sl_pct=2, eod=True)
    week = E.Exit("week", sl_pct=10, tp_pct=25, max_hold_tdays=5)
    orb_x = E.Exit("orb", use_signal_stop=True, eod=True)
    mr_x = E.Exit("mr", exit_flag=True, max_hold_tdays=5)

    print()
    print("=" * 150)
    print("COMPARAISON — fenetre %s -> %s, 1000$, 2 lignes x 520$, frais IBKR + 3 bps, mediane de %d perturbations"
          % (WIN[0].date(), WIN[1].date(), args.reps))
    print("=" * 150)
    print("%-34s %8s %15s %5s | %5s %5s %6s %6s %5s %6s %5s %4s | %7s %7s"
          % ("strategie", "net $", "p10-p90", ">0", "n", "tr/s", "gagn%", "bps/tr", "t", "DD%", "Shrp",
             "j", "1re moi", "2e moi"))
    print("-" * 150)
    rows = []
    todo = [
        ("A  config live (VIX 22, harnais)", bars_bot, s_all, live, None),
        ("A' config live (VIX 26, .env)", bars_bot, s_all26, live, None),
        ("B  breakout seul, sortie live", bars_bot, s_bko, live, None),
        ("C  breakout seul, day trade", bars_bot, s_bko, day, None),
        ("D  signaux actuels, sortie semaine", bars_bot, s_all, week, None),
        ("D2 breakout seul, sortie semaine", bars_bot, s_bko, week, None),
        ("RA config live + regime SPY", bars_bot, with_regime(s_all, h1, reg), live, None),
        ("RC breakout day trade + regime", bars_bot, with_regime(s_bko, h1, reg), day, None),
        ("RD sortie semaine + regime", bars_bot, with_regime(s_all, h1, reg), week, None),
        ("O  ORB 60 min (univers bot)", bars_bot, s_orb, orb_x, None),
        ("RO ORB 60 min + regime", bars_bot, with_regime(s_orb, h1, reg), orb_x, None),
        ("M  RSI2 Connors (univers bot)", bars_bot, s_mr, mr_x, fl_mr),
        ("ME RSI2 Connors (14 ETF)", bars_etf, s_mre, mr_x, fl_mre),
    ]
    for label, bars, sig, ex, fl in todo:
        if args.only and not any(label.startswith(x) for x in args.only.split(",")):
            continue
        if sig is None or len(sig) == 0:
            print("%-34s aucun signal" % label)
            continue
        rows.append(run_row(label, bars, sig, ex, args.reps, exit_flags=fl))

    bh = buy_hold(rdata, bdata)
    print("-" * 150)
    print("Repere (pas un objectif): buy & hold SPY %+.0f%%, QQQ %+.0f%%, panier des 9 %+.0f%% sur la fenetre."
          % (bh["SPY"], bh["QQQ"], bh["panier9"]))
    print("Colonnes: net $ = mediane; >0 = % des perturbations positives; bps/tr = gain NET moyen par trade;")
    print("t = t-stat des trades; DD% = pire drawdown du pic; j = seances de detention moyennes;")
    print("1re/2e moi = mediane sur chaque moitie de la fenetre (test de stabilite).")


if __name__ == "__main__":
    main()
