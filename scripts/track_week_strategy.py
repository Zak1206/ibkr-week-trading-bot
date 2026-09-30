# -*- coding: utf-8 -*-
"""
Suivi et VERDICT de la strategie "breakout semaine", selon docs/protocole-verdict-bot.md
(fige le 2026-09-30). Ne rien changer ici sans dater une modification dans le protocole.

1) Trades clos du journal paper depuis --since (defaut RISK_MAX_DD_START_DATE), dedoublonnes
   comme le bot. Rendement d'un trade = (PnL $ - commissions estimees) / montant investi,
   commissions = 2 x max(0.35$, 0.0035$/action). Score = somme des rendements.
2) Backtest de la MEME periode, memes regles (controle d'execution, non decisif).
3) 1000 bots aleatoires: memes tickers, meme periode, memes sorties, 1 position, memes couts;
   densite d'entrees calibree pour produire autant de trades que le paper; chaque bot compte
   sur ses N premiers trades. Rang = % des bots dont le score est inferieur a celui du paper.
4) Decision du protocole (section 5) + journal dans scripts/track_log.jsonl.

Usage:
  .\\.venv\\Scripts\\python.exe scripts\\track_week_strategy.py
  .\\.venv\\Scripts\\python.exe scripts\\track_week_strategy.py --hashes
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import dotenv_values

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import backtest_swing as B  # noqa: E402
import research_engine as E  # noqa: E402

NY = "America/New_York"
LOG = os.path.join(HERE, "track_log.jsonl")
HASHED = ["main.py", "ibkr_execution.py", "broker_hooks.py", ".env.ibkr_paper",
          "scripts/research_engine.py", "scripts/backtest_swing.py", "scripts/track_week_strategy.py",
          "docs/protocole-verdict-bot.md"]


def hashes():
    out = {}
    for rel in HASHED:
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            with open(p, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def _flat(df):
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(1, axis=1)
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])


def paper_trades(journal, since):
    """Trades clos dedoublonnes (meme logique que le bot) + heure d'achat (dernier fill BUY)."""
    import main as M
    buys = {}
    if os.path.exists(journal):
        with open(journal, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") == "ibkr_order_filled" and str(ev.get("side", "")).upper() == "BUY":
                    buys.setdefault(str(ev.get("ticker", "")).upper(), []).append(
                        pd.Timestamp(ev["ts_utc"]).tz_convert(NY))
    out = []
    for ev in M.load_trade_closed_events(journal):
        ts = pd.Timestamp(ev.get("ts_utc")).tz_convert(NY)
        if ts.date() < since:
            continue
        tk = str(ev.get("ticker", "")).upper()
        size = float(ev.get("size_usd", 0) or 0)
        px = float(ev.get("entry_price_usd", 0) or 0)
        qty = size / px if px > 0 else 0.0
        fees = 2 * max(0.35, 0.0035 * qty)
        pnl = float(ev.get("pnl_usd", 0) or 0)
        prior = [b for b in buys.get(tk, []) if b <= ts]
        out.append({"ticker": tk, "exit_ts": ts, "entry_ts": max(prior) if prior else None,
                    "pnl_usd": pnl, "size": size, "ret": (pnl - fees) / size if size > 0 else 0.0,
                    "reason": ev.get("exit_reason", "")})
    return sorted(out, key=lambda t: t["exit_ts"])


def random_trade_net(o, h, l, c, dayidx, lod, j, stop_pct, tp_pct, hold):
    """Rendement NET d'un trade entre a l'ouverture de la barre j, sorties de la strategie
    (stop, objectif, `hold` seances), couts du moteur (0.35$ min/ordre, 0.0035$/action, 3 bps).
    None si le trade n'est pas termine a la derniere barre disponible."""
    hs = 1.5e-4
    entry = o[j] * (1 + hs)
    if not entry > 0:
        return None
    slp, tpp, d0 = entry * (1 - stop_pct / 100), entry * (1 + tp_pct / 100), dayidx[j]
    exit_px = None
    for k in range(j, len(o)):
        if l[k] <= slp:
            exit_px = min(slp, o[k]); break
        if h[k] >= tpp:
            exit_px = max(tpp, o[k]); break
        if lod[k] and dayidx[k] - d0 >= hold:
            exit_px = c[k]; break
    if exit_px is None:
        return None
    qty = max(1, int(1000.0 // entry))
    fees = 2 * max(0.35, 0.0035 * qty)
    return (exit_px * (1 - hs) * qty - entry * qty - fees) / (entry * qty + fees / 2)


def decision(n, rank):
    if n < 30:
        return "AUCUN VERDICT avant 30 trades (%d/30) — protocole section 5" % n
    if n < 60:
        return ("ARRET: ne bat pas le hasard a 30 trades" if rank < 50
                else "ON CONTINUE jusqu'a 60 trades (%d/60)" % n)
    if rank >= 95:
        return "AVANTAGE DEMONTRE (rang >= 95%% a %d trades)" % n
    if rank >= 50:
        return "NON CONCLUANT (accepte d'avance): pas de passage en reel sur ce test"
    return "ARRET: ne bat pas le hasard a 60 trades"


def main():
    env = {**dotenv_values(os.path.join(ROOT, ".env")), **dotenv_values(os.path.join(ROOT, ".env.ibkr_paper"))}
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=env.get("RISK_MAX_DD_START_DATE") or "2026-09-29")
    ap.add_argument("--placebo", type=int, default=1000)
    ap.add_argument("--hashes", action="store_true")
    ap.add_argument("--no-log", action="store_true", help="essai technique: ne pas ecrire dans track_log.jsonl")
    args = ap.parse_args()
    if args.hashes:
        for k, v in hashes().items():
            print("%-34s %s" % (k, v))
        return
    since = pd.Timestamp(args.since).date()
    tickers = [t.strip().upper() for t in (env.get("TICKERS_OVERRIDE") or "").split(",") if t.strip()]
    stop = float(env.get("BUY_FIXED_STOP_PCT") or 10)
    tp = stop * float(env.get("BUY_RR_REWARD_MULT") or 2.5)
    hold = int(float(env.get("TRADING_MAX_HOLD_SESSIONS") or 5))
    vix_thr = float(env.get("RISK_OFF_VIX_THRESHOLD") or 22)
    paper = paper_trades(os.path.join(ROOT, env.get("TRADE_JOURNAL_FILE") or "trades_journal.jsonl"), since)
    n = len(paper)
    score = float(sum(t["ret"] for t in paper))

    data = {"h1": {}, "d1": {}, "earnings": {}}
    now = pd.Timestamp.now(tz=NY)
    for tk in tickers:
        h = _flat(yf.download(tk, period="730d", interval="1h", progress=False, auto_adjust=False))
        h.index = h.index.tz_localize(NY) if h.index.tz is None else h.index.tz_convert(NY)
        if len(h) and min(h.index[-1] + pd.Timedelta(hours=1),
                          h.index[-1].replace(hour=16, minute=0)) > now:
            h = h.iloc[:-1]   # barre en formation
        d = _flat(yf.download(tk, period="5y", interval="1d", progress=False, auto_adjust=False))
        d.index = d.index.tz_localize(None) if d.index.tz is not None else d.index
        data["h1"][tk], data["d1"][tk] = h, d
    vix = _flat(yf.download("^VIX", period="5y", interval="1d", progress=False, auto_adjust=False))
    vix.index = vix.index.tz_localize(None) if vix.index.tz is not None else vix.index
    data["vix"] = vix
    B.TICKERS, B.VIX_THRESHOLD = tickers, vix_thr
    sig = B.build_signals(data)
    sig = sig[sig["breakout"]].reset_index(drop=True)
    sig["kind"], sig["prio"] = "open", 0.0
    bars = E.Bars(data["h1"])
    ex = E.Exit("week", sl_pct=stop, tp_pct=tp, max_hold_tdays=hold)
    win = (pd.Timestamp(since).tz_localize(NY), bars.all_ts[-1])
    kw = dict(win=win, line_usd=1000.0, max_open=1, budget=1000.0)
    bt = E.simulate(bars, sig, ex, **kw)["trades"]

    print("=" * 104)
    print("VERDICT breakout semaine (protocole du 30/09/2026) — depuis %s | %s" % (since, ",".join(tickers)))
    print("=" * 104)
    print("Paper   : %3d trades clos | score %+.2f%% (somme des rendements nets) | %+.2f $ brut"
          % (n, 100 * score, sum(t["pnl_usd"] for t in paper)))
    print("Backtest: %3d trades meme periode | score %+.2f%%  (controle d'execution)"
          % (len(bt), 100 * sum(t["net"] / t["cost"] for t in bt)))
    matched = 0
    for t in paper:
        hit = [b for b in bt if b["ticker"] == t["ticker"] and t["entry_ts"] is not None
               and abs((pd.Timestamp(b["entry_ts"]).normalize() - t["entry_ts"].normalize()).days) <= 1]
        matched += bool(hit)
        print("  %-5s entree %s  sortie %s  %+6.2f%%  %-18s %s"
              % (t["ticker"], t["entry_ts"].strftime("%m-%d %H:%M") if t["entry_ts"] is not None else "?",
                 t["exit_ts"].strftime("%m-%d %H:%M"), 100 * t["ret"], t["reason"],
                 "= backtest" if hit else "ABSENT DU BACKTEST (verifier l'execution)"))
    if n:
        print("Trades paper retrouves dans le backtest: %d/%d" % (matched, n))

    # Bots aleatoires (protocole section 4, amende le 30/09): rendement d'un trade qui entrerait a
    # l'OUVERTURE de chaque barre de seance de la periode, memes sorties, memes couts, SANS aucun
    # filtre (ni VIX, ni tendance, ni breakout): on teste toute la regle d'entree. Chaque bot tire
    # exactement N de ces entrees sur toute la periode; le score etant une somme de rendements
    # par trade, un chevauchement de positions ne change rien.
    pool = []
    for tk in tickers:
        h = data["h1"][tk]
        o, hi_, lo_, c = (h[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
        dayidx, _ = pd.factorize(h.index.normalize())
        lod = np.r_[dayidx[1:] != dayidx[:-1], True]
        for j in np.where(h.index >= win[0])[0]:
            r = random_trade_net(o, hi_, lo_, c, dayidx, lod, int(j), stop, tp, hold)
            if r is not None:
                pool.append(r)
    pool = np.array(pool)
    rng = np.random.default_rng(20260930)

    def rank_of(score_x, n_x):
        if n_x <= 0 or len(pool) < n_x:
            return None, None
        sc = np.array([pool[rng.choice(len(pool), n_x, replace=False)].sum() for _ in range(args.placebo)])
        return 100.0 * float((sc < score_x).mean()), sc

    rank_p, sc_p = rank_of(score, n)
    bt_score = float(sum(t["net"] / t["cost"] for t in bt))
    rank_b, _ = rank_of(bt_score, len(bt))
    if rank_p is not None:
        print("\n%d bots aleatoires x %d entrees (parmi %d entrees possibles sur la periode): "
              "score median %+.2f%%, p5 %+.2f%%, p95 %+.2f%%"
              % (args.placebo, n, len(pool), 100 * np.median(sc_p), 100 * np.percentile(sc_p, 5),
                 100 * np.percentile(sc_p, 95)))
        print("RANG DU PAPER   : %5.1f%%" % rank_p)
    if rank_b is not None:
        print("RANG DU BACKTEST: %5.1f%%  (%d trades, meme moteur que les bots)" % (rank_b, len(bt)))
    ranks = [r for r in (rank_p, rank_b) if r is not None]
    rank = min(ranks) if ranks else None
    if rank is not None:
        print("Rang retenu (le plus bas des deux, protocole section 5): %.1f%%" % rank)
    verdict = decision(n, rank if rank is not None else 0.0)
    print("\nDECISION: %s" % verdict)
    if args.no_log:
        return
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": dt.datetime.now().isoformat(timespec="seconds"), "since": str(since),
                            "n_trades": n, "score_pct": round(100 * score, 3),
                            "rank_pct": None if rank is None else round(rank, 1), "decision": verdict,
                            "hashes": hashes()}) + "\n")


if __name__ == "__main__":
    main()
