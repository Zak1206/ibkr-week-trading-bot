# -*- coding: utf-8 -*-
"""
Moteur de simulation generique pour la recherche (barres 1h, 2 slots x 520$).

Memes conventions que backtest_swing.simulate (celles qui ont corrige les
+2674$ de biais en septembre), etendues a d'autres types d'ordres:

  kind "open"  : ordre au marche, rempli a l'OUVERTURE de la barre `ts`
                 (le signal a ete calcule sur la cloture de la barre d'avant).
                 Gere des cette barre (elle peut deja toucher le stop).
  kind "stop"  : ordre stop d'achat a `level`, actif de la barre `ts` jusqu'a
                 la fin de la seance. Rempli a max(level, open) — un gap
                 au-dessus du niveau se paie a l'ouverture. Occupe un slot tant
                 qu'il est en attente. Sur la barre de remplissage, si le plus
                 bas touche le stop on suppose le stop touche APRES l'entree
                 (hypothese pessimiste: l'ordre intra-barre est inconnu).
  kind "close" : ordre au close (MOC), rempli a la CLOTURE de la barre `ts`.
                 Gere a partir de la barre suivante.

Sorties: stop/TP en % (gap-aware), stop absolu par signal (ORB), trailing
"peak lock" (config live), flat live (>= x% a la derniere barre), sortie
forcee en fin de seance (day trade), plafond en SEANCES de detention, et
drapeau de sortie au close par ticker (retour a la moyenne).

Frais IBKR: max(0.35$, 0.0035$/action) par ordre + 3 bps de spread
aller-retour (la moitie a chaque passage), comme backtest_swing.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

FEE_MIN = 0.35
FEE_PER_SHARE = 0.0035


def fees(qty: float) -> float:
    return max(FEE_MIN, FEE_PER_SHARE * qty)


@dataclass
class Exit:
    name: str
    sl_pct: Optional[float] = None
    tp_pct: Optional[float] = None
    trailing: bool = False
    flat_min_profit: Optional[float] = None
    eod: bool = False
    max_hold_tdays: Optional[int] = None
    use_signal_stop: bool = False
    exit_flag: bool = False
    flag_next_open: bool = False
    # hybride: a la cloture du JOUR D'ENTREE, sortir si le P/L est < cut_first_close_pct
    cut_first_close_pct: Optional[float] = None


class Bars:
    """Tableaux numpy par ticker + index de seance, pour une boucle rapide."""

    def __init__(self, h1: Dict[str, pd.DataFrame]):
        self.tk = list(h1.keys())
        self.df = h1
        self.o, self.h, self.l, self.c = {}, {}, {}, {}
        self.pos: Dict[str, Dict[pd.Timestamp, int]] = {}
        self.dayidx: Dict[str, np.ndarray] = {}
        self.last_of_day: Dict[str, np.ndarray] = {}
        for tk, df in h1.items():
            self.o[tk] = df["Open"].astype(float).values
            self.h[tk] = df["High"].astype(float).values
            self.l[tk] = df["Low"].astype(float).values
            self.c[tk] = df["Close"].astype(float).values
            self.pos[tk] = {t: i for i, t in enumerate(df.index)}
            days = df.index.normalize()
            codes, _ = pd.factorize(days)
            self.dayidx[tk] = codes
            lod = np.zeros(len(df), dtype=bool)
            lod[:-1] = codes[1:] != codes[:-1]
            lod[-1] = True
            self.last_of_day[tk] = lod
        self.all_ts = sorted(set().union(*[set(d.index) for d in h1.values()]))


def simulate(bars: Bars, sig: pd.DataFrame, ex: Exit, *, budget: float = 1000.0,
             line_usd: float = 520.0, max_open: int = 2, spread_bps: float = 3.0,
             seed: Optional[int] = None, skip_first_days: int = 0,
             win: Optional[tuple] = None,
             exit_flags: Optional[Dict[str, np.ndarray]] = None,
             compound: bool = False,
             step_pct: Optional[float] = None, step_mode: str = "up",
             breaker_pct: Optional[float] = None) -> dict:
    hs = spread_bps / 2.0 / 10000.0
    ex_global = ex
    cash = budget
    peak_eq = budget   # plus haut de l'equity (fin de barre), pour les paliers "up"
    real_eq = real_peak = budget   # equity REALISEE (trades clos), pour le coupe-circuit
    halted = False
    open_pos: Dict[str, dict] = {}
    pending: Dict[str, dict] = {}
    trades: List[dict] = []
    equity: List[tuple] = []

    all_ts = bars.all_ts
    if win is not None:
        all_ts = [t for t in all_ts if win[0] <= t <= win[1]]
    if skip_first_days > 0 and all_ts:
        cut = all_ts[0] + pd.Timedelta(days=skip_first_days)
        all_ts = [t for t in all_ts if t >= cut]
    if not all_ts:
        return {"trades": [], "equity": [], "final_cash": cash}
    t0, t1 = all_ts[0], all_ts[-1]

    by_ts: Dict[pd.Timestamp, List[dict]] = {}
    for r in sig.to_dict("records"):
        if t0 <= r["ts"] <= t1:
            by_ts.setdefault(r["ts"], []).append(r)
    rng = random.Random(seed if seed is not None else 0)
    for k in by_ts:
        lst = by_ts[k]
        if seed is not None:
            rng.shuffle(lst)
        lst.sort(key=lambda r: -float(r.get("prio", 0.0)))

    def open_position(tk, px, t, i, r):
        nonlocal cash
        if halted:
            return False
        entry = px * (1.0 + hs)
        target = line_usd * float(r.get("size_mult", 1.0) or 1.0)
        if step_pct:
            # reinvestissement par PALIERS: ligne = base x (1+step)^k, k = nb de paliers
            # franchis par le plus haut du compte ("up") ou par le compte actuel ("updown")
            mv0 = 0.0
            for otk, op in open_pos.items():
                oi = bars.pos[otk].get(t)
                mv0 += (bars.c[otk][oi - 1] if oi is not None and oi >= 1 else op["entry"]) * op["qty"]
            ref = peak_eq if step_mode == "up" else (cash + mv0)
            k = math.floor(math.log(max(ref, 1e-9) / budget) / math.log(1.0 + step_pct) + 1e-9)
            if step_mode == "up":
                k = max(0, k)
            target *= (1.0 + step_pct) ** k
        if compound:
            # la ligne suit l'equity (cash + positions au dernier close connu)
            # (cloture de la barre PRECEDENTE: celle-ci vient seulement d'ouvrir)
            mv = 0.0
            for otk, op in open_pos.items():
                oi = bars.pos[otk].get(t)
                mv += (bars.c[otk][oi - 1] if oi is not None and oi >= 1 else op["entry"]) * op["qty"]
            target *= (cash + mv) / budget
        line = min(target, cash - FEE_MIN)
        if line < 50 or entry <= 0:
            return False
        qty = math.floor(line / entry)
        if qty < 1:
            return False
        cost = entry * qty + fees(qty)
        if cost > cash:
            return False
        cash -= cost
        pe = r.get("exit_obj")          # sortie propre au signal (strategies hybrides)
        if not isinstance(pe, Exit):     # absente, ou NaN apres concatenation de signaux
            pe = ex
        sl = None
        if pe.use_signal_stop and r.get("stop_px") is not None and not np.isnan(r["stop_px"]):
            sl = float(r["stop_px"])
        elif pe.sl_pct is not None:
            sl = entry * (1.0 - pe.sl_pct / 100.0)
        tp = entry * (1.0 + pe.tp_pct / 100.0) if pe.tp_pct is not None else None
        open_pos[tk] = {"entry": entry, "qty": qty, "cost": cost, "sl": sl, "tp": tp,
                        "entry_ts": t, "entry_day": bars.dayidx[tk][i], "peak": 0.0,
                        "tp_hit": False, "start_i": i, "ex": pe}
        return True

    def close_position(tk, px, t, reason, i):
        nonlocal cash, real_eq, real_peak, halted
        p = open_pos.pop(tk)
        proceeds = px * (1.0 - hs) * p["qty"] - fees(p["qty"])
        cash += proceeds
        real_eq += proceeds - p["cost"]
        real_peak = max(real_peak, real_eq)
        if breaker_pct is not None and (real_eq - real_peak) / real_peak * 100.0 <= -breaker_pct:
            halted = True
        trades.append({"ticker": tk, "entry_ts": p["entry_ts"], "exit_ts": t,
                       "entry": p["entry"], "exit": px, "qty": p["qty"],
                       "net": proceeds - p["cost"], "cost": p["cost"],
                       "ret_pct": (px / p["entry"] - 1.0) * 100.0, "reason": reason,
                       "tdays": int(bars.dayidx[tk][i] - p["entry_day"])})

    last_day = None
    for t in all_ts:
        day = t.normalize()
        if last_day is not None and day != last_day:
            pending.clear()  # ordres stop DAY non remplis -> annules
        sigs = by_ts.get(t, [])
        close_entries = []
        # 1) nouveaux ordres
        for r in sigs:
            tk = r["ticker"]
            if tk in open_pos or tk in pending or len(open_pos) + len(pending) >= max_open:
                continue
            i = bars.pos[tk].get(t)
            if i is None:
                continue
            kind = r.get("kind", "open")
            if kind == "open":
                open_position(tk, bars.o[tk][i], t, i, r)
            elif kind == "stop":
                pending[tk] = r
            elif kind == "close":
                close_entries.append(r)
        # 2) ordres stop en attente
        for tk in list(pending.keys()):
            i = bars.pos[tk].get(t)
            if i is None:
                continue
            r = pending[tk]
            lvl = float(r["level"])
            if bars.h[tk][i] >= lvl:
                del pending[tk]
                open_position(tk, max(lvl, bars.o[tk][i]), t, i, r)
        # 3) gestion des positions
        for tk in list(open_pos.keys()):
            i = bars.pos[tk].get(t)
            if i is None:
                continue
            p = open_pos[tk]
            ex = p.get("ex", ex_global)
            op, hi, lo, cl = bars.o[tk][i], bars.h[tk][i], bars.l[tk][i], bars.c[tk][i]
            exit_px, reason = None, None
            # drapeau leve a la cloture de la barre precedente -> sortie a
            # l'ouverture de celle-ci (variante sans aucune anticipation)
            if ex.flag_next_open and exit_flags is not None and i - 1 >= p["start_i"] \
                    and exit_flags[tk][i - 1]:
                exit_px, reason = op, "SIGNAL"
            elif ex.trailing and p["tp_hit"]:
                stop = max(p["tp"], p["peak"])
                if lo <= stop:
                    exit_px, reason = min(stop, op), "TRAIL"
                else:
                    p["peak"] = max(p["peak"], hi)
            else:
                if p["sl"] is not None and lo <= p["sl"]:
                    exit_px, reason = min(p["sl"], op), "SL"
                elif p["tp"] is not None and hi >= p["tp"]:
                    if ex.trailing:
                        p["tp_hit"], p["peak"] = True, hi
                    else:
                        exit_px, reason = max(p["tp"], op), "TP"
            lod = bars.last_of_day[tk][i]
            if exit_px is None and ex.exit_flag and not ex.flag_next_open \
                    and exit_flags is not None and exit_flags[tk][i]:
                exit_px, reason = cl, "SIGNAL"
            if exit_px is None and ex.cut_first_close_pct is not None and lod                     and bars.dayidx[tk][i] == p["entry_day"]                     and (cl / p["entry"] - 1.0) * 100.0 < ex.cut_first_close_pct:
                exit_px, reason = cl, "CUT1"
            if exit_px is None and ex.flat_min_profit is not None and lod:
                if (cl / p["entry"] - 1.0) * 100.0 >= ex.flat_min_profit:
                    exit_px, reason = cl, "FLAT"
            if exit_px is None and ex.eod and lod:
                exit_px, reason = cl, "EOD"
            if exit_px is None and ex.max_hold_tdays is not None and lod:
                if bars.dayidx[tk][i] - p["entry_day"] >= ex.max_hold_tdays:
                    exit_px, reason = cl, "TIME"
            if exit_px is not None:
                close_position(tk, exit_px, t, reason, i)
        # 4) ordres au close (MOC): geres a partir de la barre suivante
        for r in close_entries:
            tk = r["ticker"]
            if tk in open_pos or tk in pending or len(open_pos) + len(pending) >= max_open:
                continue
            i = bars.pos[tk][t]
            if open_position(tk, bars.c[tk][i], t, i, r):
                open_pos[tk]["start_i"] = i + 1
        # 5) equity de fin de barre, gardee une fois par seance (derniere barre)
        mv = 0.0
        for tk, p in open_pos.items():
            i = bars.pos[tk].get(t)
            px = bars.c[tk][i] if i is not None else p["entry"]
            mv += px * p["qty"]
        if equity and equity[-1][0] == day:
            equity[-1] = (day, cash + mv)
        else:
            equity.append((day, cash + mv))
        peak_eq = max(peak_eq, cash + mv)
        last_day = day

    # liquidation au dernier prix de la fenetre
    for tk in list(open_pos.keys()):
        idx = [i for i, ts in enumerate(bars.df[tk].index) if ts <= t1]
        i = idx[-1]
        close_position(tk, bars.c[tk][i], bars.df[tk].index[i], "END", i)
    return {"trades": trades, "equity": equity, "final_cash": cash}


def metrics(res: dict, budget: float = 1000.0) -> dict:
    tr = res["trades"]
    nets = np.array([t["net"] for t in tr]) if tr else np.zeros(0)
    n = len(nets)
    eq = pd.Series([e for _, e in res["equity"]],
                   index=[d for d, _ in res["equity"]], dtype=float)
    out = {"n": n, "net": float(nets.sum()) if n else 0.0}
    out["pct"] = 100.0 * out["net"] / budget
    if n:
        out["win"] = 100.0 * (nets > 0).mean()
        gp, gl = nets[nets > 0].sum(), -nets[nets < 0].sum()
        out["pf"] = gp / gl if gl > 0 else float("inf")
        sd = nets.std(ddof=1) if n > 1 else 0.0
        out["t"] = nets.mean() / (sd / math.sqrt(n)) if sd > 0 else 0.0
        rets = np.array([t["ret_pct"] for t in tr])
        out["bps"] = 1e4 * float(np.mean([t["net"] / t["cost"] for t in tr]))
        out["gross_bps"] = 100.0 * float(rets.mean())
        out["tdays"] = float(np.mean([t["tdays"] for t in tr]))
        out["fees"] = float(sum(fees(t["qty"]) * 2 for t in tr))
    else:
        out.update({"win": 0.0, "pf": 0.0, "t": 0.0, "bps": 0.0, "gross_bps": 0.0,
                    "tdays": 0.0, "fees": 0.0})
    if len(eq) > 2:
        peak = eq.cummax()
        out["dd_pct"] = float(((eq - peak) / peak).min() * 100.0)
        dr = eq.pct_change().dropna()
        out["sharpe"] = float(dr.mean() / dr.std() * math.sqrt(252)) if dr.std() > 0 else 0.0
        months = max(1e-9, (eq.index[-1] - eq.index[0]).days / 30.44)
        out["per_month"] = out["net"] / months
    else:
        out.update({"dd_pct": 0.0, "sharpe": 0.0, "per_month": 0.0})
    return out


def robust(bars: Bars, sig: pd.DataFrame, ex: Exit, reps: int = 20, win=None,
           exit_flags=None, **kw) -> dict:
    """Mediane et etendue sur `reps` perturbations (ordre des ex-aequo, date de depart)."""
    nets, runs = [], []
    for k in range(reps):
        r = simulate(bars, sig, ex, seed=100 + k, skip_first_days=(k % 5) * 4,
                     win=win, exit_flags=exit_flags, **kw)
        m = metrics(r, kw.get("budget", 1000.0))
        nets.append(m["net"])
        runs.append((m["net"], m, r))
    runs.sort(key=lambda x: x[0])
    med = runs[len(runs) // 2]
    return {"med": med[0], "p10": runs[int(0.1 * (reps - 1))][0],
            "p90": runs[int(0.9 * (reps - 1))][0],
            "pos": 100.0 * sum(1 for x in nets if x > 0) / reps, "m": med[1], "res": med[2]}
