# -*- coding: utf-8 -*-
"""
Dry-run EN DIRECT de la strategie "breakout semaine": que deciderait le bot
maintenant ? Donnees yfinance reelles, pipeline de decision reel (main.py,
profil .env + .env.ibkr_paper). Aucun ordre, aucune connexion IBKR, aucun
message Telegram, aucune ecriture de positions.json ni du journal.

Verifie au passage: telechargement 1h/3mo et 1d/1y, retrait de la barre en
formation, tendance journaliere de la veille, VIX, score fondamental et
earnings reels (yfinance), niveaux SL/TP et taille de ligne.

Usage: .\\.venv\\Scripts\\python.exe scripts\\dryrun_week_strategy.py
"""
from __future__ import annotations

import builtins
import os
import sys
from datetime import datetime

from dotenv import dotenv_values

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_env = {**dotenv_values(os.path.join(ROOT, ".env")), **dotenv_values(os.path.join(ROOT, ".env.ibkr_paper"))}
for k, v in _env.items():
    if v is not None:
        os.environ[k] = v
os.environ["IBKR_ENABLED"] = "0"
os.environ["IBKR_AUTO_EXECUTE"] = "0"
for _k in list(os.environ):
    if any(s in _k.upper() for s in ("TOKEN", "API_KEY", "CHAT_ID")):
        os.environ[_k] = ""

import main as M  # noqa: E402


def main():
    real_print = builtins.print
    now = datetime.now(M.MARKET_TZ)
    iv, per = os.environ["YF_INTERVAL"], os.environ["YF_PERIOD"]
    miv, mper = os.environ["MTF_INTERVAL"], os.environ["MTF_PERIOD"]
    vix = M.fetch_price_data("^VIX", period="1mo", interval="1d")
    vix = M.normalize_ohlc_columns(vix, "^VIX")
    vix = M.drop_forming_bar(vix, "1d")
    vix_last = float(vix["Close"].iloc[-1])
    risk_off = vix_last >= float(os.environ.get("RISK_OFF_VIX_THRESHOLD", "22"))
    print("=" * 110)
    print("DRY-RUN breakout semaine — %s NY | marche %s | VIX veille %.2f -> risk-off %s"
          % (now.strftime("%Y-%m-%d %H:%M"), "OUVERT" if M.is_us_market_open() else "ferme",
             vix_last, "OUI" if risk_off else "non"))
    print("Scan %s/%s (barres cloturees: %s) | MTF %s/%s (veille: %s) | SL -%s%% TP x%s | max %s seances"
          % (iv, per, os.environ.get("SCAN_COMPLETED_BARS_ONLY"), miv, mper,
             os.environ.get("MTF_COMPLETED_BARS_ONLY"), os.environ.get("BUY_FIXED_STOP_PCT"),
             os.environ.get("BUY_RR_REWARD_MULT"), os.environ.get("TRADING_MAX_HOLD_SESSIONS")))
    print("=" * 110)
    print("%-6s %-17s %8s %5s %4s %4s %5s %9s  %s"
          % ("ticker", "derniere barre", "prix", "conf", "MTF", "brk", "fund", "earnings", "decision"))
    cache = {}
    for tk in M.get_tickers_from_env():
        builtins.print = lambda *a, **k: None
        try:
            raw = M.normalize_ohlc_columns(M.fetch_price_data(tk, period=per, interval=iv), tk)
            an = M.analyze_ticker(tk, per, iv)
            ht = M.get_higher_tf_context(tk, period=mper, interval=miv)
            fd = M.fetch_fundamental_data(ticker=tk, cache=cache, cache_hours=24)
            fd["risk_off"] = risk_off
            parsed, tag = M.build_rules_scan_decision(ticker=tk, analysis=an or {}, state={},
                                                      higher_tf=ht, fund_data=fd)
        except Exception as exc:  # noqa: BLE001
            builtins.print = real_print
            print("%-6s ERREUR %s" % (tk, exc))
            continue
        finally:
            builtins.print = real_print
        kept = M.drop_forming_bar(raw, iv)
        last_ts = kept.index[-1] if len(kept) else None
        dec = parsed.get("decision", "?")
        extra = ""
        if dec == "ACHETER":
            px = float(an["current_price"])
            sl, tp = M.compute_fixed_sl_tp(px, tk)
            qty = int(float(os.environ.get("TRADING_TARGET_LINE_USD", "520")) // px)
            extra = " -> SL %.2f / TP %.2f, ~%d actions" % (sl, tp, qty)
        just = parsed.get("justification", "")
        print("%-6s %-17s %8.2f %5s %4s %4s %5s %9s  %s%s  | %s"
              % (tk, last_ts.strftime("%m-%d %H:%M") if last_ts is not None else "-",
                 float(an["current_price"]) if an else float("nan"), parsed.get("confiance"),
                 ht.get("trend_score"), "oui" if an and M.analysis_breakout_active(an) else "non",
                 fd.get("fund_score"), fd.get("earnings_days"), dec, extra, just[:70]))
    print("\nLa barre 1h affichee est la derniere CLOTUREE (celle en formation est ignoree).")


if __name__ == "__main__":
    main()
