# -*- coding: utf-8 -*-
"""
Rapport de performance NET de frais + comparaison benchmark.

Usage:
  .\\.venv\\Scripts\\python.exe scripts\\performance_net.py                     # journal racine
  .\\.venv\\Scripts\\python.exe scripts\\performance_net.py --journal trades_journal_live.jsonl
  .\\.venv\\Scripts\\python.exe scripts\\performance_net.py --json out.json    # sortie machine

Modele de frais (IBKR Pro tiered, actions US):
  frais/ordre = max(fee_min, fee_per_share x qty)   [defaut: max(0.35, 0.0035 x qty)]
  + slippage estime optionnel (--slippage-bps, defaut 0 = non inclus)

Benchmarks (via yfinance, meme periode que le journal):
  - SPY, QQQ buy-and-hold
  - Panier equipondere des tickers reellement trades, buy-and-hold
"""
import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def load_closed_dedup(path, end_date=None):
    """trade_closed dedup sur (ticker, entry, exit, pnl) — le journal peut logger 2-3x la meme cloture."""
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
    closed, seen = [], set()
    for r in sorted(
        [x for x in rows if x.get("event") == "trade_closed" and not x.get("void")],
        key=lambda x: x.get("ts_utc") or "",
    ):
        k = (
            r.get("ticker"),
            round(float(r.get("entry_price_usd") or 0), 2),
            round(float(r.get("exit_price_usd") or 0), 2),
            round(float(r.get("pnl_usd") or 0), 2),
        )
        if k in seen:
            continue
        seen.add(k)
        if end_date and (r.get("ts_utc") or "")[:10] > end_date:
            continue
        closed.append(r)
    return closed


def order_fee(qty, fee_min, fee_per_share):
    return max(fee_min, fee_per_share * max(0.0, qty))


def trade_costs(t, fee_min, fee_per_share, slippage_bps):
    """Frais entree + sortie + slippage estime pour un trade_closed."""
    size = float(t.get("size_usd") or 0)
    entry = float(t.get("entry_price_usd") or 0)
    qty = size / entry if entry > 0 else 0.0
    fees = order_fee(qty, fee_min, fee_per_share) * 2  # entree + sortie
    slip = size * (slippage_bps / 10000.0) * 2 if slippage_bps > 0 else 0.0
    return fees, slip


def perf_stats(pnls, base=1000.0):
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    gross_w, gross_l = sum(wins), -sum(losses)
    return {
        "n": len(pnls),
        "pnl": round(total, 2),
        "pnl_pct": round(100 * total / base, 2),
        "win_rate": round(100 * len(wins) / len(pnls), 1) if pnls else 0.0,
        "expectancy": round(total / len(pnls), 3) if pnls else 0.0,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
        "avg_win": round(statistics.mean(wins), 2) if wins else 0.0,
        "avg_loss": round(statistics.mean(losses), 2) if losses else 0.0,
    }


def fetch_benchmarks(tickers, start, end, base=1000.0):
    """Buy-and-hold SPY/QQQ + panier equipondere sur [start, end]. Retourne dict ou None si data KO."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    symbols = sorted(set(tickers)) + ["SPY", "QQQ"]
    try:
        data = yf.download(
            symbols,
            start=start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
        )
    except Exception as exc:
        print(f"[BENCH] Telechargement impossible: {exc}")
        return None
    if data is None or len(data) == 0:
        return None

    def first_last_close(sym):
        try:
            closes = data[sym]["Close"].dropna()
            if len(closes) < 2:
                return None
            return float(closes.iloc[0]), float(closes.iloc[-1])
        except Exception:
            return None

    out = {}
    for sym in ("SPY", "QQQ"):
        fl = first_last_close(sym)
        if fl:
            out[sym] = round(100 * (fl[1] / fl[0] - 1.0), 2)
    rets = []
    per_ticker = {}
    for sym in sorted(set(tickers)):
        fl = first_last_close(sym)
        if fl:
            r = fl[1] / fl[0] - 1.0
            rets.append(r)
            per_ticker[sym] = round(100 * r, 2)
    if rets:
        basket = sum(rets) / len(rets)
        out["PANIER"] = round(100 * basket, 2)
        out["_panier_detail"] = per_ticker
        out["_panier_n"] = len(rets)
    return out or None


def main():
    ap = argparse.ArgumentParser(description="Performance nette de frais + benchmark")
    ap.add_argument("--journal", default="trades_journal.jsonl")
    ap.add_argument("--budget", type=float, default=1000.0)
    ap.add_argument("--end-date", default="", help="Ignorer les clotures apres cette date (YYYY-MM-DD)")
    ap.add_argument("--fee-min", type=float, default=0.35, help="Frais minimum par ordre (IBKR tiered: 0.35)")
    ap.add_argument("--fee-per-share", type=float, default=0.0035)
    ap.add_argument("--slippage-bps", type=float, default=0.0, help="Slippage estime par ordre en bps (0=off)")
    ap.add_argument("--no-benchmark", action="store_true")
    ap.add_argument("--json", default="", help="Ecrire aussi le resultat en JSON vers ce fichier")
    args = ap.parse_args()

    path = args.journal
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    closed = load_closed_dedup(path, end_date=args.end_date or None)
    if not closed:
        print(f"Aucun trade_closed dans {path}")
        sys.exit(1)

    gross = [float(t.get("pnl_usd") or 0) for t in closed]
    net = []
    total_fees = 0.0
    total_slip = 0.0
    for t in closed:
        fees, slip = trade_costs(t, args.fee_min, args.fee_per_share, args.slippage_bps)
        total_fees += fees
        total_slip += slip
        net.append(float(t.get("pnl_usd") or 0) - fees - slip)

    tss = sorted([parse_ts(t.get("ts_utc")) for t in closed if parse_ts(t.get("ts_utc"))])
    start, end = tss[0], tss[-1]
    g = perf_stats(gross, args.budget)
    n = perf_stats(net, args.budget)

    print("=" * 64)
    print(f"PERFORMANCE — {os.path.basename(path)}")
    print(f"Periode: {start.date()} -> {end.date()}  |  {g['n']} trades (dedup)")
    print(f"Modele frais: max({args.fee_min:.2f}$, {args.fee_per_share:.4f}$/action) x 2 ordres"
          + (f" + slippage {args.slippage_bps:.0f} bps/ordre" if args.slippage_bps > 0 else ""))
    print("=" * 64)
    print(f"{'':24s} {'BRUT':>10s} {'NET':>10s}")
    print(f"{'PnL':24s} {g['pnl']:>+10.2f} {n['pnl']:>+10.2f}")
    print(f"{'Rendement (%)':24s} {g['pnl_pct']:>+10.2f} {n['pnl_pct']:>+10.2f}")
    print(f"{'Esperance/trade':24s} {g['expectancy']:>+10.3f} {n['expectancy']:>+10.3f}")
    print(f"{'Win rate (%)':24s} {g['win_rate']:>10.1f} {n['win_rate']:>10.1f}")
    pf_g = g['profit_factor'] if g['profit_factor'] is not None else float('inf')
    pf_n = n['profit_factor'] if n['profit_factor'] is not None else float('inf')
    print(f"{'Profit factor':24s} {pf_g:>10.2f} {pf_n:>10.2f}")
    print(f"\nCout total frais: {total_fees:.2f}$"
          + (f" | slippage estime: {total_slip:.2f}$" if total_slip > 0 else "")
          + f" | impact: {100 * (total_fees + total_slip) / g['pnl']:.0f}% du PnL brut" if g['pnl'] > 0 else "")

    result = {
        "journal": os.path.basename(path),
        "period": {"start": str(start.date()), "end": str(end.date())},
        "fee_model": {"fee_min": args.fee_min, "fee_per_share": args.fee_per_share, "slippage_bps": args.slippage_bps},
        "gross": g,
        "net": n,
        "total_fees_usd": round(total_fees, 2),
        "total_slippage_usd": round(total_slip, 2),
    }

    if not args.no_benchmark:
        tickers = sorted({t.get("ticker") for t in closed if t.get("ticker")})
        bench = fetch_benchmarks(tickers, start, end, args.budget)
        if bench:
            bot_pct = n["pnl_pct"]
            print(f"\n--- BENCHMARK (meme periode, buy-and-hold) ---")
            print(f"{'Bot (NET de frais)':22s} {bot_pct:>+8.2f}%")
            for k in ("PANIER", "SPY", "QQQ"):
                if k in bench:
                    label = f"Panier {bench.get('_panier_n', '')} tickers" if k == "PANIER" else k
                    edge = bot_pct - bench[k]
                    print(f"{label:22s} {bench[k]:>+8.2f}%   (bot {edge:+.2f} pts)")
            result["benchmark_pct"] = {k: v for k, v in bench.items() if not k.startswith("_")}
            result["benchmark_detail"] = bench.get("_panier_detail", {})
        else:
            print("\n[BENCH] Benchmark indisponible (yfinance/reseau).")

    # PnL net par exit_reason (ou l'edge fuit)
    print(f"\n--- PnL NET par exit_reason ---")
    agg = defaultdict(lambda: [0.0, 0])
    for t, np_ in zip(closed, net):
        agg[t.get("exit_reason") or "?"][0] += np_
        agg[t.get("exit_reason") or "?"][1] += 1
    for k in sorted(agg, key=lambda x: -agg[x][0]):
        pnl, cnt = agg[k]
        print(f"  {str(k):18s} n={cnt:3d}  net={pnl:+8.2f}  avg={pnl / cnt:+.2f}")
    result["net_by_exit_reason"] = {str(k): {"n": v[1], "pnl": round(v[0], 2)} for k, v in agg.items()}

    # Par config_hash si present (attribution version de strategie)
    hashes = defaultdict(lambda: [0.0, 0])
    for t, np_ in zip(closed, net):
        h = t.get("config_hash")
        if h:
            hashes[h][0] += np_
            hashes[h][1] += 1
    if hashes:
        print(f"\n--- PnL NET par version de config ---")
        for h, (pnl, cnt) in hashes.items():
            print(f"  {h}  n={cnt:3d}  net={pnl:+8.2f}  avg={pnl / cnt:+.2f}")
        result["net_by_config_hash"] = {h: {"n": v[1], "pnl": round(v[0], 2)} for h, v in hashes.items()}

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\nJSON -> {args.json}")


if __name__ == "__main__":
    main()
