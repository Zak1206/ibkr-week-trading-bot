# -*- coding: utf-8 -*-
"""
Tests de securite execution (fonctions pures, sans connexion IBKR).
Lancement:  .\\.venv\\Scripts\\python.exe scripts\\test_execution_safety.py
Couvre: SL/TP fixes -2%/+4% au fill, priorite du mode fixe sur le plancher ATR,
statuts d'ordres enfants SL/TP, sizing actions entieres.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def with_env(overrides: dict):
    """Context manager: applique des env vars puis restaure."""
    class _Ctx:
        def __enter__(self):
            self.saved = {k: os.environ.get(k) for k in overrides}
            for k, v in overrides.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = str(v)

        def __exit__(self, *a):
            for k, v in self.saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    return _Ctx()


FIXED_ENV = {
    "BUY_FIXED_SL_TP_ENABLED": "1",
    "BUY_FIXED_STOP_PCT": "2.0",
    "BUY_RR_REWARD_MULT": "2.0",
    "BUY_SNIPER_MODE": "1",
    "BUY_SNIPER_MAX_STOP_PCT": "2.0",
    # Planchers ATR volontairement CONTRADICTOIRES pour verifier que le fixe gagne
    "TRADING_MIN_STOP_DIST_PCT": "2.5",
    "TRADING_MAX_STOP_DIST_PCT": "5.0",
    "TRADING_MIN_STOP_ATR_MULT": "2.0",
}


def test_recalc_sl_tp_at_fill_fixed_mode():
    print("\n[1] recalc_sl_tp_at_fill — mode fixe -2%/+4% depuis le prix de fill")
    from ibkr_execution import recalc_sl_tp_at_fill

    with with_env(FIXED_ENV):
        sl, tp = recalc_sl_tp_at_fill(100.0, 95.0, 110.0, signal_price=99.0)
        check("SL = 98.00 (fill 100, stop -2%)", sl == 98.0, f"got {sl}")
        check("TP = 104.00 (fill 100, RR 1:2)", tp == 104.0, f"got {tp}")
        sl, tp = recalc_sl_tp_at_fill(17.75, None, None)
        # 17.75 * 0.98 = 17.395 -> arrondi vers le HAUT = 17.40 (fix rounding
        # bug: round-to-nearest donnait 17.39 et faisait retomber le R:R
        # reellement pose sous BUY_MIN_RR par pur artefact d'arrondi, cf
        # incident SOFI du 13/08/2026).
        check("SOFI-like: SL 17.40 / TP 18.46", (sl, tp) == (17.40, 18.46), f"got {sl}/{tp}")
        # fill invalide -> niveaux d'entree conserves
        sl, tp = recalc_sl_tp_at_fill(0.0, 95.0, 110.0)
        check("fill<=0 conserve SL/TP d'origine", (sl, tp) == (95.0, 110.0), f"got {sl}/{tp}")


def test_fixed_mode_beats_atr_floor_in_main():
    print("\n[2] main.adjust_sl_tp_atr_floor — le mode fixe court-circuite le plancher 2.5%")
    import main as m

    with with_env(FIXED_ENV):
        sl, tp, note = m.adjust_sl_tp_atr_floor(
            entry_price=100.0,
            stop_loss=99.0,   # stop LLM trop serre, serait elargi a 2.5% en mode ATR
            take_profit=103.0,
            atr=3.0,          # ATR enorme: en mode ATR le stop serait a -6%+ (cap 5%)
            ticker="TEST",
        )
        check("SL fixe = 98.00 malgre ATR/plancher", sl == 98.0, f"got {sl}")
        check("TP fixe = 104.00", tp == 104.0, f"got {tp}")
        check("note mentionne SL/TP fixes", bool(note) and "fixes" in note, f"got {note}")
        # Mode fixe OFF -> le plancher ATR reprend la main (stop elargi, jamais resserre)
        with with_env({"BUY_FIXED_SL_TP_ENABLED": "0"}):
            sl2, tp2, _ = m.adjust_sl_tp_atr_floor(
                entry_price=100.0,
                stop_loss=99.0,
                take_profit=103.0,
                atr=3.0,
                ticker="TEST",
            )
            check("mode ATR: stop elargi (< 98)", sl2 is not None and float(sl2) < 98.0, f"got {sl2}")


def test_child_order_status_ok():
    print("\n[3] _child_order_status_ok — statut inconnu sans fill = protection NON active")
    from ibkr_execution import _child_order_status_ok

    check("Submitted -> ok", _child_order_status_ok("Submitted", 0.0))
    check("PreSubmitted -> ok", _child_order_status_ok("PreSubmitted", 0.0))
    check("PendingSubmit -> ok", _child_order_status_ok("PendingSubmit", 0.0))
    check("ApiPending -> ok", _child_order_status_ok("ApiPending", 0.0))
    check("Filled (filled>0) -> ok", _child_order_status_ok("Filled", 1.0))
    check("Cancelled -> ko", not _child_order_status_ok("Cancelled", 0.0))
    check("Rejected -> ko", not _child_order_status_ok("Rejected", 0.0))
    check("Inactive -> ko", not _child_order_status_ok("Inactive", 0.0))
    check("statut vide sans fill -> ko (fix H2)", not _child_order_status_ok("", 0.0))
    check("statut inconnu sans fill -> ko (fix H2)", not _child_order_status_ok("Bizarre", 0.0))
    check("statut inconnu avec fill -> ok", _child_order_status_ok("Bizarre", 2.0))


def test_whole_share_sizing():
    print("\n[4] whole_share_qty_for_notional — regression sizing actions entieres")
    from ibkr_execution import whole_share_qty_for_notional

    with with_env({"IBKR_WHOLE_SHARE_ROUND_NEAREST": "1", "IBKR_WHOLE_SHARE_MAX_UPSCALE_PCT": "40"}):
        qty, adj = whole_share_qty_for_notional(200.0, 17.75)
        check("200$ @ 17.75 -> 11 actions", qty == 11.0, f"got {qty}")
        check("notional ajuste 195.25", abs(adj - 195.25) < 1e-6, f"got {adj}")
        qty, adj = whole_share_qty_for_notional(190.0, 63.3)
        check("190$ @ 63.30 -> 3 actions", qty == 3.0, f"got {qty}")


def test_market_sell_all_restores_protection():
    print("\n[5] market_sell_all — echec de vente => bracket SL/TP repose (fix C2)")
    from ibkr_execution import IBKRSession, ExecutionResult

    # Session factice sans connexion: on simule une vente qui echoue 2 fois
    class FakeSession(IBKRSession):
        def __init__(self):  # pas de super().__init__ (pas d'IB requis)
            self.restored = None
            self.cancelled = False
            self.cancel_wait_ack = None

        def ensure_connected(self):
            pass

        def position_quantity(self, ticker):
            return 14.0

        def _open_exit_orders_for_symbol(self, symbol):
            return 17.39, 18.46  # bracket existant

        def _cancel_open_orders_for_symbol(self, symbol, *, wait_ack=True, timeout_sec=8.0, only_protective=False):
            self.cancelled = True
            self.cancel_wait_ack = wait_ack
            return True

        def _contract(self, ticker):
            raise RuntimeError("Prix de marche IBKR indisponible")  # force l'echec vente

        def _restore_protective_orders(self, ticker, stop_loss, take_profit):
            self.restored = (stop_loss, take_profit)
            return f"SL/TP reposes ({stop_loss:.2f}/{take_profit:.2f})"

    with with_env({"IBKR_ORDER_RETRY": "1", "IBKR_ORDER_RETRY_DELAY_SEC": "0"}):
        s = FakeSession()
        res = s.market_sell_all("SOFI")
        check("vente en echec (ok=False)", isinstance(res, ExecutionResult) and not res.ok)
        check("bracket annule avant vente", s.cancelled)
        check("cancel avec wait_ack", s.cancel_wait_ack is True)
        check("bracket repose apres echec (fix C2)", s.restored == (17.39, 18.46), f"got {s.restored}")
        check("note visible dans l'erreur", "SL/TP reposes" in (res.error or ""), f"got {res.error}")


def test_market_sell_abort_on_cancel_ack_timeout():
    print("\n[5b] market_sell_all — ACK cancel timeout => pas de MKT + restore")
    from ibkr_execution import IBKRSession, ExecutionResult

    class FakeSession(IBKRSession):
        def __init__(self):
            self.restored = None
            self.mkt_attempted = False

        def ensure_connected(self):
            pass

        def position_quantity(self, ticker):
            return 4.0

        def _open_exit_orders_for_symbol(self, symbol):
            return 150.0, 160.0

        def _cancel_open_orders_for_symbol(self, symbol, *, wait_ack=True, timeout_sec=8.0, only_protective=False):
            return False  # STP encore actif

        def _contract(self, ticker):
            self.mkt_attempted = True
            raise AssertionError("ne doit pas placer MKT")

        def _restore_protective_orders(self, ticker, stop_loss, take_profit):
            self.restored = (stop_loss, take_profit)
            return "SL/TP reposes"

    s = FakeSession()
    res = s.market_sell_all("COIN")
    check("vente abort (ok=False)", isinstance(res, ExecutionResult) and not res.ok)
    check("pas de MKT envoye", not s.mkt_attempted)
    check("restore appele", s.restored == (150.0, 160.0), f"got {s.restored}")
    check("erreur mentionne ACK", "ACK" in (res.error or "") or "cancel" in (res.error or "").lower(), f"got {res.error}")


def test_sell_signal_kill_switch():
    print("\n[6] sell_signal_enabled — kill-switch P2 du signal VENDRE IA")
    import main as bot

    with with_env({"TRADING_SELL_SIGNAL_ENABLED": None}):
        check("absent -> actif (defaut retro-compatible)", bot.sell_signal_enabled())
    with with_env({"TRADING_SELL_SIGNAL_ENABLED": "1"}):
        check("1 -> actif", bot.sell_signal_enabled())
    with with_env({"TRADING_SELL_SIGNAL_ENABLED": "0"}):
        check("0 -> coupe (P2)", not bot.sell_signal_enabled())
    with with_env({"TRADING_SELL_SIGNAL_ENABLED": "false"}):
        check("false -> coupe", not bot.sell_signal_enabled())
    with with_env({"TRADING_SELL_SIGNAL_ENABLED": " 0 "}):
        check("espaces toleres", not bot.sell_signal_enabled())


def test_buy_min_rr_aligned_with_fixed_tp():
    print("\n[7] BUY_MIN_RR proche du R:R fixe 1.75 (gate ACHETER, seuil 1.74 depuis 13/08/2026)")
    from dotenv import load_dotenv

    root = os.path.dirname(os.path.dirname(__file__))
    load_dotenv(os.path.join(root, ".env"), override=False)
    # Profil LIVE (pas paper): c'est celui qui trade avec de l'argent reel.
    load_dotenv(os.path.join(root, ".env.ibkr_live"), override=True)
    stop = float(os.getenv("BUY_FIXED_STOP_PCT", "2.0"))
    rr_mult = float(os.getenv("BUY_RR_REWARD_MULT", "1.75"))
    min_rr = float(os.getenv("BUY_MIN_RR", "2.0"))
    entry = 100.0
    sl = entry * (1.0 - stop / 100.0)
    tp = entry * (1.0 + stop / 100.0 * rr_mult)
    implied = (tp - entry) / (entry - sl)
    check(f"BUY_MIN_RR charge={min_rr}", abs(min_rr - 1.74) < 1e-9, f"got {min_rr}")
    check(f"implied R:R={implied:.4f}", abs(implied - rr_mult) < 1e-9, f"got {implied}")
    check("gate RR passe (implied >= BUY_MIN_RR)", implied + 1e-9 >= min_rr)

    # Verifie les DEUX implementations reelles (gate main.py + bracket reel
    # ibkr_execution.py), pas juste l'arithmetique ci-dessus — c'est exactement
    # la duplication qui a cause l'incident SOFI (gate corrige, bracket reel
    # encore casse car round() au lieu de ceil() dans l'autre copie).
    import main as bot
    import ibkr_execution as ibkr

    worst_main = 999.0
    worst_ibkr = 999.0
    for entry_px in (18.155, 17.75, 9.995, 100.0, 153.46, 250.0, 1.23):
        sl_m, tp_m = bot.compute_fixed_sl_tp(entry_px)
        rr_m = (tp_m - entry_px) / (entry_px - sl_m) if entry_px > sl_m else 0.0
        worst_main = min(worst_main, rr_m)
        sl_i, tp_i = ibkr._compute_fixed_sl_tp(entry_px)
        rr_i = (tp_i - entry_px) / (entry_px - sl_i) if entry_px > sl_i else 0.0
        worst_ibkr = min(worst_ibkr, rr_i)
    check(
        f"main.compute_fixed_sl_tp: RR realise >= {min_rr} sur tous les prix testes",
        worst_main + 1e-9 >= min_rr,
        f"pire cas RR={worst_main:.4f}",
    )
    check(
        f"ibkr_execution._compute_fixed_sl_tp: RR realise >= {min_rr} sur tous les prix testes",
        worst_ibkr + 1e-9 >= min_rr,
        f"pire cas RR={worst_ibkr:.4f}",
    )


def test_protective_order_filter():
    print("\n[8] _is_protective_exit_order — LMT flat hors OCA ignore")
    from ibkr_execution import IBKRSession

    class O:
        def __init__(self, action, otype, oca=""):
            self.action = action
            self.orderType = otype
            self.ocaGroup = oca

    check("STP protectif", IBKRSession._is_protective_exit_order(O("SELL", "STP")))
    check("LMT OCA protectif", IBKRSession._is_protective_exit_order(O("SELL", "LMT", "oca1")))
    check("LMT flat NON protectif", not IBKRSession._is_protective_exit_order(O("SELL", "LMT", "")))
    check("BUY ignore", not IBKRSession._is_protective_exit_order(O("BUY", "STP")))


def test_ibkr_close_label_no_pnl_heuristic():
    print("\n[9] format_ibkr_close — pas d'inference TP/SL via PnL; foreign clientId")
    import main as bot

    msg = bot.format_ibkr_sl_tp_closed_alert(
        ticker="COIN",
        entry_price_usd=153.5,
        exit_price_usd=152.85,
        pnl_usd=-2.6,
        pnl_pct=-0.42,
        stop_loss=150.39,
        take_profit=158.87,
        exit_price_source="ibkr_fill",
        foreign_client_id=10,
    )
    check("label etranger client 10", "ETRANG" in msg.upper() or "clientId=10" in msg, msg[:120])
    check("pas STOP LOSS heuristique", "STOP LOSS" not in msg, msg[:200])

    msg0 = bot.format_ibkr_sl_tp_closed_alert(
        ticker="COIN",
        entry_price_usd=153.5,
        exit_price_usd=152.85,
        pnl_usd=-2.6,
        pnl_pct=-0.42,
        stop_loss=150.39,
        take_profit=158.87,
        exit_price_source="ibkr_fill",
        foreign_client_id=0,
    )
    check("clientId=0 = manuel/TWS", "clientId=0" in msg0 or "MANUEL" in msg0.upper(), msg0[:120])

    msg_sl = bot.format_ibkr_sl_tp_closed_alert(
        ticker="COIN",
        entry_price_usd=153.5,
        exit_price_usd=150.30,
        pnl_usd=-12.0,
        pnl_pct=-2.0,
        stop_loss=150.39,
        take_profit=158.87,
        exit_price_source="ibkr_fill",
        foreign_client_id=None,
    )
    check("vrai SL par prix", "STOP LOSS" in msg_sl, msg_sl[:120])


def test_flat_catchup_helpers():
    print("\n[10] flat catchup window + poll gate")
    import main as bot
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    with with_env(
        {
            "TRADING_INTRADAY_ONLY": "1",
            "TRADING_INTRADAY_FLAT_ENABLED": "1",
            "TRADING_INTRADAY_FLAT_CATCHUP_MAX_MIN_AFTER_CLOSE": "240",
        }
    ):
        open_am = datetime(2026, 8, 10, 10, 0, tzinfo=ny)
        after = datetime(2026, 8, 10, 16, 30, tzinfo=ny)
        late = datetime(2026, 8, 10, 23, 0, tzinfo=ny)
        check("10h NY: pas catchup", not bot.is_intraday_flat_catchup_window(open_am))
        check("16h30 NY: catchup", bot.is_intraday_flat_catchup_window(after))
        check("23h NY: hors catchup max", not bot.is_intraday_flat_catchup_window(late))


def main_tests():
    print("=== Tests securite execution (sans IBKR) ===")
    test_recalc_sl_tp_at_fill_fixed_mode()
    test_fixed_mode_beats_atr_floor_in_main()
    test_child_order_status_ok()
    test_whole_share_sizing()
    test_market_sell_all_restores_protection()
    test_market_sell_abort_on_cancel_ack_timeout()
    test_sell_signal_kill_switch()
    test_buy_min_rr_aligned_with_fixed_tp()
    test_protective_order_filter()
    test_ibkr_close_label_no_pnl_heuristic()
    test_flat_catchup_helpers()
    print(f"\nResultat: {PASS} OK / {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main_tests()
