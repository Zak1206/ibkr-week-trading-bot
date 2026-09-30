# -*- coding: utf-8 -*-
"""
Test de PARITE: le vrai code du bot (main.py, profil .env + .env.ibkr_paper)
prend-il les memes decisions d'achat que le backtest, sur les memes barres ?

Pour chaque point (ticker, barre 1h cloturee i):
  - Yahoo est remplace par les donnees historiques: 3 mois de barres 1h jusqu'a i
    inclus (barres cloturees), 1 an de journalier jusqu'a la VEILLE;
  - on appelle analyze_ticker -> get_higher_tf_context -> build_rules_scan_decision,
    c'est-a-dire le pipeline de decision du bot, sans IBKR ni reseau;
  - on compare a backtest_swing.build_signals (breakout seul, VIX 22).
Echantillon: tous les signaux du backtest d'un sous-ensemble + des barres au hasard.

Usage: .\\.venv\\Scripts\\python.exe scripts\\parity_week_strategy.py [--n 400]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
from dotenv import dotenv_values

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

# profil paper exactement comme au lancement (.env puis surcharge)
_env = {**dotenv_values(os.path.join(ROOT, ".env")), **dotenv_values(os.path.join(ROOT, ".env.ibkr_paper"))}
for k, v in _env.items():
    if v is not None:
        os.environ[k] = v
os.environ["IBKR_ENABLED"] = "0"          # aucune connexion
os.environ["IBKR_AUTO_EXECUTE"] = "0"
for _k in list(os.environ):                # ni Telegram ni LLM depuis un test
    if any(s in _k.upper() for s in ("TOKEN", "API_KEY", "CHAT_ID")):
        os.environ[_k] = ""

import backtest_swing as B  # noqa: E402
import main as M  # noqa: E402

FEED = {}


def fake_fetch(ticker, period="5d", interval="15m", **kw):
    return FEED[(M.normalize_ticker(ticker), interval)].copy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=400, help="barres au hasard (hors signaux)")
    ap.add_argument("--max-signals", type=int, default=250)
    args = ap.parse_args()

    M.fetch_price_data = fake_fetch
    M.get_latest_news = lambda *a, **k: []
    import builtins
    real_print = builtins.print

    data = B.load_data()
    B.VIX_THRESHOLD = 22.0
    sig = B.build_signals(data)
    sig = sig[sig["breakout"]]
    sig_set = {(r.ticker, int(r.bar) - 1) for r in sig.itertuples()}  # barre du SIGNAL = entree - 1
    vix = data["vix"]["Close"].astype(float)

    rng = np.random.default_rng(5)
    t_min = pd.Timestamp("2024-02-01 09:30", tz="America/New_York")
    points = []
    pos_pts = [(tk, i) for tk, i in sig_set if data["h1"][tk].index[i] >= t_min]
    rng.shuffle(pos_pts)
    points += pos_pts[: args.max_signals]
    for _ in range(args.n):
        tk = B.TICKERS[rng.integers(0, len(B.TICKERS))]
        h = data["h1"][tk]
        lo = int(np.searchsorted(h.index, t_min))
        i = int(rng.integers(lo, len(h) - 1))
        if (tk, i) not in sig_set:
            points.append((tk, i))

    rows = []
    for tk, i in points:
        h = data["h1"][tk]
        d = data["d1"][tk]
        day = h.index[i].tz_convert("America/New_York").normalize().tz_localize(None)
        FEED[(tk, os.environ["YF_INTERVAL"])] = h.iloc[max(0, i - 439): i + 1]
        FEED[(tk, os.environ["MTF_INTERVAL"])] = d[d.index < day].iloc[-252:]
        v = vix[vix.index < day]
        risk_off = bool(len(v) and float(v.iloc[-1]) >= float(os.environ["RISK_OFF_VIX_THRESHOLD"]))
        builtins.print = lambda *a, **k: None   # le pipeline est bavard
        try:
            an = M.analyze_ticker(tk, os.environ["YF_PERIOD"], os.environ["YF_INTERVAL"])
            ht = M.get_higher_tf_context(tk, period=os.environ["MTF_PERIOD"], interval=os.environ["MTF_INTERVAL"])
            parsed, tag = M.build_rules_scan_decision(
                ticker=tk, analysis=an or {}, state={}, higher_tf=ht,
                fund_data={"fund_score": 50, "risk_off": risk_off, "earnings_days": None})
        finally:
            builtins.print = real_print
        rows.append({"ticker": tk, "i": i, "ts": h.index[i], "bt": (tk, i) in sig_set,
                     "bot": parsed.get("decision") == "ACHETER", "tag": tag,
                     "conf": parsed.get("confiance"), "mtf": ht.get("trend_score"),
                     "brk": bool(an and M.analysis_breakout_active(an)),
                     "just": parsed.get("justification", "")[:110]})
    df = pd.DataFrame(rows)
    tp = int((df.bt & df.bot).sum())
    fn = int((df.bt & ~df.bot).sum())
    fp = int((~df.bt & df.bot).sum())
    tn = int((~df.bt & ~df.bot).sum())
    print("=" * 100)
    print("PARITE bot (main.py, profil paper) vs backtest — %d barres" % len(df))
    print("=" * 100)
    print("                      backtest ACHAT   backtest rien")
    print("  bot ACHETER         %8d          %8d" % (tp, fp))
    print("  bot ATTENDRE        %8d          %8d" % (fn, tn))
    agree = 100.0 * (tp + tn) / max(1, len(df))
    print("\nAccord: %.1f%%   | signaux du backtest retrouves par le bot: %.1f%%   | achats du bot hors backtest: %d"
          % (agree, 100.0 * tp / max(1, tp + fn), fp))
    bad = df[df.bt != df.bot]
    if len(bad):
        print("\nDesaccords (max 15):")
        with pd.option_context("display.width", 200, "display.max_colwidth", 110):
            print(bad[["ticker", "ts", "bt", "bot", "conf", "mtf", "brk", "just"]].head(15).to_string(index=False))
    df.to_csv(os.path.join(HERE, "parity_week_strategy.csv"), index=False)
    return 0 if agree >= 97.0 else 1


if __name__ == "__main__":
    sys.exit(main())
