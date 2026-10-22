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

        orig_px = M.get_reference_price_usd
        try:
            with with_env({"TRADING_MAX_HOLD_SESSIONS": "5", "TRADING_MAX_HOLD_RETRY_SEC": "20",
                           "TRADING_MAX_HOLD_ONLY_IF_PROFIT": "1"}):
                now = ny(2026, 9, 28, 15, 40)
                calls.clear()
                positions = {"HOOD": state(ny(2026, 9, 21, 10, 35))}
                M.get_reference_price_usd = lambda ticker: 19.0
                n = M.maybe_max_hold_exit_positions(positions, pos_file, journal_file="", journal_enabled=False,
                                                    bot_token="", chat_id="", now=now)
                check("en perte: pas de vente", n == 0 and len(calls) == 0 and "HOOD" in positions)
                calls.clear()
                positions = {"HOOD": state(ny(2026, 9, 21, 10, 35))}
                M.get_reference_price_usd = lambda ticker: 20.0
                n = M.maybe_max_hold_exit_positions(positions, pos_file, journal_file="", journal_enabled=False,
                                                    bot_token="", chat_id="", now=now)
                check("a plat: pas de vente", n == 0 and len(calls) == 0)
                calls.clear()
                positions = {"HOOD": state(ny(2026, 9, 21, 10, 35))}
                M.get_reference_price_usd = lambda ticker: None
                n = M.maybe_max_hold_exit_positions(positions, pos_file, journal_file="", journal_enabled=False,
                                                    bot_token="", chat_id="", now=now)
                check("prix inconnu: pas de vente", n == 0 and len(calls) == 0)
                calls.clear()
                fake_sell.ok = True
                positions = {"HOOD": state(ny(2026, 9, 21, 10, 35))}
                M.get_reference_price_usd = lambda ticker: 21.0
                n = M.maybe_max_hold_exit_positions(positions, pos_file, journal_file="", journal_enabled=False,
                                                    bot_token="", chat_id="", now=now)
                check("en gain: vente", n == 1 and calls and calls[0]["exit_reason"] == "max_hold_sessions")
        finally:
            M.get_reference_price_usd = orig_px
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
    check("sortie 5 seances seulement si latent en gain", env.get("TRADING_MAX_HOLD_ONLY_IF_PROFIT") == "1")
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
    check("actions entieres arrondies a l'inferieur, comme le backtest",
          env.get("IBKR_WHOLE_SHARE_ROUND_NEAREST") == "0")
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
        check("paliers: ligne exacte du backtest (1250$, sans +/-10 %)", abs(lo - 1250) < 1e-6 and abs(hi - 1250) < 1e-6,
              f"lo={lo} hi={hi}")
        with with_env({"TRADING_LINE_STEP_PCT": "0"}):
            _, lo0, hi0 = M.dynamic_notional_band_usd(current_price=40.0, atr=1.0, conviction=80, remaining_usd=5000.0,
                                                      slots_left=1, risk_off=False)
            check("sans paliers: couloir 900-1100$", abs(lo0 - 900) < 1e-6 and abs(hi0 - 1100) < 1e-6)


def _daily_trend_frame(end, crash_last=False, nan_extra=None):
    """120 seances en hausse reguliere (tendance +2); option: krach sur la derniere
    (tendance negative si elle est lue) ou ligne en plus avec cloture vide (Yahoo)."""
    import numpy as np
    idx = pd.bdate_range(end=end, periods=120)
    close = 100.0 * 1.01 ** np.arange(120)
    if crash_last:
        close[-1] = close[-2] * 0.5
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                       "Volume": 1e6}, index=idx)
    if nan_extra:
        df.loc[pd.Timestamp(nan_extra)] = [df["Close"].iloc[-1], df["Close"].iloc[-1] * 1.01,
                                           df["Close"].iloc[-1] * 0.99, float("nan"), 1e6]
    return df


def _hourly_frame(last_day, utc=False):
    import numpy as np
    days = pd.bdate_range(end=last_day, periods=15)
    idx = [pd.Timestamp("%s %s" % (d.date(), hm), tz=NY) for d in days
           for hm in ("09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30")]
    idx = pd.DatetimeIndex(idx)
    if utc:
        idx = idx.tz_convert("UTC")
    close = 50.0 + np.sin(np.arange(len(idx)) / 3.0)
    return pd.DataFrame({"Open": close, "High": close + 0.3, "Low": close - 0.3, "Close": close,
                         "Volume": 1e5}, index=idx)


def test_daily_context_before_signal_bar():
    print("\n[12] contexte journalier (tendance, VIX) = seances closes AVANT le jour de la barre du signal")
    import threading
    feed = {}
    saved = (M.fetch_price_data, M.get_latest_news, M.fetch_fundamental_data,
             M.reconcile_analysis_price_usd, M.quick_momentum_score_for_ticker)
    M.fetch_price_data = lambda ticker, period="5d", interval="15m", **k: feed[(ticker, interval)].copy()
    M.get_latest_news = lambda *a, **k: []
    M.fetch_fundamental_data = lambda **k: {"fund_score": 50, "earnings_days": None, "risk_off": False}
    M.reconcile_analysis_price_usd = lambda t, p, entry_price_hint=None: p
    M.quick_momentum_score_for_ticker = lambda t: 0.0
    try:
        with with_env({"MTF_COMPLETED_BARS_ONLY": "1", "SCAN_COMPLETED_BARS_ONLY": "1"}):
            feed[("TST", "1h")] = _hourly_frame("2026-09-25")
            an = M.analyze_ticker("TST", "3mo", "1h")
            check("analyze_ticker: date NY de la barre du signal", an and an.get("bar_date") == "2026-09-25",
                  f"bar_date={an and an.get('bar_date')}")
            feed[("TST", "1h")] = _hourly_frame("2026-09-25", utc=True)
            an = M.analyze_ticker("TST", "3mo", "1h")
            check("analyze_ticker: index UTC -> date NY", an and an.get("bar_date") == "2026-09-25")

            feed[("TST", "1d")] = _daily_trend_frame("2026-09-25", crash_last=True)
            ht = M.get_higher_tf_context("TST", period="1y", interval="1d", before_date="2026-09-25")
            check("tendance: bougie du jour de la barre exclue (+2)", ht["trend_score"] == 2, f"{ht['trend_score']}")
            ht = M.get_higher_tf_context("TST", period="1y", interval="1d")
            check("tendance sans date: derniere bougie close lue (krach -> < 0)", ht["trend_score"] < 0)
            feed[("TST", "1d")] = _daily_trend_frame("2026-09-25", nan_extra="2026-09-28")
            ht = M.get_higher_tf_context("TST", period="1y", interval="1d")
            check("tendance: cloture vide (Yahoo) ignoree", ht["trend_score"] == 2, f"{ht['trend_score']}")

            vix = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": [15.0, 18.0, 25.0, float("nan")],
                                "Volume": 0}, index=pd.DatetimeIndex(["2026-09-23", "2026-09-24", "2026-09-25",
                                                                      "2026-09-28"]))
            feed[("^VIX", "1d")] = vix
            check("VIX: veille de la barre (18 < 22)",
                  M.get_market_regime_risk_off(22.0, before_date="2026-09-25") is False)
            check("VIX sans date: derniere cloture (25, vide ignoree)", M.get_market_regime_risk_off(22.0) is True)

            # cablage du scan: barre 15h30 du 25/09 lue a 16h ou le lendemain matin
            feed[("TST", "1h")] = _hourly_frame("2026-09-25")
            feed[("TST", "1d")] = _daily_trend_frame("2026-09-25", crash_last=True)
            ctx = {"yf_period": "3mo", "yf_interval": "1h", "positions_snapshot": {},
                   "fundamental_enabled": True, "fundamental_cache": {}, "fundamental_cache_hours": 6,
                   "fund_lock": threading.Lock(), "risk_off_regime": True, "risk_off_vix_threshold": 22.0,
                   "risk_off_by_date": {}, "mtf_enabled": True, "mtf_period": "1y", "mtf_interval": "1d",
                   "effective_budget_usd": 1000.0, "deployed_for_prompt": 0.0, "scan_decision_mode": "rules"}
            _, res = M._scan_ticker_llm_worker("TST", ctx)
            check("scan: tendance de la veille de la barre", res and res["higher_tf"]["trend_score"] == 2,
                  f"{res and res['higher_tf']}")
            check("scan: VIX de la veille de la barre", res and res["fund_data"]["risk_off"] is False)
    finally:
        (M.fetch_price_data, M.get_latest_news, M.fetch_fundamental_data,
         M.reconcile_analysis_price_usd, M.quick_momentum_score_for_ticker) = saved

    check("16h00:04 NY: plus d'ordre d'achat (bourse fermee)", M.is_after_us_close(datetime(2026, 9, 28, 16, 0, 4, tzinfo=NY)))
    check("15h59 NY: achat possible", not M.is_after_us_close(ny(2026, 9, 28, 15, 59)))
    check("9h30 NY: achat possible", not M.is_after_us_close(ny(2026, 9, 28, 9, 30)))


def test_calendar_and_order_guards():
    print("\n[13] calendrier NYSE, garde-fous d'achat, etat corrompu, rapprochement IBKR, arret propre")
    with with_env({"TRADING_EXTRA_MARKET_HOLIDAYS": None, "TRADING_EXTRA_EARLY_CLOSES": None}):
        check("Thanksgiving 26/11: bourse fermee", not M.is_us_market_open(ny(2026, 11, 26, 10, 0)))
        check("27/11 12h59: ouverte", M.is_us_market_open(ny(2026, 11, 27, 12, 59)))
        check("27/11 13h00: fermee (fermeture anticipee)", not M.is_us_market_open(ny(2026, 11, 27, 13, 0)))
        check("24/12 13h30: fermee", not M.is_us_market_open(ny(2026, 12, 24, 13, 30)))
        check("16h00 pile: fermee", not M.is_us_market_open(ny(2026, 10, 26, 16, 0)))
        check("9h30 pile: ouverte", M.is_us_market_open(ny(2026, 10, 26, 9, 30)))
        check("ouverture suivante apres le 25/11 au soir = 27/11 9h30",
              M.get_next_us_market_open(ny(2026, 11, 25, 17, 0)) == ny(2026, 11, 27, 9, 30))
        with with_env({"TRADING_MAX_HOLD_SESSIONS": "5", "TRADING_MAX_HOLD_MIN_BEFORE_CLOSE": "30"}):
            check("27/11: fenetre de sortie 12h30-13h00", M.is_max_hold_window(ny(2026, 11, 27, 12, 40))
                  and not M.is_max_hold_window(ny(2026, 11, 27, 15, 40)))
            check("Thanksgiving: aucune sortie forcee", not M.is_max_hold_window(ny(2026, 11, 26, 15, 40)))
        h = _bars(pd.DatetimeIndex([pd.Timestamp("2026-11-27 11:30", tz=NY), pd.Timestamp("2026-11-27 12:30", tz=NY)]))
        check("27/11 13h05: barre 12h30 cloturee a 13h", len(M.drop_forming_bar(h, "1h", ny(2026, 11, 27, 13, 5))) == 2)

        an = {"bar_ts": "2026-10-26T10:30:00-04:00", "data_source": "yahoo"}
        open_t = ny(2026, 10, 26, 11, 30)
        check("garde: premier scan apres la barre -> achat autorise",
              M.scan_buy_block_reason(an, bar_already_seen=False, now=open_t) == "")
        check("garde: barre deja evaluee -> refuse",
              "deja evaluee" in M.scan_buy_block_reason(an, bar_already_seen=True, now=open_t))
        check("garde: sortie duree max dans le scan -> refuse",
              M.scan_buy_block_reason(an, bar_already_seen=False, entries_blocked_reason="sortie", now=open_t) == "sortie")
        check("garde: bougies IBKR -> refuse", "ibkr" in M.scan_buy_block_reason(
            {**an, "data_source": "ibkr"}, bar_already_seen=False, now=open_t))
        check("garde: jour ferie -> refuse", "fermee" in M.scan_buy_block_reason(
            an, bar_already_seen=False, now=ny(2026, 11, 26, 11, 30)))

    check("sortie sans fill: niveau SL le plus proche du cours",
          M.estimate_exit_without_fill(91.0, 90.0, 125.0, 100.0) == (90.0, "estime_niveau_sl"))
    check("sortie sans fill: niveau TP", M.estimate_exit_without_fill(124.0, 90.0, 125.0, 100.0) == (125.0, "estime_niveau_tp"))
    check("sortie sans fill ni SL/TP: cours", M.estimate_exit_without_fill(97.0, None, None, 100.0) == (97.0, "reference_price"))

    leak = "HTTPSConnectionPool: /bot1234567890:FAKEtokenFAKEtokenFAKEtoken0123456789/getUpdates ?token=abc123&x=1 apikey=zz"
    red = M.redact_secrets(leak)
    check("token Telegram masque", "FAKEtoken" not in red and "bot<TOKEN>" in red, red)
    check("cles API en query string masquees", "abc123" not in red and "apikey=<SECRET>" in red, red)

    tmp = tempfile.mkdtemp()
    pf = os.path.join(tmp, "positions.json")
    with open(pf, "w", encoding="utf-8") as f:
        f.write('{"PLTR": {"in_position": tr')
    with with_env({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
        got = M.load_positions(pf)
    check("positions.json illisible: etat vide + achats bloques", got == {} and M.positions_file_corrupt())
    check("copie de sauvegarde creee", any(n.startswith("positions.json.corrupt-") for n in os.listdir(tmp)))
    M.save_positions(pf, {})
    with open(pf, encoding="utf-8") as f:
        check("fichier illisible jamais ecrase", f.read().startswith('{"PLTR"'))
    with open(pf, "w", encoding="utf-8") as f:
        json.dump({"PLTR": {"in_position": False}}, f)
    check("fichier repare: deblocage", M.load_positions(pf) and not M.positions_file_corrupt())

    import ibkr_execution as X
    saved = (X.reconcile_ibkr_orders_and_positions, M.ibkr_auto_execute_enabled)
    X.reconcile_ibkr_orders_and_positions = lambda: (
        {"RKLB": (14.0, 50.0), "AAPL": (3.0, 200.0), "SOFI": (-5.0, 15.0)}, ["BUY HOOD MKT #12"])
    M.ibkr_auto_execute_enabled = lambda: True
    try:
        jf = os.path.join(tmp, "journal.jsonl")
        positions = {}
        with with_env({"TICKERS_OVERRIDE": "PLTR,RKLB,SOFI", "BUY_FIXED_SL_TP_ENABLED": "1", "BUY_FIXED_STOP_PCT": "10",
                       "BUY_RR_REWARD_MULT": "2.5", "BUY_SNIPER_MODE": "1", "BUY_SNIPER_MAX_STOP_PCT": "10"}):
            n = M.reconcile_ibkr_positions(positions, pf, journal_file=jf, journal_enabled=True)
        st = positions.get("RKLB", {})
        check("position IBKR inconnue de l'univers: reprise en gestion", n == 1 and st.get("in_position")
              and abs(float(st.get("entry_price_usd", 0)) - 50.0) < 1e-9, str(st))
        check("reprise: SL -10 % / TP +25 % du prix moyen",
              abs(float(st.get("stop_loss", 0)) - 45.0) < 0.011 and abs(float(st.get("take_profit", 0)) - 62.5) < 0.011)
        check("hors univers et short: pas repris", "AAPL" not in positions and not positions.get("SOFI", {}).get("in_position"))
        with open(jf, encoding="utf-8") as f:
            ev = [json.loads(x) for x in f if x.strip()]
        check("reprise tracee au journal", any(e.get("event") == "confirm_buy" and e.get("source") == "ibkr_reconcile"
                                               for e in ev))
    finally:
        X.reconcile_ibkr_orders_and_positions, M.ibkr_auto_execute_enabled = saved

    jf2 = os.path.join(tmp, "pdt.jsonl")
    with open(jf2, "w", encoding="utf-8") as f:
        f.write(json.dumps({"event": "ibkr_order_filled", "side": "BUY", "ticker": "HOOD",
                            "ts_utc": "2026-10-26T14:00:00Z"}) + "\n")
        f.write(json.dumps({"event": "trade_closed", "ticker": "HOOD", "ts_utc": "2026-10-27T14:05:00Z",
                            "exit_fill_ts_utc": "2026-10-26T18:30:00Z"}) + "\n")
    check("PDT: stop du jour constate le lendemain = day trade",
          M.count_recent_day_trades(jf2, ny(2026, 10, 27, 12, 0)) == 1)

    calls = []

    @M.order_critical
    def fake_order():
        M._STOP_REQUESTED = True     # SIGTERM recu pendant l'ordre
        calls.append("fini")
        return "ok"

    try:
        fake_order()
        check("SIGTERM pendant un ordre: arret apres l'ordre", False)
    except SystemExit:
        check("SIGTERM pendant un ordre: l'ordre se termine, puis arret", calls == ["fini"])
    finally:
        M._STOP_REQUESTED = False


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
    test_daily_context_before_signal_bar()
    test_calendar_and_order_guards()
    import test_execution_safety as T
    print("\nResultat: %d OK / %d FAIL" % (T.PASS, T.FAIL))
    return T.FAIL


if __name__ == "__main__":
    sys.exit(1 if main_tests() else 0)
