# -*- coding: utf-8 -*-
"""
Ou est l'edge ? Esperance NETTE par bucket, avec intervalle de confiance.

Usage:
  .\\.venv\\Scripts\\python.exe scripts\\edge_report.py
  .\\.venv\\Scripts\\python.exe scripts\\edge_report.py --journal trades_journal_live.jsonl

Lit les champs de contexte poses a l'entree (atr_pct, theme_bloc, entry_hour_ny,
rsi, breakout, earnings_days) et le spread capture a l'ordre, puis croise avec le
resultat net de chaque trade.

A garder en tete en lisant la sortie: sur un echantillon de quelques dizaines de
trades, presque rien ne sera significatif. Un bucket dont l'IC95% croise zero ne
dit rien — c'est le cas normal, pas une anomalie. Le tableau devient exploitable
autour de 150-200 trades.
"""
import argparse
import collections
import json
import math
import os
import random
import re
import sys

random.seed(0)


def load_events(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def build_trades(rows, fee_min, fee_per_share):
    """Apparie signal_buy / ibkr_order_filled / trade_closed et calcule le net."""
    by_ts = sorted(rows, key=lambda r: str(r.get("ts_utc") or ""))
    ctx = {}          # ticker -> derniers champs de contexte vus
    fills = {}        # ticker -> infos du dernier fill (spread, mode d'entree)
    opens = collections.defaultdict(list)
    seen = set()
    trades = []
    for e in by_ts:
        ev = e.get("event")
        tk = e.get("ticker")
        if ev == "signal_buy":
            ctx[tk] = {k: e.get(k) for k in
                       ("atr_pct", "rsi", "theme_bloc", "entry_hour_ny",
                        "earnings_days", "breakout", "mtf_score", "confidence",
                        "fund_score", "buy_rank_score")}
        elif ev == "ibkr_order_filled" and str(e.get("side", "")).upper() == "BUY":
            w = str(e.get("warning") or "")
            m = re.search(r"spread\s+([\d.]+)\s*bps", w)
            fills[tk] = {
                "spread_bps": float(m.group(1)) if m else None,
                "midprice": ("MIDPRICE rempli" in w),
                "midprice_tried": ("MIDPRICE" in w),
            }
        elif ev == "confirm_buy":
            opens[tk].append({**(ctx.get(tk) or {}), **(fills.get(tk) or {})})
        elif ev == "trade_closed":
            if e.get("void"):
                continue
            key = (tk, e.get("ts_utc"), round(float(e.get("pnl_usd") or 0), 2))
            if key in seen:
                continue
            seen.add(key)
            entry = float(e.get("entry_price_usd") or 0)
            size = float(e.get("size_usd") or 0)
            if entry <= 0:
                continue
            qty = size / entry
            net = float(e.get("pnl_usd") or 0) - max(fee_min, fee_per_share * qty) * 2
            meta = opens[tk].pop(0) if opens.get(tk) else {}
            trades.append({**meta, "ticker": tk, "net": net,
                           "pnl_pct": float(e.get("pnl_pct") or 0),
                           "exit_reason": e.get("exit_reason"),
                           "size_usd": size})
    return trades


def boot_ci(vals, n=4000):
    if len(vals) < 3:
        return 0.0, 0.0, 0.0
    m = sum(vals) / len(vals)
    s = sorted(sum(vals[random.randrange(len(vals))] for _ in range(len(vals))) / len(vals)
               for _ in range(n))
    return m, s[int(.025 * n)], s[int(.975 * n)]


def bucket_report(trades, name, keyfn, order=None, min_n=3):
    g = collections.defaultdict(list)
    for t in trades:
        k = keyfn(t)
        if k is not None:
            g[k].append(t["net"])
    if not g:
        return
    print("\n=== %s ===" % name)
    print("%-22s %5s %10s %10s %24s %s" % ("bucket", "n", "total", "$/trade", "IC95%", "verdict"))
    keys = [k for k in (order or sorted(g, key=str)) if k in g]
    for k in keys:
        v = g[k]
        if len(v) < min_n:
            print("%-22s %5d %10.2f %10s %24s %s" % (str(k), len(v), sum(v), "-", "-", "trop peu"))
            continue
        m, lo, hi = boot_ci(v)
        verdict = "positif" if lo > 0 else ("negatif" if hi < 0 else "indistinguable de 0")
        print("%-22s %5d %+10.2f %+10.3f  [%+8.3f , %+8.3f]  %s"
              % (str(k), len(v), sum(v), m, lo, hi, verdict))


def main():
    ap = argparse.ArgumentParser(description="Ou est l'edge — esperance nette par bucket")
    ap.add_argument("--journal", default="trades_journal.jsonl")
    ap.add_argument("--fee-min", type=float, default=0.35)
    ap.add_argument("--fee-per-share", type=float, default=0.0035)
    args = ap.parse_args()

    path = args.journal
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    if not os.path.exists(path):
        print("Journal introuvable: %s" % path)
        sys.exit(1)

    trades = build_trades(load_events(path), args.fee_min, args.fee_per_share)
    if not trades:
        print("Aucun trade cloture exploitable dans %s" % path)
        sys.exit(1)

    nets = [t["net"] for t in trades]
    n = len(nets)
    tot = sum(nets)
    mean = tot / n
    sd = (sum((x - mean) ** 2 for x in nets) / max(1, n - 1)) ** .5
    t_stat = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    _, lo, hi = boot_ci(nets)

    print("=" * 96)
    print("EDGE REPORT — %s" % os.path.basename(path))
    print("=" * 96)
    print("  %d trades  |  net %+.2f$  |  %+.3f $/trade  |  t = %+.2f" % (n, tot, mean, t_stat))
    print("  IC95%% de l'esperance: [%+.3f , %+.3f] $/trade  -> %s"
          % (lo, hi, "edge positif" if lo > 0 else "PAS d'edge demontre"))
    need = int((2.0 * sd / mean) ** 2) if mean > 0 else 0
    if mean > 0 and need > n:
        print("  Il faudrait ~%d trades pour atteindre t=2 a cette esperance." % need)

    instrumented = sum(1 for t in trades if t.get("atr_pct") is not None)
    with_spread = sum(1 for t in trades if t.get("spread_bps") is not None)
    print("\n  Trades instrumentes: %d/%d   |   avec spread mesure: %d/%d"
          % (instrumented, n, with_spread, n))
    if instrumented < n:
        print("  (les trades anterieurs a l'instrumentation n'ont pas de contexte)")

    sp = [t["spread_bps"] for t in trades if t.get("spread_bps") is not None]
    if sp:
        sp_sorted = sorted(sp)
        med = sp_sorted[len(sp_sorted) // 2]
        print("\n  SPREAD MESURE: median %.1f bps  (min %.1f / max %.1f sur %d ordres)"
              % (med, sp_sorted[0], sp_sorted[-1], len(sp)))
        print("  Cout aller-retour implicite: %.2f$ sur un ticket de 520$" % (520 * med / 10000.0))
        if med > 5:
            print("  /!\\ Au-dela de 5 bps, le backtest donnait la config perdante.")
    else:
        print("\n  SPREAD: aucune mesure encore (champ pose depuis le 2026-08-04).")

    mp = [t for t in trades if t.get("midprice_tried")]
    if mp:
        ok = sum(1 for t in mp if t.get("midprice"))
        print("  MIDPRICE: %d/%d ordres remplis au midpoint (%.0f%%)"
              % (ok, len(mp), 100.0 * ok / len(mp)))

    bucket_report(trades, "Par bloc de correlation", lambda t: t.get("theme_bloc"))
    bucket_report(trades, "Par volatilite (ATR a l'entree)",
                  lambda t: None if t.get("atr_pct") is None else
                  ("<3%" if t["atr_pct"] < 3 else "3-5%" if t["atr_pct"] < 5 else
                   "5-7%" if t["atr_pct"] < 7 else ">=7%"),
                  order=["<3%", "3-5%", "5-7%", ">=7%"])
    bucket_report(trades, "Par heure d'entree (NY)",
                  lambda t: (t.get("entry_hour_ny") or "")[:2] + "h"
                  if t.get("entry_hour_ny") else None)
    bucket_report(trades, "Par RSI a l'entree",
                  lambda t: None if t.get("rsi") is None else
                  ("<50" if t["rsi"] < 50 else "50-60" if t["rsi"] < 60 else
                   "60-70" if t["rsi"] < 70 else ">=70"),
                  order=["<50", "50-60", "60-70", ">=70"])
    bucket_report(trades, "Breakout actif ?",
                  lambda t: None if t.get("breakout") is None else
                  ("oui" if t["breakout"] else "non"), order=["oui", "non"])
    bucket_report(trades, "Par motif de sortie", lambda t: t.get("exit_reason"))
    bucket_report(trades, "Par ticker", lambda t: t.get("ticker"))


if __name__ == "__main__":
    main()
