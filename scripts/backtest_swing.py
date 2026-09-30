# -*- coding: utf-8 -*-
"""
Backtest: la strategie du bot avec une echelle de sortie swing (SL/TP larges,
pas de flat intraday) contre la config live actuelle.

Les signaux d'ENTREE sont identiques dans toutes les variantes (repliques a
l'identique de main.py), donc la comparaison isole l'effet de la SORTIE.

Fidelite (fonctions copiees mot pour mot depuis main.py):
  - add_indicators                 -> main.py:6331   (RSI/BB/ATR/MACD, pandas pur)
  - score de confiance             -> main.py:1819-1836
  - sniper_entry_ok                -> main.py:2466
  - get_higher_tf_context          -> main.py:6809   (trend_score -2..+2)
  - compute_intraday_breakout_metrics -> main.py:1355
  - anti-falling-knife             -> main.py:12145

Ecarts assumes (declares dans le rapport):
  - scan 1h / MTF journalier au lieu de scan 15m / MTF 1h: yfinance ne donne
    que 60 jours de 15m. Meme ratio de timeframes, decale d'un cran.
  - fund_score force a 50 (neutre, contribution nulle au score). Le cache
    fundamental_cache.json est un snapshot d'aujourd'hui: l'utiliser pour 2023
    serait du look-ahead. Impact reel: +-10 a 12 points sur un seuil a 72.
  - risk_off (VIX>=22) est, lui, reconstitue historiquement pour de vrai.
  - blocage earnings: best-effort via yfinance (historique souvent incomplet).

Usage:
  .\\.venv\\Scripts\\python.exe scripts\\backtest_swing.py
  .\\.venv\\Scripts\\python.exe scripts\\backtest_swing.py --refresh
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import random
import sys
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
random.seed(0)
np.random.seed(0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "scripts", ".backtest_cache.pkl")

# ---------------------------------------------------------------------------
# Config repliquee depuis .env (valeurs live du 2026-09-04)
# ---------------------------------------------------------------------------
TICKERS = ["PLTR", "SMCI", "IONQ", "HOOD", "SOFI", "COIN", "RIVN", "RKLB", "CVS"]

BUDGET_USD = float(os.getenv("BT_BUDGET_USD", "1000"))
TARGET_LINE_USD = float(os.getenv("BT_LINE_USD", "520"))
MAX_OPEN = int(os.getenv("BT_MAX_OPEN", "2"))

FEE_MIN = 0.35
FEE_PER_SHARE = 0.0035
SPREAD_BPS = 3.0  # median mesuree sur les ordres reels (edge_report)

# Entree (BUY_SNIPER_*)
SNIPER_MIN_CONF = 72
SNIPER_MIN_MTF = 1
SNIPER_ALT_MIN_CONF = 75
SNIPER_ALT_MIN_MTF = 2
BUY_MIN_RSI = 40.0

# Breakout (BREAKOUT_*)
BREAKOUT_LOOKBACK = 12
BREAKOUT_BUFFER_PCT = 0.15
BREAKOUT_VOL_RATIO = 1.15
BREAKOUT_GREEN_CANDLE = True
BREAKOUT_MIN_RSI = 40.0

# Sortie live (contrôle)
LIVE_STOP_PCT = 2.0
LIVE_RR_MULT = 1.75  # TP = +3.5%
FLAT_MIN_PROFIT_PCT = 1.2
FLAT_SKIP_IF_LOSS = True

VIX_THRESHOLD = 22.0
EARNINGS_BLOCK_DAYS = 2

# Re-entree (TRADING_REENTRY_*)
REENTRY_COOLDOWN_HOURS = 0.0  # non actif dans .env live

# Tickers pour lesquels le filtre earnings a reellement pu s'appliquer
EARNINGS_APPLIED: set = set()

# Mode "optimiste": reproduit les deux raccourcis classiques d'un backtest naif
# (remplissage pile au niveau meme sur gap, entree au close de la barre de
# signal). Sert uniquement a mesurer combien ces raccourcis gonflent le
# resultat. BT_OPTIMISTIC=1 pour l'activer.
OPTIMISTIC = os.getenv("BT_OPTIMISTIC", "0").strip() in {"1", "true", "yes"}

# Mode composé: la taille de ligne suit la croissance du compte, au lieu de
# rester figee a 520$ pendant que le cash s'accumule sans travailler.
COMPOUND = os.getenv("BT_COMPOUND", "0").strip() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Indicateurs — COPIE VERBATIM de main.py:6331
# ---------------------------------------------------------------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute RSI, Bollinger, ATR et MACD au DataFrame."""
    df = df.copy()
    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    # RSI 14 (Wilder)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss
    df["RSI_14"] = 100 - (100 / (1 + rs))

    # Bollinger Bands (20, 2)
    bb_mid = close.rolling(window=20, min_periods=20).mean()
    bb_std = close.rolling(window=20, min_periods=20).std(ddof=0)
    df["BBM_20_2.0"] = bb_mid
    df["BBU_20_2.0"] = bb_mid + (2 * bb_std)
    df["BBL_20_2.0"] = bb_mid - (2 * bb_std)

    # ATR 14 (Wilder)
    prev_close = close.shift(1)
    tr_components = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    tr = tr_components.max(axis=1)
    df["ATR_14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    # MACD (12,26,9)
    ema_fast = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema_slow = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=9, adjust=False, min_periods=9).mean()
    hist = macd_line - signal_line
    df["MACD_12_26_9"] = macd_line
    df["MACDs_12_26_9"] = signal_line
    df["MACDh_12_26_9"] = hist

    return df


# ---------------------------------------------------------------------------
# Chargement des donnees
# ---------------------------------------------------------------------------
def _flatten(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(1, axis=1)
    keep = ["Open", "High", "Low", "Close", "Volume"]
    df = df[[c for c in keep if c in df.columns]].copy()
    return df.dropna(subset=["Close"])


def load_data(refresh: bool = False) -> dict:
    if os.path.exists(CACHE) and not refresh:
        with open(CACHE, "rb") as f:
            return pickle.load(f)

    print("Telechargement des donnees (1h + 1j sur ~3 ans)...")
    data = {"h1": {}, "d1": {}, "earnings": {}}
    for tk in TICKERS:
        h = _flatten(yf.download(tk, period="730d", interval="1h",
                                 progress=False, auto_adjust=False), tk)
        d = _flatten(yf.download(tk, period="5y", interval="1d",
                                 progress=False, auto_adjust=False), tk)
        if h.index.tz is None:
            h.index = h.index.tz_localize("America/New_York")
        else:
            h.index = h.index.tz_convert("America/New_York")
        if d.index.tz is not None:
            d.index = d.index.tz_localize(None)
        data["h1"][tk] = h
        data["d1"][tk] = d
        try:
            ed = yf.Ticker(tk).get_earnings_dates(limit=60)
            dates = sorted({pd.Timestamp(x).tz_localize(None).normalize()
                            for x in ed.index}) if ed is not None else []
        except Exception:
            dates = []
        data["earnings"][tk] = dates
        print("  %-6s 1h=%5d  1j=%5d  earnings=%d" % (tk, len(h), len(d), len(dates)))

    vix = _flatten(yf.download("^VIX", period="5y", interval="1d",
                               progress=False, auto_adjust=False), "^VIX")
    if vix.index.tz is not None:
        vix.index = vix.index.tz_localize(None)
    data["vix"] = vix
    with open(CACHE, "wb") as f:
        pickle.dump(data, f)
    return data


# ---------------------------------------------------------------------------
# MTF journalier — replique de main.py:6809 (get_higher_tf_context)
# ---------------------------------------------------------------------------
def daily_trend_score(d1: pd.DataFrame) -> pd.Series:
    """trend_score -2..+2 par jour, calcule sur la cloture de CE jour."""
    ind = add_indicators(d1)
    close = d1["Close"].astype(float)
    ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
    rsi = ind["RSI_14"]
    macd = ind["MACD_12_26_9"]
    macd_sig = ind["MACDs_12_26_9"]

    score = pd.Series(0, index=d1.index, dtype=float)
    score += np.where(close > ema50, 1.0, -1.0)
    score += np.where(macd >= macd_sig, 1.0, -1.0)
    score += np.where(rsi >= 55, 1.0, np.where(rsi <= 45, -1.0, 0.0))
    score = score.clip(-2, 2)
    # indisponible tant que les indicateurs ne sont pas amorces
    score[ema50.isna() | rsi.isna() | macd_sig.isna()] = np.nan
    return score


# ---------------------------------------------------------------------------
# Breakout — replique de main.py:1355 (vectorise)
# ---------------------------------------------------------------------------
def breakout_flags(h: pd.DataFrame, rsi: pd.Series) -> pd.Series:
    lb = BREAKOUT_LOOKBACK
    range_high = h["High"].rolling(lb).max().shift(1)
    close = h["Close"]
    ok = close > range_high * (1.0 + BREAKOUT_BUFFER_PCT / 100.0)
    vol_avg = h["Volume"].rolling(lb).mean().shift(1)
    ok &= (vol_avg <= 0) | (h["Volume"] >= vol_avg * BREAKOUT_VOL_RATIO)
    if BREAKOUT_GREEN_CANDLE:
        ok &= close > h["Open"]
    ok &= rsi >= BREAKOUT_MIN_RSI
    return ok.fillna(False)


# ---------------------------------------------------------------------------
# Score de confiance — COPIE VERBATIM de main.py:1819-1836 (vectorise)
# ---------------------------------------------------------------------------
def confidence_score(
    macd: pd.Series,
    macd_sig: pd.Series,
    ht_score: pd.Series,
    rsi: pd.Series,
    breakout: pd.Series,
    bb_upper_touch: pd.Series,
    risk_off: pd.Series,
    fund_score: float = 50.0,
) -> pd.Series:
    score = pd.Series(50.0, index=macd.index)
    score += np.where(macd >= macd_sig, 12.0, -10.0)
    score += ht_score.astype(float) * 5.0
    score += max(-10.0, min(12.0, (fund_score - 50) * 0.35))
    score += np.where(breakout, 9.0, 0.0)
    score += np.where((rsi >= 45.0) & (rsi <= 72.0), 5.0, 0.0)
    score += np.where((rsi >= 80.0) & ~(breakout & (ht_score >= 1)), -9.0, 0.0)
    score += np.where(bb_upper_touch, 2.0, 0.0)
    score += np.where(risk_off, -6.0, 0.0)
    return score.clip(0, 100).round().astype(int)


# ---------------------------------------------------------------------------
# Gate sniper — replique de main.py:2466
# ---------------------------------------------------------------------------
def sniper_gate(
    conf: pd.Series,
    ht_score: pd.Series,
    macd_ok: pd.Series,
    breakout: pd.Series,
    risk_off: pd.Series,
) -> pd.Series:
    path_breakout = breakout & (conf >= SNIPER_MIN_CONF) & macd_ok & (ht_score >= SNIPER_MIN_MTF)
    path_struct = (conf >= SNIPER_ALT_MIN_CONF) & (ht_score >= SNIPER_ALT_MIN_MTF) & macd_ok
    return (path_breakout | path_struct) & ~risk_off


# ---------------------------------------------------------------------------
# Construction des signaux (identiques pour toutes les variantes de sortie)
# ---------------------------------------------------------------------------
def build_signals(data: dict) -> pd.DataFrame:
    vix = data["vix"]
    vix_close = vix["Close"].astype(float)
    risk_off_daily = (vix_close >= VIX_THRESHOLD)

    rows = []
    for tk in TICKERS:
        h = data["h1"][tk]
        d = data["d1"][tk]
        if h.empty or d.empty:
            continue
        ind = add_indicators(h)
        rsi = ind["RSI_14"]
        macd = ind["MACD_12_26_9"]
        macd_sig = ind["MACDs_12_26_9"]
        bbu = ind["BBU_20_2.0"]
        atr = ind["ATR_14"]
        bko = breakout_flags(h, rsi)

        # MTF: dernier jour CLOTURE strictement avant la date de la barre 1h.
        # searchsorted(side="left") - 1 => derniere date daily < date de la barre,
        # donc aucune information du jour en cours ne fuit dans le signal.
        bar_dates = h.index.tz_convert("America/New_York").normalize().tz_localize(None).values

        def prev_day_lookup(dates: np.ndarray, values: np.ndarray) -> np.ndarray:
            pos = np.searchsorted(dates, bar_dates, side="left") - 1
            out = np.full(len(bar_dates), np.nan)
            ok = pos >= 0
            out[ok] = values[pos[ok]]
            return out

        ts_day = daily_trend_score(d).dropna()
        ht = pd.Series(prev_day_lookup(ts_day.index.values,
                                       ts_day.values.astype(float)), index=h.index)

        # ATR JOURNALIER en % du close, du dernier jour cloture. C'est la bonne
        # echelle pour une detention de plusieurs jours (l'ATR 1h est ~5x plus
        # petit et ne dit rien de l'amplitude d'une semaine).
        d_ind = add_indicators(d)
        datr = (d_ind["ATR_14"] / d_ind["Close"] * 100.0).dropna()
        datr_pct = pd.Series(prev_day_lookup(datr.index.values,
                                             datr.values.astype(float)),
                             index=h.index)
        ro = pd.Series(prev_day_lookup(risk_off_daily.index.values,
                                       risk_off_daily.values.astype(float)),
                       index=h.index).fillna(0.0) > 0.5

        valid = ~(rsi.isna() | macd_sig.isna() | ht.isna() | atr.isna())
        ht_f = ht.fillna(0)
        macd_ok = macd >= macd_sig
        conf = confidence_score(macd, macd_sig, ht_f, rsi, bko,
                                h["Close"] >= bbu, ro)
        gate = sniper_gate(conf, ht_f, macd_ok, bko, ro)

        # anti-falling-knife (main.py:12145) + RSI mini
        knife = (rsi < BUY_MIN_RSI) & (macd < macd_sig)
        buy = gate & valid & ~knife

        # Blocage earnings +-2 jours. yfinance ne renvoie plus d'historique de
        # dates de resultats (0 date pour les 9 tickers au 2026-09-04), donc ce
        # filtre est INERTE ici. Consequence: le backtest prend des trades que
        # le bot live refuserait -> il SURESTIME legerement toutes les variantes,
        # de la meme facon, donc le classement entre variantes reste valide.
        ed = data["earnings"].get(tk) or []
        if ed:
            blocked = np.zeros(len(h), dtype=bool)
            for e in ed:
                blocked |= (np.abs((bar_dates - np.datetime64(e))
                                   .astype("timedelta64[D]").astype(int))
                            <= EARNINGS_BLOCK_DAYS)
            buy &= ~pd.Series(blocked, index=h.index)
            EARNINGS_APPLIED.add(tk)

        # Le signal est calcule sur la CLOTURE de la barre i, donc il n'est
        # executable qu'a l'OUVERTURE de la barre i+1. Entrer au close de i
        # serait un look-ahead (on tradrait un prix connu seulement a la fin).
        idx = np.where(buy.values)[0]
        idx = idx[idx < len(h) - 1]
        for i in idx:
            j = i if OPTIMISTIC else i + 1
            rows.append({
                "ts": h.index[j],
                "ticker": tk,
                "bar": j,
                "price": float(h["Close"].iloc[i]) if OPTIMISTIC
                else float(h["Open"].iloc[i + 1]),
                "conf": int(conf.iloc[i]),
                "mtf": int(ht_f.iloc[i]),
                "rsi": float(rsi.iloc[i]),
                "breakout": bool(bko.iloc[i]),
                "atr_pct": float(atr.iloc[i]) / float(h["Close"].iloc[i]) * 100.0,
                "datr_pct": float(datr_pct.iloc[i]) if not np.isnan(datr_pct.iloc[i]) else np.nan,
            })
    sig = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    return sig


# ---------------------------------------------------------------------------
# Simulation de portefeuille
# ---------------------------------------------------------------------------
@dataclass
class Variant:
    name: str
    stop_pct: float
    tp_pct: float
    flat: bool = False
    max_hold_days: Optional[int] = None
    trailing: bool = False
    # Si renseignes, le SL/TP est exprime en multiples de l'ATR JOURNALIER du
    # titre au lieu d'un % fixe: CVS (ATR 2.6%/j) et IONQ (8.3%/j) n'ont alors
    # plus la meme echelle de sortie. stop_pct/tp_pct servent de repli quand
    # l'ATR est indisponible, et de bornes de securite.
    atr_sl_mult: Optional[float] = None
    atr_tp_mult: Optional[float] = None

    def levels_pct(self, datr_pct: float) -> Tuple[float, float]:
        """Retourne (stop%, tp%) pour ce signal."""
        if self.atr_sl_mult is None or self.atr_tp_mult is None:
            return self.stop_pct, self.tp_pct
        if datr_pct is None or not (datr_pct > 0) or math.isnan(datr_pct):
            return self.stop_pct, self.tp_pct
        sl = self.atr_sl_mult * datr_pct
        tp = self.atr_tp_mult * datr_pct
        # bornes: un stop sous 3% se fait balayer par le bruit, au-dessus de
        # 25% il ne protege plus rien sur un compte de 1000$.
        return max(3.0, min(25.0, sl)), max(5.0, min(90.0, tp))


def fees(qty: float) -> float:
    return max(FEE_MIN, FEE_PER_SHARE * qty)


def simulate(sig: pd.DataFrame, data: dict, v: Variant,
             seed: Optional[int] = None,
             skip_first_days: int = 0,
             win: Optional[Tuple[pd.Timestamp, pd.Timestamp]] = None) -> dict:
    """
    seed: melange l'ordre des signaux SIMULTANES. Quand 3 signaux tombent sur la
    meme barre et qu'il ne reste qu'un slot, le choix est arbitraire — faire
    varier la graine mesure a quel point le resultat depend de cet arbitraire.
    skip_first_days: decale le depart, pour tester la sensibilite a la date.
    """
    half_spread = SPREAD_BPS / 2.0 / 10000.0
    cash = BUDGET_USD
    open_pos: Dict[str, dict] = {}
    trades: List[dict] = []
    equity_curve: List[Tuple[pd.Timestamp, float]] = []

    bars = {tk: data["h1"][tk] for tk in TICKERS}
    # index temporel global
    all_ts = sorted(set().union(*[set(b.index) for b in bars.values()]))
    ts_pos = {tk: {t: i for i, t in enumerate(bars[tk].index)} for tk in TICKERS}
    if win is not None:
        all_ts = [t for t in all_ts if win[0] <= t <= win[1]]
    if skip_first_days > 0 and all_ts:
        cut = all_ts[0] + pd.Timedelta(days=skip_first_days)
        all_ts = [t for t in all_ts if t >= cut]
    t_first = all_ts[0] if all_ts else None

    sig_by_ts: Dict[pd.Timestamp, List[dict]] = {}
    for r in sig.to_dict("records"):
        if t_first is not None and r["ts"] < t_first:
            continue
        sig_by_ts.setdefault(r["ts"], []).append(r)
    if seed is not None:
        rng = random.Random(seed)
        for k in sig_by_ts:
            if len(sig_by_ts[k]) > 1:
                rng.shuffle(sig_by_ts[k])

    last_day = None
    for t in all_ts:
        day = t.normalize()

        # --- 1) entrees d'abord: le signal de la barre precedente s'execute a
        # l'ouverture de CELLE-CI, donc cette meme barre peut deja toucher le
        # stop ou le TP. Les traiter apres les sorties offrirait une barre de
        # grace gratuite a chaque nouvelle position.
        for r in sig_by_ts.get(t, []):
            tk = r["ticker"]
            if tk in open_pos or len(open_pos) >= MAX_OPEN:
                continue
            entry = r["price"] * (1.0 + half_spread)
            # En mode composé, la ligne grandit avec le compte (520$ sur 1000$
            # -> 52% de l'equity), condition necessaire pour se comparer
            # honnetement a un buy & hold qui, lui, compose par construction.
            if COMPOUND:
                mv_now = 0.0
                for otk, op_ in open_pos.items():
                    oi = ts_pos[otk].get(t)
                    opx = float(bars[otk]["Close"].iloc[oi]) if oi is not None else op_["entry"]
                    mv_now += opx * op_["qty"]
                target = TARGET_LINE_USD * (cash + mv_now) / BUDGET_USD
            else:
                target = TARGET_LINE_USD
            line = min(target, cash - FEE_MIN)
            if line < 50 or entry <= 0:
                continue
            qty = math.floor(line / entry)
            if qty < 1:
                continue
            cost = entry * qty + fees(qty)
            if cost > cash:
                continue
            cash -= cost
            sl_pct, tp_pct = v.levels_pct(r.get("datr_pct", float("nan")))
            open_pos[tk] = {
                "entry": entry, "qty": qty, "cost": cost,
                "sl": entry * (1.0 - sl_pct / 100.0),
                "tp": entry * (1.0 + tp_pct / 100.0),
                "entry_ts": t, "entry_day": day, "peak": 0.0, "tp_hit": False,
                "conf": r["conf"], "mtf": r["mtf"], "atr_pct": r["atr_pct"],
            }

        # --- 2) gestion des positions ouvertes sur cette barre
        for tk in list(open_pos.keys()):
            p = open_pos[tk]
            b = bars[tk]
            i = ts_pos[tk].get(t)
            if i is None:
                continue
            op = float(b["Open"].iloc[i])
            hi = float(b["High"].iloc[i])
            lo = float(b["Low"].iloc[i])
            cl = float(b["Close"].iloc[i])
            exit_px = None
            reason = None

            # Remplissage conscient des gaps. Un stop touche par une barre qui
            # OUVRE deja sous le niveau est rempli a l'ouverture, pas au niveau
            # (sinon on encaisse un prix jamais cote). Symetriquement, une limite
            # de TP depassee a l'ouverture est remplie a l'ouverture, meilleure.
            def fill_stop(level: float) -> float:
                return level if OPTIMISTIC else min(level, op)

            def fill_limit(level: float) -> float:
                return level if OPTIMISTIC else max(level, op)

            # trailing "peak lock": une fois le TP touche, le stop suit le plus haut
            if v.trailing and p["tp_hit"]:
                stop = max(p["tp"], p["peak"])
                if lo <= stop:
                    exit_px, reason = fill_stop(stop), "TRAIL"
                else:
                    p["peak"] = max(p["peak"], hi)
            else:
                if lo <= p["sl"]:
                    # SL et TP dans la meme barre -> on suppose le SL d'abord
                    exit_px, reason = fill_stop(p["sl"]), "SL"
                elif hi >= p["tp"]:
                    if v.trailing:
                        p["tp_hit"] = True
                        p["peak"] = hi
                    else:
                        exit_px, reason = fill_limit(p["tp"]), "TP"

            # flat intraday (config live): derniere barre du jour
            if exit_px is None and v.flat:
                nxt = b.index[i + 1] if i + 1 < len(b) else None
                is_last_of_day = (nxt is None) or (nxt.normalize() != day)
                if is_last_of_day:
                    pnl_pct = (cl / p["entry"] - 1.0) * 100.0
                    if pnl_pct >= FLAT_MIN_PROFIT_PCT:
                        exit_px, reason = cl, "FLAT"
                    elif not FLAT_SKIP_IF_LOSS and pnl_pct < 0:
                        exit_px, reason = cl, "FLAT_LOSS"

            # cap de temps
            if exit_px is None and v.max_hold_days is not None:
                held = (day - p["entry_day"]).days
                if held >= v.max_hold_days:
                    nxt = b.index[i + 1] if i + 1 < len(b) else None
                    if (nxt is None) or (nxt.normalize() != day):
                        exit_px, reason = cl, "TIME"

            if exit_px is not None:
                gross = exit_px * (1.0 - half_spread)
                proceeds = gross * p["qty"] - fees(p["qty"])
                cash += proceeds
                net = proceeds - p["cost"]
                trades.append({
                    "ticker": tk, "entry_ts": p["entry_ts"], "exit_ts": t,
                    "entry": p["entry"], "exit": exit_px, "qty": p["qty"],
                    "net": net, "pnl_pct": (exit_px / p["entry"] - 1.0) * 100.0,
                    "reason": reason, "hold_days": (day - p["entry_day"]).days,
                    "conf": p["conf"], "mtf": p["mtf"], "atr_pct": p["atr_pct"],
                })
                del open_pos[tk]

        # --- 3) equity mark-to-market (1x/jour)
        if last_day != day:
            if last_day is not None:
                mv = 0.0
                for tk, p in open_pos.items():
                    i = ts_pos[tk].get(t)
                    px = float(bars[tk]["Close"].iloc[i]) if i is not None else p["entry"]
                    mv += px * p["qty"]
                equity_curve.append((day, cash + mv))
            last_day = day

    # Liquidation finale au dernier prix DE LA FENETRE simulee. Utiliser la
    # derniere barre des donnees completes ferait fuiter un prix futur dans une
    # simulation qui s'arrete plus tot (moitie train du split).
    t_last = all_ts[-1] if all_ts else None
    for tk, p in list(open_pos.items()):
        i_last = ts_pos[tk].get(t_last)
        if i_last is None:
            idx_ok = [i for i, ts in enumerate(bars[tk].index) if t_last is None or ts <= t_last]
            i_last = idx_ok[-1] if idx_ok else len(bars[tk]) - 1
        cl = float(bars[tk]["Close"].iloc[i_last])
        proceeds = cl * (1.0 - half_spread) * p["qty"] - fees(p["qty"])
        cash += proceeds
        trades.append({
            "ticker": tk, "entry_ts": p["entry_ts"], "exit_ts": bars[tk].index[i_last],
            "entry": p["entry"], "exit": cl, "qty": p["qty"],
            "net": proceeds - p["cost"], "pnl_pct": (cl / p["entry"] - 1.0) * 100.0,
            "reason": "EOD_BACKTEST", "hold_days": 0,
            "conf": p["conf"], "mtf": p["mtf"], "atr_pct": p["atr_pct"],
        })

    return {"variant": v, "trades": trades, "final_cash": cash,
            "equity": equity_curve}


# ---------------------------------------------------------------------------
# Statistiques
# ---------------------------------------------------------------------------
def boot_ci(vals: List[float], n: int = 4000) -> Tuple[float, float, float]:
    if len(vals) < 3:
        return 0.0, 0.0, 0.0
    m = sum(vals) / len(vals)
    arr = np.array(vals)
    draws = np.random.randint(0, len(arr), size=(n, len(arr)))
    means = np.sort(arr[draws].mean(axis=1))
    return m, float(means[int(0.025 * n)]), float(means[int(0.975 * n)])


def summarize(res: dict, weeks: float) -> dict:
    tr = res["trades"]
    nets = [t["net"] for t in tr]
    n = len(nets)
    tot = sum(nets)
    if n == 0:
        return {"name": res["variant"].name, "n": 0, "net": 0.0, "pct": 0.0,
                "per_week": 0.0, "t": 0.0, "lo": 0.0, "hi": 0.0,
                "win": 0.0, "hold": 0.0, "dd": 0.0}
    mean = tot / n
    sd = (sum((x - mean) ** 2 for x in nets) / max(1, n - 1)) ** 0.5
    t_stat = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    _, lo, hi = boot_ci(nets)

    # Drawdown en $ ET en % du sommet: -700$ apres avoir triple le capital
    # n'est pas la meme chose que -700$ sur 1000$.
    eq = [e for _, e in res["equity"]]
    dd = 0.0
    dd_pct = 0.0
    peak = eq[0] if eq else BUDGET_USD
    for e in eq:
        peak = max(peak, e)
        dd = min(dd, e - peak)
        if peak > 0:
            dd_pct = min(dd_pct, 100.0 * (e - peak) / peak)

    return {
        "name": res["variant"].name,
        "n": n,
        "net": tot,
        "pct": 100.0 * tot / BUDGET_USD,
        "per_week": n / weeks,
        "t": t_stat,
        "lo": lo * n, "hi": hi * n,
        "win": 100.0 * sum(1 for x in nets if x > 0) / n,
        "hold": sum(t["hold_days"] for t in tr) / n,
        "dd": dd,
        "dd_pct": dd_pct,
    }


def benchmarks(data: dict) -> dict:
    """Panier equipondere des 9, et controle realiste 2 lignes x 520$."""
    out = {}
    start_end = {}
    for tk in TICKERS:
        h = data["h1"][tk]
        start_end[tk] = (float(h["Close"].iloc[0]), float(h["Close"].iloc[-1]))

    # panier equipondere: BUDGET reparti sur 9
    per = BUDGET_USD / len(TICKERS)
    val = sum(per * (e / s) for s, e in start_end.values())
    out["panier9"] = 100.0 * (val - BUDGET_USD) / BUDGET_USD

    # controle realiste: 2 tickers tires au hasard, 520$ chacun (plafonne au budget)
    draws = []
    names = list(start_end.keys())
    for _ in range(2000):
        pick = random.sample(names, 2)
        line = min(TARGET_LINE_USD, BUDGET_USD / 2)
        v = sum(line * (start_end[p][1] / start_end[p][0]) for p in pick)
        v += BUDGET_USD - 2 * line
        draws.append(100.0 * (v - BUDGET_USD) / BUDGET_USD)
    draws.sort()
    out["ctrl2_mean"] = sum(draws) / len(draws)
    out["ctrl2_med"] = draws[len(draws) // 2]
    out["ctrl2_p10"] = draws[int(0.10 * len(draws))]
    out["ctrl2_p90"] = draws[int(0.90 * len(draws))]
    return out


def semester_split(res: dict) -> List[Tuple[str, int, float]]:
    tr = res["trades"]
    if not tr:
        return []
    buckets: Dict[str, List[float]] = {}
    for t in tr:
        ts = pd.Timestamp(t["exit_ts"])
        lab = "%d-S%d" % (ts.year, 1 if ts.month <= 6 else 2)
        buckets.setdefault(lab, []).append(t["net"])
    return [(k, len(v), sum(v)) for k, v in sorted(buckets.items())]


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--start", default=None, help="AAAA-MM-JJ (defaut: tout)")
    ap.add_argument("--end", default=None, help="AAAA-MM-JJ (defaut: tout)")
    ap.add_argument("--robust", type=int, default=0,
                    help="N perturbations par variante (test de dependance au chemin)")
    args = ap.parse_args()

    data = load_data(refresh=args.refresh)
    if args.start or args.end:
        lo = pd.Timestamp(args.start).tz_localize("America/New_York") if args.start else None
        hi = pd.Timestamp(args.end).tz_localize("America/New_York") if args.end else None
        for tk in TICKERS:
            h = data["h1"][tk]
            m = pd.Series(True, index=h.index)
            if lo is not None:
                m &= h.index >= lo
            if hi is not None:
                m &= h.index <= hi
            data["h1"][tk] = h[m.values]
    h0 = data["h1"][TICKERS[0]]
    t_start, t_end = h0.index[0], h0.index[-1]
    weeks = (t_end - t_start).days / 7.0

    print("=" * 100)
    print("BACKTEST SWING vs CONFIG LIVE")
    print("=" * 100)
    print("Fenetre: %s -> %s  (%.0f semaines, %.1f ans)"
          % (t_start.date(), t_end.date(), weeks, weeks / 52.0))
    print("Univers: %s" % ",".join(TICKERS))
    print("Capital %.0f$ | %d slots x %.0f$ | frais %.2f$/%.4f$/action | spread %.0f bps"
          % (BUDGET_USD, MAX_OPEN, TARGET_LINE_USD, FEE_MIN, FEE_PER_SHARE, SPREAD_BPS))

    print("\nConstruction des signaux d'entree (identiques dans toutes les variantes)...")
    sig = build_signals(data)
    print("  %d signaux d'achat brut sur la fenetre (%.1f/semaine avant contrainte de slots)"
          % (len(sig), len(sig) / weeks))
    if sig.empty:
        print("Aucun signal — verifier les filtres.")
        sys.exit(1)
    print("  repartition: %s" % sig["ticker"].value_counts().to_dict())

    variants = [
        Variant("CONTROLE live (-2/+3.5, flat, trail)", LIVE_STOP_PCT,
                LIVE_STOP_PCT * LIVE_RR_MULT, flat=True, trailing=True),
        Variant("CONTROLE live sans trail", LIVE_STOP_PCT,
                LIVE_STOP_PCT * LIVE_RR_MULT, flat=True),
        Variant("CONTROLE live sans flat", LIVE_STOP_PCT,
                LIVE_STOP_PCT * LIVE_RR_MULT, trailing=True),
        Variant("Swing A  -6/+15  sans cap", 6, 15),
        Variant("Swing A  -6/+15  cap 10j", 6, 15, max_hold_days=10),
        Variant("Swing A  -6/+15  cap 20j", 6, 15, max_hold_days=20),
        Variant("Swing B -10/+25  sans cap", 10, 25),
        Variant("Swing B -10/+25  cap 10j", 10, 25, max_hold_days=10),
        Variant("Swing B -10/+25  cap 20j", 10, 25, max_hold_days=20),
        Variant("Swing C -15/+50  sans cap", 15, 50),
        Variant("Swing C -15/+50  cap 10j", 15, 50, max_hold_days=10),
        Variant("Swing C -15/+50  cap 20j", 15, 50, max_hold_days=20),
    ]

    if args.robust > 0:
        print("\n" + "=" * 100)
        print("TEST DE DEPENDANCE AU CHEMIN — %d perturbations par variante" % args.robust)
        print("=" * 100)
        print("On ne change RIEN a la strategie. On fait seulement varier:")
        print("  - l'ordre des signaux simultanes (quel ticker prend le slot libre)")
        print("  - la date de depart (0 a 20 jours plus tard)")
        print("Si l'ecart entre perturbations est aussi grand que l'ecart entre")
        print("variantes de sortie, alors aucune variante ne 'gagne': c'est du bruit.\n")
        print("%-38s %9s %9s %9s %9s %9s"
              % ("variante", "median $", "min $", "max $", "etendue", "% >0"))
        print("-" * 100)
        for v in variants:
            nets = []
            for k in range(args.robust):
                r = simulate(sig, data, v, seed=1000 + k,
                             skip_first_days=(k % 5) * 5)
                nets.append(sum(t["net"] for t in r["trades"]))
            nets.sort()
            med = nets[len(nets) // 2]
            pos = 100.0 * sum(1 for x in nets if x > 0) / len(nets)
            print("%-38s %+9.1f %+9.1f %+9.1f %9.1f %8.0f%%"
                  % (v.name, med, nets[0], nets[-1], nets[-1] - nets[0], pos))
        print("\nFin du test de robustesse.")
        return

    print("\nSimulation de %d variantes de sortie..." % len(variants))
    results = []
    for v in variants:
        res = simulate(sig, data, v)
        results.append(res)
        s = summarize(res, weeks)
        print("  %-38s n=%4d  net %+9.2f$" % (v.name, s["n"], s["net"]))

    print("\n" + "=" * 100)
    print("RESULTATS")
    print("=" * 100)
    print("%-38s %5s %9s %8s %7s %6s %19s %6s %8s %7s %6s"
          % ("variante", "n", "net $", "%", "tr/sem", "t", "IC95% total $",
             "gagn%", "DD $", "DD %pic", "j/tr"))
    print("-" * 118)
    summaries = []
    for res in results:
        s = summarize(res, weeks)
        summaries.append(s)
        print("%-38s %5d %+9.1f %+8.1f %7.2f %6.2f [%+8.1f,%+8.1f] %5.1f%% %+8.1f %+7.1f%% %6.1f"
              % (s["name"], s["n"], s["net"], s["pct"], s["per_week"], s["t"],
                 s["lo"], s["hi"], s["win"], s["dd"], s["dd_pct"], s["hold"]))

    print("\n" + "=" * 100)
    print("REFERENCES (buy & hold pur, meme fenetre)")
    print("=" * 100)
    bm = benchmarks(data)
    print("  Panier equipondere des 9 tickers, achete jour 1 : %+.1f%%" % bm["panier9"])
    print("  Controle realiste 2 lignes x 520$ (2000 tirages) : moyenne %+.1f%%  mediane %+.1f%%"
          % (bm["ctrl2_mean"], bm["ctrl2_med"]))
    print("     -> intervalle p10-p90 des tirages: [%+.1f%% , %+.1f%%]"
          % (bm["ctrl2_p10"], bm["ctrl2_p90"]))

    best = max(summaries, key=lambda s: s["net"])
    ctrl = summaries[0]
    print("\n" + "=" * 100)
    print("QUI GAGNE")
    print("=" * 100)
    print("  Meilleure variante active : %s  ->  %+.2f$ (%+.2f%%)"
          % (best["name"], best["net"], best["pct"]))
    print("  Config live actuelle      : %s  ->  %+.2f$ (%+.2f%%)"
          % (ctrl["name"], ctrl["net"], ctrl["pct"]))
    print("  Ecart swing vs live       : %+.2f$" % (best["net"] - ctrl["net"]))
    print("  Buy & hold panier         : %+.1f%%" % bm["panier9"])
    print("  Buy & hold 2 lignes       : %+.1f%% (mediane des tirages)" % bm["ctrl2_med"])

    print("\n" + "=" * 100)
    print("DECOUPAGE PAR SEMESTRE (net $ par semestre de sortie)")
    print("=" * 100)
    labels = sorted({lab for res in results for lab, _, _ in semester_split(res)})
    print("%-38s %s" % ("variante", " ".join("%9s" % l for l in labels)))
    for res in results:
        sp = dict((lab, tot) for lab, _, tot in semester_split(res))
        print("%-38s %s" % (res["variant"].name,
                            " ".join("%+9.1f" % sp.get(l, 0.0) for l in labels)))

    out = os.path.join(ROOT, "scripts", "backtest_swing_results.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"window": [str(t_start), str(t_end)], "weeks": weeks,
                   "signals": len(sig), "benchmarks": bm,
                   "summaries": summaries}, f, indent=2, default=str)
    print("\nResultats detailles: %s" % out)


if __name__ == "__main__":
    main()
