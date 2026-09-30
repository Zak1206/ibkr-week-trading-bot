# -*- coding: utf-8 -*-
"""
Tests de la strategie "breakout semaine" (fonctions pures + IBKR simule).
Lancement:  .\\.venv\\Scripts\\python.exe scripts\\test_week_strategy.py
Couvre: comptage des seances (feries NYSE), fenetre de sortie, sortie forcee a
5 seances (vente, pas de vente, retry, entry_ts absent), barres en formation
1h et journaliere, garde-fou PDT, SL/TP -10/+25 et plafond de risque du profil.
Aucune connexion IBKR ni reseau.
"""
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

from test_execution_safety import check, with_env  # noqa: E402

import main as M  # noqa: E402

NY = M.MARKET_TZ


def ny(y, mo, d, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=NY)


def utc_txt(dt):
    return dt.astimezone(M.UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_sessions_held():
    print("\n[1] sessions_held — convention du backtest (jour d'entree exclu)")
    e = utc_txt(ny(2026, 9, 21, 10, 35))  # lundi
    check("lundi -> vendredi = 4", M.sessions_held(e, ny(2026, 9, 25, 15, 40)) == 4)
    check("lundi -> lundi suivant = 5", M.sessions_held(e, ny(2026, 9, 28, 15, 40)) == 5)
    check("meme jour = 0", M.sessions_held(e, ny(2026, 9, 21, 15, 40)) == 0)
    # Thanksgiving 2026-11-26 ferme: mercredi 25 -> jeudi 3 dec = 5 seances (26 exclu)
    e2 = utc_txt(ny(2026, 11, 25, 11, 0))
    check("ferie Thanksgiving exclu", M.sessions_held(e2, ny(2026, 12, 2, 15, 40)) == 4
          and M.sessions_held(e2, ny(2026, 12, 3, 15, 40)) == 5)
    check("entry_ts vide -> None", M.sessions_held("", ny(2026, 9, 28)) is None)
    with with_env({"TRADING_EXTRA_MARKET_HOLIDAYS": "2026-09-24"}):
        check("ferie supplementaire via env", M.sessions_held(e, ny(2026, 9, 28, 15, 40)) == 4)


def test_max_hold_window():
    print("\n[2] fenetre de sortie duree max")
    with with_env({"TRADING_MAX_HOLD_SESSIONS": "5", "TRADING_MAX_HOLD_MIN_BEFORE_CLOSE": "30"}):
        check("15h40 NY = fenetre", M.is_max_hold_window(ny(2026, 9, 28, 15, 40)))
        check("14h00 NY = hors fenetre", not M.is_max_hold_window(ny(2026, 9, 28, 14, 0)))
        check("samedi = hors fenetre", not M.is_max_hold_window(ny(2026, 9, 26, 15, 40)))
    with with_env({"TRADING_MAX_HOLD_SESSIONS": "0"}):
        check("desactive si 0", not M.is_max_hold_window(ny(2026, 9, 28, 15, 40)))
    with with_env({"TRADING_MAX_HOLD_SESSIONS": None}):
        check("desactive par defaut", not M.is_max_hold_window(ny(2026, 9, 28, 15, 40)))


def test_max_hold_exit():
    print("\n[3] maybe_max_hold_exit_positions — IBKR simule")
    calls = []
    orig_sell, orig_auto = M.attempt_ibkr_sell, M.ibkr_auto_execute_enabled
    tmp = tempfile.mkdtemp()
    pos_file = os.path.join(tmp, "positions.json")

    def fake_sell(**kw):
        calls.append(kw)
        ok = fake_sell.ok
        if ok:
            kw["positions"].pop(kw["ticker"], None)
        return ok

    def state(entry_dt):
        return {"in_position": True, "pending_sell": False, "entry_price_usd": 20.0,
                "entry_notional_usd": 520.0, "entry_ts_utc": utc_txt(entry_dt),
                "stop_loss": 18.0, "take_profit": 25.0}

    M.attempt_ibkr_sell = fake_sell
    M.ibkr_auto_execute_enabled = lambda: True
    try:
        with with_env({"TRADING_MAX_HOLD_SESSIONS": "5", "TRADING_MAX_HOLD_RETRY_SEC": "20"}):
            now = ny(2026, 9, 28, 15, 40)
            positions = {"HOOD": state(ny(2026, 9, 21, 10, 35)),   # 5 seances -> vendre
                         "SOFI": state(ny(2026, 9, 22, 10, 35)),   # 4 seances -> garder
                         "COIN": {**state(ny(2026, 9, 21)), "entry_ts_utc": ""}}
            M.save_positions(pos_file, positions)
            fake_sell.ok = True
            n = M.maybe_max_hold_exit_positions(positions, pos_file, journal_file=os.path.join(tmp, "j.jsonl"),
                                                journal_enabled=False, bot_token="", chat_id="", now=now)
            check("1 vente (HOOD)", n == 1 and [c["ticker"] for c in calls] == ["HOOD"])
            check("vente au marche, raison max_hold_sessions",
                  calls and calls[0]["exit_reason"] == "max_hold_sessions" and calls[0]["min_limit_price"] is None)
            check("SOFI conservee (4 seances)", "SOFI" in positions and positions["SOFI"]["in_position"])
            check("COIN sans entry_ts ignoree", "COIN" in positions)

            calls.clear()
            positions = {"RKLB": state(ny(2026, 9, 21, 10, 35))}
            fake_sell.ok = False
            M.maybe_max_hold_exit_positions(positions, pos_file, journal_file="", journal_enabled=False,
                                            bot_token="", chat_id="", now=now)
            M.maybe_max_hold_exit_positions(positions, pos_file, journal_file="", journal_enabled=False,
                                            bot_token="", chat_id="", now=now)
            st = M.normalize_position_state(positions["RKLB"])
            check("echec: 1 seul essai dans le delai de retry", len(calls) == 1)
            check("echec: pas de pending_sell collant", not bool(st.get("pending_sell")))
            check("echec: essai horodate", float(st.get("last_max_hold_try_ts", 0)) > 0)

            calls.clear()
            positions = {"RKLB": state(ny(2026, 9, 21, 10, 35))}
            M.maybe_max_hold_exit_positions(positions, pos_file, journal_file="", journal_enabled=False,
                                            bot_token="", chat_id="", now=ny(2026, 9, 28, 13, 0))
            check("hors fenetre: aucune vente", len(calls) == 0)
    finally:
        M.attempt_ibkr_sell, M.ibkr_auto_execute_enabled = orig_sell, orig_auto


def _bars(idx):
    return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1}, index=idx)


def test_drop_forming_bar():
    print("\n[4] drop_forming_bar — le signal ne voit que des barres cloturees")
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-28 09:30", tz=NY), pd.Timestamp("2026-09-28 10:30", tz=NY)])
    df = _bars(idx)
    check("10h45: barre 10h30 en formation retiree", len(M.drop_forming_bar(df, "1h", ny(2026, 9, 28, 10, 45))) == 1)
    check("11h30: barre 10h30 cloturee gardee", len(M.drop_forming_bar(df, "1h", ny(2026, 9, 28, 11, 30))) == 2)
    idx2 = pd.DatetimeIndex([pd.Timestamp("2026-09-28 14:30", tz=NY), pd.Timestamp("2026-09-28 15:30", tz=NY)])
    df2 = _bars(idx2)
    check("15h45: barre 15h30 en formation retiree", len(M.drop_forming_bar(df2, "1h", ny(2026, 9, 28, 15, 45))) == 1)
    check("16h00: barre 15h30 cloturee (fin de seance)", len(M.drop_forming_bar(df2, "1h", ny(2026, 9, 28, 16, 0))) == 2)
    utc_idx = idx.tz_convert("UTC")
    check("index UTC gere", len(M.drop_forming_bar(_bars(utc_idx), "1h", ny(2026, 9, 28, 10, 45))) == 1)
    didx = pd.DatetimeIndex([pd.Timestamp("2026-09-25"), pd.Timestamp("2026-09-28")])
    d = _bars(didx)
    check("journalier: bougie du jour retiree en seance", len(M.drop_forming_bar(d, "1d", ny(2026, 9, 28, 11, 0))) == 1)
    check("journalier: gardee apres 16h", len(M.drop_forming_bar(d, "1d", ny(2026, 9, 28, 16, 5))) == 2)
    check("journalier: veille jamais retiree", len(M.drop_forming_bar(d, "1d", ny(2026, 9, 29, 11, 0))) == 2)
    check("vide ok", len(M.drop_forming_bar(_bars(pd.DatetimeIndex([])), "1h")) == 0)


def test_pdt_guard():
    print("\n[5] garde-fou PDT (day trades sur 5 jours ouvres)")
    tmp = tempfile.mkdtemp()
    jf = os.path.join(tmp, "journal.jsonl")

    def ev(event, tk, dt, side=None):
        e = {"event": event, "ticker": tk, "ts_utc": utc_txt(dt)}
        if side:
            e["side"] = side
        return json.dumps(e)

    lines = [
        ev("ibkr_order_filled", "HOOD", ny(2026, 9, 22, 10, 0), "BUY"), ev("trade_closed", "HOOD", ny(2026, 9, 22, 14, 0)),
        ev("ibkr_order_filled", "SOFI", ny(2026, 9, 23, 10, 0), "BUY"), ev("trade_closed", "SOFI", ny(2026, 9, 23, 15, 0)),
        ev("ibkr_order_filled", "COIN", ny(2026, 9, 24, 10, 0), "BUY"), ev("trade_closed", "COIN", ny(2026, 9, 25, 11, 0)),
        ev("ibkr_order_filled", "PLTR", ny(2026, 9, 10, 10, 0), "BUY"), ev("trade_closed", "PLTR", ny(2026, 9, 10, 12, 0)),
    ]
    with open(jf, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    now = ny(2026, 9, 28, 11, 0)  # fenetre 5 jours: 22, 23, 24, 25, 28
    check("2 day trades dans la fenetre (overnight et ancien exclus)", M.count_recent_day_trades(jf, now) == 2)
    with with_env({"RISK_PDT_GUARD_ENABLED": "1", "RISK_PDT_MAX_DAY_TRADES": "3"}):
        b, n, lim = M.pdt_guard_blocks_buys(jf, now)
        check("2 < 3: achats autorises", not b and n == 2 and lim == 3)
        with open(jf, "a", encoding="utf-8") as f:
            f.write(ev("ibkr_order_filled", "RKLB", ny(2026, 9, 28, 9, 45), "BUY") + "\n")
            f.write(ev("trade_closed", "RKLB", ny(2026, 9, 28, 10, 30)) + "\n")
        b, n, _ = M.pdt_guard_blocks_buys(jf, now)
        check("3 day trades: achats bloques", b and n == 3)
        b, n, _ = M.pdt_guard_blocks_buys(jf, ny(2026, 9, 30, 11, 0))
        check("fenetre glissante: les plus anciens sortent", not b and n == 1)
    with with_env({"RISK_PDT_GUARD_ENABLED": "0"}):
        check("desactive -> jamais bloque", not M.pdt_guard_blocks_buys(jf, now)[0])
    check("journal absent -> 0", M.count_recent_day_trades(os.path.join(tmp, "absent.jsonl"), now) == 0)


def test_week_profile_levels():
    print("\n[6] niveaux du profil paper (SL -10 / TP +25, risque 5.5%)")
    env = {"BUY_FIXED_SL_TP_ENABLED": "1", "BUY_FIXED_STOP_PCT": "10", "BUY_SNIPER_MODE": "1",
           "BUY_SNIPER_MAX_STOP_PCT": "10", "BUY_RR_REWARD_MULT": "2.5",
           "TRADING_ENFORCE_RISK_CAP": "1", "TRADING_RISK_PER_TRADE_PCT": "5.5"}
    with with_env(env):
        sl, tp = M.compute_fixed_sl_tp(40.0, "HOOD")
        check("SL = -10%", abs(sl - 36.0) < 0.011, f"sl={sl}")
        check("TP = +25%", abs(tp - 50.0) < 0.011, f"tp={tp}")
        rr = (tp - 40.0) / (40.0 - sl)
        check("RR >= BUY_MIN_RR 1.74", M.risk_reward_meets_minimum(rr, 1.74))
        capped, was = M.apply_risk_cap_to_notional(520.0, entry_price=40.0, stop_price=sl,
                                                   effective_budget_usd=1000.0)
        check("ligne 520$ non rabotee par le plafond de risque", not was and abs(capped - 520.0) < 1e-6,
              f"capped={capped}")
    with with_env({**env, "TRADING_RISK_PER_TRADE_PCT": "1.0"}):
        capped1, was1 = M.apply_risk_cap_to_notional(520.0, entry_price=40.0, stop_price=36.0,
                                                     effective_budget_usd=1000.0)
        check("garde: avec 1% de risque la ligne tomberait a ~100$", was1 and capped1 < 110.0,
              f"capped={capped1}")
    with with_env({**env, "BUY_SNIPER_MAX_STOP_PCT": "2.0"}):
        sl2, _ = M.compute_fixed_sl_tp(40.0, "HOOD")
        check("garde: sans BUY_SNIPER_MAX_STOP_PCT=10 le stop retombe a -2%", abs(sl2 - 39.2) < 0.011, f"sl={sl2}")


def test_equity_breaker():
    print("\n[7] coupe-circuit sur drawdown realise")
    tmp = tempfile.mkdtemp()
    jf = os.path.join(tmp, "journal.jsonl")

    def closed(tk, dt, pnl, px):
        return json.dumps({"event": "trade_closed", "ticker": tk, "ts_utc": utc_txt(dt), "pnl_usd": pnl,
                           "entry_price_usd": px, "exit_price_usd": px + pnl / 10, "size_usd": 1000.0})

    lines = [closed("OLD", ny(2026, 9, 1), -900.0, 10.0),            # avant le lancement: ignore
             closed("HOOD", ny(2026, 10, 1), +100.0, 40.0),          # 1100 = plus haut
             closed("SOFI", ny(2026, 10, 8), -200.0, 15.0),          # 900
             closed("COIN", ny(2026, 10, 15), -250.0, 190.0)]        # 650 -> -40.9%
    with open(jf, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with with_env({"RISK_MAX_DRAWDOWN_PCT": "35", "RISK_MAX_DD_START_DATE": "2026-09-29"}):
        b, dd, lim = M.equity_drawdown_blocks_buys(1000.0, jf)
        check("-40.9% sous le plus haut: bloque", b and abs(dd + 40.909) < 0.01 and lim == 35, f"dd={dd}")
    with with_env({"RISK_MAX_DRAWDOWN_PCT": "50", "RISK_MAX_DD_START_DATE": "2026-09-29"}):
        check("limite 50%: pas bloque", not M.equity_drawdown_blocks_buys(1000.0, jf)[0])
    with with_env({"RISK_MAX_DRAWDOWN_PCT": "35", "RISK_MAX_DD_START_DATE": ""}):
        b, dd, _ = M.equity_drawdown_blocks_buys(1000.0, jf)
        check("sans date: tout l'historique compte", b and dd < -60)
    with with_env({"RISK_MAX_DRAWDOWN_PCT": "0"}):
        check("desactive si 0", not M.equity_drawdown_blocks_buys(1000.0, jf)[0])
    check("journal absent: pas bloque", not M.equity_drawdown_blocks_buys(1000.0, os.path.join(tmp, "x.jsonl"))[0])


def test_paper_profile_consistency():
    print("\n[8] coherence du VRAI profil paper (.env + .env.ibkr_paper)")
    from dotenv import dotenv_values
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**dotenv_values(os.path.join(root, ".env")), **dotenv_values(os.path.join(root, ".env.ibkr_paper"))}
    g = lambda k: float(env.get(k) or 0)  # noqa: E731
    check("breakout seul", env.get("BUY_SNIPER_BREAKOUT_REQUIRED") == "1")
    check("stop fixe = plafond sniper (sinon retombe a 2%)", g("BUY_FIXED_STOP_PCT") == g("BUY_SNIPER_MAX_STOP_PCT") == 10)
    check("TP +25% (RR 2.5)", g("BUY_RR_REWARD_MULT") == 2.5)
    check("ni flat, ni trailing, ni swap", env.get("TRADING_INTRADAY_FLAT_ENABLED") == "0"
          and env.get("TRAILING_TP_ENABLED") == "0" and env.get("TRADING_SWAP_ON_FULL_SLOTS") == "0"
          and env.get("TRADING_SWAP_ON_BUDGET") == "0")
    check("sortie 5 seances", int(g("TRADING_MAX_HOLD_SESSIONS")) == 5)
    check("barres cloturees (scan + MTF)", env.get("SCAN_COMPLETED_BARS_ONLY") == "1"
          and env.get("MTF_COMPLETED_BARS_ONLY") == "1")
    check("MTF journaliere sur >= 80 bougies (1y)", env.get("MTF_INTERVAL") == "1d" and env.get("MTF_PERIOD") == "1y")
    line, stop, budget = g("TRADING_TARGET_LINE_USD"), g("BUY_FIXED_STOP_PCT"), g("TRADING_BUDGET_USD")
    check("plafond de risque >= risque de la ligne (sinon ligne rabotee)",
          g("TRADING_RISK_PER_TRADE_PCT") / 100 * budget >= line * 1.10 * stop / 100 - 1e-6,
          f"risk={g('TRADING_RISK_PER_TRADE_PCT')}% line={line}")
    check("un stop ne declenche pas la garde DD du jour", g("RISK_DAILY_DD_LIMIT_PCT") > stop * line * 1.10 / budget)
    worst_dd = 48 if g("TRADING_LINE_STEP_PCT") > 0 else 28   # backtest: paliers -48%, ligne fixe -26/-28%
    check("coupe-circuit au-dela du pire DD backtest (-%d%%)" % worst_dd, g("RISK_MAX_DRAWDOWN_PCT") > worst_dd)
    check("CVS hors univers", "CVS" not in (env.get("TICKERS_OVERRIDE") or ""))
    with with_env({"BUY_FIXED_SL_TP_ENABLED": "1", "BUY_SNIPER_MODE": "1", "TRADING_ENFORCE_RISK_CAP": "1",
                   **{k: env[k] for k in ("BUY_FIXED_STOP_PCT", "BUY_SNIPER_MAX_STOP_PCT", "TRADING_RISK_PER_TRADE_PCT")}}):
        sl, _ = M.compute_fixed_sl_tp(40.0, "HOOD")
        capped, was = M.apply_risk_cap_to_notional(line, entry_price=40.0, stop_price=sl, effective_budget_usd=budget)
        check("ligne %.0f$ intacte au budget de depart" % line, not was, f"capped={capped}")
        capped2, was2 = M.apply_risk_cap_to_notional(line, entry_price=40.0, stop_price=sl, effective_budget_usd=700.0)
        check("apres -30%% de pertes la ligne se reduit toute seule (%.0f$)" % capped2, was2 and capped2 < line)


def test_reference_price_by_session():
    print("\n[9] prix de reference Yahoo: celui de la seance en cours")

    class FakeTicker:
        def __init__(self, info):
            self.info = info
            self.fast_info = {}

    orig = M.yf.Ticker
    cases = [
        ("pre-market: pre du jour, pas l'after-hours de la veille",
         {"marketState": "PRE", "preMarketPrice": 101.0, "postMarketPrice": 99.0, "currentPrice": 100.0}, 101.0),
        ("seance: prix courant",
         {"marketState": "REGULAR", "postMarketPrice": 99.0, "currentPrice": 100.0}, 100.0),
        ("after-hours: prix post",
         {"marketState": "POST", "postMarketPrice": 102.0, "currentPrice": 100.0}, 102.0),
        ("nuit / week-end (CLOSED): dernier post",
         {"marketState": "CLOSED", "postMarketPrice": 103.0, "currentPrice": 100.0}, 103.0),
    ]
    try:
        with with_env({"REFERENCE_PRICE_IBKR_FIRST": "0"}):
            for lab, info, want in cases:
                M.yf.Ticker = lambda tk, _i=info: FakeTicker(_i)
                got = M.get_reference_price_usd("HOOD", skip_ibkr=True)
                check(lab, got == want, f"got={got} want={want}")
    finally:
        M.yf.Ticker = orig


def test_cockpit_summary():
    print("\n[10] cockpit: PnL de la strategie en cours separe du cumul brut")
    tmp = tempfile.mkdtemp()
    jf = os.path.join(tmp, "journal.jsonl")

    def closed(tk, dt, pnl):
        return json.dumps({"event": "trade_closed", "ticker": tk, "ts_utc": utc_txt(dt), "pnl_usd": pnl,
                           "entry_price_usd": 10.0 + pnl, "exit_price_usd": 11.0, "size_usd": 1000.0})

    with open(jf, "w", encoding="utf-8") as f:
        f.write("\n".join([closed("OLD", ny(2026, 6, 1), 170.0),
                           closed("HOOD", ny(2026, 10, 5), 80.0),
                           closed("SOFI", ny(2026, 10, 12), -30.0)]) + "\n")
    env = {"RISK_MAX_DD_START_DATE": "2026-09-29", "RISK_MAX_DRAWDOWN_PCT": "35",
           "RISK_PDT_GUARD_ENABLED": "1", "RISK_PDT_MAX_DAY_TRADES": "3", "COCKPIT_FEE_PER_ORDER_USD": "0.40"}
    with with_env(env):
        d = M.build_cockpit_data({}, budget_usd=1000.0, max_open_positions=1, journal_file=jf)
    s = d["summary"]
    check("cumul brut = toutes configs (+220)", abs(s["realized_pnl_usd"] - 220.0) < 1e-6)
    check("strategie en cours = trades depuis le 29/09 (+50, 2 trades)",
          abs(s["strategy_realized_usd"] - 50.0) < 1e-6 and s["strategy_trades"] == 2)
    check("net de frais estimes (50 - 2 x 2 x 0.40 = 48.40)", abs(s["strategy_realized_net_est_usd"] - 48.40) < 1e-6)
    check("coupe-circuit expose (limite 35)", s["breaker_limit_pct"] == 35 and s["breaker_dd_pct"] <= 0)
    check("compteur PDT expose", s["pdt_limit"] == 3 and s["pdt_day_trades_5d"] == 0)
    check("slots 0/1", s["slots_used"] == 0 and s["max_slots"] == 1)


def test_line_steps():
    print("\n[11] reinvestissement par paliers (x1.25)")
    tmp = tempfile.mkdtemp()
    jf = os.path.join(tmp, "journal.jsonl")

    def write(pnls):
        with open(jf, "w", encoding="utf-8") as f:
            lines = [json.dumps({"event": "trade_closed", "ticker": "OLD", "ts_utc": utc_txt(ny(2026, 6, 1)),
                                 "pnl_usd": 500.0, "entry_price_usd": 1, "exit_price_usd": 2, "size_usd": 1})]
            for i, p in enumerate(pnls):
                lines.append(json.dumps({"event": "trade_closed", "ticker": "T%d" % i,
                                         "ts_utc": utc_txt(ny(2026, 10, 1 + i)), "pnl_usd": p,
                                         "entry_price_usd": 10 + i, "exit_price_usd": 11 + i, "size_usd": 1000}))
            f.write("\n".join(lines) + "\n")

    base = {"TRADING_BUDGET_USD": "1000", "TRADING_LINE_STEP_PCT": "25", "RISK_MAX_DD_START_DATE": "2026-09-29",
            "TRADING_LINE_STEP_START_DATE": None}
    with with_env({**base, "TRADING_LINE_STEP_MODE": "updown"}):
        write([]); f, k, _ = M.line_step_factor(jf)
        check("depart: 1000$ (palier 0, gains d'avant le 29/09 ignores)", k == 0 and abs(f - 1) < 1e-9)
        write([260.0]); f, k, _ = M.line_step_factor(jf)
        check("compte 1260$ -> palier +1 (1250$)", k == 1 and abs(1000 * f - 1250) < 1e-6)
        write([600.0]); f, k, _ = M.line_step_factor(jf)
        check("compte 1600$ -> palier +2 (1562.5$)", k == 2 and abs(1000 * f - 1562.5) < 1e-6)
        write([-10.0]); f, k, _ = M.line_step_factor(jf)
        check("compte 990$ -> palier -1 (800$)", k == -1 and abs(1000 * f - 800) < 1e-6)
        write([300.0, -250.0]); f, k, _ = M.line_step_factor(jf)
        check("updown: 1300 puis 1050$ -> redescend au palier 0", k == 0)
    with with_env({**base, "TRADING_LINE_STEP_MODE": "up"}):
        write([300.0, -250.0]); f, k, _ = M.line_step_factor(jf)
        check("up: garde le palier du plus haut (1300$ -> +1)", k == 1)
        write([-300.0]); f, k, _ = M.line_step_factor(jf)
        check("up: ne descend jamais sous le palier 0", k == 0)
    with with_env({**base, "TRADING_LINE_STEP_PCT": "0"}):
        check("desactive (0) -> facteur 1", M.line_step_factor(jf)[0] == 1.0)
    # effet sur le couloir de taille du bot
    with with_env({**base, "TRADING_LINE_STEP_MODE": "updown", "TRADING_TARGET_LINE_USD": "1000",
                   "TRADE_JOURNAL_FILE": jf}):
        write([260.0])
        _, lo, hi = M.dynamic_notional_band_usd(current_price=40.0, atr=1.0, conviction=80, remaining_usd=5000.0,
                                                slots_left=1, risk_off=False)
        check("couloir de taille suit le palier (1125-1375$)", abs(lo - 1125) < 1e-6 and abs(hi - 1375) < 1e-6,
              f"lo={lo} hi={hi}")
        with with_env({"TRADING_LINE_STEP_PCT": "0"}):
            _, lo0, hi0 = M.dynamic_notional_band_usd(current_price=40.0, atr=1.0, conviction=80, remaining_usd=5000.0,
                                                      slots_left=1, risk_off=False)
            check("sans paliers: couloir 900-1100$", abs(lo0 - 900) < 1e-6 and abs(hi0 - 1100) < 1e-6)


def main_tests():
    test_sessions_held()
    test_max_hold_window()
    test_max_hold_exit()
    test_drop_forming_bar()
    test_pdt_guard()
    test_week_profile_levels()
    test_equity_breaker()
    test_paper_profile_consistency()
    test_reference_price_by_session()
    test_cockpit_summary()
    test_line_steps()
    import test_execution_safety as T
    print("\nResultat: %d OK / %d FAIL" % (T.PASS, T.FAIL))
    return T.FAIL


if __name__ == "__main__":
    sys.exit(1 if main_tests() else 0)
