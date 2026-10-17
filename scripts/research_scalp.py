# -*- coding: utf-8 -*-
"""
Backtest scalp (regles figées, aucun réglage) sur l'univers paper.

Yahoo ne garde que ~60 jours en 5 min et ~7 jours en 1 min : c'est la
fenetre. Meme capital (1000 $, 1 ligne), memes frais IBKR que le moteur
semaine (0,35 $ min / 0,0035 $/action par ordre + 3 bps de spread A/R).

Strategies, declarees avant de voir les chiffres :

  BO   breakout N barres, buffer 0,15 %, volume >= 1,15x, bougie verte.
       SL / TP / max hold selon l'intervalle (voir VARIANTS).
  ORB  1re barre de seance verte, stop d'achat au plus haut, SL au plus bas,
       TP = 1R, sortie EOD.
  FADE bougie rouge <= -0,8 %, achat a l'ouverture suivante, SL=TP=0,4 %,
       max 6 barres (5 min) ou 15 barres (1 min), EOD.

Le hasard : toutes les ouvertures RTH possibles, memes sorties, 1000 bots
tirant N entrees (N = trades clos de la strategie sequentiale).

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_scalp.py
"""
from __future__ import annotations

import json
import math
import os
import pickle
import random
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "scripts", ".research_scalp_cache.pkl")
OUT = os.path.join(ROOT, "scripts", ".research_scalp_out.json")
NY = "America/New_York"
TICKERS = ["PLTR", "SMCI", "IONQ", "HOOD", "SOFI", "COIN", "RIVN", "RKLB"]
BUDGET = 1000.0
LINE = 1000.0
MAX_OPEN = 1
FEE_MIN = 0.35
FEE_PER_SHARE = 0.0035
VIX_MAX = 22.0
BUFFER_PCT = 0.15
VOL_RATIO = 1.15
LOOKBACK = 12
N_RAND = 1000


def fees(qty: float) -> float:
    return max(FEE_MIN, FEE_PER_SHARE * qty)


def _flat(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(1, axis=1)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    return df.dropna(subset=["Close"])


def _ny(df: pd.DataFrame) -> pd.DataFrame:
    if df.index.tz is None:
        df.index = df.index.tz_localize(NY)
    else:
        df.index = df.index.tz_convert(NY)
    return df


def _rth(df: pd.DataFrame) -> pd.DataFrame:
    hm = df.index.hour * 60 + df.index.minute
    return df[(hm >= 9 * 60 + 30) & (hm < 16 * 60)].copy()


def load(refresh: bool = False) -> dict:
    if os.path.exists(CACHE) and not refresh:
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    out = {"m5": {}, "m1": {}, "vix": None}
    print("Telechargement 5m (60j) + 1m (7j) + VIX...", flush=True)
    for tk in TICKERS:
        m5 = _rth(_ny(_flat(yf.download(tk, period="60d", interval="5m",
                                        progress=False, auto_adjust=False))))
        m1 = _rth(_ny(_flat(yf.download(tk, period="7d", interval="1m",
                                        progress=False, auto_adjust=False))))
        out["m5"][tk] = m5
        out["m1"][tk] = m1
        print("  %-5s  5m=%5d  1m=%5d" % (tk, len(m5), len(m1)), flush=True)
    vix = _flat(yf.download("^VIX", period="6mo", interval="1d",
                            progress=False, auto_adjust=False))
    if vix.index.tz is not None:
        vix.index = vix.index.tz_localize(None)
    out["vix"] = vix
    with open(CACHE, "wb") as f:
        pickle.dump(out, f)
    return out


def prev_vix_ok(index: pd.DatetimeIndex, vix: pd.DataFrame) -> np.ndarray:
    close = vix["Close"].astype(float)
    dates = index.tz_convert(NY).normalize().tz_localize(None).values
    pos = np.searchsorted(close.index.values, dates, side="left") - 1
    out = np.zeros(len(index), dtype=bool)
    ok = pos >= 0
    out[ok] = close.values[pos[ok]] < VIX_MAX
    return out


@dataclass
class Pack:
    tk: str
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    day: np.ndarray
    lod: np.ndarray
    vix_ok: np.ndarray
    index: pd.DatetimeIndex
    pos: Dict[pd.Timestamp, int]


def pack_bars(h: Dict[str, pd.DataFrame], vix: pd.DataFrame) -> Dict[str, Pack]:
    out = {}
    for tk, df in h.items():
        o = df["Open"].astype(float).values
        hi = df["High"].astype(float).values
        lo = df["Low"].astype(float).values
        c = df["Close"].astype(float).values
        v = df["Volume"].astype(float).values
        days = df.index.normalize()
        codes, _ = pd.factorize(days)
        lod = np.zeros(len(df), dtype=bool)
        lod[:-1] = codes[1:] != codes[:-1]
        lod[-1] = True
        out[tk] = Pack(
            tk=tk, o=o, h=hi, l=lo, c=c, v=v, day=codes, lod=lod,
            vix_ok=prev_vix_ok(df.index, vix), index=df.index,
            pos={t: i for i, t in enumerate(df.index)},
        )
    return out


def _trade_path(p: Pack, i: int, entry: float, sl: Optional[float],
                tp: Optional[float], max_hold: Optional[int], eod: bool,
                hs: float, line: float) -> Optional[dict]:
    if entry <= 0:
        return None
    qty = math.floor(line / (entry * (1.0 + hs)))
    if qty < 1:
        return None
    fill = entry * (1.0 + hs)
    cost = fill * qty + fees(qty)
    last = len(p.o) - 1
    stop_i = last if max_hold is None else min(last, i + max_hold - 1)
    for j in range(i, stop_i + 1):
        op, hi, lo, cl = p.o[j], p.h[j], p.l[j], p.c[j]
        exit_px, reason = None, None
        if sl is not None and lo <= sl:
            exit_px, reason = min(sl, op), "SL"
        elif tp is not None and hi >= tp:
            exit_px, reason = max(tp, op), "TP"
        elif eod and p.lod[j]:
            exit_px, reason = cl, "EOD"
        elif max_hold is not None and j >= stop_i:
            exit_px, reason = cl, "TIME"
        if exit_px is not None:
            proceeds = exit_px * (1.0 - hs) * qty - fees(qty)
            return {
                "ticker": p.tk, "entry_ts": str(p.index[i]),
                "exit_ts": str(p.index[j]), "entry": fill, "exit": exit_px,
                "qty": qty, "net": proceeds - cost, "cost": cost,
                "reason": reason,
                "day_trade": bool(p.day[j] == p.day[i]),
            }
    proceeds = p.c[stop_i] * (1.0 - hs) * qty - fees(qty)
    return {
        "ticker": p.tk, "entry_ts": str(p.index[i]),
        "exit_ts": str(p.index[stop_i]), "entry": fill, "exit": p.c[stop_i],
        "qty": qty, "net": proceeds - cost, "cost": cost, "reason": "END",
        "day_trade": bool(p.day[stop_i] == p.day[i]),
    }


def exits_for(p: Pack, i: int, spec: dict, hs: float, fill_px: float,
              sig: Optional[dict] = None):
    sl = tp = None
    if spec.get("use_signal_stop") and sig is not None:
        sl = float(sig["stop_px"])
        if spec.get("tp_r"):
            r = fill_px * (1.0 + hs) - sl
            if r > 0:
                tp = fill_px * (1.0 + hs) + r * float(spec["tp_r"])
    else:
        if spec.get("sl_pct") is not None:
            sl = fill_px * (1.0 + hs) * (1.0 - spec["sl_pct"] / 100.0)
        if spec.get("tp_pct") is not None:
            tp = fill_px * (1.0 + hs) * (1.0 + spec["tp_pct"] / 100.0)
    return sl, tp, spec.get("max_hold"), spec.get("eod", True)


def signals_bo(packs: Dict[str, Pack]) -> List[dict]:
    rows = []
    for p in packs.values():
        n = len(p.c)
        if n <= LOOKBACK + 1:
            continue
        for i in range(LOOKBACK, n - 1):
            if p.lod[i] or not p.vix_ok[i]:
                continue
            if p.day[i + 1] != p.day[i]:
                continue
            window_h = p.h[i - LOOKBACK:i].max()
            vavg = p.v[i - LOOKBACK:i].mean()
            if not (window_h > 0 and vavg > 0):
                continue
            thresh = window_h * (1.0 + BUFFER_PCT / 100.0)
            if p.c[i] > thresh and p.c[i] > p.o[i] and p.v[i] / vavg >= VOL_RATIO:
                rows.append({"ts": p.index[i + 1], "ticker": p.tk, "kind": "open",
                             "prio": float(p.c[i] / window_h - 1.0)})
    return rows


def signals_orb(packs: Dict[str, Pack]) -> List[dict]:
    rows = []
    for p in packs.values():
        first = np.zeros(len(p.day), dtype=bool)
        first[0] = True
        first[1:] = p.day[1:] != p.day[:-1]
        idx = np.where(first)[0]
        vol = p.v[idx]
        avg = pd.Series(vol).rolling(14).mean().shift(1).values
        for k, i in enumerate(idx):
            if i + 1 >= len(p.c) or p.day[i + 1] != p.day[i]:
                continue
            if not p.vix_ok[i] or not (avg[k] > 0):
                continue
            if p.c[i] <= p.o[i] or vol[k] / avg[k] < 1.0:
                continue
            rows.append({
                "ts": p.index[i + 1], "ticker": p.tk, "kind": "stop",
                "level": float(p.h[i] + 0.01), "stop_px": float(p.l[i]),
                "prio": float(vol[k] / avg[k]),
            })
    return rows


def signals_fade(packs: Dict[str, Pack]) -> List[dict]:
    rows = []
    for p in packs.values():
        for i in range(len(p.c) - 1):
            if p.lod[i] or not p.vix_ok[i]:
                continue
            if p.day[i + 1] != p.day[i]:
                continue
            if p.o[i] > 0 and (p.c[i] / p.o[i] - 1.0) <= -0.008:
                rows.append({"ts": p.index[i + 1], "ticker": p.tk, "kind": "open",
                             "prio": float(p.o[i] / p.c[i] - 1.0)})
    return rows


def simulate(packs: Dict[str, Pack], sigs: List[dict], spec: dict,
             spread_bps: float = 3.0, seed: int = 0) -> dict:
    hs = spread_bps / 2.0 / 10000.0
    cash = BUDGET
    open_pos: Dict[str, dict] = {}
    pending: Dict[str, dict] = {}
    trades: List[dict] = []
    all_ts = sorted(set().union(*[set(p.index) for p in packs.values()]))
    by_ts: Dict[pd.Timestamp, List[dict]] = {}
    for r in sigs:
        by_ts.setdefault(r["ts"], []).append(r)
    rng = random.Random(seed)
    for k in by_ts:
        lst = by_ts[k]
        rng.shuffle(lst)
        lst.sort(key=lambda r: -float(r.get("prio", 0.0)))

    def open_at(tk, px, t, i, r):
        nonlocal cash
        sl, tp, hold, eod = exits_for(packs[tk], i, spec, hs, px, r)
        tr = _trade_path(packs[tk], i, px, sl, tp, hold, eod, hs, min(LINE, cash - FEE_MIN))
        if tr is None or tr["cost"] > cash:
            return False
        cash -= tr["cost"]
        open_pos[tk] = {"plan": tr, "i": i}
        return True

    last_day = None
    for t in all_ts:
        day = t.normalize()
        if last_day is not None and day != last_day:
            pending.clear()
        last_day = day
        for r in by_ts.get(t, []):
            tk = r["ticker"]
            if tk in open_pos or tk in pending or len(open_pos) + len(pending) >= MAX_OPEN:
                continue
            p = packs[tk]
            i = p.pos.get(t)
            if i is None:
                continue
            if r.get("kind", "open") == "open":
                open_at(tk, p.o[i], t, i, r)
            else:
                pending[tk] = r
        for tk in list(pending.keys()):
            p = packs[tk]
            i = p.pos.get(t)
            if i is None:
                continue
            lvl = float(pending[tk]["level"])
            if p.h[i] >= lvl:
                r = pending.pop(tk)
                open_at(tk, max(lvl, p.o[i]), t, i, r)
        for tk in list(open_pos.keys()):
            p = packs[tk]
            i = p.pos.get(t)
            if i is None:
                continue
            plan = open_pos[tk]["plan"]
            if pd.Timestamp(plan["exit_ts"]) == t:
                cash += plan["cost"] + plan["net"]
                trades.append(plan)
                del open_pos[tk]
    for tk in list(open_pos.keys()):
        plan = open_pos.pop(tk)["plan"]
        cash += plan["cost"] + plan["net"]
        trades.append(plan)
    return {"trades": trades, "final_cash": cash}


def metrics(trades: List[dict]) -> dict:
    if not trades:
        return {"n": 0, "net": 0.0, "pct": 0.0, "win": 0.0, "pf": 0.0,
                "bps": 0.0, "fees": 0.0, "day_trades": 0, "reasons": {},
                "by_ticker": {}}
    nets = np.array([t["net"] for t in trades])
    gp = float(nets[nets > 0].sum())
    gl = float(-nets[nets < 0].sum())
    reasons: Dict[str, int] = {}
    by_tk: Dict[str, dict] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        b = by_tk.setdefault(t["ticker"], {"n": 0, "net": 0.0})
        b["n"] += 1
        b["net"] += t["net"]
    return {
        "n": int(len(trades)),
        "net": float(nets.sum()),
        "pct": float(100.0 * nets.sum() / BUDGET),
        "win": float(100.0 * (nets > 0).mean()),
        "pf": float(gp / gl) if gl > 0 else None,
        "bps": float(1e4 * np.mean([t["net"] / t["cost"] for t in trades])),
        "fees": float(sum(fees(t["qty"]) * 2 for t in trades)),
        "day_trades": int(sum(1 for t in trades if t["day_trade"])),
        "reasons": reasons,
        "by_ticker": {k: {"n": v["n"], "net": round(v["net"], 2)} for k, v in by_tk.items()},
    }


def all_random_pool(packs: Dict[str, Pack], spec: dict, spread_bps: float) -> List[float]:
    hs = spread_bps / 2.0 / 10000.0
    nets = []
    for p in packs.values():
        for i in range(LOOKBACK, len(p.c)):
            if not p.vix_ok[i]:
                continue
            sl, tp, hold, eod = exits_for(p, i, spec, hs, p.o[i], None)
            tr = _trade_path(p, i, p.o[i], sl, tp, hold, eod, hs, LINE)
            if tr is not None:
                nets.append(tr["net"])
    return nets


def random_rank(pool: List[float], n: int, score: float, reps: int = N_RAND) -> dict:
    if n < 1 or len(pool) < n:
        return {"rank": None, "med": None, "p10": None, "p90": None}
    rng = random.Random(1)
    scores = []
    for _ in range(reps):
        pick = rng.sample(pool, n)
        scores.append(sum(pick))
    scores.sort()
    below = sum(1 for s in scores if s < score)
    return {
        "rank": round(100.0 * below / reps, 1),
        "med": round(float(np.median(scores)), 2),
        "p10": round(scores[int(0.1 * (reps - 1))], 2),
        "p90": round(scores[int(0.9 * (reps - 1))], 2),
    }


def split_half(trades: List[dict]) -> dict:
    if len(trades) < 4:
        return {"first": None, "second": None}
    mid = len(trades) // 2
    a, b = trades[:mid], trades[mid:]
    return {"first": round(sum(t["net"] for t in a), 2),
            "second": round(sum(t["net"] for t in b), 2)}


VARIANTS = {
    "m5": {
        "label": "5 min / ~60 j",
        "BO": dict(sl_pct=0.4, tp_pct=0.6, max_hold=6, eod=True),
        "FADE": dict(sl_pct=0.4, tp_pct=0.4, max_hold=6, eod=True),
        "ORB": dict(use_signal_stop=True, tp_r=1.0, eod=True, max_hold=None),
    },
    "m1": {
        "label": "1 min / ~7 j (exploratoire)",
        "BO": dict(sl_pct=0.25, tp_pct=0.40, max_hold=15, eod=True),
        "FADE": dict(sl_pct=0.25, tp_pct=0.25, max_hold=15, eod=True),
        "ORB": dict(use_signal_stop=True, tp_r=1.0, eod=True, max_hold=None),
    },
}


def run_frame(name: str, packs: Dict[str, Pack], cfg: dict) -> dict:
    builders = {"BO": signals_bo, "ORB": signals_orb, "FADE": signals_fade}
    out = {"label": cfg["label"], "range": None, "strats": {}}
    idx = next(iter(packs.values())).index
    out["range"] = [str(idx.min()), str(idx.max()), int(len(set(idx.normalize())))]
    for key, builder in builders.items():
        spec = cfg[key]
        sigs = builder(packs)
        res3 = simulate(packs, sigs, spec, spread_bps=3.0)
        m3 = metrics(res3["trades"])
        res10 = simulate(packs, sigs, spec, spread_bps=10.0)
        m10 = metrics(res10["trades"])
        pool_spec = spec if key != "ORB" else dict(sl_pct=0.4, tp_pct=0.6, max_hold=None, eod=True)
        pool = all_random_pool(packs, pool_spec, 3.0)
        rank = random_rank(pool, m3["n"], m3["net"])
        out["strats"][key] = {
            "n_signals": len(sigs),
            "spread_3bps": m3,
            "spread_10bps": {"n": m10["n"], "net": m10["net"], "pct": m10["pct"],
                             "win": m10["win"], "bps": m10["bps"]},
            "random": rank,
            "halves": split_half(res3["trades"]),
            "spec": {k: v for k, v in spec.items() if v is not None},
        }
        print("  %-4s  n=%4d  net=%+8.1f  wr=%.0f%%  bps=%+.1f  pdt=%d  rank=%s"
              % (key, m3["n"], m3["net"], m3["win"], m3["bps"], m3["day_trades"],
                 rank["rank"]), flush=True)
    return out


def main():
    refresh = "--refresh" in sys.argv
    data = load(refresh=refresh)
    report = {
        "universe": TICKERS,
        "budget_usd": BUDGET,
        "vix_max": VIX_MAX,
        "lookback_bars": LOOKBACK,
        "buffer_pct": BUFFER_PCT,
        "vol_ratio": VOL_RATIO,
        "rules": (
            "Signaux sur barre cloturee, fill a l'ouverture suivante. "
            "1 ligne 1000 $. SL intra-barre prioritaire sur TP. "
            "Frais IBKR identiques au moteur semaine."
        ),
        "frames": {},
    }
    for frame, key in (("m5", "m5"), ("m1", "m1")):
        print("=== %s ===" % VARIANTS[key]["label"], flush=True)
        packs = pack_bars(data[frame], data["vix"])
        report["frames"][frame] = run_frame(frame, packs, VARIANTS[key])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print("ecrit %s" % OUT, flush=True)


if __name__ == "__main__":
    main()
