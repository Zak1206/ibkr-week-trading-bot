"""
Execution automatique des ordres via Interactive Brokers (IB Gateway / TWS + ib_insync).
Gestion rejets / partiels / retry orientee live.
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

_IBKR_PRICE_CACHE: Dict[str, Tuple[float, float]] = {}
_IBKR_PRICE_FAIL_LOG_TS: Dict[str, float] = {}
_IBKR_CONTRACT_CACHE: Dict[str, object] = {}


class _IbkrWrapperLogFilter(logging.Filter):
    """Reduit le spam console (reconnexions TWS, account summary, market data paper)."""

    _NOISE_MARKERS = (
        "Error 1100,",
        "Error 1102,",
        "Error 322,",
        "Error 10089,",
        "Error 300,",
        "Error 2104,",
        "Error 2106,",
        "Error 10197,",
        "account summary requests exceeded",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(m in msg for m in self._NOISE_MARKERS)


def _install_ib_insync_patches(ib: object) -> None:
    """
    ib_insync relance reqAccountSummary sur erreur 1102 sans desabonner -> boucle 322.
    Le bot n'utilise pas accountSummary : on neutralise ce comportement.
    """
    if getattr(ib, "_tb_patched", False):
        return
    setattr(ib, "_tb_patched", True)

    def _on_error_no_acct_summary_resubscribe(
        req_id: int, error_code: int, error_string: str, contract: object
    ) -> None:
        if error_code == 1102:
            return

    ib._onError = _on_error_no_acct_summary_resubscribe  # type: ignore[attr-defined]

    wrapper_logger = logging.getLogger("ib_insync.wrapper")
    if not any(isinstance(f, _IbkrWrapperLogFilter) for f in wrapper_logger.filters):
        wrapper_logger.addFilter(_IbkrWrapperLogFilter())


def _ibkr_price_cache_ttl_sec() -> float:
    try:
        return max(2.0, float(os.getenv("IBKR_PRICE_CACHE_SEC", "10")))
    except ValueError:
        return 10.0

try:
    from ib_insync import IB, LimitOrder, MarketOrder, Stock, StopOrder
    from ib_insync.order import OrderStatus as IBOrderStatus
except ImportError:  # pragma: no cover
    IB = None  # type: ignore
    LimitOrder = None  # type: ignore
    MarketOrder = None  # type: ignore
    Stock = None  # type: ignore
    StopOrder = None  # type: ignore
    IBOrderStatus = None  # type: ignore

# Statuts IBKR = rejet / annulation (pas de fill utilisable)
_REJECT_STATUSES = frozenset(
    {"Cancelled", "ApiCancelled", "Inactive", "PendingCancel", "Rejected"}
)

# Statuts IBKR = ordre accepte / en attente d'execution (ordre enfant SL/TP considere actif)
_WORKING_STATUSES = frozenset({"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted"})


def _child_order_status_ok(status: str, filled: float) -> bool:
    """
    Un ordre enfant SL/TP n'est considere actif que sur statut working/filled explicite.
    Statut inconnu sans fill -> False (ne jamais supposer qu'une protection est en place).
    """
    if status in _REJECT_STATUSES:
        return False
    if status in _WORKING_STATUSES:
        return True
    return float(filled or 0.0) > 0


@dataclass
class ExecutionResult:
    ok: bool
    ticker: str
    side: str
    quantity: float = 0.0
    fill_price: float = 0.0
    notional_usd: float = 0.0
    order_id: int = 0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    sl_tp_placed: bool = False
    sl_order_id: int = 0
    tp_order_id: int = 0
    order_status: str = ""
    error: str = ""
    warning: str = ""
    partial_fill: bool = False
    retried: bool = False
    ib_log_tail: str = ""
    requested_qty: float = 0.0


def ibkr_package_available() -> bool:
    return IB is not None


def ibkr_place_sl_tp_enabled() -> bool:
    return os.getenv("IBKR_PLACE_SL_TP", "1").strip().lower() in {"1", "true", "yes", "y"}


def ibkr_order_retry_enabled() -> bool:
    return os.getenv("IBKR_ORDER_RETRY", "1").strip().lower() in {"1", "true", "yes", "y"}


def ibkr_fractional_enabled() -> bool:
    return os.getenv("IBKR_ALLOW_FRACTIONAL", "0").strip().lower() in {"1", "true", "yes", "y"}


def ibkr_our_client_id() -> int:
    return _env_int("IBKR_CLIENT_ID", 1)


def ibkr_order_ref() -> str:
    return (os.getenv("IBKR_ORDER_REF", "TradingBot").strip() or "TradingBot")[:32]


def ibkr_guard_foreign_orders_enabled() -> bool:
    return os.getenv("IBKR_GUARD_FOREIGN_ORDERS", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def _stamp_bot_order(order: object) -> None:
    """Marque tous les ordres du bot pour les distinguer des autres clients API."""
    try:
        order.orderRef = ibkr_order_ref()
    except Exception:
        pass
    acct = os.getenv("IBKR_ACCOUNT", "").strip()
    if acct:
        try:
            order.account = acct
        except Exception:
            pass


def _fixed_sl_tp_enabled() -> bool:
    return os.getenv("BUY_FIXED_SL_TP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "y"}


def _sniper_mode_enabled() -> bool:
    return os.getenv("BUY_SNIPER_MODE", "0").strip().lower() in {"1", "true", "yes", "y"}


def _fixed_stop_distance_pct() -> float:
    try:
        pct = max(0.5, min(15.0, float(os.getenv("BUY_FIXED_STOP_PCT", "2.5")))) / 100.0
    except ValueError:
        pct = 0.025
    if _sniper_mode_enabled():
        try:
            sniper_cap = max(0.5, min(15.0, float(os.getenv("BUY_SNIPER_MAX_STOP_PCT", "2.0")))) / 100.0
        except ValueError:
            sniper_cap = 0.02
        pct = min(pct, sniper_cap)
    return pct


def _fixed_rr_reward_multiplier() -> float:
    try:
        return max(1.0, min(5.0, float(os.getenv("BUY_RR_REWARD_MULT", "2.0"))))
    except ValueError:
        return 2.0


def _compute_fixed_sl_tp(entry_price: float) -> Tuple[float, float]:
    """
    SL/TP fixes, arrondis vers le haut (jamais au plus proche): garantit que le
    R:R reellement pose chez le broker ne retombe jamais sous le multiplicateur
    nominal a cause du seul arrondi au centime (meme correctif que
    main.compute_fixed_sl_tp — ne pas laisser diverger, c'est le calcul utilise
    au fill reel, pas juste le gate de controle pre-trade).
    """
    if entry_price <= 0:
        return 0.0, 0.0
    risk_usd = entry_price * _fixed_stop_distance_pct()
    sl = math.ceil(max(0.01, entry_price - risk_usd) * 100 - 1e-9) / 100.0
    tp = math.ceil((entry_price + risk_usd * _fixed_rr_reward_multiplier()) * 100 - 1e-9) / 100.0
    return sl, tp


def recalc_sl_tp_at_fill(
    fill_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    *,
    signal_price: Optional[float] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Recalcule SL/TP sur le prix de remplissage IBKR (evite drift prix scan vs fill).
    Mode fixe: SL -X% / TP +R:R depuis le fill. Sinon: conserve les % depuis le signal.
    """
    fill = float(fill_price)
    if fill <= 0:
        return stop_loss, take_profit

    if _fixed_sl_tp_enabled():
        sl_f, tp_f = _compute_fixed_sl_tp(fill)
        return sl_f, tp_f

    try:
        sl_raw = float(stop_loss or 0.0)
        tp_raw = float(take_profit or 0.0)
    except (TypeError, ValueError):
        return stop_loss, take_profit
    sig = float(signal_price or 0.0)
    if sig > 0 and sl_raw > 0 and tp_raw > 0 and sl_raw < sig < tp_raw:
        sl_dist = (sig - sl_raw) / sig
        tp_dist = (tp_raw - sig) / sig
        new_sl = round(max(0.01, fill * (1.0 - sl_dist)), 2)
        new_tp = round(fill * (1.0 + tp_dist), 2)
        if new_sl < fill < new_tp:
            return new_sl, new_tp
    if sig > 0 and sl_raw > 0 and tp_raw > 0:
        sl_usd = sig - sl_raw
        tp_usd = tp_raw - sig
        if sl_usd > 0 and tp_usd > 0:
            new_sl = round(max(0.01, fill - sl_usd), 2)
            new_tp = round(fill + tp_usd, 2)
            if new_sl < fill < new_tp:
                return new_sl, new_tp
    return stop_loss, take_profit


def ibkr_whole_share_round_nearest_enabled() -> bool:
    return os.getenv("IBKR_WHOLE_SHARE_ROUND_NEAREST", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def ibkr_whole_share_max_upscale_pct() -> float:
    """
    Depassement max du ticket (%) si arrondi au plus proche depasse le montant cible.
    IBKR_WHOLE_SHARE_MAX_UPSCALE_PCT prioritaire ; sinon IBKR_FRACTIONAL_FALLBACK_MAX_UPSCALE_PCT ;
    si round-nearest actif et les deux a 0 -> 10% par defaut.
    """
    raw = os.getenv("IBKR_WHOLE_SHARE_MAX_UPSCALE_PCT", "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    legacy = _env_float("IBKR_FRACTIONAL_FALLBACK_MAX_UPSCALE_PCT", 0.0)
    if legacy > 0:
        return legacy
    if ibkr_whole_share_round_nearest_enabled():
        return 12.0
    return 0.0


def whole_share_qty_for_notional(notional_usd: float, px: float) -> Tuple[float, float]:
    """
    Quantite entiere + notional ajuste pour un ticket USD (sans fractional).
    Mode round-nearest (defaut): choisit floor ou round le plus proche du ticket,
    en respectant IBKR_WHOLE_SHARE_MAX_UPSCALE_PCT si le choix depasse le ticket.
    Mode floor (IBKR_WHOLE_SHARE_ROUND_NEAREST=0): comportement historique.
    """
    import math

    if px <= 0 or notional_usd <= 0:
        raise ValueError("notional ou prix invalide")
    raw_qty = notional_usd / px
    floor_qty = max(1, int(math.floor(raw_qty)))
    if ibkr_whole_share_round_nearest_enabled():
        round_qty = max(1, int(round(raw_qty)))
    else:
        round_qty = floor_qty
    max_upscale = ibkr_whole_share_max_upscale_pct()

    def _upscale_pct(adjusted: float) -> float:
        if adjusted <= notional_usd:
            return 0.0
        return ((adjusted - notional_usd) / max(1e-6, notional_usd)) * 100.0

    def _allowed(qty: int) -> bool:
        adjusted = qty * px
        if adjusted <= notional_usd + 1e-6:
            return True
        if max_upscale <= 0:
            return False
        return _upscale_pct(adjusted) <= max_upscale + 1e-9

    candidates: List[Tuple[int, float, float]] = []
    for qty in sorted({floor_qty, round_qty}):
        if not _allowed(qty):
            continue
        adjusted = qty * px
        err = abs(adjusted - notional_usd)
        candidates.append((qty, adjusted, err))

    if candidates:
        best_qty, best_adj, _ = min(candidates, key=lambda item: item[2])
        return float(best_qty), best_adj

    # 2 actions plus proches du ticket mais refusees pour +10.0x% strict (ex. IONQ 129.60 USD)
    round_adj = round_qty * px
    floor_adj = floor_qty * px
    if (
        ibkr_whole_share_round_nearest_enabled()
        and round_qty > floor_qty
        and round_adj > notional_usd + 1e-6
        and (notional_usd - floor_adj) / max(1e-6, notional_usd) > 0.30
        and abs(round_adj - notional_usd) < abs(floor_adj - notional_usd)
        and _upscale_pct(round_adj) <= max_upscale + 2.0 + 1e-9
    ):
        return float(round_qty), round_adj

    adjusted_floor = floor_qty * px
    if adjusted_floor <= notional_usd + 1e-6:
        return float(floor_qty), adjusted_floor

    if max_upscale > 0:
        raise RuntimeError(
            f"Aucune quantite entiere dans +{max_upscale:.0f}% du ticket "
            f"({notional_usd:.2f} USD, px {px:.2f})."
        )
    return float(floor_qty), adjusted_floor


def _is_fractional_unsupported_error(err: str) -> bool:
    """Detecte les rejets IBKR lies aux fractional shares non supportes par le ticker."""
    if not err:
        return False
    low = err.lower()
    # Code 10244 = "cash quantity cannot be used" (FR: "La quantite cash ne peut pas etre utilisee")
    code_markers = ("10244", "error 10244", "errorcode=10244")
    keywords_en = (
        "fractional",
        "cash quantity",
        "cashqty",
        "does not support",
        "not eligible for fractional",
        "cannot be cash quantity",
        "minimum order size",
    )
    keywords_fr = (
        "quantite cash",
        "quantité cash",
        "cash ne peut",
        "fractionnement",
        "non eligible",
        "non éligible",
        "non supporte",
        "non supporté",
        "n'accepte pas",
    )
    return any(k in low for k in (*code_markers, *keywords_en, *keywords_fr))


def is_ibkr_auto_enabled() -> bool:
    if not ibkr_package_available():
        return False
    enabled = os.getenv("IBKR_ENABLED", "0").strip().lower() in {"1", "true", "yes", "y"}
    auto = os.getenv("IBKR_AUTO_EXECUTE", "0").strip().lower() in {"1", "true", "yes", "y"}
    return enabled and auto


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _trade_log_tail(trade, *, max_lines: int = 5) -> str:
    lines: List[str] = []
    try:
        for entry in list(trade.log or [])[-max_lines:]:
            msg = str(getattr(entry, "message", "") or "").strip()
            st = str(getattr(entry, "status", "") or "").strip()
            if msg:
                lines.append(f"{st}: {msg}" if st else msg)
    except Exception:
        pass
    return " | ".join(lines)


def _trade_status_detail(trade) -> Tuple[str, str, str]:
    """Retourne (status, why_held, log_tail)."""
    st = ""
    why = ""
    try:
        st = str(trade.orderStatus.status or "")
        why = str(getattr(trade.orderStatus, "whyHeld", "") or "").strip()
        adv = str(getattr(trade, "advancedError", "") or "").strip()
        if adv and adv not in why:
            why = f"{why} | {adv}".strip(" |")
    except Exception:
        st = "unknown"
    return st, why, _trade_log_tail(trade)


def _format_order_error(trade, *, context: str = "") -> str:
    status, why, log_tail = _trade_status_detail(trade)
    parts = [context] if context else []
    parts.append(f"status={status}")
    if why:
        parts.append(f"detail={why}")
    if log_tail:
        parts.append(f"log={log_tail}")
    return " — ".join(p for p in parts if p)


class IBKRSession:
    """Connexion IBKR partagee (singleton process)."""

    _instance: Optional["IBKRSession"] = None

    def __init__(self) -> None:
        if IB is None:
            raise RuntimeError("ib_insync non installe. Lance: pip install ib-insync")
        self.ib = IB()
        _install_ib_insync_patches(self.ib)
        self._connected = False
        self._contract_by_symbol: Dict[str, object] = {}

    @classmethod
    def get(cls) -> "IBKRSession":
        if cls._instance is None:
            cls._instance = IBKRSession()
        return cls._instance

    def connect(self, *, force: bool = False) -> None:
        if self._connected and self.ib.isConnected() and not force:
            return
        host = os.getenv("IBKR_HOST", "127.0.0.1").strip()
        port = _env_int("IBKR_PORT", 4002)
        client_id = _env_int("IBKR_CLIENT_ID", 1)
        timeout = _env_int("IBKR_CONNECT_TIMEOUT", 20)
        if self.ib.isConnected():
            self.ib.disconnect()
        self.ib.connect(host, port, clientId=client_id, timeout=timeout)
        self._connected = True
        # Type de donnees marche: 1=live, 2=frozen, 3=delayed, 4=delayed-frozen.
        # Paper sans souscription => 3 (delayed) recommande pour eviter Error 10089.
        md_type = _env_int("IBKR_MARKET_DATA_TYPE", 3)
        try:
            self.ib.reqMarketDataType(md_type)
        except Exception:
            pass
        # Master client: voit/annule les ordres des autres clients API (anti-interference).
        # A configurer aussi dans Gateway: API → Master API client ID = IBKR_CLIENT_ID.
        try:
            self.ib.reqAllOpenOrders()
        except Exception:
            pass
        try:
            self.ib.reqAutoOpenOrders(True)
        except Exception:
            pass
        print(
            f"[IBKR] Client API exclusif id={client_id}, orderRef={ibkr_order_ref()}. "
            f"Dans Gateway: Master API client ID = {client_id} "
            "(sinon les autres clients peuvent vendre tes positions)."
        )

    def ensure_connected(self) -> None:
        if not self.ib.isConnected():
            self.connect(force=True)

    def force_reconnect(self, *, cancel_ticker: Optional[str] = None) -> None:
        """Deconnexion + reconnexion propre. Annule les ordres ouverts du ticker si fourni."""
        sym = (cancel_ticker or "").upper().strip()
        if sym and self.ib.isConnected():
            try:
                for trade in list(self.ib.openTrades()):
                    if getattr(trade.contract, "symbol", "").upper() == sym:
                        self.ib.cancelOrder(trade.order)
                self.ib.sleep(0.5)
            except Exception:
                pass
        try:
            self.ib.disconnect()
        except Exception:
            pass
        self._connected = False
        self._contract_by_symbol.clear()
        time.sleep(2.0)
        self.connect(force=True)
        print(f"[IBKR] Reconnexion forcee OK{f' (ticker: {sym})' if sym else ''}.")

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
        self._connected = False

    def account_label(self) -> str:
        self.ensure_connected()
        accounts = self.ib.managedAccounts()
        return accounts[0] if accounts else "unknown"

    def _contract(self, ticker: str) -> Stock:
        symbol = ticker.upper().strip()
        cached = self._contract_by_symbol.get(symbol)
        if cached is not None:
            return cached
        contract = Stock(symbol, "SMART", "USD")
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(f"Contrat IBKR introuvable pour {symbol}")
        self._contract_by_symbol[symbol] = qualified[0]
        return qualified[0]

    def historical_ohlcv(
        self,
        ticker: str,
        *,
        period: str = "1mo",
        interval: str = "15m",
    ):
        """
        Bougies OHLCV via reqHistoricalData (pandas DataFrame index datetime).
        Thread principal uniquement (meme contrainte que le reste IBKR).
        """
        import pandas as pd
        import threading
        from ib_insync import util

        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("historical_ohlcv: thread principal requis")
        symbol = ticker.upper().strip()
        if not symbol or symbol.startswith("^"):
            raise ValueError(f"Ticker IBKR non supporte pour historique: {ticker}")

        bar_map = {
            "1m": "1 min",
            "2m": "2 mins",
            "5m": "5 mins",
            "15m": "15 mins",
            "30m": "30 mins",
            "60m": "1 hour",
            "1h": "1 hour",
            "1d": "1 day",
            "1wk": "1 week",
        }
        bar_size = bar_map.get((interval or "").strip().lower())
        if not bar_size:
            raise ValueError(f"Intervalle IBKR non supporte: {interval}")

        # IB durationStr: S/D/W/M/Y
        p = (period or "1mo").strip().lower()
        duration_map = {
            "1d": "1 D",
            "5d": "5 D",
            "7d": "1 W",
            "1wk": "1 W",
            "2wk": "2 W",
            "1mo": "1 M",
            "3mo": "3 M",
            "6mo": "6 M",
            "1y": "1 Y",
        }
        duration = duration_map.get(p)
        if duration is None:
            # approx depuis secondes
            sec = 30 * 24 * 3600
            if sec <= 86400:
                duration = "1 D"
            elif sec <= 7 * 86400:
                duration = f"{max(1, sec // 86400)} D"
            elif sec <= 30 * 86400:
                duration = "1 M"
            else:
                duration = "3 M"

        # IB limite: 15m max ~1 mois souvent OK; 1h plus long OK.
        self.ensure_connected()
        contract = self._contract(symbol)
        use_rth = os.getenv("IBKR_HIST_USE_RTH", "1").strip().lower() in {"1", "true", "yes", "y"}
        what = os.getenv("IBKR_HIST_WHAT", "TRADES").strip() or "TRADES"
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what,
            useRTH=use_rth,
            formatDate=1,
        )
        if not bars:
            raise ValueError(f"IBKR: aucune bougie pour {symbol} ({duration}/{bar_size})")
        df = util.df(bars)
        if df is None or df.empty:
            raise ValueError(f"IBKR: DataFrame vide pour {symbol}")
        # Colonnes ib_insync: date, open, high, low, close, volume, ...
        rename = {
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "date" in df.columns:
            df = df.set_index("date")
        need = ["Open", "High", "Low", "Close"]
        missing = [c for c in need if c not in df.columns]
        if missing:
            raise ValueError(f"IBKR: colonnes manquantes {missing}")
        if "Volume" not in df.columns:
            df["Volume"] = 0.0
        out = df[need + ["Volume"]].copy()
        out.index = pd.to_datetime(out.index)
        if getattr(out.index, "tz", None) is not None:
            out.index = out.index.tz_localize(None)
        out = out[~out.index.duplicated(keep="last")].sort_index()
        return out

    def _reference_price_ibkr_only(self, contract: Stock) -> float:
        """Cotation IBKR uniquement (live + delayed snapshot), sans fallback externe."""
        ticker = self.ib.reqMktData(contract, "", False, False)
        deadline = time.time() + _env_float("IBKR_PRICE_WAIT_SEC", 4.0)
        while time.time() < deadline:
            self.ib.sleep(0.2)
            px = ticker.marketPrice()
            if px is None or px != px or px <= 0:
                px = (
                    ticker.last
                    or ticker.close
                    or ticker.ask
                    or ticker.bid
                    or getattr(ticker, "delayedLast", None)
                    or getattr(ticker, "delayedClose", None)
                    or getattr(ticker, "delayedAsk", None)
                    or getattr(ticker, "delayedBid", None)
                )
            if px and px > 0:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass
                return float(px)
        try:
            self.ib.cancelMktData(contract)
        except Exception:
            pass

        try:
            self.ib.reqMarketDataType(4)
            snap = self.ib.reqMktData(contract, "", True, False)
            snap_deadline = time.time() + _env_float("IBKR_PRICE_SNAPSHOT_WAIT_SEC", 5.0)
            while time.time() < snap_deadline:
                self.ib.sleep(0.3)
                px = snap.marketPrice()
                if px is None or px != px or px <= 0:
                    px = (
                        getattr(snap, "delayedLast", None)
                        or getattr(snap, "delayedClose", None)
                        or snap.last
                        or snap.close
                    )
                if px and px > 0:
                    return float(px)
        except Exception:
            pass

        raise RuntimeError("Prix de marche IBKR indisponible (live + delayed)")

    def _reference_price(self, contract: Stock) -> float:
        symbol = getattr(contract, "symbol", "").upper().strip()
        try:
            return self._reference_price_ibkr_only(contract)
        except RuntimeError:
            pass

        if os.getenv("IBKR_PRICE_FALLBACK_EXTERNAL", "1").strip().lower() in {"1", "true", "yes", "y"} and symbol:
            try:
                from main import get_reference_price_usd  # type: ignore

                ext_px = get_reference_price_usd(symbol, skip_ibkr=True)
                if ext_px is not None and float(ext_px) > 0:
                    print(f"[IBKR] Prix indisponible IBKR pour {symbol}, fallback externe: {float(ext_px):.2f} USD")
                    return float(ext_px)
            except Exception as exc:
                print(f"[IBKR] Fallback externe echoue pour {symbol}: {exc}")

        raise RuntimeError("Prix de marche IBKR indisponible (live + delayed + fallback externe)")

    def _wait_fill(
        self,
        trade,
        timeout_sec: float,
        *,
        requested_qty: float,
    ) -> Tuple[float, float, str, bool, str]:
        """
        Attend le fill. Retourne (filled, avg, status, partial, log_tail).
        Leve RuntimeError si rejet sans fill.
        """
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            self.ib.sleep(0.3)
            if trade.isDone():
                break
        status, _, log_tail = _trade_status_detail(trade)
        filled = float(trade.orderStatus.filled or 0.0)
        avg = float(trade.orderStatus.avgFillPrice or 0.0)

        if status in _REJECT_STATUSES and filled <= 0:
            raise RuntimeError(_format_order_error(trade, context="Ordre rejete"))

        if filled <= 0 or avg <= 0:
            if status in {"Submitted", "PreSubmitted", "PendingSubmit"}:
                raise RuntimeError(
                    _format_order_error(trade, context=f"Timeout {timeout_sec:.0f}s sans fill")
                )
            raise RuntimeError(_format_order_error(trade, context="Ordre non rempli"))

        partial = requested_qty > 0 and filled + 1e-6 < requested_qty
        if partial and status in _REJECT_STATUSES:
            # Fill partiel puis annulation du reste
            pass
        return filled, avg, status, partial, log_tail

    def bid_ask_snapshot(self, contract: Stock) -> Tuple[Optional[float], Optional[float]]:
        """
        Bid/ask a l'instant T. Sert a mesurer le spread reellement paye — c'est
        l'inconnue majeure du modele de couts (la config bascule en negatif
        au-dela d'environ 5 bps). Ne leve jamais: retourne (None, None) si indisponible.
        """
        ticker = None
        try:
            ticker = self.ib.reqMktData(contract, "", False, False)
            deadline = time.time() + _env_float("IBKR_QUOTE_WAIT_SEC", 2.0)
            while time.time() < deadline:
                self.ib.sleep(0.2)
                bid = getattr(ticker, "bid", None)
                ask = getattr(ticker, "ask", None)
                ok = lambda v: isinstance(v, (int, float)) and v == v and v > 0
                if ok(bid) and ok(ask) and ask >= bid:
                    return float(bid), float(ask)
            return None, None
        except Exception:
            return None, None
        finally:
            if ticker is not None:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass

    @staticmethod
    def _format_spread_note(bid: Optional[float], ask: Optional[float]) -> str:
        if not bid or not ask or ask < bid:
            return "spread inconnu"
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return "spread inconnu"
        bps = 10000.0 * (ask - bid) / mid
        return f"spread {bps:.1f} bps (bid {bid:.4f} / ask {ask:.4f})"

    def _midprice_entry_enabled(self) -> bool:
        """MIDPRICE seulement pour les ENTREES: une sortie sur stop doit partir au marche."""
        return (os.getenv("IBKR_ENTRY_ORDER_TYPE", "MKT").strip().upper() == "MIDPRICE")

    def _try_midprice(
        self,
        contract: Stock,
        *,
        action: str,
        qty: float,
    ) -> Tuple[float, float, int, str]:
        """
        Tente un ordre MIDPRICE (execution au milieu du NBBO ou mieux).
        Retourne (filled, avg, order_id, note). Ne leve jamais: en cas d'echec
        ou de non-execution, retourne filled=0 et l'appelant bascule au marche.

        MIDPRICE n'est pas garanti de s'executer — c'est le compromis: on economise
        environ un demi-spread quand ca passe, on perd quelques secondes sinon.
        """
        from ib_insync import Order

        timeout = _env_float("IBKR_MIDPRICE_TIMEOUT_SEC", 20.0)
        order = Order()
        order.action = action
        order.orderType = "MIDPRICE"
        order.totalQuantity = qty
        order.tif = "DAY"          # MIDPRICE est un ordre de seance
        _stamp_bot_order(order)
        order.transmit = True
        trade = None
        try:
            trade = self.ib.placeOrder(contract, order)
            deadline = time.time() + max(1.0, timeout)
            while time.time() < deadline:
                self.ib.sleep(0.3)
                if trade.isDone():
                    break
            filled = float(trade.orderStatus.filled or 0.0)
            avg = float(trade.orderStatus.avgFillPrice or 0.0)
            if filled + 1e-6 < qty:
                # non rempli ou partiel: on annule le reliquat avant de basculer
                try:
                    self.ib.cancelOrder(order)
                    self.ib.sleep(0.5)
                except Exception as exc:
                    print(f"[IBKR] Annulation MIDPRICE impossible: {exc}")
                filled = float(trade.orderStatus.filled or 0.0)
                avg = float(trade.orderStatus.avgFillPrice or 0.0)
            oid = int(trade.order.orderId or 0)
            if filled > 0 and avg > 0:
                note = ("MIDPRICE rempli" if filled + 1e-6 >= qty
                        else f"MIDPRICE partiel {filled:.0f}/{qty:.0f}")
                return filled, avg, oid, note
            return 0.0, 0.0, oid, f"MIDPRICE non rempli en {timeout:.0f}s"
        except Exception as exc:
            if trade is not None:
                try:
                    self.ib.cancelOrder(order)
                except Exception:
                    pass
            return 0.0, 0.0, 0, f"MIDPRICE indisponible ({exc})"

    def _place_and_wait_market(
        self,
        contract: Stock,
        *,
        action: str,
        quantity: float,
        cash_qty: Optional[float] = None,
    ) -> Tuple[float, float, int, str, bool, str, str]:
        mid_filled = 0.0
        mid_avg = 0.0
        mid_oid = 0
        note = ""
        # Photo du carnet juste avant l'ordre: seule facon de mesurer le spread
        # reellement supporte. Achats uniquement (une vente sur stop ne doit pas attendre).
        if action.upper() == "BUY" and os.getenv(
            "IBKR_LOG_SPREAD", "1"
        ).strip().lower() in {"1", "true", "yes", "y"}:
            b, a = self.bid_ask_snapshot(contract)
            spread_note = self._format_spread_note(b, a)
            note = spread_note
            print(f"[IBKR] {spread_note}")

        # MIDPRICE: entrees uniquement, quantite entiere uniquement
        # (incompatible avec cashQty, et sans objet hors RTH).
        if (
            action.upper() == "BUY"
            and cash_qty is None
            and quantity >= 1
            and self._midprice_entry_enabled()
            and os.getenv("IBKR_OUTSIDE_RTH", "0").strip().lower() not in {"1", "true", "yes", "y"}
        ):
            want = float(max(1, int(quantity)))
            mid_filled, mid_avg, mid_oid, mid_note = self._try_midprice(
                contract, action=action, qty=want
            )
            if mid_note:
                print(f"[IBKR] {mid_note}")
                note = f"{note} | {mid_note}" if note else mid_note
            if mid_filled + 1e-6 >= want and mid_avg > 0:
                return mid_filled, mid_avg, mid_oid, "Filled", False, "", note
            # reliquat a couvrir au marche
            quantity = want - mid_filled

        if cash_qty is not None and cash_qty > 0:
            # Ordre marche en notional USD: IBKR calcule la quantite fractionnelle.
            order = MarketOrder(action, 0)
            order.cashQty = float(round(cash_qty, 2))
            requested_qty = 0.0
        else:
            qty = max(1, int(quantity)) if quantity >= 1 else float(quantity)
            order = MarketOrder(action, qty)
            requested_qty = float(qty)
        order.tif = os.getenv("IBKR_TIF", "DAY").strip() or "DAY"
        if os.getenv("IBKR_OUTSIDE_RTH", "0").strip().lower() in {"1", "true", "yes", "y"}:
            order.outsideRth = True
        _stamp_bot_order(order)
        order.transmit = True
        trade = self.ib.placeOrder(contract, order)
        filled, avg, status, partial, log_tail = self._wait_fill(
            trade,
            _env_float("IBKR_FILL_TIMEOUT_SEC", 45.0),
            requested_qty=requested_qty,
        )
        if mid_filled > 0 and mid_avg > 0 and filled > 0 and avg > 0:
            # fusion du fill MIDPRICE partiel et du complement au marche
            total = mid_filled + filled
            avg = (mid_filled * mid_avg + filled * avg) / total
            filled = total
            note = f"{note} + complement marche"
        return filled, avg, int(trade.order.orderId or 0), status, partial, log_tail, note

    def position_quantity(self, ticker: str) -> float:
        self.ensure_connected()
        symbol = ticker.upper().strip()
        for pos in self.ib.positions():
            c = pos.contract
            if getattr(c, "symbol", "").upper() == symbol:
                return float(pos.position)
        return 0.0

    def last_sell_fill_price_usd(
        self,
        ticker: str,
        *,
        since_utc_iso: Optional[str] = None,
    ) -> Tuple[Optional[float], str]:
        """
        Prix moyen pondere des executions de vente (SLD) IBKR pour le symbole.
        Retourne (prix_usd, source) avec source 'ibkr_fill' ou '' si introuvable.
        """
        symbol = ticker.upper().strip()
        if not symbol:
            return None, ""
        self.ensure_connected()
        since_utc = _parse_utc_iso_z(since_utc_iso) if since_utc_iso else None

        try:
            from ib_insync import ExecutionFilter

            ef = ExecutionFilter()
            ef.symbol = symbol
            ef.secType = "STK"
            self.ib.reqExecutions(ef)
            self.ib.sleep(_env_float("IBKR_EXECUTIONS_WAIT_SEC", 0.6))
        except Exception as exc:
            print(f"[IBKR] reqExecutions {symbol}: {exc}")

        rows: List[Tuple[float, float, Optional[datetime]]] = []
        seen_exec: set = set()
        for fill in list(self.ib.fills()):
            sym = getattr(fill.contract, "symbol", "").upper()
            if sym != symbol:
                continue
            ex = fill.execution
            exec_id = str(getattr(ex, "execId", "") or "")
            if exec_id and exec_id in seen_exec:
                continue
            if exec_id:
                seen_exec.add(exec_id)
            side = str(getattr(ex, "side", "") or "").upper()
            if side not in {"SLD", "SELL"}:
                continue
            px = float(getattr(ex, "price", 0) or 0)
            sh = float(getattr(ex, "shares", 0) or 0)
            if px <= 0 or sh <= 0:
                continue
            rows.append((sh, px, _execution_time_utc(ex)))

        if not rows:
            for trade in list(self.ib.trades()):
                sym = getattr(trade.contract, "symbol", "").upper()
                if sym != symbol:
                    continue
                action = str(getattr(trade.order, "action", "") or "").upper()
                if action != "SELL":
                    continue
                avg = float(trade.orderStatus.avgFillPrice or 0)
                filled = float(trade.orderStatus.filled or 0)
                if avg <= 0 or filled <= 0:
                    continue
                t_utc: Optional[datetime] = None
                for f in trade.fills or []:
                    t_utc = _execution_time_utc(f.execution)
                    if t_utc is not None:
                        break
                rows.append((filled, avg, t_utc))

        if not rows:
            return None, ""

        def _filter_since(
            items: List[Tuple[float, float, Optional[datetime]]],
        ) -> List[Tuple[float, float, Optional[datetime]]]:
            if since_utc is None:
                return items
            out: List[Tuple[float, float, Optional[datetime]]] = []
            for sh, px, t in items:
                if t is None or t >= since_utc:
                    out.append((sh, px, t))
            return out

        scoped = _filter_since(rows)
        if not scoped and since_utc is not None:
            scoped = rows
            print(
                f"[IBKR] {symbol}: aucun fill SLD apres entree — "
                "utilisation de la derniere vente IBKR connue."
            )

        def _vwap(items: List[Tuple[float, float, Optional[datetime]]]) -> float:
            total_sh = sum(x[0] for x in items)
            if total_sh <= 0:
                return 0.0
            return sum(x[0] * x[1] for x in items) / total_sh

        if since_utc is not None:
            timed = [x for x in scoped if x[2] is not None]
            if timed:
                timed.sort(key=lambda x: x[2])  # type: ignore[arg-type]
                latest_t = timed[-1][2]
                latest_rows = [x for x in scoped if x[2] == latest_t]
                px_out = _vwap(latest_rows)
            else:
                px_out = _vwap(scoped)
        else:
            timed = [x for x in scoped if x[2] is not None]
            if timed:
                timed.sort(key=lambda x: x[2])  # type: ignore[arg-type]
                latest_t = timed[-1][2]
                latest_rows = [x for x in scoped if x[2] == latest_t]
                px_out = _vwap(latest_rows)
            else:
                px_out = _vwap(scoped)

        if px_out > 0:
            return float(px_out), "ibkr_fill"
        return None, ""

    @staticmethod
    def _is_protective_exit_order(order: object) -> bool:
        """SL/TP (STP/Trail ou LMT dans un OCA) — pas une vente LMT flat manuelle."""
        if str(getattr(order, "action", "") or "").upper() != "SELL":
            return False
        otype = str(getattr(order, "orderType", "") or "").upper()
        if otype in {"STP", "STP LMT", "TRAIL", "TRAIL LIMIT"}:
            return True
        if otype == "LMT" and str(getattr(order, "ocaGroup", "") or "").strip():
            return True
        return False

    def _cancel_open_orders_for_symbol(
        self,
        symbol: str,
        *,
        wait_ack: bool = True,
        timeout_sec: float = 8.0,
        only_protective: bool = False,
    ) -> bool:
        """
        Annule les ordres ouverts du symbole.
        Si only_protective=True, n'annule que SL/TP (conserve LMT flat / MKT en cours).
        Si wait_ack=True, attend Cancelled/Inactive (anti race: STP qui fillerait
        pendant un MKT sell). Retourne True si plus aucun ordre cible actif, False sinon.
        """
        sym = symbol.upper().strip()

        def _matches(trade: object) -> bool:
            if getattr(trade.contract, "symbol", "").upper() != sym:
                return False
            if only_protective and not self._is_protective_exit_order(trade.order):
                return False
            return True

        for trade in list(self.ib.openTrades()):
            if not _matches(trade):
                continue
            try:
                self.ib.cancelOrder(trade.order)
            except Exception:
                pass
        if not wait_ack:
            self.ib.sleep(0.5)
            return True

        deadline = time.time() + max(1.0, float(timeout_sec))
        terminal = {"Cancelled", "Inactive", "ApiCancelled", "Filled"}
        while time.time() < deadline:
            self.ib.sleep(0.25)
            active = False
            for trade in list(self.ib.openTrades()):
                if not _matches(trade):
                    continue
                status = str(getattr(trade.orderStatus, "status", "") or "")
                if status not in terminal and status not in {"", "PendingCancel"}:
                    active = True
                    break
                if status == "PendingCancel":
                    active = True
                    break
            if not active:
                return True
        leftover = []
        for trade in list(self.ib.openTrades()):
            if not _matches(trade):
                continue
            status = str(getattr(trade.orderStatus, "status", "") or "")
            if status not in {"Cancelled", "Inactive", "ApiCancelled"}:
                leftover.append(f"{getattr(trade.order, 'orderType', '?')}:{status}")
        if leftover:
            print(f"[IBKR] {sym}: cancel ACK timeout — encore actif: {', '.join(leftover)}")
            return False
        return True

    def _is_our_order(self, order: object) -> bool:
        our_id = ibkr_our_client_id()
        our_ref = ibkr_order_ref().lower()
        try:
            cid = int(getattr(order, "clientId", -1) or -1)
        except (TypeError, ValueError):
            cid = -1
        ref = str(getattr(order, "orderRef", "") or "").strip().lower()
        if cid == our_id:
            return True
        if ref and ref == our_ref:
            return True
        return False

    def cancel_foreign_exit_orders(
        self,
        symbols: List[str],
    ) -> List[str]:
        """
        Annule les ordres SELL ouverts qui ne viennent PAS de ce bot
        (autre clientId / sans orderRef TradingBot) sur les symboles geres.
        Necessite Master API client ID = IBKR_CLIENT_ID dans Gateway.
        """
        if not ibkr_guard_foreign_orders_enabled():
            return []
        wanted = {str(s).upper().strip() for s in symbols if str(s).strip()}
        if not wanted:
            return []
        self.ensure_connected()
        try:
            self.ib.reqAllOpenOrders()
            self.ib.sleep(0.4)
        except Exception:
            pass
        cancelled: List[str] = []
        for trade in list(self.ib.openTrades()):
            sym = str(getattr(trade.contract, "symbol", "") or "").upper()
            if sym not in wanted:
                continue
            order = trade.order
            if str(getattr(order, "action", "") or "").upper() != "SELL":
                continue
            status = str(getattr(trade.orderStatus, "status", "") or "")
            if status in {"Filled", "Cancelled", "Inactive", "ApiCancelled"}:
                continue
            if self._is_our_order(order):
                continue
            try:
                cid = int(getattr(order, "clientId", -1) or -1)
            except (TypeError, ValueError):
                cid = -1
            otype = str(getattr(order, "orderType", "") or "")
            try:
                self.ib.cancelOrder(order)
                msg = f"{sym} {otype} clientId={cid}"
                cancelled.append(msg)
                print(f"[IBKR] GARDE: annulation vente etrangere — {msg}")
            except Exception as exc:
                print(f"[IBKR] GARDE: echec annulation {sym} clientId={cid}: {exc}")
        if cancelled:
            self.ib.sleep(0.5)
        return cancelled

    def last_sell_fill_detail(
        self,
        ticker: str,
        *,
        since_utc_iso: Optional[str] = None,
    ) -> Dict[str, object]:
        """Detail du dernier fill SELL (prix, clientId, orderId)."""
        symbol = ticker.upper().strip()
        out: Dict[str, object] = {
            "price": None,
            "source": "",
            "client_id": None,
            "order_id": None,
            "foreign": False,
        }
        if not symbol:
            return out
        self.ensure_connected()
        since_utc = _parse_utc_iso_z(since_utc_iso) if since_utc_iso else None

        # IBKR peut mettre plus d'une seconde a relayer le rapport d'execution
        # apres un fill (surtout sur un gap d'ouverture volatil). Un seul essai
        # court fait tomber sur le fallback reference_price alors que le vrai
        # fill (TP/SL) existe deja cote broker -> plusieurs tentatives avant
        # d'abandonner.
        attempts = max(1, int(_env_float("IBKR_EXECUTIONS_RETRY_ATTEMPTS", 4)))
        wait_sec = _env_float("IBKR_EXECUTIONS_WAIT_SEC", 1.5)
        best: Optional[Tuple[datetime, float, int, int]] = None
        for attempt in range(attempts):
            try:
                from ib_insync import ExecutionFilter

                ef = ExecutionFilter()
                ef.symbol = symbol
                ef.secType = "STK"
                self.ib.reqExecutions(ef)
                self.ib.sleep(wait_sec)
            except Exception as exc:
                print(f"[IBKR] reqExecutions {symbol}: {exc}")

            for fill in list(self.ib.fills()):
                if getattr(fill.contract, "symbol", "").upper() != symbol:
                    continue
                ex = fill.execution
                side = str(getattr(ex, "side", "") or "").upper()
                if side not in {"SLD", "SELL"}:
                    continue
                px = float(getattr(ex, "price", 0) or 0)
                if px <= 0:
                    continue
                t = _execution_time_utc(ex)
                # Horodatage illisible + fenetre since_utc active -> on ne peut
                # pas garantir que ce fill est bien apres l'entree; on l'exclut
                # par prudence plutot que de risquer un fill d'un trade plus ancien.
                if since_utc is not None and (t is None or t < since_utc):
                    continue
                try:
                    cid = int(getattr(ex, "clientId", -1) or -1)
                except (TypeError, ValueError):
                    cid = -1
                try:
                    oid = int(getattr(ex, "orderId", 0) or 0)
                except (TypeError, ValueError):
                    oid = 0
                rank_t = t or datetime.min.replace(tzinfo=timezone.utc)
                if best is None or rank_t >= best[0]:
                    best = (rank_t, px, cid, oid)
            if best is not None:
                break
            if attempt < attempts - 1:
                print(
                    f"[IBKR] Fill SELL {symbol} introuvable (essai {attempt + 1}/{attempts}), nouvelle tentative..."
                )
        if best is None:
            px2, src2 = self.last_sell_fill_price_usd(symbol, since_utc_iso=since_utc_iso)
            out["price"] = px2
            out["source"] = src2
            return out
        _, px, cid, oid = best
        our = ibkr_our_client_id()
        out["price"] = float(px)
        out["source"] = "ibkr_fill"
        out["client_id"] = int(cid)
        out["order_id"] = int(oid)
        out["foreign"] = bool(cid != our)
        # cid=0 = TWS/GUI manuel: pas le bot API — traiter comme etranger/manuel.
        if cid == 0:
            out["foreign"] = True
        return out

    def _cancel_open_buy_orders_for_symbol(self, symbol: str) -> None:
        """Annule uniquement les ordres BUY ouverts (avant retry, anti double-achat)."""
        sym = symbol.upper().strip()
        for trade in list(self.ib.openTrades()):
            c = trade.contract
            if getattr(c, "symbol", "").upper() != sym:
                continue
            if str(getattr(trade.order, "action", "") or "").upper() != "BUY":
                continue
            try:
                self.ib.cancelOrder(trade.order)
            except Exception:
                pass
        self.ib.sleep(0.5)

    def _recover_late_buy_fill(
        self,
        ticker: str,
        qty_before: float,
        *,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        signal_price: Optional[float],
    ) -> Optional[ExecutionResult]:
        """
        Detecte un fill tardif du 1er ordre BUY (timeout puis execution quand meme).
        Si la position existe deja chez IBKR: pose le bracket et retourne un resultat
        ok=True SANS envoyer de 2e ordre (anti double-achat). Sinon retourne None.
        """
        try:
            self.ensure_connected()
            qty_now = float(self.position_quantity(ticker))
        except Exception:
            return None
        filled = qty_now - float(qty_before)
        if filled <= 1e-9:
            return None
        avg = 0.0
        try:
            for tr in list(self.ib.trades()):
                if getattr(tr.contract, "symbol", "").upper() != ticker:
                    continue
                if str(getattr(tr.order, "action", "") or "").upper() != "BUY":
                    continue
                f = float(tr.orderStatus.filled or 0.0)
                a = float(tr.orderStatus.avgFillPrice or 0.0)
                if f > 0 and a > 0:
                    avg = a
        except Exception:
            pass
        if avg <= 0:
            try:
                avg = float(self._reference_price(self._contract(ticker)))
            except Exception:
                avg = 0.0
        sl_fill, tp_fill = recalc_sl_tp_at_fill(
            avg, stop_loss, take_profit, signal_price=signal_price
        )
        if ibkr_fractional_enabled():
            sell_qty: float = float(filled)
        else:
            sell_qty = max(1, int(round(filled)))
        fill_hint = avg
        if fill_hint <= 0 and sl_fill and tp_fill:
            fill_hint = (float(sl_fill) + float(tp_fill)) / 2.0
        sl_placed, sl_px, tp_px, sl_id, tp_id, sl_err = False, 0.0, 0.0, 0, 0, ""
        try:
            contract = self._contract(ticker)
            sl_placed, sl_px, tp_px, sl_id, tp_id, sl_err = self._place_sl_tp_oca(
                contract,
                sell_qty,
                fill_price=fill_hint,
                stop_loss=sl_fill,
                take_profit=tp_fill,
            )
        except Exception as exc:
            sl_err = str(exc)
        warning = "Fill tardif detecte apres timeout — 2e ordre NON envoye (anti double achat)."
        if not sl_placed:
            warning += (
                f"\n🚨 ATTENTION: SL/TP IBKR non poses ({sl_err}). "
                "Surveillance locale active — poser un stop manuellement si possible."
            )
        print(f"[IBKR] {ticker}: fill tardif detecte (qty +{filled:g} @ ~{avg:.2f}) — retry annule.")
        return ExecutionResult(
            ok=True,
            ticker=ticker,
            side="BUY",
            quantity=filled,
            fill_price=avg,
            notional_usd=filled * avg,
            order_id=0,
            stop_loss_price=float(sl_px if sl_placed and sl_px > 0 else float(sl_fill or 0.0)),
            take_profit_price=float(tp_px if sl_placed and tp_px > 0 else float(tp_fill or 0.0)),
            sl_tp_placed=sl_placed,
            sl_order_id=sl_id,
            tp_order_id=tp_id,
            order_status="Filled",
            warning=warning,
            partial_fill=False,
            retried=True,
            requested_qty=float(filled),
        )

    def _child_order_ok(self, trade, timeout_sec: float = 4.0) -> Tuple[bool, str]:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            self.ib.sleep(0.2)
            if trade.isDone():
                break
        status, why, log_tail = _trade_status_detail(trade)
        filled = float(trade.orderStatus.filled or 0)
        if _child_order_status_ok(status, filled):
            return True, ""
        if status in _REJECT_STATUSES:
            return False, _format_order_error(trade, context="Ordre enfant rejete")
        # Statut inconnu sans fill: ne pas considerer la protection comme active.
        return False, _format_order_error(
            trade, context=f"Ordre enfant statut incertain ({status or 'inconnu'})"
        )

    def _place_sl_tp_oca(
        self,
        contract: Stock,
        qty: float,
        *,
        fill_price: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> Tuple[bool, float, float, int, int, str]:
        """
        Pose SL + TP OCA. Retourne (ok, sl_px, tp_px, sl_id, tp_id, error_msg).
        """
        if not ibkr_place_sl_tp_enabled() or StopOrder is None or LimitOrder is None:
            return False, 0.0, 0.0, 0, 0, "IBKR_PLACE_SL_TP=0"
        sl_raw = float(stop_loss or 0.0)
        tp_raw = float(take_profit or 0.0)
        if sl_raw <= 0 or tp_raw <= 0:
            return False, 0.0, 0.0, 0, 0, "SL/TP absents"
        fill = float(fill_price)
        if not (sl_raw < fill < tp_raw):
            return (
                False,
                0.0,
                0.0,
                0,
                0,
                f"Niveaux invalides (stop={sl_raw:.2f}, fill={fill:.2f}, tp={tp_raw:.2f})",
            )

        sl_px = round(sl_raw, 2)
        tp_px = round(tp_raw, 2)
        oca_group = f"EXIT_{contract.symbol}_{int(time.time())}"
        errors: List[str] = []
        # IBKR accepte int ou float pour qty (fractional shares supportes).
        child_qty: float = float(qty) if qty < 1 else (int(qty) if abs(qty - round(qty)) < 1e-6 else float(qty))

        stop_order = StopOrder("SELL", child_qty, sl_px)
        stop_order.tif = "GTC"
        stop_order.ocaGroup = oca_group
        stop_order.ocaType = 1
        _stamp_bot_order(stop_order)
        stop_trade = self.ib.placeOrder(contract, stop_order)
        sl_ok, sl_err = self._child_order_ok(stop_trade)

        tp_order = LimitOrder("SELL", child_qty, tp_px)
        tp_order.tif = "GTC"
        tp_order.ocaGroup = oca_group
        tp_order.ocaType = 1
        _stamp_bot_order(tp_order)
        tp_trade = self.ib.placeOrder(contract, tp_order)
        tp_ok, tp_err = self._child_order_ok(tp_trade)

        if not sl_ok:
            errors.append(f"SL: {sl_err}")
            try:
                self.ib.cancelOrder(tp_trade.order)
            except Exception:
                pass
        if not tp_ok:
            errors.append(f"TP: {tp_err}")
            try:
                self.ib.cancelOrder(stop_trade.order)
            except Exception:
                pass

        if sl_ok and tp_ok:
            return True, sl_px, tp_px, int(stop_trade.order.orderId or 0), int(tp_trade.order.orderId or 0), ""
        return False, sl_px, tp_px, 0, 0, "; ".join(errors)

    def _open_exit_orders_for_symbol(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """Retourne (stop_px, tp_px) des ordres SELL ouverts pour un symbole."""
        sym = symbol.upper().strip()
        stop_px: Optional[float] = None
        tp_px: Optional[float] = None
        for trade in list(self.ib.openTrades()):
            c = trade.contract
            if getattr(c, "symbol", "").upper() != sym:
                continue
            order = trade.order
            status = str(getattr(trade.orderStatus, "status", "") or "")
            if status not in {"Submitted", "PreSubmitted", "PendingSubmit"}:
                continue
            if str(getattr(order, "action", "") or "").upper() != "SELL":
                continue
            otype = str(getattr(order, "orderType", "") or "").upper()
            if otype in {"STP", "STP LMT"}:
                try:
                    stop_px = float(getattr(order, "auxPrice", 0) or 0)
                except Exception:
                    stop_px = None
            elif otype == "LMT":
                try:
                    tp_px = float(getattr(order, "lmtPrice", 0) or 0)
                except Exception:
                    tp_px = None
        return stop_px, tp_px

    def _restore_protective_orders(
        self,
        ticker: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> str:
        """
        Repose le SL/TP OCA apres un echec de vente (le bracket a ete annule avant l'ordre).
        Retourne une note pour le log/Telegram ('' si position deja flat).
        """
        sym = ticker.upper().strip()
        sl = float(stop_loss or 0.0)
        tp = float(take_profit or 0.0)
        try:
            self.ensure_connected()
            qty = float(self.position_quantity(sym))
        except Exception as exc:
            return f"repose SL/TP impossible ({exc}) — position potentiellement SANS protection broker"
        if qty <= 0:
            return ""
        if sl <= 0 or tp <= 0 or sl >= tp:
            return "SL/TP precedents inconnus — position SANS protection broker (poser un stop manuellement)"
        try:
            contract = self._contract(sym)
            fill_hint = (sl + tp) / 2.0
            try:
                px = float(self._reference_price_ibkr_only(contract))
                if sl < px < tp:
                    fill_hint = px
            except Exception:
                pass
            fill_hint = max(sl + 0.01, min(tp - 0.01, fill_hint))
            ok, sl_px, tp_px, _, _, err = self._place_sl_tp_oca(
                contract,
                qty,
                fill_price=fill_hint,
                stop_loss=sl,
                take_profit=tp,
            )
        except Exception as exc:
            ok, sl_px, tp_px, err = False, 0.0, 0.0, str(exc)
        if ok:
            note = f"SL/TP reposes ({sl_px:.2f}/{tp_px:.2f}) — position protegee malgre l'echec de vente"
            print(f"[IBKR] {sym}: {note}.")
            return note
        note = f"echec repose SL/TP ({err}) — position SANS protection broker (poser un stop manuellement)"
        print(f"[IBKR] {sym}: {note}.")
        return note

    def sync_sl_tp_for_open_position(
        self,
        ticker: str,
        *,
        stop_loss: float,
        take_profit: float,
    ) -> Tuple[bool, bool, str]:
        """
        Synchronise les ordres SL/TP ouverts chez IBKR pour une position.
        Retourne (ok, changed, message).
        """
        symbol = ticker.upper().strip()
        if not symbol:
            return False, False, "ticker vide"
        if stop_loss <= 0 or take_profit <= 0:
            return False, False, "SL/TP absents"
        if stop_loss >= take_profit:
            return False, False, f"SL/TP invalides ({stop_loss:.2f}/{take_profit:.2f})"

        self.ensure_connected()
        qty = float(self.position_quantity(symbol))
        if qty <= 0:
            return False, False, f"aucune position IBKR sur {symbol}"
        contract = self._contract(symbol)
        sl_want = round(float(stop_loss), 2)
        tp_want = round(float(take_profit), 2)
        sl_live, tp_live = self._open_exit_orders_for_symbol(symbol)
        if (
            sl_live is not None
            and tp_live is not None
            and abs(float(sl_live) - sl_want) <= 0.01
            and abs(float(tp_live) - tp_want) <= 0.01
        ):
            return True, False, "deja synchro"

        cancelled_ok = self._cancel_open_orders_for_symbol(
            symbol,
            wait_ack=True,
            only_protective=True,
        )
        if not cancelled_ok:
            return False, False, "cancel SL/TP non confirme (ACK timeout) — sync abandonnee"
        fill_hint = (sl_want + tp_want) / 2.0
        try:
            px = float(self._reference_price_ibkr_only(contract))
            if sl_want < px < tp_want:
                fill_hint = px
        except Exception:
            pass
        fill_hint = max(sl_want + 0.01, min(tp_want - 0.01, fill_hint))
        ok, sl_px, tp_px, _, _, err = self._place_sl_tp_oca(
            contract,
            qty,
            fill_price=fill_hint,
            stop_loss=sl_want,
            take_profit=tp_want,
        )
        if not ok:
            # L'ancien bracket a ete annule: reposer l'ancien niveau plutot que rien.
            note = self._restore_protective_orders(symbol, sl_live, tp_live)
            msg = err or "echec pose SL/TP OCA"
            if note:
                msg = f"{msg} | {note}"
            return False, False, msg
        return True, True, f"SL/TP synchro ({sl_px:.2f}/{tp_px:.2f})"

    def market_buy_usd(
        self,
        ticker: str,
        notional_usd: float,
        *,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        signal_price: Optional[float] = None,
    ) -> ExecutionResult:
        ticker = ticker.upper().strip()
        max_ticket = _env_float("IBKR_MAX_NOTIONAL_USD", 0.0)
        if max_ticket > 0:
            notional_usd = min(notional_usd, max_ticket)
        if notional_usd <= 0:
            return ExecutionResult(False, ticker, "BUY", error="notional<=0")

        retried = False
        last_error = ""
        _last_was_presubmitted = False
        qty_before: Optional[float] = None
        buy_order_sent = False

        for attempt in range(2):
            if attempt == 1:
                if not ibkr_order_retry_enabled():
                    break
                delay = _env_float("IBKR_ORDER_RETRY_DELAY_SEC", 2.0)
                if _last_was_presubmitted:
                    print(f"[IBKR] {ticker}: ordre bloque PreSubmitted — reconnexion forcee avant retry.")
                    try:
                        self.force_reconnect(cancel_ticker=ticker)
                    except Exception as reconn_exc:
                        print(f"[IBKR] Reconnexion echouee: {reconn_exc}")
                        time.sleep(delay)
                else:
                    # Anti double-achat: annuler tout ordre BUY residuel du 1er essai
                    # (un timeout ne signifie pas que l'ordre est mort chez IBKR).
                    if buy_order_sent:
                        try:
                            self.ensure_connected()
                            self._cancel_open_buy_orders_for_symbol(ticker)
                        except Exception:
                            pass
                    time.sleep(delay)
                # Anti double-achat: si le 1er ordre a fill malgre le timeout, ne pas racheter.
                if buy_order_sent and qty_before is not None:
                    late = self._recover_late_buy_fill(
                        ticker,
                        qty_before,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        signal_price=signal_price,
                    )
                    if late is not None:
                        return late
                notional_usd *= float(os.getenv("IBKR_ORDER_RETRY_SIZE_MULT", "0.85"))
                retried = True

            try:
                self.ensure_connected()
                contract = self._contract(ticker)
                if qty_before is None:
                    try:
                        qty_before = float(self.position_quantity(ticker))
                    except Exception:
                        qty_before = 0.0
                px = self._reference_price(contract)
                use_fractional = ibkr_fractional_enabled()
                cash_qty: Optional[float] = None
                qty: float
                fractional_fallback_used = False

                if use_fractional:
                    qty = max(0.0, notional_usd / px)
                    cash_qty = notional_usd
                    min_notional = _env_float("IBKR_MIN_NOTIONAL_USD", 1.0)
                    if notional_usd < min_notional:
                        return ExecutionResult(
                            False,
                            ticker,
                            "BUY",
                            error=f"notional {notional_usd:.2f} < min {min_notional:.2f}",
                            retried=retried,
                        )
                else:
                    try:
                        qty, adjusted_notional = whole_share_qty_for_notional(notional_usd, px)
                        if abs(adjusted_notional - notional_usd) > 0.05:
                            mode = "arrondi" if ibkr_whole_share_round_nearest_enabled() else "floor"
                            print(
                                f"[IBKR] {ticker}: {int(qty)} action(s) entiere(s) ({mode}, "
                                f"~{adjusted_notional:.2f} USD, ticket {notional_usd:.2f} USD)."
                            )
                        if adjusted_notional > notional_usd + 1e-6:
                            notional_usd = adjusted_notional
                    except RuntimeError as exc:
                        return ExecutionResult(
                            False,
                            ticker,
                            "BUY",
                            error=str(exc),
                            retried=retried,
                        )

                buy_order_sent = True
                entry_note = ""
                try:
                    filled, avg, order_id, status, partial, log_tail, entry_note = self._place_and_wait_market(
                        contract,
                        action="BUY",
                        quantity=qty,
                        cash_qty=cash_qty,
                    )
                except RuntimeError as exc:
                    err_txt = str(exc)
                    if use_fractional and _is_fractional_unsupported_error(err_txt):
                        try:
                            whole_qty_f, adjusted_notional = whole_share_qty_for_notional(
                                notional_usd, px
                            )
                        except RuntimeError as qty_exc:
                            raise RuntimeError(
                                f"Fractional refuse et quantite entiere impossible: {qty_exc} — {err_txt}"
                            ) from qty_exc
                        whole_qty = int(whole_qty_f)
                        max_ticket_env = _env_float("IBKR_MAX_NOTIONAL_USD", 0.0)
                        ticket_too_big = max_ticket_env > 0 and adjusted_notional > max_ticket_env + 1e-6
                        if ticket_too_big:
                            raise RuntimeError(
                                f"Fractional refuse et arrondi a {whole_qty} action ({adjusted_notional:.2f} USD) "
                                f"> plafond IBKR_MAX_NOTIONAL_USD: {err_txt}"
                            )
                        print(
                            f"[IBKR] Fractional refuse pour {ticker}, retry en quantite entiere "
                            f"{whole_qty} action(s) (~{adjusted_notional:.2f} USD)."
                        )
                        cash_qty = None
                        qty = float(whole_qty)
                        notional_usd = adjusted_notional
                        fractional_fallback_used = True
                        filled, avg, order_id, status, partial, log_tail, entry_note = self._place_and_wait_market(
                            contract,
                            action="BUY",
                            quantity=qty,
                            cash_qty=None,
                        )
                    else:
                        raise
                # qty restante a couvrir par SL/TP = qty remplie (fractionnelle ou entiere)
                if use_fractional and not fractional_fallback_used:
                    sell_qty: float = float(filled)
                else:
                    sell_qty = int(filled) if filled >= 1 else max(1, int(filled))
                sl_fill, tp_fill = recalc_sl_tp_at_fill(
                    avg,
                    stop_loss,
                    take_profit,
                    signal_price=signal_price,
                )
                sig_px = float(signal_price or 0.0)
                if (
                    sl_fill is not None
                    and tp_fill is not None
                    and (sl_fill != stop_loss or tp_fill != take_profit)
                ):
                    sig_txt = f", signal {sig_px:.2f}" if sig_px > 0 else ""
                    print(
                        f"[IBKR] {ticker}: SL/TP recalcules sur fill {avg:.2f}{sig_txt} "
                        f"-> {float(sl_fill):.2f} / {float(tp_fill):.2f}"
                    )
                sl_placed, sl_px, tp_px, sl_id, tp_id, sl_err = self._place_sl_tp_oca(
                    contract,
                    sell_qty,
                    fill_price=avg,
                    stop_loss=sl_fill,
                    take_profit=tp_fill,
                )
                # Retry unique: un rejet transitoire du bracket ne doit pas laisser
                # une position reelle sans stop chez le broker.
                if (
                    not sl_placed
                    and sl_err not in {"IBKR_PLACE_SL_TP=0", "SL/TP absents"}
                    and not str(sl_err).startswith("Niveaux invalides")
                ):
                    self.ib.sleep(1.0)
                    self._cancel_open_orders_for_symbol(
                        ticker,
                        wait_ack=True,
                        only_protective=True,
                    )
                    sl_placed, sl_px, tp_px, sl_id, tp_id, sl_err2 = self._place_sl_tp_oca(
                        contract,
                        sell_qty,
                        fill_price=avg,
                        stop_loss=sl_fill,
                        take_profit=tp_fill,
                    )
                    if sl_placed:
                        print(f"[IBKR] {ticker}: SL/TP poses apres retry.")
                    else:
                        sl_err = f"{sl_err}; retry: {sl_err2}"
                warning = ""
                # Trace du mode d'entree (MIDPRICE / repli marche) pour pouvoir mesurer
                # a posteriori si l'ordre au midpoint fonctionne reellement.
                if entry_note:
                    warning = f"[entree] {entry_note}"
                if fractional_fallback_used:
                    frac_msg = (
                        f"Fractional non supporte pour {ticker} -> arrondi automatique a "
                        f"{int(filled) if filled >= 1 else filled} action(s) (~{filled * avg:.2f} USD)."
                    )
                    warning = f"{warning}\n{frac_msg}" if warning else frac_msg
                if not sl_placed:
                    sl_msg = (
                        f"🚨 ATTENTION: SL/TP IBKR non poses ({sl_err}). "
                        "Surveillance locale active — poser un stop manuellement si possible."
                    )
                    warning = f"{warning}\n{sl_msg}" if warning else sl_msg

                sl_out = float(sl_px if sl_placed and sl_px > 0 else (sl_fill or 0.0) or 0.0)
                tp_out = float(tp_px if sl_placed and tp_px > 0 else (tp_fill or 0.0) or 0.0)
                return ExecutionResult(
                    ok=True,
                    ticker=ticker,
                    side="BUY",
                    quantity=filled,
                    fill_price=avg,
                    notional_usd=filled * avg,
                    order_id=order_id,
                    stop_loss_price=sl_out,
                    take_profit_price=tp_out,
                    sl_tp_placed=sl_placed,
                    sl_order_id=sl_id,
                    tp_order_id=tp_id,
                    order_status=status,
                    warning=warning,
                    partial_fill=partial,
                    retried=retried and attempt > 0,
                    ib_log_tail=log_tail,
                    requested_qty=float(qty),
                )
            except Exception as exc:
                last_error = str(exc)
                _last_was_presubmitted = "PreSubmitted" in last_error or "Timeout" in last_error
                if attempt == 0 and ibkr_order_retry_enabled():
                    continue
                return ExecutionResult(
                    False,
                    ticker,
                    "BUY",
                    error=last_error,
                    retried=retried,
                )

        return ExecutionResult(False, ticker, "BUY", error=last_error or "echec inconnu", retried=retried)

    def market_sell_all(self, ticker: str) -> ExecutionResult:
        ticker = ticker.upper().strip()
        retried = False
        last_error = ""
        prev_sl: Optional[float] = None
        prev_tp: Optional[float] = None
        protection_cancelled = False

        for attempt in range(2):
            if attempt == 1:
                if not ibkr_order_retry_enabled():
                    break
                retried = True
                time.sleep(_env_float("IBKR_ORDER_RETRY_DELAY_SEC", 2.0))

            try:
                self.ensure_connected()
                qty = self.position_quantity(ticker)
                if qty <= 0:
                    return ExecutionResult(
                        False,
                        ticker,
                        "SELL",
                        error="aucune position IBKR (qty=0)",
                        retried=retried,
                    )
                # Memorise le bracket SL/TP existant AVANT annulation: si la vente
                # echoue ensuite, on le repose pour ne jamais laisser la position nue.
                if prev_sl is None and prev_tp is None:
                    prev_sl, prev_tp = self._open_exit_orders_for_symbol(ticker)
                cancelled_ok = self._cancel_open_orders_for_symbol(ticker, wait_ack=True)
                protection_cancelled = True
                qty = self.position_quantity(ticker)
                if qty <= 0:
                    return ExecutionResult(
                        False,
                        ticker,
                        "SELL",
                        error="aucune position IBKR (qty=0)",
                        retried=retried,
                    )
                if not cancelled_ok:
                    # Ne pas envoyer un MKT tant qu'un STP/LMT peut encore filler.
                    note = self._restore_protective_orders(ticker, prev_sl, prev_tp)
                    err = "cancel SL/TP non confirme (ACK timeout) — vente MKT abortée"
                    if note:
                        err = f"{err} | {note}"
                    return ExecutionResult(
                        False,
                        ticker,
                        "SELL",
                        error=err,
                        retried=retried,
                    )
                use_fractional = ibkr_fractional_enabled()
                if use_fractional:
                    sell_qty: float = float(qty)
                else:
                    # Sortie: vendre toute la qty (float) pour ne pas laisser de residu.
                    sell_qty = float(qty) if qty < 1 else (
                        int(qty) if abs(qty - round(qty)) < 1e-6 else float(qty)
                    )
                contract = self._contract(ticker)
                filled, avg, order_id, status, partial, log_tail, _ = self._place_and_wait_market(
                    contract, action="SELL", quantity=sell_qty
                )
                return ExecutionResult(
                    ok=True,
                    ticker=ticker,
                    side="SELL",
                    quantity=filled,
                    fill_price=avg,
                    notional_usd=filled * avg,
                    order_id=order_id,
                    order_status=status,
                    partial_fill=partial,
                    retried=retried and attempt > 0,
                    ib_log_tail=log_tail,
                    requested_qty=float(sell_qty),
                )
            except Exception as exc:
                last_error = str(exc)
                if attempt == 0 and ibkr_order_retry_enabled():
                    continue
                if protection_cancelled:
                    note = self._restore_protective_orders(ticker, prev_sl, prev_tp)
                    if note:
                        last_error = f"{last_error} | {note}"
                return ExecutionResult(
                    False,
                    ticker,
                    "SELL",
                    error=last_error,
                    retried=retried,
                )

        last_error = last_error or "echec inconnu"
        if protection_cancelled:
            note = self._restore_protective_orders(ticker, prev_sl, prev_tp)
            if note:
                last_error = f"{last_error} | {note}"
        return ExecutionResult(False, ticker, "SELL", error=last_error, retried=retried)

    def limit_sell_all(
        self,
        ticker: str,
        min_limit_price: float,
        *,
        force_outside_rth: bool = False,
    ) -> ExecutionResult:
        """Vente limite (plancher) — flat intraday sans accepter une vente sous le gain minimum."""
        ticker = ticker.upper().strip()
        limit_px = round(float(min_limit_price), 2)
        if limit_px <= 0:
            return ExecutionResult(False, ticker, "SELL", error="prix limite invalide")
        retried = False
        last_error = ""
        prev_sl: Optional[float] = None
        prev_tp: Optional[float] = None
        protection_cancelled = False
        outside_rth = force_outside_rth or (
            os.getenv("IBKR_OUTSIDE_RTH", "0").strip().lower() in {"1", "true", "yes", "y"}
        )

        for attempt in range(2):
            if attempt == 1:
                if not ibkr_order_retry_enabled():
                    break
                retried = True
                time.sleep(_env_float("IBKR_ORDER_RETRY_DELAY_SEC", 2.0))

            try:
                self.ensure_connected()
                qty = self.position_quantity(ticker)
                if qty <= 0:
                    return ExecutionResult(
                        False,
                        ticker,
                        "SELL",
                        error="aucune position IBKR (qty=0)",
                        retried=retried,
                    )
                if prev_sl is None and prev_tp is None:
                    prev_sl, prev_tp = self._open_exit_orders_for_symbol(ticker)
                cancelled_ok = self._cancel_open_orders_for_symbol(ticker, wait_ack=True)
                if not cancelled_ok:
                    note = self._restore_protective_orders(ticker, prev_sl, prev_tp)
                    err = (
                        "annulation SL/TP non confirmee — vente limite abandonnee "
                        "(evite double fill)"
                    )
                    if note:
                        err = f"{err} | {note}"
                    return ExecutionResult(False, ticker, "SELL", error=err, retried=retried)
                protection_cancelled = True
                qty = self.position_quantity(ticker)
                if qty <= 0:
                    return ExecutionResult(
                        False,
                        ticker,
                        "SELL",
                        error="aucune position IBKR (qty=0)",
                        retried=retried,
                    )
                use_fractional = ibkr_fractional_enabled()
                if use_fractional:
                    sell_qty: float = float(qty)
                else:
                    sell_qty = int(qty) if qty >= 1 else float(qty)
                contract = self._contract(ticker)
                if LimitOrder is None:
                    return ExecutionResult(
                        False,
                        ticker,
                        "SELL",
                        error="LimitOrder indisponible",
                        retried=retried,
                    )
                order = LimitOrder("SELL", sell_qty, limit_px)
                order.tif = os.getenv("IBKR_TIF", "DAY").strip() or "DAY"
                if outside_rth:
                    order.outsideRth = True
                _stamp_bot_order(order)
                order.transmit = True
                trade = self.ib.placeOrder(contract, order)
                timeout = _env_float("TRADING_INTRADAY_FLAT_LIMIT_TIMEOUT_SEC", 25.0)
                filled, avg, status, partial, log_tail = self._wait_fill(
                    trade,
                    timeout,
                    requested_qty=float(sell_qty),
                )
                if filled <= 0:
                    try:
                        self.ib.cancelOrder(trade.order)
                    except Exception:
                        pass
                    note = self._restore_protective_orders(ticker, prev_sl, prev_tp)
                    err = f"limite {limit_px:.2f} non remplie (status={status})"
                    if note:
                        err = f"{err} | {note}"
                    return ExecutionResult(
                        False,
                        ticker,
                        "SELL",
                        error=err,
                        order_status=status,
                        ib_log_tail=log_tail,
                        retried=retried and attempt > 0,
                    )
                return ExecutionResult(
                    ok=True,
                    ticker=ticker,
                    side="SELL",
                    quantity=filled,
                    fill_price=avg,
                    notional_usd=filled * avg,
                    order_id=int(trade.order.orderId or 0),
                    order_status=status,
                    partial_fill=partial,
                    retried=retried and attempt > 0,
                    ib_log_tail=log_tail,
                    requested_qty=float(sell_qty),
                )
            except Exception as exc:
                last_error = str(exc)
                if attempt == 0 and ibkr_order_retry_enabled():
                    continue
                if protection_cancelled:
                    note = self._restore_protective_orders(ticker, prev_sl, prev_tp)
                    if note:
                        last_error = f"{last_error} | {note}"
                return ExecutionResult(
                    False,
                    ticker,
                    "SELL",
                    error=last_error,
                    retried=retried,
                )

        last_error = last_error or "echec inconnu"
        if protection_cancelled:
            note = self._restore_protective_orders(ticker, prev_sl, prev_tp)
            if note:
                last_error = f"{last_error} | {note}"
        return ExecutionResult(False, ticker, "SELL", error=last_error, retried=retried)


def adjust_notional_for_ibkr_one_share(
    notional_usd: float,
    *,
    price_usd: float,
    remaining_usd: float,
) -> Tuple[float, Optional[str]]:
    """
    Actions entieres uniquement: ajuste le ticket vers >= 1 action si le cash restant le permet.
    Retourne (notional_ajuste, message_erreur_si_impossible).
    """
    if price_usd <= 0 or notional_usd <= 0:
        return notional_usd, None
    blocked = whole_share_auto_buy_blocked(notional_usd, price_usd)
    if blocked:
        return notional_usd, blocked
    target = max(float(notional_usd), float(price_usd))
    if target > float(remaining_usd) + 1e-6:
        return notional_usd, (
            f"Budget restant {remaining_usd:.2f} USD insuffisant pour 1 action "
            f"(~{price_usd:.2f} USD)."
        )
    return target, None


def whole_share_auto_buy_blocked(notional_usd: float, px: float) -> Optional[str]:
    """
    Bloque l'auto-IBKR si 1 action entiere depasse le ticket cible (ex: NVDA ~212 USD vs ticket 130 USD).
    mult=0 desactive le garde-fou.
    """
    mult = _env_float("IBKR_WHOLE_SHARE_MAX_TICKET_MULT", 1.5)
    if mult <= 0 or notional_usd <= 0 or px <= 0:
        return None
    if px > notional_usd * mult:
        return (
            f"1 action (~{px:.2f} USD) > {mult:.1f}x le ticket cible ({notional_usd:.2f} USD) "
            f"— auto IBKR ignore pour ce ticker (fractional indisponible ou arrondi trop cher). "
            f"Utilise /confirm_buy manuel si tu veux 1 action entiere."
        )
    return None


def _parse_utc_iso_z(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _execution_time_utc(execution) -> Optional[datetime]:
    raw = str(getattr(execution, "time", "") or "").strip()
    if not raw:
        return None
    try:
        from ib_insync.util import parseIBDatetime

        parsed = parseIBDatetime(raw)
        if isinstance(parsed, date) and not isinstance(parsed, datetime):
            parsed = datetime.combine(parsed, datetime.min.time())
        if not isinstance(parsed, datetime):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_ibkr_last_sell_fill_price_usd(
    ticker: str,
    *,
    since_utc_iso: Optional[str] = None,
) -> Tuple[Optional[float], str]:
    """Dernier prix de vente execute IBKR (fill), thread principal uniquement."""
    detail = fetch_ibkr_last_sell_fill_detail(ticker, since_utc_iso=since_utc_iso)
    px = detail.get("price")
    src = str(detail.get("source") or "")
    try:
        return (float(px) if px is not None else None), src
    except (TypeError, ValueError):
        return None, src


def fetch_ibkr_last_sell_fill_detail(
    ticker: str,
    *,
    since_utc_iso: Optional[str] = None,
) -> Dict[str, object]:
    """Detail fill SELL (prix + clientId) — detecte les ventes d'autres clients API."""
    import threading

    empty: Dict[str, object] = {
        "price": None,
        "source": "",
        "client_id": None,
        "order_id": None,
        "foreign": False,
    }
    if threading.current_thread() is not threading.main_thread():
        return empty
    if not ibkr_package_available():
        return empty
    symbol = ticker.upper().strip()
    if not symbol:
        return empty
    try:
        session = IBKRSession.get()
        session.ensure_connected()
        return session.last_sell_fill_detail(symbol, since_utc_iso=since_utc_iso)
    except Exception as exc:
        print(f"[IBKR] Fills vente introuvables pour {symbol}: {exc}")
        return empty


def guard_ibkr_foreign_exit_orders(symbols: List[str]) -> List[str]:
    """Annule les ventes etrangeres sur les tickers geres (anti-interference)."""
    if not is_ibkr_auto_enabled() or not ibkr_guard_foreign_orders_enabled():
        return []
    try:
        return IBKRSession.get().cancel_foreign_exit_orders(symbols)
    except Exception as exc:
        print(f"[IBKR] GARDE foreign orders: {exc}")
        return []


def fetch_ibkr_bid_price_usd(ticker: str, *, skip_cache: bool = False) -> Optional[float]:
    """
    Bid IBKR (prix conservateur pour une vente marche).
    Thread principal uniquement. skip_cache=True pour decision intraday flat.
    """
    import threading

    if threading.current_thread() is not threading.main_thread():
        return None
    if not ibkr_package_available():
        return None
    symbol = ticker.upper().strip()
    if not symbol:
        return None
    now = time.time()
    if not skip_cache:
        ttl = _ibkr_price_cache_ttl_sec()
        cached = _IBKR_PRICE_CACHE.get(f"{symbol}:bid")
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]
    try:
        wait_sec = _env_float("IBKR_FLAT_BID_WAIT_SEC", 2.0)
        session = IBKRSession.get()
        session.ensure_connected()
        contract = session._contract(symbol)
        ticker_obj = session.ib.reqMktData(contract, "", False, False)
        deadline = time.time() + max(0.5, wait_sec)
        bid_px: Optional[float] = None
        while time.time() < deadline:
            session.ib.sleep(0.15)
            # Fenetre d'attente: bid uniquement (live ou delayed). Juste apres
            # reqMktData, IBKR envoie le tick "close" (veille) avant le bid reel,
            # et marketPrice() retombe sur ce close — prix faux pour le flat.
            for candidate in (
                ticker_obj.bid,
                getattr(ticker_obj, "delayedBid", None),
            ):
                if candidate is None or candidate != candidate or float(candidate) <= 0:
                    continue
                bid_px = float(candidate)
                break
            if bid_px is not None:
                break
        if bid_px is None:
            # Dernier recours a l'expiration: last puis marketPrice(), en ecartant
            # toute valeur egale au close de la veille (fallback deguise).
            close_px = getattr(ticker_obj, "close", None)
            close_valid = close_px is not None and close_px == close_px and float(close_px) > 0
            for candidate in (
                ticker_obj.last,
                ticker_obj.marketPrice(),
            ):
                if candidate is None or candidate != candidate or float(candidate) <= 0:
                    continue
                if close_valid and float(candidate) == float(close_px):
                    continue
                bid_px = float(candidate)
                break
        try:
            session.ib.cancelMktData(contract)
        except Exception:
            pass
        if bid_px is None or bid_px <= 0:
            return None
        _IBKR_PRICE_CACHE[f"{symbol}:bid"] = (now, bid_px)
        return bid_px
    except Exception as exc:
        print(f"[IBKR] Bid {symbol} indisponible: {exc}")
        return None


def fetch_ibkr_reference_price_usd(ticker: str) -> Optional[float]:
    """Prix IBKR best-effort (sans fallback externe). Thread principal uniquement."""
    import threading

    if threading.current_thread() is not threading.main_thread():
        return None
    if not ibkr_package_available():
        return None
    symbol = ticker.upper().strip()
    if not symbol:
        return None
    now = time.time()
    ttl = _ibkr_price_cache_ttl_sec()
    cached = _IBKR_PRICE_CACHE.get(symbol)
    if cached is not None and (now - cached[0]) < ttl:
        return cached[1]
    try:
        session = IBKRSession.get()
        session.ensure_connected()
        contract = session._contract(symbol)
        px = session._reference_price_ibkr_only(contract)
        _IBKR_PRICE_CACHE[symbol] = (now, float(px))
        return float(px)
    except Exception as exc:
        if cached is not None:
            return cached[1]
        # Evite le spam scan: un seul log par symbole / fenetre.
        last = float(_IBKR_PRICE_FAIL_LOG_TS.get(symbol, 0.0) or 0.0)
        if (now - last) >= 300.0:
            _IBKR_PRICE_FAIL_LOG_TS[symbol] = now
            print(f"[PRIX] IBKR indisponible pour {symbol}: {exc}")
        return None


def connect_ibkr_at_startup() -> Optional[str]:
    if not is_ibkr_auto_enabled():
        return None
    session = IBKRSession.get()
    session.connect()
    acct = session.account_label()
    port = _env_int("IBKR_PORT", 4002)
    paper = port in {4002, 7497}
    mode = "paper" if paper else "live"
    cid = ibkr_our_client_id()
    return (
        f"IBKR connecte ({mode}) — compte {acct}, port {port}, "
        f"clientId={cid}, orderRef={ibkr_order_ref()}"
    )


def execute_market_buy(
    ticker: str,
    notional_usd: float,
    *,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    signal_price: Optional[float] = None,
) -> ExecutionResult:
    return IBKRSession.get().market_buy_usd(
        ticker,
        notional_usd,
        stop_loss=stop_loss,
        take_profit=take_profit,
        signal_price=signal_price,
    )


def execute_market_sell(ticker: str) -> ExecutionResult:
    return IBKRSession.get().market_sell_all(ticker)


def execute_limit_sell(
    ticker: str,
    min_limit_price: float,
    *,
    force_outside_rth: bool = False,
) -> ExecutionResult:
    return IBKRSession.get().limit_sell_all(
        ticker,
        min_limit_price,
        force_outside_rth=force_outside_rth,
    )


def fetch_ibkr_historical_ohlcv(
    ticker: str,
    *,
    period: str = "1mo",
    interval: str = "15m",
) -> "object":
    """OHLCV historique IBKR — thread principal uniquement."""
    import threading

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("fetch_ibkr_historical_ohlcv: thread principal requis")
    if os.getenv("DATA_FALLBACK_TO_IBKR", "1").strip().lower() not in {"1", "true", "yes", "y"}:
        raise RuntimeError("DATA_FALLBACK_TO_IBKR=0")
    return IBKRSession.get().historical_ohlcv(ticker, period=period, interval=interval)


def sync_ibkr_sl_tp_for_position(
    ticker: str,
    *,
    stop_loss: float,
    take_profit: float,
) -> Tuple[bool, bool, str]:
    """
    Aligne les ordres de sortie IBKR (SL/TP OCA) sur les niveaux fournis.
    Retourne (ok, changed, message).
    """
    if not is_ibkr_auto_enabled():
        return False, False, "IBKR auto desactive"
    if not ibkr_place_sl_tp_enabled():
        return False, False, "IBKR_PLACE_SL_TP=0"
    return IBKRSession.get().sync_sl_tp_for_open_position(
        ticker,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
