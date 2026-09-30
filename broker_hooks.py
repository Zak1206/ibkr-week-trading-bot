"""
Branchement execution broker (IBKR) sur le flux signaux / alertes du bot.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

from ibkr_execution import (
    ExecutionResult,
    execute_limit_sell,
    execute_market_buy,
    execute_market_sell,
    is_ibkr_auto_enabled,
)


def ibkr_fallback_to_manual() -> bool:
    return os.getenv("IBKR_AUTO_FALLBACK_MANUAL", "1").strip().lower() in {"1", "true", "yes", "y"}


def format_ibkr_fill_line(result: ExecutionResult) -> str:
    if not result.ok:
        extra = ""
        if result.retried:
            extra = " (apres retry)"
        if result.ib_log_tail:
            extra += f"\nLog IBKR: {result.ib_log_tail}"
        return f"IBKR ECHEC{extra}: {result.error}"
    base = (
        f"IBKR OK {result.side} {result.ticker}: "
        f"{result.quantity:.0f} @ {result.fill_price:.2f} USD "
        f"(~{result.notional_usd:.2f} USD, order #{result.order_id}, status={result.order_status})"
    )
    if result.partial_fill:
        base += f"\nFill PARTIEL ({result.quantity:.0f}/{result.requested_qty:.0f} actions demandees)."
    if result.retried:
        base += "\nOrdre reussi apres 1 retry automatique."
    if result.side == "BUY" and result.sl_tp_placed:
        base += (
            f"\nSL/TP IBKR actifs (OCA GTC): stop {result.stop_loss_price:.2f} | "
            f"TP {result.take_profit_price:.2f} (#{result.sl_order_id} / #{result.tp_order_id})"
        )
    elif result.side == "BUY" and result.warning:
        base += f"\n{result.warning}"
    elif result.side == "BUY":
        base += "\nSL/TP: surveillance locale uniquement."
    if result.warning and result.sl_tp_placed:
        base += f"\n{result.warning}"
    return base


def apply_buy_fill_to_state(
    state: Dict[str, object],
    *,
    notional_usd: float,
    entry_price_usd: float,
    confidence: int,
    portfolio_profile: str,
    take_profit: Optional[float],
    stop_loss: Optional[float],
) -> Dict[str, object]:
    state["in_position"] = True
    state["pending_buy"] = False
    state["pending_sell"] = False
    state["portfolio_profile"] = portfolio_profile
    state["entry_notional_usd"] = float(notional_usd)
    state["entry_price_usd"] = float(entry_price_usd)
    state["entry_confidence"] = int(confidence)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    state["entry_ts_utc"] = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    state["last_sell_alert_ts"] = 0.0
    if take_profit is not None:
        state["take_profit"] = float(take_profit)
    if stop_loss is not None:
        state["stop_loss"] = float(stop_loss)
    return state


def apply_sell_fill_to_state(state: Dict[str, object]) -> Tuple[Dict[str, object], float, float, float, float]:
    """Retourne (state, entry_notional, entry_price, pnl_usd, pnl_pct) avant reset."""
    entry_notional = float(state.get("entry_notional_usd", 0.0) or 0.0)
    entry_price = float(state.get("entry_price_usd", 0.0) or 0.0)
    state["in_position"] = False
    state["pending_buy"] = False
    state["pending_sell"] = False
    state["last_sell_alert_ts"] = 0.0
    state["entry_notional_usd"] = 0.0
    state["entry_price_usd"] = 0.0
    state["entry_confidence"] = 0
    return state, entry_notional, entry_price, 0.0, 0.0


def try_ibkr_buy(
    ticker: str,
    notional_usd: float,
    *,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    signal_price: Optional[float] = None,
) -> Tuple[Optional[ExecutionResult], str]:
    if not is_ibkr_auto_enabled():
        return None, ""
    result = execute_market_buy(
        ticker,
        notional_usd,
        stop_loss=stop_loss,
        take_profit=take_profit,
        signal_price=signal_price,
    )
    return result, format_ibkr_fill_line(result)


def try_ibkr_sell(
    ticker: str,
    *,
    min_limit_price: Optional[float] = None,
    force_outside_rth: bool = False,
) -> Tuple[Optional[ExecutionResult], str]:
    if not is_ibkr_auto_enabled():
        return None, ""
    if min_limit_price is not None and float(min_limit_price) > 0:
        result = execute_limit_sell(
            ticker,
            float(min_limit_price),
            force_outside_rth=force_outside_rth,
        )
    else:
        result = execute_market_sell(ticker)
    return result, format_ibkr_fill_line(result)
