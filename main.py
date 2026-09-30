import argparse
import copy
import difflib
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
from openai import OpenAI
from zoneinfo import ZoneInfo

"""
Bot de trading US: scan multi-titres, signaux LLM, Telegram, suivi des positions (JSON local).
Entree: main() (ligne de commande).
"""

# Univers par defaut (modifiable via TICKERS_OVERRIDE / TICKERS_EXTRA dans .env)
# Defaut si pas de TICKERS_OVERRIDE (volatil / liquide US).
TICKERS = ["TSLA", "NVDA", "AMD", "COIN", "GME", "SNAP"]
PORTFOLIO_PROFILES: Dict[str, List[str]] = {
    # Max mouvement pour un mois de test intensif (seul panier predefini actif).
    "turbo_beta": ["SMCI", "TSLA", "COIN", "HOOD", "OXY", "RKLB"],
    # Profil de transition pour anciennes positions deja ouvertes.
    "legacy_open": ["SMCI", "MSTR", "COIN", "RKLB"],
}

# Anciens tags journal (momentum_mix / rotation_swing) rapproches des rapports turbo_beta.
_PROFILE_EVENTS_MERGED_INTO_TURBO: frozenset[str] = frozenset({"momentum_mix", "rotation_swing"})
MARKET_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MINUTE = 0

_FUNDAMENTAL_CACHE_LOCK = threading.Lock()
_YF_DOWNLOAD_LOCK = threading.Lock()
_BOT_LOCK_FH = None  # handle fichier lock mono-instance (main loop)


def ensure_stdio_utf8() -> None:
    """Evite UnicodeEncodeError (cp1252) sur Windows pour les prints FR / IBKR."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def acquire_bot_singleton_lock(lock_path: Optional[str] = None) -> str:
    """
    Refuse un 2e main.py (Telegram 409 + conflits clientId).
    BOT_SINGLETON_LOCK=0 pour desactiver. Retourne le chemin du lockfile.
    """
    global _BOT_LOCK_FH
    if os.getenv("BOT_SINGLETON_LOCK", "1").strip().lower() not in {"1", "true", "yes", "y"}:
        return ""
    path = (lock_path or os.getenv("BOT_LOCKFILE", "bot_instance.lock") or "bot_instance.lock").strip()
    if not path:
        path = "bot_instance.lock"
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)) or ".", path)

    def _stale_or_free() -> bool:
        if not os.path.exists(path):
            return True
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = (fh.read() or "").strip()
            old_pid = int(raw.split()[0]) if raw else 0
        except Exception:
            return True
        if old_pid == os.getpid():
            return True
        return not _pid_is_alive(old_pid)

    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()}\n")
            _BOT_LOCK_FH = open(path, "a+", encoding="utf-8")
            try:
                import msvcrt

                msvcrt.locking(_BOT_LOCK_FH.fileno(), msvcrt.LK_NBLCK, 1)
            except Exception:
                pass

            def _release() -> None:
                global _BOT_LOCK_FH
                try:
                    if _BOT_LOCK_FH is not None:
                        try:
                            import msvcrt

                            msvcrt.locking(_BOT_LOCK_FH.fileno(), msvcrt.LK_UNLCK, 1)
                        except Exception:
                            pass
                        _BOT_LOCK_FH.close()
                except Exception:
                    pass
                _BOT_LOCK_FH = None
                try:
                    if os.path.exists(path):
                        with open(path, "r", encoding="utf-8") as fh:
                            raw = (fh.read() or "").strip()
                        if raw.startswith(str(os.getpid())):
                            os.remove(path)
                except Exception:
                    pass

            import atexit

            atexit.register(_release)
            return path
        except FileExistsError:
            if _stale_or_free():
                try:
                    os.remove(path)
                except Exception:
                    pass
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    other = (fh.read() or "").strip()
            except Exception:
                other = "?"
            raise SystemExit(
                f"[LOCK] Un autre bot tourne deja (PID {other}, lock={path}). "
                f"Arrete-le ou BOT_SINGLETON_LOCK=0."
            ) from None
    raise SystemExit(f"[LOCK] Impossible d'obtenir le lock {path}.")


def seconds_until_next_time_slot(now: datetime, interval_minutes: int) -> float:
    """
    Secondes a attendre pour tomber sur le prochain creneau d'horloge
    (ex: avec 15 min en America/New_York -> :00, :15, :30, :45).
    Si 'now' est pile sur un creneau, on attend un intervalle complet (prochain tick).
    """
    if interval_minutes <= 0:
        return 0.0
    interval_sec = float(interval_minutes * 60)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_since_midnight = (now - midnight).total_seconds()
    rem = seconds_since_midnight % interval_sec
    if rem < 0.001:
        return interval_sec
    return interval_sec - rem


# Etat des positions local (pour eviter de proposer un VENDRE sans achat confirme)
POSITION_FILE_DEFAULT = "positions.json"
TELEGRAM_OFFSET_FILE_DEFAULT = "telegram_offset.json"
FLASH_STATE_FILE_DEFAULT = "flash_state.json"
SWAP_PENDING_FILE_DEFAULT = "swap_pending.json"
FUNDAMENTAL_CACHE_FILE_DEFAULT = "fundamental_cache.json"
TRADE_JOURNAL_FILE_DEFAULT = "trades_journal.jsonl"
PREMARKET_PREP_STATE_FILE_DEFAULT = "premarket_prep_state.json"
# Incremente cette version quand la logique du plan pre-ouverture change,
# pour autoriser un renvoi unique corrige sur la meme ouverture.
PREMARKET_PLAN_VERSION = "2"
ENV_FILE_ENVVAR = "BOT_ENV_FILE"

# Backoff global Yahoo Finance (anti-429 / anti-spam download).
_YF_BACKOFF_UNTIL_TS = 0.0
_YF_BACKOFF_STRIKES = 0


def _yf_backoff_seconds_left() -> float:
    return max(0.0, float(_YF_BACKOFF_UNTIL_TS) - time.time())


def _yf_backoff_active() -> bool:
    return _yf_backoff_seconds_left() > 0.0


def _looks_like_yf_rate_limit_error(msg: str) -> bool:
    txt = (msg or "").strip().lower()
    if not txt:
        return False
    patterns = (
        "too many requests",
        "edge: too many requests",
        "429",
        "401",
        "unauthorized",
        "invalid crumb",
        "unable to access this feature",
        # Ne PAS traiter empty/"delisted"/OHLC manquant comme rate-limit:
        # Yahoo renvoie souvent empty sur period=1d intraday (bug API), pas un 429.
    )
    return any(p in txt for p in patterns)


def _yahoo_intraday_interval(interval: str) -> bool:
    iv = (interval or "").strip().lower()
    if not iv:
        return False
    if iv.endswith("m") or iv.endswith("h"):
        return True
    return iv in {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}


def _yahoo_fetch_period(period: str, interval: str) -> str:
    """
    Yahoo chart API renvoie souvent vide pour period=1d + intervals intraday
    (message trompeur 'possibly delisted'). On elargit a 5d puis on trimme.
    """
    p = (period or "").strip().lower()
    if p in {"1d", "1day"} and _yahoo_intraday_interval(interval):
        return "5d"
    return period


def _trim_ohlc_to_last_session(data: pd.DataFrame) -> pd.DataFrame:
    if data is None or getattr(data, "empty", True):
        return data
    try:
        idx = data.index
        if not hasattr(idx, "date"):
            return data
        last_day = idx[-1].date()
        mask = [ts.date() == last_day for ts in idx]
        trimmed = data.loc[mask]
        return trimmed if not trimmed.empty else data
    except Exception:
        return data


def _data_fallback_to_finnhub_enabled() -> bool:
    return os.getenv("DATA_FALLBACK_TO_FINNHUB", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def _yf_parallel_stagger_delay(ticker: str) -> None:
    """Ecarte legerement les requetes Yahoo en scan parallele (evite rafales 401)."""
    if not scan_parallel_enabled():
        return
    try:
        sec = float(os.getenv("YF_PARALLEL_STAGGER_SEC", "0.35"))
    except ValueError:
        sec = 0.35
    if sec <= 0:
        return
    workers = scan_parallel_workers()
    idx = abs(hash(normalize_ticker(ticker))) % max(1, workers)
    time.sleep(idx * sec)


def _register_yf_backoff(reason: str) -> None:
    global _YF_BACKOFF_UNTIL_TS, _YF_BACKOFF_STRIKES
    try:
        base_sec = float(os.getenv("YF_BACKOFF_BASE_SEC", "45"))
    except ValueError:
        base_sec = 45.0
    try:
        max_sec = float(os.getenv("YF_BACKOFF_MAX_SEC", "900"))
    except ValueError:
        max_sec = 900.0

    base_sec = max(10.0, base_sec)
    max_sec = max(base_sec, max_sec)
    _YF_BACKOFF_STRIKES = min(8, max(1, int(_YF_BACKOFF_STRIKES) + 1))
    cooldown = min(max_sec, base_sec * (2 ** (_YF_BACKOFF_STRIKES - 1)))
    until = time.time() + cooldown
    if until > float(_YF_BACKOFF_UNTIL_TS):
        _YF_BACKOFF_UNTIL_TS = until
    left = _yf_backoff_seconds_left()
    print(
        f"[DATA] Yahoo rate-limit detecte ({reason}). "
        f"Cooldown auto {left:.0f}s (strike {_YF_BACKOFF_STRIKES})."
    )


def _register_yf_success() -> None:
    global _YF_BACKOFF_STRIKES
    if _YF_BACKOFF_STRIKES > 0:
        _YF_BACKOFF_STRIKES -= 1


def load_runtime_env() -> None:
    """
    Charge la configuration .env par defaut puis, optionnellement,
    un fichier de surcharge (ex: profil live capital) via BOT_ENV_FILE.
    """
    load_dotenv()
    extra_env = os.getenv(ENV_FILE_ENVVAR, "").strip()
    if extra_env:
        if os.path.exists(extra_env):
            load_dotenv(extra_env, override=True)
        else:
            print(f"[Config] Fichier env introuvable (ignore): {extra_env}")


# Garde-fou quota Twelve Data (plan free typique: 8/min, 800/jour).
_TD_LAST_WINDOW_MIN = ""
_TD_MIN_COUNT = 0
_TD_LAST_WINDOW_DAY = ""
_TD_DAY_COUNT = 0
_FLASH_QUOTE_HISTORY: Dict[str, List[Tuple[float, float]]] = {}
_FLASH_QUOTE_CANDIDATES: Dict[str, Dict[str, float]] = {}


def _td_can_consume_credit() -> bool:
    if os.getenv("TWELVEDATA_GUARD_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "y"}:
        return True
    try:
        max_per_min = int(os.getenv("TWELVEDATA_MAX_PER_MIN", "8"))
    except ValueError:
        max_per_min = 8
    try:
        max_per_day = int(os.getenv("TWELVEDATA_MAX_PER_DAY", "800"))
    except ValueError:
        max_per_day = 800
    max_per_min = max(1, max_per_min)
    max_per_day = max(1, max_per_day)

    now_utc = datetime.now(UTC_TZ)
    cur_min = now_utc.strftime("%Y-%m-%dT%H:%M")
    cur_day = now_utc.strftime("%Y-%m-%d")
    global _TD_LAST_WINDOW_MIN, _TD_MIN_COUNT, _TD_LAST_WINDOW_DAY, _TD_DAY_COUNT
    if _TD_LAST_WINDOW_DAY != cur_day:
        _TD_LAST_WINDOW_DAY = cur_day
        _TD_DAY_COUNT = 0
    if _TD_LAST_WINDOW_MIN != cur_min:
        _TD_LAST_WINDOW_MIN = cur_min
        _TD_MIN_COUNT = 0
    return _TD_MIN_COUNT < max_per_min and _TD_DAY_COUNT < max_per_day


def _td_register_consume() -> None:
    now_utc = datetime.now(UTC_TZ)
    cur_min = now_utc.strftime("%Y-%m-%dT%H:%M")
    cur_day = now_utc.strftime("%Y-%m-%d")
    global _TD_LAST_WINDOW_MIN, _TD_MIN_COUNT, _TD_LAST_WINDOW_DAY, _TD_DAY_COUNT
    if _TD_LAST_WINDOW_DAY != cur_day:
        _TD_LAST_WINDOW_DAY = cur_day
        _TD_DAY_COUNT = 0
    if _TD_LAST_WINDOW_MIN != cur_min:
        _TD_LAST_WINDOW_MIN = cur_min
        _TD_MIN_COUNT = 0
    _TD_MIN_COUNT += 1
    _TD_DAY_COUNT += 1


def load_positions(path: str) -> Dict[str, dict]:
    """Charge un etat local des positions (achete/vente en attente)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except Exception:
        # En cas de fichier corrompu, on repart proprement.
        return {}
    return {}


def _atomic_replace_file(tmp: str, path: str, *, retries: int = 5) -> None:
    """Remplace atomiquement un fichier (retries si OneDrive/antivirus verrouille)."""
    last_exc: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt + 1 >= retries:
                break
            time.sleep(0.15 * (attempt + 1))
    if last_exc is not None:
        raise last_exc


def save_positions(path: str, positions: Dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)
    try:
        _atomic_replace_file(tmp, path)
    except PermissionError as exc:
        print(
            f"[Positions] Acces refuse pour {path} (OneDrive/sync?). "
            f"Ferme l'éditeur sur ce fichier ou deplace le projet hors OneDrive. ({exc})"
        )
        raise


def clear_positions_file(path: str) -> None:
    """Reinitialise la memoire locale des positions (nouvelle session)."""
    save_positions(path, {})


def load_flash_state(path: str) -> Dict[str, float]:
    """Charge l'etat anti-spam des alertes FLASH (epoch par cle ticker:side)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            out: Dict[str, float] = {}
            for k, v in data.items():
                try:
                    out[str(k)] = float(v)
                except Exception:
                    continue
            return out
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return {}


def save_flash_state(path: str, state: Dict[str, float]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json_dict(path: str) -> Dict[str, dict]:
    """Charge un dict JSON generique (cache local)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return {}


def save_json_dict(path: str, data: Dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _atomic_replace_file(tmp, path)


def utc_now_iso_z() -> str:
    """Horodatage UTC type journal (remplace datetime.utcnow() deprecie)."""
    return datetime.now(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


# Prefixes de config qui definissent la strategie (pour hash d'attribution des trades)
_STRATEGY_ENV_PREFIXES = (
    "TRADING_", "BUY_", "SELL_", "SWAP_", "MTF_", "FUNDAMENTAL_", "EARNINGS_",
    "RISK_", "RULES_", "SCAN_", "FLASH_", "BREAKOUT_", "PREMARKET_",
)
_LAST_CONFIG_HASH: Optional[str] = None
_CONFIG_SNAPSHOT_IN_PROGRESS = False


def strategy_config_dict() -> Dict[str, str]:
    """Sous-ensemble de l'environnement qui definit la strategie (tri stable)."""
    out: Dict[str, str] = {}
    for k in sorted(os.environ):
        if any(k.startswith(p) for p in _STRATEGY_ENV_PREFIXES):
            out[k] = os.environ[k]
    return out


def strategy_config_hash() -> str:
    """Hash court de la config strategie — permet d'attribuer chaque trade a une version."""
    import hashlib

    cfg = strategy_config_dict()
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:10]


def append_journal_event(path: str, event: Dict[str, object]) -> None:
    """Append un evenement JSONL pour audit/performance (+ hash de config strategie)."""
    global _LAST_CONFIG_HASH, _CONFIG_SNAPSHOT_IN_PROGRESS
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = dict(event)
    payload["ts_utc"] = utc_now_iso_z()
    try:
        cfg_hash = strategy_config_hash()
        payload.setdefault("config_hash", cfg_hash)
        # Nouveau hash jamais vu ce process -> snapshot complet AVANT l'evenement
        # (le journal reste auto-suffisant: hash -> config resolvable localement).
        if cfg_hash != _LAST_CONFIG_HASH and not _CONFIG_SNAPSHOT_IN_PROGRESS:
            _CONFIG_SNAPSHOT_IN_PROGRESS = True
            try:
                snap = {
                    "event": "config_snapshot",
                    "config_hash": cfg_hash,
                    "config": strategy_config_dict(),
                    "ts_utc": utc_now_iso_z(),
                }
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(snap, ensure_ascii=False) + "\n")
                _LAST_CONFIG_HASH = cfg_hash
            finally:
                _CONFIG_SNAPSHOT_IN_PROGRESS = False
    except Exception:
        pass  # le hash ne doit jamais bloquer la journalisation d'un trade
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def rewrite_journal_ticker(path: str, old_ticker: str, new_ticker: str) -> int:
    """
    Corrige les evenements JSONL en remplaçant un ticker par un autre.
    Retourne le nombre de lignes modifiees.
    """
    old_ticker = normalize_ticker(old_ticker)
    new_ticker = normalize_ticker(new_ticker)
    if not old_ticker or not new_ticker or old_ticker == new_ticker:
        return 0

    changed = 0
    rows: List[Dict[str, object]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                txt = line.strip()
                if not txt:
                    continue
                try:
                    event = json.loads(txt)
                except Exception:
                    continue
                if isinstance(event, dict):
                    tk = normalize_ticker(str(event.get("ticker", "")))
                    if tk == old_ticker:
                        event["ticker"] = new_ticker
                        changed += 1
                    rows.append(event)
    except FileNotFoundError:
        return 0
    except Exception:
        return 0

    if changed <= 0:
        return 0

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for ev in rows:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return changed


def normalize_ticker(ticker: str) -> str:
    return (ticker or "").strip().upper()


def get_allowed_command_tickers(positions: Dict[str, dict]) -> List[str]:
    """
    Tickers autorises dans les commandes Telegram manuelles.
    Inclut la watchlist active + tickers deja connus en positions.json.
    """
    allowed = set(get_tickers_from_env())
    allowed.update(normalize_ticker(tk) for tk in positions.keys() if normalize_ticker(tk))
    return sorted(allowed)


def suggest_ticker_typo(ticker: str, allowed_tickers: List[str]) -> Optional[str]:
    """Propose un ticker proche en cas de faute de frappe."""
    if not ticker or not allowed_tickers:
        return None
    matches = difflib.get_close_matches(ticker, allowed_tickers, n=1, cutoff=0.6)
    return matches[0] if matches else None


def get_tickers_from_env() -> List[str]:
    """
    Permet d'ajouter facilement des tickers sans toucher au code.
    - TICKERS_OVERRIDE: remplace completement la liste (ex: "TSLA,NVDA,PLTR")
    - TICKERS_EXTRA: ajoute des tickers (ex: "AMD,SOFI")
    """
    override = os.getenv("TICKERS_OVERRIDE", "").strip()
    if override:
        return [normalize_ticker(t) for t in override.split(",") if normalize_ticker(t)]

    extra = os.getenv("TICKERS_EXTRA", "").strip()
    tickers = set(normalize_ticker(t) for t in TICKERS)
    if extra:
        tickers.update(normalize_ticker(t) for t in extra.split(",") if normalize_ticker(t))
    return sorted(tickers)


def normalize_portfolio_profile_key(value: str) -> str:
    txt = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "env": "current",
        "default": "current",
        "turbo": "turbo_beta",
        "beta": "turbo_beta",
        "momentum": "turbo_beta",
        "mix": "turbo_beta",
        "momentum_mix": "turbo_beta",
        "rotation": "turbo_beta",
        "swing": "turbo_beta",
        "rotation_swing": "turbo_beta",
        "legacy": "legacy_open",
    }
    return aliases.get(txt, txt)


def get_active_portfolio_profile_key() -> str:
    key = normalize_portfolio_profile_key(os.getenv("ACTIVE_PORTFOLIO_PROFILE", "current"))
    if key == "current" or key in PORTFOLIO_PROFILES:
        return key
    return "current"


def infer_portfolio_profile_for_ticker(ticker: str, state: Optional[Dict[str, object]] = None) -> str:
    state_key = normalize_portfolio_profile_key(str((state or {}).get("portfolio_profile", "")))
    if state_key == "current" or state_key in PORTFOLIO_PROFILES:
        return state_key
    active_key = get_active_portfolio_profile_key()
    if active_key == "current" or active_key in PORTFOLIO_PROFILES:
        return active_key
    tk = normalize_ticker(ticker)
    if not tk:
        return "current"
    matches = [k for k, names in PORTFOLIO_PROFILES.items() if tk in set(normalize_ticker(t) for t in names)]
    if len(matches) == 1:
        return matches[0]
    return "current"


def _portfolio_profile_tickers(profile_key: str) -> List[str]:
    key = normalize_portfolio_profile_key(profile_key)
    if key == "current":
        return get_tickers_from_env()
    return list(PORTFOLIO_PROFILES.get(key, []))


def _filter_trade_closed_events_for_profile(
    events: List[Dict[str, object]],
    profile_key: str,
) -> List[Dict[str, object]]:
    key = normalize_portfolio_profile_key(profile_key)
    include_legacy_by_ticker = os.getenv("PORTFOLIO_REPORT_INCLUDE_LEGACY_BY_TICKER", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    profile_tickers = set(normalize_ticker(t) for t in _portfolio_profile_tickers(key))
    out: List[Dict[str, object]] = []
    for ev in events:
        ev_key = normalize_portfolio_profile_key(str(ev.get("portfolio_profile", "")))
        if ev_key:
            if ev_key == key or (
                key == "turbo_beta"
                and str(ev.get("portfolio_profile", "")).strip().lower().replace("-", "_").replace(" ", "_")
                in _PROFILE_EVENTS_MERGED_INTO_TURBO
            ):
                out.append(ev)
            continue
        if include_legacy_by_ticker and key != "current":
            tk = normalize_ticker(str(ev.get("ticker", "")))
            if tk and tk in profile_tickers:
                out.append(ev)
    return out


def _pnl_for_day_from_events(events: List[Dict[str, object]], now_ny: datetime) -> float:
    day_txt = now_ny.strftime("%Y-%m-%d")
    total = 0.0
    for r in _collect_closed_trade_rows(events, since_ny=None):
        if str(r.get("date_ny", "")) != day_txt:
            continue
        try:
            total += float(r.get("pnl_usd", 0.0) or 0.0)
        except Exception:
            continue
    return total


def build_portfolio_profile_performance_report(
    journal_file: str,
    profile_key: str,
    *,
    budget_usd: float,
    now_ny: Optional[datetime] = None,
) -> str:
    if now_ny is None:
        now_ny = datetime.now(MARKET_TZ)
    else:
        now_ny = now_ny.astimezone(MARKET_TZ)
    key = normalize_portfolio_profile_key(profile_key)
    events = _filter_trade_closed_events_for_profile(load_trade_closed_events(journal_file), key)
    d7 = now_ny - timedelta(days=7)
    d30 = now_ny - timedelta(days=30)
    s_all = _summarize_closed_trades(events, since_ny=None)
    s7 = _summarize_closed_trades(events, since_ny=d7)
    s30 = _summarize_closed_trades(events, since_ny=d30)
    pnl_today = _pnl_for_day_from_events(events, now_ny=now_ny)
    pnl_total = float(s_all.get("total_pnl", 0.0) or 0.0)
    effective_budget = max(0.0, float(budget_usd) + pnl_total)
    tickers_txt = ", ".join(_portfolio_profile_tickers(key)) or "N/A"

    lines = [
        f"RAPPORT PERFORMANCE PAR PORTEFEUILLE [{key}]",
        f"Ref. {now_ny.strftime('%Y-%m-%d %H:%M %Z')} | Budget base: {budget_usd:.0f} USD",
        f"Tickers cibles: {tickers_txt}",
        f"Budget theorique portfolio (base + PnL profile): {effective_budget:.2f} USD",
        "",
        "PnL realise (USD):",
        f"  Aujourd'hui (NY): {pnl_today:+.2f}",
        f"  7 jours: {float(s7.get('total_pnl', 0.0) or 0.0):+.2f} ({int(s7.get('n', 0) or 0)} trade(s))",
        f"  30 jours: {float(s30.get('total_pnl', 0.0) or 0.0):+.2f} ({int(s30.get('n', 0) or 0)} trade(s))",
        f"  Tout l'historique: {pnl_total:+.2f} ({int(s_all.get('n', 0) or 0)} trade(s))",
        "",
    ]
    if int(s_all.get("n", 0) or 0) > 0:
        lines.append(
            f"Win rate (tout): {float(s_all.get('win_rate', 0.0) or 0.0):.0f}% | "
            f"gain moyen: {float(s_all.get('avg_win', 0.0) or 0.0):+.2f} USD ({float(s_all.get('avg_win_pct', 0.0) or 0.0):+.2f}%) | "
            f"perte moyenne: {float(s_all.get('avg_loss', 0.0) or 0.0):+.2f} USD ({float(s_all.get('avg_loss_pct', 0.0) or 0.0):+.2f}%)"
        )
    else:
        lines.append("Aucun trade clos tagge avec ce portefeuille pour le moment.")
        lines.append("Tip: les nouveaux trades sont tagges automatiquement au profil actif de session.")
    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3850] + "\n\n...(rapport tronque, trop long pour Telegram)"
    return out


def build_portfolio_profile_daily_report(
    journal_file: str,
    profile_key: str,
    *,
    budget_usd: float,
    now_ny: Optional[datetime] = None,
) -> str:
    if now_ny is None:
        now_ny = datetime.now(MARKET_TZ)
    else:
        now_ny = now_ny.astimezone(MARKET_TZ)
    key = normalize_portfolio_profile_key(profile_key)
    events = _filter_trade_closed_events_for_profile(load_trade_closed_events(journal_file), key)
    rows_all = _collect_closed_trade_rows(events, since_ny=None)
    day_txt = now_ny.strftime("%Y-%m-%d")
    rows_today = [r for r in rows_all if str(r.get("date_ny", "")) == day_txt]
    rows_today = sorted(rows_today, key=lambda r: str(r.get("ts_utc", "")))
    pnl_day = sum(float(r.get("pnl_usd", 0.0) or 0.0) for r in rows_today)
    pnl_total = sum(float(r.get("pnl_usd", 0.0) or 0.0) for r in rows_all)
    effective_budget = max(0.0, float(budget_usd) + pnl_total)
    try:
        list_max = max(1, min(25, int(os.getenv("PF_DAILY_REPORT_TRADES_MAX", "10"))))
    except ValueError:
        list_max = 10
    lines = [
        f"RAPPORT QUOTIDIEN PAR PORTEFEUILLE [{key}]",
        f"Ref {now_ny.strftime('%Y-%m-%d %H:%M %Z')}",
        f"Tickers cibles: {', '.join(_portfolio_profile_tickers(key)) or 'N/A'}",
        f"Budget base: {budget_usd:.2f} USD | Budget theorique profile: {effective_budget:.2f} USD",
        f"PnL realise jour: {pnl_day:+.2f} USD ({len(rows_today)} trade(s) clos aujourd'hui)",
        f"PnL realise cumule profile: {pnl_total:+.2f} USD ({len(rows_all)} trade(s) clos)",
        "",
        f"Trades clos aujourd'hui (max {list_max}):",
    ]
    if not rows_today:
        lines.append("  Aucun trade clos aujourd'hui.")
    else:
        for r in rows_today[-list_max:]:
            lines.append(
                "  "
                f"{str(r.get('time_ny', '--:--'))} | "
                f"{str(r.get('ticker', '?'))} | "
                f"Entree {float(r.get('entry_price_usd', 0.0) or 0.0):.2f} -> "
                f"Sortie {float(r.get('exit_price_usd', 0.0) or 0.0):.2f} | "
                f"Taille {float(r.get('size_usd', 0.0) or 0.0):.0f} USD | "
                f"PnL {float(r.get('pnl_usd', 0.0) or 0.0):+.2f} USD "
                f"({float(r.get('pnl_pct', 0.0) or 0.0):+.2f}%)"
            )
    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3850] + "\n\n...(rapport tronque, trop long pour Telegram)"
    return out


def build_portfolio_profile_window_report(
    journal_file: str,
    profile_key: str,
    *,
    budget_usd: float,
    window_days: int,
    title: str,
    now_ny: Optional[datetime] = None,
) -> str:
    if now_ny is None:
        now_ny = datetime.now(MARKET_TZ)
    else:
        now_ny = now_ny.astimezone(MARKET_TZ)
    key = normalize_portfolio_profile_key(profile_key)
    window_days = max(1, int(window_days))
    since_ny = now_ny - timedelta(days=window_days)
    events = _filter_trade_closed_events_for_profile(load_trade_closed_events(journal_file), key)
    s_window = _summarize_closed_trades(events, since_ny=since_ny)
    s_all = _summarize_closed_trades(events, since_ny=None)
    pnl_today = _pnl_for_day_from_events(events, now_ny=now_ny)
    pnl_total = float(s_all.get("total_pnl", 0.0) or 0.0)
    effective_budget = max(0.0, float(budget_usd) + pnl_total)
    lines = [
        f"{(title or 'RAPPORT FENETRE PROFILE').strip()} [{key}]",
        f"Periode glissante: {window_days} jour(s) | Ref {now_ny.strftime('%Y-%m-%d %H:%M %Z')}",
        f"Tickers cibles: {', '.join(_portfolio_profile_tickers(key)) or 'N/A'}",
        f"Budget base: {budget_usd:.2f} USD | Budget theorique profile: {effective_budget:.2f} USD",
        f"PnL jour (NY): {pnl_today:+.2f} USD",
        (
            f"PnL realise periode: {float(s_window.get('total_pnl', 0.0) or 0.0):+.2f} USD "
            f"({int(s_window.get('n', 0) or 0)} trade(s) clos)"
        ),
        (
            f"Win rate periode: {float(s_window.get('win_rate', 0.0) or 0.0):.0f}% | "
            f"gain moyen: {float(s_window.get('avg_win', 0.0) or 0.0):+.2f} USD ({float(s_window.get('avg_win_pct', 0.0) or 0.0):+.2f}%) | "
            f"perte moyenne: {float(s_window.get('avg_loss', 0.0) or 0.0):+.2f} USD ({float(s_window.get('avg_loss_pct', 0.0) or 0.0):+.2f}%)"
        ),
        f"PnL realise cumule profile: {pnl_total:+.2f} USD ({int(s_all.get('n', 0) or 0)} trade(s) clos)",
    ]
    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3850] + "\n\n...(rapport tronque, trop long pour Telegram)"
    return out


def build_portfolio_profiles_scoreboard(journal_file: str, *, now_ny: Optional[datetime] = None) -> str:
    if now_ny is None:
        now_ny = datetime.now(MARKET_TZ)
    else:
        now_ny = now_ny.astimezone(MARKET_TZ)
    events = load_trade_closed_events(journal_file)
    profiles = ["current"] + list(PORTFOLIO_PROFILES.keys())
    snapshots: List[Dict[str, object]] = []

    for key in profiles:
        ev = _filter_trade_closed_events_for_profile(events, key)
        s = _summarize_closed_trades(ev, since_ny=None)
        rows = _collect_closed_trade_rows(ev, since_ny=None)
        pnls = [float(r.get("pnl_usd", 0.0) or 0.0) for r in rows]
        wins_sum = sum(p for p in pnls if p > 0)
        losses_sum_abs = abs(sum(p for p in pnls if p < 0))
        if losses_sum_abs > 0:
            profit_factor = wins_sum / losses_sum_abs
        elif wins_sum > 0:
            profit_factor = 5.0
        else:
            profit_factor = 0.0

        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cum += p
            if cum > peak:
                peak = cum
            dd = cum - peak
            if dd < max_dd:
                max_dd = dd
        dd_abs = abs(max_dd)

        day_totals: Dict[str, float] = {}
        for r in rows:
            d = str(r.get("date_ny", "")).strip()
            if not d:
                continue
            day_totals[d] = day_totals.get(d, 0.0) + float(r.get("pnl_usd", 0.0) or 0.0)
        positive_day_ratio = (
            (sum(1 for v in day_totals.values() if v > 0) / len(day_totals))
            if day_totals
            else 0.0
        )

        snapshots.append(
            {
                "key": key,
                "pnl_total": float(s.get("total_pnl", 0.0) or 0.0),
                "n": float(int(s.get("n", 0) or 0)),
                "win_rate": float(s.get("win_rate", 0.0) or 0.0),
                "profit_factor": float(profit_factor),
                "max_drawdown_abs": float(dd_abs),
                "positive_day_ratio": float(positive_day_ratio),
            }
        )

    def _norm(v: float, lo: float, hi: float) -> float:
        if hi - lo <= 1e-9:
            return 0.5
        out = (v - lo) / (hi - lo)
        return max(0.0, min(1.0, out))

    pnl_vals = [float(x["pnl_total"]) for x in snapshots]
    wr_vals = [float(x["win_rate"]) for x in snapshots]
    pf_vals = [min(3.0, float(x["profit_factor"])) for x in snapshots]
    dd_vals = [float(x["max_drawdown_abs"]) for x in snapshots]
    reg_vals = [float(x["positive_day_ratio"]) for x in snapshots]
    n_vals = [float(x["n"]) for x in snapshots]

    pnl_lo, pnl_hi = min(pnl_vals or [0.0]), max(pnl_vals or [0.0])
    wr_lo, wr_hi = min(wr_vals or [0.0]), max(wr_vals or [0.0])
    pf_lo, pf_hi = min(pf_vals or [0.0]), max(pf_vals or [0.0])
    dd_lo, dd_hi = min(dd_vals or [0.0]), max(dd_vals or [0.0])
    reg_lo, reg_hi = min(reg_vals or [0.0]), max(reg_vals or [0.0])
    n_lo, n_hi = min(n_vals or [0.0]), max(n_vals or [0.0])

    ranked: List[Dict[str, object]] = []
    for snap in snapshots:
        pnl_norm = _norm(float(snap["pnl_total"]), pnl_lo, pnl_hi)
        wr_norm = _norm(float(snap["win_rate"]), wr_lo, wr_hi)
        pf_norm = _norm(min(3.0, float(snap["profit_factor"])), pf_lo, pf_hi)
        dd_good = 1.0 - _norm(float(snap["max_drawdown_abs"]), dd_lo, dd_hi)
        reg_norm = _norm(float(snap["positive_day_ratio"]), reg_lo, reg_hi)
        sample_norm = _norm(float(snap["n"]), n_lo, n_hi)

        perf_score = 100.0 * (0.60 * pnl_norm + 0.25 * wr_norm + 0.15 * pf_norm)
        risk_score = 100.0 * dd_good
        regularity_score = 100.0 * (0.75 * reg_norm + 0.25 * sample_norm)
        total_score = 0.50 * perf_score + 0.30 * risk_score + 0.20 * regularity_score

        if total_score >= 70:
            verdict = "solide"
        elif total_score >= 50:
            verdict = "equilibre"
        else:
            verdict = "agressif/instable"

        ranked.append(
            {
                "key": str(snap["key"]),
                "pnl_total": float(snap["pnl_total"]),
                "n": int(float(snap["n"])),
                "win_rate": float(snap["win_rate"]),
                "profit_factor": float(snap["profit_factor"]),
                "max_drawdown_abs": float(snap["max_drawdown_abs"]),
                "regularity": float(snap["positive_day_ratio"]) * 100.0,
                "perf_score": perf_score,
                "risk_score": risk_score,
                "regularity_score": regularity_score,
                "total_score": total_score,
                "verdict": verdict,
            }
        )
    ranked.sort(key=lambda x: float(x["total_score"]), reverse=True)

    lines = [
        "SCOREBOARD PORTEFEUILLES (QUALITATIF)",
        f"Ref. {now_ny.strftime('%Y-%m-%d %H:%M %Z')}",
        "Commandes detail: /report_pf <current|turbo_beta|legacy_open>",
        "",
    ]
    for idx, item in enumerate(ranked, start=1):
        lines.append(
            f"#{idx} {item['key']} ({item['verdict']}) | score {float(item['total_score']):.1f}/100 "
            f"[perf {float(item['perf_score']):.0f} | risque {float(item['risk_score']):.0f} | reg {float(item['regularity_score']):.0f}]"
        )
        lines.append(
            f"  PnL {float(item['pnl_total']):+.2f} USD | trades {int(item['n'])} | WR {float(item['win_rate']):.0f}% | "
            f"PF {float(item['profit_factor']):.2f} | DD max -{float(item['max_drawdown_abs']):.2f} USD | jours verts {float(item['regularity']):.0f}%"
        )
    lines.append("")
    lines.append("Note: score qualitatif base sur performance, drawdown et regularite.")
    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3850] + "\n\n...(rapport tronque, trop long pour Telegram)"
    return out


def _apply_session_ticker_override(tickers: List[str]) -> None:
    cleaned: List[str] = []
    seen = set()
    for tk in tickers:
        n = normalize_ticker(tk)
        if not n or n in seen:
            continue
        cleaned.append(n)
        seen.add(n)
    if not cleaned:
        return
    os.environ["TICKERS_OVERRIDE"] = ",".join(cleaned)
    os.environ["TICKERS_EXTRA"] = ""


def choose_portfolio_profile_on_start(current_tickers: List[str]) -> Tuple[str, List[str]]:
    menu: List[Tuple[str, str, List[str]]] = [
        ("current", "Config .env actuelle", current_tickers),
        ("turbo_beta", "Turbo beta (max mouvement)", PORTFOLIO_PROFILES["turbo_beta"]),
    ]

    print("\nSelection portefeuille du jour (6 tickers max):")
    for idx, (_, label, tickers) in enumerate(menu):
        print(f"  {idx}) {label}: {', '.join(tickers)}")

    for _ in range(3):
        try:
            raw = input("Choix [0-1] (defaut 0): ").strip()
        except EOFError:
            return menu[0][0], menu[0][2]
        if not raw:
            return menu[0][0], menu[0][2]
        if raw.isdigit():
            idx = int(raw)
            if 0 <= idx < len(menu):
                key, _, tickers = menu[idx]
                return key, tickers
        print("Choix invalide. Reessaie avec 0 ou 1.")
    print("Aucun choix valide detecte, conservation de la config actuelle.")
    return menu[0][0], menu[0][2]


def count_portfolio_slots_used(positions: Dict[str, dict]) -> int:
    """Nombre de 'slots' occupes : position ouverte ou signal ACHETER en attente de confirmation."""
    n = 0
    for st in positions.values():
        if st.get("in_position") or st.get("pending_buy"):
            n += 1
    return n


def list_open_tickers(positions: Dict[str, dict]) -> List[str]:
    return sorted(t for t, st in positions.items() if st.get("in_position"))


def entry_context_fields(
    *,
    ticker: str,
    analysis: Dict[str, str],
    fund_data: Dict[str, object],
) -> Dict[str, object]:
    """
    Champs de contexte enregistres a chaque signal, pour pouvoir attribuer le
    resultat a posteriori. Sans eux, impossible de savoir si un edge vient de la
    volatilite, du bloc de risque, de l'heure ou d'autre chose.
    Ne leve jamais: un champ manquant vaut None plutot que de casser le journal.
    """
    out: Dict[str, object] = {
        "atr_pct": None,
        "rsi": None,
        "theme_bloc": None,
        "entry_hour_ny": None,
        "earnings_days": None,
        "breakout": None,
    }
    try:
        px = float(analysis.get("current_price", 0.0) or 0.0)
        atr = float(analysis.get("atr", 0.0) or 0.0)
        if px > 0 and atr > 0:
            out["atr_pct"] = round(100.0 * atr / px, 3)
    except (TypeError, ValueError):
        pass
    try:
        out["rsi"] = round(float(analysis.get("rsi", 0.0) or 0.0), 1)
    except (TypeError, ValueError):
        pass
    try:
        out["theme_bloc"] = risk_theme_of(ticker)
    except Exception:
        pass
    try:
        out["entry_hour_ny"] = datetime.now(MARKET_TZ).strftime("%H:%M")
    except Exception:
        pass
    try:
        ed = fund_data.get("earnings_days")
        out["earnings_days"] = int(ed) if isinstance(ed, int) else None
    except Exception:
        pass
    try:
        out["breakout"] = bool(analysis_breakout_active(analysis))
    except Exception:
        pass
    return out


def risk_theme_map() -> Dict[str, str]:
    """
    Carte ticker -> bloc de risque, format 'TK:BLOC,TK:BLOC'.

    Les blocs viennent de la correlation mesuree des rendements, pas des secteurs :
    HOOD/COIN/SOFI/IONQ correlent 0.55-0.77 alors qu'ils relevent de 3 secteurs.
    Un ticker absent de la carte forme son propre bloc.
    """
    out: Dict[str, str] = {}
    for part in (os.getenv("RISK_THEME_MAP", "") or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        raw_tk, raw_th = part.split(":", 1)
        tk = normalize_ticker(raw_tk)
        th = raw_th.strip().upper()
        if tk and th:
            out[tk] = th
    return out


def risk_theme_of(ticker: str) -> str:
    tk = normalize_ticker(ticker)
    return risk_theme_map().get(tk, f"_{tk}")


def risk_max_per_theme() -> int:
    """Lignes ouvertes max par bloc de risque. 0 = desactive."""
    try:
        return max(0, int(os.getenv("RISK_MAX_PER_THEME", "0")))
    except ValueError:
        return 0


def theme_exposure_blocks(
    positions: Dict[str, Dict[str, object]],
    ticker: str,
) -> Tuple[bool, str]:
    """
    True si ouvrir `ticker` depasserait le plafond de lignes du meme bloc de risque.

    Garde-fou anti-concentration : le 31/07 le bot a pris SMCI, RGTI et IONQ en
    2 minutes, soit 990$ sur 1096$ dans un seul et meme trade.
    """
    cap = risk_max_per_theme()
    if cap <= 0:
        return False, ""
    tk = normalize_ticker(ticker)
    if not tk:
        return False, ""
    theme = risk_theme_of(tk)
    same: List[str] = []
    for other, raw in positions.items():
        o = normalize_ticker(other)
        if not o or o == tk:
            continue
        st = normalize_position_state(raw)
        if not (bool(st.get("in_position")) or bool(st.get("pending_buy"))):
            continue
        if risk_theme_of(o) == theme:
            same.append(o)
    if len(same) < cap:
        return False, ""
    return True, (
        f"cap concentration bloc {theme}: {len(same)}/{cap} ligne(s) deja ouverte(s) "
        f"({', '.join(sorted(same))})"
    )


def build_scan_tickers(
    base_tickers: List[str],
    positions: Dict[str, Dict[str, object]],
    *,
    include_open_positions: bool,
) -> List[str]:
    """
    Construit l'univers de scan:
    - base_tickers (watchlist .env)
    - + positions deja ouvertes (ou vente en attente) si include_open_positions=1
    """
    ordered: List[str] = []
    seen = set()
    for tk in base_tickers:
        n = normalize_ticker(tk)
        if not n or n in seen:
            continue
        ordered.append(n)
        seen.add(n)

    if include_open_positions:
        for tk in sorted(positions.keys()):
            n = normalize_ticker(tk)
            if not n or n in seen:
                continue
            st = normalize_position_state(positions.get(tk, {}))
            if bool(st.get("in_position")) or bool(st.get("pending_sell")):
                ordered.append(n)
                seen.add(n)
    return ordered


def scan_prioritize_momentum_enabled() -> bool:
    return os.getenv("SCAN_PRIORITIZE_MOMENTUM", "1").strip().lower() in {"1", "true", "yes", "y"}


def breakout_early_relax_macd_enabled() -> bool:
    return os.getenv("BREAKOUT_EARLY_RELAX_MACD", "1").strip().lower() in {"1", "true", "yes", "y"}


def breakout_lookback_bars() -> int:
    try:
        return max(4, int(os.getenv("BREAKOUT_LOOKBACK_BARS", "12")))
    except ValueError:
        return 12


def breakout_buffer_pct() -> float:
    try:
        return max(0.0, float(os.getenv("BREAKOUT_BUFFER_PCT", "0.15")))
    except ValueError:
        return 0.15


def analysis_breakout_active(analysis: Dict[str, str]) -> bool:
    return str(analysis.get("breakout_active", "0")).strip().lower() in {"1", "true", "yes", "y"}


def breakout_fund_exception_enabled() -> bool:
    return os.getenv("BUY_BREAKOUT_FUND_EXCEPTION_ENABLED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def breakout_fund_exception_min_score() -> int:
    try:
        return max(0, min(100, int(os.getenv("BUY_BREAKOUT_FUND_MIN_SCORE", "28"))))
    except ValueError:
        return 28


def breakout_fund_exception_min_mtf() -> int:
    try:
        return int(os.getenv("BUY_BREAKOUT_FUND_MIN_MTF", "0"))
    except ValueError:
        return 0


def breakout_fund_exception_tickers() -> Optional[set[str]]:
    """
    Liste blanche optionnelle (ex: RIVN,SOFI).
    Vide => tous les tickers peuvent beneficier de l'exception si breakout valide.
    """
    raw = (os.getenv("BUY_BREAKOUT_FUND_EXCEPTION_TICKERS", "RIVN") or "").strip()
    if not raw:
        return None
    out: set[str] = set()
    for part in raw.split(","):
        tk = normalize_ticker(part.strip())
        if tk:
            out.add(tk)
    return out if out else None


def breakout_fundamental_exception_ok(
    *,
    ticker: str,
    analysis: Dict[str, str],
    higher_tf: Dict[str, object],
    fund_score: int,
    fundamental_min_score: int,
) -> Tuple[bool, str]:
    """
    Autorise un ACHETER malgre fonda sous le seuil si breakout 15m haussier confirme.
  """
    if not breakout_fund_exception_enabled():
        return False, ""
    tk = normalize_ticker(ticker)
    allowlist = breakout_fund_exception_tickers()
    if allowlist is not None and tk not in allowlist:
        return False, ""
    min_exc = breakout_fund_exception_min_score()
    if int(fund_score) >= int(fundamental_min_score):
        return False, ""
    if int(fund_score) < min_exc:
        return False, ""
    if not analysis_breakout_active(analysis):
        return False, ""
    ht_score = int(higher_tf.get("trend_score", 0) or 0)
    if ht_score < breakout_fund_exception_min_mtf():
        return False, ""
    macd_val = float(analysis.get("macd_value", 0.0) or 0.0)
    macd_sig = float(analysis.get("macd_signal", 0.0) or 0.0)
    if macd_val < macd_sig:
        return False, ""
    return (
        True,
        f"breakout 15m + MTF {ht_score} (fonda {fund_score} >= plancher exception {min_exc})",
    )


def breakout_require_volume_ratio() -> float:
    try:
        return max(0.0, float(os.getenv("BREAKOUT_REQUIRE_VOLUME_RATIO", "1.15")))
    except ValueError:
        return 1.15


def breakout_min_rsi() -> float:
    try:
        return max(0.0, float(os.getenv("BREAKOUT_MIN_RSI", "40")))
    except ValueError:
        return 40.0


def breakout_require_green_candle() -> bool:
    return os.getenv("BREAKOUT_REQUIRE_GREEN_CANDLE", "1").strip().lower() in {"1", "true", "yes", "y"}


def effective_scan_interval_min() -> int:
    """
    Intervalle entre cycles complet.
    - Nombre explicite dans SCAN_INTERVAL_MIN (ex: 10, 15)
    - auto / optimal : ~10 min avec scan parallele + FLASH 45s entre les cycles
      (2 passes LLM par bougie 15m sans surcharger Groq/Yahoo)
    """
    raw = (os.getenv("SCAN_INTERVAL_MIN", "10") or "10").strip().lower()
    if raw not in {"auto", "optimal", "smart"}:
        try:
            return max(3, min(60, int(raw)))
        except ValueError:
            return 10
    n_tickers = len(get_tickers_from_env())
    if scan_parallel_enabled():
        if n_tickers <= 10:
            return 10
        if n_tickers <= 16:
            return 12
        return 15
    return 15


def compute_intraday_breakout_metrics(df: "pd.DataFrame") -> tuple[bool, float]:
    """Casse du plus haut des N bougies precedentes (15m par defaut)."""
    lookback = breakout_lookback_bars()
    buffer_pct = breakout_buffer_pct()
    vol_ratio_min = breakout_require_volume_ratio()
    if df is None or len(df) < lookback + 2:
        return False, 0.0
    if "High" not in df.columns or "Close" not in df.columns:
        return False, 0.0
    try:
        range_high = float(df["High"].iloc[-(lookback + 1) : -1].max())
        close_now = float(df["Close"].iloc[-1])
    except Exception:
        return False, 0.0
    if range_high <= 0 or close_now <= 0:
        return False, range_high
    threshold = range_high * (1.0 + buffer_pct / 100.0)
    if close_now <= threshold:
        return False, range_high
    if vol_ratio_min > 0 and "Volume" in df.columns:
        try:
            vol_now = float(df["Volume"].iloc[-1])
            vol_avg = float(df["Volume"].iloc[-(lookback + 1) : -1].mean())
            if vol_avg > 0 and vol_now < vol_avg * vol_ratio_min:
                return False, range_high
        except Exception:
            pass
    if breakout_require_green_candle() and "Open" in df.columns:
        try:
            if float(df["Close"].iloc[-1]) <= float(df["Open"].iloc[-1]):
                return False, range_high
        except Exception:
            pass
    min_rsi = breakout_min_rsi()
    if min_rsi > 0 and "RSI_14" in df.columns:
        try:
            if float(df["RSI_14"].iloc[-1]) < min_rsi:
                return False, range_high
        except Exception:
            pass
    return True, range_high


def buy_early_entry_enabled() -> bool:
    return os.getenv("BUY_EARLY_ENTRY_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}


def rules_min_conf_buy() -> int:
    try:
        return max(0, min(100, int(os.getenv("RULES_MIN_CONF_BUY", "68"))))
    except ValueError:
        return 68


def rules_early_entry_min_conf() -> int:
    try:
        return max(0, min(100, int(os.getenv("RULES_EARLY_ENTRY_MIN_CONF", "65"))))
    except ValueError:
        return 65


def buy_anticipate_breakout_enabled() -> bool:
    return os.getenv("BUY_ANTICIPATE_BREAKOUT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}


def buy_anticipate_breakout_near_pct() -> float:
    try:
        return max(0.05, float(os.getenv("BUY_ANTICIPATE_BREAKOUT_NEAR_PCT", "0.35")))
    except ValueError:
        return 0.35


def buy_max_chase_from_breakout_pct() -> float:
    try:
        return max(0.0, float(os.getenv("BUY_MAX_CHASE_FROM_BREAKOUT_PCT", "1.0")))
    except ValueError:
        return 1.0


def reentry_cooldown_hours() -> float:
    try:
        return max(0.0, float(os.getenv("TRADING_REENTRY_COOLDOWN_HOURS", "4")))
    except ValueError:
        return 4.0


def reentry_block_chase_pct() -> float:
    """Apres sortie gagnante: bloque rachat si prix signal > sortie + X%. 0 = desactive."""
    try:
        return max(0.0, min(10.0, float(os.getenv("TRADING_REENTRY_BLOCK_CHASE_PCT", "1.0"))))
    except ValueError:
        return 1.0


def reentry_block_chase_max_hours() -> float:
    """
    Duree max apres une sortie gagnante pendant laquelle l'anti-chase s'applique.
    Evite de bloquer un ticker des semaines plus tard (ex. HOOD sorti en juin).
    0 = pas de limite temporelle (ancien comportement).
    """
    try:
        return max(0.0, float(os.getenv("TRADING_REENTRY_BLOCK_CHASE_MAX_HOURS", "4")))
    except ValueError:
        return 4.0


def macd_histogram_rising(analysis: Dict[str, str]) -> bool:
    macd_now = float(analysis.get("macd_value", 0.0) or 0.0)
    macd_sig = float(analysis.get("macd_signal", 0.0) or 0.0)
    macd_prev = float(analysis.get("macd_prev", macd_now) or macd_now)
    macd_sig_prev = float(analysis.get("macd_sig_prev", macd_sig) or macd_sig)
    hist_now = macd_now - macd_sig
    hist_prev = macd_prev - macd_sig_prev
    return hist_now > hist_prev


def early_entry_signal_ok(
    analysis: Dict[str, str],
    higher_tf: Dict[str, object],
    *,
    rsi: float,
    macd_val: float,
    macd_sig: float,
) -> Tuple[bool, str]:
    """
    Autorise une entree avant le croisement MACD complet si la structure se retourne
    (breakout 15m ou test du plus haut de range + MTF haussier).
    """
    if not buy_early_entry_enabled() or not breakout_early_relax_macd_enabled():
        return False, ""
    ht_score = int(higher_tf.get("trend_score", 0) or 0)
    if ht_score < 1:
        return False, ""
    try:
        buy_min_rsi = float(os.getenv("BUY_MIN_RSI", "35"))
    except ValueError:
        buy_min_rsi = 35.0
    if rsi < buy_min_rsi:
        return False, ""
    if not macd_histogram_rising(analysis):
        return False, ""
    if macd_val >= macd_sig:
        return False, ""

    if analysis_breakout_active(analysis):
        return True, "breakout 15m + MACD en acceleration (entree anticipee)"

    if buy_anticipate_breakout_enabled():
        range_high = float(analysis.get("breakout_range_high", 0.0) or 0.0)
        current_price = float(analysis.get("current_price", 0.0) or 0.0)
        if range_high > 0 and current_price > 0:
            dist_pct = ((range_high - current_price) / range_high) * 100.0
            near_pct = buy_anticipate_breakout_near_pct()
            if 0.0 <= dist_pct <= near_pct and ht_score >= 2:
                return True, (
                    f"anticipation casse range ({dist_pct:.2f}% sous high 15m {range_high:.2f}, "
                    "MACD en acceleration)"
                )
    return False, ""


def buy_chase_too_extended(analysis: Dict[str, str]) -> Tuple[bool, str]:
    """Bloque les entrees trop loin au-dessus du plus haut de range 15m (anti chase)."""
    max_chase = buy_max_chase_from_breakout_pct()
    if max_chase <= 0:
        return False, ""
    range_high = float(analysis.get("breakout_range_high", 0.0) or 0.0)
    current_price = float(analysis.get("current_price", 0.0) or 0.0)
    if range_high <= 0 or current_price <= 0:
        return False, ""
    ext_pct = ((current_price / range_high) - 1.0) * 100.0
    if ext_pct <= max_chase:
        return False, ""
    return True, (
        f"entree trop tardive: prix +{ext_pct:.2f}% au-dessus du range 15m "
        f"({range_high:.2f}), max chase {max_chase:.2f}%"
    )


def reentry_blocked_by_cooldown(
    ticker: str,
    journal_file: str,
    *,
    signal_price: Optional[float] = None,
) -> Tuple[bool, str]:
    if not journal_file:
        return False, ""
    tk = normalize_ticker(ticker)
    if not tk:
        return False, ""

    last_ev: Optional[Dict[str, object]] = None
    for ev in reversed(load_trade_closed_events(journal_file)):
        if normalize_ticker(str(ev.get("ticker", ""))) == tk:
            last_ev = ev
            break
    if last_ev is None:
        return False, ""

    ts_raw = str(last_ev.get("ts_utc", "") or "")
    closed_at: Optional[datetime] = None
    if ts_raw:
        try:
            closed_at = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            closed_at = None
        if closed_at is not None and closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=UTC_TZ)

    chase_pct = reentry_block_chase_pct()
    chase_max_h = reentry_block_chase_max_hours()
    if chase_pct > 0 and signal_price is not None:
        chase_fresh = True
        if chase_max_h > 0:
            if closed_at is None:
                chase_fresh = False
            else:
                age_h = (datetime.now(UTC_TZ) - closed_at).total_seconds() / 3600.0
                chase_fresh = age_h <= chase_max_h
        if chase_fresh:
            try:
                sig = float(signal_price)
                prev_pnl = float(last_ev.get("pnl_usd", 0.0) or 0.0)
                exit_px = float(last_ev.get("exit_price_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                sig = 0.0
                prev_pnl = 0.0
                exit_px = 0.0
            if sig > 0 and prev_pnl > 0 and exit_px > 0:
                max_px = exit_px * (1.0 + chase_pct / 100.0)
                if sig > max_px + 1e-9:
                    chase_delta_pct = ((sig - exit_px) / exit_px) * 100.0
                    return True, (
                        f"anti-chase re-entry {tk}: signal {sig:.2f} > sortie gagnante "
                        f"{exit_px:.2f} + {chase_pct:.1f}% (+{chase_delta_pct:.2f}% vs sortie)"
                    )

    hours = reentry_cooldown_hours()
    if hours <= 0:
        return False, ""
    if closed_at is None:
        return False, ""
    cutoff = datetime.now(UTC_TZ) - timedelta(hours=hours)
    if closed_at >= cutoff:
        left_h = (closed_at + timedelta(hours=hours) - datetime.now(UTC_TZ)).total_seconds() / 3600.0
        return True, f"cooldown re-entry {tk} ({max(0.0, left_h):.1f}h restantes apres sortie recente)"
    return False, ""


def scan_momentum_source() -> str:
    return (os.getenv("SCAN_MOMENTUM_SOURCE", "ibkr") or "ibkr").strip().lower()


def scan_parallel_enabled() -> bool:
    return os.getenv("SCAN_PARALLEL_ENABLED", "0").strip().lower() in {"1", "true", "yes", "y"}


def scan_parallel_workers() -> int:
    try:
        return max(1, min(12, int(os.getenv("SCAN_PARALLEL_WORKERS", "4"))))
    except ValueError:
        return 4


def scan_decision_mode() -> str:
    """
    Mode de decision scan:
    - llm: LLM pour tous les tickers
    - hybrid: regles pour nouvelles entrees, LLM pour tickers deja en position
    - rules: regles pour tous les tickers
    """
    raw = (os.getenv("SCAN_DECISION_MODE", "hybrid") or "hybrid").strip().lower()
    if raw in {"llm", "rules", "hybrid"}:
        return raw
    return "hybrid"


def _yahoo_quote_snapshot_light(ticker: str) -> Dict[str, float]:
    """
    Snapshot quote Yahoo leger (fast_info / info) — AUCUNE bougie OHLCV.
    Sert uniquement au ranking pre-scan; les bougies restent telechargees
    pendant le scan du ticker.
    """
    out = {"price": 0.0, "high": 0.0, "open": 0.0, "daily_pct": 0.0}
    try:
        tkr = yf.Ticker(ticker)
    except Exception:
        return out

    def _pick(mapping: object, keys: Tuple[str, ...]) -> float:
        if not isinstance(mapping, dict):
            return 0.0
        for key in keys:
            try:
                v = mapping.get(key)
                if v is None:
                    continue
                f = float(v)
                if f == f and f > 0:  # na-safe
                    return f
            except (TypeError, ValueError):
                continue
        return 0.0

    fi = {}
    info = {}
    try:
        fi = getattr(tkr, "fast_info", None) or {}
        if hasattr(fi, "items"):
            fi = dict(fi)
        elif not isinstance(fi, dict):
            fi = {}
    except Exception:
        fi = {}
    try:
        info = getattr(tkr, "info", None) or {}
        if not isinstance(info, dict):
            info = {}
    except Exception:
        info = {}

    out["price"] = _pick(
        fi, ("lastPrice", "last_price", "regularMarketPrice")
    ) or _pick(info, ("regularMarketPrice", "currentPrice", "lastPrice"))
    out["high"] = _pick(
        fi, ("dayHigh", "regular_market_day_high")
    ) or _pick(info, ("dayHigh", "regularMarketDayHigh"))
    out["open"] = _pick(fi, ("open", "regularMarketOpen")) or _pick(
        info, ("open", "regularMarketOpen")
    )
    try:
        chg = None
        if isinstance(fi, dict):
            chg = fi.get("regularMarketChangePercent")
        if chg is None and isinstance(info, dict):
            chg = info.get("regularMarketChangePercent")
        if chg is not None:
            out["daily_pct"] = float(chg)
    except (TypeError, ValueError):
        pass
    if out["daily_pct"] == 0.0 and out["price"] > 0 and out["open"] > 0:
        out["daily_pct"] = ((out["price"] / out["open"]) - 1.0) * 100.0
    if out["high"] <= 0 and out["price"] > 0:
        out["high"] = out["price"]
    return out


def quick_momentum_score_for_ticker(ticker: str) -> float:
    """
    Score rapide pour ordonner le scan: hausse jour + proximite du plus haut session.
    Quote-only (Finnhub ou Yahoo light) — ne telecharge PAS de bougies.
    Les OHLCV sont charges uniquement lors du scan du ticker.
    """
    source = scan_momentum_source()
    if source == "finnhub":
        api_key = os.getenv("FINNHUB_API_KEY", "").strip()
        if not api_key:
            return 0.0
        try:
            q = _fetch_finnhub_quote(ticker)
            daily_pct = float(q.get("daily_pct", 0.0) or 0.0)
            px = float(q.get("price", 0.0) or 0.0)
            high = float(q.get("high", px) or px)
            if px <= 0 or high <= 0:
                return max(0.0, daily_pct)
            dist_to_high_pct = ((high - px) / high) * 100.0
            near_high_bonus = max(0.0, 3.0 - dist_to_high_pct) * 2.0
            return daily_pct + near_high_bonus
        except Exception:
            return 0.0

    # ibkr / yahoo / auto: snapshot leger uniquement (pas de fetch_price_data).
    try:
        snap = _yahoo_quote_snapshot_light(ticker)
        px = float(snap.get("price", 0.0) or 0.0)
        high = float(snap.get("high", 0.0) or 0.0)
        daily_pct = float(snap.get("daily_pct", 0.0) or 0.0)
        if px <= 0:
            return 0.0
        if high <= 0:
            return max(0.0, daily_pct)
        dist_to_high_pct = ((high - px) / high) * 100.0
        near_high_bonus = max(0.0, 3.0 - dist_to_high_pct) * 2.0
        return daily_pct + near_high_bonus
    except Exception:
        return 0.0


def rank_buy_candidate_score(
    *,
    conf_val: int,
    analysis: Dict[str, str],
    parsed: Dict[str, str],
    fund_score_val: int,
    higher_tf: Dict[str, object],
    momentum_score: float,
) -> float:
    """Plus le score est eleve, plus le candidat ACHETER est prioritaire."""
    score = float(conf_val) * 10.0
    if analysis_breakout_active(analysis):
        score += 12.0
    score += momentum_score
    score += float(fund_score_val) * 0.15
    score += float(higher_tf.get("trend_score", 0) or 0) * 2.0
    current_price = float(analysis.get("current_price", 0) or 0)
    stop_price = parse_price_value(parsed.get("stop_loss", ""))
    target_price = parse_price_value(parsed.get("objectif_1", ""))
    if (
        current_price > 0
        and stop_price is not None
        and target_price is not None
        and stop_price < current_price < target_price
    ):
        risk = current_price - stop_price
        reward = target_price - current_price
        if risk > 0:
            score += min(15.0, (reward / risk) * 4.0)
    return score


def build_rules_scan_decision(
    *,
    ticker: str,
    analysis: Dict[str, str],
    state: Dict[str, object],
    higher_tf: Dict[str, object],
    fund_data: Dict[str, object],
) -> Tuple[Dict[str, str], str]:
    """
    Decision deterministe pour reduire la dependance LLM.
    Retourne (parsed, reason_tag).
    """
    st = normalize_position_state(state)
    in_pos = bool(st.get("in_position", False))
    pending_sell = bool(st.get("pending_sell", False))
    current_price = float(analysis.get("current_price", 0.0) or 0.0)
    atr = float(analysis.get("atr", 0.0) or 0.0)
    rsi = float(analysis.get("rsi", 50.0) or 50.0)
    macd_val = float(analysis.get("macd_value", 0.0) or 0.0)
    macd_sig = float(analysis.get("macd_signal", 0.0) or 0.0)
    bb_pos = str(analysis.get("bb_pos", "")).strip().lower()
    breakout_on = analysis_breakout_active(analysis)
    ht_score = int(higher_tf.get("trend_score", 0) or 0)
    fund_score = int(fund_data.get("fund_score", 50) or 50)
    risk_off = bool(fund_data.get("risk_off", False))

    parsed: Dict[str, str] = {
        "ticker": normalize_ticker(ticker),
        "decision": "ATTENDRE",
        "justification": "",
        "confiance": "60",
        "stop_loss": "N/A",
        "objectif_1": "N/A",
        "taille_suggeree_usd": "0",
    }

    if in_pos or pending_sell:
        parsed["justification"] = (
            f"Mode regles: ligne {ticker} deja ouverte. "
            "Gestion prioritaire via TP/SL IBKR et garde-fous structurels."
        )
        return parsed, "rules-hold-open"

    # Score d'entree deterministe (0-100)
    score = 50.0
    if macd_val >= macd_sig:
        score += 12.0
    else:
        score -= 10.0
    score += float(ht_score) * 5.0
    score += max(-10.0, min(12.0, (fund_score - 50) * 0.35))
    if breakout_on:
        score += 9.0
    if 45.0 <= rsi <= 72.0:
        score += 5.0
    if rsi >= 80.0 and not (breakout_on and ht_score >= 1):
        score -= 9.0
    if bb_pos in {"upper_band"}:
        score += 2.0
    if risk_off:
        score -= 6.0
    conf = max(0, min(100, int(round(score))))
    parsed["confiance"] = str(conf)

    try:
        fund_min_default = int(os.getenv("FUNDAMENTAL_MIN_SCORE", "40"))
    except ValueError:
        fund_min_default = 40
    fund_exc_ok, fund_exc_note = breakout_fundamental_exception_ok(
        ticker=ticker,
        analysis=analysis,
        higher_tf=higher_tf,
        fund_score=fund_score,
        fundamental_min_score=fund_min_default,
    )
    if fund_exc_ok:
        score += 6.0
        conf = max(0, min(100, int(round(score))))
        parsed["confiance"] = str(conf)

    early_ok, early_note = early_entry_signal_ok(
        analysis,
        higher_tf,
        rsi=rsi,
        macd_val=macd_val,
        macd_sig=macd_sig,
    )
    sniper_ok = False
    sniper_note = ""
    if sniper_mode_enabled():
        early_ok = False
        early_note = ""
        conf_min = sniper_min_confidence()
        macd_ok = macd_val >= macd_sig
        sniper_ok, sniper_note = sniper_entry_ok(
            conf=conf,
            ht_score=ht_score,
            macd_ok=macd_ok,
            breakout_on=breakout_on,
            risk_off=risk_off,
        )
        buy_ok = sniper_ok
    else:
        conf_min = rules_early_entry_min_conf() if early_ok else rules_min_conf_buy()
        macd_ok = macd_val >= macd_sig or early_ok
        buy_ok = conf >= conf_min and macd_ok and ht_score >= 0 and not (risk_off and conf < 78)
    if buy_ok and fund_score < fund_min_default and not fund_exc_ok:
        buy_ok = False
    if not buy_ok:
        reason_extra = ""
        if sniper_mode_enabled():
            reason_extra = f", {sniper_note}" if sniper_note else ""
        if fund_score < fund_min_default and breakout_on and not fund_exc_ok:
            reason_extra += f", fonda {fund_score} < {fund_min_default} sans exception breakout"
        if early_ok and conf < conf_min:
            reason_extra += f", conf {conf} < seuil anticipe {conf_min}"
        mode_lbl = "sniper" if sniper_mode_enabled() else "regles"
        parsed["justification"] = (
            f"Mode {mode_lbl}: setup incomplet ({ticker}) "
            f"(conf {conf}, MTF {ht_score}, MACD {'ok' if macd_val >= macd_sig else 'faible'}"
            f"{reason_extra})."
        )
        return parsed, "rules-wait"

    parsed["decision"] = "ACHETER"
    if current_price > 0:
        if fixed_sl_tp_enabled():
            sl, tp = compute_fixed_sl_tp(current_price, ticker)
        else:
            min_dist, _, _ = stop_distance_floor_usd(current_price, atr, ticker)
            risk_dist = max(0.01, float(min_dist))
            sl = round(max(0.01, current_price - risk_dist), 2)
            rr_target = 1.45 if (breakout_on or early_ok) else 1.30
            tp = round(current_price + (risk_dist * rr_target), 2)
        parsed["stop_loss"] = f"{sl:.2f}"
        parsed["objectif_1"] = f"{tp:.2f}"
    exc_txt = f" ({fund_exc_note})" if fund_exc_ok else ""
    early_txt = f" — {early_note}" if early_ok and early_note else ""
    sniper_txt = f" — {sniper_note}" if sniper_mode_enabled() and sniper_ok else ""
    parsed["justification"] = (
        f"Mode {'sniper' if sniper_mode_enabled() else 'regles'}: entree valide sur {ticker} (conf {conf}) "
        f"avec MTF {ht_score}, MACD {'haussier' if macd_val >= macd_sig else 'anticipe'}"
        f"{' et breakout actif' if breakout_on else ''}{exc_txt}{early_txt}{sniper_txt}."
    )
    return parsed, "rules-buy"


def prioritize_scan_tickers(
    tickers: List[str],
    positions: Dict[str, Dict[str, object]],
) -> List[str]:
    """Positions ouvertes d'abord, puis candidats tries par momentum intraday."""
    open_first: List[str] = []
    candidates: List[str] = []
    seen: set[str] = set()
    for tk in tickers:
        n = normalize_ticker(tk)
        if not n or n in seen:
            continue
        seen.add(n)
        st = normalize_position_state(positions.get(n, positions.get(tk, {})))
        if bool(st.get("in_position")) or bool(st.get("pending_sell")):
            open_first.append(n)
        else:
            candidates.append(n)
    scores = {tk: quick_momentum_score_for_ticker(tk) for tk in candidates}
    candidates.sort(key=lambda t: scores.get(t, 0.0), reverse=True)
    if candidates:
        top = ", ".join(f"{t}({scores[t]:+.1f})" for t in candidates[:5])
        print(f"[Universe] Scan priorise momentum (top): {top}")
    return open_first + candidates


def parse_confidence(value: str) -> Optional[int]:
    """Extrait un entier 0-100 depuis la reponse LLM (CONFIANCE)."""
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits:
        return None
    try:
        return max(0, min(100, int(digits[:3])))
    except ValueError:
        return None


def parse_price_value(value: str) -> Optional[float]:
    """Parse un prix utilisateur (Telegram) en float positif."""
    if not value:
        return None
    cleaned = value.strip().replace(",", ".").replace("$", "")
    try:
        p = float(cleaned)
        if p > 0:
            return p
    except ValueError:
        # Tolerant parser: accepte "22.50 (ATR*2)", "stop 172.3 USD", etc.
        m = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
        if m:
            try:
                p = float(m.group(0))
                if p > 0:
                    return p
            except ValueError:
                pass
    return None


def parse_plain_price(value: str) -> Optional[float]:
    """Parse un prix si la chaine entiere est un nombre (evite d'interpreter '2025-...' via regex)."""
    if not value:
        return None
    cleaned = value.strip().replace(",", ".").replace("$", "")
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cleaned):
        return None
    try:
        p = float(cleaned)
    except ValueError:
        return None
    if p > 0:
        return p
    return None


def _token_looks_like_entry_timestamp(s: str) -> bool:
    t = s.strip()
    if not t:
        return False
    if "T" in t:
        return True
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", t):
        return True
    if re.search(r"\b\d{1,2}:\d{2}", t):
        return True
    return False


def parse_entry_ts_utc(*fragments: str) -> Optional[str]:
    """
    Produit un horodatage UTC type '2025-04-22T14:30:00Z' pour entry_ts_utc.
    Accepte le token unique ou plusieurs morceaux (ex: '2025-04-22' '14:30:00').
    """
    s = " ".join(str(f).strip() for f in fragments if f and str(f).strip())
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)
    utc = dt.astimezone(UTC_TZ)
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def parse_notional_usd(value: str) -> Optional[float]:
    """Parse un montant notionnel USD (ex: '500', '500.0', '$500')."""
    if not value:
        return None
    cleaned = value.strip().replace(",", ".").replace("$", "")
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount


def compute_closed_pnl_usd_pct(
    entry_notional_usd: float,
    entry_price_usd: float,
    exit_price_usd: float,
) -> Tuple[Optional[float], Optional[float]]:
    """
    PnL realise coherent avec un releve type courtier (ex. eToro):
    quantite = capital_deploye / prix_moyen_dentree,
    PnL USD = (prix_sortie - prix_moyen) * quantite,
    pourcentage = PnL / capital_deploye (pas seulement variation du cours).
    Les deux derniers sont numeriquement lies si capital et prix moyen sont coherents.
    """
    if entry_notional_usd <= 0 or entry_price_usd <= 0 or exit_price_usd <= 0:
        return None, None
    qty = entry_notional_usd / entry_price_usd
    pnl_usd = (exit_price_usd - entry_price_usd) * qty
    pnl_pct = (pnl_usd / entry_notional_usd) * 100.0
    return pnl_usd, pnl_pct


def strip_edit_entry_kv_tokens(parts: List[str]) -> Tuple[Optional[float], List[str]]:
    """
    Extrait notional= / usd= / montant= / deploy= / ligne= d'une commande /edit_entry
    (montant de la ligne en USD, impact budget deploye). Retourne (notionnel, parts filtrees).
    """
    notional: Optional[float] = None
    kept: List[str] = []
    for p in parts or []:
        m = re.match(
            r"(?i)^(notional|montant|usd|deploy|deploye|ligne|line)[:=](.+)$",
            (p or "").strip(),
        )
        if m:
            n = parse_notional_usd(m.group(2).strip())
            if n is not None:
                notional = n
            continue
        kept.append(p)
    return notional, kept


def parse_edit_entry_trailing(
    data_tail: List[str],
) -> Tuple[Optional[float], Optional[str]]:
    """
    Apres notional + prix d'entree: optionnel 3e prix (cours a l'ordre) puis date/heure en ISO
    (un ou plusieurs tokens). Si le 1er token ressemble a une date, c'est l'heure d'entree
    (pas de 3e prix).
    """
    if not data_tail:
        return None, None
    t0 = (data_tail[0] or "").strip()
    if _token_looks_like_entry_timestamp(t0):
        return None, parse_entry_ts_utc(*data_tail)
    mkt = parse_plain_price(t0)
    if mkt is None:
        mkt = parse_price_value(t0)
    if mkt is None:
        return None, None
    ts: Optional[str] = None
    if len(data_tail) >= 2:
        ts = parse_entry_ts_utc(*data_tail[1:])
    return mkt, ts


def normalize_position_state(raw: Dict[str, object]) -> Dict[str, object]:
    """
    Normalise l'etat position tout en conservant les champs additionnels (ex: take_profit).
    """
    state: Dict[str, object] = dict(raw or {})
    state["in_position"] = bool(state.get("in_position", False))
    state["pending_buy"] = bool(state.get("pending_buy", False))
    state["pending_sell"] = bool(state.get("pending_sell", False))
    tp_raw = state.get("take_profit")
    tp = parse_price_value(str(tp_raw)) if tp_raw is not None else None
    state["take_profit"] = tp
    sl_raw = state.get("stop_loss")
    sl = parse_price_value(str(sl_raw)) if sl_raw is not None else None
    state["stop_loss"] = sl
    notional_raw = state.get("entry_notional_usd")
    notional = parse_notional_usd(str(notional_raw)) if notional_raw is not None else None
    state["entry_notional_usd"] = float(notional) if notional is not None else 0.0
    entry_price_raw = state.get("entry_price_usd")
    entry_price = parse_price_value(str(entry_price_raw)) if entry_price_raw is not None else None
    state["entry_price_usd"] = float(entry_price) if entry_price is not None else 0.0
    conf_raw = state.get("entry_confidence")
    try:
        conf_val = int(conf_raw) if conf_raw is not None else 0
    except Exception:
        conf_val = 0
    state["entry_confidence"] = max(0, min(100, conf_val))
    mkt_raw = state.get("entry_market_price_usd")
    mkt = parse_plain_price(str(mkt_raw)) if mkt_raw is not None else None
    state["entry_market_price_usd"] = float(mkt) if mkt is not None else 0.0
    return state


def deployed_budget_usd(positions: Dict[str, Dict[str, object]], budget_usd: float) -> float:
    """
    Estime le capital deja alloue.
    Par defaut: uniquement positions ouvertes confirmees (cash reellement engage).
    Optionnel via env TRADING_COUNT_PENDING_BUYS_AS_DEPLOYED=1: inclure les achats en attente.
    Les lignes sans entry_notional_usd se partagent le reliquat du budget — sinon
    avec TRADING_MAX_OPEN_POSITIONS eleve et peu de lignes reelles le bot sous-estime
    l'occupation (ex: 2 titres a 500 USD chacun comptes comme 2 x budget/6 seulement).
    """
    include_pending_buys = os.getenv("TRADING_COUNT_PENDING_BUYS_AS_DEPLOYED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    occupied: List[Dict[str, object]] = []
    for raw in positions.values():
        st = normalize_position_state(raw)
        if st.get("in_position") or (include_pending_buys and st.get("pending_buy")):
            occupied.append(st)
    if not occupied:
        return 0.0
    explicit_sum = 0.0
    n_unspecified = 0
    for st in occupied:
        n = float(st.get("entry_notional_usd", 0.0) or 0.0)
        if n > 0:
            explicit_sum += n
        else:
            n_unspecified += 1
    if n_unspecified <= 0:
        return float(explicit_sum)
    remainder = max(0.0, float(budget_usd) - explicit_sum)
    share = remainder / n_unspecified
    total = explicit_sum + share * n_unspecified
    return float(total)


def reserve_cash_per_free_slot() -> float:
    try:
        return max(0.0, float(os.getenv("TRADING_RESERVE_CASH_PER_FREE_SLOT", "150")))
    except ValueError:
        return 150.0


def buyable_remaining_usd(
    effective_budget_usd: float,
    deployed_usd: float,
    *,
    slots_used: int,
    max_open_positions: int,
) -> float:
    """Cash pour achat direct en laissant une reserve par slot libre."""
    remaining = max(0.0, float(effective_budget_usd) - float(deployed_usd))
    free = max(0, int(max_open_positions) - int(slots_used))
    reserve = reserve_cash_per_free_slot() * free
    if reserve <= 0:
        return remaining
    cap_deployed = float(effective_budget_usd) - reserve
    return max(0.0, min(remaining, cap_deployed - float(deployed_usd)))


def remaining_and_suggested_notional_usd(
    positions: Dict[str, Dict[str, object]],
    budget_usd: float,
    max_open_positions: int,
) -> Tuple[float, float]:
    """
    Budget encore disponible et taille indicative pour UNE nouvelle ligne (equilibrage
    sur les slots libres), sans appeler le LLM.
    """
    deployed = deployed_budget_usd(positions, budget_usd)
    remaining = max(0.0, float(budget_usd) - deployed)
    slots_used = count_portfolio_slots_used(positions)
    buyable = buyable_remaining_usd(
        budget_usd, deployed, slots_used=slots_used, max_open_positions=max_open_positions
    )
    vacant = int(max_open_positions) - int(slots_used)
    if vacant <= 0 or buyable <= 0:
        return remaining, 0.0
    suggested = buyable / float(vacant)
    return remaining, suggested


def dynamic_notional_band_usd(
    *,
    current_price: float,
    atr: float,
    conviction: float,
    remaining_usd: float,
    slots_left: int,
    risk_off: bool,
) -> Tuple[float, float, float]:
    """
    Couloir [min, max] + valeur cible (apres clamp) pour le sizing dynamique.
    - Base: cash restant / min(slots libres, TRADING_DYNAMIC_BASE_SLOT_DIV_CAP) (defaut 3)
       pour eviter /6 lorsque peu de lignes sont reellement vises (voir .env ; 0 = tout slots).
    - Volatilite: ATR% eleve => taille reduite ; ATR% faible => taille augmentee
    - Conviction: score/confiance plus haut => taille plus haute
    - Option TRADING_DYNAMIC_MIN_LINE_USD: plancher USD sur le minimum du couloir.
    Retourne (suggere_clamp, min_usd, max_usd).
    """
    if remaining_usd <= 0 or slots_left <= 0:
        return 0.0, 0.0, 0.0
    try:
        cap_div = os.getenv("TRADING_DYNAMIC_BASE_SLOT_DIV_CAP", "").strip()
        cap_slots = int(cap_div) if cap_div else 3
        if cap_slots <= 0:
            slot_chunks = max(1, int(slots_left))
        else:
            # Evite de tirer trop vers le bas le notionnel quand beaucoup de slots sont libres :
            # ex. 1158 USD libres avec 6 slots -> base ancienne = /6 (~193) ; avec cap 3 -> /386.
            slot_chunks = max(1, min(int(slots_left), cap_slots))
    except ValueError:
        slot_chunks = max(1, min(int(slots_left), 3))
    base = remaining_usd / float(slot_chunks)

    price = max(1e-6, float(current_price))
    atr_pct = (float(atr) / price) if atr > 0 else 0.02  # fallback prudent
    atr_low = 0.012
    atr_high = 0.06
    atr_clamped = max(atr_low, min(atr_high, atr_pct))
    # ATR faible => vol_mult vers 1.30 ; ATR eleve => vol_mult vers 0.72
    t = (atr_clamped - atr_low) / (atr_high - atr_low)
    vol_mult = 1.30 - (0.58 * t)

    conf = max(45.0, min(95.0, float(conviction)))
    # Conviction 45..95 -> 0.88..1.22
    conf_mult = 0.88 + ((conf - 45.0) / 50.0) * 0.34
    regime_mult = 0.92 if risk_off else 1.0

    try:
        target_env = os.getenv("TRADING_TARGET_LINE_USD", "").strip()
        line_target = float(target_env) if target_env else 0.0
    except ValueError:
        line_target = 0.0
    if line_target > 0:
        line_target *= line_step_factor()[0]   # reinvestissement par paliers (1.0 si desactive)
        raw = line_target * vol_mult * conf_mult * regime_mult
        band_min = max(60.0, min(remaining_usd, line_target * 0.90))
        band_max = max(band_min, min(remaining_usd, line_target * 1.10))
        clamped = max(band_min, min(band_max, raw))
        return clamped, band_min, band_max

    raw = base * vol_mult * conf_mult * regime_mult
    try:
        floor_env = os.getenv("TRADING_DYNAMIC_MIN_LINE_USD", "").strip()
        line_floor = float(floor_env) if floor_env else 0.0
    except ValueError:
        line_floor = 0.0
    min_formula = max(60.0, remaining_usd * 0.08)
    if line_floor > 0:
        min_formula = max(min_formula, line_floor)
    min_line = min(remaining_usd, min_formula)
    max_line = max(min_line, remaining_usd * (0.35 if risk_off else 0.50))
    clamped = max(min_line, min(max_line, raw))
    return clamped, min_line, max_line


def cap_notional_by_risk_pct(
    notional_usd: float,
    *,
    entry_price: float,
    stop_price: Optional[float],
    effective_budget_usd: float,
    risk_pct: float,
) -> float:
    """
    Plafonne le notionnel pour que la perte au stop ne depasse pas risk_pct % du budget effectif.
    Ex: 1% sur 1000 USD => perte max ~10 USD si le stop est touche.
    """
    if notional_usd <= 0 or entry_price <= 0 or effective_budget_usd <= 0:
        return max(0.0, notional_usd)
    if stop_price is None or stop_price <= 0 or stop_price >= entry_price:
        return notional_usd
    try:
        risk_pct = max(0.1, float(risk_pct))
    except (TypeError, ValueError):
        risk_pct = 1.0
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return notional_usd
    max_loss_usd = effective_budget_usd * (risk_pct / 100.0)
    max_shares = max_loss_usd / risk_per_share
    max_notional = max_shares * entry_price
    if max_notional <= 0:
        return 0.0
    if max_notional < notional_usd:
        return max_notional
    return notional_usd


def apply_risk_cap_to_notional(
    notional_usd: float,
    *,
    entry_price: float,
    stop_price: Optional[float],
    effective_budget_usd: float,
) -> Tuple[float, bool]:
    """Applique TRADING_ENFORCE_RISK_CAP + TRADING_RISK_PER_TRADE_PCT si actif."""
    enforce = os.getenv("TRADING_ENFORCE_RISK_CAP", "1").strip().lower() in {"1", "true", "yes", "y"}
    if not enforce or notional_usd <= 0:
        return notional_usd, False
    try:
        risk_pct = float(os.getenv("TRADING_RISK_PER_TRADE_PCT", "1.0"))
    except ValueError:
        risk_pct = 1.0
    capped = cap_notional_by_risk_pct(
        notional_usd,
        entry_price=entry_price,
        stop_price=stop_price,
        effective_budget_usd=effective_budget_usd,
        risk_pct=risk_pct,
    )
    return capped, capped + 1e-6 < notional_usd


def stop_atr_mult_for_ticker(ticker: str) -> float:
    """Multiplicateur ATR minimal sous l'entree (defaut global ou override TICKER:mult)."""
    tk = normalize_ticker(ticker)
    raw = os.getenv("TRADING_TICKER_STOP_ATR_MULT", "").strip()
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        sym, mult_txt = part.split(":", 1)
        if normalize_ticker(sym.strip()) != tk:
            continue
        try:
            return max(1.0, float(mult_txt.strip()))
        except ValueError:
            break
    try:
        return max(1.0, float(os.getenv("TRADING_MIN_STOP_ATR_MULT", "2.5")))
    except ValueError:
        return 2.5


def min_stop_distance_pct() -> float:
    try:
        return max(0.5, float(os.getenv("TRADING_MIN_STOP_DIST_PCT", "4.0"))) / 100.0
    except ValueError:
        return 0.04


def max_stop_distance_pct() -> float:
    """Plafond % sous l'entree (0 = desactive). Evite des stops intraday trop larges (ex. 9%+)."""
    try:
        return max(0.0, float(os.getenv("TRADING_MAX_STOP_DIST_PCT", "7.0"))) / 100.0
    except ValueError:
        return 0.07


def fixed_sl_tp_enabled() -> bool:
    return os.getenv("BUY_FIXED_SL_TP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "y"}


def trailing_tp_enabled() -> bool:
    """TP dynamique ('peak lock'): une fois le TP fixe atteint, le stop suit le plus haut."""
    return os.getenv("TRAILING_TP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "y"}


def trailing_tp_ceiling_pct() -> float:
    """Plafond provisoire (marge de securite) au-dessus du plus haut suivi, pour la jambe LMT de l'OCA."""
    try:
        return max(1.0, float(os.getenv("TRAILING_TP_CEILING_PCT", "15")))
    except ValueError:
        return 15.0


def trailing_tp_check_sec() -> float:
    """Cadence de verification/remontee du stop dynamique (entre 2 cycles de scan)."""
    try:
        return max(30.0, float(os.getenv("TRAILING_TP_CHECK_SEC", "300")))
    except ValueError:
        return 300.0


def sniper_mode_enabled() -> bool:
    return os.getenv("BUY_SNIPER_MODE", "0").strip().lower() in {"1", "true", "yes", "y"}


def sniper_min_confidence() -> int:
    try:
        return max(0, min(100, int(os.getenv("BUY_SNIPER_MIN_CONF", "78"))))
    except ValueError:
        return 78


def sniper_min_mtf_score() -> int:
    try:
        return max(0, min(2, int(os.getenv("BUY_SNIPER_MIN_MTF", "2"))))
    except ValueError:
        return 2


def sniper_breakout_required() -> bool:
    return os.getenv("BUY_SNIPER_BREAKOUT_REQUIRED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def sniper_alt_min_confidence() -> int:
    """Chemin structure sniper light: conf elevee sans breakout."""
    try:
        return max(0, min(100, int(os.getenv("BUY_SNIPER_ALT_MIN_CONF", "75"))))
    except ValueError:
        return 75


def sniper_alt_min_mtf_score() -> int:
    try:
        return max(0, min(2, int(os.getenv("BUY_SNIPER_ALT_MIN_MTF", "2"))))
    except ValueError:
        return 2


def sniper_entry_ok(
    *,
    conf: int,
    ht_score: int,
    macd_ok: bool,
    breakout_on: bool,
    risk_off: bool,
) -> Tuple[bool, str]:
    """
    Sniper light: breakout + base OU structure forte (conf/MTF/MACD) sans breakout.
    """
    if risk_off:
        return False, "risk-off actif"
    conf_min = sniper_min_confidence()
    ht_min = sniper_min_mtf_score()
    if sniper_breakout_required():
        if breakout_on and conf >= conf_min and macd_ok and ht_score >= ht_min:
            return True, "breakout 15m"
        return False, "breakout 15m requis (mode sniper strict)"
    breakout_path = breakout_on and conf >= conf_min and macd_ok and ht_score >= ht_min
    if breakout_path:
        return True, "breakout 15m"
    alt_conf = sniper_alt_min_confidence()
    alt_mtf = sniper_alt_min_mtf_score()
    if conf >= alt_conf and ht_score >= alt_mtf and macd_ok:
        return True, f"structure forte (conf>={alt_conf}, MTF>={alt_mtf})"
    if not breakout_on:
        return False, (
            f"pas de breakout 15m ni structure "
            f"(conf>={alt_conf}, MTF>={alt_mtf}, MACD ok)"
        )
    if conf < conf_min:
        return False, f"conf {conf} < {conf_min}"
    if ht_score < ht_min:
        return False, f"MTF {ht_score} < {ht_min}"
    if not macd_ok:
        return False, "MACD faible"
    return False, "setup incomplet"


def fixed_stop_distance_pct() -> float:
    """Distance stop fixe sous l'entree (ex: 2.5 -> 2.5%)."""
    try:
        pct = max(0.5, min(15.0, float(os.getenv("BUY_FIXED_STOP_PCT", "2.5")))) / 100.0
    except ValueError:
        pct = 0.025
    if sniper_mode_enabled():
        try:
            sniper_cap = max(0.5, min(15.0, float(os.getenv("BUY_SNIPER_MAX_STOP_PCT", "2.0")))) / 100.0
        except ValueError:
            sniper_cap = 0.02
        pct = min(pct, sniper_cap)
    return pct


def fixed_rr_reward_multiplier() -> float:
    """Gain cible = mult x risque (1:2 -> mult=2)."""
    try:
        return max(1.0, min(5.0, float(os.getenv("BUY_RR_REWARD_MULT", "2.0"))))
    except ValueError:
        return 2.0


def compute_fixed_sl_tp(entry_price: float, ticker: str = "") -> Tuple[float, float]:
    """
    SL fixe (% sous entree), TP fixe (R:R 1:mult).
    Arrondi toujours vers le haut (SL et TP) plutot qu'au plus proche: sur les
    titres a prix bas (stop 2% = quelques centimes), un round-to-nearest classique
    pouvait faire tomber le R:R realise sous BUY_MIN_RR par pur artefact d'arrondi
    (ex: SOFI ~18$, RR nominal 1.75 mais 1.7397 realise -> rejet ACHETER alors que
    le trade est exactement conforme a la regle). Arrondir vers le haut garantit
    risk <= risk nominal et reward >= reward nominal, donc RR realise >= mult.
    """
    _ = ticker
    if entry_price <= 0:
        return 0.0, 0.0
    risk_usd = entry_price * fixed_stop_distance_pct()
    # -1e-9 avant ceil: evite un cent de trop quand la valeur est deja "ronde"
    # mais legerement au-dessus a cause de l'imprecision flottante.
    sl = math.ceil(max(0.01, entry_price - risk_usd) * 100 - 1e-9) / 100.0
    tp = math.ceil((entry_price + risk_usd * fixed_rr_reward_multiplier()) * 100 - 1e-9) / 100.0
    return sl, tp


def stop_distance_floor_usd(
    entry_price: float,
    atr: float,
    ticker: str,
) -> Tuple[float, float, float]:
    """
    Distance stop sous l'entree (USD) apres plancher ATR/% et plafond %.
    Retourne (distance_usd, mult_atr, max_pct_applique_ou_0).
    """
    if entry_price <= 0:
        return 0.0, stop_atr_mult_for_ticker(ticker), 0.0
    mult = stop_atr_mult_for_ticker(ticker)
    min_pct = min_stop_distance_pct()
    max_pct = max_stop_distance_pct()
    atr_v = max(0.0, float(atr))
    raw = max(atr_v * mult, entry_price * min_pct) if atr_v > 0 else entry_price * min_pct
    capped = False
    if max_pct > 0 and raw > entry_price * max_pct + 1e-9:
        raw = entry_price * max_pct
        capped = True
    return raw, mult, max_pct if capped else 0.0


def buy_max_tp_notional_threshold_usd() -> float:
    """En dessous = petit/moyen ticket (TP plus large), au dessus = gros ticket."""
    try:
        raw = float(os.getenv("BUY_MAX_TP_SIZE_THRESHOLD_USD", "175"))
    except ValueError:
        raw = 175.0
    return max(50.0, raw)


def buy_max_tp_pct_small() -> float:
    try:
        raw = float(os.getenv("BUY_MAX_TP_PCT_SMALL", "0.055"))
    except ValueError:
        raw = 0.055
    return max(0.01, min(0.30, raw))


def buy_max_tp_pct_large() -> float:
    try:
        raw = float(os.getenv("BUY_MAX_TP_PCT_LARGE", "0.035"))
    except ValueError:
        raw = 0.035
    return max(0.01, min(0.30, raw))


def buy_max_tp_pct(notional_usd: Optional[float] = None) -> float:
    """Plafond TP au-dessus de l'entree, selon la taille de position si connue."""
    if notional_usd is not None and float(notional_usd) > 0:
        if float(notional_usd) < buy_max_tp_notional_threshold_usd():
            return buy_max_tp_pct_small()
        return buy_max_tp_pct_large()
    legacy = os.getenv("BUY_MAX_TP_PCT", "").strip()
    if legacy:
        try:
            return max(0.01, min(0.30, float(legacy)))
        except ValueError:
            pass
    return buy_max_tp_pct_small()


def risk_reward_meets_minimum(rr: float, min_rr: float, eps: float = 1e-6) -> bool:
    """Evite les rejets RR du type 1.00 < 1.00 (arrondi flottant)."""
    return float(rr) + eps >= float(min_rr)


def cap_take_profit_by_pct(
    entry_price: float,
    take_profit: Optional[float],
    *,
    notional_usd: Optional[float] = None,
) -> Tuple[Optional[float], bool, float]:
    """Cap hard du TP en % de l'entree pour eviter des objectifs irrealistes."""
    max_pct = buy_max_tp_pct(notional_usd)
    if notional_usd is None or float(notional_usd) <= 0:
        return take_profit, False, max_pct
    if take_profit is None or entry_price <= 0:
        return take_profit, False, max_pct
    try:
        tp = float(take_profit)
    except (TypeError, ValueError):
        return take_profit, False, max_pct
    if tp <= entry_price:
        return tp, False, max_pct
    cap_tp = round(entry_price * (1.0 + max_pct), 2)
    if tp <= cap_tp + 1e-9:
        return tp, False, max_pct
    return cap_tp, True, max_pct


def adjust_sl_tp_atr_floor(
    *,
    entry_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    atr: float,
    ticker: str = "",
    min_rr: Optional[float] = None,
    notional_usd: Optional[float] = None,
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """
    Elargit le stop si le modele/LLM propose un stop trop serre (ATR ou % plancher).
    Ne resserre jamais. Recale le TP pour conserver le R:R initial si possible.
    Mode BUY_FIXED_SL_TP_ENABLED: SL/TP fixes (% stop + R:R 1:mult).
    """
    if fixed_sl_tp_enabled() and entry_price > 0:
        sl_f, tp_f = compute_fixed_sl_tp(entry_price, ticker)
        stop_pct = fixed_stop_distance_pct() * 100.0
        reward_pct = stop_pct * fixed_rr_reward_multiplier()
        note = (
            f"SL/TP fixes: stop -{stop_pct:.2f}%, TP +{reward_pct:.2f}% "
            f"(R:R 1:{fixed_rr_reward_multiplier():.1f})."
        )
        return sl_f, tp_f, note
    if stop_loss is None or entry_price <= 0:
        capped_tp, capped, max_pct = cap_take_profit_by_pct(
            entry_price, take_profit, notional_usd=notional_usd
        )
        if capped:
            return (
                stop_loss,
                capped_tp,
                f"TP resserre {float(take_profit):.2f} -> {float(capped_tp):.2f} USD (cap +{max_pct * 100:.1f}%).",
            )
        return stop_loss, take_profit, None
    try:
        sl = float(stop_loss)
    except (TypeError, ValueError):
        return stop_loss, take_profit, None
    if sl <= 0 or sl >= entry_price:
        capped_tp, capped, max_pct = cap_take_profit_by_pct(
            entry_price, take_profit, notional_usd=notional_usd
        )
        if capped:
            return (
                stop_loss,
                capped_tp,
                f"TP resserre {float(take_profit):.2f} -> {float(capped_tp):.2f} USD (cap +{max_pct * 100:.1f}%).",
            )
        return stop_loss, take_profit, None
    atr_v = max(0.0, float(atr))
    min_dist, mult, cap_pct = stop_distance_floor_usd(entry_price, atr_v, ticker)
    min_pct = min_stop_distance_pct()
    current_dist = entry_price - sl
    new_sl = sl
    notes: List[str] = []
    if current_dist < min_dist - 1e-6:
        new_sl = round(entry_price - min_dist, 2)
        cap_txt = f", plafond {cap_pct * 100:.1f}%" if cap_pct > 0 else ""
        notes.append(
            f"Stop elargi {sl:.2f} -> {new_sl:.2f} USD "
            f"({(entry_price - new_sl) / entry_price * 100:.1f}% sous entree, "
            f"regle {mult:.1f}x ATR14 / min {min_pct * 100:.1f}%{cap_txt}, {normalize_ticker(ticker)})"
        )
    new_tp = take_profit
    if take_profit is not None:
        try:
            tp = float(take_profit)
        except (TypeError, ValueError):
            tp = 0.0
        if tp > entry_price:
            old_risk = entry_price - sl
            old_reward = tp - entry_price
            if old_risk > 1e-6 and old_reward > 0:
                rr = old_reward / old_risk
                if min_rr is not None and float(min_rr) > 0:
                    rr = max(rr, float(min_rr))
                new_risk = entry_price - new_sl
                new_tp = round(entry_price + new_risk * rr, 2)
    capped_tp, tp_capped, max_tp_pct = cap_take_profit_by_pct(
        entry_price, new_tp, notional_usd=notional_usd
    )
    if tp_capped and new_tp is not None and capped_tp is not None:
        notes.append(f"TP resserre {float(new_tp):.2f} -> {float(capped_tp):.2f} USD (cap +{max_tp_pct * 100:.1f}%).")
    if (
        min_rr is not None
        and float(min_rr) > 0
        and capped_tp is not None
        and entry_price > float(new_sl)
    ):
        new_risk = entry_price - float(new_sl)
        min_tp_rr = entry_price + new_risk * float(min_rr)
        cap_max = round(entry_price * (1.0 + max_tp_pct), 2)
        if min_tp_rr <= cap_max + 1e-6 and float(capped_tp) < min_tp_rr - 1e-6:
            capped_tp = round(min(min_tp_rr, cap_max), 2)
    return new_sl, capped_tp, " | ".join(notes) if notes else None


def tighten_open_positions_take_profit(
    positions: Dict[str, Dict[str, object]],
    *,
    position_file: str,
) -> int:
    """Resserre les TP des lignes ouvertes selon le cap TP (taille de position)."""
    if fixed_sl_tp_enabled():
        return 0
    changed = 0
    for tk, raw in list(positions.items()):
        st = normalize_position_state(raw)
        if not bool(st.get("in_position")) or bool(st.get("pending_sell")):
            continue
        entry = float(st.get("entry_price_usd", 0.0) or 0.0)
        if entry <= 0:
            continue
        notional = float(st.get("entry_notional_usd", 0.0) or 0.0)
        tp_raw = st.get("take_profit")
        tp = float(tp_raw) if tp_raw is not None else 0.0
        if tp <= entry:
            continue
        new_tp, was_capped, max_pct = cap_take_profit_by_pct(entry, tp, notional_usd=notional)
        if not was_capped or new_tp is None:
            continue
        st["take_profit"] = float(new_tp)
        positions[tk] = st
        changed += 1
        print(f"[{tk}] TP resserre (ligne ouverte): {tp:.2f} -> {float(new_tp):.2f} USD (cap +{max_pct * 100:.1f}%).")
    if changed > 0:
        save_positions(position_file, positions)
    return changed


def suggest_dynamic_notional_usd(
    *,
    current_price: float,
    atr: float,
    conviction: float,
    remaining_usd: float,
    slots_left: int,
    risk_off: bool,
) -> float:
    """Valeur indicative unique (compatible ancien code). Meme logique que dynamic_notional_band_usd."""
    v, _, _ = dynamic_notional_band_usd(
        current_price=current_price,
        atr=atr,
        conviction=conviction,
        remaining_usd=remaining_usd,
        slots_left=slots_left,
        risk_off=risk_off,
    )
    return v


def average_open_entry_notional_usd(positions: Dict[str, Dict[str, object]]) -> float:
    total = 0.0
    n = 0
    for raw in positions.values():
        st = normalize_position_state(raw)
        if not st.get("in_position"):
            continue
        v = float(st.get("entry_notional_usd", 0.0) or 0.0)
        if v > 0:
            total += v
            n += 1
    return total / float(n) if n > 0 else 0.0


def derive_buy_notional_usd(
    *,
    current_price: float,
    atr: float,
    conviction: float,
    remaining_usd: float,
    slots_left: int,
    risk_off: bool,
    positions: Dict[str, Dict[str, object]],
    max_open_positions: int,
) -> Tuple[float, float, float]:
    """
    Notional pour entree scan (achat direct ou proposition swap).
    Slots pleins: size comme remplacement d'une ligne, pas abandon du signal.
    """
    slots_for_band = max(1, int(slots_left))
    derived, dyn_min, dyn_max = dynamic_notional_band_usd(
        current_price=current_price,
        atr=atr,
        conviction=conviction,
        remaining_usd=remaining_usd,
        slots_left=slots_for_band,
        risk_off=risk_off,
    )
    if derived <= 0 and int(slots_left) <= 0:
        avg = average_open_entry_notional_usd(positions)
        derived = avg if avg > 0 else max(80.0, remaining_usd / max(1, int(max_open_positions)))
        dyn_min = derived * 0.85
        dyn_max = derived * 1.15
    return derived, dyn_min, dyn_max


def build_portfolio_snapshot(
    positions: Dict[str, Dict[str, object]],
    *,
    budget_usd: float,
    max_open_positions: int,
    journal_file: Optional[str] = None,
) -> str:
    """Resume l'etat portefeuille en mode operateur (Telegram/heartbeat)."""
    realized_total = 0.0
    effective_budget_usd = float(budget_usd)
    if journal_file:
        realized_total = compute_total_realized_pnl_usd(journal_file)
        effective_budget_usd = max(0.0, float(budget_usd) + realized_total)

    deployed = deployed_budget_usd(positions, effective_budget_usd)
    # Valeur affichee non plafonnee (un depassement doit se voir), separee du
    # "suggested" qui reste borne a >=0 puisqu'il sert au sizing du prochain achat.
    remaining = effective_budget_usd - deployed
    _, suggested = remaining_and_suggested_notional_usd(
        positions, effective_budget_usd, max_open_positions
    )
    open_tickers = list_open_tickers(positions)
    pending_buys = sorted(
        tk for tk, raw in positions.items() if normalize_position_state(raw).get("pending_buy")
    )
    pending_sells = sorted(
        tk for tk, raw in positions.items() if normalize_position_state(raw).get("pending_sell")
    )
    slots_used = count_portfolio_slots_used(positions)
    if journal_file:
        lines = [
            f"Budget base: {budget_usd:.0f} USD | PnL realise cumule: {realized_total:+.2f} USD | Budget effectif: {effective_budget_usd:.2f} USD",
            f"Deploye: {deployed:.2f} USD | Libre: {remaining:.2f} USD",
            f"Slots occupes: {slots_used}/{max_open_positions}",
            f"Positions ouvertes: {', '.join(open_tickers) if open_tickers else 'aucune'}",
            f"Achats en attente: {', '.join(pending_buys) if pending_buys else 'aucun'}",
            f"Ventes en attente: {', '.join(pending_sells) if pending_sells else 'aucune'}",
        ]
    else:
        lines = [
            f"Budget: {budget_usd:.0f} USD | Deploye: {deployed:.2f} USD | Libre: {remaining:.2f} USD",
            f"Slots occupes: {slots_used}/{max_open_positions}",
            f"Positions ouvertes: {', '.join(open_tickers) if open_tickers else 'aucune'}",
            f"Achats en attente: {', '.join(pending_buys) if pending_buys else 'aucun'}",
            f"Ventes en attente: {', '.join(pending_sells) if pending_sells else 'aucune'}",
        ]
    if suggested > 0:
        lines.append(f"Ligne moyenne (reference): ~{suggested:.0f} USD")
        lines.append("Sizing reel: ajuste par ticker selon volatilite (ATR) + conviction + regime marche.")
    return "\n".join(lines)


def _cockpit_unrealized_totals(
    pos_rows: List[Dict[str, object]],
    *,
    deployed_usd: float = 0.0,
) -> Tuple[Optional[float], Optional[float]]:
    """Somme PnL latent (USD et % vs capital deploye) sur les lignes avec prix live."""
    total_usd = 0.0
    has_any = False
    for row in pos_rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("pnl_usd")
        if raw is None:
            continue
        try:
            total_usd += float(raw)
            has_any = True
        except (TypeError, ValueError):
            continue
    if not has_any:
        return None, None
    total_usd = round(total_usd, 2)
    pct: Optional[float] = None
    if deployed_usd > 0:
        pct = round(total_usd / float(deployed_usd) * 100.0, 2)
    return total_usd, pct


def _cockpit_apply_unrealized_summary(data: Dict[str, object]) -> None:
    """Met a jour summary.unrealized_pnl_* depuis data.positions."""
    summary = data.get("summary")
    if not isinstance(summary, dict):
        return
    rows = data.get("positions")
    if not isinstance(rows, list):
        return
    try:
        deployed = float(summary.get("deployed_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        deployed = 0.0
    u_usd, u_pct = _cockpit_unrealized_totals(rows, deployed_usd=deployed)
    summary["unrealized_pnl_usd"] = u_usd
    summary["unrealized_pnl_pct"] = u_pct


def _cockpit_row_price_metrics(
    *,
    px: float,
    entry: float,
    notional: float,
    sl: Optional[float],
    tp: Optional[float],
) -> Dict[str, object]:
    pnl_pct = ((px - entry) / entry) * 100.0
    pnl_usd = notional * (pnl_pct / 100.0) if notional > 0 else 0.0
    metrics: Dict[str, object] = {
        "price_usd": round(px, 4),
        "pnl_usd": round(pnl_usd, 2),
        "pnl_pct": round(pnl_pct, 2),
        "dist_sl_pct": None,
        "dist_tp_pct": None,
    }
    if sl is not None and sl > 0:
        metrics["dist_sl_pct"] = round((px - float(sl)) / px * 100.0, 2)
    if tp is not None and tp > 0:
        metrics["dist_tp_pct"] = round((float(tp) - px) / px * 100.0, 2)
    return metrics


def enrich_cockpit_data_live_prices(
    data: Dict[str, object],
    *,
    skip_ibkr: bool = False,
) -> Dict[str, object]:
    """Rafraichit prix / PnL / dist SL-TP pour la page web (entre deux cycles)."""
    import copy

    out = copy.deepcopy(data)
    rows = out.get("positions")
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        tk = normalize_ticker(str(row.get("ticker", "")))
        entry_raw = row.get("entry_usd")
        if not tk or entry_raw is None:
            continue
        try:
            entry = float(entry_raw)
        except (TypeError, ValueError):
            continue
        if entry <= 0:
            continue
        px = get_reference_price_usd(tk, skip_ibkr=skip_ibkr)
        if px is None:
            continue
        notional = float(row.get("notional_usd", 0.0) or 0.0)
        sl_raw = row.get("stop_loss")
        tp_raw = row.get("take_profit")
        sl = float(sl_raw) if sl_raw is not None else None
        tp = float(tp_raw) if tp_raw is not None else None
        row.update(
            _cockpit_row_price_metrics(
                px=float(px),
                entry=entry,
                notional=notional,
                sl=sl,
                tp=tp,
            )
        )
    _cockpit_apply_unrealized_summary(out)
    now_ny = datetime.now(MARKET_TZ)
    out["prices_live"] = True
    out["prices_updated_at_ny"] = now_ny.strftime("%Y-%m-%d %H:%M:%S %Z")
    out["prices_updated_at_utc"] = datetime.now(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


def build_cockpit_data(
    positions: Dict[str, Dict[str, object]],
    *,
    budget_usd: float,
    max_open_positions: int,
    journal_file: str,
    cycle_idx: int = 0,
    market_open: bool = True,
    scan_interval_min: int = 15,
) -> Dict[str, object]:
    """Donnees structurees cockpit (web live + Telegram)."""
    realized_total = compute_total_realized_pnl_usd(journal_file)
    effective_budget = max(0.0, float(budget_usd) + realized_total)
    deployed = deployed_budget_usd(positions, effective_budget)
    # Non plafonne a 0: un depassement (ex: position fusionnee > budget ref.)
    # doit rester visible dans le cockpit plutot que d'afficher "Libre: 0.00$"
    # comme si tout etait exactement a l'equilibre.
    remaining = effective_budget - deployed
    slots_used = count_portfolio_slots_used(positions)
    now_ny = datetime.now(MARKET_TZ)
    now_utc = datetime.now(UTC_TZ)

    pos_rows: List[Dict[str, object]] = []
    for tk in sorted(positions.keys()):
        st = normalize_position_state(positions.get(tk, {}))
        if not bool(st.get("in_position")):
            continue
        entry = float(st.get("entry_price_usd", 0.0) or 0.0)
        notional = float(st.get("entry_notional_usd", 0.0) or 0.0)
        sl = parse_price_value(str(st.get("stop_loss", "")))
        tp = parse_price_value(str(st.get("take_profit", "")))
        px = get_reference_price_usd(tk)
        qty: Optional[float] = None
        pct_budget: Optional[float] = None
        if notional > 0 and entry > 0:
            qty = round(notional / entry, 4)
            if effective_budget > 0:
                pct_budget = round(notional / effective_budget * 100.0, 1)
        row: Dict[str, object] = {
            "ticker": tk,
            "entry_usd": entry if entry > 0 else None,
            "notional_usd": notional if notional > 0 else None,
            "qty": qty,
            "pct_budget": pct_budget,
            "stop_loss": float(sl) if sl and sl > 0 else None,
            "take_profit": float(tp) if tp and tp > 0 else None,
            "confidence": int(st.get("entry_confidence", 0) or 0),
            "ibkr_sl_tp_active": bool(st.get("ibkr_sl_tp_active")),
            "price_usd": None,
            "pnl_usd": None,
            "pnl_pct": None,
            "dist_sl_pct": None,
            "dist_tp_pct": None,
        }
        if px is not None and entry > 0:
            row.update(
                _cockpit_row_price_metrics(
                    px=float(px),
                    entry=entry,
                    notional=notional,
                    sl=float(sl) if sl and sl > 0 else None,
                    tp=float(tp) if tp and tp > 0 else None,
                )
            )
        pos_rows.append(row)

    # PnL de la strategie EN COURS seulement (depuis RISK_MAX_DD_START_DATE): le cumul
    # ci-dessus melange toutes les configs depuis le debut du paper et reste BRUT.
    strat_since = os.getenv("RISK_MAX_DD_START_DATE", "").strip()
    strat_pnl: Optional[float] = None
    strat_n = 0
    if strat_since:
        try:
            since_d = datetime.strptime(strat_since, "%Y-%m-%d").date()
            strat_pnl = 0.0
            for ev in load_trade_closed_events(journal_file):
                ts_ev = _parse_journal_ts_to_ny(str(ev.get("ts_utc", "")))
                if ts_ev is not None and ts_ev.date() >= since_d:
                    strat_pnl += float(ev.get("pnl_usd", 0.0) or 0.0)
                    strat_n += 1
        except (TypeError, ValueError):
            strat_pnl = None
    try:
        fee_per_order = float(os.getenv("COCKPIT_FEE_PER_ORDER_USD", "0.40"))
    except ValueError:
        fee_per_order = 0.40
    _, breaker_dd_pct, breaker_limit = equity_drawdown_blocks_buys(float(budget_usd), journal_file)
    pdt_n = count_recent_day_trades(journal_file)
    try:
        line_base = float(os.getenv("TRADING_TARGET_LINE_USD", "0") or 0)
    except ValueError:
        line_base = 0.0
    line_factor, line_k, _ = line_step_factor(journal_file)
    line_step_on = float(os.getenv("TRADING_LINE_STEP_PCT", "0") or 0) > 0

    try:
        live_price_refresh_sec = float(os.getenv("COCKPIT_WEB_PRICE_REFRESH_SEC", "5"))
    except ValueError:
        live_price_refresh_sec = 5.0
    live_prices_on = os.getenv("COCKPIT_WEB_LIVE_PRICES", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }

    data: Dict[str, object] = {
        "updated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at_ny": now_ny.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "cycle_idx": int(cycle_idx),
        "market_open": bool(market_open),
        "scan_interval_min": int(scan_interval_min),
        "live_prices_enabled": live_prices_on,
        "live_price_refresh_sec": max(2.0, live_price_refresh_sec),
        "summary": {
            "budget_effective_usd": round(effective_budget, 2),
            "deployed_usd": round(deployed, 2),
            "remaining_usd": round(remaining, 2),
            "slots_used": slots_used,
            "max_slots": int(max_open_positions),
            "realized_pnl_usd": round(realized_total, 2),   # brut, toutes configs confondues
            "strategy_since": strat_since or None,
            "strategy_trades": strat_n,
            "strategy_realized_usd": round(strat_pnl, 2) if strat_pnl is not None else None,
            "strategy_realized_net_est_usd": (round(strat_pnl - 2 * fee_per_order * strat_n, 2)
                                              if strat_pnl is not None else None),
            "breaker_dd_pct": round(breaker_dd_pct, 1) if breaker_limit > 0 else None,
            "breaker_limit_pct": breaker_limit if breaker_limit > 0 else None,
            "pdt_day_trades_5d": pdt_n,
            "line_target_usd": round(line_base * line_factor, 2) if line_base > 0 else None,
            "line_step_k": line_k if line_step_on else None,
            "pdt_limit": (int(os.getenv("RISK_PDT_MAX_DAY_TRADES", "3") or 3)
                          if _env_on("RISK_PDT_GUARD_ENABLED") else None),
        },
        "positions": pos_rows,
    }
    _cockpit_apply_unrealized_summary(data)
    return data


def build_cockpit_snapshot(
    positions: Dict[str, Dict[str, object]],
    *,
    budget_usd: float,
    max_open_positions: int,
    journal_file: str,
) -> str:
    """Vue Telegram compacte type 'cockpit' pour suivi live."""
    data = build_cockpit_data(
        positions,
        budget_usd=budget_usd,
        max_open_positions=max_open_positions,
        journal_file=journal_file,
    )
    summary = data.get("summary") or {}
    unrealized_usd = summary.get("unrealized_pnl_usd")
    unrealized_pct = summary.get("unrealized_pnl_pct")
    latent_line = "PnL latent: indisponible (prix live manquants)"
    if unrealized_usd is not None:
        pct_txt = f" ({float(unrealized_pct):+.2f}%)" if unrealized_pct is not None else ""
        latent_line = f"PnL latent: {float(unrealized_usd):+.2f} USD{pct_txt}"
    lines = [
        "COCKPIT LIVE",
        (
            f"Budget eff.: {summary.get('budget_effective_usd', 0):.2f} USD "
            f"| Deploye: {summary.get('deployed_usd', 0):.2f} | Libre: {summary.get('remaining_usd', 0):.2f}"
        ),
        (
            f"Slots: {summary.get('slots_used', 0)}/{summary.get('max_slots', 0)} "
            f"| PnL realise cumule: {float(summary.get('realized_pnl_usd', 0) or 0):+.2f} USD"
        ),
        latent_line,
    ]
    pos_rows = data.get("positions") or []
    if pos_rows:
        lines.append("")
        lines.append("Positions ouvertes:")
        for p in pos_rows:
            if not isinstance(p, dict):
                continue
            tk = str(p.get("ticker", "?"))
            entry = p.get("entry_usd")
            px = p.get("price_usd")
            pnl_usd = p.get("pnl_usd")
            pnl_pct = p.get("pnl_pct")
            if px is not None and entry is not None:
                sl_gap = ""
                tp_gap = ""
                if p.get("dist_sl_pct") is not None:
                    sl_gap = f" | dist SL {float(p['dist_sl_pct']):+.2f}%"
                if p.get("dist_tp_pct") is not None:
                    tp_gap = f" | dist TP {float(p['dist_tp_pct']):+.2f}%"
                invest = ""
                if p.get("notional_usd") is not None:
                    invest = f" | investi {float(p['notional_usd']):.2f} USD"
                    if p.get("qty") is not None:
                        invest += f" ({float(p['qty']):g} act.)"
                lines.append(
                    f"- {tk}: px {float(px):.2f} | entry {float(entry):.2f}{invest} "
                    f"| latent {float(pnl_usd or 0):+.2f} USD ({float(pnl_pct or 0):+.2f}%)"
                    f"{sl_gap}{tp_gap}"
                )
            else:
                notional = p.get("notional_usd")
                lines.append(
                    f"- {tk}: entry {entry} | notional {notional} | px live indisponible"
                )
    else:
        lines.append("")
        lines.append("Positions ouvertes: aucune")
    return "\n".join(lines)


def publish_cockpit_live_state(
    positions: Dict[str, Dict[str, object]],
    *,
    budget_usd: float,
    max_open_positions: int,
    journal_file: str,
    cycle_idx: int = 0,
) -> None:
    """Ecrit cockpit_live.json et sert la page web (si activee)."""
    try:
        from cockpit_web import cockpit_web_enabled, write_cockpit_state
    except ImportError:
        return
    if not cockpit_web_enabled():
        return
    try:
        scan_interval_min = effective_scan_interval_min()
    except ValueError:
        scan_interval_min = 15
    data = build_cockpit_data(
        positions,
        budget_usd=budget_usd,
        max_open_positions=max_open_positions,
        journal_file=journal_file,
        cycle_idx=cycle_idx,
        market_open=is_us_market_open(),
        scan_interval_min=scan_interval_min,
    )
    write_cockpit_state(data)


def refresh_cockpit_live_prices_overlay(*, skip_ibkr: bool = False) -> None:
    """Met a jour les prix live (thread principal) dans cockpit_live.json."""
    try:
        from cockpit_web import cockpit_live_prices_enabled, cockpit_web_enabled, write_cockpit_state
    except ImportError:
        return
    if not cockpit_web_enabled() or not cockpit_live_prices_enabled():
        return
    path = os.getenv("COCKPIT_LIVE_FILE", "cockpit_live.json").strip() or "cockpit_live.json"
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            base = json.load(f)
    except Exception:
        return
    if not isinstance(base, dict):
        return
    enriched = enrich_cockpit_data_live_prices(base, skip_ibkr=skip_ibkr)
    write_cockpit_state(enriched)


def swap_min_conf_edge() -> int:
    try:
        return max(0, int(os.getenv("TRADING_SWAP_MIN_CONF_EDGE", "8")))
    except ValueError:
        return 8


def swap_rank_conf_tolerance() -> int:
    try:
        return max(0, int(os.getenv("TRADING_SWAP_RANK_CONF_TOLERANCE", "4")))
    except ValueError:
        return 4


def swap_min_rank_score() -> float:
    try:
        return float(os.getenv("TRADING_SWAP_MIN_RANK_SCORE", "88"))
    except ValueError:
        return 88.0


def swap_min_new_confidence() -> int:
    try:
        return max(0, min(100, int(os.getenv("TRADING_SWAP_MIN_NEW_CONFIDENCE", "88"))))
    except ValueError:
        return 88


def swap_min_rank_edge() -> float:
    try:
        return max(0.0, float(os.getenv("TRADING_SWAP_MIN_RANK_EDGE", "22")))
    except ValueError:
        return 22.0


def swap_min_momentum_edge() -> float:
    try:
        return max(0.0, float(os.getenv("TRADING_SWAP_MIN_MOMENTUM_EDGE", "1.5")))
    except ValueError:
        return 1.5


def swap_protect_profit_pct() -> float:
    """Ne pas liquider une ligne en gain latent au-dessus de ce seuil (%)."""
    try:
        return float(os.getenv("TRADING_SWAP_PROTECT_PROFIT_PCT", "0.4"))
    except ValueError:
        return 0.4


def swap_min_hold_min() -> float:
    try:
        return max(0.0, float(os.getenv("TRADING_SWAP_MIN_HOLD_MIN", "50")))
    except ValueError:
        return 50.0


def swap_cooldown_min() -> float:
    try:
        return max(0.0, float(os.getenv("TRADING_SWAP_COOLDOWN_MIN", "50")))
    except ValueError:
        return 50.0


def swap_max_per_day() -> int:
    try:
        return max(0, int(os.getenv("TRADING_SWAP_MAX_PER_DAY", "3")))
    except ValueError:
        return 3


def swap_min_mtf_score() -> int:
    """Score MTF 1h minimum pour accepter un swap (trend_score -2..+2)."""
    try:
        return int(os.getenv("SWAP_MIN_MTF_SCORE", "0"))
    except ValueError:
        return 0


def _position_hold_seconds(state: Dict[str, object]) -> Optional[float]:
    ts_raw = str(state.get("entry_ts_utc", "") or "").strip()
    if not ts_raw:
        return None
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC_TZ)
        return max(0.0, (datetime.now(UTC_TZ) - ts.astimezone(UTC_TZ)).total_seconds())
    except (TypeError, ValueError):
        return None


def _position_rank_score(state: Dict[str, object]) -> float:
    try:
        rs = float(state.get("entry_rank_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        rs = 0.0
    if rs > 0:
        return rs
    try:
        return float(int(state.get("entry_confidence", 0) or 0)) * 10.0
    except (TypeError, ValueError):
        return 0.0


def _swaps_today_from_journal(journal_file: str) -> Tuple[int, float]:
    """Nombre de swaps aujourd'hui (UTC) + timestamp du dernier."""
    if not journal_file or not os.path.exists(journal_file):
        return 0, 0.0
    today = datetime.now(UTC_TZ).date()
    count = 0
    last_ts = 0.0
    try:
        with open(journal_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") != "swap_executed":
                    continue
                ts_s = str(ev.get("ts_utc", "") or "").strip()
                if not ts_s:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC_TZ)
                except ValueError:
                    continue
                if ts.astimezone(UTC_TZ).date() != today:
                    continue
                count += 1
                last_ts = max(last_ts, ts.timestamp())
    except OSError:
        return 0, 0.0
    return count, last_ts


def swap_guardrails_block(
    *,
    new_ticker: str,
    new_confidence: int,
    signal_rank_score: Optional[float],
    sell_ticker: str,
    sell_state: Dict[str, object],
    sell_pnl_pct: Optional[float],
    journal_file: str,
    new_mtf_score: Optional[int] = None,
) -> Optional[str]:
    """Retourne un message si le swap doit etre refuse."""
    if int(new_confidence) < swap_min_new_confidence():
        return (
            f"confiance {new_confidence} < minimum swap {swap_min_new_confidence()}"
        )
    if signal_rank_score is not None and float(signal_rank_score) < swap_min_rank_score():
        return f"score priorite {float(signal_rank_score):.0f} < {swap_min_rank_score():.0f}"

    min_mtf = swap_min_mtf_score()
    mtf_enabled = os.getenv("MTF_FILTER_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    if mtf_enabled and min_mtf > -99:
        mtf_score = new_mtf_score
        if mtf_score is None:
            try:
                ht = get_higher_tf_context(
                    new_ticker,
                    period=os.getenv("MTF_PERIOD", "3mo").strip() or "3mo",
                    interval=os.getenv("MTF_INTERVAL", "1h").strip() or "1h",
                )
                mtf_score = int(ht.get("trend_score", 0))
            except Exception:
                mtf_score = 0
        if int(mtf_score) < min_mtf:
            return (
                f"MTF 1h {new_ticker} ({int(mtf_score)}) < seuil swap {min_mtf} "
                f"(structure pas assez haussiere pour une rotation)"
            )

    hold_sec = _position_hold_seconds(sell_state)
    min_hold = swap_min_hold_min() * 60.0
    if hold_sec is not None and min_hold > 0 and hold_sec < min_hold:
        return (
            f"{sell_ticker} detenue depuis {hold_sec / 60.0:.0f} min "
            f"(min {swap_min_hold_min():.0f} min avant rotation)"
        )

    protect = swap_protect_profit_pct()
    if sell_pnl_pct is not None and protect > 0 and float(sell_pnl_pct) >= protect:
        return (
            f"{sell_ticker} en gain latent {float(sell_pnl_pct):+.2f}% "
            f"(protection swap >= {protect:.2f}%)"
        )

    sell_rank = _position_rank_score(sell_state)
    if signal_rank_score is not None:
        rank_edge = swap_min_rank_edge()
        if rank_edge > 0 and float(signal_rank_score) < sell_rank + rank_edge:
            return (
                f"score achat {float(signal_rank_score):.0f} insuffisant vs "
                f"{sell_ticker} ({sell_rank:.0f}, marge +{rank_edge:.0f} requise)"
            )

    mom_edge = swap_min_momentum_edge()
    if mom_edge > 0:
        try:
            buy_mom = quick_momentum_score_for_ticker(new_ticker)
            sell_mom = quick_momentum_score_for_ticker(sell_ticker)
            if buy_mom < sell_mom + mom_edge:
                return (
                    f"momentum {new_ticker} ({buy_mom:.1f}) < {sell_ticker} "
                    f"({sell_mom:.1f}) + {mom_edge:.1f}"
                )
        except Exception:
            pass

    max_day = swap_max_per_day()
    if max_day > 0 and journal_file:
        n_swaps, _ = _swaps_today_from_journal(journal_file)
        if n_swaps >= max_day:
            return f"limite {max_day} swap(s)/jour atteinte ({n_swaps})"

    cool_min = swap_cooldown_min()
    if cool_min > 0 and journal_file:
        _, last_ts = _swaps_today_from_journal(journal_file)
        if last_ts > 0:
            left = (cool_min * 60.0) - (time.time() - last_ts)
            if left > 0:
                return f"cooldown swap ({left / 60.0:.0f} min restantes)"

    return None


def swap_confidence_allows(
    new_confidence: int,
    weakest_confidence: int,
    *,
    signal_rank_score: Optional[float] = None,
    weakest_rank_score: Optional[float] = None,
) -> bool:
    """True si le nouveau signal est assez fort vs la ligne a remplacer."""
    if int(new_confidence) < swap_min_new_confidence():
        return False
    edge = swap_min_conf_edge()
    if int(new_confidence) >= int(weakest_confidence) + edge:
        if signal_rank_score is None or float(signal_rank_score) >= swap_min_rank_score():
            if (
                signal_rank_score is None
                or weakest_rank_score is None
                or float(signal_rank_score) >= float(weakest_rank_score) + swap_min_rank_edge()
            ):
                return True
    if signal_rank_score is None:
        return False
    if float(signal_rank_score) < swap_min_rank_score():
        return False
    if (
        weakest_rank_score is not None
        and float(signal_rank_score) < float(weakest_rank_score) + swap_min_rank_edge()
    ):
        return False
    return int(new_confidence) + swap_rank_conf_tolerance() >= int(weakest_confidence)


def swap_auto_execute_enabled() -> bool:
    """Rotation immediate IBKR sans confirmation Telegram."""
    return ibkr_auto_execute_enabled() and os.getenv("TRADING_SWAP_AUTO_EXECUTE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def buy_execute_immediate_enabled() -> bool:
    """Execute ACHETER des validation (intraday), pas apres scan complet."""
    return os.getenv("TRADING_BUY_EXECUTE_IMMEDIATE", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def swap_on_budget_enabled() -> bool:
    return os.getenv("TRADING_SWAP_ON_BUDGET", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def ibkr_prep_budget_insufficient(err: Optional[str]) -> bool:
    if not err:
        return False
    low = str(err).lower()
    return "insuffisant" in low and ("budget" in low or "cash restant" in low or "action" in low)


def ibkr_prep_swap_budget_eligible(
    err: Optional[str],
    *,
    requested_notional_usd: float,
    remaining_usd: float,
    price_usd: float,
) -> bool:
    if not swap_on_budget_enabled():
        return False
    ticket = buy_min_ticket_usd(requested_notional_usd, price_usd)
    if buy_needs_budget_swap(ticket, remaining_usd, price_usd):
        return True
    return ibkr_prep_budget_insufficient(err)


def buy_min_ticket_usd(requested_notional_usd: float, price_usd: float) -> float:
    """Ticket minimum IBKR (1 action entiere) pour evaluer cash vs achat direct."""
    req = max(0.0, float(requested_notional_usd))
    px = max(0.0, float(price_usd))
    if ibkr_auto_execute_enabled() and px > 0:
        return max(req, px)
    return req


def buy_needs_budget_swap(
    requested_notional_usd: float,
    remaining_usd: float,
    price_usd: float,
) -> bool:
    """True si le cash libre ne couvre pas le ticket minimum (typ. 1 action IBKR)."""
    ticket = buy_min_ticket_usd(requested_notional_usd, price_usd)
    return ticket > float(remaining_usd) + 1e-6


def _buy_signal_taken(positions: Dict[str, Dict[str, object]], ticker: str) -> bool:
    st = normalize_position_state(positions.get(normalize_ticker(ticker), {}))
    return bool(st.get("in_position")) or bool(st.get("pending_buy"))


def pick_swap_candidate(positions: Dict[str, Dict[str, object]], exclude_ticker: str) -> Optional[Dict[str, object]]:
    """Retourne la position la moins convaincante a remplacer (pertes d'abord, gains proteges)."""
    protect = swap_protect_profit_pct()
    min_hold_sec = swap_min_hold_min() * 60.0
    candidates: List[Dict[str, object]] = []
    for tk, raw in positions.items():
        if tk == exclude_ticker:
            continue
        st = normalize_position_state(raw)
        if not bool(st.get("in_position")) or bool(st.get("pending_sell")):
            continue
        hold_sec = _position_hold_seconds(st)
        if hold_sec is not None and min_hold_sec > 0 and hold_sec < min_hold_sec:
            continue
        entry_px = float(st.get("entry_price_usd", 0.0) or 0.0)
        pnl_pct: Optional[float] = None
        if entry_px > 0:
            try:
                live_px = get_reference_price_usd(tk)
            except Exception:
                live_px = None
            if live_px is not None and live_px > 0:
                pnl_pct = (float(live_px) - entry_px) / entry_px * 100.0
        if pnl_pct is not None and protect > 0 and float(pnl_pct) >= protect:
            continue
        candidates.append(
            {
                "ticker": tk,
                "entry_confidence": int(st.get("entry_confidence", 0) or 0),
                "entry_rank_score": _position_rank_score(st),
                "entry_notional_usd": float(st.get("entry_notional_usd", 0.0) or 0.0),
                "pnl_pct": pnl_pct,
                "hold_min": (hold_sec / 60.0) if hold_sec is not None else None,
            }
        )
    if not candidates:
        return None
    # Pire P/L d'abord, puis confiance/rank les plus faibles.
    candidates.sort(
        key=lambda c: (
            float(c["pnl_pct"]) if c["pnl_pct"] is not None else 0.0,
            float(c["entry_rank_score"]),
            int(c["entry_confidence"]),
            float(c["entry_notional_usd"]),
        )
    )
    return candidates[0]


def _swap_pending_file() -> str:
    return (
        os.getenv("SWAP_PENDING_FILE", SWAP_PENDING_FILE_DEFAULT).strip()
        or SWAP_PENDING_FILE_DEFAULT
    )


def load_pending_swap() -> Optional[Dict[str, object]]:
    data = load_json_dict(_swap_pending_file())
    pending = data.get("pending")
    return pending if isinstance(pending, dict) else None


def save_pending_swap(pending: Dict[str, object]) -> None:
    save_json_dict(_swap_pending_file(), {"pending": pending})


def clear_pending_swap() -> None:
    save_json_dict(_swap_pending_file(), {})


def execute_confirmed_swap(
    *,
    pending: Dict[str, object],
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    bot_token: str,
    chat_id: str,
) -> Tuple[bool, str]:
    """
    Execute la rotation: vente IBKR de sell_ticker puis achat buy_ticker.
    Retourne (ok, message_utilisateur).
    """
    if not ibkr_auto_execute_enabled():
        return False, "IBKR auto desactive — active IBKR_AUTO_EXECUTE=1 pour le switch auto."
    sell_tk = normalize_ticker(str(pending.get("sell_ticker", "")))
    buy_tk = normalize_ticker(str(pending.get("buy_ticker", "")))
    if not sell_tk or not buy_tk:
        return False, "Switch invalide (tickers manquants)."
    try:
        buy_notional = float(pending.get("buy_notional_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        buy_notional = 0.0
    if buy_notional <= 0:
        return False, f"Switch invalide: montant achat {buy_tk} absent."
    buy_conf = int(pending.get("buy_confidence", 0) or 0)
    sell_conf = int(pending.get("sell_confidence", 0) or 0)
    buy_rank = pending.get("buy_rank_score")
    sell_rank = pending.get("sell_rank_score")
    try:
        buy_rank_f = float(buy_rank) if buy_rank is not None else None
    except (TypeError, ValueError):
        buy_rank_f = None
    try:
        sell_rank_f = float(sell_rank) if sell_rank is not None else None
    except (TypeError, ValueError):
        sell_rank_f = None
    if not swap_confidence_allows(
        buy_conf,
        sell_conf,
        signal_rank_score=buy_rank_f,
        weakest_rank_score=sell_rank_f,
    ):
        edge = swap_min_conf_edge()
        return (
            False,
            f"Switch expire: confiance {buy_tk} ({buy_conf}) insuffisante vs {sell_tk} ({sell_conf}, edge +{edge}).",
        )

    sell_st = normalize_position_state(positions.get(sell_tk, {}))
    if not bool(sell_st.get("in_position")):
        return False, f"Switch impossible: plus de position ouverte sur {sell_tk}."
    buy_st = normalize_position_state(positions.get(buy_tk, {}))
    if bool(buy_st.get("in_position")):
        return False, f"Switch impossible: {buy_tk} deja en position."

    # La rotation libere sell_tk : on evalue le cap sur le portefeuille APRES la vente.
    positions_after_sell = {
        t: s for t, s in positions.items() if normalize_ticker(t) != sell_tk
    }
    theme_block, theme_note = theme_exposure_blocks(positions_after_sell, buy_tk)
    if theme_block:
        return False, f"Switch bloque sur {buy_tk}: {theme_note}."

    journal_file = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
    journal_on = os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}

    sl_raw = pending.get("buy_stop_loss")
    tp_raw = pending.get("buy_take_profit")
    stop_loss = float(sl_raw) if sl_raw is not None else None
    take_profit = float(tp_raw) if tp_raw is not None else None
    justif = str(pending.get("justification", "") or "").strip()
    yf_period_sw = os.getenv("YF_PERIOD", "5d").strip()
    yf_interval_sw = os.getenv("YF_INTERVAL", "15m").strip()
    analysis_sw = analyze_ticker(buy_tk, period=yf_period_sw, interval=yf_interval_sw)
    if analysis_sw is not None and stop_loss is not None:
        try:
            buy_min_rr_sw = float(os.getenv("BUY_MIN_RR", "1.2"))
        except ValueError:
            buy_min_rr_sw = 1.2
        buy_rr_on = os.getenv("BUY_REQUIRE_RR", "1").strip().lower() in {"1", "true", "yes", "y"}
        sl_adj, tp_adj, sl_note = adjust_sl_tp_atr_floor(
            entry_price=float(analysis_sw["current_price"]),
            stop_loss=stop_loss,
            take_profit=take_profit,
            atr=float(analysis_sw.get("atr", 0) or 0),
            ticker=buy_tk,
            min_rr=buy_min_rr_sw if buy_rr_on else None,
            notional_usd=buy_notional,
        )
        if sl_note:
            print(f"[SWAP] {buy_tk}: {sl_note}")
        stop_loss, take_profit = sl_adj, tp_adj

    sold = attempt_ibkr_sell(
        ticker=sell_tk,
        positions=positions,
        position_file=position_file,
        state=sell_st,
        journal_file=journal_file,
        journal_enabled=journal_on,
        bot_token=bot_token,
        chat_id=chat_id,
        reason="swap_rotation",
        exit_reason="swap_rotation",
    )
    if not sold:
        return False, f"Vente IBKR echouee pour {sell_tk} — switch annule."

    buy_st2 = normalize_position_state(positions.get(buy_tk, {}))
    swap_signal_px = (
        float(analysis_sw["current_price"])
        if analysis_sw is not None and float(analysis_sw.get("current_price", 0) or 0) > 0
        else None
    )
    bought = attempt_ibkr_buy(
        ticker=buy_tk,
        notional_usd=buy_notional,
        positions=positions,
        position_file=position_file,
        state=buy_st2,
        confidence=buy_conf,
        take_profit=take_profit,
        stop_loss=stop_loss,
        journal_file=journal_file,
        journal_enabled=journal_on,
        bot_token=bot_token,
        chat_id=chat_id,
        current_price_hint=swap_signal_px,
        justification=justif or f"Switch auto: rotation {sell_tk} -> {buy_tk}",
    )
    if not bought:
        return (
            False,
            f"Vente OK ({sell_tk}) mais achat IBKR echoue pour {buy_tk}. "
            f"Slot libere — tu peux /confirm_buy {buy_tk} {buy_notional:.0f} si besoin.",
        )

    if buy_rank_f is not None and buy_rank_f > 0:
        st_buy = normalize_position_state(positions.get(buy_tk, {}))
        st_buy["entry_rank_score"] = buy_rank_f
        positions[buy_tk] = st_buy
        save_positions(position_file, positions)

    clear_pending_swap()
    if journal_on and journal_file:
        append_journal_event(
            journal_file,
            {
                "event": "swap_executed",
                "sell_ticker": sell_tk,
                "buy_ticker": buy_tk,
                "buy_notional_usd": buy_notional,
                "buy_confidence": buy_conf,
                "sell_confidence": sell_conf,
                "reason": str(pending.get("reason", "")),
            },
        )
    return True, (
        f"SWITCH EXECUTE\n"
        f"Vente: {sell_tk}\n"
        f"Achat: {buy_tk} (~{buy_notional:.2f} USD)\n"
        f"Confiance: {buy_conf}/100 (remplace {sell_tk} @ {sell_conf}/100)"
    )


def handle_swap_telegram_reply(
    *,
    cmd: str,
    text_lower: str,
    parts: List[str],
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    bot_token: str,
    chat_id: str,
) -> bool:
    """Traite /swap_oui, /swap_non ou reponse 'oui'/'yes' si un switch est en attente."""
    swap_cmds_confirm = {"/swap_oui", "/swap_ok", "/swap_yes", "/confirm_swap", "/swap_confirm"}
    swap_cmds_cancel = {"/swap_non", "/swap_no", "/swap_cancel", "/swap_annuler"}
    is_confirm_word = len(parts) == 1 and text_lower in {"oui", "yes", "ok", "y"}
    if cmd in swap_cmds_cancel:
        if load_pending_swap():
            clear_pending_swap()
            send_telegram_alert(bot_token, chat_id, "Switch en attente annule.")
        else:
            send_telegram_alert(bot_token, chat_id, "Aucun switch en attente.")
        return True
    if cmd not in swap_cmds_confirm and not is_confirm_word:
        return False

    pending = load_pending_swap()
    if not pending:
        send_telegram_alert(
            bot_token,
            chat_id,
            "Aucune opportunite SWITCH en attente. Attends un message OPPORTUNITE SWITCH du bot.",
        )
        return True

    try:
        ttl_sec = float(os.getenv("TRADING_SWAP_PENDING_TTL_SEC", "7200"))
    except ValueError:
        ttl_sec = 7200.0
    created_ts = float(pending.get("created_ts", 0.0) or 0.0)
    if created_ts > 0 and (time.time() - created_ts) > max(300.0, ttl_sec):
        clear_pending_swap()
        send_telegram_alert(bot_token, chat_id, "Switch expire (delai depasse). Attends une nouvelle proposition.")
        return True

    ok, msg = execute_confirmed_swap(
        pending=pending,
        positions=positions,
        position_file=position_file,
        bot_token=bot_token,
        chat_id=chat_id,
    )
    if not ok:
        send_telegram_alert(bot_token, chat_id, f"SWITCH non execute\n{msg}")
    else:
        send_telegram_alert(bot_token, chat_id, msg)
    return True


def maybe_send_swap_opportunity(
    *,
    new_ticker: str,
    new_confidence: int,
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    bot_token: str,
    chat_id: str,
    reason: str,
    slots_used: int,
    max_open_positions: int,
    remaining_usd: float = 0.0,
    requested_notional_usd: float = 0.0,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    justification: str = "",
    signal_rank_score: Optional[float] = None,
    new_mtf_score: Optional[int] = None,
) -> bool:
    """
    Rotation portefeuille: liquider la ligne la plus faible pour liberer un slot / du cash.
    TRADING_SWAP_AUTO_EXECUTE=1 -> vente+achat IBKR immediat (sans /swap_oui).
    Sinon -> proposition Telegram + attente confirmation.
    reason: 'slots_full' | 'budget'
    """
    reason_key = (reason or "").strip().lower()
    if reason_key == "slots_full":
        enabled = os.getenv("TRADING_SWAP_ON_FULL_SLOTS", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
    elif reason_key == "budget":
        enabled = os.getenv("TRADING_SWAP_ON_BUDGET", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
    else:
        return False
    if not enabled:
        return False
    buy_blocked, mins_left = is_buy_blocked_near_close()
    if buy_blocked:
        print(
            f"[{new_ticker}] Switch non propose ({reason_key}): fin de seance "
            f"(~{int(mins_left)} min avant cloture NY, entrees bloquees)."
        )
        return False
    journal_file = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT

    weakest = pick_swap_candidate(positions, new_ticker)
    if weakest is None:
        print(
            f"[{new_ticker}] Switch non propose ({reason_key}): aucune ligne eligible "
            f"(gain protege, hold min {swap_min_hold_min():.0f} min, ou slots)."
        )
        return False
    weakest_conf = int(weakest["entry_confidence"])
    weakest_ticker = str(weakest["ticker"])
    weakest_rank = float(weakest.get("entry_rank_score", 0.0) or 0.0)
    weakest_pnl = weakest.get("pnl_pct")
    sell_st = normalize_position_state(positions.get(weakest_ticker, {}))

    block = swap_guardrails_block(
        new_ticker=new_ticker,
        new_confidence=new_confidence,
        signal_rank_score=signal_rank_score,
        sell_ticker=weakest_ticker,
        sell_state=sell_st,
        sell_pnl_pct=float(weakest_pnl) if weakest_pnl is not None else None,
        journal_file=journal_file,
        new_mtf_score=new_mtf_score,
    )
    if block:
        print(f"[{new_ticker}] Switch non propose ({reason_key}): {block}.")
        return False

    if not swap_confidence_allows(
        new_confidence,
        weakest_conf,
        signal_rank_score=signal_rank_score,
        weakest_rank_score=weakest_rank,
    ):
        edge = swap_min_conf_edge()
        rank_note = (
            f", score {float(signal_rank_score):.0f} vs {weakest_rank:.0f} (+{swap_min_rank_edge():.0f} requis)"
            if signal_rank_score is not None
            else ""
        )
        print(
            f"[{new_ticker}] Switch non propose ({reason_key}): confiance {new_confidence} "
            f"< {weakest_ticker} ({weakest_conf}) + {edge}{rank_note}."
        )
        return False
    weakest_size = float(weakest["entry_notional_usd"])
    pnl_hint = f", P/L latent {float(weakest_pnl):+.1f}%" if weakest_pnl is not None else ""
    if reason_key == "slots_full":
        ctx_line = f"Slots pleins: {slots_used}/{max_open_positions} (cash restant {remaining_usd:.2f} USD)."
    else:
        ctx_line = (
            f"Budget insuffisant: reste {remaining_usd:.2f} USD "
            f"(besoin {requested_notional_usd:.2f} USD)."
        )
    notional_hint = requested_notional_usd
    if notional_hint <= 0:
        notional_hint = max(80.0, remaining_usd / max(1, max_open_positions))
    buy_tk = normalize_ticker(new_ticker)
    cash_after_sell = max(0.0, float(remaining_usd) + weakest_size)
    prepared_notional, ibkr_prep_err = ibkr_auto_buy_prepare_notional(
        float(notional_hint),
        price_usd=get_reference_price_usd(buy_tk),
        remaining_usd=cash_after_sell,
        ticker=buy_tk,
    )
    if ibkr_prep_err:
        print(f"[{new_ticker}] Switch ignore ({reason_key}): {ibkr_prep_err}")
        return False
    if prepared_notional > 0:
        notional_hint = prepared_notional

    pending: Dict[str, object] = {
        "sell_ticker": weakest_ticker,
        "sell_confidence": weakest_conf,
        "sell_rank_score": weakest_rank,
        "buy_ticker": buy_tk,
        "buy_confidence": int(new_confidence),
        "buy_rank_score": float(signal_rank_score) if signal_rank_score is not None else None,
        "buy_notional_usd": float(notional_hint),
        "buy_stop_loss": float(stop_loss) if stop_loss is not None else None,
        "buy_take_profit": float(take_profit) if take_profit is not None else None,
        "justification": (justification or "").strip(),
        "reason": reason_key,
        "created_ts": time.time(),
    }

    if swap_auto_execute_enabled():
        ok, msg = execute_confirmed_swap(
            pending=pending,
            positions=positions,
            position_file=position_file,
            bot_token=bot_token,
            chat_id=chat_id,
        )
        notify = (
            f"SWITCH AUTO ({reason_key})\n"
            f"{ctx_line}\n"
            f"Rotation: {weakest_ticker} -> {buy_tk} (~{notional_hint:.0f} USD)\n"
            f"{msg}"
        )
        if not ok:
            notify = f"SWITCH AUTO ECHEC ({reason_key})\n{msg}"
        print(
            f"[{new_ticker}] Switch auto ({reason_key}): "
            f"{weakest_ticker} -> {buy_tk} — {'OK' if ok else 'ECHEC'}."
        )
        if bot_token and chat_id:
            try:
                send_telegram_alert(bot_token, chat_id, notify)
            except Exception as exc:
                print(f"[{new_ticker}] Echec alerte switch auto ({reason_key}): {exc}")
        return ok

    if not bot_token or not chat_id:
        print(f"[{new_ticker}] Switch non propose ({reason_key}): Telegram non configure.")
        return False
    auto_on = ibkr_auto_execute_enabled() and os.getenv("TRADING_SWAP_AUTO_ON_CONFIRM", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    swap_msg = (
        f"OPPORTUNITE SWITCH - {new_ticker}\n"
        f"Confiance nouvelle idee: {new_confidence}/100\n"
        f"{ctx_line}\n"
        f"Position a remplacer: {weakest_ticker} "
        f"(confiance {weakest_conf}/100, taille {weakest_size:.2f} USD{pnl_hint})\n"
        f"Plan: vendre {weakest_ticker} puis acheter {new_ticker} (~{notional_hint:.0f} USD).\n"
    )
    if auto_on:
        swap_msg += (
            f"\nReponds OUI ou /swap_oui pour executer automatiquement chez IBKR.\n"
            f"/swap_non pour refuser."
        )
    else:
        swap_msg += (
            f"\nManuel: /confirm_sell {weakest_ticker} PRIX_SORTIE "
            f"puis /confirm_buy {new_ticker} {notional_hint:.0f}"
        )
    try:
        save_pending_swap(pending)
        send_telegram_alert(bot_token, chat_id, swap_msg)
        print(
            f"[{new_ticker}] Suggestion switch ({reason_key}): "
            f"{weakest_ticker} -> {new_ticker} (conf {new_confidence} vs {weakest_conf})."
        )
        return True
    except Exception as exc:
        print(f"[{new_ticker}] Echec alerte switch ({reason_key}): {exc}")
        return False


def price_suspect_vs_entry(current_price: float, entry_price: float) -> bool:
    """True si le prix live semble incoherent avec l'entree (donnee scan corrompue)."""
    if current_price <= 0 or entry_price <= 0:
        return False
    try:
        low_r = float(os.getenv("RECONCILE_ENTRY_RATIO_LOW", "0.72"))
        high_r = float(os.getenv("RECONCILE_ENTRY_RATIO_HIGH", "1.35"))
    except ValueError:
        low_r, high_r = 0.72, 1.35
    ratio = current_price / entry_price
    return ratio < low_r or ratio > high_r


def check_take_profit_alert(
    *,
    ticker: str,
    current_price: float,
    state: Dict[str, object],
    bot_token: str,
    chat_id: str,
    position_file: str,
    positions: Dict[str, Dict[str, object]],
) -> None:
    """
    Envoie une alerte si le take profit est atteint (sans executer la vente).
    """
    if not bool(state.get("in_position", False)):
        return
    if bool(state.get("pending_sell", False)):
        return
    entry_px = float(state.get("entry_price_usd", 0.0) or 0.0)
    if price_suspect_vs_entry(current_price, entry_px):
        print(
            f"[{ticker}] TP ignore: prix {current_price:.2f} suspect vs entree {entry_px:.2f} USD."
        )
        return
    tp = state.get("take_profit")
    tp_val = float(tp) if isinstance(tp, (int, float)) else None
    if tp_val is None:
        return
    if current_price < tp_val:
        return
    if bool(state.get("ibkr_sl_tp_active")):
        return
    if ibkr_auto_execute_enabled():
        journal_file = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
        journal_on = os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
        if attempt_ibkr_sell(
            ticker=ticker,
            positions=positions,
            position_file=position_file,
            journal_file=journal_file,
            journal_enabled=journal_on,
            bot_token=bot_token,
            chat_id=chat_id,
            reason="take_profit",
            exit_reason="take_profit",
        ):
            return
    msg = (
        f"TAKE PROFIT ATTEINT - {ticker}\n"
        f"Prix actuel: {current_price:.2f} USD\n"
        f"Take profit: {tp_val:.2f} USD\n"
        "Action suggeree: prendre profits / confirmer vente si executee."
    )
    send_telegram_alert(bot_token, chat_id, msg)
    state["pending_sell"] = True
    positions[ticker] = state
    save_positions(position_file, positions)


def check_stop_loss_alert(
    *,
    ticker: str,
    current_price: float,
    state: Dict[str, object],
    bot_token: str,
    chat_id: str,
    position_file: str,
    positions: Dict[str, Dict[str, object]],
) -> None:
    """
    Envoie une alerte si le prix passe sous le stop loss enregistre.
    Ne declenche qu'une seule fois (marque pending_sell).
    """
    if not bool(state.get("in_position", False)):
        return
    if bool(state.get("pending_sell", False)):
        return
    entry_px = float(state.get("entry_price_usd", 0.0) or 0.0)
    if price_suspect_vs_entry(current_price, entry_px):
        print(
            f"[{ticker}] SL ignore: prix {current_price:.2f} suspect vs entree {entry_px:.2f} USD."
        )
        return
    sl_raw = state.get("stop_loss")
    if sl_raw is None:
        return
    try:
        sl_val = float(sl_raw)
    except (TypeError, ValueError):
        return
    if sl_val <= 0:
        return
    if current_price > sl_val:
        return
    entry_px = float(state.get("entry_price_usd", 0.0) or 0.0)
    pnl_txt = ""
    if entry_px > 0:
        pnl_pct = ((current_price - entry_px) / entry_px) * 100.0
        pnl_txt = f"\nP/L latent: {pnl_pct:+.1f}% (entree {entry_px:.2f} USD)"
    if bool(state.get("ibkr_sl_tp_active")):
        return
    if ibkr_auto_execute_enabled():
        journal_file = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
        journal_on = os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
        if attempt_ibkr_sell(
            ticker=ticker,
            positions=positions,
            position_file=position_file,
            journal_file=journal_file,
            journal_enabled=journal_on,
            bot_token=bot_token,
            chat_id=chat_id,
            reason="stop_loss",
            exit_reason="stop_loss",
        ):
            return
    msg = (
        f"STOP LOSS ATTEINT - {ticker}\n"
        f"Prix actuel: {current_price:.2f} USD\n"
        f"Stop loss: {sl_val:.2f} USD{pnl_txt}\n"
        "Action suggeree: sortir / confirmer vente si executee.\n"
        f"/confirm_sell {ticker} PRIX_SORTIE"
        "\n(precis releve: /confirm_sell TICKER PRIX_SORTIE CAPITAL_INVESTI PRIX_MOYEN_ENTREE)"
    )
    send_telegram_alert(bot_token, chat_id, msg)
    state["pending_sell"] = True
    state["last_sell_alert_ts"] = time.time()
    positions[ticker] = state
    save_positions(position_file, positions)


def is_us_market_open(now: Optional[datetime] = None) -> bool:
    """Retourne True si le marche US est ouvert (lun-ven, 9h30-16h00 EST/EDT)."""
    if now is None:
        now = datetime.now(MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)

    if now.weekday() >= 5:  # 5 = samedi, 6 = dimanche
        return False

    current_minutes = now.hour * 60 + now.minute
    open_minutes = MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MINUTE
    close_minutes = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MINUTE
    return open_minutes <= current_minutes <= close_minutes


def minutes_until_us_market_close(now: Optional[datetime] = None) -> float:
    """Minutes restantes avant la cloture reguliere US (16h00 NY). Negatif si marche ferme."""
    if now is None:
        now = datetime.now(MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)
    close_today = now.replace(hour=MARKET_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    return (close_today - now).total_seconds() / 60.0


def block_buy_min_before_close_minutes() -> float:
    """Minutes avant cloture NY sans nouvelle entree (0 = desactive)."""
    raw = os.getenv("TRADING_BLOCK_BUY_MIN_BEFORE_CLOSE", "").strip()
    if not raw:
        intraday_on = os.getenv("TRADING_INTRADAY_FLAT_ENABLED", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
        if intraday_on:
            try:
                return max(0.0, float(os.getenv("TRADING_INTRADAY_FLAT_MIN_BEFORE_CLOSE", "40")))
            except ValueError:
                return 40.0
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def is_buy_blocked_near_close(now: Optional[datetime] = None) -> Tuple[bool, float]:
    """True si nouvelles entrees / switch bloques en fin de seance. Retourne (bloque, mins_restantes)."""
    mins_left = minutes_until_us_market_close(now)
    block_min = block_buy_min_before_close_minutes()
    if block_min <= 0 or not is_us_market_open(now):
        return False, mins_left
    if 0 <= mins_left <= block_min:
        return True, mins_left
    return False, mins_left


def intraday_runner_min_pnl_pct() -> float:
    try:
        return max(0.0, float(os.getenv("TRADING_RUNNER_MIN_PNL_PCT", "3.0")))
    except ValueError:
        return 3.0


def intraday_runner_max_overnight() -> int:
    try:
        return max(0, int(os.getenv("TRADING_RUNNER_MAX_OVERNIGHT", "2")))
    except ValueError:
        return 2


def intraday_flat_min_before_close_minutes() -> float:
    try:
        return max(5.0, float(os.getenv("TRADING_INTRADAY_FLAT_MIN_BEFORE_CLOSE", "30")))
    except ValueError:
        return 30.0


def intraday_flat_poll_sec() -> float:
    """Intervalle de surveillance fin de seance (plus court = reaction plus rapide)."""
    try:
        return max(0.5, min(5.0, float(os.getenv("TRADING_INTRADAY_FLAT_POLL_SEC", "1"))))
    except ValueError:
        return 1.0


def intraday_flat_retry_sec() -> float:
    """Delai avant nouvel essai si vente flat IBKR echoue."""
    try:
        return max(0.5, min(30.0, float(os.getenv("TRADING_INTRADAY_FLAT_RETRY_SEC", "2"))))
    except ValueError:
        return 2.0


def _intraday_flat_min_sell_price_usd(entry_px: float, min_profit_pct: float) -> float:
    """Prix plancher: en dessous, le flat intraday ne vend pas."""
    return round(float(entry_px) * (1.0 + float(min_profit_pct) / 100.0), 2)


def is_intraday_flat_window(now: Optional[datetime] = None) -> bool:
    if os.getenv("TRADING_INTRADAY_FLAT_ENABLED", "0").strip().lower() not in {
        "1",
        "true",
        "yes",
        "y",
    }:
        return False
    if not is_us_market_open(now):
        return False
    mins_left = minutes_until_us_market_close(now)
    return 0 <= mins_left <= intraday_flat_min_before_close_minutes()


def intraday_flat_catchup_max_min_after_close() -> float:
    """Minutes apres 16h00 NY pour tenter un flat de rattrapage (extended hours)."""
    try:
        return max(0.0, float(os.getenv("TRADING_INTRADAY_FLAT_CATCHUP_MAX_MIN_AFTER_CLOSE", "240")))
    except ValueError:
        return 240.0


def is_intraday_flat_catchup_window(now: Optional[datetime] = None) -> bool:
    """
    Rattrapage flat apres cloture reguliere (meme jour ouvré), tant que les
    extended hours restent utilisables. Sert quand le bot rate la fenetre 15:30-16:00.
    """
    if os.getenv("TRADING_INTRADAY_FLAT_ENABLED", "0").strip().lower() not in {
        "1",
        "true",
        "yes",
        "y",
    }:
        return False
    if now is None:
        now = datetime.now(MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)
    if now.weekday() >= 5 or is_us_market_open(now):
        return False
    current_minutes = now.hour * 60 + now.minute
    open_minutes = MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MINUTE
    close_minutes = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MINUTE
    if current_minutes < open_minutes:
        return False
    mins_since_close = float(current_minutes - close_minutes)
    max_after = intraday_flat_catchup_max_min_after_close()
    return 0 < mins_since_close <= max_after


def _intraday_flat_exit_price_usd(
    ticker: str,
    *,
    allow_reference_fallback: bool = False,
) -> Optional[float]:
    """Prix conservateur (bid IBKR) pour mesurer le P/L avant flat fin de seance."""
    if ibkr_auto_execute_enabled():
        try:
            from ibkr_execution import fetch_ibkr_bid_price_usd

            bid = fetch_ibkr_bid_price_usd(ticker, skip_cache=True)
            if bid is not None and bid > 0:
                return float(bid)
        except ImportError:
            pass
        if allow_reference_fallback:
            return get_reference_price_usd(ticker)
        # Pas de bid fiable: flat reporte (poll 1s) plutot que decider sur un
        # prix de reference potentiellement differe ou egal au close de la veille.
        return None
    return get_reference_price_usd(ticker)


def _position_pnl_pct(entry_px: float, mark_px: Optional[float]) -> Optional[float]:
    if entry_px <= 0 or mark_px is None or float(mark_px) <= 0:
        return None
    return ((float(mark_px) - entry_px) / entry_px) * 100.0


def _intraday_flat_in_small_gain_zone(
    pnl_pct: float,
    *,
    min_profit_pct: float,
    runner_min_pnl: float,
    runners_enabled: bool = True,
) -> bool:
    """Eligible flat fin de seance: gain >= min_profit (et < runner_min si runners actifs)."""
    if float(pnl_pct) < float(min_profit_pct):
        return False
    if not runners_enabled:
        return True
    return float(pnl_pct) < float(runner_min_pnl)


def poll_intraday_flat_if_due() -> None:
    """Appel le plus souvent possible en fin de seance (entre les cycles) + catchup post-cloture."""
    poll_max_hold_if_due()
    if not (is_intraday_flat_window() or is_intraday_flat_catchup_window()):
        return
    pos_file = os.getenv("POSITION_FILE", POSITION_FILE_DEFAULT).strip() or POSITION_FILE_DEFAULT
    jfile = (
        os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip()
        or TRADE_JOURNAL_FILE_DEFAULT
    )
    j_on = os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    tg_t = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_c = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    flat_positions = load_positions(pos_file)
    maybe_intraday_flat_positions(
        flat_positions,
        pos_file,
        journal_file=jfile,
        journal_enabled=j_on,
        bot_token=tg_t,
        chat_id=tg_c,
    )


def _pick_overnight_runner_tickers(
    candidates: List[Tuple[str, float]],
    *,
    runner_min_pnl_pct: float,
    max_runners: int,
) -> set:
    """Retourne les tickers a conserver overnight (runners: P/L >= seuil, top N)."""
    eligible = [(tk, pnl) for tk, pnl in candidates if pnl >= runner_min_pnl_pct]
    eligible.sort(key=lambda x: x[1], reverse=True)
    if max_runners <= 0:
        return set()
    return {tk for tk, _ in eligible[:max_runners]}


def maybe_intraday_flat_positions(
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    *,
    journal_file: str,
    journal_enabled: bool,
    bot_token: str,
    chat_id: str,
) -> int:
    """
    Flat fin de seance (option 3):
    - P/L < min_profit: conserve overnight (pertes / sous seuil frais, stop IBKR)
    - P/L >= min_profit: flat (securise les gains)
    - Si runners actifs (max_runners > 0): P/L >= runner_min peut etre conserve overnight
    """
    enabled = os.getenv("TRADING_INTRADAY_FLAT_ENABLED", "0").strip().lower() in {"1", "true", "yes", "y"}
    if not enabled:
        return 0
    min_before = intraday_flat_min_before_close_minutes()
    mins_left = minutes_until_us_market_close()
    catchup = False
    if is_us_market_open():
        if mins_left > min_before or mins_left < 0:
            return 0
    else:
        # Scan hors seance: rattrapage flat si positions encore ouvertes (extended hours).
        if not is_intraday_flat_catchup_window():
            return 0
        catchup = True
        open_count = 0
        for raw in positions.values():
            st0 = normalize_position_state(raw)
            if bool(st0.get("in_position")) and not bool(st0.get("pending_sell")):
                open_count += 1
        if open_count <= 0:
            return 0
        print(
            f"[INTRADAY] Rattrapage flat post-cloture "
            f"({open_count} position(s), fenetre +{intraday_flat_catchup_max_min_after_close():.0f} min apres 16h NY)."
        )

    skip_if_loss = os.getenv("TRADING_INTRADAY_FLAT_SKIP_IF_LOSS", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    try:
        min_profit_pct = float(os.getenv("TRADING_INTRADAY_FLAT_MIN_PROFIT_PCT", "0.0"))
    except ValueError:
        min_profit_pct = 0.0
    runner_min_pnl = intraday_runner_min_pnl_pct()
    max_runners = intraday_runner_max_overnight()
    runners_enabled = max_runners > 0
    alert_loss_hold = os.getenv("TRADING_INTRADAY_FLAT_ALERT_LOSS_HOLD", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    alert_runner_hold = os.getenv("TRADING_RUNNER_ALERT_HOLD", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }

    rows: List[Dict[str, object]] = []
    for tk, raw in list(positions.items()):
        st = normalize_position_state(raw)
        if not bool(st.get("in_position")) or bool(st.get("pending_sell")):
            continue
        ticker = normalize_ticker(tk)
        entry_px = float(st.get("entry_price_usd", 0.0) or 0.0)
        px_now = _intraday_flat_exit_price_usd(
            ticker,
            allow_reference_fallback=catchup,
        )
        pnl_pct = _position_pnl_pct(entry_px, px_now)
        rows.append(
            {
                "ticker": ticker,
                "state": st,
                "entry_px": entry_px,
                "px_now": float(px_now) if px_now is not None else None,
                "pnl_pct": pnl_pct,
            }
        )

    pnl_for_runners = [
        (str(r["ticker"]), float(r["pnl_pct"]))
        for r in rows
        if r.get("pnl_pct") is not None
    ]
    runner_keep = (
        _pick_overnight_runner_tickers(
            pnl_for_runners,
            runner_min_pnl_pct=runner_min_pnl,
            max_runners=max_runners,
        )
        if runners_enabled
        else set()
    )

    closed = 0
    for row in rows:
        ticker = str(row["ticker"])
        st: Dict[str, object] = row["state"]  # type: ignore[assignment]
        entry_px = float(row["entry_px"])
        px_now = row["px_now"]
        pnl_pct = row["pnl_pct"]

        if skip_if_loss and pnl_pct is not None and float(pnl_pct) < min_profit_pct:
            if alert_loss_hold and not bool(st.get("intraday_loss_hold_notified")):
                st["intraday_loss_hold_notified"] = True
                st.pop("overnight_runner", None)
                positions[ticker] = st
                save_positions(position_file, positions)
                if bot_token and chat_id:
                    if catchup:
                        hold_line = "Rattrapage post-cloture: pas de vente forcee en perte."
                    else:
                        hold_line = (
                            f"Cloture dans ~{int(mins_left)} min: pas de vente forcee en perte."
                        )
                    send_telegram_alert(
                        bot_token,
                        chat_id,
                        (
                            f"POSITION CONSERVEE OVERNIGHT — {ticker}\n"
                            f"P/L latent: {float(pnl_pct):+.2f}% (entree {entry_px:.2f} | ref {float(px_now):.2f})\n"
                            f"{hold_line}\n"
                            f"Stop/TP IBKR restent actifs."
                        ),
                    )
                print(
                    f"[INTRADAY] {ticker} conserve overnight (P/L {float(pnl_pct):+.2f}% < flat {min_profit_pct:.2f}%)."
                )
            continue

        if pnl_pct is None:
            print(f"[INTRADAY] {ticker} ignore flat: P/L latent indisponible.")
            continue

        pnl_f = float(pnl_pct)
        if pnl_f < min_profit_pct:
            continue

        if runners_enabled and pnl_f >= runner_min_pnl and ticker in runner_keep:
            st["overnight_runner"] = True
            st.pop("intraday_loss_hold_notified", None)
            positions[ticker] = st
            save_positions(position_file, positions)
            if alert_runner_hold and not bool(st.get("intraday_runner_hold_notified")):
                st["intraday_runner_hold_notified"] = True
                positions[ticker] = st
                save_positions(position_file, positions)
                if bot_token and chat_id:
                    send_telegram_alert(
                        bot_token,
                        chat_id,
                        (
                            f"RUNNER OVERNIGHT — {ticker}\n"
                            f"P/L latent: {pnl_f:+.2f}% (seuil runner >= {runner_min_pnl:.1f}%)\n"
                            f"Conserve pour laisser le TP IBKR travailler (soir / lendemain).\n"
                            f"Max {max_runners} runner(s) overnight autorise(s)."
                        ),
                    )
                print(
                    f"[INTRADAY] {ticker} runner overnight conserve "
                    f"(P/L {pnl_f:+.2f}% >= {runner_min_pnl:.1f}%)."
                )
            continue

        if not _intraday_flat_in_small_gain_zone(
            pnl_f,
            min_profit_pct=min_profit_pct,
            runner_min_pnl=runner_min_pnl,
            runners_enabled=runners_enabled,
        ):
            continue

        exit_px = _intraday_flat_exit_price_usd(
            ticker,
            allow_reference_fallback=catchup,
        )
        pnl_verify = _position_pnl_pct(entry_px, exit_px)
        if pnl_verify is None:
            print(
                f"[INTRADAY] {ticker} flat reporte: "
                f"{'prix' if catchup else 'bid IBKR'} indisponible."
            )
            continue
        if not _intraday_flat_in_small_gain_zone(
            float(pnl_verify),
            min_profit_pct=min_profit_pct,
            runner_min_pnl=runner_min_pnl,
            runners_enabled=runners_enabled,
        ):
            if runners_enabled:
                print(
                    f"[INTRADAY] {ticker} flat annule: P/L bid {float(pnl_verify):+.2f}% "
                    f"hors zone [{min_profit_pct:.1f}%, {runner_min_pnl:.1f}%)."
                )
            else:
                print(
                    f"[INTRADAY] {ticker} flat annule: P/L bid {float(pnl_verify):+.2f}% "
                    f"sous seuil flat {min_profit_pct:.1f}%."
                )
            continue
        pnl_f = float(pnl_verify)
        px_now = exit_px
        min_sell_px = _intraday_flat_min_sell_price_usd(entry_px, min_profit_pct)
        if float(px_now or 0.0) < min_sell_px:
            print(
                f"[INTRADAY] {ticker} flat annule: bid {float(px_now):.2f} < "
                f"plancher gain {min_sell_px:.2f} (+{min_profit_pct:.1f}%)."
            )
            continue

        flat_retry = intraday_flat_retry_sec()
        last_flat_ts = float(st.get("last_intraday_flat_alert_ts", 0.0) or 0.0)
        if time.time() - last_flat_ts < flat_retry:
            continue

        sold = False
        ibkr_require_open = os.getenv("IBKR_REQUIRE_MARKET_OPEN", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
        # Catch-up post-cloture: autorise la vente extended hours meme si REQUIRE_MARKET_OPEN=1.
        if ibkr_auto_execute_enabled() and (
            catchup or (not ibkr_require_open) or is_us_market_open()
        ):
            sold = attempt_ibkr_sell(
                ticker=ticker,
                positions=positions,
                position_file=position_file,
                state=st,
                journal_file=journal_file,
                journal_enabled=journal_enabled,
                bot_token=bot_token,
                chat_id=chat_id,
                exit_reason="intraday_flat_catchup" if catchup else "intraday_flat",
                min_limit_price=min_sell_px,
                force_outside_rth=catchup,
            )
        if sold:
            st = normalize_position_state(positions.get(ticker, {}))
            st.pop("last_intraday_flat_alert_ts", None)
            positions[ticker] = st
            save_positions(position_file, positions)
        else:
            st["last_intraday_flat_alert_ts"] = time.time()
            st.pop("intraday_loss_hold_notified", None)
            st.pop("intraday_runner_hold_notified", None)
            st.pop("overnight_runner", None)
            positions[ticker] = st
            save_positions(position_file, positions)
        pnl_hint = f" (P/L bid {pnl_f:+.2f}%, ref {float(px_now):.2f})"
        zone_txt = (
            f">= {min_profit_pct:.1f}%"
            if not runners_enabled
            else f"[{min_profit_pct:.1f}%, {runner_min_pnl:.1f}%)"
        )
        print(
            f"[INTRADAY] {ticker} flat gain {zone_txt} — "
            f"{'OK IBKR' if sold else 'echec / alerte'}"
            f"{' (rattrapage)' if catchup else ''}."
        )
        if not sold:
            st2 = normalize_position_state(positions.get(ticker, {}))
            st2["pending_sell"] = True
            st2["last_sell_alert_ts"] = time.time()
            positions[ticker] = st2
            save_positions(position_file, positions)
            if bot_token and chat_id:
                if catchup:
                    timing_line = (
                        "Rattrapage post-cloture NY (extended hours): gain a securiser."
                    )
                else:
                    timing_line = (
                        f"~{int(mins_left)} min avant 16h00 NY: gain securise (option 3)."
                    )
                send_telegram_alert(
                    bot_token,
                    chat_id,
                    (
                        f"CLOTURE INTRADAY — {ticker}{pnl_hint}\n"
                        f"{timing_line}\n"
                        f"/confirm_sell {ticker} PRIX_SORTIE"
                    ),
                )
        if sold:
            closed += 1
    return closed


# ---------------------------------------------------------------------------
# Strategie "semaine" (recherche du 2026-09-29, docs/reports/recherche-quant-2026-09-29.md)
# - sortie forcee apres N seances detenues (TRADING_MAX_HOLD_SESSIONS)
# - signaux sur barres CLOTUREES uniquement (SCAN_/MTF_COMPLETED_BARS_ONLY)
# - garde-fou PDT (RISK_PDT_GUARD_ENABLED)
# Tout est desactive par defaut: seul un profil qui pose les cles change de comportement.
# ---------------------------------------------------------------------------

# Jours de fermeture NYSE (hors week-ends). A completer via TRADING_EXTRA_MARKET_HOLIDAYS.
NYSE_HOLIDAYS = {
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def _env_on(key: str, default: str = "0") -> bool:
    return os.getenv(key, default).strip().lower() in {"1", "true", "yes", "y"}


def market_holidays() -> List[str]:
    extra = [x.strip() for x in os.getenv("TRADING_EXTRA_MARKET_HOLIDAYS", "").split(",") if x.strip()]
    return sorted(NYSE_HOLIDAYS | set(extra))


def max_hold_sessions() -> int:
    """0 = desactive. N = vendre en fin de N-ieme seance apres le jour d'entree."""
    try:
        return max(0, int(os.getenv("TRADING_MAX_HOLD_SESSIONS", "0")))
    except ValueError:
        return 0


def max_hold_min_before_close() -> float:
    try:
        return max(5.0, float(os.getenv("TRADING_MAX_HOLD_MIN_BEFORE_CLOSE", "30")))
    except ValueError:
        return 30.0


def sessions_held(entry_ts_utc: str, now: Optional[datetime] = None) -> Optional[int]:
    """
    Seances de bourse ecoulees depuis le jour d'entree, jour courant inclus,
    jour d'entree exclu. Achat lundi -> vendredi = 4, lundi suivant = 5.
    Meme convention que le backtest (research_engine: dayidx(jour) - dayidx(entree)).
    """
    import numpy as np

    entry_ny = _parse_journal_ts_to_ny(str(entry_ts_utc or ""))
    if entry_ny is None:
        return None
    now_ny = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
    return int(np.busday_count(entry_ny.date().isoformat(), now_ny.date().isoformat(),
                               holidays=market_holidays()))


def is_max_hold_window(now: Optional[datetime] = None) -> bool:
    if max_hold_sessions() <= 0 or not is_us_market_open(now):
        return False
    mins_left = minutes_until_us_market_close(now)
    return 0 <= mins_left <= max_hold_min_before_close()


def maybe_max_hold_exit_positions(
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    *,
    journal_file: str,
    journal_enabled: bool,
    bot_token: str,
    chat_id: str,
    now: Optional[datetime] = None,
) -> int:
    """
    Vend au marche, dans les dernieres minutes de seance, les lignes detenues depuis
    >= TRADING_MAX_HOLD_SESSIONS seances, quel que soit le P/L. attempt_ibkr_sell annule
    le bracket SL/TP avant de vendre et le restaure si la vente echoue.
    En cas d'echec on retente dans la fenetre (pas de pending_sell collant).
    """
    n_max = max_hold_sessions()
    if n_max <= 0 or not is_max_hold_window(now):
        return 0
    try:
        retry_sec = max(5.0, float(os.getenv("TRADING_MAX_HOLD_RETRY_SEC", "20")))
    except ValueError:
        retry_sec = 20.0
    closed = 0
    for tk, raw in list(positions.items()):
        st = normalize_position_state(raw)
        if not bool(st.get("in_position")) or bool(st.get("pending_sell")):
            continue
        ticker = normalize_ticker(tk)
        held = sessions_held(str(st.get("entry_ts_utc", "") or ""), now)
        if held is None:
            if not bool(st.get("max_hold_no_ts_notified")):
                st["max_hold_no_ts_notified"] = True
                positions[ticker] = st
                save_positions(position_file, positions)
                print(f"[MAX_HOLD] {ticker}: entry_ts_utc absent, duree de detention inconnue — ignore.")
            continue
        if held < n_max:
            continue
        last_try = float(st.get("last_max_hold_try_ts", 0.0) or 0.0)
        if time.time() - last_try < retry_sec:
            continue
        sold = False
        if ibkr_auto_execute_enabled():
            sold = attempt_ibkr_sell(
                ticker=ticker,
                positions=positions,
                position_file=position_file,
                state=st,
                journal_file=journal_file,
                journal_enabled=journal_enabled,
                bot_token=bot_token,
                chat_id=chat_id,
                exit_reason="max_hold_sessions",
                min_limit_price=None,
            )
        print(f"[MAX_HOLD] {ticker}: {held} seance(s) >= {n_max} — "
              f"{'vendu' if sold else 'vente non executee, nouvel essai'}.")
        if sold:
            closed += 1
            continue
        st = normalize_position_state(positions.get(ticker, st))
        st["last_max_hold_try_ts"] = time.time()
        tries = int(st.get("max_hold_tries", 0) or 0) + 1
        st["max_hold_tries"] = tries
        positions[ticker] = st
        save_positions(position_file, positions)
        if tries == 3 and bot_token and chat_id:
            send_telegram_alert(
                bot_token,
                chat_id,
                (
                    f"SORTIE DUREE MAX NON EXECUTEE — {ticker}\n"
                    f"Detenue {held} seances (max {n_max}). 3 essais echoues.\n"
                    f"SL/TP IBKR restent actifs. /confirm_sell {ticker} PRIX_SORTIE"
                ),
            )
    return closed


def poll_max_hold_if_due() -> None:
    if not is_max_hold_window():
        return
    pos_file = os.getenv("POSITION_FILE", POSITION_FILE_DEFAULT).strip() or POSITION_FILE_DEFAULT
    jfile = (
        os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip()
        or TRADE_JOURNAL_FILE_DEFAULT
    )
    maybe_max_hold_exit_positions(
        load_positions(pos_file),
        pos_file,
        journal_file=jfile,
        journal_enabled=_env_on("JOURNAL_ENABLED", "1"),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    )


def _interval_minutes(interval: str) -> Optional[int]:
    iv = (interval or "").strip().lower()
    try:
        if iv.endswith("m") and not iv.endswith("mo"):
            return int(iv[:-1])
        if iv.endswith("h"):
            return int(iv[:-1]) * 60
    except ValueError:
        return None
    return None


def drop_forming_bar(df: pd.DataFrame, interval: str, now: Optional[datetime] = None) -> pd.DataFrame:
    """
    Retire la derniere barre si elle n'est pas encore cloturee. Le backtest ne
    decide que sur des barres terminees (entree a l'ouverture de la suivante);
    decider sur une barre en formation est un autre signal, non teste.
    Intraday: barre [debut, debut+intervalle) bornee a 16h00. Journalier: la
    bougie du jour tant que la seance n'est pas finie.
    """
    if df is None or len(df) == 0:
        return df
    now_ny = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
    ts = pd.Timestamp(df.index[-1])
    ts = ts.tz_localize(MARKET_TZ) if ts.tzinfo is None else ts.tz_convert(MARKET_TZ)
    iv = (interval or "").strip().lower()
    if iv == "1d":
        closed = now_ny.hour * 60 + now_ny.minute >= MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MINUTE
        if ts.date() == now_ny.date() and not closed:
            return df.iloc[:-1]
        return df
    minutes = _interval_minutes(iv)
    if minutes is None:
        return df
    end = ts + pd.Timedelta(minutes=minutes)
    session_close = ts.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    if end > session_close >= ts:
        end = session_close
    if end > pd.Timestamp(now_ny):
        return df.iloc[:-1]
    return df


def count_recent_day_trades(journal_file: str, now: Optional[datetime] = None, window_days: int = 5) -> int:
    """
    Day trades (achat et vente du meme titre la meme seance) sur les `window_days`
    derniers jours ouvres, jour courant inclus. Vente = evenement trade_closed; son
    achat = dernier fill BUY du meme ticker avant la vente.
    """
    import numpy as np

    now_ny = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
    hol = market_holidays()
    start = np.busday_offset(now_ny.date().isoformat(), -(window_days - 1), roll="backward", holidays=hol)
    start_d = pd.Timestamp(start).date()
    buys: Dict[str, List[datetime]] = {}
    closes: List[Tuple[str, datetime]] = []
    try:
        with open(journal_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_journal_ts_to_ny(str(ev.get("ts_utc", "")))
                if ts is None:
                    continue
                tk = normalize_ticker(str(ev.get("ticker", "")))
                if ev.get("event") == "ibkr_order_filled" and str(ev.get("side", "")).upper() == "BUY":
                    buys.setdefault(tk, []).append(ts)
                elif ev.get("event") == "trade_closed" and ts.date() >= start_d:
                    closes.append((tk, ts))
    except FileNotFoundError:
        return 0
    n = 0
    for tk, ts in closes:
        prior = [b for b in buys.get(tk, []) if b <= ts]
        if prior and max(prior).date() == ts.date():
            n += 1
    return n


def pdt_guard_blocks_buys(journal_file: str, now: Optional[datetime] = None) -> Tuple[bool, int, int]:
    """
    Regle PDT (compte sur marge < 25 000 $): 4 day trades sur 5 jours ouvres = compte
    restreint. Des que le compteur atteint la limite, plus aucune nouvelle entree (une
    entree du jour pourrait devenir le day trade de trop si son stop part le jour meme).
    Retourne (bloque, compteur, limite).
    """
    if not _env_on("RISK_PDT_GUARD_ENABLED", "0"):
        return False, 0, 0
    try:
        limit = max(1, int(os.getenv("RISK_PDT_MAX_DAY_TRADES", "3")))
    except ValueError:
        limit = 3
    n = count_recent_day_trades(journal_file, now)
    return n >= limit, n, limit


def strategy_equity_path(base_budget_usd: float, journal_file: str, since_txt: str) -> Tuple[float, float]:
    """(equity actuelle, plus haut) = budget + PnL des trades clos depuis `since_txt` (AAAA-MM-JJ)."""
    since_d = None
    if since_txt:
        try:
            since_d = datetime.strptime(since_txt, "%Y-%m-%d").date()
        except ValueError:
            since_d = None
    rows = []
    for ev in load_trade_closed_events(journal_file):
        ts = _parse_journal_ts_to_ny(str(ev.get("ts_utc", "")))
        if ts is None or (since_d is not None and ts.date() < since_d):
            continue
        try:
            rows.append((ts, float(ev.get("pnl_usd", 0.0) or 0.0)))
        except (TypeError, ValueError):
            continue
    eq = peak = float(base_budget_usd)
    for _, pnl in sorted(rows, key=lambda x: x[0]):
        eq += pnl
        peak = max(peak, eq)
    return eq, peak


def line_step_factor(journal_file: Optional[str] = None) -> Tuple[float, int, float]:
    """
    Reinvestissement par PALIERS (TRADING_LINE_STEP_PCT, ex 25). La ligne vaut
    TRADING_TARGET_LINE_USD x (1+pas)^k, k = paliers franchis par le compte depuis le
    lancement de la strategie (TRADING_LINE_STEP_START_DATE, sinon RISK_MAX_DD_START_DATE):
      mode "updown" (defaut): k suit le compte ACTUEL -> 1250$ des 1250$, 800$ sous 1000$
      mode "up": k suit le PLUS HAUT du compte, ne redescend jamais
    Meme regle que le backtest (research_engine.simulate step_pct/step_mode), rapport
    section 14. Retourne (facteur, k, equity de reference). 0 / absent = desactive.
    """
    try:
        step = float(os.getenv("TRADING_LINE_STEP_PCT", "0") or 0) / 100.0
    except ValueError:
        step = 0.0
    if step <= 0:
        return 1.0, 0, 0.0
    mode = os.getenv("TRADING_LINE_STEP_MODE", "updown").strip().lower()
    try:
        base = float(os.getenv("TRADING_BUDGET_USD", "1000") or 1000)
    except ValueError:
        base = 1000.0
    jf = journal_file or (os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip()
                          or TRADE_JOURNAL_FILE_DEFAULT)
    since = (os.getenv("TRADING_LINE_STEP_START_DATE", "").strip()
             or os.getenv("RISK_MAX_DD_START_DATE", "").strip())
    eq, peak = strategy_equity_path(base, jf, since)
    ref = peak if mode == "up" else eq
    k = math.floor(math.log(max(ref, 1e-9) / base) / math.log(1.0 + step) + 1e-9)
    if mode == "up":
        k = max(0, k)
    k = max(-4, min(12, k))   # bornes de securite: 0.41x a 14.6x
    return (1.0 + step) ** k, k, ref


_EQUITY_BREAKER_NOTIFIED = False


def notify_equity_breaker_once(bot_token: str, chat_id: str, dd_pct: float, limit_pct: float) -> None:
    """Une seule alerte Telegram par lancement du bot."""
    global _EQUITY_BREAKER_NOTIFIED
    if _EQUITY_BREAKER_NOTIFIED or not (bot_token and chat_id):
        return
    _EQUITY_BREAKER_NOTIFIED = True
    send_telegram_alert(
        bot_token,
        chat_id,
        (
            f"COUPE-CIRCUIT ACTIF\n"
            f"Drawdown realise {dd_pct:.1f}% (limite -{limit_pct:.0f}%).\n"
            f"Plus aucune nouvelle entree. Les positions ouvertes gardent SL/TP/duree max.\n"
            f"Pire que le pire drawdown du backtest: verifier le regime avant de relancer."
        ),
    )


def equity_drawdown_blocks_buys(base_budget_usd: float, journal_file: str) -> Tuple[bool, float, float]:
    """
    Coupe-circuit: plus aucune nouvelle entree si l'equity REALISEE (budget + PnL des
    trades clos depuis RISK_MAX_DD_START_DATE) est a plus de RISK_MAX_DRAWDOWN_PCT sous
    son plus haut. Le seuil doit etre au-dela du pire drawdown du backtest: l'atteindre
    veut dire que le marche ne ressemble plus a celui du backtest. Sans nouvelle entree
    l'equity ne remonte pas: la reprise est manuelle (relever le seuil ou la date).
    Retourne (bloque, drawdown_pct, limite_pct).
    """
    try:
        limit = float(os.getenv("RISK_MAX_DRAWDOWN_PCT", "0") or 0)
    except ValueError:
        limit = 0.0
    if limit <= 0:
        return False, 0.0, 0.0
    eq, peak = strategy_equity_path(max(1e-9, float(base_budget_usd)), journal_file,
                                    os.getenv("RISK_MAX_DD_START_DATE", "").strip())
    dd_pct = (eq - peak) / peak * 100.0
    return dd_pct <= -limit, dd_pct, limit


def get_next_us_market_open(now: Optional[datetime] = None) -> datetime:
    """
    Retourne la prochaine ouverture theorique du marche US (lun-ven 9h30 NY).
    Note: ne tient pas compte des jours feries.
    """
    if now is None:
        now = datetime.now(MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)

    open_today = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)

    if now.weekday() >= 5:
        days_ahead = (7 - now.weekday()) % 7
        if days_ahead <= 0:
            days_ahead = 1
        target = now + timedelta(days=days_ahead)
        return target.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)

    if now < open_today:
        return open_today

    target = now + timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)


def _reference_price_ibkr_first_enabled() -> bool:
    if os.getenv("REFERENCE_PRICE_IBKR_FIRST", "1").strip().lower() not in {"1", "true", "yes", "y"}:
        return False
    ibkr_on = os.getenv("IBKR_ENABLED", "0").strip().lower() in {"1", "true", "yes", "y"}
    if ibkr_on:
        return True
    try:
        from ibkr_execution import is_ibkr_auto_enabled

        return is_ibkr_auto_enabled()
    except ImportError:
        return False


def _try_ibkr_reference_price_usd(ticker: str) -> Optional[float]:
    try:
        from ibkr_execution import fetch_ibkr_reference_price_usd

        return fetch_ibkr_reference_price_usd(ticker)
    except Exception:
        return None


def get_reference_price_usd(ticker: str, mode: str = "intraday", *, skip_ibkr: bool = False) -> Optional[float]:
    """
    Prix de reference best-effort (hybride si REFERENCE_PRICE_IBKR_FIRST=1).
    - IBKR en priorite quand Gateway actif, sinon Yahoo/Finnhub
    - skip_ibkr=True: force la source externe (evite recursion depuis ibkr_execution)
  """
    if not skip_ibkr and _reference_price_ibkr_first_enabled():
        ibkr_px = _try_ibkr_reference_price_usd(ticker)
        if ibkr_px is not None and ibkr_px > 0:
            return float(ibkr_px)
    return _get_external_reference_price_usd(ticker, mode)


def _get_external_reference_price_usd(ticker: str, mode: str = "intraday") -> Optional[float]:
    """
    Prix de reference externe (Yahoo / provider configure).
    - mode intraday: live/1m puis fallback daily
    - mode daily_close: close daily uniquement
    """
    try:
        tkr = yf.Ticker(ticker)
        use_daily_only = (mode or "").strip().lower() in {"daily", "daily_close", "close"}

        if not use_daily_only:
            # 1) Tentative live Yahoo strictement pre/post market.
            try:
                fi = getattr(tkr, "fast_info", None) or {}
                live_candidates = (
                    fi.get("postMarketPrice"),
                    fi.get("preMarketPrice"),
                )
                for live_px in live_candidates:
                    if live_px is None:
                        continue
                    p = float(live_px)
                    if p > 0:
                        return p
            except Exception:
                pass

            # 1bis) Fallback info detaillee Yahoo (souvent plus proche du prix broker hors-seance).
            try:
                info = getattr(tkr, "info", None) or {}
                # Choisir le prix de la SEANCE EN COURS: avant l'ouverture, Yahoo garde
                # encore l'after-hours de la veille (postMarketPrice) a cote du pre-market
                # du jour; le lire en premier donnait un prix perime.
                market_state = str(info.get("marketState", "") or "").upper()
                if market_state.startswith("PRE"):
                    keys = ("preMarketPrice", "currentPrice", "regularMarketPrice")
                elif market_state == "REGULAR":
                    keys = ("currentPrice", "regularMarketPrice")
                elif market_state.startswith("POST") or market_state == "CLOSED":
                    keys = ("postMarketPrice", "currentPrice", "regularMarketPrice")
                else:
                    keys = ("postMarketPrice", "preMarketPrice", "currentPrice", "regularMarketPrice")
                live_candidates = tuple(info.get(k) for k in keys)
                for live_px in live_candidates:
                    if live_px is None:
                        continue
                    p = float(live_px)
                    if p > 0:
                        return p
            except Exception:
                pass

            # 2) Derniere bougie 1m Yahoo avec pre/post inclus.
            # period=1d intraday est souvent vide cote Yahoo -> 5d.
            try:
                df_ext = tkr.history(period="5d", interval="1m", prepost=True, auto_adjust=False)
                if isinstance(df_ext, pd.DataFrame) and (not df_ext.empty) and ("Close" in df_ext.columns):
                    close_ext = df_ext["Close"]
                    if isinstance(close_ext, pd.DataFrame):
                        close_ext = close_ext.iloc[:, 0]
                    close_ext = close_ext.dropna()
                    if not close_ext.empty:
                        p = float(close_ext.iloc[-1])
                        if p > 0:
                            return p
            except Exception:
                pass

            # 3) Fallback intraday: derniere bougie 1m.
            try:
                df_intra = fetch_price_data(ticker, period="1d", interval="1m")
                df_intra = normalize_ohlc_columns(df_intra, ticker)
                if "Close" in df_intra.columns:
                    close_intra = df_intra["Close"]
                    if isinstance(close_intra, pd.DataFrame):
                        close_intra = close_intra.iloc[:, 0]
                    close_intra = close_intra.dropna()
                    if not close_intra.empty:
                        p = float(close_intra.iloc[-1])
                        if p > 0:
                            return p
            except Exception:
                pass

            # 4) Dernier fallback live si aucune cote pre/post dispo.
            try:
                fi = getattr(tkr, "fast_info", None) or {}
                live_candidates = (
                    fi.get("lastPrice"),
                    fi.get("regularMarketPrice"),
                )
                for live_px in live_candidates:
                    if live_px is None:
                        continue
                    p = float(live_px)
                    if p > 0:
                        return p
            except Exception:
                pass

        # 5) Dernier fallback: close daily.
        df = fetch_price_data(ticker, period="5d", interval="1d")
        df = normalize_ohlc_columns(df, ticker)
        if "Close" not in df.columns:
            return None
        close_series = df["Close"]
        if isinstance(close_series, pd.DataFrame):
            close_series = close_series.iloc[:, 0]
        close_series = close_series.dropna()
        if close_series.empty:
            return None
        p = float(close_series.iloc[-1])
        return p if p > 0 else None
    except Exception:
        return None


def compute_day_realized_pnl_usd(journal_file: str, now_ny: datetime) -> float:
    """Somme du PnL realise sur la journee NY depuis le journal."""
    total = 0.0
    target_day = now_ny.date()
    for event in load_trade_closed_events(journal_file):
        ts_txt = str(event.get("ts_utc", "")).strip()
        if not ts_txt:
            continue
        try:
            ts_utc = datetime.fromisoformat(ts_txt.replace("Z", "+00:00"))
            ts_ny = ts_utc.astimezone(MARKET_TZ)
        except Exception:
            continue
        if ts_ny.date() != target_day:
            continue
        try:
            total += float(event.get("pnl_usd", 0.0) or 0.0)
        except Exception:
            continue
    return total


def risk_new_buys_blocked(
    *,
    budget_usd: float,
    journal_file: str,
    now_ny: datetime,
) -> Tuple[bool, float, float]:
    """
    Bloque les nouveaux ACHETER si drawdown realise jour > seuil.
    Retourne (blocked, pnl_day_usd, dd_limit_usd).
    """
    enabled = os.getenv("RISK_DD_GUARD_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        dd_limit_pct = float(os.getenv("RISK_DAILY_DD_LIMIT_PCT", "3.5"))
    except ValueError:
        dd_limit_pct = 3.5
    dd_limit_pct = max(0.1, dd_limit_pct)
    dd_limit_usd = abs(float(budget_usd) * (dd_limit_pct / 100.0))
    pnl_day = compute_day_realized_pnl_usd(journal_file, now_ny=now_ny)
    blocked = bool(enabled and (pnl_day <= -dd_limit_usd))
    return blocked, pnl_day, dd_limit_usd


def _parse_journal_ts_to_ny(ts_txt: str) -> Optional[datetime]:
    ts_txt = (ts_txt or "").strip()
    if not ts_txt:
        return None
    try:
        ts_utc = datetime.fromisoformat(ts_txt.replace("Z", "+00:00"))
        return ts_utc.astimezone(MARKET_TZ)
    except Exception:
        return None


def trade_closed_dedupe_key(ev: Dict[str, object]) -> str:
    """Cle stable pour un trade unique (ignore source / double sync)."""
    tk = normalize_ticker(str(ev.get("ticker", "")))
    try:
        entry_px = round(float(ev.get("entry_price_usd", 0.0) or 0.0), 4)
        exit_px = round(float(ev.get("exit_price_usd", 0.0) or 0.0), 4)
        size_usd = round(float(ev.get("size_usd", 0.0) or 0.0), 2)
    except Exception:
        return f"{tk}|invalid"
    return f"{tk}|{entry_px}|{exit_px}|{size_usd}"


def dedupe_trade_closed_events(events: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Garde le premier trade_closed par trade (ticker + entree/sortie/notionnel)."""
    seen: set[str] = set()
    out: List[Dict[str, object]] = []
    for ev in events:
        key = trade_closed_dedupe_key(ev)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def load_trade_closed_events_raw(journal_file: str) -> List[Dict[str, object]]:
    """Lit le journal JSONL et retourne tous les evenements trade_closed (brut)."""
    out: List[Dict[str, object]] = []
    try:
        with open(journal_file, "r", encoding="utf-8") as f:
            for line in f:
                txt = line.strip()
                if not txt:
                    continue
                try:
                    event = json.loads(txt)
                except Exception:
                    continue
                if event.get("event") != "trade_closed":
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("void"):
                    continue
                out.append(event)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return out


def load_trade_closed_events(journal_file: str) -> List[Dict[str, object]]:
    """Lit le journal JSONL et retourne les trade_closed uniques."""
    return dedupe_trade_closed_events(load_trade_closed_events_raw(journal_file))


def journal_trade_closed_exists(journal_file: str, closed_evt: Dict[str, object]) -> bool:
    """True si ce trade_closed est deja present dans le journal."""
    if not journal_file:
        return False
    key = trade_closed_dedupe_key(closed_evt)
    for ev in load_trade_closed_events_raw(journal_file):
        if trade_closed_dedupe_key(ev) == key:
            return True
    return False


def compute_total_realized_pnl_usd(journal_file: str) -> float:
    """Somme du PnL realise sur tout l'historique trade_closed."""
    total = 0.0
    for ev in load_trade_closed_events(journal_file):
        try:
            total += float(ev.get("pnl_usd", 0.0) or 0.0)
        except Exception:
            continue
    return total


def compute_effective_budget_usd(base_budget_usd: float, journal_file: str) -> float:
    """
    Budget pilotable = budget initial + PnL realise cumule.
    Permet de reinjecter automatiquement les gains (ou absorber les pertes).
    """
    realized_total = compute_total_realized_pnl_usd(journal_file)
    return max(0.0, float(base_budget_usd) + float(realized_total))


def compute_objective_day_index(now_ny: datetime) -> int:
    """
    Jour courant de la campagne TRADING_OBJECTIVE_START_DATE.
    Retourne >= 1.
    """
    start_day_env = os.getenv("TRADING_OBJECTIVE_START_DATE", "").strip()
    if start_day_env:
        try:
            start_day = datetime.strptime(start_day_env, "%Y-%m-%d").date()
            return max(1, (now_ny.date() - start_day).days + 1)
        except ValueError:
            pass
    return 1


def _collect_closed_trade_rows(
    events: List[Dict[str, object]],
    *,
    since_ny: Optional[datetime] = None,
) -> List[Dict[str, object]]:
    """
    Une ligne par evenement trade_closed (trade unique), avec date/heure NY pour affichage.
    """
    rows: List[Dict[str, object]] = []
    for ev in events:
        ts_ny = _parse_journal_ts_to_ny(str(ev.get("ts_utc", "")))
        if ts_ny is None:
            continue
        if since_ny is not None and ts_ny < since_ny:
            continue
        try:
            pnl = float(ev.get("pnl_usd", 0.0) or 0.0)
        except Exception:
            continue
        pnl_pct = None
        try:
            pnl_pct = float(ev.get("pnl_pct", 0.0) or 0.0)
        except Exception:
            pnl_pct = None
        if pnl_pct is None:
            try:
                entry_px = float(ev.get("entry_price_usd", 0.0) or 0.0)
                exit_px = float(ev.get("exit_price_usd", 0.0) or 0.0)
                if entry_px > 0 and exit_px > 0:
                    pnl_pct = ((exit_px / entry_px) - 1.0) * 100.0
                else:
                    pnl_pct = 0.0
            except Exception:
                pnl_pct = 0.0
        try:
            size_usd = float(ev.get("size_usd", 0.0) or 0.0)
        except Exception:
            size_usd = 0.0
        try:
            entry_px = float(ev.get("entry_price_usd", 0.0) or 0.0)
        except Exception:
            entry_px = 0.0
        try:
            exit_px = float(ev.get("exit_price_usd", 0.0) or 0.0)
        except Exception:
            exit_px = 0.0
        tk = normalize_ticker(str(ev.get("ticker", "")))
        rows.append(
            {
                "ticker": tk,
                "pnl_usd": pnl,
                "pnl_pct": float(pnl_pct),
                "size_usd": max(0.0, size_usd),
                "entry_price_usd": max(0.0, entry_px),
                "exit_price_usd": max(0.0, exit_px),
                "ts_utc": str(ev.get("ts_utc", "")),
                "date_ny": ts_ny.strftime("%Y-%m-%d"),
                "time_ny": ts_ny.strftime("%H:%M"),
            }
        )
    return rows


def _summarize_closed_trades(
    events: List[Dict[str, object]],
    *,
    since_ny: Optional[datetime] = None,
) -> Dict[str, object]:
    """
    Agrege PnL, win rate, moyennes sur une liste filtree par date (since_ny inclus).
    """
    detail = _collect_closed_trade_rows(events, since_ny=since_ny)
    filtered: List[Tuple[float, str, float, float]] = [
        (float(r["pnl_usd"]), str(r["ticker"]), float(r["pnl_pct"]), float(r["size_usd"])) for r in detail
    ]

    pnls = [p for p, _, _, _ in filtered]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_pcts = [pct for p, _, pct, _ in filtered if p > 0]
    loss_pcts = [pct for p, _, pct, _ in filtered if p < 0]
    n = len(pnls)
    total = sum(pnls)
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    avg_win_pct = (sum(win_pcts) / len(win_pcts)) if win_pcts else 0.0
    avg_loss_pct = (sum(loss_pcts) / len(loss_pcts)) if loss_pcts else 0.0

    by_ticker: Dict[str, float] = {}
    by_ticker_size: Dict[str, float] = {}
    for pnl, tk, _, size_usd in filtered:
        if not tk:
            continue
        by_ticker[tk] = by_ticker.get(tk, 0.0) + pnl
        by_ticker_size[tk] = by_ticker_size.get(tk, 0.0) + size_usd

    by_ticker_pct: Dict[str, float] = {}
    for tk, pnl_usd in by_ticker.items():
        size_usd = float(by_ticker_size.get(tk, 0.0) or 0.0)
        if size_usd > 0:
            by_ticker_pct[tk] = (pnl_usd / size_usd) * 100.0
        else:
            by_ticker_pct[tk] = 0.0

    return {
        "n": n,
        "total_pnl": total,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "by_ticker": by_ticker,
        "by_ticker_pct": by_ticker_pct,
    }


def build_daily_progress_report(
    journal_file: str,
    positions: Dict[str, Dict[str, object]],
    *,
    base_budget_usd: float,
    objective_days: int,
    now_ny: Optional[datetime] = None,
) -> str:
    """
    Rapport quotidien de progression: jour N / objectif, deploye, libre, PnL.
    """
    if now_ny is None:
        now_ny = datetime.now(MARKET_TZ)
    else:
        now_ny = now_ny.astimezone(MARKET_TZ)

    events = load_trade_closed_events(journal_file)
    start_day_env = os.getenv("TRADING_OBJECTIVE_START_DATE", "").strip()
    start_day: Optional[date] = None
    if start_day_env:
        try:
            start_day = datetime.strptime(start_day_env, "%Y-%m-%d").date()
        except ValueError:
            start_day = None
    if start_day is None and events:
        first_ts = _parse_journal_ts_to_ny(str(events[0].get("ts_utc", "")))
        if first_ts is not None:
            start_day = first_ts.date()
    if start_day is None:
        start_day = now_ny.date()

    day_index = max(1, (now_ny.date() - start_day).days + 1)
    objective_days = max(1, int(objective_days))

    pnl_day = compute_day_realized_pnl_usd(journal_file, now_ny=now_ny)
    pnl_total = compute_total_realized_pnl_usd(journal_file)
    effective_budget = compute_effective_budget_usd(base_budget_usd, journal_file)
    deployed = deployed_budget_usd(positions, effective_budget)
    free_cash = effective_budget - deployed

    rows_all = _collect_closed_trade_rows(events, since_ny=None)
    rows_today = [r for r in rows_all if str(r.get("date_ny", "")) == now_ny.strftime("%Y-%m-%d")]
    rows_today = sorted(rows_today, key=lambda r: str(r.get("ts_utc", "")))
    closed_count = len(rows_today)
    try:
        daily_list_max = max(1, min(25, int(os.getenv("DAILY_REPORT_TRADES_MAX", "10"))))
    except ValueError:
        daily_list_max = 10

    lines = [
        "RAPPORT QUOTIDIEN",
        f"Jour {day_index}/{objective_days} | Ref {now_ny.strftime('%Y-%m-%d %H:%M %Z')}",
        f"Budget base: {base_budget_usd:.2f} USD",
        f"PnL realise jour: {pnl_day:+.2f} USD ({closed_count} trade(s) clos aujourd'hui)",
        f"PnL realise cumule: {pnl_total:+.2f} USD",
        f"Budget effectif: {effective_budget:.2f} USD",
        f"Montant deploye: {deployed:.2f} USD | Libre: {free_cash:.2f} USD",
    ]
    lines.append("")
    lines.append(f"Trades clos aujourd'hui (max {daily_list_max}):")
    if not rows_today:
        lines.append("  Aucun trade clos aujourd'hui.")
    else:
        for r in rows_today[-daily_list_max:]:
            lines.append(
                "  "
                f"{str(r.get('time_ny', '--:--'))} | "
                f"{str(r.get('ticker', '?'))} | "
                f"Entree {float(r.get('entry_price_usd', 0.0) or 0.0):.2f} -> "
                f"Sortie {float(r.get('exit_price_usd', 0.0) or 0.0):.2f} | "
                f"Taille {float(r.get('size_usd', 0.0) or 0.0):.0f} USD | "
                f"PnL {float(r.get('pnl_usd', 0.0) or 0.0):+.2f} USD "
                f"({float(r.get('pnl_pct', 0.0) or 0.0):+.2f}%)"
            )
    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3850] + "\n\n...(rapport tronque, trop long pour Telegram)"
    return out


def build_performance_report(
    journal_file: str,
    positions: Dict[str, Dict[str, object]],
    *,
    budget_usd: float,
    now_ny: Optional[datetime] = None,
) -> str:
    """
    Rapport texte: PnL realise (periodes), win rate,
    meilleurs/moins bons trades uniques (chaque trade_closed),
    top/moins bons symboles (PnL cumule par ticker),
    meilleures/moins bonnes journees de trading.
    """
    if now_ny is None:
        now_ny = datetime.now(MARKET_TZ)
    else:
        now_ny = now_ny.astimezone(MARKET_TZ)

    events = load_trade_closed_events(journal_file)
    d7 = now_ny - timedelta(days=7)
    d30 = now_ny - timedelta(days=30)
    effective_budget = compute_effective_budget_usd(budget_usd, journal_file)

    pnl_today = compute_day_realized_pnl_usd(journal_file, now_ny=now_ny)
    s_all = _summarize_closed_trades(events, since_ny=None)
    s7 = _summarize_closed_trades(events, since_ny=d7)
    s30 = _summarize_closed_trades(events, since_ny=d30)

    lines = [
        "RAPPORT PERFORMANCE",
        f"Ref. {now_ny.strftime('%Y-%m-%d %H:%M %Z')} | Budget: {budget_usd:.0f} USD",
        f"Budget effectif (base + PnL realise): {effective_budget:.2f} USD",
        "",
        "PnL realise (USD):",
        f"  Aujourd'hui (NY): {pnl_today:+.2f}",
        f"  7 jours: {s7['total_pnl']:+.2f} ({int(s7['n'])} trade(s))",
        f"  30 jours: {s30['total_pnl']:+.2f} ({int(s30['n'])} trade(s))",
        f"  Tout l'historique: {s_all['total_pnl']:+.2f} ({int(s_all['n'])} trade(s))",
        "",
    ]

    if s_all["n"] > 0:
        lines.append(
            f"Win rate (tout): {s_all['win_rate']:.0f}% | "
            f"gain moyen: {s_all['avg_win']:+.2f} USD ({s_all['avg_win_pct']:+.2f}%) | "
            f"perte moyenne: {s_all['avg_loss']:+.2f} USD ({s_all['avg_loss_pct']:+.2f}%)"
        )
    else:
        lines.append("Aucun trade clos dans le journal (trade_closed).")

    try:
        report_top_trades_n = max(1, min(15, int(os.getenv("REPORT_TOP_TRADES_N", "5"))))
    except ValueError:
        report_top_trades_n = 5

    rows_all = _collect_closed_trade_rows(events, since_ny=None)
    if rows_all:
        best_trades = sorted(
            rows_all,
            key=lambda r: (float(r["pnl_pct"]), float(r["pnl_usd"])),
            reverse=True,
        )[:report_top_trades_n]
        worst_trades = sorted(
            rows_all,
            key=lambda r: (float(r["pnl_pct"]), float(r["pnl_usd"])),
        )[:report_top_trades_n]
        lines.append("")
        lines.append(f"Meilleurs trades (uniques, top {report_top_trades_n} par performance %):")
        for r in best_trades:
            tk = str(r.get("ticker", "?"))
            lines.append(
                f"  {tk}: {float(r['pnl_usd']):+.2f} USD ({float(r['pnl_pct']):+.2f}%) | {r.get('date_ny', '')}"
            )
        lines.append("")
        lines.append(f"Moins bons trades (uniques, top {report_top_trades_n} par performance %):")
        for r in worst_trades:
            tk = str(r.get("ticker", "?"))
            lines.append(
                f"  {tk}: {float(r['pnl_usd']):+.2f} USD ({float(r['pnl_pct']):+.2f}%) | {r.get('date_ny', '')}"
            )

    bt = s_all.get("by_ticker") or {}
    bt_pct = s_all.get("by_ticker_pct") or {}
    if isinstance(bt, dict) and bt:
        sorted_tickers = sorted(bt.items(), key=lambda x: x[1], reverse=True)
        best = sorted_tickers[:3]
        worst = sorted(bt.items(), key=lambda x: x[1])[:3]
        any_neg = any(pnl < 0 for _, pnl in bt.items())
        lines.append("")
        lines.append("Top symboles (PnL cumule sur plusieurs trades possibles):")
        for tk, pnl in best:
            pct = float(bt_pct.get(tk, 0.0) or 0.0) if isinstance(bt_pct, dict) else 0.0
            lines.append(f"  {tk}: {pnl:+.2f} USD ({pct:+.2f}%)")
        lines.append("Moins bons symboles (cumul):")
        for tk, pnl in worst:
            pct = float(bt_pct.get(tk, 0.0) or 0.0) if isinstance(bt_pct, dict) else 0.0
            lines.append(f"  {tk}: {pnl:+.2f} USD ({pct:+.2f}%)")
        if not any_neg:
            lines.append(
                "  (tous les symboles sont en cumul positif; les pertes au trade sont dans Moins bons trades ci-dessus.)"
            )
    try:
        report_top_days_n = max(1, min(15, int(os.getenv("REPORT_TOP_DAYS_N", "5"))))
    except ValueError:
        report_top_days_n = 5

    if rows_all:
        day_totals: Dict[str, Dict[str, float]] = {}
        for r in rows_all:
            d = str(r.get("date_ny", "")).strip()
            if not d:
                continue
            if d not in day_totals:
                day_totals[d] = {"pnl_usd": 0.0, "trades": 0.0}
            day_totals[d]["pnl_usd"] += float(r.get("pnl_usd", 0.0) or 0.0)
            day_totals[d]["trades"] += 1.0

        day_items = [(d, v["pnl_usd"], int(v["trades"])) for d, v in day_totals.items()]
        best_days = sorted(day_items, key=lambda x: x[1], reverse=True)[:report_top_days_n]
        worst_days = sorted(day_items, key=lambda x: x[1])[:report_top_days_n]

        lines.append("")
        lines.append(f"Meilleures journees de trade (top {report_top_days_n}):")
        for d, pnl, n_trades in best_days:
            lines.append(f"  {d}: {pnl:+.2f} USD ({n_trades} trade(s) clos)")
        lines.append("")
        lines.append(f"Moins bonnes journees de trade (top {report_top_days_n}):")
        for d, pnl, n_trades in worst_days:
            lines.append(f"  {d}: {pnl:+.2f} USD ({n_trades} trade(s) clos)")

    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3850] + "\n\n...(rapport tronque, trop long pour Telegram)"
    return out


def build_window_progress_report(
    journal_file: str,
    positions: Dict[str, Dict[str, object]],
    *,
    base_budget_usd: float,
    window_days: int,
    title: str,
    now_ny: Optional[datetime] = None,
) -> str:
    """
    Rapport de progression sur une fenetre glissante (ex: 7j, 30j),
    au format proche de /daily_report.
    """
    if now_ny is None:
        now_ny = datetime.now(MARKET_TZ)
    else:
        now_ny = now_ny.astimezone(MARKET_TZ)

    window_days = max(1, int(window_days))
    since_ny = now_ny - timedelta(days=window_days)
    events = load_trade_closed_events(journal_file)
    s_window = _summarize_closed_trades(events, since_ny=since_ny)
    pnl_total = compute_total_realized_pnl_usd(journal_file)
    effective_budget = compute_effective_budget_usd(base_budget_usd, journal_file)
    deployed = deployed_budget_usd(positions, effective_budget)
    free_cash = effective_budget - deployed

    lines = [
        title.strip() or "RAPPORT FENETRE",
        f"Periode glissante: {window_days} jour(s) | Ref {now_ny.strftime('%Y-%m-%d %H:%M %Z')}",
        f"Budget base: {base_budget_usd:.2f} USD",
        f"PnL realise periode: {float(s_window.get('total_pnl', 0.0)):+.2f} USD ({int(s_window.get('n', 0))} trade(s) clos)",
        (
            f"Win rate periode: {float(s_window.get('win_rate', 0.0)):.0f}% | "
            f"gain moyen: {float(s_window.get('avg_win', 0.0)):+.2f} USD ({float(s_window.get('avg_win_pct', 0.0)):+.2f}%) | "
            f"perte moyenne: {float(s_window.get('avg_loss', 0.0)):+.2f} USD ({float(s_window.get('avg_loss_pct', 0.0)):+.2f}%)"
        ),
        f"PnL realise cumule: {pnl_total:+.2f} USD",
        f"Budget effectif: {effective_budget:.2f} USD",
        f"Montant deploye: {deployed:.2f} USD | Libre: {free_cash:.2f} USD",
    ]

    # Si campagne datee: ajoute un recap depuis le debut d'objectif.
    objective_start_day_env = os.getenv("TRADING_OBJECTIVE_START_DATE", "").strip()
    objective_since_ny: Optional[datetime] = None
    if objective_start_day_env:
        try:
            objective_start_day = datetime.strptime(objective_start_day_env, "%Y-%m-%d").date()
            objective_since_ny = datetime.combine(objective_start_day, datetime.min.time(), tzinfo=MARKET_TZ)
            s_objective = _summarize_closed_trades(events, since_ny=objective_since_ny)
            lines.append(
                f"PnL depuis debut objectif ({objective_start_day.isoformat()}): "
                f"{float(s_objective.get('total_pnl', 0.0)):+.2f} USD "
                f"({int(s_objective.get('n', 0))} trade(s) clos)"
            )
        except ValueError:
            pass

    # Breakdown hebdomadaire en semaine ISO:
    # - si objectif date configure: depuis debut objectif
    # - sinon: sur la fenetre glissante demandee
    weekly_since_ny = objective_since_ny if objective_since_ny is not None else since_ny
    week_totals: Dict[str, Dict[str, float]] = {}
    for ev in events:
        ts_dt = _parse_journal_ts_to_ny(str(ev.get("ts_utc", "")))
        if ts_dt is None or ts_dt < weekly_since_ny:
            continue
        iso = ts_dt.isocalendar()
        wk = f"{iso.year}-W{int(iso.week):02d}"
        if wk not in week_totals:
            week_totals[wk] = {"pnl_usd": 0.0, "trades": 0.0}
        week_totals[wk]["pnl_usd"] += float(ev.get("pnl_usd", 0.0) or 0.0)
        week_totals[wk]["trades"] += 1.0

    if week_totals:
        lines.append("")
        if objective_since_ny is not None and objective_start_day_env:
            lines.append(f"PnL par semaine (ISO) depuis objectif ({objective_start_day_env}):")
        else:
            lines.append("PnL par semaine (ISO):")
        for wk in sorted(week_totals.keys()):
            row = week_totals[wk]
            lines.append(f"  {wk}: {float(row['pnl_usd']):+.2f} USD ({int(row['trades'])} trade(s) clos)")
    else:
        lines.append("")
        if objective_since_ny is not None and objective_start_day_env:
            lines.append(f"PnL par semaine (ISO) depuis objectif ({objective_start_day_env}): aucune cloture.")
        else:
            lines.append("PnL par semaine (ISO): aucune cloture sur la fenetre.")

    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3850] + "\n\n...(rapport tronque, trop long pour Telegram)"
    return out


def _period_to_seconds(period: str) -> int:
    txt = (period or "").strip().lower()
    if not txt:
        return 30 * 24 * 3600
    mapping = {
        "1d": 1 * 24 * 3600,
        "5d": 5 * 24 * 3600,
        "7d": 7 * 24 * 3600,
        "1wk": 7 * 24 * 3600,
        "2wk": 14 * 24 * 3600,
        "1mo": 30 * 24 * 3600,
        "3mo": 90 * 24 * 3600,
        "6mo": 180 * 24 * 3600,
        "1y": 365 * 24 * 3600,
        "2y": 2 * 365 * 24 * 3600,
        "5y": 5 * 365 * 24 * 3600,
    }
    if txt in mapping:
        return mapping[txt]
    m = re.fullmatch(r"(\d+)\s*([dwmy])", txt)
    if not m:
        return 30 * 24 * 3600
    n = max(1, int(m.group(1)))
    unit = m.group(2)
    if unit == "d":
        return n * 24 * 3600
    if unit == "w":
        return n * 7 * 24 * 3600
    if unit == "m":
        return n * 30 * 24 * 3600
    if unit == "y":
        return n * 365 * 24 * 3600
    return 30 * 24 * 3600


def _finnhub_resolution_from_interval(interval: str) -> Optional[str]:
    txt = (interval or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([mhd])", txt)
    if not m:
        return None
    n = max(1, int(m.group(1)))
    unit = m.group(2)
    if unit == "d":
        return "D"
    if unit == "h":
        minutes = n * 60
    else:
        minutes = n
    allowed = [1, 5, 15, 30, 60]
    if minutes <= 1:
        return "1"
    if minutes >= 60:
        return "60"
    for a in allowed:
        if minutes <= a:
            return str(a)
    return "60"


def _twelvedata_interval_from_input(interval: str) -> Optional[str]:
    txt = (interval or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([mhdw])", txt)
    if not m:
        return None
    n = max(1, int(m.group(1)))
    unit = m.group(2)
    if unit == "m":
        return f"{n}min"
    if unit == "h":
        return f"{n}h"
    if unit == "d":
        return f"{n}day"
    if unit == "w":
        return f"{n}week"
    return None


def _fetch_price_data_twelvedata(ticker: str, period: str, interval: str) -> pd.DataFrame:
    api_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TWELVEDATA_API_KEY manquante")
    symbol = normalize_ticker(ticker)
    if symbol == "^VIX":
        symbol = "VIX"
    if not symbol:
        raise ValueError("Ticker vide")

    td_interval = _twelvedata_interval_from_input(interval)
    if not td_interval:
        raise ValueError(f"Intervalle non supporte par Twelve Data: {interval}")
    if not _td_can_consume_credit():
        raise RuntimeError("Twelve Data quota guard actif (limite minute/jour atteinte)")

    # Outputsize raisonnable selon periode/intervalle (cap a 5000).
    sec_total = _period_to_seconds(period)
    sec_per_bar = _period_to_seconds(interval)
    bars = int(max(80, min(5000, (sec_total / max(60, sec_per_bar)) + 30)))
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval,
        "outputsize": bars,
        "format": "JSON",
        "apikey": api_key,
    }
    resp = requests.get(url, params=params, timeout=20)
    _td_register_consume()
    if resp.status_code == 429:
        raise RuntimeError("Twelve Data rate-limit 429")
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("Reponse Twelve Data invalide")
    if payload.get("status") == "error":
        code = str(payload.get("code", "")).strip()
        msg = str(payload.get("message", "error")).strip()
        raise RuntimeError(f"TwelveData {code or 'error'}: {msg}")
    values = payload.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError(f"TwelveData: aucune donnee pour {symbol}")

    rows: List[Dict[str, float]] = []
    idx_list: List[datetime] = []
    for row in values:
        if not isinstance(row, dict):
            continue
        dt_txt = str(row.get("datetime", "")).strip()
        if not dt_txt:
            continue
        try:
            ts = datetime.strptime(dt_txt, "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                ts = datetime.strptime(dt_txt, "%Y-%m-%d")
            except Exception:
                continue
        try:
            rows.append(
                {
                    "Open": float(row.get("open", 0.0) or 0.0),
                    "High": float(row.get("high", 0.0) or 0.0),
                    "Low": float(row.get("low", 0.0) or 0.0),
                    "Close": float(row.get("close", 0.0) or 0.0),
                    "Volume": float(row.get("volume", 0.0) or 0.0),
                }
            )
            idx_list.append(ts)
        except Exception:
            continue
    if not rows:
        raise ValueError(f"TwelveData: donnees OHLCV incompletes pour {symbol}")

    df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx_list))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _fetch_price_data_finnhub(ticker: str, period: str, interval: str) -> pd.DataFrame:
    api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY manquante")
    symbol = normalize_ticker(ticker)
    if not symbol:
        raise ValueError("Ticker vide")
    # Cas indices Yahoo-style (^VIX, etc.): passe direct au fallback Yahoo.
    if symbol.startswith("^"):
        raise ValueError(f"Ticker non supporte par Finnhub: {symbol}")

    resolution = _finnhub_resolution_from_interval(interval)
    if resolution is None:
        raise ValueError(f"Intervalle non supporte par Finnhub: {interval}")
    now_ts = int(time.time())
    from_ts = max(0, now_ts - _period_to_seconds(period))
    url = "https://finnhub.io/api/v1/stock/candle"
    params = {
        "symbol": symbol,
        "resolution": resolution,
        "from": from_ts,
        "to": now_ts,
        "token": api_key,
    }
    resp = requests.get(url, params=params, timeout=20)
    if resp.status_code == 429:
        raise RuntimeError("Finnhub rate-limit 429")
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("Reponse Finnhub invalide")
    status_txt = str(payload.get("s", "")).strip().lower()
    if status_txt != "ok":
        raise ValueError(f"Finnhub: status={status_txt or 'unknown'}")

    ts = payload.get("t")
    o = payload.get("o")
    h = payload.get("h")
    l = payload.get("l")
    c = payload.get("c")
    v = payload.get("v")
    if not all(isinstance(x, list) and x for x in [ts, o, h, l, c, v]):
        raise ValueError("Finnhub: donnees OHLCV incompletes")

    n = min(len(ts), len(o), len(h), len(l), len(c), len(v))
    if n <= 0:
        raise ValueError("Finnhub: aucune bougie exploitable")
    idx = pd.to_datetime(ts[:n], unit="s", utc=True).tz_convert(MARKET_TZ).tz_localize(None)
    df = pd.DataFrame(
        {
            "Open": o[:n],
            "High": h[:n],
            "Low": l[:n],
            "Close": c[:n],
            "Volume": v[:n],
        },
        index=idx,
    )
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _finnhub_quote_timeout_sec() -> float:
    try:
        return max(3.0, float(os.getenv("FINNHUB_QUOTE_TIMEOUT_SEC", "12")))
    except ValueError:
        return 12.0


def _fetch_finnhub_quote(ticker: str) -> Dict[str, float]:
    api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY manquante")
    symbol = normalize_ticker(ticker)
    if not symbol or symbol.startswith("^"):
        raise ValueError(f"Ticker non supporte pour quote Finnhub: {ticker}")
    url = "https://finnhub.io/api/v1/quote"
    resp = requests.get(
        url,
        params={"symbol": symbol, "token": api_key},
        timeout=_finnhub_quote_timeout_sec(),
    )
    if resp.status_code == 429:
        raise RuntimeError("Finnhub quote rate-limit 429")
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("Reponse Finnhub quote invalide")
    px = payload.get("c")
    if px is None:
        raise ValueError("Finnhub quote sans champ c")
    price = float(px)
    if price <= 0:
        raise ValueError("Finnhub quote prix <= 0")
    high = float(payload.get("h") or price)
    low = float(payload.get("l") or price)
    open_px = float(payload.get("o") or price)
    prev_close = float(payload.get("pc") or open_px or price)
    daily_pct = float(payload.get("dp") or 0.0)
    quote_ts = float(payload.get("t") or 0.0)
    return {
        "price": price,
        "high": max(price, high),
        "low": min(price, low),
        "open": open_px,
        "prev_close": prev_close,
        "daily_pct": daily_pct,
        "timestamp": quote_ts,
    }


def _fetch_finnhub_quote_price(ticker: str) -> float:
    return float(_fetch_finnhub_quote(ticker)["price"])


def _fetch_price_data_yahoo(ticker: str, period: str, interval: str) -> pd.DataFrame:
    _yf_parallel_stagger_delay(ticker)
    if _yf_backoff_active():
        # Ne plus bloquer le scan: les fallbacks Finnhub/IBKR prennent le relais.
        wait_on_cooldown = os.getenv("YF_WAIT_ON_COOLDOWN", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
        left = _yf_backoff_seconds_left()
        try:
            max_wait_sec = float(os.getenv("YF_WAIT_MAX_SEC", "120"))
        except ValueError:
            max_wait_sec = 120.0
        max_wait_sec = max(5.0, max_wait_sec)
        if wait_on_cooldown and left > 0 and left <= max_wait_sec:
            print(f"[DATA] Yahoo en cooldown ({left:.0f}s). Attente (YF_WAIT_ON_COOLDOWN=1)...")
            time.sleep(left)
        if _yf_backoff_active():
            raise RuntimeError(
                f"Yahoo en cooldown auto ({_yf_backoff_seconds_left():.0f}s restantes)"
            )

    fetch_period = _yahoo_fetch_period(period, interval)
    if fetch_period != period:
        print(
            f"[DATA] Yahoo: {ticker} period={period}/{interval} -> {fetch_period} "
            f"(API 1d intraday souvent vide)."
        )

    try:
        with _YF_DOWNLOAD_LOCK:
            data = yf.download(
                ticker,
                period=fetch_period,
                interval=interval,
                progress=False,
                auto_adjust=False,
                threads=False,
            )
            # Retry court si vide (glitch reseau / cookie), puis Ticker.history.
            if data is None or getattr(data, "empty", True):
                time.sleep(0.8)
                data = yf.download(
                    ticker,
                    period=fetch_period,
                    interval=interval,
                    progress=False,
                    auto_adjust=False,
                    threads=False,
                )
            if data is None or getattr(data, "empty", True):
                hist = yf.Ticker(ticker).history(
                    period=fetch_period,
                    interval=interval,
                    auto_adjust=False,
                )
                if hist is not None and not hist.empty:
                    data = hist
    except Exception as exc:
        msg = str(exc)
        if _looks_like_yf_rate_limit_error(msg):
            _register_yf_backoff(msg)
        raise
    if data is None or getattr(data, "empty", True):
        raise ValueError(f"Aucune donnee recuperee pour {ticker} ({fetch_period}/{interval}).")

    # Si l'appelant demandait 1d, garder uniquement la derniere seance.
    if (period or "").strip().lower() in {"1d", "1day"} and fetch_period != period:
        data = _trim_ohlc_to_last_session(data)

    try:
        data = normalize_ohlc_columns(data, ticker)
    except ValueError:
        # Erreur ticker/format locale — pas un rate-limit global.
        raise
    _register_yf_success()
    return data


def _data_fallback_to_ibkr_enabled() -> bool:
    return os.getenv("DATA_FALLBACK_TO_IBKR", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def _fetch_price_data_ibkr(ticker: str, period: str, interval: str) -> pd.DataFrame:
    from ibkr_execution import fetch_ibkr_historical_ohlcv

    df = fetch_ibkr_historical_ohlcv(ticker, period=period, interval=interval)
    if df is None or getattr(df, "empty", True):
        raise ValueError(f"IBKR: aucune donnee pour {ticker}")
    return df


def fetch_price_data(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """
    Telecharge OHLCV: provider (.env) puis fallbacks Yahoo -> Finnhub -> IBKR.
    Si Yahoo est en cooldown: on ne bloque plus le scan — on saute direct aux fallbacks.
    """
    provider = os.getenv("DATA_PROVIDER", "auto").strip().lower() or "auto"
    fallback_yahoo = os.getenv("DATA_FALLBACK_TO_YAHOO", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    td_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
    finnhub_key = os.getenv("FINNHUB_API_KEY", "").strip()
    errors: List[str] = []

    def _try_finnhub() -> Optional[pd.DataFrame]:
        if not finnhub_key or not _data_fallback_to_finnhub_enabled():
            return None
        try:
            return _fetch_price_data_finnhub(ticker, period, interval)
        except Exception as exc:
            errors.append(f"Finnhub: {exc}")
            print(f"[DATA] Finnhub indisponible pour {ticker}: {exc}")
            return None

    def _try_ibkr() -> Optional[pd.DataFrame]:
        if not _data_fallback_to_ibkr_enabled():
            return None
        # Indices type ^VIX: pas de Stock SMART USD.
        if normalize_ticker(ticker).startswith("^"):
            return None
        try:
            df = _fetch_price_data_ibkr(ticker, period, interval)
            print(f"[DATA] Bougies IBKR pour {ticker} ({period}/{interval}).")
            return df
        except Exception as exc:
            errors.append(f"IBKR: {exc}")
            print(f"[DATA] IBKR historique indisponible pour {ticker}: {exc}")
            return None

    if provider in {"twelvedata", "auto"} and td_key:
        try:
            return _fetch_price_data_twelvedata(ticker, period, interval)
        except Exception as exc:
            if provider == "twelvedata" and not fallback_yahoo:
                raise
            print(f"[DATA] Twelve Data indisponible pour {ticker}: {exc}. Fallback suite.")
            errors.append(f"TwelveData: {exc}")

    if provider in {"finnhub"} and finnhub_key:
        try:
            return _fetch_price_data_finnhub(ticker, period, interval)
        except Exception as exc:
            print(f"[DATA] Finnhub indisponible pour {ticker}: {exc}. Fallback suite.")
            errors.append(f"Finnhub: {exc}")
            fh = None
            ib = _try_ibkr()
            if ib is not None:
                return ib
            if fallback_yahoo and not _yf_backoff_active():
                return _fetch_price_data_yahoo(ticker, period, interval)
            raise RuntimeError("; ".join(errors) or str(exc))

    # Yahoo (provider=yahoo|auto) — skip si cooldown actif.
    if provider in {"yahoo", "auto"} or (provider in {"twelvedata"} and fallback_yahoo):
        if _yf_backoff_active():
            left = _yf_backoff_seconds_left()
            via = []
            if finnhub_key and _data_fallback_to_finnhub_enabled():
                via.append("Finnhub")
            if _data_fallback_to_ibkr_enabled():
                via.append("IBKR")
            via_txt = "/".join(via) if via else "aucun fallback"
            print(
                f"[DATA] Yahoo en cooldown ({left:.0f}s). "
                f"Scan continue via {via_txt} (bougies)."
            )
        else:
            try:
                return _fetch_price_data_yahoo(ticker, period, interval)
            except Exception as exc:
                print(f"[DATA] Yahoo indisponible pour {ticker}: {exc}. Fallback suite.")
                errors.append(f"Yahoo: {exc}")

        fh = _try_finnhub()
        if fh is not None:
            return fh
        ib = _try_ibkr()
        if ib is not None:
            return ib
        if errors:
            raise RuntimeError("; ".join(errors))
        raise RuntimeError(f"Aucune source OHLCV disponible pour {ticker}")

    if provider == "twelvedata" and not td_key:
        if not fallback_yahoo:
            raise RuntimeError("DATA_PROVIDER=twelvedata mais TWELVEDATA_API_KEY manquante")
        print("[DATA] TWELVEDATA_API_KEY absente. Fallback Yahoo/Finnhub/IBKR.")

    # Dernier recours
    if not _yf_backoff_active():
        try:
            return _fetch_price_data_yahoo(ticker, period, interval)
        except Exception as exc:
            errors.append(f"Yahoo: {exc}")
    fh = _try_finnhub()
    if fh is not None:
        return fh
    ib = _try_ibkr()
    if ib is not None:
        return ib
    raise RuntimeError("; ".join(errors) or f"Aucune donnee pour {ticker}")


def normalize_ohlc_columns(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """
    Normalise les colonnes yfinance pour garantir High/Low/Close simples.
    yfinance peut renvoyer un MultiIndex selon la version/configuration.
    """
    if df is None or df.empty:
        raise ValueError(f"Colonnes OHLC manquantes pour {ticker}: dataframe vide")

    sym = normalize_ticker(ticker)
    out = df.copy()

    def _flatten_multiindex(frame: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(frame.columns, pd.MultiIndex):
            return frame
        names = [str(n) for n in frame.columns.names if n]
        level0 = [str(v) for v in frame.columns.get_level_values(0)]
        level1 = [str(v) for v in frame.columns.get_level_values(-1)]
        ohlc_tokens = {"OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "ADJ CLOSE"}
        lvl0_ohlc = sum(1 for v in level0 if v.upper() in ohlc_tokens)
        lvl1_ohlc = sum(1 for v in level1 if v.upper() in ohlc_tokens)
        sym_u = sym.upper() if sym else ""
        if sym_u and sym_u in [v.upper() for v in level1]:
            return frame.xs(sym, axis=1, level=-1, drop_level=True)
        if sym_u and sym_u in [v.upper() for v in level0]:
            return frame.xs(sym, axis=1, level=0, drop_level=True)
        if lvl0_ohlc >= lvl1_ohlc:
            frame.columns = frame.columns.get_level_values(0)
        else:
            frame.columns = frame.columns.get_level_values(-1)
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        return frame

    out = _flatten_multiindex(out)

    col_names = [str(c).strip() for c in out.columns]
    lower_set = {c.lower() for c in col_names}
    if "close" in lower_set and ("adj close" in lower_set or "adjclose" in lower_set):
        drop_adj = [c for c in out.columns if str(c).strip().lower() in {"adj close", "adjclose"}]
        if drop_adj:
            out = out.drop(columns=drop_adj, errors="ignore")

    canon = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    rename: Dict[str, str] = {}
    for col in list(out.columns):
        key = str(col).strip().lower()
        if key in canon:
            rename[str(col)] = canon[key]
        elif key in {"adj close", "adjclose"} and "close" not in lower_set:
            rename[str(col)] = "Close"
    if rename:
        out = out.rename(columns=rename)

    if out.columns.duplicated().any():
        out = out.loc[:, ~pd.Index(out.columns).duplicated(keep="last")]

    required = {"Open", "High", "Low", "Close"}
    missing = required - {str(c) for c in out.columns}
    if missing and "Close" in missing:
        for alt in ("Adj Close", "adjclose", "AdjClose"):
            if alt in out.columns:
                out = out.rename(columns={alt: "Close"})
                missing = required - {str(c) for c in out.columns}
                break
    if missing:
        raise ValueError(f"Colonnes OHLC manquantes pour {ticker}: {sorted(missing)}")
    return out


def reconcile_analysis_price_usd(
    ticker: str,
    price_usd: float,
    *,
    entry_price_hint: Optional[float] = None,
) -> float:
    """
    Corrige un prix OHLC aberrant (yfinance parallele / MultiIndex / crumb 401).
    """
    px = float(price_usd or 0.0)
    entry = float(entry_price_hint or 0.0)
    suspicious = px <= 0
    if not suspicious and entry > 0:
        ratio = px / entry
        try:
            low_r = float(os.getenv("RECONCILE_ENTRY_RATIO_LOW", "0.72"))
            high_r = float(os.getenv("RECONCILE_ENTRY_RATIO_HIGH", "1.35"))
        except ValueError:
            low_r, high_r = 0.72, 1.35
        if ratio < low_r or ratio > high_r:
            suspicious = True
    elif not suspicious and entry <= 0:
        if px > 50000 or px < 0.5:
            suspicious = True
    if not suspicious:
        return px
    alt: Optional[float] = None
    try:
        alt = float(_fetch_finnhub_quote_price(ticker))
    except Exception:
        alt = None
    if alt is None or alt <= 0:
        try:
            with _YF_DOWNLOAD_LOCK:
                data = yf.download(
                    normalize_ticker(ticker),
                    period="5d",
                    interval="1d",
                    progress=False,
                    auto_adjust=False,
                )
            data = normalize_ohlc_columns(data, ticker)
            if not data.empty:
                alt = float(data["Close"].iloc[-1])
        except Exception as exc:
            print(f"[{ticker}] Reconciliation prix echouee: {exc}")
            alt = None
    if alt is None or alt <= 0:
        return px
    if entry > 0:
        try:
            low_r = float(os.getenv("RECONCILE_ENTRY_RATIO_LOW", "0.72"))
            high_r = float(os.getenv("RECONCILE_ENTRY_RATIO_HIGH", "1.35"))
        except ValueError:
            low_r, high_r = 0.72, 1.35
        ratio_alt = alt / entry
        if ratio_alt < low_r or ratio_alt > high_r:
            print(
                f"[{ticker}] Prix suspect conserve {px:.2f} USD "
                f"(fallback {alt:.2f} aussi incoherent vs entree {entry:.2f})."
            )
            return px
    print(f"[{ticker}] Prix scan corrige {px:.2f} -> {alt:.2f} USD.")
    return float(alt)


def refresh_analysis_live_price(
    ticker: str,
    analysis: Dict[str, str],
    positions: Dict[str, Dict[str, object]],
) -> Dict[str, str]:
    """
    Remplace le prix du scan par le prix live (IBKR prioritaire) avant TP/VENTE.
    Evite VENDRE 'prise de profits' sur un prix Yahoo fantome (ex: SMCI 69 vs 47 reel).
    """
    out = dict(analysis)
    sym = normalize_ticker(ticker)
    st = normalize_position_state(positions.get(sym, {}))
    entry = float(st.get("entry_price_usd", 0.0) or 0.0)
    scan_px = float(out.get("current_price", 0) or 0.0)
    live_px = get_reference_price_usd(sym)
    if live_px is not None and live_px > 0:
        fixed = reconcile_analysis_price_usd(
            sym,
            float(live_px),
            entry_price_hint=entry if entry > 0 else None,
        )
    elif scan_px > 0:
        fixed = reconcile_analysis_price_usd(
            sym,
            scan_px,
            entry_price_hint=entry if entry > 0 else None,
        )
    else:
        return out
    if scan_px > 0 and abs(fixed - scan_px) / max(scan_px, 1e-6) > 0.04:
        src = "IBKR/ref" if live_px is not None else "reconciliation"
        print(f"[{sym}] Prix execution ({src}): {scan_px:.2f} -> {fixed:.2f} USD.")
    out["current_price"] = f"{fixed}"
    return out


def adjust_sell_decision_after_live_price(
    ticker: str,
    decision: str,
    analysis: Dict[str, str],
    state: Dict[str, object],
    parsed: Dict[str, str],
) -> str:
    """Annule un VENDRE 'profit' si le prix live ne le justifie pas."""
    decision_up = (decision or "").upper().strip()
    if decision_up != "VENDRE":
        return decision_up
    st = normalize_position_state(state)
    if not bool(st.get("in_position")):
        return decision_up
    px = float(analysis.get("current_price", 0.0) or 0.0)
    entry = float(st.get("entry_price_usd", 0.0) or 0.0)
    if px <= 0 or entry <= 0:
        return decision_up
    tp_val = parse_price_value(str(st.get("take_profit", "")))
    pnl_pct = ((px - entry) / entry) * 100.0
    justif_lower = str(parsed.get("justification", "")).lower()
    profit_words = (
        "gain",
        "gains",
        "profit",
        "tp ",
        "take profit",
        "objectif",
        "securisation",
        "sécurisation",
        "depasse",
        "dépassé",
        "atteint",
    )
    claims_profit = any(w in justif_lower for w in profit_words)
    if tp_val is not None and tp_val > 0 and px < tp_val * 0.998 and claims_profit:
        print(
            f"[{ticker}] VENDRE annule -> ATTENDRE: prix live {px:.2f} < TP {tp_val:.2f} "
            f"(le scan LLM avait probablement un mauvais prix)."
        )
        parsed["decision"] = "ATTENDRE"
        return "ATTENDRE"
    if pnl_pct < -0.1 and claims_profit and "protection" not in justif_lower:
        print(
            f"[{ticker}] VENDRE annule -> ATTENDRE: P/L latent {pnl_pct:+.2f}% "
            f"(pas une prise de profits)."
        )
        parsed["decision"] = "ATTENDRE"
        return "ATTENDRE"
    return decision_up


def _looks_like_indicator_not_price(
    value: float,
    *,
    current_price: float,
    rsi: float,
    conf: Optional[int] = None,
) -> bool:
    """True si la valeur ressemble a RSI/CONFIANCE plutot qu'un prix USD du ticker."""
    v = float(value)
    if v <= 2.0:
        return True
    if 5.0 <= v <= 100.0 and abs(v - float(rsi)) < 2.5:
        return True
    if conf is not None and 5.0 <= v <= 100.0 and abs(v - float(conf)) < 1.5:
        return True
    return False


def sanitize_llm_parsed_levels(
    parsed: Dict[str, str],
    *,
    ticker: str,
    current_price: float,
    rsi: float,
    decision: str,
) -> int:
    """
    Retire stop/TP LLM incoherents (confusion RSI, autre ticker, ordre de grandeur).
    Le bot recalculera ensuite via adjust_sl_tp_atr_floor sur le prix scan.
    """
    if current_price <= 0:
        return 0
    conf = parse_confidence(str(parsed.get("confiance", "") or ""))
    decision_up = (decision or "").upper().strip()
    fixed = 0

    checks: List[Tuple[str, str, bool]] = [
        ("stop_loss", "STOP_LOSS", True),
        ("objectif_1", "OBJECTIF_1", False),
    ]
    for field, label, is_stop in checks:
        val = parse_price_value(str(parsed.get(field, "") or ""))
        if val is None or val <= 0:
            continue
        bad = False
        reason = ""
        if _looks_like_indicator_not_price(val, current_price=current_price, rsi=rsi, conf=conf):
            bad = True
            reason = "valeur type indicateur (RSI/CONFIANCE), pas un prix USD"
        elif decision_up == "ACHETER":
            if is_stop and val >= current_price * 0.995:
                bad, reason = True, "stop au-dessus ou egal au prix actuel"
            elif is_stop and val < current_price * 0.50:
                bad, reason = True, "stop trop bas vs prix actuel"
            elif (not is_stop) and val <= current_price * 1.002:
                bad, reason = True, "objectif sous le prix actuel"
            elif (not is_stop) and val > current_price * 2.5:
                bad, reason = True, "objectif trop eloigne du prix actuel"
        rel = abs(val - current_price) / max(current_price, 1e-6)
        if not bad and rel > 0.40:
            bad = True
            reason = f"ecart {rel * 100:.0f}% vs prix scan"
        if bad:
            print(
                f"[{ticker}] {label} LLM ignore ({val:.2f} USD): {reason} — "
                f"recalcul depuis prix scan {current_price:.2f} USD."
            )
            parsed[field] = ""
            fixed += 1
    return fixed


def warn_llm_price_drift(
    ticker: str,
    *,
    current_price: float,
    entry_price: float,
    justification: str,
    rsi: float = 50.0,
    conf: Optional[int] = None,
) -> None:
    """Alerte si la justification LLM cite un prix tres eloigne du prix scan."""
    if not justification or current_price <= 0:
        return
    if conf is None:
        conf = parse_confidence(justification)
    prices = [float(m.group(0)) for m in re.finditer(r"\d+(?:\.\d+)?", justification)]
    if not prices:
        return
    for p in prices:
        if p > 50000.0:
            continue
        if _looks_like_indicator_not_price(p, current_price=current_price, rsi=rsi, conf=conf):
            continue
        if abs(p - current_price) / current_price > 0.35:
            if entry_price > 0 and abs(p - entry_price) / entry_price <= 0.12:
                continue
            print(
                f"[{ticker}] Alerte coherence LLM: prix cite ~{p:.2f} USD "
                f"vs scan {current_price:.2f} USD — ignorer formulations hors ticker."
            )
            break


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute RSI, Bollinger, ATR et MACD au DataFrame."""
    df = df.copy()
    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    # RSI 14 (Wilder)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss
    df["RSI_14"] = 100 - (100 / (1 + rs))

    # Bollinger Bands (20, 2)
    bb_mid = close.rolling(window=20, min_periods=20).mean()
    bb_std = close.rolling(window=20, min_periods=20).std(ddof=0)
    df["BBM_20_2.0"] = bb_mid
    df["BBU_20_2.0"] = bb_mid + (2 * bb_std)
    df["BBL_20_2.0"] = bb_mid - (2 * bb_std)

    # ATR 14 (Wilder)
    prev_close = close.shift(1)
    tr_components = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    tr = tr_components.max(axis=1)
    df["ATR_14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    # MACD (12,26,9)
    ema_fast = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema_slow = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=9, adjust=False, min_periods=9).mean()
    hist = macd_line - signal_line
    df["MACD_12_26_9"] = macd_line
    df["MACDs_12_26_9"] = signal_line
    df["MACDh_12_26_9"] = hist

    return df


def get_latest_news(ticker: str, limit: int = 5) -> List[str]:
    """Recupere les dernieres news d'un ticker via yfinance."""
    try:
        news_items = yf.Ticker(ticker).news or []
    except Exception:
        return []

    cleaned: List[str] = []
    for item in news_items[:limit]:
        title = item.get("title", "").strip()
        publisher = item.get("publisher", "").strip()
        if title:
            cleaned.append(f"- {title} ({publisher})" if publisher else f"- {title}")
    return cleaned


def parse_earnings_date(raw_value: object) -> Optional[date]:
    """Convertit un champ earningsDate Yahoo en date locale simple."""
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        return raw_value.date()
    if isinstance(raw_value, date):
        return raw_value
    if isinstance(raw_value, (list, tuple)) and raw_value:
        return parse_earnings_date(raw_value[0])
    if isinstance(raw_value, str):
        txt = raw_value.strip()
        if not txt:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(txt, fmt).date()
            except Exception:
                continue
    return None


def _earnings_date_from_timestamp(raw: object) -> Optional[date]:
    """Convertit un timestamp unix Yahoo (earningsTimestamp*) en date marche."""
    if not isinstance(raw, (int, float)) or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC_TZ).astimezone(MARKET_TZ).date()
    except (OverflowError, OSError, ValueError):
        return None


def next_earnings_date(ticker_obj: object, info: Dict[str, object]) -> Optional[date]:
    """
    Prochaine date de resultats, ou None si aucune date FUTURE connue.

    yfinance >= 1.x ne renvoie plus 'earningsDate' dans .info (le champ etait lu
    tel quel et valait toujours None : blocage earnings inoperant). On lit donc
    .calendar puis les timestamps, et on ne garde que les dates >= aujourd'hui —
    'earningsTimestamp' peut pointer sur les resultats *passes*.
    """
    today = datetime.now(MARKET_TZ).date()
    candidates: List[date] = []

    try:
        cal = getattr(ticker_obj, "calendar", None) or {}
        raw_cal = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if isinstance(raw_cal, (list, tuple)):
            for item in raw_cal:
                d = parse_earnings_date(item)
                if d is not None:
                    candidates.append(d)
        else:
            d = parse_earnings_date(raw_cal)
            if d is not None:
                candidates.append(d)
    except Exception:
        pass

    for key in ("earningsTimestampStart", "earningsTimestamp", "earningsTimestampEnd"):
        d = _earnings_date_from_timestamp(info.get(key))
        if d is not None:
            candidates.append(d)

    future = [d for d in candidates if d >= today]
    return min(future) if future else None


def clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


_SECTOR_ETF_BY_NAME = {
    "technology": "XLK",
    "communication services": "XLC",
    "consumer cyclical": "XLY",
    "consumer defensive": "XLP",
    "financial services": "XLF",
    "healthcare": "XLV",
    "industrials": "XLI",
    "basic materials": "XLB",
    "energy": "XLE",
    "real estate": "XLRE",
    "utilities": "XLU",
}


def _safe_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _compute_close_return_pct(ticker: str, period: str = "3mo", interval: str = "1d") -> Optional[float]:
    try:
        df = fetch_price_data(ticker, period=period, interval=interval)
        df = normalize_ohlc_columns(df, ticker)
        if "Close" not in df.columns:
            return None
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) < 20:
            return None
        first = float(close.iloc[0])
        last = float(close.iloc[-1])
        if first <= 0:
            return None
        return ((last / first) - 1.0) * 100.0
    except Exception:
        return None


def _sector_relative_strength_bonus(sector_name: str) -> Tuple[float, Optional[float], Optional[float]]:
    """
    Bonus/malus de regime sectoriel vs SPY sur 3 mois.
    Retourne (bonus_score, spread_pct, sector_ret_pct).
    """
    sector_key = (sector_name or "").strip().lower()
    etf = _SECTOR_ETF_BY_NAME.get(sector_key)
    if not etf:
        return 0.0, None, None

    cache_now = time.time()
    state = getattr(_sector_relative_strength_bonus, "_cache", {})
    if not isinstance(state, dict):
        state = {}
    cache_key = f"{etf}|3mo"
    row = state.get(cache_key) if isinstance(state.get(cache_key), dict) else {}
    ts = float(row.get("ts", 0.0) or 0.0)
    if cache_now - ts <= 1800.0:
        spread = _safe_float(row.get("spread_pct"))
        sec_ret = _safe_float(row.get("sector_ret_pct"))
        bonus = _safe_float(row.get("bonus"))
        if bonus is not None:
            return bonus, spread, sec_ret

    sector_ret = _compute_close_return_pct(etf, period="3mo", interval="1d")
    spy_ret = _compute_close_return_pct("SPY", period="3mo", interval="1d")
    if sector_ret is None or spy_ret is None:
        return 0.0, None, sector_ret
    spread = float(sector_ret - spy_ret)
    if spread >= 4.0:
        bonus = 4.0
    elif spread >= 1.5:
        bonus = 2.0
    elif spread <= -4.0:
        bonus = -4.0
    elif spread <= -1.5:
        bonus = -2.0
    else:
        bonus = 0.0

    state[cache_key] = {
        "ts": cache_now,
        "spread_pct": spread,
        "sector_ret_pct": sector_ret,
        "bonus": bonus,
    }
    setattr(_sector_relative_strength_bonus, "_cache", state)
    return bonus, spread, sector_ret


def fundamental_score_from_metrics(metrics: Dict[str, Optional[float]]) -> int:
    """
    Score fondamental 0..100 (rules-based) pour trading court terme:
    croissance + qualite business + bilan + valorisation + analystes.
    """
    score = 50.0
    rev_growth = metrics.get("revenueGrowth")
    earn_growth = metrics.get("earningsGrowth")
    margin = metrics.get("profitMargins")
    op_margin = metrics.get("operatingMargins")
    gross_margin = metrics.get("grossMargins")
    roe = metrics.get("returnOnEquity")
    debt_to_eq = metrics.get("debtToEquity")
    net_debt_to_ebitda = metrics.get("netDebtToEbitda")
    current_ratio = metrics.get("currentRatio")
    fcf_yield = metrics.get("fcfYield")
    forward_pe = metrics.get("forwardPE")
    peg_ratio = metrics.get("pegRatio")
    recommendation_mean = metrics.get("recommendationMean")
    analyst_count = metrics.get("numberOfAnalystOpinions")
    target_upside_pct = metrics.get("targetUpsidePct")
    sector_strength_bonus = metrics.get("sectorStrengthBonus")

    if rev_growth is not None:
        score += max(-10.0, min(12.0, rev_growth * 40.0))
    if earn_growth is not None:
        score += max(-10.0, min(12.0, earn_growth * 35.0))
    if margin is not None:
        score += max(-8.0, min(10.0, margin * 50.0))
    if debt_to_eq is not None:
        if debt_to_eq <= 60:
            score += 5
        elif debt_to_eq >= 180:
            score -= 8
    if current_ratio is not None:
        if current_ratio >= 1.3:
            score += 4
        elif current_ratio < 1.0:
            score -= 5
    if op_margin is not None:
        score += max(-6.0, min(8.0, op_margin * 35.0))
    if gross_margin is not None:
        score += max(-4.0, min(6.0, gross_margin * 22.0))
    if roe is not None:
        score += max(-5.0, min(7.0, roe * 18.0))
    if net_debt_to_ebitda is not None:
        if net_debt_to_ebitda <= 1.5:
            score += 3
        elif net_debt_to_ebitda >= 4.0:
            score -= 5
    if fcf_yield is not None:
        if fcf_yield >= 0.04:
            score += 5
        elif fcf_yield <= 0.0:
            score -= 5
    if forward_pe is not None and forward_pe > 0:
        if forward_pe > 80:
            score -= 8
        elif forward_pe < 25:
            score += 4
    if peg_ratio is not None and peg_ratio > 0:
        if peg_ratio > 3:
            score -= 6
        elif peg_ratio < 1.5:
            score += 4
    if recommendation_mean is not None and recommendation_mean > 0:
        if recommendation_mean <= 2.0:
            score += 4
        elif recommendation_mean >= 3.5:
            score -= 4
    if analyst_count is not None:
        if analyst_count >= 15:
            score += 2
        elif analyst_count <= 3:
            score -= 2
    if target_upside_pct is not None:
        if target_upside_pct >= 10.0:
            score += 4
        elif target_upside_pct <= -5.0:
            score -= 6
    if sector_strength_bonus is not None:
        score += max(-5.0, min(5.0, sector_strength_bonus))

    return clamp_score(score)


def fetch_fundamental_data(
    ticker: str,
    cache: Dict[str, dict],
    cache_hours: float,
) -> Dict[str, object]:
    """
    Recupere des donnees fondamentales gratuites Yahoo + cache local.
    """
    now_ts = time.time()
    key = normalize_ticker(ticker)
    cached = cache.get(key)
    if isinstance(cached, dict):
        ts = float(cached.get("ts", 0.0))
        if now_ts - ts <= max(300.0, cache_hours * 3600.0):
            payload = cached.get("payload")
            if isinstance(payload, dict):
                return payload

    payload: Dict[str, object] = {
        "fund_score": 50,
        "metrics": {},
        "earnings_date": None,
        "earnings_days": None,
        "risk_off": False,
    }
    tk = None
    try:
        tk = yf.Ticker(key)
        info = tk.info or {}
    except Exception:
        info = {}

    metrics: Dict[str, Optional[float]] = {}
    for k in [
        "marketCap",
        "forwardPE",
        "trailingPE",
        "pegRatio",
        "profitMargins",
        "operatingMargins",
        "grossMargins",
        "returnOnEquity",
        "debtToEquity",
        "currentRatio",
        "revenueGrowth",
        "earningsGrowth",
        "numberOfAnalystOpinions",
        "recommendationMean",
        "targetMeanPrice",
        "currentPrice",
        "freeCashflow",
        "totalDebt",
        "totalCash",
        "ebitda",
    ]:
        metrics[k] = _safe_float(info.get(k))

    market_cap = metrics.get("marketCap")
    free_cf = metrics.get("freeCashflow")
    total_debt = metrics.get("totalDebt")
    total_cash = metrics.get("totalCash")
    ebitda = metrics.get("ebitda")
    target_mean_price = metrics.get("targetMeanPrice")
    current_price = metrics.get("currentPrice")
    if current_price is None:
        current_price = get_reference_price_usd(key, mode="intraday")

    if market_cap is not None and market_cap > 0 and free_cf is not None:
        metrics["fcfYield"] = free_cf / market_cap
    else:
        metrics["fcfYield"] = None

    if ebitda is not None and ebitda > 0:
        net_debt = (total_debt or 0.0) - (total_cash or 0.0)
        metrics["netDebtToEbitda"] = net_debt / ebitda
    else:
        metrics["netDebtToEbitda"] = None

    if target_mean_price is not None and current_price is not None and current_price > 0:
        metrics["targetUpsidePct"] = ((target_mean_price / current_price) - 1.0) * 100.0
    else:
        metrics["targetUpsidePct"] = None

    sector_name = str(info.get("sector", "") or "").strip()
    sector_bonus, sector_spread_pct, sector_ret_pct = _sector_relative_strength_bonus(sector_name)
    metrics["sectorStrengthBonus"] = sector_bonus
    metrics["sectorRelSpreadPct"] = sector_spread_pct
    metrics["sectorReturnPct"] = sector_ret_pct

    earnings_d = next_earnings_date(tk, info)
    earnings_days: Optional[int] = None
    if earnings_d is not None:
        earnings_days = (earnings_d - datetime.now(MARKET_TZ).date()).days

    fund_score = fundamental_score_from_metrics(metrics)
    payload = {
        "fund_score": fund_score,
        "metrics": metrics,
        "sector": sector_name or None,
        "earnings_date": earnings_d.isoformat() if earnings_d else None,
        "earnings_days": earnings_days,
        "risk_off": False,
    }
    cache[key] = {"ts": now_ts, "payload": payload}
    return payload


def get_market_regime_risk_off(vix_threshold: float = 22.0) -> bool:
    """True si regime risk-off (proxy VIX)."""
    try:
        vix = fetch_price_data("^VIX", period="5d", interval="1d")
        vix = normalize_ohlc_columns(vix, "^VIX")
        if vix.empty:
            return False
        close_series = vix["Close"]
        if isinstance(close_series, pd.DataFrame):
            close_series = close_series.iloc[:, 0]
        last_close = float(close_series.dropna().iloc[-1])
        return last_close >= vix_threshold
    except Exception:
        return False


def get_higher_tf_context(ticker: str, period: str = "3mo", interval: str = "1h") -> Dict[str, object]:
    """
    Contexte tendance timeframe superieur (gratuit).
    trend_score: -2 (baissier) ... +2 (haussier)
    """
    out: Dict[str, object] = {
        "trend_score": 0,
        "summary": "Indisponible",
        "interval": interval,
        "period_high": 0.0,
        "period_low": 0.0,
        "distance_to_period_high_pct": 0.0,
    }
    try:
        raw = fetch_price_data(ticker, period=period, interval=interval)
        raw = normalize_ohlc_columns(raw, ticker)
        if _env_on("MTF_COMPLETED_BARS_ONLY"):
            # la tendance journaliere du backtest est celle de la cloture de la VEILLE
            raw = drop_forming_bar(raw, interval)
        if len(raw) < 80:
            return out
        close = raw["Close"].astype(float)
        high_series = raw["High"].astype(float)
        low_series = raw["Low"].astype(float)
        ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
        ind = add_indicators(raw)
        req_cols = [c for c in ["RSI_14", "MACD_12_26_9", "MACDs_12_26_9"] if c in ind.columns]
        ind = ind.dropna(subset=req_cols) if req_cols else ind
        if ind.empty:
            return out
        last = ind.iloc[-1]
        last_close = float(last["Close"])
        period_high = float(high_series.max()) if not high_series.empty else last_close
        period_low = float(low_series.min()) if not low_series.empty else last_close
        dist_to_high_pct = ((period_high - last_close) / period_high * 100.0) if period_high > 0 else 0.0
        last_ema50 = float(ema50.dropna().iloc[-1]) if not ema50.dropna().empty else last_close
        rsi = float(last.get("RSI_14", 50.0))
        macd = float(last.get("MACD_12_26_9", 0.0))
        macd_sig = float(last.get("MACDs_12_26_9", 0.0))

        score = 0
        if last_close > last_ema50:
            score += 1
        else:
            score -= 1
        if macd >= macd_sig:
            score += 1
        else:
            score -= 1
        if rsi >= 55:
            score += 1
        elif rsi <= 45:
            score -= 1

        score = max(-2, min(2, score))
        if score >= 1:
            bias = "Haussier"
        elif score <= -1:
            bias = "Baissier"
        else:
            bias = "Neutre"
        out = {
            "trend_score": score,
            "summary": f"{bias} ({interval}) | close_vs_ema50={'up' if last_close > last_ema50 else 'down'}, macd={'up' if macd >= macd_sig else 'down'}, rsi={rsi:.1f}",
            "interval": interval,
            "period_high": period_high,
            "period_low": period_low,
            "distance_to_period_high_pct": dist_to_high_pct,
        }
        return out
    except Exception:
        return out


def bollinger_position(last_row: pd.Series) -> str:
    """Donne la position du prix par rapport aux bandes de Bollinger."""
    close = last_row.get("Close")
    bbl = last_row.get("BBL_20_2.0")
    bbu = last_row.get("BBU_20_2.0")

    if pd.isna(close) or pd.isna(bbl) or pd.isna(bbu):
        return "Indisponible"
    if close > bbu:
        return "Au-dessus de la bande superieure (possible breakout haussier)"
    if close < bbl:
        return "En-dessous de la bande inferieure (possible breakout baissier)"
    return "A l'interieur des bandes"


def build_llm_prompt(
    ticker: str,
    current_price: float,
    atr: float,
    bb_position: str,
    rsi: float,
    macd_value: float,
    macd_signal: float,
    news_lines: List[str],
    *,
    budget_usd: float,
    horizon_days: int,
    risk_per_trade_pct: float,
    max_open_positions: int,
    slots_used: int,
    open_tickers: List[str],
    fundamental_score: int,
    earnings_days: Optional[int],
    risk_off_regime: bool,
    higher_tf_summary: str,
    in_position_on_this_ticker: bool = False,
    pending_buy_on_this_ticker: bool = False,
    pending_sell_on_this_ticker: bool = False,
    entry_notional_usd_hint: float = 0.0,
    remaining_budget_usd: float = 0.0,
    entry_price_usd_hint: float = 0.0,
    take_profit_usd_hint: Optional[float] = None,
) -> str:
    """Construit le prompt dynamique envoye au modele IA (cadre trading court terme, USD)."""
    news_text = "\n".join(news_lines) if news_lines else "- Aucune news recente trouvee."
    price_anchor_line = (
        f"PRIX ACTUEL OFFICIEL {ticker} (seul prix valable pour ce ticker): {current_price:.2f} USD.\n"
        "N'utilise jamais le prix d'un autre symbole dans la justification.\n"
        f"NE CONFONDS PAS indicateurs et prix: RSI={rsi:.2f} et CONFIANCE (0-100) ne sont PAS des prix USD.\n"
        f"STOP_LOSS et OBJECTIF_1 doivent rester dans la meme echelle que ~{current_price:.2f} USD "
        f"(typiquement stop ~{max(0.01, current_price * 0.94):.2f}-{current_price * 0.998:.2f}, "
        f"TP ~{current_price * 1.02:.2f}-{current_price * 1.12:.2f} selon setup).\n"
    )
    open_txt = ", ".join(open_tickers) if open_tickers else "aucune"
    risk_usd = budget_usd * (risk_per_trade_pct / 100.0)
    slots_free = max(0, int(max_open_positions) - int(slots_used))
    line_moyenne = budget_usd / max(1, int(max_open_positions))
    portfolio_full = slots_used >= max_open_positions
    swap_on_full = os.getenv("TRADING_SWAP_ON_FULL_SLOTS", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    if portfolio_full:
        if swap_on_full and not in_position_on_this_ticker:
            cap_msg = (
                f"IMPORTANT: Portefeuille PLEIN ({slots_used}/{max_open_positions} slots). "
                f"Positions ouvertes: {open_txt}. "
                "Pas d'achat supplementaire direct — MAIS si CE ticker presente un setup entree clairement "
                "superieur a une ligne deja ouverte, tu PEUX repondre ACHETER pour declencher une proposition "
                "de ROTATION (le bot proposera de vendre la ligne la plus faible). "
                "Sinon ATTENDRE. Si ACHETER rotation: calibrer CONFIANCE selon la force reelle (voir echelle), "
                "pas un 80 automatique."
            )
        else:
            cap_msg = (
                f"IMPORTANT: Le portefeuille operationnel est PLEIN ({slots_used}/{max_open_positions} slots). "
                "Tu DOIS repondre DÉCISION : ATTENDRE pour ce ticker (pas d'ACHETER). "
                "Tu peux encore proposer VENDRE uniquement si le cas justifie une sortie "
                "(et si l'utilisateur est en position)."
            )
    else:
        cap_msg = (
            f"Slots positions/achats en attente: {slots_used}/{max_open_positions}. "
            f"Positions ouvertes confirmees: {open_txt}."
        )
    hint_notional = ""
    if entry_notional_usd_hint and entry_notional_usd_hint > 0:
        hint_notional = f" (notionnel enregistre dans le bot ~{entry_notional_usd_hint:.0f} USD, indicatif)"
    if in_position_on_this_ticker:
        entry_line = (
            f"- Prix d'entree enregistre dans le bot: {entry_price_usd_hint:.2f} USD.\n"
            if entry_price_usd_hint and entry_price_usd_hint > 0
            else "- Prix d'entree enregistre: inconnu (raisonne au prix actuel + structure).\n"
        )
        tp_line = (
            f"- Take profit enregistre: {take_profit_usd_hint:.2f} USD (atteint ou depasse => VENDRE / degagement gains pertinent).\n"
            if take_profit_usd_hint is not None and float(take_profit_usd_hint) > 0
            else "- Take profit enregistre: aucun — VENDRE surtout si plan technique cassé ou objectif palier atteint.\n"
        )
        tp_distance_line = ""
        tp_wording_guard = ""
        proactive_tp_exit_line = ""
        if take_profit_usd_hint is not None and float(take_profit_usd_hint) > 0:
            try:
                tp_val = float(take_profit_usd_hint)
                px_val = float(current_price)
                atr_val = max(0.0, float(atr))
                tp_gap_usd = tp_val - px_val
                tp_gap_pct = (tp_gap_usd / px_val) * 100.0 if px_val > 0 else 0.0
                close_threshold_usd = max(atr_val * 0.6, px_val * 0.01)
                is_close_tp = tp_gap_usd <= close_threshold_usd
                # Permet de suggerer une sortie proactive quand on est proche du TP
                # et que des signes de faiblesse apparaissent.
                near_tp_proactive = tp_gap_usd > 0 and tp_gap_pct <= 2.0
                weakness_flags: List[str] = []
                if float(macd_value) <= float(macd_signal):
                    weakness_flags.append("MACD<=signal")
                if float(rsi) >= 70.0:
                    weakness_flags.append("RSI tendu")
                if str(bb_position).strip().lower() in {"upper_band", "above_mid"}:
                    weakness_flags.append("zone haute Bollinger")
                proactive_exit_ok = near_tp_proactive and len(weakness_flags) >= 2
                weakness_txt = ", ".join(weakness_flags) if weakness_flags else "aucun"
                tp_distance_line = (
                    f"- Distance au TP: {tp_gap_usd:+.2f} USD ({tp_gap_pct:+.2f}%), ATR={atr_val:.2f}. "
                    f"Proximite TP objective: {'OUI' if is_close_tp else 'NON'}.\n"
                )
                proactive_tp_exit_line = (
                    f"- Fenetre sortie proactive avant TP: {'OUI' if proactive_exit_ok else 'NON'} "
                    f"(distance TP <= 2.0%: {'OUI' if near_tp_proactive else 'NON'}, "
                    f"faiblesses detectees: {weakness_txt}).\n"
                )
                tp_wording_guard = (
                    "- IMPORTANT REDACTION TP: n'ecris 'proche du TP' que si la proximite TP objective = OUI. "
                    "Si NON, formule 'encore a distance du TP'.\n"
                )
            except (TypeError, ValueError, ZeroDivisionError):
                tp_distance_line = ""
                tp_wording_guard = ""
                proactive_tp_exit_line = ""
        pnl_line = ""
        pnl_pct_hint: Optional[float] = None
        if entry_price_usd_hint and entry_price_usd_hint > 0:
            try:
                pnl_pct_hint = (float(current_price) - float(entry_price_usd_hint)) / float(entry_price_usd_hint) * 100.0
                pnl_line = f"- P/L latent indicatif vs entree: {pnl_pct_hint:+.1f}%.\n"
            except (TypeError, ValueError, ZeroDivisionError):
                pnl_pct_hint = None
                pnl_line = ""
        pnl_wording_guard = ""
        if pnl_pct_hint is not None and pnl_pct_hint > 0:
            pnl_wording_guard = (
                "- IMPORTANT REDACTION: la ligne est actuellement EN GAIN. "
                "Dans la JUSTIFICATION, n'utilise PAS les formulations 'vente a perte', 'couper la perte', "
                "ou equivalents. Parle de prise de profits, maintien, ou invalidation de these.\n"
            )
        position_context_msg = f"""
CONTEXTE POSITION (OBLIGATOIRE pour ce ticker):
- L'utilisateur a DEJA une position OUVERTE et CONFIRMEE sur {ticker}{hint_notional}.
{entry_line}{tp_line}{tp_distance_line}{proactive_tp_exit_line}{pnl_line}{pnl_wording_guard}{tp_wording_guard}
- Indique quand sortir (benefices ou these morte), mais NE pousse pas une sortie a perte sur du bruit court terme.
- VENDRE en GAIN ou au TP: pertinent si prix >= TP, palier atteint, ou signes de exhaustion utile pour prendre les gains.
- VENDRE en GAIN AVANT TP: pertinent si on est proche du TP et qu'une faiblesse credible apparait (momentum qui s'essouffle, rejection en zone haute, confluence de signaux).
- VENDRE a PERTE: reserve a une INVALIDATION CLAIRE du plan (cassure nette sous support majeur intraday, contexte 1h qui invalide la these, ou risque qui depasse le cadre) — pas seulement parce que MACD 15m < signal ou une contre-danse normale.
- Si P/L negatif leger mais structure 1h encore coherente avec la these d'origine, privilegie ATTENDRE sauf invalidation explicite — ne "panique sell" pas.
- Ne formule PAS la justification comme un simple refus d'ACHETER pour quelqu'un sans ligne.
- DÉCISION ATTENDRE: tenir tant que la these tient et qu'on ne coupe pas sans raison structurelle.
- JUSTIFICATION: cite entree/PM si utile, these, et pourquoi sortir ou tenir.
- ACHETER: pyramider seulement si exceptionnel; sinon TAILLE_SUGGEREE_USD = 0.
- VENDRE: signal clair seulement quand encaisser ou couper a bon escient; le client execute puis /confirm_sell.
""".strip()
    elif pending_sell_on_this_ticker:
        position_context_msg = """
CONTEXTE POSITION:
- Une sortie (VENDRE) est deja en attente de confirmation pour ce ticker. JUSTIFICATION: rappel du plan / invalidation executee ou a valider.
""".strip()
    elif pending_buy_on_this_ticker:
        position_context_msg = """
CONTEXTE POSITION:
- Un ACHETER est en attente de confirmation broker (pas encore valide par l'utilisateur). Ne dis pas qu'il detient deja la ligne; parle validation du plan d'entree ou attente de confirmation.
""".strip()
    else:
        position_context_msg = ""
    earnings_msg = "Inconnu"
    if earnings_days is not None:
        if earnings_days == 0:
            earnings_msg = "Resultats aujourd'hui"
        elif earnings_days > 0:
            earnings_msg = f"Resultats dans {earnings_days} jour(s)"
        else:
            earnings_msg = f"Resultats passes il y a {abs(earnings_days)} jour(s)"
    risk_regime_msg = "RISK-OFF (prudence renforcee)" if risk_off_regime else "Neutre/Risk-on"
    macd_bull = float(macd_value) > float(macd_signal)
    try:
        buy_min_rsi_gate = float(os.getenv("BUY_MIN_RSI", "30"))
    except ValueError:
        buy_min_rsi_gate = 30.0
    buy_require_macd = os.getenv("BUY_REQUIRE_MACD_CROSS", "1").strip().lower() in {"1", "true", "yes", "y"}
    buy_block_fk = os.getenv("BUY_BLOCK_FALLING_KNIFE", "1").strip().lower() in {"1", "true", "yes", "y"}
    macd_gate_txt = "OUI" if macd_bull else "NON"
    fk_risk_txt = ""
    if buy_block_fk and not in_position_on_this_ticker:
        if float(rsi) < buy_min_rsi_gate and not macd_bull:
            fk_risk_txt = (
                f" (attention: RSI<{buy_min_rsi_gate:.0f} sans MACD>signal => le bot bloquera tout ACHETER ici.)"
            )
    bot_buy_sync = ""
    if buy_require_macd and not in_position_on_this_ticker:
        bot_buy_sync = (
            f"- Synchronisation bot (nouvelle entree): MACD ligne > signal requis actuellement: {macd_gate_txt}. "
            f"Si NON, reponds ATTENDRE — un ACHETER serait refuse.{fk_risk_txt}\n"
            f"- Si OUI et contexte 1h au moins neutre/haussier, tu PEUX proposer ACHETER quand stop/TP et R:R sont credibles "
            "(evite ATTENDRE systematique par simple habitude)."
        )
    bot_sell_sync = ""
    if in_position_on_this_ticker and not pending_sell_on_this_ticker:
        macd_bear_txt = "OUI" if not macd_bull else "NON"
        bot_sell_sync = (
            f"- Info gestion: MACD (ce TF) ligne < signal -> {macd_bear_txt}. "
            "Ce seul indicateur ne suffit PAS pour imposer VENDRE a perte; ne t'en sers seul pour vendre qu'avec invalidation de structure ou pour degager des gains.\n"
        )
    pos_block = f"\n{position_context_msg}\n" if position_context_msg else "\n"
    buy_sync_block = f"{bot_buy_sync}\n\n" if bot_buy_sync else ""
    sell_sync_block = f"{bot_sell_sync}\n\n" if bot_sell_sync else ""
    sl_dist_usd, sl_mult_hint, _ = stop_distance_floor_usd(current_price, atr, ticker)
    sl_hint_px = current_price - sl_dist_usd if current_price > 0 else 0.0
    sl_hint_pct = (sl_dist_usd / current_price * 100.0) if current_price > 0 else 0.0
    max_sl_pct_txt = (
        f", plafond bot {max_stop_distance_pct() * 100:.1f}% sous le prix"
        if max_stop_distance_pct() > 0
        else ""
    )
    return f"""
Tu es le gestionnaire de portefeuille / chef de desk actions US pour UN client qui PASSE les ordres lui-meme au broker.
Tu raisonnes en TRADING court terme (scalping / intraday / swing tres court), pas en investissement buy-and-hold. Tout est en USD.

Mode operatoire:
- Donne une consigne NETTE par ticker: ACHETER (nouvelle entree), VENDRE (sortir ou reduire selon le plan), ou ATTENDRE (tenir / ne rien ajouter / surveiller).
- {"Les ordres ACHETER/VENDRE valides sont executes automatiquement chez IBKR (paper ou live selon la config). Tu ne demandes pas de /confirm_buy manuel." if ibkr_auto_execute_enabled() else "Le client execute au broker puis te repond par Telegram (/confirm_buy, /confirm_sell) ou ignore ta consigne. Tu ne fais pas d'ordres automatiques."}
- Ton style: operationnel, direct, comme une note de desk — pas de blabla pedagogique; la JUSTIFICATION reste courte (2-4 phrases) et actionnable.

Cadre de la session (defini par l'utilisateur au lancement du bot):
- Budget TOTAL a gerer pour CETTE session (tout le portefeuille, a repartir entre les lignes): {budget_usd:.0f} USD
  (Ce n'est PAS un budget "par action" : la somme des notional suggeres sur les prochains ACHETER doit rester coherente avec ce total.)
- Horizon de trading: {horizon_days} jour(s) — privilegie la gestion active et evite de trainer des setups sans plan
- Risque cible par idee (cadre discipline): {risk_per_trade_pct:.1f}% du budget TOTAL ~= {risk_usd:.2f} USD si le stop est respecte (perte en dollars sur le PORTEFEUILLE si la ligne est vite coupee). Ce n'est PAS un plafond pour TAILLE_SUGGEREE_USD: la taille proposee est le CAPITAL DEPLOYE sur le trade (chez le broker), pas ce montant risque.
- Indicatif "ligne moyenne" si tous les slots etaient remplis: ~{line_moyenne:.0f} USD. Pour un ACHETER serieux avec budget disponible large, une TAILLE_SUGGEREE_USD doit en general RESTER DU MEME ORDRE (souvent ~{max(120.0, line_moyenne * 0.55):.0f}-{min(remaining_budget_usd, line_moyenne * 1.45):.0f} USD selon conviction/vol sauf si tu justifies explicitement une plus petite ligne (liquidite limitee, forte volatilite, ou peu de creneaux / reste serre — ici creneaux libres: {slots_free}/{max_open_positions}).
- Budget ENCORE DISPONIBLE estime pour de nouvelles entrees (hors lignes deja reservees): ~{remaining_budget_usd:.0f} USD. Toute taille que tu proposes pour ACHETER sur CE ticker doit rester <= ce reste (et coherente avec le budget total {budget_usd:.0f} USD).
- Ne propose ACHETER que si setup "A+" (confluence claire). Sinon ATTENDRE.
- News: si incertitude elevee ou contradiction forte, privilegie ATTENDRE.

{cap_msg}
{buy_sync_block}{sell_sync_block}{pos_block}
Donnees techniques:
- Ticker: {ticker}
{price_anchor_line}- Prix actuel (USD): {current_price:.2f}
- ATR(14) (USD): {atr:.4f}
- Position Bollinger: {bb_position}
- RSI(14): {rsi:.2f}
- MACD: {macd_value:.4f}
- Signal MACD: {macd_signal:.4f}
- Gate technique bot (nouvelle entree, lecture immediate): MACD ligne > signal -> {macd_gate_txt}{fk_risk_txt}

Contexte fondamental/macro (gratuit):
- Score fondamental (0-100): {fundamental_score}
- Prochain earnings: {earnings_msg}
- Regime de marche (proxy VIX): {risk_regime_msg}
- Contexte tendance timeframe superieur: {higher_tf_summary}

News recentes:
{news_text}

Rappels discipline:
- ACHETER seulement si risque/rendement coherent (stop base sur structure ou ATR), pas par FOMO.
- Stop minimal pour {ticker} (bot): environ {sl_hint_px:.2f} USD (~{sl_hint_pct:.1f}% sous le prix, regle {sl_mult_hint:.1f}x ATR14 / min {min_stop_distance_pct() * 100:.1f}%{max_sl_pct_txt}) — ne propose PAS un STOP_LOSS plus serre.
- VENDRE = prise de gains ou sortie disciplinee quand la these est morte — ne coupe pas une ligne en perte sans invalidation claire (pas sur simple lecture MACD court terme).
- Si position ouverte: signale sortie quand c'est justifie (TP, gains a securiser, plan casse); sinon ATTENDRE est souvent sain.
- Si donnees neutres ou range: ATTENDRE.
- Si score fondamental faible (<45) ou earnings imminents, durcis le seuil d'entree.
- En regime risk-off, reduis l'agressivite des ACHETER (privilegie ATTENDRE sauf setup exceptionnel).
- Pour un ACHETER valide, sois plus ambitieux sur OBJECTIF_1: privilegie un TP de continuation (pas un scalp trop court), avec un R:R cible plutot >= 1.3 quand c'est plausible.
- Evite les objectifs trop proches du prix d'entree qui se font filtrer au check RR.

Regles de sortie obligatoires (format strict, une ligne par champ, pas de markdown):
TICKER : {ticker}
DÉCISION : [ACHETER, VENDRE ou ATTENDRE]
CONFIANCE : [0-100, entier — calibre: 65-72 setup correct, 73-79 acceptable, 80-86 bon A, 87-92 tres fort A+, 93-100 exceptionnel; n'utilise PAS 80 par defaut]
STOP_LOSS : [prix USD ou N/A]
OBJECTIF_1 : [prix USD ou N/A; si ACHETER, vise un objectif plus ambitieux (continuation) plutot qu'un TP trop proche]
TAILLE_SUGGEREE_USD : [si ACHETER: entier USD = capital deploye sur CE ticker seul OBLIGATOIRE (PAS le meme chiffre que le "risque ~{risk_usd:.0f} USD"). Evite mini-montants < ~{max(90.0, line_moyenne * 0.35):.0f} USD si le RESTE disponible est large (~{remaining_budget_usd:.0f} USD) sans justification courte dans JUSTIFICATION (volatilite extreme, erreur broker, cash insuffisant). Doit etre <= {remaining_budget_usd:.0f} USD (budget disponible estime). Si ATTENDRE ou VENDRE: 0]
JUSTIFICATION : [2-4 phrases. Si position ouverte sur ce ticker: tenue/gestion/sortie de LA ligne existante. Sinon: confluence entree + volatilite + news; risque principal]

Note: ce n'est pas un conseil financier personnalise; tu aides a structurer une hypothese de trading.
""".strip()


def _groq_candidate_models(primary: str) -> List[str]:
    """
    Modeles Groq a essayer dans l'ordre.
    Par defaut: uniquement le modele .env + 8b-instant (le 70b epuise vite le quota TPD gratuit).
    Override: GROQ_FALLBACK_MODELS=llama-3.1-8b-instant,llama-3.3-70b-versatile
    """
    primary = (primary or "").strip()
    raw = os.getenv("GROQ_FALLBACK_MODELS", "llama-3.1-8b-instant").strip()
    fallbacks = [m.strip() for m in raw.split(",") if m.strip()]
    ordered = [primary] + fallbacks if primary else fallbacks
    return list(dict.fromkeys(ordered))


def ask_llm_decision(prompt: str, model: str, api_key: str, base_url: str) -> Dict[str, str]:
    """Appelle Groq (API compatible OpenAI) avec fallback de modeles."""
    client = OpenAI(api_key=api_key, base_url=base_url)
    unique_candidates = _groq_candidate_models(model)
    last_error: Optional[Exception] = None

    for candidate in unique_candidates:
        try:
            response = client.chat.completions.create(
                model=candidate,
                temperature=0.15,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Tu pilotes un portefeuille actions US court terme pour un client qui execute et confirme. "
                            "Tu evites le bruit mais ne propose pas des TAILLE_SUGGEREE_USD derisoires si le prompt indique beaucoup de budget et de creneaux libres sans raison: la taille = capital deploye, pas les ~X USD \"risque par idee\". "
                            "Pour une nouvelle entree, si MACD>signal OUI dans le prompt, ne reste pas sur ATTENDRE sans raison. "
                            "Sur position ouverte: propose VENDRE pour prendre des gains ou si la these est invalidee structurellement; "
                            "evite de recommander une vente a perte sur la seule faiblesse MACD intraday ou une legere contrepartie. "
                            "Tu respectes STRICTEMENT le format de sortie; pas de sections hors format."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            text = (response.choices[0].message.content or "").strip()
            return {"text": text, "model_used": candidate}
        except Exception as exc:
            last_error = exc
            err_txt = str(exc).lower()
            if "rate_limit" in err_txt and "tokens per day" in err_txt:
                print(
                    "[Groq] Quota journalier tokens atteint — "
                    "scan LLM en pause jusqu'au reset Groq (ou upgrade). "
                    f"Dernier modele: {candidate}."
                )
                break
            continue

    raise RuntimeError(f"Aucun modele Groq compatible n'a repondu. Derniere erreur: {last_error}")


def parse_decision(llm_text: str) -> Dict[str, str]:
    """
    Parse la reponse IA en dictionnaire.
    On supporte les variantes mineures de casse.
    """
    result: Dict[str, str] = {
        "ticker": "",
        "decision": "",
        "justification": llm_text.strip(),
        "confiance": "",
        "stop_loss": "",
        "objectif_1": "",
        "taille_suggeree_usd": "",
    }
    lines = [line.strip() for line in llm_text.splitlines() if line.strip()]

    for line in lines:
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        normalized_key = key.strip().upper().replace("É", "E")
        value = value.strip()

        if normalized_key == "TICKER":
            result["ticker"] = value
        elif normalized_key == "DECISION":
            result["decision"] = value.upper()
        elif normalized_key in {"CONFIANCE", "CONFIANCE_PCT"}:
            result["confiance"] = value
        elif normalized_key in {"STOP_LOSS", "STOP_LOSS_PRIX", "STOP"}:
            result["stop_loss"] = value
        elif normalized_key in {"OBJECTIF_1", "OBJECTIF", "TAKE_PROFIT", "TAKE_PROFIT_1"}:
            result["objectif_1"] = value
        elif normalized_key in {"TAILLE_SUGGEREE_USD", "TAILLE_USD", "SIZE_USD"}:
            result["taille_suggeree_usd"] = value
        elif normalized_key in {"TAILLE_SUGGEREE_EUR", "TAILLE_EUR", "SIZE_EUR"}:
            # Ancien format (retrocompat)
            result["taille_suggeree_usd"] = value
        elif normalized_key == "JUSTIFICATION":
            result["justification"] = value

    return result


def sanitize_sell_justification_for_position(
    *,
    ticker: str,
    decision: str,
    current_price: float,
    state: Dict[str, object],
    parsed: Dict[str, str],
) -> bool:
    """
    Evite les justifications contradictoires sur une ligne ouverte.
    Ex: "prendre les gains" alors que le P/L latent est negatif.
    Retourne True si une correction texte a ete appliquee.
    """
    if decision != "VENDRE":
        return False
    st = normalize_position_state(state)
    if not bool(st.get("in_position", False)):
        return False

    entry_price = float(st.get("entry_price_usd", 0.0) or 0.0)
    if entry_price <= 0:
        return False

    justification = (parsed.get("justification") or "").strip()
    if not justification:
        return False

    try:
        pnl_pct = ((float(current_price) - entry_price) / entry_price) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return False

    txt = justification.lower()
    gain_words = (
        "prendre les gains",
        "prise de profit",
        "prise des profits",
        "encaisser les gains",
        "prendre profit",
        "securiser les gains",
    )
    loss_words = ("vente a perte", "couper la perte", "en perte")

    # Cas 1: la ligne est en perte mais la justification parle de gains.
    if pnl_pct < 0 and any(w in txt for w in gain_words):
        parsed["justification"] = (
            f"La position {ticker} est sous le prix d'entree ({entry_price:.2f} USD, "
            f"P/L latent {pnl_pct:+.1f}%). Si vente, c'est une sortie de protection "
            "sur invalidation du plan, pas une prise de profits."
        )
        return True

    # Cas 2: la ligne est en gain mais la justification parle de perte.
    if pnl_pct > 0 and any(w in txt for w in loss_words):
        parsed["justification"] = (
            f"La position {ticker} reste au-dessus de l'entree ({entry_price:.2f} USD, "
            f"P/L latent {pnl_pct:+.1f}%). Si vente, c'est une prise de profits ou une "
            "securisation du gain, pas une coupe de perte."
        )
        return True

    return False


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def maybe_force_proactive_near_tp_sell(
    *,
    ticker: str,
    decision: str,
    analysis: Dict[str, str],
    state: Dict[str, object],
    parsed: Dict[str, str],
) -> Optional[str]:
    """
    Force un VENDRE de securisation si on est proche du TP avec signes de faiblesse.
    Retourne une raison courte quand un override est applique, sinon None.
    """
    st = normalize_position_state(state)
    if not bool(st.get("in_position", False)):
        return None
    if bool(st.get("pending_sell", False)):
        return None

    decision_up = (decision or "").upper().strip()
    if decision_up == "VENDRE":
        return None

    tp_raw = st.get("take_profit")
    tp_val = parse_price_value(str(tp_raw)) if tp_raw is not None else None
    if tp_val is None or tp_val <= 0:
        return None

    current_price = float(analysis.get("current_price", 0.0) or 0.0)
    if current_price <= 0:
        return None
    entry_price_chk = float(st.get("entry_price_usd", 0.0) or 0.0)
    if price_suspect_vs_entry(current_price, entry_price_chk):
        return None
    # Si TP deja atteint, la logique TP standard prend deja la main.
    if current_price >= tp_val:
        return None

    try:
        near_tp_max_dist_pct = float(os.getenv("SELL_PROACTIVE_NEAR_TP_MAX_DIST_PCT", "1.2"))
    except ValueError:
        near_tp_max_dist_pct = 1.2
    try:
        min_profit_pct = float(os.getenv("SELL_PROACTIVE_NEAR_TP_MIN_PROFIT_PCT", "0.4"))
    except ValueError:
        min_profit_pct = 0.4
    try:
        min_weak_signals = int(os.getenv("SELL_PROACTIVE_NEAR_TP_MIN_WEAK_SIGNALS", "2"))
    except ValueError:
        min_weak_signals = 2
    try:
        min_rsi_weak = float(os.getenv("SELL_PROACTIVE_NEAR_TP_MIN_RSI", "68"))
    except ValueError:
        min_rsi_weak = 68.0

    tp_gap_pct = ((tp_val - current_price) / current_price) * 100.0
    if tp_gap_pct <= 0 or tp_gap_pct > max(0.05, near_tp_max_dist_pct):
        return None

    entry_price = float(st.get("entry_price_usd", 0.0) or 0.0)
    pnl_pct = ((current_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
    if pnl_pct < min_profit_pct:
        return None

    macd_val = float(analysis.get("macd_value", 0.0) or 0.0)
    macd_sig = float(analysis.get("macd_signal", 0.0) or 0.0)
    rsi = float(analysis.get("rsi", 50.0) or 50.0)
    bb_pos = str(analysis.get("bb_pos", "")).strip().lower()

    weak_signals: List[str] = []
    if macd_val <= macd_sig:
        weak_signals.append("MACD<=signal")
    if rsi >= min_rsi_weak:
        weak_signals.append("RSI tendu")
    if bb_pos in {"upper_band", "above_mid"}:
        weak_signals.append("zone haute Bollinger")

    if len(weak_signals) < max(1, min_weak_signals):
        return None

    # Override deterministe pour eviter les occasions ratees de securisation.
    parsed["decision"] = "VENDRE"
    if not parsed.get("confiance"):
        parsed["confiance"] = "78"
    if parse_price_value(parsed.get("objectif_1", "")) is None:
        parsed["objectif_1"] = f"{tp_val:.2f}"
    if parse_price_value(parsed.get("stop_loss", "")) is None:
        sl_state = parse_price_value(str(st.get("stop_loss", "")))
        if sl_state is not None and sl_state > 0:
            parsed["stop_loss"] = f"{sl_state:.2f}"
    parsed["taille_suggeree_usd"] = "0"
    parsed["justification"] = (
        f"Sortie proactive proposee sur {ticker}: prix a {tp_gap_pct:.2f}% du TP "
        f"({tp_val:.2f} USD) avec signaux de faiblesse ({', '.join(weak_signals)}). "
        "Objectif: securiser le gain avant un retracement potentiel."
    )
    return f"proche TP ({tp_gap_pct:.2f}%) + faiblesse ({', '.join(weak_signals)})"


def maybe_force_structure_invalidation_sell(
    *,
    ticker: str,
    decision: str,
    analysis: Dict[str, str],
    state: Dict[str, object],
    parsed: Dict[str, str],
    higher_tf: Dict[str, object],
) -> Optional[str]:
    """
    Coupe une perte avant le stop IBKR si la structure 1h est cassee
    (MTF baissier + signaux techniques faibles).
    """
    enabled = os.getenv("SELL_STRUCTURE_INVALID_ENABLED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    if not enabled:
        return None

    st = normalize_position_state(state)
    if not bool(st.get("in_position", False)) or bool(st.get("pending_sell", False)):
        return None

    decision_up = (decision or "").upper().strip()
    if decision_up == "VENDRE":
        return None

    try:
        min_loss_pct = float(os.getenv("SELL_STRUCTURE_INVALID_MIN_LOSS_PCT", "2.0"))
    except ValueError:
        min_loss_pct = 2.0
    try:
        max_mtf_score = int(os.getenv("SELL_STRUCTURE_INVALID_MAX_MTF_SCORE", "-1"))
    except ValueError:
        max_mtf_score = -1
    try:
        min_weak_signals = int(os.getenv("SELL_STRUCTURE_INVALID_MIN_WEAK_SIGNALS", "2"))
    except ValueError:
        min_weak_signals = 2
    try:
        max_rsi_weak = float(os.getenv("SELL_STRUCTURE_INVALID_MAX_RSI", "45"))
    except ValueError:
        max_rsi_weak = 45.0

    current_price = float(analysis.get("current_price", 0.0) or 0.0)
    entry_price = float(st.get("entry_price_usd", 0.0) or 0.0)
    if current_price <= 0 or entry_price <= 0:
        return None
    if price_suspect_vs_entry(current_price, entry_price):
        return None

    pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
    if pnl_pct > -max(0.05, min_loss_pct):
        return None

    ht_score = int(higher_tf.get("trend_score", 0) or 0)
    if ht_score > max_mtf_score:
        return None

    macd_val = float(analysis.get("macd_value", 0.0) or 0.0)
    macd_sig = float(analysis.get("macd_signal", 0.0) or 0.0)
    rsi = float(analysis.get("rsi", 50.0) or 50.0)
    bb_pos = str(analysis.get("bb_pos", "")).strip().lower()

    weak_signals: List[str] = []
    if ht_score <= max_mtf_score:
        weak_signals.append("MTF baissier")
    if macd_val <= macd_sig:
        weak_signals.append("MACD<=signal")
    if rsi <= max_rsi_weak:
        weak_signals.append("RSI faible")
    if bb_pos in {"lower_band", "below_mid"}:
        weak_signals.append("Bollinger basse")

    if len(weak_signals) < max(1, min_weak_signals):
        return None

    parsed["decision"] = "VENDRE"
    if not parsed.get("confiance"):
        parsed["confiance"] = "76"
    sl_state = parse_price_value(str(st.get("stop_loss", "")))
    if parse_price_value(parsed.get("stop_loss", "")) is None and sl_state is not None:
        parsed["stop_loss"] = f"{sl_state:.2f}"
    parsed["taille_suggeree_usd"] = "0"
    parsed["justification"] = (
        f"Sortie protection sur {ticker}: P/L latent {pnl_pct:+.2f}% avec structure 1h "
        f"invalidee ({', '.join(weak_signals)}). Objectif: limiter la perte avant le stop IBKR."
    )
    return f"P/L {pnl_pct:+.2f}% + structure cassee ({', '.join(weak_signals)})"


def rewrite_decision_justification(
    *,
    ticker: str,
    decision: str,
    parsed: Dict[str, str],
    analysis: Dict[str, str],
    state: Dict[str, object],
) -> None:
    """
    Rend la justification Telegram claire et coherente (tous modes de decision LLM).
    On garde le sens du modele, mais on impose une structure lisible en 2 phrases max.
    """
    decision_up = (decision or "").upper().strip()
    if decision_up not in {"ACHETER", "VENDRE", "ATTENDRE"}:
        return
    st = normalize_position_state(state)
    current_price = float(analysis.get("current_price", 0.0) or 0.0)
    rsi = float(analysis.get("rsi", 50.0) or 50.0)
    macd = float(analysis.get("macd_value", 0.0) or 0.0)
    macd_sig = float(analysis.get("macd_signal", 0.0) or 0.0)
    bb_pos = _compact_text(str(analysis.get("bb_pos", "")))
    stop_txt = _compact_text(str(parsed.get("stop_loss", "")))
    tp_txt = _compact_text(str(parsed.get("objectif_1", "")))

    macd_state = "MACD>signal" if macd > macd_sig else "MACD<=signal"
    entry_price = float(st.get("entry_price_usd", 0.0) or 0.0)
    pnl_pct = None
    if entry_price > 0 and current_price > 0:
        pnl_pct = ((current_price - entry_price) / entry_price) * 100.0

    if decision_up == "ACHETER":
        sentence_1 = (
            f"Setup haussier sur {ticker}: RSI {rsi:.1f}, {macd_state}, Bollinger: {bb_pos.lower() or 'lecture mixte'}."
        )
        sentence_2 = (
            f"Entree validee uniquement si le plan de risque tient (stop {stop_txt or 'N/A'}, objectif {tp_txt or 'N/A'})."
        )
    elif decision_up == "VENDRE":
        pnl_txt = ""
        if pnl_pct is not None:
            pnl_txt = f" (P/L latent {pnl_pct:+.1f}% vs entree {entry_price:.2f} USD)"
            if pnl_pct >= 0:
                action_reason = "prise/securisation de gains"
            else:
                action_reason = "sortie de protection sur invalidation"
        else:
            action_reason = "sortie disciplinee selon le plan"
        sentence_1 = f"Sortie proposee sur {ticker}: {action_reason}{pnl_txt}."
        sentence_2 = f"Execution propre autour du plan de sortie (stop {stop_txt or 'N/A'}, objectif {tp_txt or 'N/A'})."
    else:  # ATTENDRE
        if bool(st.get("in_position", False)):
            if pnl_pct is not None and pnl_pct >= 0:
                pos_state = f"position en gain ({pnl_pct:+.1f}%)"
            elif pnl_pct is not None:
                pos_state = f"position en retrait ({pnl_pct:+.1f}%)"
            else:
                pos_state = "position ouverte"
            sentence_1 = (
                f"On conserve {ticker}: {pos_state}, et aucun signal de sortie assez fort n'est valide a cet instant."
            )
            sentence_2 = (
                f"On surveille une confirmation plus nette avant d'agir (stop {stop_txt or 'N/A'}, objectif {tp_txt or 'N/A'})."
            )
        else:
            sentence_1 = (
                f"Pas de confluence assez solide sur {ticker} pour entrer maintenant (RSI {rsi:.1f}, {macd_state})."
            )
            sentence_2 = "On attend une meilleure zone d'entree et une confirmation plus claire du momentum."

    rewritten = f"{sentence_1} {sentence_2}".strip()
    parsed["justification"] = rewritten


def format_telegram_signal(
    decision: str,
    final_ticker: str,
    analysis: Dict[str, str],
    parsed: Dict[str, str],
    *,
    notional_usd: Optional[float] = None,
) -> str:
    """Formate le message Telegram pour ACHETER/VENDRE avec les champs risque."""
    lines = [
        f"Consigne portefeuille — {final_ticker}",
        f"Directive: {decision}",
        f"Prix (USD): {float(analysis['current_price']):.2f}",
        f"ATR(14): {float(analysis['atr']):.4f}",
        f"Bollinger: {analysis['bb_pos']}",
        f"RSI: {float(analysis['rsi']):.2f}",
    ]
    if parsed.get("confiance"):
        lines.append(f"Confiance: {parsed['confiance']}")
    if parsed.get("stop_loss"):
        lines.append(f"Stop loss (USD): {parsed['stop_loss']}")
    if parsed.get("objectif_1"):
        lines.append(f"Take profit recommande (USD): {parsed['objectif_1']}")
    if decision == "ACHETER":
        llm_size_txt = (parsed.get("taille_suggeree_usd") or "").strip()
        llm_size = parse_notional_usd(llm_size_txt) if llm_size_txt else None
        if notional_usd is not None and notional_usd > 0:
            lines.append(f"Montant a investir (USD): {notional_usd:.0f}")
            if ibkr_auto_execute_enabled():
                lines.append(
                    "(Ordre marche IBKR auto + SL/TP poses chez IBKR si niveaux valides et connexion OK.)"
                )
            else:
                lines.append("(Indicatif, pas un ordre automatique — adapte a ton broker / cash.)")
            if llm_size is not None and abs(llm_size - notional_usd) > 1.0:
                if notional_usd + 1e-6 < llm_size:
                    lines.append(
                        f"Reduction vs taille brute modele ({llm_size_txt} USD): phase campagne "
                        "ou autres regles budget appliques apres coup."
                    )
                else:
                    lines.append(
                        f"Cadre dynamique bot (budget + vol ATR vs taille brute {llm_size_txt} USD) "
                        f"-> retenu {notional_usd:.0f} USD pour rester coherent avec le risque/ligne."
                    )
            if not ibkr_auto_execute_enabled():
                lines.append("Apres execution chez le broker, enregistre la position avec:")
                lines.append(f"/confirm_buy {final_ticker} {notional_usd:.0f}")
        elif llm_size_txt:
            lines.append(f"Taille suggeree (USD): {llm_size_txt}")
            lines.append(f"Montant a investir maintenant: {llm_size_txt} USD")
    if decision == "VENDRE":
        if ibkr_auto_execute_enabled():
            lines.append("Vente marche IBKR declenchee automatiquement si connexion OK.")
        else:
            lines.append("Apres execution chez le broker, enregistre la sortie avec:")
            lines.append(f"/confirm_sell {final_ticker} PRIX_SORTIE")
            lines.append(
                "Pour coller au releve broker (investi + cours moyen entree), optionnel:"
            )
            lines.append(
                f"/confirm_sell {final_ticker} PRIX_SORTIE CAPITAL_INVESTI PRIX_MOYEN_ENTREE"
            )
    lines.append(f"Justification: {parsed.get('justification', '').strip()}")
    return "\n".join(lines)


def ibkr_auto_execute_enabled() -> bool:
    """True si execution automatique IBKR activee (.env IBKR_ENABLED + IBKR_AUTO_EXECUTE)."""
    try:
        from ibkr_execution import is_ibkr_auto_enabled

        return is_ibkr_auto_enabled()
    except ImportError:
        return False


def sell_signal_enabled() -> bool:
    """P2: kill-switch du signal VENDRE IA (TRADING_SELL_SIGNAL_ENABLED=0).
    Coupe uniquement les sorties sur signal (reason=signal_vendre) — le bracket SL/TP,
    l'intraday_flat, les swaps et /confirm_sell manuel restent actifs."""
    return os.getenv("TRADING_SELL_SIGNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}


def ibkr_auto_buy_prepare_notional(
    notional_usd: float,
    *,
    price_usd: Optional[float],
    remaining_usd: float,
    ticker: str = "",
) -> Tuple[float, Optional[str]]:
    """
    Prepare le montant pour auto-IBKR (actions entieres): ajuste vers >= 1 action si possible.
    Retourne (notional_ajuste, erreur_si_budget_insuffisant).
    """
    if not ibkr_auto_execute_enabled():
        return notional_usd, None
    px = float(price_usd) if price_usd is not None and float(price_usd) > 0 else None
    if px is None and ticker:
        px = get_reference_price_usd(ticker)
    if px is None or px <= 0 or notional_usd <= 0:
        return notional_usd, None
    try:
        from ibkr_execution import adjust_notional_for_ibkr_one_share

        return adjust_notional_for_ibkr_one_share(
            notional_usd,
            price_usd=px,
            remaining_usd=remaining_usd,
        )
    except ImportError:
        return notional_usd, None


def record_confirmed_buy(
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    ticker: str,
    *,
    notional_usd: Optional[float] = None,
    entry_price_usd: Optional[float] = None,
    confidence: Optional[int] = None,
    portfolio_profile: Optional[str] = None,
    take_profit: Optional[float] = None,
    stop_loss: Optional[float] = None,
    journal_file: str = "",
    journal_enabled: bool = True,
    source: str = "manual",
    entry_source: Optional[str] = None,
) -> Dict[str, object]:
    """Enregistre un achat confirme (Telegram, CLI ou IBKR auto)."""
    tk = normalize_ticker(ticker)
    state = normalize_position_state(positions.get(tk, {}))
    pf = portfolio_profile or infer_portfolio_profile_for_ticker(tk, state)
    if entry_price_usd is None or float(entry_price_usd) <= 0:
        px = get_reference_price_usd(tk)
        entry_price_usd = float(px) if px is not None else 0.0
    state["in_position"] = True
    state["pending_buy"] = False
    state["pending_sell"] = False
    state["portfolio_profile"] = pf
    state["last_sell_alert_ts"] = 0.0
    state["trailing_active"] = False
    state["trailing_floor_usd"] = 0.0
    state["trailing_peak_usd"] = 0.0
    if notional_usd is not None and notional_usd > 0:
        state["entry_notional_usd"] = float(notional_usd)
    if entry_price_usd and entry_price_usd > 0:
        state["entry_price_usd"] = float(entry_price_usd)
    if confidence is not None:
        state["entry_confidence"] = int(confidence)
    state["entry_ts_utc"] = utc_now_iso_z()
    if take_profit is not None:
        state["take_profit"] = float(take_profit)
    if stop_loss is not None:
        state["stop_loss"] = float(stop_loss)
    es = (entry_source or source or "manual").strip() or "manual"
    state["entry_source"] = es
    positions[tk] = state
    save_positions(position_file, positions)
    _record_confirmed_buy_journal(
        journal_file, journal_enabled, tk, pf, state, source, entry_source=es
    )
    return state


def mark_ibkr_sl_tp_active(
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    ticker: str,
    *,
    sl_px: float,
    tp_px: float,
) -> None:
    tk = normalize_ticker(ticker)
    state = normalize_position_state(positions.get(tk, {}))
    state["ibkr_sl_tp_active"] = True
    if sl_px > 0:
        state["stop_loss"] = float(sl_px)
    if tp_px > 0:
        state["take_profit"] = float(tp_px)
    positions[tk] = state
    save_positions(position_file, positions)


def telegram_notify_ibkr_close_enabled() -> bool:
    return os.getenv("TELEGRAM_NOTIFY_IBKR_CLOSE", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def format_ibkr_sl_tp_closed_alert(
    *,
    ticker: str,
    entry_price_usd: float,
    exit_price_usd: float,
    pnl_usd: Optional[float],
    pnl_pct: Optional[float],
    stop_loss: Optional[float],
    take_profit: Optional[float],
    exit_price_source: str,
    size_usd: float = 0.0,
    foreign_client_id: Optional[int] = None,
    trailing_was_active: bool = False,
) -> str:
    """Message Telegram visible pour cloture detectee via sync IBKR (TP/SL executes chez broker)."""
    tk = normalize_ticker(ticker)
    if foreign_client_id is not None:
        # clientId=0 = TWS GUI / Master non configure — pas un vrai SL/TP bot
        if int(foreign_client_id) == 0:
            kind = "VENTE MANUELLE/TWS (clientId=0)"
        else:
            kind = f"VENTE ETRANGÈRE (clientId={foreign_client_id})"
    elif trailing_was_active:
        # Une fois le TP dynamique actif, "stop_loss" local est le plancher
        # remonte (au-dessus de l'entree) — un fill dessus est un gain verrouille,
        # pas une perte : ne jamais l'etiqueter STOP LOSS.
        kind = "TP DYNAMIQUE (stop remonte)"
    else:
        kind = "CLOTURE IBKR"
        # Ne plus inferer TP/SL depuis le PnL seul (COIN -0.4% etait marque STOP LOSS a tort).
        if take_profit is not None and take_profit > 0 and exit_price_usd >= float(take_profit) * 0.995:
            kind = "TAKE PROFIT"
        elif stop_loss is not None and stop_loss > 0 and exit_price_usd <= float(stop_loss) * 1.005:
            kind = "STOP LOSS"
    icon = "⚠️" if foreign_client_id is not None else ("✅" if (pnl_usd is not None and float(pnl_usd) >= 0) else "🔴")
    src = "fill IBKR" if exit_price_source == "ibkr_fill" else "prix estime"
    lines = [
        f"{icon} POSITION FERMEE — {tk} ({kind})",
        f"Entree: {entry_price_usd:.2f} USD -> Sortie: {exit_price_usd:.2f} USD ({src})",
    ]
    if size_usd > 0:
        lines.append(f"Taille: {size_usd:.2f} USD")
    if stop_loss is not None and stop_loss > 0:
        lines.append(f"Stop etait: {float(stop_loss):.2f} USD")
    if take_profit is not None and take_profit > 0:
        lines.append(f"TP etait: {float(take_profit):.2f} USD")
    if pnl_usd is not None and pnl_pct is not None:
        lines.append(f"PnL: {float(pnl_usd):+.2f} USD ({float(pnl_pct):+.2f}%)")
    if foreign_client_id is not None:
        lines.append(
            "Cause: autre client API IBKR (pas le bot). "
            f"Ferme les autres connexions et mets Master API client ID = bot "
            f"dans Gateway."
        )
    else:
        lines.append("Detection: plus de position chez IBKR (SL/TP ou vente broker).")
    return "\n".join(lines)


def ibkr_close_confirm_debounce_sec() -> float:
    """Delai avant de confirmer un qty=0 (evite un faux positif juste apres un fill)."""
    try:
        return max(0.0, min(30.0, float(os.getenv("IBKR_CLOSE_CONFIRM_DEBOUNCE_SEC", "5"))))
    except ValueError:
        return 5.0


def sync_ticker_closed_at_ibkr(
    ticker: str,
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    *,
    journal_file: str,
    journal_enabled: bool,
    bot_token: str = "",
    chat_id: str = "",
    notify_telegram: bool = True,
) -> bool:
    """
    Si IBKR n'a plus de qty pour ce ticker mais le bot est encore in_position, cloture localement.
    """
    if not ibkr_auto_execute_enabled():
        return False
    tk = normalize_ticker(ticker)
    st = normalize_position_state(positions.get(tk, {}))
    if not bool(st.get("in_position")):
        return False
    try:
        from ibkr_execution import IBKRSession
    except ImportError:
        return False
    try:
        session = IBKRSession.get()
        session.ensure_connected()
        qty = session.position_quantity(tk)
    except Exception as exc:
        print(f"[IBKR] Sync {tk} ignoree: {exc}")
        return False
    if qty > 0:
        return False
    # Debounce anti-faux-positif (incident COIN 13/08/2026): un fill tout juste
    # confirme peut ne pas encore etre reflete dans le cache positions() —
    # on reverifie avant de declarer la ligne fermee.
    debounce_sec = ibkr_close_confirm_debounce_sec()
    if debounce_sec > 0:
        try:
            # time.sleep() ne pompe pas la boucle asyncio d'ib_insync: le cache
            # positions() ne se rafraichirait jamais et le recheck relirait la
            # meme valeur figee. reqPositions() + ib.sleep() forcent le refresh.
            session.ib.reqPositions()
            session.ib.sleep(debounce_sec)
            qty_confirm = session.position_quantity(tk)
        except Exception as exc:
            print(f"[IBKR] Sync {tk}: recheck qty echoue ({exc}) — poursuite avec la 1ere lecture.")
            qty_confirm = qty
        if qty_confirm > 0:
            print(f"[IBKR] Sync {tk}: qty=0 puis {qty_confirm} au recheck — faux positif evite.")
            return False
    entry_px_chk = float(st.get("entry_price_usd", 0.0) or 0.0)
    size_chk = float(st.get("entry_notional_usd", 0.0) or 0.0)
    if entry_px_chk > 0 and size_chk > 0 and journal_file:
        try:
            from ibkr_execution import fetch_ibkr_last_sell_fill_price_usd

            fill_px_chk, _ = fetch_ibkr_last_sell_fill_price_usd(
                tk, since_utc_iso=str(st.get("entry_ts_utc", "") or "").strip() or None
            )
            if fill_px_chk is not None and fill_px_chk > 0:
                if journal_trade_closed_exists(
                    journal_file,
                    {
                        "ticker": tk,
                        "entry_price_usd": entry_px_chk,
                        "exit_price_usd": float(fill_px_chk),
                        "size_usd": size_chk,
                    },
                ):
                    st_fix = normalize_position_state(st)
                    st_fix["in_position"] = False
                    st_fix["pending_buy"] = False
                    st_fix["pending_sell"] = False
                    st_fix["entry_notional_usd"] = 0.0
                    st_fix["entry_price_usd"] = 0.0
                    st_fix["entry_confidence"] = 0
                    st_fix.pop("entry_source", None)
                    st_fix.pop("ibkr_sl_tp_active", None)
                    st_fix.pop("trailing_active", None)
                    st_fix.pop("trailing_floor_usd", None)
                    st_fix.pop("trailing_peak_usd", None)
                    positions[tk] = st_fix
                    save_positions(position_file, positions)
                    print(f"[IBKR] Sync {tk}: deja journalise, etat local corrige.")
                    return True
        except ImportError:
            pass
    entry_ts = str(st.get("entry_ts_utc", "") or "").strip()
    exit_px: Optional[float] = None
    exit_src = ""
    foreign_client_id: Optional[int] = None
    fill_detail: Dict[str, object] = {}
    try:
        from ibkr_execution import fetch_ibkr_last_sell_fill_detail

        fill_detail = fetch_ibkr_last_sell_fill_detail(tk, since_utc_iso=entry_ts or None)
        fill_px = fill_detail.get("price")
        if fill_px is not None and float(fill_px) > 0:
            exit_px = float(fill_px)
            exit_src = str(fill_detail.get("source") or "ibkr_fill")
            if bool(fill_detail.get("foreign")):
                try:
                    foreign_client_id = int(fill_detail.get("client_id"))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    foreign_client_id = None
    except ImportError:
        pass
    if exit_px is None or exit_px <= 0:
        ref_px = get_reference_price_usd(tk)
        exit_px = float(ref_px) if ref_px is not None and ref_px > 0 else float(st.get("entry_price_usd", 0.0) or 0.0)
        exit_src = "reference_price"
    entry_px = float(st.get("entry_price_usd", 0.0) or 0.0)
    size_usd = float(st.get("entry_notional_usd", 0.0) or 0.0)
    sl_ref = parse_price_value(str(st.get("stop_loss", "") or ""))
    if sl_ref is None:
        try:
            sl_ref = float(st.get("stop_loss", 0.0) or 0.0) or None
        except (TypeError, ValueError):
            sl_ref = None
    tp_ref = parse_price_value(str(st.get("take_profit", "") or ""))
    if tp_ref is None:
        try:
            tp_ref = float(st.get("take_profit", 0.0) or 0.0) or None
        except (TypeError, ValueError):
            tp_ref = None
    trailing_was_active = bool(st.get("trailing_active"))
    if foreign_client_id is not None:
        exit_reason = "ibkr_foreign_client"
    elif trailing_was_active:
        exit_reason = "ibkr_trailing_stop"
    else:
        exit_reason = "ibkr_sl_tp"
    _, pnl_usd, pnl_pct = record_confirmed_sell(
        positions,
        position_file,
        tk,
        exit_price_usd=exit_px,
        journal_file=journal_file,
        journal_enabled=journal_enabled,
        source="ibkr_sync",
        exit_reason=exit_reason,
        exit_price_source=exit_src,
    )
    st2 = normalize_position_state(positions.get(tk, {}))
    st2.pop("ibkr_sl_tp_active", None)
    positions[tk] = st2
    save_positions(position_file, positions)
    txt = f"[IBKR] Sync: {tk} ferme cote broker (plus de qty IBKR)."
    if foreign_client_id is not None:
        txt += f"\n⚠️ Vente ETRANGÈRE clientId={foreign_client_id} (pas le bot)."
    if exit_src == "ibkr_fill":
        txt += f"\nPrix sortie IBKR (fill): {exit_px:.2f} USD"
    else:
        txt += f"\nPrix sortie estime (cours): {exit_px:.2f} USD"
    if pnl_usd is not None and pnl_pct is not None:
        pnl_lbl = "PnL" if exit_src == "ibkr_fill" else "PnL estime"
        txt += f"\n{pnl_lbl}: {pnl_usd:+.2f} USD ({pnl_pct:+.2f}%)"
    print(txt)
    if (
        notify_telegram
        and telegram_notify_ibkr_close_enabled()
        and bot_token
        and chat_id
    ):
        alert = format_ibkr_sl_tp_closed_alert(
            ticker=tk,
            entry_price_usd=entry_px,
            exit_price_usd=float(exit_px),
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            stop_loss=sl_ref,
            take_profit=tp_ref,
            exit_price_source=exit_src,
            size_usd=size_usd,
            foreign_client_id=foreign_client_id,
            trailing_was_active=trailing_was_active,
        )
        try:
            send_telegram_alert(bot_token, chat_id, alert)
        except Exception as exc:
            print(f"[IBKR] Telegram sync close {tk}: {exc}")
    return True


def sync_positions_closed_at_ibkr(
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    *,
    journal_file: str,
    journal_enabled: bool,
    bot_token: str,
    chat_id: str,
) -> int:
    """
    Si IBKR n'a plus de ligne mais le bot pense encore in_position (SL/TP executes chez IBKR),
    cloture localement et journalise.
    """
    if not ibkr_auto_execute_enabled():
        return 0
    closed = 0
    for tk, raw in list(positions.items()):
        st = normalize_position_state(raw)
        if not bool(st.get("in_position")):
            continue
        if sync_ticker_closed_at_ibkr(
            tk,
            positions,
            position_file,
            journal_file=journal_file,
            journal_enabled=journal_enabled,
            bot_token=bot_token,
            chat_id=chat_id,
            notify_telegram=True,
        ):
            closed += 1
    return closed


def poll_ibkr_closed_positions_if_due(
    *,
    position_file: str,
    journal_file: str,
    journal_enabled: bool,
    bot_token: str,
    chat_id: str,
) -> int:
    """
    Entre deux cycles de scan : detecte les TP/SL executes chez IBKR et envoie Telegram.
    Evite d'attendre le prochain scan (ex. 10 min) pour la notification.
    """
    if not ibkr_auto_execute_enabled():
        return 0
    if os.getenv("IBKR_SYNC_CLOSE_POLL_ENABLED", "1").strip().lower() not in {
        "1",
        "true",
        "yes",
        "y",
    }:
        return 0
    try:
        interval_sec = float(os.getenv("IBKR_SYNC_CLOSE_POLL_SEC", "45"))
    except ValueError:
        interval_sec = 45.0
    interval_sec = max(15.0, interval_sec)
    now = time.time()
    last_ts = float(getattr(poll_ibkr_closed_positions_if_due, "_last_ts", 0.0) or 0.0)
    if now - last_ts < interval_sec:
        return 0
    poll_ibkr_closed_positions_if_due._last_ts = now  # type: ignore[attr-defined]
    positions = load_positions(position_file)
    try:
        from ibkr_execution import guard_ibkr_foreign_exit_orders

        open_syms = [
            normalize_ticker(tk)
            for tk, raw in positions.items()
            if bool(normalize_position_state(raw).get("in_position"))
        ]
        if open_syms:
            guard_ibkr_foreign_exit_orders(open_syms)
    except ImportError:
        pass
    closed = sync_positions_closed_at_ibkr(
        positions,
        position_file,
        journal_file=journal_file,
        journal_enabled=journal_enabled,
        bot_token=bot_token,
        chat_id=chat_id,
    )
    if closed > 0:
        print(f"[IBKR] Sync inter-cycle: {closed} position(s) fermee(s) detectee(s).")
    return closed


def update_trailing_stop_for_positions(
    positions: Dict[str, Dict[str, object]],
    position_file: str,
) -> int:
    """
    TP dynamique ('peak lock'): une fois le prix >= TP fixe, le plancher de sortie
    devient ce TP (au lieu du stop -2% d'origine) et remonte a chaque nouveau plus
    haut observe — vente au 1er retournement, jamais sous le TP fixe initial.

    Ecrit uniquement l'etat local (stop_loss/take_profit/trailing_*) ; c'est
    sync_open_positions_sl_tp_at_ibkr qui pousse ensuite ces niveaux chez IBKR.
    Le stop remonte est un ordre STP broker (GTC) : la vente au retournement est
    geree par IBKR meme si le bot est hors ligne entre 2 checks — seule la
    remontee du stop necessite que le bot tourne. Si le bot ne reagit jamais
    (crash avant le 1er passage), l'ancien TP fixe reste pose et se declenche
    normalement: le plancher garanti est donc toujours au moins le TP fixe.
    """
    if not fixed_sl_tp_enabled() or not trailing_tp_enabled():
        return 0
    changed = 0
    ceiling_mult = 1.0 + trailing_tp_ceiling_pct() / 100.0
    for tk, raw in list(positions.items()):
        try:
            st = normalize_position_state(raw)
            if not bool(st.get("in_position")) or bool(st.get("pending_sell")):
                continue
            if not bool(st.get("ibkr_sl_tp_active")):
                continue
            entry = float(st.get("entry_price_usd", 0.0) or 0.0)
            tp = float(st.get("take_profit", 0.0) or 0.0)
            sl = float(st.get("stop_loss", 0.0) or 0.0)
            if entry <= 0 or tp <= 0 or sl <= 0:
                continue
            try:
                price = get_reference_price_usd(tk, mode="intraday")
            except Exception:
                price = None
            if price is None or price <= 0:
                continue
            price = float(price)
            trailing_active = bool(st.get("trailing_active"))
            if not trailing_active:
                if price < tp:
                    continue
                floor_px = tp
                new_sl = floor_px
                new_tp = round(price * ceiling_mult, 2)
                st["trailing_active"] = True
                st["trailing_floor_usd"] = float(floor_px)
                st["trailing_peak_usd"] = float(price)
                st["stop_loss"] = float(new_sl)
                st["take_profit"] = float(new_tp)
                positions[tk] = st
                changed += 1
                print(
                    f"[{tk}] TP dynamique active: prix {price:.2f} >= TP fixe {floor_px:.2f} -> "
                    f"plancher releve a {new_sl:.2f} (plafond provisoire {new_tp:.2f})."
                )
                continue
            peak_px = float(st.get("trailing_peak_usd", 0.0) or 0.0)
            if peak_px <= 0:
                peak_px = float(st.get("trailing_floor_usd", tp) or tp)
            if price <= peak_px:
                continue
            # round(price,2) peut arrondir AU-DESSUS du prix reel (ex: 100.006 ->
            # 100.01) -> stop de vente deja "in the money", rejet ou declenchement
            # instantane chez le broker. On arrondit toujours vers le bas pour
            # garantir new_sl < price au moment ou on le pose.
            new_sl = math.floor(price * 100 - 1e-6) / 100.0
            if new_sl >= price:
                new_sl = round(price - 0.01, 2)
            new_tp = round(price * ceiling_mult, 2)
            st["trailing_peak_usd"] = float(price)
            st["stop_loss"] = float(new_sl)
            st["take_profit"] = float(new_tp)
            positions[tk] = st
            changed += 1
            print(f"[{tk}] TP dynamique: nouveau plus haut {price:.2f} -> stop remonte a {new_sl:.2f}.")
        except Exception as exc:
            # Une ligne corrompue/atypique ne doit jamais bloquer les autres tickers.
            print(f"[{tk}] TP dynamique: erreur ignoree ({exc}).")
            continue
    if changed > 0:
        save_positions(position_file, positions)
    return changed


def poll_trailing_tp_if_due(*, position_file: str) -> int:
    """Entre deux cycles de scan: remonte le stop dynamique et le pousse chez IBKR."""
    if not ibkr_auto_execute_enabled() or not fixed_sl_tp_enabled() or not trailing_tp_enabled():
        return 0
    interval_sec = trailing_tp_check_sec()
    now = time.time()
    last_ts = float(getattr(poll_trailing_tp_if_due, "_last_ts", 0.0) or 0.0)
    if now - last_ts < interval_sec:
        return 0
    poll_trailing_tp_if_due._last_ts = now  # type: ignore[attr-defined]
    try:
        positions = load_positions(position_file)
        changed = update_trailing_stop_for_positions(positions, position_file)
        if changed > 0:
            synced = sync_open_positions_sl_tp_at_ibkr(positions, position_file)
            print(f"[IBKR] TP dynamique inter-cycle: {changed} niveau(x) remonte(s), {synced} synchro(s) IBKR.")
        return changed
    except Exception as exc:
        # Ne jamais laisser une erreur ici casser la boucle principale du bot.
        print(f"[IBKR] TP dynamique inter-cycle: erreur ignoree ({exc}).")
        return 0


def sync_open_positions_sl_tp_at_ibkr(
    positions: Dict[str, Dict[str, object]],
    position_file: str,
) -> int:
    """Aligne les ordres SL/TP OCA ouverts chez IBKR avec l'etat local."""
    if not ibkr_auto_execute_enabled():
        return 0
    if os.getenv("IBKR_SYNC_SL_TP_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "y"}:
        return 0
    try:
        from ibkr_execution import guard_ibkr_foreign_exit_orders, sync_ibkr_sl_tp_for_position
    except ImportError:
        return 0
    open_syms = [
        normalize_ticker(tk)
        for tk, raw in positions.items()
        if bool(normalize_position_state(raw).get("in_position"))
        and not bool(normalize_position_state(raw).get("pending_sell"))
    ]
    if open_syms:
        cancelled = guard_ibkr_foreign_exit_orders(open_syms)
        if cancelled:
            print(f"[IBKR] GARDE: {len(cancelled)} vente(s) etrangere(s) annulee(s).")
    synced = 0
    for tk, raw in list(positions.items()):
        st = normalize_position_state(raw)
        if not bool(st.get("in_position")) or bool(st.get("pending_sell")):
            continue
        if not bool(st.get("ibkr_sl_tp_active")):
            continue
        sl = float(st.get("stop_loss", 0.0) or 0.0)
        tp = float(st.get("take_profit", 0.0) or 0.0)
        entry = float(st.get("entry_price_usd", 0.0) or 0.0)
        if sl <= 0 or tp <= 0 or entry <= 0 or sl >= tp:
            continue
        # Une fois le TP dynamique actif, le stop remonte au-dessus de l'entree
        # (c'est le but) : ne plus exiger sl < entry dans ce cas, seulement sl < tp.
        if not bool(st.get("trailing_active")) and not (sl < entry < tp):
            continue
        ok, changed, msg = sync_ibkr_sl_tp_for_position(
            tk,
            stop_loss=sl,
            take_profit=tp,
        )
        if not ok:
            print(f"[IBKR] Sync SL/TP {tk} ignore: {msg}")
            continue
        if changed:
            synced += 1
            # Re-assure l'etat local (notamment apres cap TP dynamique).
            mark_ibkr_sl_tp_active(
                positions,
                position_file,
                tk,
                sl_px=sl,
                tp_px=tp,
            )
            print(f"[IBKR] Sync SL/TP {tk}: {msg}")
    return synced


def _record_confirmed_buy_journal(
    journal_file: str,
    journal_enabled: bool,
    tk: str,
    pf: str,
    state: Dict[str, object],
    source: str,
    *,
    entry_source: str = "",
) -> None:
    if journal_enabled and journal_file:
        append_journal_event(
            journal_file,
            {
                "event": "confirm_buy",
                "ticker": tk,
                "source": source,
                "entry_source": entry_source or source,
                "portfolio_profile": pf,
                "size_usd": float(state.get("entry_notional_usd", 0.0) or 0.0),
                "entry_price_usd": float(state.get("entry_price_usd", 0.0) or 0.0),
                "stop_loss": float(state.get("stop_loss", 0.0) or 0.0) or None,
                "take_profit": float(state.get("take_profit", 0.0) or 0.0) or None,
            },
        )


def record_confirmed_sell(
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    ticker: str,
    *,
    exit_price_usd: Optional[float] = None,
    journal_file: str = "",
    journal_enabled: bool = True,
    source: str = "manual",
    exit_reason: Optional[str] = None,
    exit_price_source: Optional[str] = None,
) -> Tuple[Dict[str, object], Optional[float], Optional[float]]:
    """Enregistre une vente confirmee. Retourne (state, pnl_usd, pnl_pct)."""
    tk = normalize_ticker(ticker)
    state = normalize_position_state(positions.get(tk, {}))
    pf = infer_portfolio_profile_for_ticker(tk, state)
    if exit_price_usd is None or float(exit_price_usd) <= 0:
        px = get_reference_price_usd(tk)
        exit_price_usd = float(px) if px is not None else 0.0
    entry_price = float(state.get("entry_price_usd", 0.0) or 0.0)
    entry_notional = float(state.get("entry_notional_usd", 0.0) or 0.0)
    entry_source = str(state.get("entry_source", "") or "").strip()
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    if entry_notional > 0 and entry_price > 0 and exit_price_usd and exit_price_usd > 0:
        pnl_usd, pnl_pct = compute_closed_pnl_usd_pct(entry_notional, entry_price, float(exit_price_usd))
    state["in_position"] = False
    state["pending_buy"] = False
    state["pending_sell"] = False
    state["last_sell_alert_ts"] = 0.0
    state["entry_notional_usd"] = 0.0
    state["entry_price_usd"] = 0.0
    state["entry_confidence"] = 0
    state.pop("entry_source", None)
    state.pop("ibkr_sl_tp_active", None)
    state.pop("trailing_active", None)
    state.pop("trailing_floor_usd", None)
    state.pop("trailing_peak_usd", None)
    state.pop("overnight_runner", None)
    state.pop("intraday_runner_hold_notified", None)
    state.pop("intraday_loss_hold_notified", None)
    positions[tk] = state
    save_positions(position_file, positions)
    if journal_enabled and journal_file:
        append_journal_event(
            journal_file,
            {
                "event": "confirm_sell",
                "ticker": tk,
                "source": source,
                "portfolio_profile": pf,
                "size_usd": entry_notional,
                "exit_price_usd": float(exit_price_usd or 0.0),
            },
        )
        if pnl_usd is not None and pnl_pct is not None:
            closed_evt: Dict[str, object] = {
                    "event": "trade_closed",
                    "ticker": tk,
                    "source": source,
                    "portfolio_profile": pf,
                    "size_usd": entry_notional,
                    "entry_price_usd": entry_price,
                    "exit_price_usd": float(exit_price_usd or 0.0),
                    "pnl_usd": float(pnl_usd),
                    "pnl_pct": float(pnl_pct),
                }
            if entry_source:
                closed_evt["entry_source"] = entry_source
            if exit_reason:
                closed_evt["exit_reason"] = str(exit_reason).strip()
            if exit_price_source:
                closed_evt["exit_price_source"] = str(exit_price_source).strip()
            if journal_trade_closed_exists(journal_file, closed_evt):
                print(f"[Journal] trade_closed duplique ignore: {tk}")
            else:
                append_journal_event(journal_file, closed_evt)
    return state, pnl_usd, pnl_pct


def clear_rejected_buy_signal_state(
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    ticker: str,
    state: Dict[str, object],
) -> None:
    """Apres rejet IBKR auto: ne laisse pas un pending_buy fantome."""
    tk = normalize_ticker(ticker)
    state["pending_buy"] = False
    state["pending_sell"] = False
    positions[tk] = state
    save_positions(position_file, positions)


def attempt_ibkr_buy(
    *,
    ticker: str,
    notional_usd: float,
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    state: Dict[str, object],
    confidence: int,
    take_profit: Optional[float],
    stop_loss: Optional[float],
    journal_file: str,
    journal_enabled: bool,
    bot_token: str,
    chat_id: str,
    current_price_hint: Optional[float] = None,
    justification: str = "",
) -> bool:
    """
    Tente un achat marche IBKR et enregistre la position si rempli.
    Retourne True si execute, False si fallback manuel requis.
    """
    from broker_hooks import ibkr_fallback_to_manual, try_ibkr_buy

    result, status_line = try_ibkr_buy(
        ticker,
        notional_usd,
        stop_loss=stop_loss,
        take_profit=take_profit,
        signal_price=current_price_hint,
    )
    if result is None:
        return False
    if result.ok:
        fill_sl = result.stop_loss_price if result.stop_loss_price > 0 else stop_loss
        fill_tp = result.take_profit_price if result.take_profit_price > 0 else take_profit
        record_confirmed_buy(
            positions,
            position_file,
            ticker,
            notional_usd=result.notional_usd,
            entry_price_usd=result.fill_price,
            confidence=confidence,
            take_profit=fill_tp,
            stop_loss=fill_sl,
            journal_file=journal_file,
            journal_enabled=journal_enabled,
            source="ibkr_auto",
            entry_source="ibkr_auto",
        )
        if result.sl_tp_placed:
            mark_ibkr_sl_tp_active(
                positions,
                position_file,
                ticker,
                sl_px=result.stop_loss_price,
                tp_px=result.take_profit_price,
            )
        if journal_enabled and journal_file:
            append_journal_event(
                journal_file,
                {
                    "event": "ibkr_order_filled",
                    "ticker": normalize_ticker(ticker),
                    "side": "BUY",
                    "quantity": result.quantity,
                    "fill_price_usd": result.fill_price,
                    "notional_usd": result.notional_usd,
                    "order_id": result.order_id,
                    "stop_loss": result.stop_loss_price,
                    "take_profit": result.take_profit_price,
                    "sl_tp_placed": result.sl_tp_placed,
                    "order_status": result.order_status,
                    "partial_fill": result.partial_fill,
                    "warning": result.warning,
                    "retried": result.retried,
                },
            )
        if bot_token and chat_id:
            justif = (justification or "").strip()
            if not justif:
                justif = "Confluence technique validee par le bot (mode auto IBKR)."
            px_hint = float(current_price_hint) if current_price_hint and current_price_hint > 0 else float(result.fill_price)
            qty_txt = ""
            if result.quantity and float(result.quantity) >= 1:
                qty_txt = f"Quantite: {int(float(result.quantity))} action(s)\n"
            msg = (
                f"ACHAT AUTO IBKR — {normalize_ticker(ticker)}\n"
                f"{qty_txt}"
                f"Prix action (USD): {result.fill_price:.2f} (ref signal {px_hint:.2f})\n"
                f"Montant investi (USD): {result.notional_usd:.2f}\n"
                f"SL/TP (USD): {result.stop_loss_price:.2f} / {result.take_profit_price:.2f}\n"
                f"Justification: {justif}"
            )
            if result.warning:
                msg += f"\nNote: {result.warning}"
            send_telegram_alert(bot_token, chat_id, msg)
        print(f"[IBKR] {status_line}")
        return True
    print(f"[IBKR] Echec achat {ticker}: {result.error}")
    clear_rejected = os.getenv("IBKR_CLEAR_PENDING_ON_REJECT", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    if clear_rejected:
        clear_rejected_buy_signal_state(positions, position_file, ticker, state)
    if journal_enabled and journal_file:
        append_journal_event(
            journal_file,
            {
                "event": "ibkr_order_failed",
                "ticker": normalize_ticker(ticker),
                "side": "BUY",
                "error": result.error,
                "order_status": result.order_status,
                "ib_log_tail": result.ib_log_tail,
                "requested_notional_usd": notional_usd,
                "retried": result.retried,
                "pending_cleared": clear_rejected,
            },
        )
    if ibkr_fallback_to_manual() and not clear_rejected:
        if bot_token and chat_id:
            send_telegram_alert(
                bot_token,
                chat_id,
                f"IBKR achat echoue ({ticker}): {result.error}\n"
                f"Passe manuellement ou /confirm_buy {ticker} {notional_usd:.0f}",
            )
        return False
    if bot_token and chat_id:
        send_telegram_alert(
            bot_token,
            chat_id,
            f"❌ IBKR ACHAT REJETE — {normalize_ticker(ticker)}\n{result.error}\n"
            f"Slot libere (pas de pending_buy). Verifie Gateway / buying power / marche ouvert.",
        )
    return False


def attempt_ibkr_sell(
    *,
    ticker: str,
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    state: Optional[Dict[str, object]] = None,
    journal_file: str,
    journal_enabled: bool,
    bot_token: str,
    chat_id: str,
    reason: str = "",
    exit_reason: Optional[str] = None,
    min_limit_price: Optional[float] = None,
    force_outside_rth: bool = False,
) -> bool:
    """Tente une vente IBKR (marche ou limite plancher). Retourne True si execute."""
    from broker_hooks import ibkr_fallback_to_manual, try_ibkr_sell

    tk = normalize_ticker(ticker)
    if sync_ticker_closed_at_ibkr(
        tk,
        positions,
        position_file,
        journal_file=journal_file,
        journal_enabled=journal_enabled,
        bot_token=bot_token,
        chat_id=chat_id,
        notify_telegram=False,
    ):
        print(f"[IBKR] {tk}: vente inutile — position deja fermee chez IBKR (SL/TP ou ordre execute).")
        return True

    result, status_line = try_ibkr_sell(
        ticker,
        min_limit_price=min_limit_price,
        force_outside_rth=force_outside_rth,
    )
    if result is None:
        return False
    if result.ok:
        xr = (exit_reason or reason or "ibkr_auto").strip()
        _, pnl_usd, pnl_pct = record_confirmed_sell(
            positions,
            position_file,
            ticker,
            exit_price_usd=result.fill_price,
            journal_file=journal_file,
            journal_enabled=journal_enabled,
            source="ibkr_auto",
            exit_reason=xr,
        )
        if journal_enabled and journal_file:
            append_journal_event(
                journal_file,
                {
                    "event": "ibkr_order_filled",
                    "ticker": normalize_ticker(ticker),
                    "side": "SELL",
                    "quantity": result.quantity,
                    "fill_price_usd": result.fill_price,
                    "notional_usd": result.notional_usd,
                    "order_id": result.order_id,
                    "reason": reason,
                },
            )
        pnl_txt = ""
        if pnl_usd is not None and pnl_pct is not None:
            pnl_txt = f"\nPnL estime: {pnl_usd:+.2f} USD ({pnl_pct:+.2f}%)"
        if bot_token and chat_id:
            send_telegram_alert(
                bot_token,
                chat_id,
                f"VENTE AUTO IBKR — {normalize_ticker(ticker)}\n{status_line}{pnl_txt}",
            )
        print(f"[IBKR] {status_line}{pnl_txt}")
        return True
    err = str(result.error or "")
    if "qty=0" in err:
        if sync_ticker_closed_at_ibkr(
            tk,
            positions,
            position_file,
            journal_file=journal_file,
            journal_enabled=journal_enabled,
            bot_token=bot_token,
            chat_id=chat_id,
            notify_telegram=True,
        ):
            print(f"[IBKR] {tk}: desync corrigee apres echec vente (deja flat IBKR).")
            return True
    print(f"[IBKR] Echec vente {ticker}: {result.error}")
    if journal_enabled and journal_file:
        append_journal_event(
            journal_file,
            {
                "event": "ibkr_order_failed",
                "ticker": tk,
                "side": "SELL",
                "error": result.error,
                "order_status": result.order_status,
                "ib_log_tail": result.ib_log_tail,
                "reason": reason,
                "retried": result.retried,
            },
        )
    if bot_token and chat_id:
        send_telegram_alert(
            bot_token,
            chat_id,
            f"❌ IBKR VENTE REJETEE — {tk}\n{result.error}\n"
            f"Position locale conservee. /confirm_sell {tk} PRIX_SORTIE si execute manuellement.",
        )
    return False


def send_telegram_alert(bot_token: str, chat_id: str, message: str) -> None:
    """Envoie une alerte Telegram via l'API officielle."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            response = requests.post(url, json=payload, timeout=20)
            response.raise_for_status()
            return
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Echec envoi Telegram apres 3 tentatives: {last_error}")


def load_offset(path: str) -> int:
    """Charge le dernier update_id Telegram traité."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("offset"), int):
            return data["offset"]
    except FileNotFoundError:
        return 0
    except Exception:
        return 0
    return 0


def save_offset(path: str, offset: int) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"offset": int(offset)}, f)
    os.replace(tmp, path)


def poll_telegram_confirmations(
    bot_token: str,
    allowed_chat_id: str,
    position_file: str,
    offset_file: str,
    positions: Dict[str, dict],
) -> Dict[str, dict]:
    """
    Lit les commandes Telegram (ex: /confirm_buy TSLA) et met a jour positions.json.
    Le bot ne fait pas de webhook : on passe par polling (getUpdates).
    """
    journal_file = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
    offset = load_offset(offset_file)
    max_update_id = offset
    try:
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
        params = {"timeout": 1, "limit": 10, "offset": offset}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return positions
        updates = data.get("result") or []
        if not updates:
            return positions

        updated_any = False

        for upd in updates:
            update_id = upd.get("update_id")
            if isinstance(update_id, int) and update_id >= max_update_id:
                max_update_id = update_id + 1

            message = upd.get("message") or upd.get("edited_message") or {}
            text = (message.get("text") or "").strip()
            if not text:
                continue

            chat = message.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            if allowed_chat_id and chat_id and chat_id != str(allowed_chat_id):
                if text.strip().lower().startswith(("/confirm_", "/confirm", "/sell", "/buy")):
                    print(
                        f"[Telegram] Message ignore: chat_id recu={chat_id!r} "
                        f"!= TELEGRAM_CHAT_ID attendu={str(allowed_chat_id)!r}. "
                        "Ajuste .env ou envoie depuis le bon chat (PV vs groupe)."
                    )
                continue

            parts = text.split()
            if not parts:
                continue

            cmd = parts[0].lower()
            if "@" in cmd:
                cmd = cmd.split("@", 1)[0]  # /confirm_buy@MyBot -> /confirm_buy

            if handle_swap_telegram_reply(
                cmd=cmd,
                text_lower=text.lower(),
                parts=parts,
                positions=positions,
                position_file=position_file,
                bot_token=bot_token,
                chat_id=chat_id,
            ):
                updated_any = True
                continue

            if cmd in {"/status", "/portfolio", "/etat", "/state"}:
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                try:
                    max_open_positions = int(os.getenv("TRADING_MAX_OPEN_POSITIONS", "2"))
                except ValueError:
                    max_open_positions = 2
                status_msg = "Etat portefeuille:\n" + build_portfolio_snapshot(
                    positions,
                    budget_usd=budget_usd,
                    max_open_positions=max_open_positions,
                    journal_file=journal_file,
                )
                try:
                    send_telegram_alert(bot_token, chat_id, status_msg)
                except Exception:
                    pass
                continue

            if cmd in {"/cockpit", "/dashboard", "/live"}:
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                try:
                    max_open_positions = int(os.getenv("TRADING_MAX_OPEN_POSITIONS", "2"))
                except ValueError:
                    max_open_positions = 2
                cockpit_msg = build_cockpit_snapshot(
                    positions,
                    budget_usd=budget_usd,
                    max_open_positions=max_open_positions,
                    journal_file=journal_file,
                )
                try:
                    send_telegram_alert(bot_token, chat_id, cockpit_msg)
                except Exception as exc:
                    print(f"[Telegram] Echec envoi /cockpit: {exc}")
                continue

            if cmd in {"/report", "/perf", "/stats", "/performance"}:
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                report_msg = build_performance_report(
                    journal_file,
                    positions,
                    budget_usd=budget_usd,
                    now_ny=datetime.now(MARKET_TZ),
                )
                try:
                    send_telegram_alert(bot_token, chat_id, report_msg)
                except Exception as exc:
                    print(f"[Telegram] Echec envoi /report: {exc}")
                continue

            if cmd in {"/daily_report", "/daily", "/jour"}:
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                objective_days_raw = os.getenv("TRADING_OBJECTIVE_DAYS", "").strip()
                try:
                    horizon_days_fallback = int(os.getenv("TRADING_HORIZON_DAYS", "7"))
                except ValueError:
                    horizon_days_fallback = 7
                if objective_days_raw:
                    try:
                        objective_days = int(objective_days_raw)
                    except ValueError:
                        objective_days = horizon_days_fallback
                else:
                    objective_days = horizon_days_fallback
                daily_msg = build_daily_progress_report(
                    journal_file,
                    positions,
                    base_budget_usd=budget_usd,
                    objective_days=max(1, objective_days),
                    now_ny=datetime.now(MARKET_TZ),
                )
                try:
                    send_telegram_alert(bot_token, chat_id, daily_msg)
                except Exception as exc:
                    print(f"[Telegram] Echec envoi /daily_report: {exc}")
                continue

            if cmd in {"/weekly_report", "/weekly", "/hebdo", "/semaine"}:
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                weekly_msg = build_window_progress_report(
                    journal_file,
                    positions,
                    base_budget_usd=budget_usd,
                    window_days=7,
                    title="RAPPORT HEBDOMADAIRE",
                    now_ny=datetime.now(MARKET_TZ),
                )
                try:
                    send_telegram_alert(bot_token, chat_id, weekly_msg)
                except Exception as exc:
                    print(f"[Telegram] Echec envoi /weekly_report: {exc}")
                continue

            if cmd in {"/monthly_report", "/monthly", "/mois"}:
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                monthly_msg = build_window_progress_report(
                    journal_file,
                    positions,
                    base_budget_usd=budget_usd,
                    window_days=30,
                    title="RAPPORT MENSUEL",
                    now_ny=datetime.now(MARKET_TZ),
                )
                try:
                    send_telegram_alert(bot_token, chat_id, monthly_msg)
                except Exception as exc:
                    print(f"[Telegram] Echec envoi /monthly_report: {exc}")
                continue

            if cmd in {"/plan", "/premarket", "/preopen", "/plan_preopen"}:
                yf_period = os.getenv("YF_PERIOD", "5d").strip()
                yf_interval = os.getenv("YF_INTERVAL", "15m").strip()
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                objective_days_raw = os.getenv("TRADING_OBJECTIVE_DAYS", "").strip()
                try:
                    horizon_days_fallback = int(os.getenv("TRADING_HORIZON_DAYS", "7"))
                except ValueError:
                    horizon_days_fallback = 7
                if objective_days_raw:
                    try:
                        objective_days = int(objective_days_raw)
                    except ValueError:
                        objective_days = horizon_days_fallback
                else:
                    objective_days = horizon_days_fallback
                try:
                    premarket_window_min = float(os.getenv("PREMARKET_PREP_WINDOW_MIN", "120"))
                except ValueError:
                    premarket_window_min = 120.0
                try:
                    premarket_max_tickers = int(os.getenv("PREMARKET_PREP_MAX_TICKERS", "3"))
                except ValueError:
                    premarket_max_tickers = 3
                premarket_state_file = (
                    os.getenv("PREMARKET_PREP_STATE_FILE", PREMARKET_PREP_STATE_FILE_DEFAULT).strip()
                    or PREMARKET_PREP_STATE_FILE_DEFAULT
                )
                manage_open_tickers = os.getenv("TICKERS_MANAGE_OPEN", "1").strip().lower() in {"1", "true", "yes", "y"}
                base_tickers = get_tickers_from_env()
                tickers = build_scan_tickers(
                    base_tickers,
                    positions,
                    include_open_positions=manage_open_tickers,
                )
                sent = run_premarket_preparation(
                    tickers=tickers,
                    yf_period=yf_period,
                    yf_interval=yf_interval,
                    telegram_token=bot_token,
                    telegram_chat_id=chat_id,
                    position_file=position_file,
                    journal_file=journal_file,
                    base_budget_usd=budget_usd,
                    objective_days=max(1, objective_days),
                    send_daily_report=False,
                    state_file=premarket_state_file,
                    window_minutes=max(15.0, premarket_window_min),
                    max_items=max(1, premarket_max_tickers),
                    force_send=True,
                )
                if not sent:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            "Impossible de generer le plan pour le moment (verifie Yahoo/Telegram/config).",
                        )
                    except Exception:
                        pass
                continue

            if cmd in {"/portfolio_reports", "/portfolios_report", "/report_pf_all", "/pf_all"}:
                scoreboard_msg = build_portfolio_profiles_scoreboard(
                    journal_file,
                    now_ny=datetime.now(MARKET_TZ),
                )
                try:
                    send_telegram_alert(bot_token, chat_id, scoreboard_msg)
                except Exception as exc:
                    print(f"[Telegram] Echec envoi /report_pf_all: {exc}")
                continue

            if cmd in {"/report_pf", "/report-portfolio", "/portfolio_report", "/pf"}:
                profile_arg = normalize_portfolio_profile_key(
                    parts[1] if len(parts) >= 2 else get_active_portfolio_profile_key()
                )
                allowed_profiles = {"current"} | set(PORTFOLIO_PROFILES.keys())
                if not profile_arg or profile_arg not in allowed_profiles:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            (
                                "Commande incomplete. Exemple: /report_pf turbo_beta\n"
                                "Profils: current, turbo_beta, legacy_open\n"
                                "Scoreboard global: /report_pf_all"
                            ),
                        )
                    except Exception:
                        pass
                    continue
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                pf_msg = build_portfolio_profile_performance_report(
                    journal_file,
                    profile_arg,
                    budget_usd=budget_usd,
                    now_ny=datetime.now(MARKET_TZ),
                )
                try:
                    send_telegram_alert(bot_token, chat_id, pf_msg)
                except Exception as exc:
                    print(f"[Telegram] Echec envoi /report_pf: {exc}")
                continue

            if cmd in {"/daily_pf", "/daily-portfolio", "/pf_daily"}:
                profile_arg = normalize_portfolio_profile_key(
                    parts[1] if len(parts) >= 2 else get_active_portfolio_profile_key()
                )
                allowed_profiles = {"current"} | set(PORTFOLIO_PROFILES.keys())
                if not profile_arg or profile_arg not in allowed_profiles:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            (
                                "Exemple: /daily_pf turbo_beta\n"
                                "Profils: current, turbo_beta, legacy_open\n"
                                "Si aucun profil n'est passe, le bot utilise le profil actif de session."
                            ),
                        )
                    except Exception:
                        pass
                    continue
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                daily_pf_msg = build_portfolio_profile_daily_report(
                    journal_file,
                    profile_arg,
                    budget_usd=budget_usd,
                    now_ny=datetime.now(MARKET_TZ),
                )
                try:
                    send_telegram_alert(bot_token, chat_id, daily_pf_msg)
                except Exception as exc:
                    print(f"[Telegram] Echec envoi /daily_pf: {exc}")
                continue

            if cmd in {"/weekly_pf", "/weekly-portfolio", "/pf_weekly"}:
                profile_arg = normalize_portfolio_profile_key(
                    parts[1] if len(parts) >= 2 else get_active_portfolio_profile_key()
                )
                allowed_profiles = {"current"} | set(PORTFOLIO_PROFILES.keys())
                if not profile_arg or profile_arg not in allowed_profiles:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            "Exemple: /weekly_pf turbo_beta (profils: current, turbo_beta, legacy_open).",
                        )
                    except Exception:
                        pass
                    continue
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                weekly_pf_msg = build_portfolio_profile_window_report(
                    journal_file,
                    profile_arg,
                    budget_usd=budget_usd,
                    window_days=7,
                    title="RAPPORT HEBDOMADAIRE PAR PORTEFEUILLE",
                    now_ny=datetime.now(MARKET_TZ),
                )
                try:
                    send_telegram_alert(bot_token, chat_id, weekly_pf_msg)
                except Exception as exc:
                    print(f"[Telegram] Echec envoi /weekly_pf: {exc}")
                continue

            if cmd in {"/monthly_pf", "/monthly-portfolio", "/pf_monthly"}:
                profile_arg = normalize_portfolio_profile_key(
                    parts[1] if len(parts) >= 2 else get_active_portfolio_profile_key()
                )
                allowed_profiles = {"current"} | set(PORTFOLIO_PROFILES.keys())
                if not profile_arg or profile_arg not in allowed_profiles:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            "Exemple: /monthly_pf turbo_beta (profils: current, turbo_beta, legacy_open).",
                        )
                    except Exception:
                        pass
                    continue
                try:
                    budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    budget_usd = 1000.0
                monthly_pf_msg = build_portfolio_profile_window_report(
                    journal_file,
                    profile_arg,
                    budget_usd=budget_usd,
                    window_days=30,
                    title="RAPPORT MENSUEL PAR PORTEFEUILLE",
                    now_ny=datetime.now(MARKET_TZ),
                )
                try:
                    send_telegram_alert(bot_token, chat_id, monthly_pf_msg)
                except Exception as exc:
                    print(f"[Telegram] Echec envoi /monthly_pf: {exc}")
                continue

            if cmd in {"/fix_ticker", "/fix-ticker", "/rename_ticker", "/rename-ticker", "/typo"}:
                wrong_ticker = normalize_ticker(parts[1] if len(parts) >= 2 else "")
                right_ticker = normalize_ticker(parts[2] if len(parts) >= 3 else "")
                if not wrong_ticker or not right_ticker:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            "Commande incomplete. Exemple: /fix_ticker SMCU SMCI",
                        )
                    except Exception:
                        pass
                    continue
                if wrong_ticker == right_ticker:
                    try:
                        send_telegram_alert(bot_token, chat_id, "Aucune correction: les deux tickers sont identiques.")
                    except Exception:
                        pass
                    continue

                wrong_state = normalize_position_state(positions.get(wrong_ticker, {}))
                right_state = normalize_position_state(positions.get(right_ticker, {}))
                wrong_active = bool(wrong_state.get("in_position")) or bool(wrong_state.get("pending_buy")) or bool(
                    wrong_state.get("pending_sell")
                )
                right_active = bool(right_state.get("in_position")) or bool(right_state.get("pending_buy")) or bool(
                    right_state.get("pending_sell")
                )
                if wrong_active and right_active:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            (
                                f"Correction refusee: {wrong_ticker} et {right_ticker} ont tous les deux un etat actif. "
                                "Regle d'abord les positions manuellement."
                            ),
                        )
                    except Exception:
                        pass
                    continue

                positions_changed = False
                if wrong_ticker in positions:
                    wrong_raw = dict(positions.get(wrong_ticker, {}))
                    if right_ticker in positions:
                        merged = dict(positions.get(right_ticker, {}))
                        if wrong_active:
                            merged.update(wrong_raw)
                        else:
                            for k, v in wrong_raw.items():
                                if k not in merged:
                                    merged[k] = v
                        positions[right_ticker] = merged
                    else:
                        positions[right_ticker] = wrong_raw
                    positions.pop(wrong_ticker, None)
                    save_positions(position_file, positions)
                    positions_changed = True

                journal_changed = rewrite_journal_ticker(journal_file, wrong_ticker, right_ticker)
                if positions_changed or journal_changed > 0:
                    try:
                        append_journal_event(
                            journal_file,
                            {
                                "event": "ticker_renamed",
                                "source": "telegram",
                                "old_ticker": wrong_ticker,
                                "new_ticker": right_ticker,
                                "journal_rows_changed": int(journal_changed),
                            },
                        )
                    except Exception:
                        pass
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            (
                                f"Correction appliquee: {wrong_ticker} -> {right_ticker}. "
                                f"positions={'oui' if positions_changed else 'non'}, "
                                f"journal={journal_changed} ligne(s)."
                            ),
                        )
                    except Exception:
                        pass
                else:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            f"Aucune entree trouvee pour {wrong_ticker} a corriger vers {right_ticker}.",
                        )
                    except Exception:
                        pass
                continue

            if cmd in {"/edit_entry", "/edit-entry", "/set_entry", "/set-entry", "/corriger_entree", "/corriger-entree"}:
                notional_kv, parts_f = strip_edit_entry_kv_tokens(parts)
                t_edit = normalize_ticker(parts_f[1] if len(parts_f) >= 2 else "")
                if not t_edit or len(parts_f) < 2:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            (
                                "Commande incomplete. Forme voulue:\n"
                                f"/edit_entry TICKER MONTANT_USD PRIX_ACHAT_PAR_ACTION\n"
                                f"ex: /edit_entry FCX 181 69.99\n"
                                f"Option: 3e nombre = autre cotation, puis date ISO d'achat.\n"
                                f"Raccourci: /edit_entry TICKER notional=500 (seul le deploye)\n"
                                f"1 seul nombre: prix d'entree (par action) si tu corriges sans toucher au deploye."
                            ),
                        )
                    except Exception:
                        pass
                    continue

                st_edit = normalize_position_state(positions.get(t_edit, {}))
                if not bool(st_edit.get("in_position")):
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            f"Pas de position ouverte enregistree pour {t_edit} (in_position=0).",
                        )
                    except Exception:
                        pass
                    continue

                data_args = parts_f[2:]

                # /edit_entry TICKER notional=xxx — uniquement le deploye
                if not data_args and notional_kv is not None:
                    st_edit["entry_notional_usd"] = float(notional_kv)
                    positions[t_edit] = st_edit
                    updated_any = True
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            f"Notionnel corrige pour {t_edit}: {notional_kv:.2f} USD (deploye).",
                        )
                    except Exception as exc:
                        print(f"[Telegram] Echec reponse /edit_entry: {exc}")
                    try:
                        append_journal_event(
                            journal_file,
                            {
                                "event": "entry_edited",
                                "ticker": t_edit,
                                "source": "telegram",
                                "entry_notional_usd": float(notional_kv),
                            },
                        )
                    except Exception:
                        pass
                    continue

                if not data_args:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            f"Renseigne le montant deploye (USD) puis le prix d'achat, ex: /edit_entry {t_edit} 181 69.99",
                        )
                    except Exception:
                        pass
                    continue

                # Un seul nombre: uniquement le cours d'entree (par action), deploiement inchange
                if len(data_args) == 1 and notional_kv is None:
                    p_only = parse_price_value(data_args[0])
                    if p_only is None:
                        try:
                            send_telegram_alert(
                                bot_token,
                                chat_id,
                                f"Prix par action invalide pour {t_edit}.",
                            )
                        except Exception:
                            pass
                        continue
                    st_edit["entry_price_usd"] = float(p_only)
                    positions[t_edit] = st_edit
                    updated_any = True
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            f"Entree corrigee pour {t_edit}: prix achat (par action) {p_only:.2f} USD (deploiement inchange).",
                        )
                    except Exception as exc:
                        print(f"[Telegram] Echec reponse /edit_entry: {exc}")
                    try:
                        append_journal_event(
                            journal_file,
                            {
                                "event": "entry_edited",
                                "ticker": t_edit,
                                "source": "telegram",
                                "entry_price_usd": float(p_only),
                            },
                        )
                    except Exception:
                        pass
                    continue

                # Un seul nombre + notional= dans le texte: deploye + prix d'achat
                if len(data_args) == 1 and notional_kv is not None:
                    p_with_n = parse_price_value(data_args[0])
                    if p_with_n is None:
                        try:
                            send_telegram_alert(
                                bot_token,
                                chat_id,
                                f"Prix d'achat (par action) invalide apres le ticker. Ex: /edit_entry {t_edit} 69.99 notional=181",
                            )
                        except Exception:
                            pass
                        continue
                    st_edit["entry_notional_usd"] = float(notional_kv)
                    st_edit["entry_price_usd"] = float(p_with_n)
                    positions[t_edit] = st_edit
                    updated_any = True
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            f"Entree corrigee pour {t_edit}: deploye {notional_kv:.2f} USD, prix achat {p_with_n:.2f} USD/action.",
                        )
                    except Exception as exc:
                        print(f"[Telegram] Echec reponse /edit_entry: {exc}")
                    try:
                        append_journal_event(
                            journal_file,
                            {
                                "event": "entry_edited",
                                "ticker": t_edit,
                                "source": "telegram",
                                "entry_notional_usd": float(notional_kv),
                                "entry_price_usd": float(p_with_n),
                            },
                        )
                    except Exception:
                        pass
                    continue

                # Regle: deploye (USD) puis prix achat (par action) ; si notional= en kv, 1er nombre = prix achat, reste = 3e prix / heure
                mkt2: Optional[float] = None
                ts2: Optional[str] = None
                if notional_kv is not None:
                    n_res = float(notional_kv)
                    new_entry = parse_price_value(data_args[0])
                    mkt2, ts2 = parse_edit_entry_trailing(data_args[1:])
                else:
                    n_res_num = parse_notional_usd(data_args[0])
                    if n_res_num is None:
                        try:
                            send_telegram_alert(
                                bot_token,
                                chat_id,
                                f"1er nombre = montant deploye (USD), ex. 181 (ou utilise notional=).",
                            )
                        except Exception:
                            pass
                        continue
                    n_res = float(n_res_num)
                    new_entry = parse_price_value(data_args[1])
                    mkt2, ts2 = parse_edit_entry_trailing(data_args[2:])
                if new_entry is None:
                    try:
                        send_telegram_alert(
                            bot_token,
                            chat_id,
                            f"Prix d'achat (par action) invalide. Forme: /edit_entry {t_edit} 181 69.99",
                        )
                    except Exception:
                        pass
                    continue
                st_edit["entry_notional_usd"] = float(n_res)
                st_edit["entry_price_usd"] = float(new_entry)
                if mkt2 is not None:
                    st_edit["entry_market_price_usd"] = float(mkt2)
                if ts2 is not None:
                    st_edit["entry_ts_utc"] = ts2
                positions[t_edit] = st_edit
                updated_any = True
                try:
                    details = (
                        f"Deploye: {float(n_res):.2f} USD. Prix achat (par action): {float(new_entry):.2f} USD."
                    )
                    m_show = float(st_edit.get("entry_market_price_usd", 0.0) or 0.0)
                    if mkt2 is not None and m_show > 0:
                        details += f" Cours a l'ordre: {m_show:.2f} USD."
                    ts_show = st_edit.get("entry_ts_utc")
                    if ts2 is not None and ts_show:
                        details += f" Heure: {ts_show}."
                    send_telegram_alert(
                        bot_token,
                        chat_id,
                        f"Entree corrigee pour {t_edit}. {details}",
                    )
                except Exception as exc:
                    print(f"[Telegram] Echec reponse /edit_entry: {exc}")
                try:
                    journal_event: Dict[str, object] = {
                        "event": "entry_edited",
                        "ticker": t_edit,
                        "source": "telegram",
                        "entry_notional_usd": float(n_res),
                        "entry_price_usd": float(new_entry),
                        "entry_market_price_usd": float(st_edit.get("entry_market_price_usd", 0.0) or 0.0),
                        "entry_ts_utc": st_edit.get("entry_ts_utc"),
                    }
                    append_journal_event(journal_file, journal_event)
                except Exception:
                    pass
                continue

            ticker_arg = parts[1] if len(parts) >= 2 else ""
            ticker_arg = normalize_ticker(ticker_arg)
            if not ticker_arg:
                try:
                    send_telegram_alert(bot_token, chat_id, "Commande incomplete. Exemple: /confirm_buy TSLA 500")
                except Exception:
                    pass
                continue

            allowed_tickers = get_allowed_command_tickers(positions)
            if ticker_arg not in allowed_tickers:
                suggestion = suggest_ticker_typo(ticker_arg, allowed_tickers)
                hint = (
                    f" Ticker proche: {suggestion}. Corrige avec /fix_ticker {ticker_arg} {suggestion} si besoin."
                    if suggestion
                    else ""
                )
                try:
                    send_telegram_alert(
                        bot_token,
                        chat_id,
                        f"Ticker inconnu: {ticker_arg}. Autorises: {', '.join(allowed_tickers)}.{hint}",
                    )
                except Exception:
                    pass
                continue

            if cmd in {"/confirm_buy", "/confirm-buy", "/buy", "/confirmbuy"}:
                prev = normalize_position_state(positions.get(ticker_arg, {}))
                portfolio_profile = infer_portfolio_profile_for_ticker(ticker_arg, prev)
                if bool(prev.get("in_position")):
                    try:
                        send_telegram_alert(
                            bot_token, chat_id,
                            f"Position deja ouverte pour {ticker_arg}. /confirm_buy ignore (doublon).",
                        )
                    except Exception:
                        pass
                    continue
                notional_arg = parts[2] if len(parts) >= 3 else ""
                confirmed_notional = parse_notional_usd(notional_arg) if notional_arg else None
                entry_price_arg = parts[3] if len(parts) >= 4 else ""
                confirmed_entry_price = parse_price_value(entry_price_arg) if entry_price_arg else None
                if confirmed_entry_price is None:
                    confirmed_entry_price = get_reference_price_usd(ticker_arg)
                prev["in_position"] = True
                prev["pending_buy"] = False
                prev["pending_sell"] = False
                prev["portfolio_profile"] = portfolio_profile
                prev["last_sell_alert_ts"] = 0.0
                if confirmed_notional is not None:
                    prev["entry_notional_usd"] = confirmed_notional
                if confirmed_entry_price is not None:
                    prev["entry_price_usd"] = confirmed_entry_price
                prev["entry_ts_utc"] = utc_now_iso_z()
                positions[ticker_arg] = prev
                updated_any = True
                try:
                    msg = f"Confirmation ACHAT enregistree pour {ticker_arg}."
                    if confirmed_notional is not None:
                        msg += f" Notionnel: {confirmed_notional:.2f} USD."
                    if confirmed_entry_price is not None:
                        msg += f" Prix ref: {confirmed_entry_price:.2f} USD."
                    send_telegram_alert(bot_token, chat_id, msg)
                except Exception as exc:
                    print(f"[Telegram] Confirmation envoyee en fichier mais echec reponse Telegram: {exc}")
                try:
                    append_journal_event(
                        journal_file,
                        {
                            "event": "confirm_buy",
                            "ticker": ticker_arg,
                            "source": "telegram",
                            "portfolio_profile": portfolio_profile,
                            "size_usd": float(prev.get("entry_notional_usd", 0.0) or 0.0),
                            "entry_price_usd": float(prev.get("entry_price_usd", 0.0) or 0.0),
                        },
                    )
                except Exception:
                    pass

            elif cmd in {"/confirm_sell", "/confirm-sell", "/sell", "/confirmsell"}:
                prev = normalize_position_state(positions.get(ticker_arg, {}))
                portfolio_profile = infer_portfolio_profile_for_ticker(ticker_arg, prev)
                exit_price_arg = parts[2] if len(parts) >= 3 else ""
                confirmed_exit_price = parse_price_value(exit_price_arg) if exit_price_arg else None
                if confirmed_exit_price is None:
                    confirmed_exit_price = get_reference_price_usd(ticker_arg)
                entry_price = float(prev.get("entry_price_usd", 0.0) or 0.0)
                entry_notional = float(prev.get("entry_notional_usd", 0.0) or 0.0)
                pnl_basis_note = ""
                if len(parts) >= 5:
                    inv_ov = parse_notional_usd(parts[3])
                    px_ov = parse_price_value(parts[4])
                    if inv_ov is not None and px_ov is not None and inv_ov > 0 and px_ov > 0:
                        entry_notional = inv_ov
                        entry_price = px_ov
                        pnl_basis_note = (
                            f" — base releve vente: {entry_notional:.2f} USD @ {entry_price:.2f} / action"
                        )
                pnl_usd: Optional[float] = None
                pnl_pct: Optional[float] = None
                if (
                    entry_price > 0
                    and entry_notional > 0
                    and confirmed_exit_price is not None
                    and confirmed_exit_price > 0
                ):
                    pnl_usd, pnl_pct = compute_closed_pnl_usd_pct(
                        entry_notional, entry_price, confirmed_exit_price
                    )
                prev["in_position"] = False
                prev["pending_buy"] = False
                prev["pending_sell"] = False
                prev["last_sell_alert_ts"] = 0.0
                prev["entry_notional_usd"] = 0.0
                prev["entry_price_usd"] = 0.0
                prev["entry_confidence"] = 0
                positions[ticker_arg] = prev
                updated_any = True
                try:
                    msg = f"Confirmation VENTE enregistree pour {ticker_arg}."
                    if confirmed_exit_price is not None:
                        msg += f" Prix ref sortie: {confirmed_exit_price:.2f} USD."
                    if pnl_usd is not None and pnl_pct is not None:
                        msg += (
                            f" PnL estime: {pnl_usd:+.2f} USD ({pnl_pct:+.2f}% / capital deploye"
                            f"{pnl_basis_note})."
                        )
                    send_telegram_alert(bot_token, chat_id, msg)
                except Exception as exc:
                    print(
                        f"[Telegram] Vente enregistree dans positions.json mais echec envoi message Telegram: {exc}"
                    )
                try:
                    append_journal_event(
                        journal_file,
                        {
                            "event": "confirm_sell",
                            "ticker": ticker_arg,
                            "source": "telegram",
                            "portfolio_profile": portfolio_profile,
                            "size_usd": entry_notional,
                            "exit_price_usd": float(confirmed_exit_price or 0.0),
                        },
                    )
                    if pnl_usd is not None and pnl_pct is not None:
                        closed_evt = {
                                "event": "trade_closed",
                                "ticker": ticker_arg,
                                "source": "telegram",
                                "portfolio_profile": portfolio_profile,
                                "size_usd": entry_notional,
                                "entry_price_usd": entry_price,
                                "exit_price_usd": float(confirmed_exit_price or 0.0),
                                "pnl_usd": float(pnl_usd),
                                "pnl_pct": float(pnl_pct),
                            }
                        if not journal_trade_closed_exists(journal_file, closed_evt):
                            append_journal_event(journal_file, closed_evt)
                except Exception:
                    pass

            elif cmd in {"/set_tp", "/set-tp", "/tp"}:
                price_arg = parts[2] if len(parts) >= 3 else ""
                tp_price = parse_price_value(price_arg)
                if tp_price is None:
                    try:
                        send_telegram_alert(bot_token, chat_id, f"Format invalide. Exemple: /set_tp {ticker_arg} 172.5")
                    except Exception:
                        pass
                    continue
                prev = normalize_position_state(positions.get(ticker_arg, {}))
                prev["take_profit"] = tp_price
                positions[ticker_arg] = prev
                updated_any = True
                try:
                    send_telegram_alert(bot_token, chat_id, f"Take profit defini pour {ticker_arg}: {tp_price:.2f} USD")
                except Exception:
                    pass

            elif cmd in {"/clear_tp", "/clear-tp"}:
                prev = normalize_position_state(positions.get(ticker_arg, {}))
                prev["take_profit"] = None
                positions[ticker_arg] = prev
                updated_any = True
                try:
                    send_telegram_alert(bot_token, chat_id, f"Take profit supprime pour {ticker_arg}.")
                except Exception:
                    pass

        if updated_any:
            save_positions(position_file, positions)

        return positions
    except Exception as exc:
        # Si Telegram a un souci temporaire, on ne casse pas le bot trading.
        print(f"[Telegram] Polling confirmations en erreur: {exc}")
        return positions
    finally:
        # Evite de retraiter indefiniment les memes updates si une commande plante.
        if max_update_id > offset:
            try:
                save_offset(offset_file, max_update_id)
            except Exception as exc:
                print(f"[Telegram] Echec sauvegarde offset ({max_update_id}): {exc}")


def analyze_ticker(ticker: str, period: str, interval: str) -> Optional[Dict[str, str]]:
    """Pipeline complet pour un ticker: donnees, indicateurs, news, IA."""
    try:
        raw = fetch_price_data(ticker, period=period, interval=interval)
        raw = normalize_ohlc_columns(raw, ticker)
        print(f"[{ticker}] Bougies telechargees ({period}, {interval}): {len(raw)}")

        # Fallback utile sur yfinance: 15m peut etre limite/instable selon le ticker/periode.
        used_interval = interval
        if len(raw) < 40 and interval == "15m":
            fallback_interval = "1h"
            print(
                f"[{ticker}] Donnees limitees en 15m ({len(raw)}). "
                f"Tentative fallback en {fallback_interval}..."
            )
            raw = fetch_price_data(ticker, period=period, interval=fallback_interval)
            raw = normalize_ohlc_columns(raw, ticker)
            used_interval = fallback_interval
            print(f"[{ticker}] Bougies telechargees ({period}, {fallback_interval}): {len(raw)}")

        if _env_on("SCAN_COMPLETED_BARS_ONLY"):
            n_before = len(raw)
            raw = drop_forming_bar(raw, used_interval)
            if len(raw) < n_before:
                print(f"[{ticker}] Barre {used_interval} en formation ignoree (signal sur barres cloturees).")

        df = add_indicators(raw)
        required_cols = ["Close", "RSI_14", "ATR_14", "BBL_20_2.0", "BBU_20_2.0", "MACD_12_26_9", "MACDs_12_26_9"]
        existing_required = [col for col in required_cols if col in df.columns]
        df = df.dropna(subset=existing_required)

        if df.empty:
            print(f"[{ticker}] Pas assez de donnees apres calcul des indicateurs (apres dropna cible).")
            return None
    except Exception as exc:
        print(f"[{ticker}] Erreur donnees/indicateurs: {exc}")
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    current_price = float(last["Close"])
    atr = float(last.get("ATR_14", float("nan")))
    rsi = float(last.get("RSI_14", float("nan")))
    macd_value = float(last.get("MACD_12_26_9", float("nan")))
    macd_signal = float(last.get("MACDs_12_26_9", float("nan")))
    macd_prev = float(prev.get("MACD_12_26_9", macd_value))
    macd_sig_prev = float(prev.get("MACDs_12_26_9", macd_signal))
    bb_pos = bollinger_position(last)
    breakout_active, range_high = compute_intraday_breakout_metrics(df)
    news = get_latest_news(ticker, limit=5)

    return {
        "ticker": ticker,
        "current_price": f"{current_price}",
        "atr": f"{atr}",
        "rsi": f"{rsi}",
        "macd_value": f"{macd_value}",
        "macd_signal": f"{macd_signal}",
        "macd_prev": f"{macd_prev}",
        "macd_sig_prev": f"{macd_sig_prev}",
        "breakout_active": "1" if breakout_active else "0",
        "breakout_range_high": f"{range_high}",
        "bb_pos": bb_pos,
        "news_text": "\n".join(news),
    }


def estimate_news_sentiment(news_lines: List[str]) -> float:
    """
    Estimation simple du sentiment news dans [-1, 1] basee sur mots-cles.
    +1 = plutot haussier, -1 = plutot baissier.
    """
    if not news_lines:
        return 0.0
    pos_words = (
        "beat",
        "upgrade",
        "surge",
        "growth",
        "record",
        "profit",
        "partnership",
        "approval",
        "buyback",
        "guidance raised",
    )
    neg_words = (
        "miss",
        "downgrade",
        "lawsuit",
        "probe",
        "fraud",
        "decline",
        "loss",
        "bankruptcy",
        "guidance cut",
        "dilution",
    )
    score = 0
    for line in news_lines:
        txt = line.lower()
        for w in pos_words:
            if w in txt:
                score += 1
        for w in neg_words:
            if w in txt:
                score -= 1
    # Compression douce pour eviter les extremes.
    return max(-1.0, min(1.0, score / max(2.0, len(news_lines) * 1.5)))


def flash_peak_exit_quality(
    *,
    last_close: float,
    high_20_prev: float,
    rsi: float,
    close_loc: float,
    vol_ratio: float,
    pct_change: float,
    mom_5: float,
    flash_volume_spike: float,
    breakout_up: bool,
    trend_up: bool,
    atr: float,
    entry_px: float,
) -> Tuple[float, bool]:
    """
    Score 0..100 + zone pic (extension haussiere / epuisement).
    Reserve aux sorties flash en GAIN: ne pas utiliser pour une vente sur cassure.
    """
    s = 0.0
    if rsi >= 78:
        s += 26
    elif rsi >= 75:
        s += 22
    elif rsi >= 72:
        s += 18
    elif rsi >= 70:
        s += 14
    elif rsi >= 68:
        s += 10
    elif rsi >= 65:
        s += 5

    near_extension = False
    if high_20_prev > 0:
        ext_pct = (last_close / high_20_prev - 1.0) * 100.0
        if ext_pct >= -0.06:
            s += 28
            near_extension = True
        elif ext_pct >= -0.28:
            s += 18
            near_extension = True
        elif ext_pct >= -0.55:
            s += 8

    if close_loc >= 0.92:
        s += 18
    elif close_loc >= 0.82:
        s += 12
    elif close_loc >= 0.72:
        s += 6

    s += min(16.0, (vol_ratio / max(0.15, flash_volume_spike)) * 12.0)
    if mom_5 > 0.35:
        s += 12
    elif mom_5 > 0.15:
        s += 8
    elif mom_5 > 0:
        s += 4
    if breakout_up:
        s += 8
    if trend_up and pct_change > 0:
        s += 6
    if atr > 0 and entry_px > 0:
        gain_atr = (last_close - entry_px) / atr
        s += min(14.0, max(0.0, gain_atr * 3.5))

    score = min(100.0, s)
    exhaustion_zone = (rsi >= 72 and close_loc >= 0.80) or rsi >= 76
    peak_zone_ok = near_extension or exhaustion_zone
    return score, peak_zone_ok


def run_flash_watchdog() -> None:
    """Sentinelle FLASH multi-signaux hors cycle."""
    load_runtime_env()
    if os.getenv("FLASH_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "y"}:
        return
    if not is_us_market_open():
        return
    buy_blocked_near_close, _ = is_buy_blocked_near_close()
    if _yf_backoff_active():
        return

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not telegram_token or not telegram_chat_id:
        return

    position_file = os.getenv("POSITION_FILE", POSITION_FILE_DEFAULT).strip() or POSITION_FILE_DEFAULT
    offset_file = os.getenv("TELEGRAM_OFFSET_FILE", TELEGRAM_OFFSET_FILE_DEFAULT).strip() or TELEGRAM_OFFSET_FILE_DEFAULT
    flash_state_file = os.getenv("FLASH_STATE_FILE", FLASH_STATE_FILE_DEFAULT).strip() or FLASH_STATE_FILE_DEFAULT
    flash_source = os.getenv("FLASH_SOURCE", "candles").strip().lower() or "candles"
    flash_period = os.getenv("FLASH_YF_PERIOD", "1d").strip() or "1d"
    flash_interval = os.getenv("FLASH_YF_INTERVAL", "1m").strip() or "1m"

    try:
        flash_buy_pct = float(os.getenv("FLASH_BREAKOUT_PCT", "0.9"))
    except ValueError:
        flash_buy_pct = 0.9
    try:
        flash_sell_pct = float(os.getenv("FLASH_DROP_PCT", "0.9"))
    except ValueError:
        flash_sell_pct = 0.9
    flash_sell_enabled = os.getenv("FLASH_SELL_ENABLED", "0").strip().lower() in {"1", "true", "yes", "y"}
    manage_open_tickers = os.getenv("TICKERS_MANAGE_OPEN", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        flash_volume_spike = float(os.getenv("FLASH_VOLUME_SPIKE", "2.0"))
    except ValueError:
        flash_volume_spike = 2.0
    try:
        flash_cooldown_min = float(os.getenv("FLASH_COOLDOWN_MIN", "30"))
    except ValueError:
        flash_cooldown_min = 30.0
    try:
        sell_pending_reminder_min = float(os.getenv("SELL_PENDING_REMINDER_MIN", "25"))
    except ValueError:
        sell_pending_reminder_min = 25.0
    sell_pending_reminder_sec = max(60.0, sell_pending_reminder_min * 60.0)
    try:
        flash_min_score = float(os.getenv("FLASH_MIN_SCORE", "72"))
    except ValueError:
        flash_min_score = 72.0
    # FLASH_MIN_SCORE_SELL: ancien seuil vente sur cassure — remplace par FLASH_SELL_PEAK_* (pic en gain).
    try:
        flash_hard_score = float(os.getenv("FLASH_HARD_SCORE", "86"))
    except ValueError:
        flash_hard_score = 86.0
    try:
        flash_min_move_vs_atr = float(os.getenv("FLASH_MIN_MOVE_ATR", "0.35"))
    except ValueError:
        flash_min_move_vs_atr = 0.35
    try:
        flash_buy_stop_atr_mult = float(os.getenv("FLASH_BUY_STOP_ATR_MULT", "1.6"))
    except ValueError:
        flash_buy_stop_atr_mult = 1.6
    try:
        flash_buy_target_atr_mult = float(os.getenv("FLASH_BUY_TARGET_ATR_MULT", "3.0"))
    except ValueError:
        flash_buy_target_atr_mult = 3.0
    try:
        flash_sell_min_profit_pct = float(os.getenv("FLASH_SELL_MIN_PROFIT_PCT", "0.2"))
    except ValueError:
        flash_sell_min_profit_pct = 0.2
    try:
        flash_sell_peak_min_score = float(os.getenv("FLASH_SELL_PEAK_MIN_SCORE", "80"))
    except ValueError:
        flash_sell_peak_min_score = 80.0
    try:
        flash_sell_peak_min_rsi = float(os.getenv("FLASH_SELL_PEAK_MIN_RSI", "68"))
    except ValueError:
        flash_sell_peak_min_rsi = 68.0
    flash_sell_peak_require_zone = os.getenv("FLASH_SELL_PEAK_REQUIRE_ZONE", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    try:
        flash_sell_hard_peak_score = float(os.getenv("FLASH_SELL_HARD_PEAK_SCORE", "88"))
    except ValueError:
        flash_sell_hard_peak_score = 88.0
    try:
        flash_quote_lookback_sec = float(os.getenv("FLASH_QUOTE_LOOKBACK_SEC", "180"))
    except ValueError:
        flash_quote_lookback_sec = 180.0
    try:
        flash_quote_spike_pct = float(os.getenv("FLASH_QUOTE_SPIKE_PCT", "0.6"))
    except ValueError:
        flash_quote_spike_pct = 0.6
    try:
        flash_quote_min_step_pct = float(os.getenv("FLASH_QUOTE_MIN_STEP_PCT", "0.12"))
    except ValueError:
        flash_quote_min_step_pct = 0.12
    try:
        flash_quote_stop_pct = float(os.getenv("FLASH_QUOTE_STOP_PCT", "0.9"))
    except ValueError:
        flash_quote_stop_pct = 0.9
    try:
        flash_quote_tp_pct = float(os.getenv("FLASH_QUOTE_TP_PCT", "2.1"))
    except ValueError:
        flash_quote_tp_pct = 2.1
    try:
        flash_quote_synth_atr_pct = float(os.getenv("FLASH_QUOTE_SYNTH_ATR_PCT", "1.1"))
    except ValueError:
        flash_quote_synth_atr_pct = 1.1
    try:
        max_open_positions = int(os.getenv("TRADING_MAX_OPEN_POSITIONS", "2"))
    except ValueError:
        max_open_positions = 2
    try:
        budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
    except ValueError:
        budget_usd = 1000.0
    journal_file = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
    effective_budget_usd = compute_effective_budget_usd(budget_usd, journal_file)

    positions = load_positions(position_file)
    positions = poll_telegram_confirmations(
        bot_token=telegram_token,
        allowed_chat_id=telegram_chat_id,
        position_file=position_file,
        offset_file=offset_file,
        positions=positions,
    )
    maybe_intraday_flat_positions(
        positions,
        position_file,
        journal_file=journal_file,
        journal_enabled=os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"},
        bot_token=telegram_token,
        chat_id=telegram_chat_id,
    )
    base_tickers = get_tickers_from_env()
    tickers = build_scan_tickers(
        base_tickers,
        positions,
        include_open_positions=manage_open_tickers,
    )
    base_set = set(base_tickers)
    extra_managed = [tk for tk in tickers if tk not in base_set]
    if extra_managed:
        print(f"[Universe] FLASH gestion hors watchlist (positions ouvertes): {', '.join(extra_managed)}")
    slots_used = count_portfolio_slots_used(positions)
    flash_state = load_flash_state(flash_state_file)
    now_ts = time.time()
    cooldown_sec = max(60.0, flash_cooldown_min * 60.0)
    effective_budget_flash = compute_effective_budget_usd(budget_usd, journal_file)
    risk_block_buys, day_pnl_usd, dd_limit_usd = risk_new_buys_blocked(
        budget_usd=effective_budget_flash,
        journal_file=journal_file,
        now_ny=datetime.now(MARKET_TZ),
    )
    if risk_block_buys:
        print(
            f"[RISK] FLASH ACHETER bloques: PnL jour {day_pnl_usd:.2f} USD <= -{dd_limit_usd:.2f} USD."
        )

    if flash_source in {"quote_finnhub", "finnhub_quote", "quote"}:
        lookback_sec = max(30.0, flash_quote_lookback_sec)
        retention_sec = max(lookback_sec * 2.0, 900.0)
        try:
            flash_quote_confirm_sec = float(os.getenv("FLASH_QUOTE_CONFIRM_SEC", "75"))
        except ValueError:
            flash_quote_confirm_sec = 75.0
        flash_quote_confirm_sec = max(0.0, flash_quote_confirm_sec)
        try:
            flash_quote_max_daily_pct = float(os.getenv("FLASH_QUOTE_MAX_DAILY_PCT", "3.5"))
        except ValueError:
            flash_quote_max_daily_pct = 3.5
        try:
            flash_quote_max_dist_to_high_pct = float(os.getenv("FLASH_QUOTE_MAX_DIST_TO_HIGH_PCT", "0.25"))
        except ValueError:
            flash_quote_max_dist_to_high_pct = 0.25
        try:
            flash_quote_pullback_min_pct = float(os.getenv("FLASH_QUOTE_PULLBACK_MIN_PCT", "0.10"))
        except ValueError:
            flash_quote_pullback_min_pct = 0.10
        try:
            flash_quote_pullback_max_pct = float(os.getenv("FLASH_QUOTE_PULLBACK_MAX_PCT", "0.80"))
        except ValueError:
            flash_quote_pullback_max_pct = 0.80
        if flash_quote_pullback_max_pct < flash_quote_pullback_min_pct:
            flash_quote_pullback_max_pct = flash_quote_pullback_min_pct
        try:
            flash_quote_min_trend_pct = float(os.getenv("FLASH_QUOTE_MIN_TREND_PCT", "0.55"))
        except ValueError:
            flash_quote_min_trend_pct = 0.55
        try:
            flash_quote_min_up_ratio = float(os.getenv("FLASH_QUOTE_MIN_UP_RATIO", "0.62"))
        except ValueError:
            flash_quote_min_up_ratio = 0.62
        flash_quote_min_up_ratio = max(0.0, min(1.0, flash_quote_min_up_ratio))
        try:
            flash_quote_notional_factor = float(os.getenv("FLASH_QUOTE_NOTIONAL_FACTOR", "0.68"))
        except ValueError:
            flash_quote_notional_factor = 0.68
        flash_quote_notional_factor = max(0.1, min(1.0, flash_quote_notional_factor))
        try:
            flash_quote_max_remaining_share = float(os.getenv("FLASH_QUOTE_MAX_REMAINING_SHARE", "0.18"))
        except ValueError:
            flash_quote_max_remaining_share = 0.18
        flash_quote_max_remaining_share = max(0.05, min(1.0, flash_quote_max_remaining_share))
        try:
            flash_quote_stop_atr_mult = float(os.getenv("FLASH_QUOTE_STOP_ATR_MULT", "1.35"))
        except ValueError:
            flash_quote_stop_atr_mult = 1.35
        try:
            flash_quote_tp_atr_mult = float(os.getenv("FLASH_QUOTE_TP_ATR_MULT", "2.9"))
        except ValueError:
            flash_quote_tp_atr_mult = 2.9
        for ticker in tickers:
            try:
                state = normalize_position_state(positions.get(ticker, {}))
                if state["in_position"]:
                    # Evite toute contradiction avec la gestion TP des positions ouvertes.
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                if state["pending_sell"]:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                buy_key = f"{ticker}:BUY"
                buy_cooldown_ok = (now_ts - float(flash_state.get(buy_key, 0.0))) >= cooldown_sec
                if not buy_cooldown_ok:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                if risk_block_buys:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                if buy_blocked_near_close:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                if slots_used >= max_open_positions:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue

                quote_now = _fetch_finnhub_quote(ticker)
                price_now = float(quote_now["price"])
                hist = _FLASH_QUOTE_HISTORY.get(ticker, [])
                hist.append((now_ts, price_now))
                cutoff = now_ts - retention_sec
                hist = [(ts, px) for ts, px in hist if ts >= cutoff and px > 0]
                _FLASH_QUOTE_HISTORY[ticker] = hist
                if len(hist) < 2:
                    continue

                window_start = now_ts - lookback_sec
                window = [(ts, px) for ts, px in hist if ts >= window_start]
                if len(window) < 2:
                    continue
                ref_px = float(window[0][1])
                if ref_px <= 0:
                    continue
                spike_pct = ((price_now - ref_px) / ref_px) * 100.0
                if spike_pct < flash_quote_spike_pct:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                high_px = max(px for _, px in window)
                dist_to_window_high_pct = ((high_px - price_now) / high_px * 100.0) if high_px > 0 else 999.0
                # On veut une tendance vivante avec leger repli (continuation), pas un achat
                # exactement sur le sommet local ni un repli trop profond.
                if (
                    dist_to_window_high_pct < flash_quote_pullback_min_pct
                    or dist_to_window_high_pct > flash_quote_pullback_max_pct
                ):
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue

                step_ref = next((px for ts, px in reversed(hist) if ts <= now_ts - 60.0), ref_px)
                step_pct = ((price_now - float(step_ref)) / float(step_ref) * 100.0) if float(step_ref) > 0 else 0.0
                if step_pct < flash_quote_min_step_pct:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                window_prices = [float(px) for _, px in window if float(px) > 0]
                if len(window_prices) < 3:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                window_low = min(window_prices)
                trend_from_low_pct = ((price_now - window_low) / window_low * 100.0) if window_low > 0 else 0.0
                deltas = [window_prices[i] - window_prices[i - 1] for i in range(1, len(window_prices))]
                up_ratio = (sum(1 for d in deltas if d > 0) / len(deltas)) if deltas else 0.0
                if trend_from_low_pct < flash_quote_min_trend_pct or up_ratio < flash_quote_min_up_ratio:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue

                day_high = max(price_now, float(quote_now.get("high", price_now)))
                day_pct = float(quote_now.get("daily_pct", 0.0))
                dist_to_high_pct = ((day_high - price_now) / day_high * 100.0) if day_high > 0 else 999.0
                # Evite les signaux momentum trop "chasse de sommet" quand la hausse journaliere
                # est deja etiree et que le prix colle au plus haut du jour.
                if day_pct >= flash_quote_max_daily_pct and dist_to_high_pct <= flash_quote_max_dist_to_high_pct:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue

                candidate = _FLASH_QUOTE_CANDIDATES.get(ticker)
                if candidate is None:
                    _FLASH_QUOTE_CANDIDATES[ticker] = {
                        "ts": now_ts,
                        "ref_px": ref_px,
                        "trigger_px": price_now,
                    }
                    continue
                if (now_ts - float(candidate.get("ts", now_ts))) < flash_quote_confirm_sec:
                    continue
                trigger_px = float(candidate.get("trigger_px", price_now))
                if price_now < trigger_px * 0.998:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue

                try:
                    fq_raw = fetch_price_data(ticker, period="5d", interval="15m")
                    fq_raw = normalize_ohlc_columns(fq_raw, ticker)
                    if len(fq_raw) >= 30:
                        fq_ind = add_indicators(fq_raw)
                        fq_last = fq_ind.dropna(subset=["RSI_14", "MACD_12_26_9", "MACDs_12_26_9"]).iloc[-1]
                        fq_rsi = float(fq_last["RSI_14"])
                        fq_macd = float(fq_last["MACD_12_26_9"])
                        fq_macd_sig = float(fq_last["MACDs_12_26_9"])
                        fq_min_rsi = float(os.getenv("FLASH_QUOTE_MIN_RSI", "40"))
                        fq_max_rsi = float(os.getenv("FLASH_QUOTE_MAX_RSI", "78"))
                        if fq_rsi < fq_min_rsi:
                            print(f"[FLASH-QUOTE] {ticker} filtre RSI trop bas ({fq_rsi:.1f} < {fq_min_rsi:.0f}).")
                            _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                            continue
                        if fq_rsi > fq_max_rsi:
                            print(f"[FLASH-QUOTE] {ticker} filtre RSI surchauffe ({fq_rsi:.1f} > {fq_max_rsi:.0f}).")
                            _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                            continue
                        fq_require_macd = os.getenv("FLASH_QUOTE_REQUIRE_MACD_CROSS", "1").strip().lower() in {"1", "true", "yes", "y"}
                        if fq_require_macd and fq_macd < fq_macd_sig:
                            print(f"[FLASH-QUOTE] {ticker} filtre MACD sous signal ({fq_macd:.4f} < {fq_macd_sig:.4f}).")
                            _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                            continue
                except Exception as fq_exc:
                    print(f"[FLASH-QUOTE] {ticker} filtre RSI/MACD indisponible ({fq_exc}), signal accepte.")

                deployed_usd = deployed_budget_usd(positions, effective_budget_usd)
                remaining_usd = max(0.0, effective_budget_usd - deployed_usd)
                slots_left = max(0, int(max_open_positions) - int(count_portfolio_slots_used(positions)))
                synthetic_atr = max(0.01, price_now * max(0.2, flash_quote_synth_atr_pct) / 100.0)
                quote_score = max(0.0, min(100.0, 55.0 + (spike_pct * 35.0) + (step_pct * 40.0)))
                suggested_flash_usd = suggest_dynamic_notional_usd(
                    current_price=price_now,
                    atr=synthetic_atr,
                    conviction=quote_score,
                    remaining_usd=remaining_usd,
                    slots_left=slots_left,
                    risk_off=False,
                )
                max_flash_ticket = remaining_usd * flash_quote_max_remaining_share
                suggested_flash_usd = min(suggested_flash_usd * flash_quote_notional_factor, max_flash_ticket)
                if suggested_flash_usd <= 1e-6:
                    continue

                stop_loss_pct = price_now * (1.0 - max(0.2, flash_quote_stop_pct) / 100.0)
                stop_loss_atr = price_now - (synthetic_atr * max(0.6, flash_quote_stop_atr_mult))
                stop_loss = max(0.01, min(stop_loss_pct, stop_loss_atr))
                target_1_pct = price_now * (1.0 + max(0.4, flash_quote_tp_pct) / 100.0)
                target_1_atr = price_now + (synthetic_atr * max(1.2, flash_quote_tp_atr_mult))
                target_1 = max(target_1_pct, target_1_atr)
                rr_ratio = (target_1 - price_now) / max(1e-6, price_now - stop_loss)
                try:
                    flash_quote_min_rr = float(os.getenv("FLASH_QUOTE_MIN_RR", "1.0"))
                except ValueError:
                    flash_quote_min_rr = 1.0
                if rr_ratio < flash_quote_min_rr:
                    print(
                        f"[FLASH-QUOTE] {ticker} ignore: RR {rr_ratio:.2f} < seuil {flash_quote_min_rr:.2f}."
                    )
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                capped_flash, risk_capped = apply_risk_cap_to_notional(
                    suggested_flash_usd,
                    entry_price=price_now,
                    stop_price=stop_loss,
                    effective_budget_usd=effective_budget_usd,
                )
                if risk_capped:
                    print(
                        f"[FLASH-QUOTE] {ticker} sizing plafonne risque: "
                        f"{suggested_flash_usd:.0f} -> {capped_flash:.0f} USD."
                    )
                suggested_flash_usd = capped_flash
                if suggested_flash_usd <= 1e-6:
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                prepared_flash, flash_ibkr_err = ibkr_auto_buy_prepare_notional(
                    suggested_flash_usd,
                    price_usd=price_now,
                    remaining_usd=remaining_usd,
                    ticker=ticker,
                )
                if flash_ibkr_err:
                    print(f"[FLASH-QUOTE] {ticker} ignore (auto IBKR): {flash_ibkr_err}")
                    _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                    continue
                if prepared_flash > suggested_flash_usd + 1.0:
                    print(
                        f"[FLASH-QUOTE] {ticker} ticket ajuste 1 action: "
                        f"{suggested_flash_usd:.0f} -> {prepared_flash:.0f} USD."
                    )
                suggested_flash_usd = prepared_flash
                flash_reason = (
                    "Justification: tendance haussiere courte confirmee avec continuation (impulsion + acceleration + "
                    "repli sain sous le sommet local), puis plan risque/rendement valide."
                )
                flash_manual_hint = (
                    f"/confirm_buy {ticker} {suggested_flash_usd:.0f}"
                    if not ibkr_auto_execute_enabled()
                    else "Ordre marche IBKR auto si connexion OK."
                )
                msg = (
                    f"FLASH ACHAT QUOTE (FINNHUB) - {ticker}\n"
                    f"Prix (USD): {price_now:.2f}\n"
                    f"Impulsion: +{spike_pct:.2f}% sur {int(lookback_sec)}s | accel 1m: {step_pct:+.2f}%\n"
                    f"Tendance courte: +{trend_from_low_pct:.2f}% depuis le plus bas | up-ratio: {up_ratio:.0%}\n"
                    f"Distance au sommet local: {dist_to_window_high_pct:.2f}% | plus-haut jour: {dist_to_high_pct:.2f}%\n"
                    f"Variation jour: {day_pct:+.2f}% | R/R estime: {rr_ratio:.2f}\n"
                    f"Montant indicatif (USD): {suggested_flash_usd:.0f}\n"
                    f"Stop loss propose (USD): {stop_loss:.2f}\n"
                    f"Take profit (USD): {target_1:.2f}\n"
                    f"{flash_reason}\n"
                    "Mode quote-only momentum: signal opportunite instantanee pour nouvelles entrees "
                    "(ne remplace pas la logique TP des positions ouvertes).\n"
                    f"{flash_manual_hint}"
                )
                state["portfolio_profile"] = infer_portfolio_profile_for_ticker(ticker, state)
                state["entry_notional_usd"] = suggested_flash_usd
                state["entry_confidence"] = int(round(quote_score))
                state["entry_source"] = "flash_quote"
                state["take_profit"] = float(target_1)
                state["stop_loss"] = float(stop_loss)
                positions[ticker] = state
                journal_file_flash = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
                journal_on_flash = os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
                ibkr_flash_ok = False
                ibkr_require_open = os.getenv("IBKR_REQUIRE_MARKET_OPEN", "1").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "y",
                }
                if ibkr_auto_execute_enabled() and (not ibkr_require_open or is_us_market_open()):
                    ibkr_flash_ok = attempt_ibkr_buy(
                        ticker=ticker,
                        notional_usd=suggested_flash_usd,
                        positions=positions,
                        position_file=position_file,
                        state=state,
                        confidence=int(round(quote_score)),
                        take_profit=float(target_1),
                        stop_loss=float(stop_loss),
                        journal_file=journal_file_flash,
                        journal_enabled=journal_on_flash,
                        bot_token=telegram_token,
                        chat_id=telegram_chat_id,
                        current_price_hint=float(price_now),
                        justification=flash_reason,
                    )
                if not ibkr_auto_execute_enabled():
                    send_telegram_alert(telegram_token, telegram_chat_id, msg)
                if not ibkr_flash_ok and not ibkr_auto_execute_enabled():
                    state["pending_buy"] = True
                    positions[ticker] = state
                elif not ibkr_flash_ok:
                    positions[ticker] = state
                save_positions(position_file, positions)
                slots_used = count_portfolio_slots_used(positions)
                flash_state[buy_key] = now_ts
                _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                print(f"[FLASH] Alerte ACHAT quote envoyee pour {ticker}.")
            except Exception as exc:
                _FLASH_QUOTE_CANDIDATES.pop(ticker, None)
                print(f"[FLASH-QUOTE] {ticker} ignore: {exc}")
        save_flash_state(flash_state_file, flash_state)
        return

    for ticker in tickers:
        try:
            raw = fetch_price_data(ticker, period=flash_period, interval=flash_interval)
            raw = normalize_ohlc_columns(raw, ticker)
            if len(raw) < 25:
                continue
            raw_ind = add_indicators(raw)
            raw_ind = raw_ind.dropna(subset=[c for c in ["Close", "ATR_14", "RSI_14", "MACD_12_26_9", "MACDs_12_26_9"] if c in raw_ind.columns])
            if len(raw_ind) < 25:
                continue
            news_lines = get_latest_news(ticker, limit=4)
            news_sent = estimate_news_sentiment(news_lines)
            last = raw_ind.iloc[-1]
            prev = raw_ind.iloc[-2]
            prev_close = float(prev["Close"])
            if prev_close <= 0:
                continue
            last_close = float(last["Close"])
            pct_change = ((last_close - prev_close) / prev_close) * 100.0

            vol_series = raw_ind["Volume"].astype(float)
            avg_vol20 = float(vol_series.iloc[-21:-1].mean()) if len(vol_series) >= 21 else float(vol_series.iloc[:-1].mean())
            last_vol = float(vol_series.iloc[-1])
            vol_ratio = (last_vol / avg_vol20) if avg_vol20 > 0 else 0.0

            close_series = raw_ind["Close"].astype(float)
            ema_fast5 = float(close_series.ewm(span=5, adjust=False).mean().iloc[-1]) if len(close_series) >= 5 else last_close
            ema_slow20 = (
                float(close_series.ewm(span=20, adjust=False).mean().iloc[-1]) if len(close_series) >= 20 else last_close
            )
            trend_up = ema_fast5 >= ema_slow20
            trend_down = ema_fast5 <= ema_slow20
            if len(close_series) >= 6:
                close_5ago = float(close_series.iloc[-6])
                mom_5 = ((last_close - close_5ago) / close_5ago) * 100.0 if close_5ago > 0 else pct_change
            else:
                mom_5 = pct_change

            high_20_prev = float(raw_ind["High"].iloc[-21:-1].max()) if len(raw_ind) >= 21 else float(raw_ind["High"].iloc[:-1].max())
            low_20_prev = float(raw_ind["Low"].iloc[-21:-1].min()) if len(raw_ind) >= 21 else float(raw_ind["Low"].iloc[:-1].min())
            last_high = float(raw_ind["High"].iloc[-1])
            last_low = float(raw_ind["Low"].iloc[-1])
            bar_range = max(1e-6, last_high - last_low)
            close_loc = (last_close - last_low) / bar_range
            strong_close_up = close_loc >= 0.65
            strong_close_down = close_loc <= 0.35
            atr = float(last.get("ATR_14", 0.0))
            rsi = float(last.get("RSI_14", 50.0))
            macd = float(last.get("MACD_12_26_9", 0.0))
            macd_sig = float(last.get("MACDs_12_26_9", 0.0))
            move_vs_atr = (abs(last_close - prev_close) / atr) if atr > 0 else 0.0

            state = normalize_position_state(positions.get(ticker, {}))
            check_take_profit_alert(
                ticker=ticker,
                current_price=last_close,
                state=state,
                bot_token=telegram_token,
                chat_id=telegram_chat_id,
                position_file=position_file,
                positions=positions,
            )
            check_stop_loss_alert(
                ticker=ticker,
                current_price=last_close,
                state=state,
                bot_token=telegram_token,
                chat_id=telegram_chat_id,
                position_file=position_file,
                positions=positions,
            )

            buy_key = f"{ticker}:BUY"
            sell_key = f"{ticker}:SELL"
            buy_cooldown_ok = (now_ts - float(flash_state.get(buy_key, 0.0))) >= cooldown_sec
            sell_cooldown_ok = (now_ts - float(flash_state.get(sell_key, 0.0))) >= cooldown_sec

            breakout_up = last_close >= high_20_prev
            breakout_down = last_close <= low_20_prev
            macd_up = macd > macd_sig
            macd_down = macd < macd_sig
            rsi_buy_ok = 52 <= rsi <= 78
            rsi_sell_ok = 22 <= rsi <= 48

            # Score directionnel 0..100 (plus haut = meilleure qualite de signal).
            buy_score = 0.0
            sell_score = 0.0
            if breakout_up:
                buy_score += 26
            if breakout_down:
                sell_score += 26
            if trend_up:
                buy_score += 10
            else:
                buy_score -= 6
            if trend_down:
                sell_score += 10
            else:
                sell_score -= 6
            if strong_close_up:
                buy_score += 8
            if strong_close_down:
                sell_score += 8
            if mom_5 > 0:
                buy_score += min(8.0, mom_5 * 2.0)
            elif mom_5 < 0:
                sell_score += min(8.0, abs(mom_5) * 2.0)
            buy_score += min(22.0, max(0.0, (pct_change / max(0.01, flash_buy_pct)) * 20.0))
            sell_score += min(22.0, max(0.0, ((-pct_change) / max(0.01, flash_sell_pct)) * 20.0))
            buy_score += min(18.0, max(0.0, (vol_ratio / max(0.1, flash_volume_spike)) * 15.0))
            sell_score += min(18.0, max(0.0, (vol_ratio / max(0.1, flash_volume_spike)) * 15.0))
            if macd_up:
                buy_score += 12
            if macd_down:
                sell_score += 12
            if rsi_buy_ok:
                buy_score += 8
            if rsi_sell_ok:
                sell_score += 8
            buy_score += max(-7.0, min(7.0, news_sent * 7.0))
            sell_score += max(-7.0, min(7.0, -news_sent * 7.0))

            # Penalite anti-bruit: mouvement trop faible vs ATR.
            if move_vs_atr < flash_min_move_vs_atr:
                buy_score -= 16
                sell_score -= 16

            # Qualite de structure: penalise les breakouts/breakdowns sans confluence de tendance/close.
            if breakout_up and (not trend_up or not strong_close_up):
                buy_score -= 8
            if breakout_down and (not trend_down or not strong_close_down):
                sell_score -= 8

            # Hard trigger: signal exceptionnel sans attendre tout.
            hard_buy = breakout_up and vol_ratio >= (flash_volume_spike * 1.4) and pct_change >= (flash_buy_pct * 1.35)
            buy_structure_ok = breakout_up and macd_up and trend_up and strong_close_up

            if (
                ((buy_score >= flash_min_score and buy_structure_ok) or (buy_score >= flash_hard_score and hard_buy))
                and not state["in_position"]
                and not state["pending_sell"]
                and slots_used < max_open_positions
                and buy_cooldown_ok
                and not risk_block_buys
                and not buy_blocked_near_close
            ):
                deployed_usd = deployed_budget_usd(positions, effective_budget_usd)
                remaining_usd = max(0.0, effective_budget_usd - deployed_usd)
                slots_left = max(0, int(max_open_positions) - int(count_portfolio_slots_used(positions)))
                suggested_flash_usd = suggest_dynamic_notional_usd(
                    current_price=last_close,
                    atr=atr,
                    conviction=buy_score,
                    remaining_usd=remaining_usd,
                    slots_left=slots_left,
                    risk_off=False,
                )
                if suggested_flash_usd <= 1e-6:
                    print(
                        f"[FLASH] ACHAT ignore pour {ticker}: budget deploye {deployed_usd:.2f}/{effective_budget_usd:.2f} USD "
                        f"(reste {remaining_usd:.2f} USD, impossible de dimensionner une nouvelle ligne)."
                    )
                    continue
                prepared_flash2, flash2_ibkr_err = ibkr_auto_buy_prepare_notional(
                    suggested_flash_usd,
                    price_usd=last_close,
                    remaining_usd=remaining_usd,
                    ticker=ticker,
                )
                if flash2_ibkr_err:
                    print(f"[FLASH] {ticker} ignore (auto IBKR): {flash2_ibkr_err}")
                    continue
                if prepared_flash2 > suggested_flash_usd + 1.0:
                    print(
                        f"[FLASH] {ticker} ticket ajuste 1 action: "
                        f"{suggested_flash_usd:.0f} -> {prepared_flash2:.0f} USD."
                    )
                suggested_flash_usd = prepared_flash2
                if atr > 0:
                    stop_loss = max(0.01, last_close - (atr * max(0.2, flash_buy_stop_atr_mult)))
                    target_1 = last_close + (atr * max(0.3, flash_buy_target_atr_mult))
                else:
                    # Fallback si ATR indisponible: cadre simple pour garder un plan exploitable.
                    stop_loss = max(0.01, last_close * 0.985)
                    target_1 = last_close * 1.03
                flash_reason = (
                    "Justification: breakout haussier valide par la confluence (trend, MACD, cloture, volume) "
                    "avec un plan SL/TP explicite."
                )
                flash_candle_hint = (
                    f"/confirm_buy {ticker} {suggested_flash_usd:.0f}"
                    if not ibkr_auto_execute_enabled()
                    else "Ordre marche IBKR auto si connexion OK."
                )
                msg = (
                    f"FLASH ACHAT - {ticker}\n"
                    f"Prix (USD): {last_close:.2f}\n"
                    f"Montant indicatif (USD): {suggested_flash_usd:.0f} "
                    "(sizing dynamique: ATR + score flash + budget restant)\n"
                    f"Stop loss propose (USD): {stop_loss:.2f}\n"
                    f"Take profit (USD): {target_1:.2f}\n"
                    f"Score flash: {buy_score:.1f}/100\n"
                    f"Impulsion: +{pct_change:.2f}% ({flash_interval}), mom5={mom_5:+.2f}%, move/ATR={move_vs_atr:.2f}\n"
                    f"Volume spike: x{vol_ratio:.2f} (moyenne 20 bougies)\n"
                    f"Confluence: breakout={breakout_up}, trend_up={trend_up}, close_loc={close_loc:.2f}, MACD>{'' if macd_up else 'NON '}signal, RSI={rsi:.1f}, news_sent={news_sent:+.2f}\n"
                    f"{flash_reason}\n"
                    f"{flash_candle_hint}"
                )
                state["portfolio_profile"] = infer_portfolio_profile_for_ticker(ticker, state)
                state["entry_notional_usd"] = suggested_flash_usd
                state["entry_confidence"] = int(max(0.0, min(100.0, buy_score)))
                state["take_profit"] = float(target_1)
                state["stop_loss"] = float(stop_loss)
                positions[ticker] = state
                journal_file_flash2 = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
                journal_on_flash2 = os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
                ibkr_flash2_ok = False
                ibkr_require_open2 = os.getenv("IBKR_REQUIRE_MARKET_OPEN", "1").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "y",
                }
                if ibkr_auto_execute_enabled() and (not ibkr_require_open2 or is_us_market_open()):
                    ibkr_flash2_ok = attempt_ibkr_buy(
                        ticker=ticker,
                        notional_usd=suggested_flash_usd,
                        positions=positions,
                        position_file=position_file,
                        state=state,
                        confidence=int(max(0.0, min(100.0, buy_score))),
                        take_profit=float(target_1),
                        stop_loss=float(stop_loss),
                        journal_file=journal_file_flash2,
                        journal_enabled=journal_on_flash2,
                        bot_token=telegram_token,
                        chat_id=telegram_chat_id,
                        current_price_hint=float(last_close),
                        justification=flash_reason,
                    )
                if not ibkr_auto_execute_enabled():
                    send_telegram_alert(telegram_token, telegram_chat_id, msg)
                    print(f"[FLASH] Alerte ACHAT envoyee pour {ticker}.")
                elif ibkr_flash2_ok:
                    print(f"[FLASH] ACHAT auto IBKR execute pour {ticker}.")
                if not ibkr_flash2_ok and not ibkr_auto_execute_enabled():
                    state["pending_buy"] = True
                    positions[ticker] = state
                elif not ibkr_flash2_ok:
                    positions[ticker] = state
                save_positions(position_file, positions)
                slots_used = count_portfolio_slots_used(positions)
                flash_state[buy_key] = now_ts

            # FLASH VENTE: uniquement prise de profit au pic (gain + zone pic / score eleve).
            # Pas de vente flash sur cassure / perte — utilise le scan principal ou ton stop.
            if flash_sell_enabled and state["in_position"] and not state["pending_buy"] and sell_cooldown_ok:
                entry_px = float(state.get("entry_price_usd", 0.0) or 0.0)
                profit_pct = ((last_close - entry_px) / entry_px * 100.0) if entry_px > 0 else -999.0
                peak_score, peak_zone_ok = flash_peak_exit_quality(
                    last_close=last_close,
                    high_20_prev=high_20_prev,
                    rsi=rsi,
                    close_loc=close_loc,
                    vol_ratio=vol_ratio,
                    pct_change=pct_change,
                    mom_5=mom_5,
                    flash_volume_spike=flash_volume_spike,
                    breakout_up=breakout_up,
                    trend_up=trend_up,
                    atr=atr,
                    entry_px=entry_px,
                )
                hard_peak_ok = peak_score >= flash_sell_hard_peak_score and profit_pct >= max(
                    flash_sell_min_profit_pct * 2.5, 0.5
                )
                zone_ok = (not flash_sell_peak_require_zone) or peak_zone_ok or hard_peak_ok
                gain_ok = entry_px > 0 and profit_pct >= flash_sell_min_profit_pct
                peak_signal_ok = (
                    gain_ok
                    and zone_ok
                    and peak_score >= flash_sell_peak_min_score
                    and rsi >= flash_sell_peak_min_rsi
                )
                if peak_signal_ok:
                    was_pending_sell = bool(state.get("pending_sell"))
                    if was_pending_sell:
                        last_sell_alert_ts = float(state.get("last_sell_alert_ts", 0.0) or 0.0)
                        if (now_ts - last_sell_alert_ts) < sell_pending_reminder_sec:
                            continue
                    if peak_zone_ok:
                        zone_lbl = "OUI"
                    elif hard_peak_ok:
                        zone_lbl = "hard (score+gain)"
                    elif not flash_sell_peak_require_zone:
                        zone_lbl = "N/A (zone non exigee)"
                    else:
                        zone_lbl = "NON"
                    sell_reason = (
                        "Justification: prise de profit optimisee sur zone de pic (gain deja present + score de sortie eleve), "
                        "pas une vente defensive a perte."
                    )
                    flash_sell_hint = (
                        f"/confirm_sell {ticker} PRIX_SORTIE"
                        if not ibkr_auto_execute_enabled()
                        else "Vente marche IBKR auto si connexion OK."
                    )
                    msg = (
                        f"{'FLASH RAPPEL VENTE (PIC)' if was_pending_sell else 'FLASH VENTE PIC (prise de profit)'} - {ticker}\n"
                        f"Prix (USD): {last_close:.2f}\n"
                        f"Entree enregistree: {entry_px:.2f} USD | P/L latent ~{profit_pct:+.2f}%\n"
                        f"Score pic sortie: {peak_score:.1f}/100 (seuil {flash_sell_peak_min_score:.0f}) | RSI={rsi:.1f}\n"
                        f"Zone pic: {zone_lbl} | "
                        f"Impulsion: {pct_change:+.2f}% ({flash_interval}), mom5={mom_5:+.2f}%, move/ATR={move_vs_atr:.2f}\n"
                        f"Volume spike: x{vol_ratio:.2f} | close_loc={close_loc:.2f} | breakout_up={breakout_up}\n"
                        f"{sell_reason}\n"
                        f"{flash_sell_hint}"
                    )
                    send_telegram_alert(telegram_token, telegram_chat_id, msg)
                    journal_file_fs = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
                    journal_on_fs = os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
                    ibkr_fs_sold = False
                    ibkr_require_open_fs = os.getenv("IBKR_REQUIRE_MARKET_OPEN", "1").strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "y",
                    }
                    if ibkr_auto_execute_enabled() and (not ibkr_require_open_fs or is_us_market_open()):
                        ibkr_fs_sold = attempt_ibkr_sell(
                            ticker=ticker,
                            positions=positions,
                            position_file=position_file,
                            journal_file=journal_file_fs,
                            journal_enabled=journal_on_fs,
                            bot_token=telegram_token,
                            chat_id=telegram_chat_id,
                            reason="flash_peak_sell",
                        )
                    if not ibkr_fs_sold:
                        state["pending_sell"] = True
                        state["last_sell_alert_ts"] = now_ts
                    positions[ticker] = state
                    save_positions(position_file, positions)
                    flash_state[sell_key] = now_ts
                    print(f"[FLASH] {'Rappel VENTE pic' if was_pending_sell else 'Alerte VENTE pic'} envoyee pour {ticker}.")
        except Exception as exc:
            print(f"[FLASH] {ticker} ignore (erreur sentinelle): {exc}")

    save_flash_state(flash_state_file, flash_state)


def poll_confirmations_fast() -> None:
    """Polling Telegram dedie pour capter vite /confirm_buy et /confirm_sell."""
    load_runtime_env()
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not telegram_token or not telegram_chat_id:
        return
    position_file = os.getenv("POSITION_FILE", POSITION_FILE_DEFAULT).strip() or POSITION_FILE_DEFAULT
    offset_file = os.getenv("TELEGRAM_OFFSET_FILE", TELEGRAM_OFFSET_FILE_DEFAULT).strip() or TELEGRAM_OFFSET_FILE_DEFAULT
    positions = load_positions(position_file)
    poll_telegram_confirmations(
        bot_token=telegram_token,
        allowed_chat_id=telegram_chat_id,
        position_file=position_file,
        offset_file=offset_file,
        positions=positions,
    )


def sleep_with_flash(total_wait_sec: float) -> None:
    """
    Attend entre 2 cycles et lance la sentinelle FLASH hors cycle.
    """
    if total_wait_sec <= 0:
        return
    load_runtime_env()
    flash_enabled = os.getenv("FLASH_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        flash_check_sec = float(os.getenv("FLASH_CHECK_SEC", "30"))
    except ValueError:
        flash_check_sec = 30.0
    flash_check_sec = max(5.0, flash_check_sec)
    try:
        flash_min_remaining_sec = float(os.getenv("FLASH_MIN_REMAINING_SEC", "20"))
    except ValueError:
        flash_min_remaining_sec = 20.0
    flash_min_remaining_sec = max(5.0, flash_min_remaining_sec)
    try:
        confirm_poll_sec = float(os.getenv("TELEGRAM_CONFIRM_POLL_SEC", "3"))
    except ValueError:
        confirm_poll_sec = 3.0
    confirm_poll_sec = max(1.0, confirm_poll_sec)

    # Prend en compte les confirmations immediatement meme en debut de pause.
    poll_confirmations_fast()
    poll_intraday_flat_if_due()

    cockpit_refresh_sec = 0.0
    try:
        from cockpit_web import cockpit_live_prices_enabled, cockpit_web_enabled

        if cockpit_web_enabled() and cockpit_live_prices_enabled():
            cockpit_refresh_sec = float(os.getenv("COCKPIT_WEB_PRICE_REFRESH_SEC", "5"))
            cockpit_refresh_sec = max(2.0, cockpit_refresh_sec)
    except ImportError:
        cockpit_refresh_sec = 0.0
    last_cockpit_px_ts = 0.0

    end_at = time.time() + total_wait_sec
    while True:
        remaining = end_at - time.time()
        if remaining <= 0:
            break
        loop_sleep = confirm_poll_sec
        if is_intraday_flat_window():
            loop_sleep = min(confirm_poll_sec, intraday_flat_poll_sec())
        time.sleep(min(loop_sleep, remaining))
        remaining_after_sleep = end_at - time.time()
        if remaining_after_sleep <= 0:
            break
        poll_intraday_flat_if_due()
        poll_confirmations_fast()
        if ibkr_auto_execute_enabled():
            pos_file_ibkr = os.getenv("POSITION_FILE", POSITION_FILE_DEFAULT).strip() or POSITION_FILE_DEFAULT
            jfile_ibkr = (
                os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip()
                or TRADE_JOURNAL_FILE_DEFAULT
            )
            j_on_ibkr = os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
            tg_t_ibkr = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            tg_c_ibkr = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            poll_ibkr_closed_positions_if_due(
                position_file=pos_file_ibkr,
                journal_file=jfile_ibkr,
                journal_enabled=j_on_ibkr,
                bot_token=tg_t_ibkr,
                chat_id=tg_c_ibkr,
            )
            poll_trailing_tp_if_due(position_file=pos_file_ibkr)
        if cockpit_refresh_sec > 0 and (time.time() - last_cockpit_px_ts) >= cockpit_refresh_sec:
            # Entre cycles : Yahoo/Finnhub suffisent (evite rafale reqMktData IBKR).
            refresh_cockpit_live_prices_overlay(skip_ibkr=True)
            last_cockpit_px_ts = time.time()
        if flash_enabled:
            # Le flash reste moins frequent pour eviter surcharge API.
            now_ts = time.time()
            last_flash_ts = getattr(sleep_with_flash, "_last_flash_ts", 0.0)
            avg_flash_sec = float(getattr(sleep_with_flash, "_avg_flash_sec", 8.0))
            enough_budget = remaining_after_sleep >= max(
                flash_min_remaining_sec,
                (avg_flash_sec * 1.4),
                (confirm_poll_sec + 1.0),
            )
            if enough_budget and (now_ts - float(last_flash_ts)) >= flash_check_sec:
                flash_start = time.perf_counter()
                try:
                    run_flash_watchdog()
                except Exception as flash_exc:
                    print(f"[FLASH] Erreur sentinelle (ignoree): {flash_exc}")
                flash_elapsed = max(0.1, time.perf_counter() - flash_start)
                prev_avg = float(getattr(sleep_with_flash, "_avg_flash_sec", flash_elapsed))
                # EWMA pour estimer la duree future et eviter de depasser la fin de pause.
                setattr(sleep_with_flash, "_avg_flash_sec", (0.7 * prev_avg) + (0.3 * flash_elapsed))
                setattr(sleep_with_flash, "_last_flash_ts", now_ts)


def run_premarket_preparation(
    *,
    tickers: List[str],
    yf_period: str,
    yf_interval: str,
    telegram_token: str,
    telegram_chat_id: str,
    position_file: str,
    journal_file: str,
    base_budget_usd: float,
    objective_days: int,
    send_daily_report: bool,
    state_file: str,
    window_minutes: float,
    max_items: int,
    force_send: bool = False,
) -> bool:
    """
    Construit un plan pre-ouverture (watchlist + scenarios) sans envoyer d'ordre.
    Le message est envoye au plus une fois par prochaine ouverture.
    """
    now_ny = datetime.now(MARKET_TZ)
    next_open = get_next_us_market_open(now_ny)
    min_to_open = (next_open - now_ny).total_seconds() / 60.0

    if (not force_send) and (min_to_open < 0 or min_to_open > window_minutes):
        print(
            f"[PreMarket] Hors fenetre ({min_to_open:.1f} min avant open, fenetre {window_minutes:.0f} min)."
        )
        return False

    state = load_json_dict(state_file)
    open_key = next_open.strftime("%Y-%m-%d")
    if not telegram_token or not telegram_chat_id:
        print("[PreMarket] Telegram non configure, plan non envoye.")
        return False

    if send_daily_report:
        day_key = now_ny.strftime("%Y-%m-%d")
        if str(state.get("last_daily_report_day", "")).strip() != day_key:
            try:
                positions_now = load_positions(position_file)
                daily_msg = build_daily_progress_report(
                    journal_file,
                    positions_now,
                    base_budget_usd=base_budget_usd,
                    objective_days=max(1, int(objective_days)),
                    now_ny=now_ny,
                )
                send_telegram_alert(telegram_token, telegram_chat_id, daily_msg)
                state["last_daily_report_day"] = day_key
                save_json_dict(state_file, state)
                print(f"[PreMarket] Rapport quotidien envoye ({day_key}).")
            except Exception as exc:
                print(f"[PreMarket] Echec envoi rapport quotidien: {exc}")

    last_open_key = str(state.get("last_open_key", "")).strip()
    last_plan_version = str(state.get("last_plan_version", "")).strip()
    last_plan_tickers_key = str(state.get("last_plan_tickers_key", "")).strip()
    last_plan_profile = normalize_portfolio_profile_key(str(state.get("last_plan_profile", "")))
    try:
        last_plan_max_items = int(state.get("last_plan_max_items", -1))
    except Exception:
        last_plan_max_items = -1
    current_plan_max_items = max(1, int(max_items))
    universe_tickers = [normalize_ticker(t) for t in tickers if normalize_ticker(t)]
    current_plan_tickers_key = "|".join(sorted(universe_tickers))
    current_profile = get_active_portfolio_profile_key()
    already_sent_for_open = (
        (last_open_key == open_key)
        and (last_plan_version == PREMARKET_PLAN_VERSION)
        and (last_plan_max_items == current_plan_max_items)
        and (last_plan_tickers_key == current_plan_tickers_key)
        and (last_plan_profile == current_profile)
    )
    if already_sent_for_open and (not force_send):
        print(f"[PreMarket] Plan deja envoye pour l'ouverture {open_key}.")
        return False

    candidates: List[Tuple[float, str]] = []
    scanned = 0
    positions_now = load_positions(position_file)
    premarket_price_source = (os.getenv("PREMARKET_PRICE_SOURCE", "auto") or "auto").strip().lower()
    premarket_use_live_price = os.getenv("PREMARKET_USE_LIVE_PRICE", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        max_open_positions = int(os.getenv("TRADING_MAX_OPEN_POSITIONS", "2"))
    except ValueError:
        max_open_positions = 2
    max_open_positions = max(1, max_open_positions)
    try:
        premarket_notional_factor = float(os.getenv("PREMARKET_NOTIONAL_FACTOR", "0.90"))
    except ValueError:
        premarket_notional_factor = 0.90
    premarket_notional_factor = max(0.20, min(1.20, premarket_notional_factor))
    effective_budget_usd = compute_effective_budget_usd(base_budget_usd, journal_file)
    deployed_budget_preopen = deployed_budget_usd(positions_now, effective_budget_usd)
    free_budget_preopen = max(0.0, effective_budget_usd - deployed_budget_preopen)
    slots_left_preopen = max(0, int(max_open_positions) - int(count_portfolio_slots_used(positions_now)))
    for ticker in tickers:
        analysis = analyze_ticker(ticker, period=yf_period, interval=yf_interval)
        if analysis is None:
            continue
        scanned += 1
        try:
            rsi = float(analysis.get("rsi", "50"))
            macd_value = float(analysis.get("macd_value", "0"))
            macd_signal = float(analysis.get("macd_signal", "0"))
            current_price = float(analysis.get("current_price", "0"))
            atr = max(0.0, float(analysis.get("atr", "0") or 0.0))
        except (TypeError, ValueError):
            continue
        price_source = "candles"
        if premarket_use_live_price:
            live_price: Optional[float] = None
            if premarket_price_source in {"auto", "reference", "yahoo", "intraday"}:
                try:
                    ref_px = get_reference_price_usd(ticker, mode="intraday")
                    if ref_px is not None and float(ref_px) > 0:
                        live_price = float(ref_px)
                        price_source = "ref_intraday"
                except Exception:
                    live_price = None
            if live_price is None and premarket_price_source in {"auto", "finnhub", "quote_finnhub", "quote"}:
                try:
                    live_price = float(_fetch_finnhub_quote_price(ticker))
                    if live_price > 0:
                        price_source = "finnhub_quote"
                except Exception:
                    live_price = None
            if live_price is not None and live_price > 0:
                current_price = live_price

        bb_pos = str(analysis.get("bb_pos", "")).strip()
        news_lines = (analysis.get("news_text", "") or "").splitlines()
        news_sent = estimate_news_sentiment(news_lines)

        buy_score = 0.0
        sell_score = 0.0
        if macd_value > macd_signal:
            buy_score += 24
        elif macd_value < macd_signal:
            sell_score += 24

        if 45 <= rsi <= 65:
            buy_score += 20
        elif rsi >= 70:
            sell_score += 15
        if 35 <= rsi <= 55:
            sell_score += 20
        elif rsi <= 30:
            buy_score += 15

        if bb_pos in {"below_mid", "lower_band"}:
            buy_score += 10
        if bb_pos in {"above_mid", "upper_band"}:
            sell_score += 10

        buy_score += max(-12.0, min(12.0, news_sent * 12.0))
        sell_score += max(-12.0, min(12.0, -news_sent * 12.0))

        st = normalize_position_state(positions_now.get(ticker, {}))
        in_position = bool(st.get("in_position", False))
        pending_buy = bool(st.get("pending_buy", False))
        pending_sell = bool(st.get("pending_sell", False))
        entry_notional_state = float(st.get("entry_notional_usd", 0.0) or 0.0)

        # Pre-open coherent avec le portefeuille:
        # - si deja en position: gestion/sortie seulement (pas d'ACHETER)
        # - si pas en position: entree/attente seulement (pas de VENDRE)
        if pending_buy:
            direction = "ATTENDRE"
            score = buy_score
            state_tag = "achat en attente"
        elif pending_sell:
            direction = "ATTENDRE"
            score = sell_score
            state_tag = "vente en attente"
        elif in_position:
            direction = "VENDRE" if sell_score > buy_score else "ATTENDRE"
            score = sell_score if direction == "VENDRE" else buy_score
            state_tag = "deja en position"
        else:
            direction = "ACHETER" if buy_score >= sell_score else "ATTENDRE"
            score = buy_score
            state_tag = "pas de position"

        suggested_notional_preopen: Optional[float] = None
        if (
            direction == "ACHETER"
            and (not in_position)
            and (not pending_buy)
            and free_budget_preopen > 0
            and slots_left_preopen > 0
        ):
            suggested = suggest_dynamic_notional_usd(
                current_price=current_price,
                atr=atr,
                conviction=max(0.0, min(100.0, float(score))),
                remaining_usd=free_budget_preopen,
                slots_left=slots_left_preopen,
                risk_off=False,
            )
            suggested *= premarket_notional_factor
            if suggested > 0:
                suggested_notional_preopen = min(free_budget_preopen, suggested)

        # Justification courte pour rendre le plan pre-open actionnable.
        reasons: List[str] = []
        if direction == "ACHETER":
            if macd_value > macd_signal:
                reasons.append("momentum MACD haussier")
            if 45 <= rsi <= 65:
                reasons.append("RSI en zone constructive")
            elif rsi <= 30:
                reasons.append("RSI survendu (rebond possible)")
            if bb_pos in {"below_mid", "lower_band"}:
                reasons.append("prix en zone de repli")
            if news_sent > 0.15:
                reasons.append("news plutot positive")
        elif direction == "VENDRE":
            if macd_value < macd_signal:
                reasons.append("MACD en affaiblissement")
            if rsi >= 70:
                reasons.append("RSI tendu/surachete")
            if bb_pos in {"above_mid", "upper_band"}:
                reasons.append("prix en zone haute")
            if news_sent < -0.15:
                reasons.append("news plutot negative")
            if in_position:
                reasons.append("ligne deja en portefeuille")
        else:
            if pending_buy:
                reasons.append("achat en attente de confirmation")
            elif pending_sell:
                reasons.append("vente en attente de confirmation")
            elif abs(buy_score - sell_score) < 6:
                reasons.append("signaux mitiges")
            elif in_position:
                reasons.append("conserver tant que signal de sortie faible")
            else:
                reasons.append("pas d'avantage clair pre-open")
        if not reasons:
            reasons.append("confluence insuffisante")
        reason_txt = "; ".join(reasons[:2])

        # Affiche un plan de prix lisible (entree/TP/SL) dans le pre-open.
        entry_state = float(st.get("entry_price_usd", 0.0) or 0.0)
        tp_state = parse_price_value(str(st.get("take_profit", "")))
        sl_state = parse_price_value(str(st.get("stop_loss", "")))

        if in_position and entry_state > 0:
            entry_px = entry_state
        else:
            entry_px = current_price

        atr_sl = (atr * 1.2) if atr > 0 else max(0.01, entry_px * 0.015)
        atr_tp = (atr * 2.0) if atr > 0 else max(0.01, entry_px * 0.03)
        sl_fallback = max(0.01, entry_px - atr_sl)
        tp_fallback = max(entry_px + 0.01, entry_px + atr_tp)
        sl_px = float(sl_state) if sl_state is not None else sl_fallback
        tp_px = float(tp_state) if tp_state is not None else tp_fallback

        if in_position or pending_sell:
            plan_prefix = "Plan suivi"
        elif pending_buy:
            plan_prefix = "Plan achat en attente"
        else:
            plan_prefix = "Plan entree indicatif"
        plan_txt = f"{plan_prefix}: entree ~{entry_px:.2f} | TP {tp_px:.2f} | SL {sl_px:.2f}"
        if suggested_notional_preopen is not None:
            plan_txt += f" | Montant indicatif ~{suggested_notional_preopen:.0f} USD"
        elif in_position and entry_notional_state > 0:
            plan_txt += f" | Ligne en cours ~{entry_notional_state:.0f} USD"

        signal_txt = (
            f"{ticker}: {direction} (score {score:.0f}/100) | "
            f"prix {current_price:.2f} [{price_source}] | RSI {rsi:.1f} | MACD {macd_value:.3f}/{macd_signal:.3f} | "
            f"{state_tag}\n   {plan_txt}\n   Justification: {reason_txt}"
        )
        candidates.append((score, signal_txt))

    candidates.sort(key=lambda x: x[0], reverse=True)
    top_n = max(1, int(max_items))
    selected = candidates[:top_n]

    if not selected:
        msg = (
            "PLAN PRE-OUVERTURE\n"
            f"Marche ouvre a {next_open.strftime('%H:%M %Z')} (dans {max(0.0, min_to_open):.0f} min).\n"
            f"Profil actif: {current_profile}\n"
            f"Univers suivi ({len(universe_tickers)}): {', '.join(universe_tickers) if universe_tickers else 'aucun'}\n"
            f"Mode: {'renvoi manuel' if force_send else 'auto pre-open'}\n"
            "Aucune configuration exploitable detectee sur la watchlist."
        )
    else:
        lines = [
            "PLAN PRE-OUVERTURE",
            f"Marche ouvre a {next_open.strftime('%H:%M %Z')} (dans {max(0.0, min_to_open):.0f} min).",
            f"Profil actif: {current_profile}",
            f"Univers suivi ({len(universe_tickers)}): {', '.join(universe_tickers) if universe_tickers else 'aucun'}",
            f"Mode: {'renvoi manuel' if force_send else 'auto pre-open'}",
            (
                f"Budget effectif {effective_budget_usd:.2f} USD | "
                f"Deja deploye {deployed_budget_preopen:.2f} USD | "
                f"Libre {free_budget_preopen:.2f} USD | "
                f"Slots libres {slots_left_preopen}/{max_open_positions}"
            ),
            f"Tickers analyses: {scanned}. Top scenarios:",
        ]
        for i, (_, txt) in enumerate(selected, start=1):
            lines.append(f"{i}) {txt}")
        lines.append("")
        lines.append("Mode preparation uniquement: pas d'ordre execute automatiquement.")
        msg = "\n".join(lines)

    try:
        send_telegram_alert(telegram_token, telegram_chat_id, msg)
        state["last_open_key"] = open_key
        state["last_plan_version"] = PREMARKET_PLAN_VERSION
        state["last_plan_max_items"] = current_plan_max_items
        state["last_plan_tickers_key"] = current_plan_tickers_key
        state["last_plan_profile"] = current_profile
        state["last_sent_ts_utc"] = utc_now_iso_z()
        save_json_dict(state_file, state)
        print(f"[PreMarket] Plan pre-ouverture envoye ({len(selected)} scenario(s)).")
        return True
    except Exception as exc:
        print(f"[PreMarket] Echec envoi Telegram: {exc}")
        return False


def _scan_ticker_llm_worker(ticker: str, ctx: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Phase lecture + LLM (thread-safe). Pas d'ordre IBKR ni mutation positions."""
    try:
        analysis = analyze_ticker(ticker, period=ctx["yf_period"], interval=ctx["yf_interval"])
        if analysis is None:
            return ticker, None
        st_for_prompt_early = normalize_position_state(
            ctx["positions_snapshot"].get(normalize_ticker(ticker), {})
        )
        entry_px_early = float(st_for_prompt_early.get("entry_price_usd", 0.0) or 0.0)
        cp_raw = float(analysis.get("current_price", 0) or 0)
        cp_fixed = reconcile_analysis_price_usd(
            ticker,
            cp_raw,
            entry_price_hint=entry_px_early if entry_px_early > 0 else None,
        )
        if abs(cp_fixed - cp_raw) > 1e-6:
            analysis["current_price"] = f"{cp_fixed}"

        fund_data: Dict[str, object] = {
            "fund_score": 50,
            "earnings_days": None,
            "risk_off": ctx["risk_off_regime"],
        }
        if ctx["fundamental_enabled"]:
            with ctx["fund_lock"]:
                fund_data = fetch_fundamental_data(
                    ticker=ticker,
                    cache=ctx["fundamental_cache"],
                    cache_hours=ctx["fundamental_cache_hours"],
                )
            fund_data["risk_off"] = ctx["risk_off_regime"]
        fund_score_val = int(fund_data.get("fund_score", 50))
        higher_tf = (
            get_higher_tf_context(
                ticker=ticker,
                period=ctx["mtf_period"],
                interval=ctx["mtf_interval"],
            )
            if ctx["mtf_enabled"]
            else {"trend_score": 0, "summary": "Desactive", "interval": ctx["mtf_interval"]}
        )

        positions_snapshot: Dict[str, Dict[str, object]] = ctx["positions_snapshot"]
        slots_used = sum(
            1
            for _tk, raw in positions_snapshot.items()
            if normalize_position_state(raw).get("in_position")
        )
        open_tickers = sorted(
            _tk
            for _tk, raw in positions_snapshot.items()
            if normalize_position_state(raw).get("in_position")
        )
        st_for_prompt = normalize_position_state(positions_snapshot.get(ticker, {}))
        entry_px_hint = float(st_for_prompt.get("entry_price_usd", 0.0) or 0.0)
        tp_raw = st_for_prompt.get("take_profit")
        try:
            tp_val_hint = float(tp_raw) if tp_raw is not None and float(tp_raw) > 0 else None
        except (TypeError, ValueError):
            tp_val_hint = None

        effective_budget_usd = ctx["effective_budget_usd"]
        deployed_for_prompt = ctx["deployed_for_prompt"]
        remaining_for_prompt = max(0.0, effective_budget_usd - deployed_for_prompt)
        final_ticker = normalize_ticker(ticker)
        mode = str(ctx.get("scan_decision_mode", "hybrid")).strip().lower()
        use_rules = mode == "rules" or (
            mode == "hybrid"
            and not bool(st_for_prompt.get("in_position"))
            and not bool(st_for_prompt.get("pending_sell"))
        )

        if use_rules:
            parsed, reason_tag = build_rules_scan_decision(
                ticker=final_ticker,
                analysis=analysis,
                state=st_for_prompt,
                higher_tf=higher_tf,
                fund_data=fund_data,
            )
            decision = str(parsed.get("decision", "ATTENDRE")).upper()
            llm_answer = (
                f"TICKER : {final_ticker}\n"
                f"DÉCISION : {decision}\n"
                f"CONFIANCE : {parsed.get('confiance', '60')}\n"
                f"STOP_LOSS : {parsed.get('stop_loss', 'N/A')}\n"
                f"OBJECTIF_1 : {parsed.get('objectif_1', 'N/A')}\n"
                f"TAILLE_SUGGEREE_USD : {parsed.get('taille_suggeree_usd', '0')}\n"
                f"JUSTIFICATION : {parsed.get('justification', '')}"
            )
            model_used = f"rules-v1/{reason_tag}"
        else:
            prompt = build_llm_prompt(
                ticker=analysis["ticker"],
                current_price=float(analysis["current_price"]),
                atr=float(analysis["atr"]),
                bb_position=analysis["bb_pos"],
                rsi=float(analysis["rsi"]),
                macd_value=float(analysis["macd_value"]),
                macd_signal=float(analysis["macd_signal"]),
                news_lines=analysis["news_text"].splitlines() if analysis["news_text"] else [],
                budget_usd=effective_budget_usd,
                horizon_days=ctx["horizon_days"],
                risk_per_trade_pct=ctx["risk_per_trade_pct"],
                max_open_positions=ctx["max_open_positions"],
                slots_used=slots_used,
                open_tickers=open_tickers,
                fundamental_score=fund_score_val,
                earnings_days=fund_data.get("earnings_days"),  # type: ignore[arg-type]
                risk_off_regime=bool(fund_data.get("risk_off", False)),
                higher_tf_summary=str(higher_tf.get("summary", "Indisponible")),
                in_position_on_this_ticker=bool(st_for_prompt.get("in_position")),
                pending_buy_on_this_ticker=bool(st_for_prompt.get("pending_buy")),
                pending_sell_on_this_ticker=bool(st_for_prompt.get("pending_sell")),
                entry_notional_usd_hint=float(st_for_prompt.get("entry_notional_usd", 0.0) or 0.0),
                remaining_budget_usd=remaining_for_prompt,
                entry_price_usd_hint=entry_px_hint,
                take_profit_usd_hint=tp_val_hint,
            )
            llm_result = ask_llm_decision(
                prompt,
                model=ctx["groq_model"],
                api_key=ctx["groq_api_key"],
                base_url=ctx["groq_base_url"],
            )
            llm_answer = llm_result["text"]
            model_used = llm_result["model_used"]
            parsed = parse_decision(llm_answer)
            decision = parsed.get("decision", "").upper()
            llm_ticker = normalize_ticker(parsed.get("ticker") or "")
            if llm_ticker and llm_ticker != final_ticker:
                print(
                    f"[{ticker}] Ticker LLM ignore ({llm_ticker}) -> ticker du scan force ({final_ticker})."
                )
            parsed["ticker"] = final_ticker
            parsed["decision"] = decision
            scan_px = float(analysis["current_price"])
            scan_rsi = float(analysis.get("rsi", 50.0) or 50.0)
            conf_val = parse_confidence(str(parsed.get("confiance", "") or ""))
            n_fixed = sanitize_llm_parsed_levels(
                parsed,
                ticker=final_ticker,
                current_price=scan_px,
                rsi=scan_rsi,
                decision=decision,
            )
            if n_fixed > 0:
                print(
                    f"[{final_ticker}] Niveaux LLM nettoyes ({n_fixed} champ(s)) — "
                    f"decision basee sur prix scan {scan_px:.2f} USD."
                )
            warn_llm_price_drift(
                final_ticker,
                current_price=scan_px,
                entry_price=entry_px_early,
                justification=str(parsed.get("justification", "") or ""),
                rsi=scan_rsi,
                conf=conf_val,
            )
        momentum_score = quick_momentum_score_for_ticker(ticker)
        return ticker, {
            "analysis": analysis,
            "fund_data": fund_data,
            "fund_score_val": fund_score_val,
            "higher_tf": higher_tf,
            "llm_answer": llm_answer,
            "model_used": model_used,
            "parsed": parsed,
            "decision": decision,
            "final_ticker": final_ticker,
            "momentum_score": momentum_score,
        }
    except Exception as exc:
        return ticker, {"error": str(exc)}


def _execute_pending_scan_buy(
    buy: Dict[str, Any],
    *,
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    telegram_token: str,
    telegram_chat_id: str,
    journal_file: str,
    journal_enabled: bool,
    stats: Dict[str, int],
) -> bool:
    """Execute un ACHETER retenu (apres classement). Retourne True si ordre/alerte emis."""
    ticker = str(buy.get("scan_ticker", ""))
    final_ticker = normalize_ticker(str(buy.get("final_ticker", "")))
    analysis = buy["analysis"]
    parsed = buy["parsed"]
    conf_val = int(buy["conf_val"])
    fund_data = buy["fund_data"]
    higher_tf = buy["higher_tf"]
    requested_notional_usd = float(buy["requested_notional_usd"])
    deployed_usd = float(buy["deployed_usd"])
    remaining_usd = float(buy["remaining_usd"])
    decision = "ACHETER"

    state = normalize_position_state(positions.get(final_ticker, {}))
    if state["in_position"] or state["pending_sell"]:
        print(
            f"[{final_ticker}] ACHETER classement ignore: deja en position "
            "ou vente en attente."
        )
        return False

    theme_block, theme_note = theme_exposure_blocks(positions, final_ticker)
    if theme_block:
        print(f"[{final_ticker}] ACHETER classement ignore: {theme_note}.")
        return False

    message = format_telegram_signal(
        decision, final_ticker, analysis, parsed, notional_usd=requested_notional_usd
    )
    sl_suggested = parse_price_value(parsed.get("stop_loss", ""))
    tp_suggested = parse_price_value(parsed.get("objectif_1", ""))
    state["portfolio_profile"] = infer_portfolio_profile_for_ticker(final_ticker, state)
    state["entry_notional_usd"] = requested_notional_usd
    state["entry_confidence"] = conf_val
    state["entry_rank_score"] = float(buy.get("rank_score", 0.0) or 0.0)
    state["entry_source"] = "scan_llm"
    if tp_suggested is not None:
        state["take_profit"] = tp_suggested
    if sl_suggested is not None:
        state["stop_loss"] = sl_suggested
    positions[final_ticker] = state

    ibkr_executed = False
    ibkr_require_open = os.getenv("IBKR_REQUIRE_MARKET_OPEN", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    if ibkr_auto_execute_enabled() and (not ibkr_require_open or is_us_market_open()):
        ibkr_executed = attempt_ibkr_buy(
            ticker=final_ticker,
            notional_usd=requested_notional_usd,
            positions=positions,
            position_file=position_file,
            state=state,
            confidence=conf_val,
            take_profit=tp_suggested,
            stop_loss=sl_suggested,
            journal_file=journal_file,
            journal_enabled=journal_enabled,
            bot_token=telegram_token,
            chat_id=telegram_chat_id,
            current_price_hint=float(analysis["current_price"]),
            justification=str(parsed.get("justification", "") or ""),
        )
    if not ibkr_executed:
        if not ibkr_auto_execute_enabled():
            state["pending_buy"] = True
            positions[final_ticker] = state
        else:
            positions[final_ticker] = state
    save_positions(position_file, positions)
    try:
        if not ibkr_executed and (not ibkr_auto_execute_enabled()):
            send_telegram_alert(telegram_token, telegram_chat_id, message)
            print(f"[{ticker}] Alerte Telegram envoyee ({decision}).")
            stats["signals_sent"] += 1
            stats["buy_sent"] += 1
        elif ibkr_executed:
            stats["signals_sent"] += 1
            stats["buy_sent"] += 1
        if journal_enabled:
            append_journal_event(
                journal_file,
                {
                    "event": "signal_buy",
                    "ticker": final_ticker,
                    "decision": decision,
                    "portfolio_profile": str(state.get("portfolio_profile", "")),
                    "confidence": conf_val,
                    "price": float(analysis["current_price"]),
                    "deployed_budget_usd": deployed_usd,
                    "remaining_budget_usd": remaining_usd,
                    "stop_loss": parsed.get("stop_loss", ""),
                    "target_1": parsed.get("objectif_1", ""),
                    "size_usd": requested_notional_usd,
                    "fund_score": int(fund_data.get("fund_score", 50)),
                    "risk_off": bool(fund_data.get("risk_off", False)),
                    "mtf_score": int(higher_tf.get("trend_score", 0)),
                    "ibkr_auto": ibkr_executed,
                    "buy_rank_score": float(buy.get("rank_score", 0.0) or 0.0),
                    **entry_context_fields(
                        ticker=final_ticker,
                        analysis=analysis,
                        fund_data=fund_data,
                    ),
                },
            )
    except Exception as exc:
        print(f"[{ticker}] Erreur Telegram: {exc}")
    return ibkr_executed or (not ibkr_auto_execute_enabled())


def _execute_or_swap_ranked_buy(
    buy: Dict[str, Any],
    *,
    positions: Dict[str, Dict[str, object]],
    position_file: str,
    telegram_token: str,
    telegram_chat_id: str,
    journal_file: str,
    journal_enabled: bool,
    stats: Dict[str, int],
    budget_usd: float,
    max_open_positions: int,
    risk_block_buys: bool,
) -> None:
    """Achat direct si slot/cash, sinon proposition ou execution SWITCH (rotation)."""
    final_ticker = normalize_ticker(str(buy.get("final_ticker", "")))
    scan_ticker = str(buy.get("scan_ticker", final_ticker))
    parsed = buy["parsed"]
    conf_val = int(buy["conf_val"])
    rank_score = float(buy.get("rank_score", 0.0) or 0.0)

    if risk_block_buys:
        print(f"[{final_ticker}] ACHETER classement ignore: garde-fou DD journalier actif.")
        return

    state = normalize_position_state(positions.get(final_ticker, {}))
    if state["in_position"] or state["pending_sell"]:
        print(f"[{final_ticker}] ACHETER classement ignore: deja en position ou vente en attente.")
        return

    theme_block, theme_note = theme_exposure_blocks(positions, final_ticker)
    if theme_block:
        print(f"[{final_ticker}] ACHETER classement ignore: {theme_note}.")
        return

    effective_budget_usd = compute_effective_budget_usd(budget_usd, journal_file)
    deployed_usd = deployed_budget_usd(positions, effective_budget_usd)
    remaining_usd = max(0.0, effective_budget_usd - deployed_usd)
    slots_used_now = count_portfolio_slots_used(positions)
    req = float(buy.get("requested_notional_usd", 0.0) or 0.0)
    buyable_usd = buyable_remaining_usd(
        effective_budget_usd,
        deployed_usd,
        slots_used=slots_used_now,
        max_open_positions=max_open_positions,
    )
    cash_for_direct = buyable_usd if buyable_usd > 0 else remaining_usd

    can_buy_direct = (
        slots_used_now < max_open_positions
        and not buy_needs_budget_swap(
            req,
            cash_for_direct,
            float((buy.get("analysis") or {}).get("current_price", 0.0) or 0.0),
        )
    )
    if can_buy_direct:
        buy["deployed_usd"] = deployed_usd
        buy["remaining_usd"] = remaining_usd
        _execute_pending_scan_buy(
            buy,
            positions=positions,
            position_file=position_file,
            telegram_token=telegram_token,
            telegram_chat_id=telegram_chat_id,
            journal_file=journal_file,
            journal_enabled=journal_enabled,
            stats=stats,
        )
        return

    reason = "slots_full" if slots_used_now >= max_open_positions else "budget"
    swap_ok = maybe_send_swap_opportunity(
        new_ticker=final_ticker,
        new_confidence=conf_val,
        positions=positions,
        position_file=position_file,
        bot_token=telegram_token,
        chat_id=telegram_chat_id,
        reason=reason,
        slots_used=slots_used_now,
        max_open_positions=max_open_positions,
        remaining_usd=remaining_usd,
        requested_notional_usd=req,
        stop_loss=parse_price_value(parsed.get("stop_loss", "")),
        take_profit=parse_price_value(parsed.get("objectif_1", "")),
        justification=str(parsed.get("justification", "") or ""),
        signal_rank_score=float(buy.get("rank_score", 0.0) or 0.0),
        new_mtf_score=int((buy.get("higher_tf") or {}).get("trend_score", 0) or 0),
    )
    if swap_ok:
        mode = "auto IBKR" if swap_auto_execute_enabled() else "Telegram (/swap_oui)"
        print(
            f"[{scan_ticker}] SWITCH {mode} ({reason}, score priorite {rank_score:.1f}) "
            f"— rotation vers {final_ticker}."
        )
        stats["signals_sent"] += 1
        stats["buy_sent"] += 1
        return

    print(
        f"[{final_ticker}] ACHETER classe (score {rank_score:.1f}) sans slot/cash: "
        f"swap non retenu ({reason}) — confiance ou regles swap insuffisantes vs la ligne la plus faible."
    )


def _collect_ticker_scan_cache(tickers: List[str], ctx: Dict[str, Any]) -> Dict[str, Optional[Dict[str, Any]]]:
    """Phase LLM en parallele uniquement (desactive par defaut)."""
    cache: Dict[str, Optional[Dict[str, Any]]] = {}
    workers = min(scan_parallel_workers(), len(tickers))
    print(f"[Scan] Mode parallele: {workers} workers pour {len(tickers)} tickers.")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_scan_ticker_llm_worker, tk, ctx): tk for tk in tickers}
        for fut in as_completed(futures):
            tk = futures[fut]
            try:
                _, outcome = fut.result()
                cache[tk] = outcome
            except Exception as exc:
                print(f"[{tk}] Scan parallele en erreur: {exc}")
                cache[tk] = {"error": str(exc)}
    return cache


def run_cycle() -> Dict[str, int]:
    """Lance une analyse complete sur tous les tickers configures."""
    stats = {"signals_sent": 0, "buy_sent": 0, "sell_sent": 0, "scanned": 0}
    load_runtime_env()

    groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
    groq_model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant").strip()
    groq_base_url = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip()
    decision_mode = scan_decision_mode()
    position_file = os.getenv("POSITION_FILE", POSITION_FILE_DEFAULT).strip() or POSITION_FILE_DEFAULT
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    yf_period = os.getenv("YF_PERIOD", "5d").strip()
    yf_interval = os.getenv("YF_INTERVAL", "15m").strip()
    offset_file = os.getenv("TELEGRAM_OFFSET_FILE", TELEGRAM_OFFSET_FILE_DEFAULT).strip() or TELEGRAM_OFFSET_FILE_DEFAULT
    premarket_enabled = os.getenv("PREMARKET_PREP_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        premarket_window_min = float(os.getenv("PREMARKET_PREP_WINDOW_MIN", "120"))
    except ValueError:
        premarket_window_min = 120.0
    try:
        premarket_max_tickers = int(os.getenv("PREMARKET_PREP_MAX_TICKERS", "3"))
    except ValueError:
        premarket_max_tickers = 3
    premarket_state_file = (
        os.getenv("PREMARKET_PREP_STATE_FILE", PREMARKET_PREP_STATE_FILE_DEFAULT).strip()
        or PREMARKET_PREP_STATE_FILE_DEFAULT
    )
    daily_report_enabled = os.getenv("DAILY_REPORT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    objective_days_raw = os.getenv("TRADING_OBJECTIVE_DAYS", "").strip()
    if objective_days_raw:
        try:
            objective_days = int(objective_days_raw)
        except ValueError:
            objective_days = 0
    else:
        objective_days = 0

    try:
        budget_usd = float(os.getenv("TRADING_BUDGET_USD", "1000"))
    except ValueError:
        budget_usd = 1000.0
    try:
        horizon_days = int(os.getenv("TRADING_HORIZON_DAYS", "7"))
    except ValueError:
        horizon_days = 7
    horizon_days = max(1, min(365, horizon_days))
    if objective_days <= 0:
        objective_days = horizon_days
    try:
        risk_per_trade_pct = float(os.getenv("TRADING_RISK_PER_TRADE_PCT", "1.0"))
    except ValueError:
        risk_per_trade_pct = 1.0
    try:
        max_open_positions = int(os.getenv("TRADING_MAX_OPEN_POSITIONS", "2"))
    except ValueError:
        max_open_positions = 2
    manage_open_tickers = os.getenv("TICKERS_MANAGE_OPEN", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        min_conf_acheter = int(os.getenv("TRADING_MIN_CONFIDENCE_ACHETER", "65"))
    except ValueError:
        min_conf_acheter = 65
    try:
        sell_pending_reminder_min = float(os.getenv("SELL_PENDING_REMINDER_MIN", "25"))
    except ValueError:
        sell_pending_reminder_min = 25.0
    sell_pending_reminder_sec = max(60.0, sell_pending_reminder_min * 60.0)
    buy_confirmation_enabled = os.getenv("BUY_CONFIRMATION_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    buy_require_macd_cross = os.getenv("BUY_REQUIRE_MACD_CROSS", "1").strip().lower() in {"1", "true", "yes", "y"}
    buy_block_falling_knife = os.getenv("BUY_BLOCK_FALLING_KNIFE", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        buy_min_rsi = float(os.getenv("BUY_MIN_RSI", "35"))
    except ValueError:
        buy_min_rsi = 35.0
    buy_require_rr = os.getenv("BUY_REQUIRE_RR", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        buy_min_rr = float(os.getenv("BUY_MIN_RR", "1.2"))
    except ValueError:
        buy_min_rr = 1.2
    buy_block_near_3mo_high = os.getenv("BUY_BLOCK_NEAR_3MO_HIGH", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        buy_max_dist_to_3mo_high_pct = float(os.getenv("BUY_MAX_DIST_TO_3MO_HIGH_PCT", "1.8"))
    except ValueError:
        buy_max_dist_to_3mo_high_pct = 1.8
    try:
        buy_breakout_over_3mo_high_pct = float(os.getenv("BUY_BREAKOUT_OVER_3MO_HIGH_PCT", "0.35"))
    except ValueError:
        buy_breakout_over_3mo_high_pct = 0.35
    mtf_enabled = os.getenv("MTF_FILTER_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    mtf_period = os.getenv("MTF_PERIOD", "3mo").strip() or "3mo"
    mtf_interval = os.getenv("MTF_INTERVAL", "1h").strip() or "1h"
    try:
        mtf_min_buy_score = int(os.getenv("MTF_MIN_BUY_SCORE", "0"))
    except ValueError:
        mtf_min_buy_score = 0
    journal_enabled = os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    journal_file = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
    fundamental_enabled = os.getenv("FUNDAMENTAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        fundamental_min_score = int(os.getenv("FUNDAMENTAL_MIN_SCORE", "45"))
    except ValueError:
        fundamental_min_score = 45
    try:
        earnings_block_days = int(os.getenv("EARNINGS_BLOCK_DAYS", "1"))
    except ValueError:
        earnings_block_days = 1
    try:
        fundamental_cache_hours = float(os.getenv("FUNDAMENTAL_CACHE_HOURS", "24"))
    except ValueError:
        fundamental_cache_hours = 24.0
    try:
        risk_off_vix_threshold = float(os.getenv("RISK_OFF_VIX_THRESHOLD", "22"))
    except ValueError:
        risk_off_vix_threshold = 22.0
    fundamental_cache_file = (
        os.getenv("FUNDAMENTAL_CACHE_FILE", FUNDAMENTAL_CACHE_FILE_DEFAULT).strip()
        or FUNDAMENTAL_CACHE_FILE_DEFAULT
    )
    positions = load_positions(position_file)
    if ibkr_auto_execute_enabled():
        sync_positions_closed_at_ibkr(
            positions,
            position_file,
            journal_file=journal_file,
            journal_enabled=journal_enabled,
            bot_token=telegram_token,
            chat_id=telegram_chat_id,
        )
    maybe_intraday_flat_positions(
        positions,
        position_file,
        journal_file=journal_file,
        journal_enabled=journal_enabled,
        bot_token=telegram_token,
        chat_id=telegram_chat_id,
    )
    tighten_open_positions_take_profit(
        positions,
        position_file=position_file,
    )
    if ibkr_auto_execute_enabled():
        update_trailing_stop_for_positions(positions, position_file)
        synced_sl_tp = sync_open_positions_sl_tp_at_ibkr(
            positions,
            position_file,
        )
        if synced_sl_tp > 0:
            print(f"[IBKR] Sync SL/TP: {synced_sl_tp} position(s) alignee(s).")
    base_tickers = get_tickers_from_env()
    tickers = build_scan_tickers(
        base_tickers,
        positions,
        include_open_positions=manage_open_tickers,
    )
    base_set = set(base_tickers)
    extra_managed = [tk for tk in tickers if tk not in base_set]
    if extra_managed:
        print(f"[Universe] Gestion hors watchlist (positions ouvertes): {', '.join(extra_managed)}")
    if scan_prioritize_momentum_enabled():
        tickers = prioritize_scan_tickers(tickers, positions)

    if not is_us_market_open():
        now_est = datetime.now(MARKET_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        print(f"Marche US ferme ({now_est}). Aucun scan lance.")
        if premarket_enabled:
            run_premarket_preparation(
                tickers=tickers,
                yf_period=yf_period,
                yf_interval=yf_interval,
                telegram_token=telegram_token,
                telegram_chat_id=telegram_chat_id,
                position_file=position_file,
                journal_file=journal_file,
                base_budget_usd=budget_usd,
                objective_days=objective_days,
                send_daily_report=daily_report_enabled,
                state_file=premarket_state_file,
                window_minutes=max(15.0, premarket_window_min),
                max_items=max(1, premarket_max_tickers),
            )
        return stats

    if decision_mode == "llm" and not groq_api_key:
        print("GROQ_API_KEY manquante dans .env (mode SCAN_DECISION_MODE=llm)")
        return stats
    if decision_mode in {"hybrid", "rules"} and not groq_api_key:
        print(f"[Scan] Mode {decision_mode}: scan sans Groq actif (regles deterministes).")
    if not telegram_token or not telegram_chat_id:
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquante dans .env")
        return stats

    print(
        f"Marche ouvert. Debut du scan (budget ref. {budget_usd:.0f} USD, horizon {horizon_days} j)..."
    )
    if _yf_backoff_active():
        finnhub_ok = bool(os.getenv("FINNHUB_API_KEY", "").strip()) and _data_fallback_to_finnhub_enabled()
        ibkr_ok = _data_fallback_to_ibkr_enabled()
        if finnhub_ok or ibkr_ok:
            via = []
            if finnhub_ok:
                via.append("Finnhub")
            if ibkr_ok:
                via.append("IBKR")
            print(
                f"[DATA] Yahoo en cooldown ({_yf_backoff_seconds_left():.0f}s). "
                f"Scan continue via {'/'.join(via)} (bougies)."
            )
        else:
            print(
                f"[DATA] Yahoo en cooldown auto ({_yf_backoff_seconds_left():.0f}s restantes). "
                "Aucun fallback bougies (Finnhub/IBKR) — cycle saute."
            )
            return stats
    now_ny_cycle = datetime.now(MARKET_TZ)
    maybe_intraday_flat_positions(
        positions,
        position_file,
        journal_file=journal_file,
        journal_enabled=journal_enabled,
        bot_token=telegram_token,
        chat_id=telegram_chat_id,
    )
    maybe_max_hold_exit_positions(
        positions,
        position_file,
        journal_file=journal_file,
        journal_enabled=journal_enabled,
        bot_token=telegram_token,
        chat_id=telegram_chat_id,
    )
    pdt_block_buys, pdt_count, pdt_limit = pdt_guard_blocks_buys(journal_file)
    if pdt_block_buys:
        print(f"[RISK] Garde-fou PDT: {pdt_count} day trade(s) sur 5 jours ouvres "
              f"(limite {pdt_limit}) — nouvelles entrees bloquees.")
    eq_block_buys, eq_dd_pct, eq_dd_limit = equity_drawdown_blocks_buys(budget_usd, journal_file)
    if eq_block_buys:
        print(f"[RISK] COUPE-CIRCUIT: drawdown realise {eq_dd_pct:.1f}% <= -{eq_dd_limit:.0f}% "
              f"— plus aucune nouvelle entree (reprise manuelle).")
        notify_equity_breaker_once(telegram_token, telegram_chat_id, eq_dd_pct, eq_dd_limit)
    effective_budget_for_dd = compute_effective_budget_usd(budget_usd, journal_file)
    risk_block_buys, day_pnl_usd, dd_limit_usd = risk_new_buys_blocked(
        budget_usd=effective_budget_for_dd,
        journal_file=journal_file,
        now_ny=datetime.now(MARKET_TZ),
    )
    if risk_block_buys:
        print(
            f"[RISK] Nouveaux ACHETER bloques: PnL jour {day_pnl_usd:.2f} USD <= -{dd_limit_usd:.2f} USD (limite DD)."
        )

    fundamental_cache = load_json_dict(fundamental_cache_file) if fundamental_enabled else {}
    risk_off_regime = get_market_regime_risk_off(vix_threshold=risk_off_vix_threshold) if fundamental_enabled else False
    effective_budget_scan = compute_effective_budget_usd(budget_usd, journal_file)
    deployed_at_scan_start = deployed_budget_usd(positions, effective_budget_scan)
    positions_snapshot = {
        normalize_ticker(k): copy.deepcopy(normalize_position_state(v))
        for k, v in positions.items()
    }
    scan_ctx: Dict[str, Any] = {
        "yf_period": yf_period,
        "yf_interval": yf_interval,
        "groq_model": groq_model,
        "groq_api_key": groq_api_key,
        "groq_base_url": groq_base_url,
        "scan_decision_mode": decision_mode,
        "fundamental_enabled": fundamental_enabled,
        "fundamental_cache": fundamental_cache,
        "fundamental_cache_hours": fundamental_cache_hours,
        "risk_off_regime": risk_off_regime,
        "mtf_enabled": mtf_enabled,
        "mtf_period": mtf_period,
        "mtf_interval": mtf_interval,
        "horizon_days": horizon_days,
        "risk_per_trade_pct": risk_per_trade_pct,
        "max_open_positions": max_open_positions,
        "effective_budget_usd": effective_budget_scan,
        "deployed_for_prompt": deployed_at_scan_start,
        "positions_snapshot": positions_snapshot,
        "fund_lock": _FUNDAMENTAL_CACHE_LOCK,
    }
    use_parallel_scan = scan_parallel_enabled() and len(tickers) > 1
    scan_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    if use_parallel_scan:
        scan_cache = _collect_ticker_scan_cache(tickers, scan_ctx)
    else:
        print(f"[Scan] Mode sequentiel: {len(tickers)} ticker(s), un par un.")
    pending_buys: List[Dict[str, Any]] = []

    for ticker in tickers:
        # Permet de traiter les confirmations Telegram entre deux tickers.
        positions = poll_telegram_confirmations(
            bot_token=telegram_token,
            allowed_chat_id=telegram_chat_id,
            position_file=position_file,
            offset_file=offset_file,
            positions=positions,
        )

        if use_parallel_scan:
            oc = scan_cache.get(ticker)
        else:
            _, oc = _scan_ticker_llm_worker(ticker, scan_ctx)
        if oc is None:
            continue
        if oc.get("error"):
            print(f"[{ticker}] Scan ignore: {oc['error']}")
            continue
        analysis = refresh_analysis_live_price(
            ticker,
            oc["analysis"],
            positions,
        )
        stats["scanned"] += 1
        fund_data = oc["fund_data"]
        fund_score_val = int(oc["fund_score_val"])
        higher_tf = oc["higher_tf"]
        llm_answer = str(oc["llm_answer"])
        model_used = str(oc["model_used"])
        parsed = dict(oc["parsed"])
        decision = str(oc["decision"]).upper()
        final_ticker = normalize_ticker(str(oc["final_ticker"]))
        momentum_score = float(oc.get("momentum_score", 0.0) or 0.0)

        try:
            if ticker not in positions:
                positions[ticker] = {}
            check_take_profit_alert(
                ticker=ticker,
                current_price=float(analysis["current_price"]),
                state=positions[ticker],
                bot_token=telegram_token,
                chat_id=telegram_chat_id,
                position_file=position_file,
                positions=positions,
            )
            check_stop_loss_alert(
                ticker=ticker,
                current_price=float(analysis["current_price"]),
                state=positions[ticker],
                bot_token=telegram_token,
                chat_id=telegram_chat_id,
                position_file=position_file,
                positions=positions,
            )
        except Exception as exc:
            print(f"[{ticker}] Verification TP/SL ignoree: {exc}")

        state = normalize_position_state(positions.get(final_ticker, {}))
        decision = adjust_sell_decision_after_live_price(
            final_ticker,
            decision,
            analysis,
            state,
            parsed,
        )
        override_reason = maybe_force_proactive_near_tp_sell(
            ticker=final_ticker,
            decision=decision,
            analysis=analysis,
            state=state,
            parsed=parsed,
        )
        if not override_reason:
            override_reason = maybe_force_structure_invalidation_sell(
                ticker=final_ticker,
                decision=decision,
                analysis=analysis,
                state=state,
                parsed=parsed,
                higher_tf=higher_tf,
            )
        if override_reason:
            decision = "VENDRE"
            print(f"[{ticker}] Override decision: ATTENDRE -> VENDRE ({override_reason}).")
        corrected_justif = sanitize_sell_justification_for_position(
            ticker=final_ticker,
            decision=decision,
            current_price=float(analysis["current_price"]),
            state=state,
            parsed=parsed,
        )
        if corrected_justif:
            print(f"[{ticker}] Justification VENDRE corrigee (coherence P/L).")
        rewrite_decision_justification(
            ticker=final_ticker,
            decision=decision,
            parsed=parsed,
            analysis=analysis,
            state=state,
        )
        parsed["decision"] = decision

        label = "Reponse IA" if not model_used.startswith("rules-v1/") else "Reponse moteur regles"
        print(f"\n[{ticker}] {label} (modele: {model_used}):\n{llm_answer}\n")
        if fundamental_enabled:
            print(
                f"[{ticker}] Fonda: score={fund_score_val}, "
                f"earnings_days={fund_data.get('earnings_days')}, risk_off={bool(fund_data.get('risk_off', False))}"
            )
        if mtf_enabled:
            print(f"[{ticker}] MTF: {higher_tf.get('summary', 'Indisponible')}")

        if decision == "ACHETER":
            buy_blocked_near_close, mins_to_close = is_buy_blocked_near_close()
            if buy_blocked_near_close:
                print(
                    f"[{ticker}] Decision ACHETER ignoree: fin de seance "
                    f"(~{int(mins_to_close)} min avant cloture NY, nouvelles entrees bloquees)."
                )
                continue
            if risk_block_buys:
                print(
                    f"[{ticker}] Decision ACHETER ignorée: garde-fou DD journalier actif "
                    f"(PnL jour {day_pnl_usd:.2f} USD, limite -{dd_limit_usd:.2f} USD)."
                )
                continue
            if pdt_block_buys:
                print(
                    f"[{ticker}] Decision ACHETER ignoree: garde-fou PDT "
                    f"({pdt_count} day trades / 5 jours ouvres, limite {pdt_limit})."
                )
                continue
            if eq_block_buys:
                print(
                    f"[{ticker}] Decision ACHETER ignoree: coupe-circuit "
                    f"(drawdown realise {eq_dd_pct:.1f}%, limite -{eq_dd_limit:.0f}%)."
                )
                continue
            slots_used = count_portfolio_slots_used(positions)
            # Pas de duplication si deja achete ou achat deja en attente.
            if state["in_position"] or state["pending_sell"]:
                print(
                    f"[{ticker}] Decision ACHETER ignorée: deja en position "
                    "ou vente en attente a confirmer."
                )
                continue

            conf_val = parse_confidence(parsed.get("confiance", ""))
            if conf_val is None:
                print(f"[{ticker}] Decision ACHETER ignorée: CONFIANCE manquante ou illisible.")
                continue
            dynamic_min_conf = min_conf_acheter
            if bool(fund_data.get("risk_off", False)):
                try:
                    risk_off_bonus = int(os.getenv("TRADING_MIN_CONFIDENCE_RISK_OFF_BONUS", "6"))
                except ValueError:
                    risk_off_bonus = 6
                dynamic_min_conf = min(100, max(0, min_conf_acheter + max(0, risk_off_bonus)))
            if conf_val < dynamic_min_conf:
                print(
                    f"[{ticker}] Decision ACHETER ignoree: "
                    f"confiance {conf_val} < seuil {dynamic_min_conf}."
                )
                continue
            sl_work = parse_price_value(parsed.get("stop_loss", ""))
            tp_work = parse_price_value(parsed.get("objectif_1", ""))
            try:
                atr_sl = float(analysis["atr"])
            except (TypeError, ValueError, KeyError):
                atr_sl = 0.0
            sl_work, tp_work, sl_floor_note = adjust_sl_tp_atr_floor(
                entry_price=float(analysis["current_price"]),
                stop_loss=sl_work,
                take_profit=tp_work,
                atr=atr_sl,
                ticker=final_ticker,
                min_rr=buy_min_rr if buy_require_rr else None,
                notional_usd=None,
            )
            if sl_floor_note:
                print(f"[{ticker}] {sl_floor_note}")
            if sl_work is not None:
                parsed["stop_loss"] = f"{sl_work:.2f}"
            if tp_work is not None:
                parsed["objectif_1"] = f"{tp_work:.2f}"
            effective_budget_usd = compute_effective_budget_usd(budget_usd, journal_file)
            deployed_usd = deployed_budget_usd(positions, effective_budget_usd)
            remaining_usd = max(0.0, effective_budget_usd - deployed_usd)
            slots_used_now = int(count_portfolio_slots_used(positions))
            slots_left = max(0, int(max_open_positions) - slots_used_now)
            buyable_usd = buyable_remaining_usd(
                effective_budget_usd,
                deployed_usd,
                slots_used=slots_used_now,
                max_open_positions=max_open_positions,
            )
            if reserve_cash_per_free_slot() > 0 and slots_left > 0 and buyable_usd + 1.0 < remaining_usd:
                print(
                    f"[Budget] Reserve {reserve_cash_per_free_slot():.0f} USD/slot libre "
                    f"({slots_left} slot(s)) — achat direct ~{buyable_usd:.0f} USD "
                    f"(cash {remaining_usd:.0f} USD)."
                )
            derived_line_usd, dyn_min_usd, dyn_max_usd = derive_buy_notional_usd(
                current_price=float(analysis["current_price"]),
                atr=float(analysis["atr"]),
                conviction=float(conf_val),
                remaining_usd=buyable_usd if buyable_usd > 0 else remaining_usd,
                slots_left=slots_left,
                risk_off=bool(fund_data.get("risk_off", False)),
                positions=positions,
                max_open_positions=max_open_positions,
            )
            parsed_size = parse_notional_usd(parsed.get("taille_suggeree_usd", ""))
            clamp_llm_to_band = os.getenv(
                "TRADING_CLAMP_LLM_NOTIONAL_TO_DYNAMIC_BAND",
                "1",
            ).strip().lower() in {"1", "true", "yes", "y"}
            if parsed_size is not None and parsed_size > 0:
                ps = float(parsed_size)
                if clamp_llm_to_band and dyn_max_usd > 0:
                    bounded = max(dyn_min_usd, min(dyn_max_usd, ps))
                    requested_notional_usd = min(remaining_usd, bounded)
                    if abs(requested_notional_usd - ps) > 1.0:
                        print(
                            f"[{ticker}] Taille modele ({ps:.0f} USD) cadree dans le couloir "
                            f"dynamique [{dyn_min_usd:.0f}, {dyn_max_usd:.0f}] "
                            f"-> {requested_notional_usd:.0f} USD."
                        )
                else:
                    requested_notional_usd = ps
            else:
                if derived_line_usd <= 0:
                    print(
                        f"[{ticker}] Decision ACHETER ignorée: TAILLE_SUGGEREE_USD absente ou 0 "
                        "et aucun montant derivable (budget insuffisant)."
                    )
                    continue
                requested_notional_usd = derived_line_usd
                if slots_left <= 0:
                    print(
                        f"[{ticker}] Slots pleins — montant rotation swap "
                        f"(sizing dynamique ATR+confiance): {derived_line_usd:.0f} USD."
                    )
                else:
                    print(
                        f"[{ticker}] TAILLE IA absente — montant derive (sizing dynamique ATR+confiance): "
                        f"{derived_line_usd:.0f} USD."
                    )
            entry_px_for_ticket = float(analysis["current_price"])
            if (
                ibkr_auto_execute_enabled()
                and swap_on_budget_enabled()
                and buy_needs_budget_swap(requested_notional_usd, remaining_usd, entry_px_for_ticket)
            ):
                ticket_target = buy_min_ticket_usd(requested_notional_usd, entry_px_for_ticket)
                requested_notional_usd = ticket_target
                print(
                    f"[{ticker}] Cash libre {remaining_usd:.2f} USD < ticket ~{ticket_target:.0f} USD "
                    f"— candidat rotation swap (budget)."
                )
            if fundamental_enabled and (os.getenv("FUNDAMENTAL_SIZE_BONUS_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}):
                try:
                    strong_threshold = int(os.getenv("FUNDAMENTAL_SIZE_BONUS_THRESHOLD", "70"))
                except ValueError:
                    strong_threshold = 70
                try:
                    strong_mult = float(os.getenv("FUNDAMENTAL_SIZE_BONUS_MULT", "1.08"))
                except ValueError:
                    strong_mult = 1.08
                strong_mult = max(1.0, min(1.30, strong_mult))
                if fund_score_val >= strong_threshold and requested_notional_usd > 0:
                    boosted = requested_notional_usd * strong_mult
                    if swap_on_budget_enabled() and buy_needs_budget_swap(
                        boosted, remaining_usd, entry_px_for_ticket
                    ):
                        requested_notional_usd = boosted
                    else:
                        cap_usd = buyable_usd if buyable_usd > 0 else remaining_usd
                        requested_notional_usd = min(cap_usd, boosted)
                    print(
                        f"[{ticker}] Sizing fonda bonus: score {fund_score_val} >= {strong_threshold}, "
                        f"taille {requested_notional_usd:.0f} USD (x{strong_mult:.2f})."
                    )
            sl_for_risk = parse_price_value(parsed.get("stop_loss", ""))
            if requested_notional_usd > 0:
                capped_risk, was_capped = apply_risk_cap_to_notional(
                    requested_notional_usd,
                    entry_price=float(analysis["current_price"]),
                    stop_price=sl_for_risk,
                    effective_budget_usd=effective_budget_usd,
                )
                if was_capped:
                    print(
                        f"[{ticker}] Sizing plafonne par risque ({risk_per_trade_pct:.1f}% budget): "
                        f"{requested_notional_usd:.0f} -> {capped_risk:.0f} USD."
                    )
                requested_notional_usd = capped_risk
            if requested_notional_usd <= 1e-6:
                print(f"[{ticker}] Decision ACHETER ignorée: taille nulle apres plafond risque.")
                continue
            sl_work, tp_work, tp_cap_note = adjust_sl_tp_atr_floor(
                entry_price=float(analysis["current_price"]),
                stop_loss=sl_work,
                take_profit=tp_work,
                atr=atr_sl,
                ticker=final_ticker,
                min_rr=buy_min_rr if buy_require_rr else None,
                notional_usd=requested_notional_usd,
            )
            if tp_cap_note:
                print(f"[{ticker}] {tp_cap_note}")
                if tp_work is not None:
                    print(
                        f"[{ticker}] TP final: {tp_work:.2f} USD "
                        f"(ticket {requested_notional_usd:.0f} USD, "
                        f"cap {'petit' if requested_notional_usd < buy_max_tp_notional_threshold_usd() else 'gros'})."
                    )
            if sl_work is not None:
                parsed["stop_loss"] = f"{sl_work:.2f}"
            if tp_work is not None:
                parsed["objectif_1"] = f"{tp_work:.2f}"
            if ibkr_auto_execute_enabled():
                entry_px_prep = float(analysis["current_price"])
                if swap_on_budget_enabled() and buy_needs_budget_swap(
                    requested_notional_usd, remaining_usd, entry_px_prep
                ):
                    requested_notional_usd = buy_min_ticket_usd(
                        requested_notional_usd, entry_px_prep
                    )
                prepared_notional, ibkr_prep_err = ibkr_auto_buy_prepare_notional(
                    requested_notional_usd,
                    price_usd=entry_px_prep,
                    remaining_usd=remaining_usd,
                    ticker=final_ticker,
                )
                if ibkr_prep_err:
                    ticket = buy_min_ticket_usd(requested_notional_usd, entry_px_prep)
                    if ibkr_prep_swap_budget_eligible(
                        ibkr_prep_err,
                        requested_notional_usd=ticket,
                        remaining_usd=remaining_usd,
                        price_usd=entry_px_prep,
                    ):
                        requested_notional_usd = ticket
                        print(
                            f"[{ticker}] {ibkr_prep_err} "
                            f"— eligible rotation swap budget (~{requested_notional_usd:.0f} USD)."
                        )
                    else:
                        print(f"[{ticker}] Decision ACHETER ignorée (auto IBKR): {ibkr_prep_err}")
                        continue
                else:
                    if prepared_notional > requested_notional_usd + 1.0:
                        print(
                            f"[{ticker}] Ticket ajuste pour 1 action entiere: "
                            f"{requested_notional_usd:.0f} -> {prepared_notional:.0f} USD "
                            f"(prix ~{float(analysis['current_price']):.2f})."
                        )
                    requested_notional_usd = prepared_notional
            if buy_confirmation_enabled:
                rsi_now = float(analysis["rsi"])
                macd_now = float(analysis["macd_value"])
                macd_sig_now = float(analysis["macd_signal"])
                if buy_block_falling_knife and rsi_now < buy_min_rsi and macd_now < macd_sig_now:
                    print(
                        f"[{ticker}] Decision ACHETER ignorée: anti-falling-knife (RSI {rsi_now:.2f} < {buy_min_rsi:.1f} et MACD < signal)."
                    )
                    continue
                chase_block, chase_note = buy_chase_too_extended(analysis)
                if chase_block:
                    print(f"[{ticker}] Decision ACHETER ignoree: {chase_note}.")
                    continue
                reentry_block, reentry_note = reentry_blocked_by_cooldown(
                    final_ticker,
                    journal_file,
                    signal_price=float(analysis["current_price"]),
                )
                if reentry_block:
                    print(f"[{ticker}] Decision ACHETER ignoree: {reentry_note}.")
                    continue
                if buy_require_macd_cross and macd_now < macd_sig_now:
                    early_ok, early_note = early_entry_signal_ok(
                        analysis,
                        higher_tf,
                        rsi=rsi_now,
                        macd_val=macd_now,
                        macd_sig=macd_sig_now,
                    )
                    if early_ok:
                        print(f"[{ticker}] Entree anticipee: {early_note}.")
                    else:
                        print(
                            f"[{ticker}] Decision ACHETER ignorée: MACD sous le signal "
                            f"(confirmation haussiere absente)."
                        )
                        continue

                if buy_require_rr:
                    current_price = float(analysis["current_price"])
                    stop_price = parse_price_value(parsed.get("stop_loss", ""))
                    target_price = parse_price_value(parsed.get("objectif_1", ""))
                    if stop_price is None or target_price is None:
                        print(f"[{ticker}] Decision ACHETER ignorée: stop/objectif non exploitables pour verifier le RR.")
                        continue
                    if not (stop_price < current_price < target_price):
                        print(
                            f"[{ticker}] Decision ACHETER ignorée: structure prix incoherente (stop={stop_price:.2f}, prix={current_price:.2f}, objectif={target_price:.2f})."
                        )
                        continue
                    risk = current_price - stop_price
                    reward = target_price - current_price
                    rr = reward / risk if risk > 0 else 0.0
                    if not risk_reward_meets_minimum(rr, buy_min_rr):
                        print(
                            f"[{ticker}] Decision ACHETER ignorée: RR {rr:.4f} < seuil {buy_min_rr:.2f} "
                            f"(stop {stop_price:.2f}, prix {current_price:.2f}, objectif {target_price:.2f})."
                        )
                        continue

                if buy_block_near_3mo_high:
                    current_price = float(analysis["current_price"])
                    high_3mo = float(higher_tf.get("period_high", 0.0) or 0.0)
                    if high_3mo > 0 and current_price > 0:
                        dist_to_high_pct = ((high_3mo - current_price) / high_3mo) * 100.0
                        breakout_ok = current_price >= (high_3mo * (1.0 + (buy_breakout_over_3mo_high_pct / 100.0)))
                        near_high = dist_to_high_pct <= buy_max_dist_to_3mo_high_pct
                        if near_high and (not breakout_ok):
                            print(
                                f"[{ticker}] Decision ACHETER ignorée: prix trop proche du plus haut 3mo "
                                f"({current_price:.2f} vs high {high_3mo:.2f}, dist {dist_to_high_pct:.2f}% <= {buy_max_dist_to_3mo_high_pct:.2f}%) "
                                f"sans breakout confirme (+{buy_breakout_over_3mo_high_pct:.2f}% requis)."
                            )
                            continue
            if mtf_enabled:
                ht_score = int(higher_tf.get("trend_score", 0))
                if ht_score < mtf_min_buy_score:
                    print(
                        f"[{ticker}] Decision ACHETER ignorée: filtre MTF ({ht_score}) < seuil {mtf_min_buy_score}."
                    )
                    continue
            if fundamental_enabled:
                fund_score = fund_score_val
                earnings_days_val = fund_data.get("earnings_days")
                near_earnings = isinstance(earnings_days_val, int) and 0 <= earnings_days_val <= earnings_block_days
                if fund_score < fundamental_min_score:
                    fund_exc_ok, fund_exc_note = breakout_fundamental_exception_ok(
                        ticker=final_ticker,
                        analysis=analysis,
                        higher_tf=higher_tf,
                        fund_score=fund_score,
                        fundamental_min_score=fundamental_min_score,
                    )
                    if fund_exc_ok:
                        print(
                            f"[{ticker}] Exception breakout haussier: fonda {fund_score} < {fundamental_min_score} "
                            f"mais entree autorisee ({fund_exc_note})."
                        )
                    else:
                        print(
                            f"[{ticker}] Decision ACHETER ignorée: score fondamental {fund_score} < seuil {fundamental_min_score}."
                        )
                        continue
                if near_earnings:
                    print(
                        f"[{ticker}] Decision ACHETER ignorée: earnings imminent (J+{earnings_days_val}), blocage actif."
                    )
                    continue

            theme_block, theme_note = theme_exposure_blocks(positions, final_ticker)
            if theme_block:
                print(f"[{ticker}] Decision ACHETER ignoree: {theme_note}.")
                continue

            rank_score = rank_buy_candidate_score(
                conf_val=conf_val,
                analysis=analysis,
                parsed=parsed,
                fund_score_val=fund_score_val,
                higher_tf=higher_tf,
                momentum_score=momentum_score,
            )
            buy_payload: Dict[str, Any] = {
                "scan_ticker": ticker,
                "final_ticker": final_ticker,
                "analysis": analysis,
                "parsed": parsed,
                "conf_val": conf_val,
                "fund_data": fund_data,
                "fund_score_val": fund_score_val,
                "higher_tf": higher_tf,
                "requested_notional_usd": requested_notional_usd,
                "deployed_usd": deployed_usd,
                "remaining_usd": remaining_usd,
                "rank_score": rank_score,
            }
            slots_used_now = int(count_portfolio_slots_used(positions))
            slots_full_now = slots_used_now >= int(max_open_positions)
            entry_px_now = float(analysis["current_price"])
            needs_budget_swap = buy_needs_budget_swap(
                float(requested_notional_usd),
                float(remaining_usd),
                entry_px_now,
            )
            if buy_execute_immediate_enabled() and not use_parallel_scan:
                if slots_full_now:
                    swap_reason = f"slots {slots_used_now}/{max_open_positions}"
                    if needs_budget_swap:
                        swap_reason += (
                            f", budget {remaining_usd:.0f} < "
                            f"~{buy_min_ticket_usd(float(requested_notional_usd), entry_px_now):.0f} USD"
                        )
                    pending_buys.append(buy_payload)
                    print(
                        f"[{ticker}] ACHETER valide — file swap fin de cycle "
                        f"({swap_reason}, score {rank_score:.1f})."
                    )
                    continue
                if needs_budget_swap:
                    swap_reason = (
                        f"budget {remaining_usd:.0f} < "
                        f"~{buy_min_ticket_usd(float(requested_notional_usd), entry_px_now):.0f} USD"
                    )
                    print(
                        f"[{ticker}] ACHETER valide (score {rank_score:.1f}) — "
                        f"swap budget immediat ({swap_reason})."
                    )
                    _execute_or_swap_ranked_buy(
                        buy_payload,
                        positions=positions,
                        position_file=position_file,
                        telegram_token=telegram_token,
                        telegram_chat_id=telegram_chat_id,
                        journal_file=journal_file,
                        journal_enabled=journal_enabled,
                        stats=stats,
                        budget_usd=budget_usd,
                        max_open_positions=max_open_positions,
                        risk_block_buys=risk_block_buys,
                    )
                    positions = load_positions(position_file)
                    if not _buy_signal_taken(positions, final_ticker):
                        pending_buys.append(buy_payload)
                        print(
                            f"[{ticker}] Swap budget immediat non retenu — "
                            f"file fin de cycle (score {rank_score:.1f})."
                        )
                    continue
                print(
                    f"[{ticker}] ACHETER valide (score {rank_score:.1f}) — "
                    f"execution immediate (slot libre)."
                )
                _execute_or_swap_ranked_buy(
                    buy_payload,
                    positions=positions,
                    position_file=position_file,
                    telegram_token=telegram_token,
                    telegram_chat_id=telegram_chat_id,
                    journal_file=journal_file,
                    journal_enabled=journal_enabled,
                    stats=stats,
                    budget_usd=budget_usd,
                    max_open_positions=max_open_positions,
                    risk_block_buys=risk_block_buys,
                )
                positions = load_positions(position_file)
                if not _buy_signal_taken(positions, final_ticker):
                    print(
                        f"[{ticker}] ACHETER non execute (ticket IBKR) — "
                        f"swap budget non retenu (garde-fous swap)."
                    )
                continue
            pending_buys.append(buy_payload)
            print(
                f"[{ticker}] ACHETER valide — file fin de cycle "
                f"(score {rank_score:.1f})."
            )
            continue

        elif decision == "VENDRE":
            if not sell_signal_enabled():
                print(
                    f"[{ticker}] Decision VENDRE ignorée: signal vendeur coupé "
                    f"(TRADING_SELL_SIGNAL_ENABLED=0) — sortie geree par SL/TP."
                )
                if journal_enabled:
                    try:
                        append_journal_event(
                            journal_file,
                            {
                                "event": "signal_sell_suppressed",
                                "ticker": final_ticker,
                                "decision": decision,
                                "price": float(analysis["current_price"]),
                                "reason": "TRADING_SELL_SIGNAL_ENABLED=0",
                            },
                        )
                    except Exception:
                        pass
                continue
            if ibkr_auto_execute_enabled():
                sync_ticker_closed_at_ibkr(
                    final_ticker,
                    positions,
                    position_file,
                    journal_file=journal_file,
                    journal_enabled=journal_enabled,
                    bot_token=telegram_token,
                    chat_id=telegram_chat_id,
                    notify_telegram=False,
                )
                state = normalize_position_state(positions.get(final_ticker, {}))
            # Règle demandee: proposer un VENDRE uniquement si achat confirme.
            if not state["in_position"]:
                print(f"[{ticker}] Decision VENDRE ignorée: position deja fermee (locale ou IBKR).")
                continue
            if state["pending_sell"]:
                last_sell_alert_ts = float(state.get("last_sell_alert_ts", 0.0) or 0.0)
                elapsed = time.time() - last_sell_alert_ts
                if elapsed < sell_pending_reminder_sec:
                    wait_left = max(0.0, sell_pending_reminder_sec - elapsed)
                    print(
                        f"[{ticker}] Decision VENDRE ignorée: vente deja en attente "
                        f"(rappel dans ~{int(wait_left)}s)."
                    )
                    continue

            ibkr_sold = False
            ibkr_require_open = os.getenv("IBKR_REQUIRE_MARKET_OPEN", "1").strip().lower() in {
                "1",
                "true",
                "yes",
                "y",
            }
            if ibkr_auto_execute_enabled() and (not ibkr_require_open or is_us_market_open()):
                ibkr_sold = attempt_ibkr_sell(
                    ticker=final_ticker,
                    positions=positions,
                    position_file=position_file,
                    journal_file=journal_file,
                    journal_enabled=journal_enabled,
                    bot_token=telegram_token,
                    chat_id=telegram_chat_id,
                    reason="signal_vendre",
                )
            if not ibkr_sold:
                was_pending_sell = bool(state.get("pending_sell"))
                state["pending_sell"] = True
                state["last_sell_alert_ts"] = time.time()
                positions[final_ticker] = state
                save_positions(position_file, positions)
                message = format_telegram_signal(decision, final_ticker, analysis, parsed)
                if was_pending_sell and not ibkr_auto_execute_enabled():
                    message = (
                        "RAPPEL VENTE (toujours en attente de confirmation)\n"
                        + message
                        + "\n\nSi ordre deja execute: /confirm_sell "
                        + final_ticker
                        + " PRIX_SORTIE"
                        + "\n(precis releve broker: /confirm_sell TICKER PRIX_SORTIE CAPITAL_INVESTI PRIX_MOYEN_ENTREE)"
                    )
                try:
                    send_telegram_alert(telegram_token, telegram_chat_id, message)
                    print(
                        f"[{ticker}] {'Rappel' if was_pending_sell else 'Alerte'} "
                        f"Telegram envoyee ({decision})."
                    )
                except Exception as exc:
                    print(f"[{ticker}] Erreur Telegram: {exc}")
            else:
                print(
                    f"[{ticker}] Vente IBKR auto — pas de 2e message signal "
                    f"(prix fill dans alerte VENTE AUTO IBKR)."
                )
            stats["signals_sent"] += 1
            stats["sell_sent"] += 1
            if journal_enabled:
                try:
                    append_journal_event(
                        journal_file,
                        {
                            "event": "signal_sell",
                            "ticker": final_ticker,
                            "decision": decision,
                            "price": float(analysis["current_price"]),
                            "stop_loss": parsed.get("stop_loss", ""),
                            "target_1": parsed.get("objectif_1", ""),
                            "fund_score": int(fund_data.get("fund_score", 50)),
                            "risk_off": bool(fund_data.get("risk_off", False)),
                            "mtf_score": int(higher_tf.get("trend_score", 0)),
                            "ibkr_auto": ibkr_sold,
                        },
                    )
                except Exception:
                    pass
        else:
            print(f"[{ticker}] Decision {decision or 'INCONNUE'}: pas d'alerte envoyee.")

    if pending_buys:
        pending_buys.sort(key=lambda b: float(b.get("rank_score", 0.0) or 0.0), reverse=True)
        order_txt = ", ".join(
            f"{b['final_ticker']}({float(b.get('rank_score', 0)):.0f})" for b in pending_buys
        )
        label = (
            "Rotation swap (slots pleins / budget), priorite"
            if buy_execute_immediate_enabled() and not use_parallel_scan
            else "Execution ACHETER par priorite"
        )
        print(f"[Scan] {label}: {order_txt}")
        for buy in pending_buys:
            positions = poll_telegram_confirmations(
                bot_token=telegram_token,
                allowed_chat_id=telegram_chat_id,
                position_file=position_file,
                offset_file=offset_file,
                positions=positions,
            )
            _execute_or_swap_ranked_buy(
                buy,
                positions=positions,
                position_file=position_file,
                telegram_token=telegram_token,
                telegram_chat_id=telegram_chat_id,
                journal_file=journal_file,
                journal_enabled=journal_enabled,
                stats=stats,
                budget_usd=budget_usd,
                max_open_positions=max_open_positions,
                risk_block_buys=risk_block_buys,
            )

    if fundamental_enabled:
        save_json_dict(fundamental_cache_file, fundamental_cache)
    return stats


def main() -> None:
    """
    Boucle semi-automatique:
    - Si le marche est ouvert, lance un cycle complet.
    - Pause ensuite pour que le **debut** du prochain cycle tombe environ toutes les N minutes
      (si le cycle est long, on ne rajoute pas N minutes en plus).
    """
    load_runtime_env()
    ensure_stdio_utf8()

    parser = argparse.ArgumentParser(
        description="Bot trading Telegram + LLM. Mode manuel (/confirm_*) ou execution auto IBKR (IBKR_AUTO_EXECUTE=1)."
    )
    parser.add_argument("--confirm-buy", dest="confirm_buy", default=None, help="Confirme que tu as achete le ticker (ex: --confirm-buy TSLA)")
    parser.add_argument("--confirm-sell", dest="confirm_sell", default=None, help="Confirme que tu as vendu le ticker (ex: --confirm-sell TSLA)")
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=None,
        help="Budget de reference en USD pour cette session (ex: 1000 ou 10000). Surcharge le .env.",
    )
    parser.add_argument(
        "--horizon-days",
        type=int,
        default=None,
        help="Horizon de trading en jours (ex: 7 pour une semaine). Surcharge le .env.",
    )
    parser.add_argument(
        "--horizon-weeks",
        type=int,
        default=None,
        help="Horizon en semaines (ex: 2 => 14 jours). Prioritaire sur --horizon-days si les deux sont passes.",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Sans effet (compatibilite): les positions ne sont jamais effacees automatiquement.",
    )
    parser.add_argument(
        "--reset-positions",
        action="store_true",
        help="Au demarrage: vide positions.json. A utiliser seulement si tu veux repartir sans aucune ligne enregistree.",
    )
    parser.add_argument(
        "--portfolio-profile",
        default="",
        help=(
            "Profil watchlist de session: current, turbo_beta, legacy_open. "
            "Si vide, prompt interactif au demarrage (si terminal interactif)."
        ),
    )
    parser.add_argument(
        "--no-portfolio-prompt",
        action="store_true",
        help="Desactive la question de selection portefeuille au demarrage.",
    )
    parser.add_argument(
        "--env-file",
        default="",
        help=(
            "Fichier .env additionnel a superposer (ex: .env.live_capital). "
            "Permet de basculer de profil sans modifier .env."
        ),
    )
    args = parser.parse_args()
    if args.env_file:
        os.environ[ENV_FILE_ENVVAR] = str(args.env_file).strip()
        load_runtime_env()

    position_file = os.getenv("POSITION_FILE", POSITION_FILE_DEFAULT).strip() or POSITION_FILE_DEFAULT
    if args.confirm_buy:
        ticker = normalize_ticker(args.confirm_buy)
        positions = load_positions(position_file)
        state = normalize_position_state(positions.get(ticker, {}))
        portfolio_profile = infer_portfolio_profile_for_ticker(ticker, state)
        if float(state.get("entry_price_usd", 0.0) or 0.0) <= 0:
            px = get_reference_price_usd(ticker)
            if px is not None:
                state["entry_price_usd"] = px
        state["in_position"] = True
        state["pending_buy"] = False
        state["pending_sell"] = False
        state["portfolio_profile"] = portfolio_profile
        state["last_sell_alert_ts"] = 0.0
        state["entry_ts_utc"] = utc_now_iso_z()
        positions[ticker] = state
        save_positions(position_file, positions)
        try:
            journal_file = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
            if os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}:
                append_journal_event(
                    journal_file,
                    {
                        "event": "confirm_buy",
                        "ticker": ticker,
                        "source": "cli",
                        "portfolio_profile": portfolio_profile,
                        "size_usd": float(state.get("entry_notional_usd", 0.0) or 0.0),
                        "entry_price_usd": float(state.get("entry_price_usd", 0.0) or 0.0),
                    },
                )
        except Exception:
            pass
        print(f"[Positions] Achat confirme pour {ticker}.")
        return

    if args.confirm_sell:
        ticker = normalize_ticker(args.confirm_sell)
        positions = load_positions(position_file)
        # On suppose que tu as vendu : plus de position.
        state = normalize_position_state(positions.get(ticker, {}))
        portfolio_profile = infer_portfolio_profile_for_ticker(ticker, state)
        entry_notional = float(state.get("entry_notional_usd", 0.0) or 0.0)
        entry_price = float(state.get("entry_price_usd", 0.0) or 0.0)
        exit_price = get_reference_price_usd(ticker)
        pnl_usd: Optional[float] = None
        pnl_pct: Optional[float] = None
        if entry_notional > 0 and entry_price > 0 and exit_price is not None and exit_price > 0:
            pnl_usd, pnl_pct = compute_closed_pnl_usd_pct(entry_notional, entry_price, exit_price)
        state["in_position"] = False
        state["pending_buy"] = False
        state["pending_sell"] = False
        state["last_sell_alert_ts"] = 0.0
        state["entry_notional_usd"] = 0.0
        state["entry_price_usd"] = 0.0
        state["entry_confidence"] = 0
        positions[ticker] = state
        save_positions(position_file, positions)
        try:
            journal_file = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
            if os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}:
                append_journal_event(
                    journal_file,
                    {
                        "event": "confirm_sell",
                        "ticker": ticker,
                        "source": "cli",
                        "portfolio_profile": portfolio_profile,
                        "size_usd": entry_notional,
                        "exit_price_usd": float(exit_price or 0.0),
                    },
                )
                if pnl_usd is not None and pnl_pct is not None:
                    append_journal_event(
                        journal_file,
                        {
                            "event": "trade_closed",
                            "ticker": ticker,
                            "source": "cli",
                            "portfolio_profile": portfolio_profile,
                            "size_usd": entry_notional,
                            "entry_price_usd": entry_price,
                            "exit_price_usd": float(exit_price or 0.0),
                            "pnl_usd": float(pnl_usd),
                            "pnl_pct": float(pnl_pct),
                        },
                    )
        except Exception:
            pass
        if pnl_usd is not None and pnl_pct is not None:
            print(f"[Positions] Vente confirmée pour {ticker}. PnL estime: {pnl_usd:+.2f} USD ({pnl_pct:+.2f}%).")
        else:
            print(f"[Positions] Vente confirmée pour {ticker}.")
        return

    if args.budget_usd is not None:
        os.environ["TRADING_BUDGET_USD"] = str(args.budget_usd)
    if args.horizon_weeks is not None:
        os.environ["TRADING_HORIZON_DAYS"] = str(max(1, args.horizon_weeks * 7))
    elif args.horizon_days is not None:
        os.environ["TRADING_HORIZON_DAYS"] = str(max(1, args.horizon_days))

    if args.reset_positions:
        clear_positions_file(position_file)
        print("[Positions] positions.json vide (--reset-positions).")

    lock_path = acquire_bot_singleton_lock()
    if lock_path:
        print(f"[LOCK] Instance unique OK (pid={os.getpid()}, {lock_path}).")

    active_tickers = get_tickers_from_env()
    selected_profile = "current"
    selected_tickers = active_tickers
    profile_arg = normalize_ticker(args.portfolio_profile).lower()
    if profile_arg:
        if profile_arg in {"current", "env"}:
            selected_profile = "current"
        elif profile_arg in PORTFOLIO_PROFILES:
            selected_profile = profile_arg
            selected_tickers = PORTFOLIO_PROFILES[profile_arg]
        else:
            print(
                "[Portfolio] Profil inconnu via --portfolio-profile. "
                "Valeurs: current, turbo_beta, legacy_open."
            )
            return
    else:
        prompt_on_start = os.getenv("PORTFOLIO_PROMPT_ON_START", "1").strip().lower() in {"1", "true", "yes", "y"}
        if prompt_on_start and not args.no_portfolio_prompt and sys.stdin.isatty():
            selected_profile, selected_tickers = choose_portfolio_profile_on_start(active_tickers)

    _apply_session_ticker_override(selected_tickers)
    os.environ["ACTIVE_PORTFOLIO_PROFILE"] = normalize_portfolio_profile_key(selected_profile) or "current"

    scan_interval_raw = (os.getenv("SCAN_INTERVAL_MIN", "10") or "10").strip()
    scan_interval_min = effective_scan_interval_min()
    b_show = os.getenv("TRADING_BUDGET_USD", "1000")
    h_show = os.getenv("TRADING_HORIZON_DAYS", "7")
    use_clock = os.getenv("SCAN_ALIGN_CLOCK", "0").strip().lower() in {"1", "true", "yes", "y"}
    interval_note = (
        f"{scan_interval_raw} -> {scan_interval_min} min"
        if scan_interval_raw.lower() in {"auto", "optimal", "smart"}
        else f"{scan_interval_min} min"
    )
    align_msg = (
        "oui (creneaux :00, :15, :30, :45 heure NY)"
        if use_clock
        else (
            f"non — cycle complet puis pause jusqu'a {scan_interval_min} min "
            f"(FLASH actif entre les cycles)"
        )
    )
    print(f"Bot demarre. Intervalle scan: {interval_note}, aligne horloge NY: {align_msg}.")
    print(f"Session: budget reference {b_show} USD, horizon {h_show} jour(s).")
    print(
        f"[Portfolio] Profil actif: {selected_profile} | "
        f"tickers ({len(selected_tickers)}): {', '.join(selected_tickers)}"
    )
    funda_on = os.getenv("FUNDAMENTAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    funda_score_show = os.getenv("FUNDAMENTAL_MIN_SCORE", "45").strip()
    earnings_block_show = os.getenv("EARNINGS_BLOCK_DAYS", "1").strip()
    print(
        f"Filtre fondamental: {'active' if funda_on else 'desactive'} "
        f"(score min {funda_score_show}, blocage earnings J+{earnings_block_show})."
    )
    scan_prio_on = scan_prioritize_momentum_enabled()
    breakout_relax_on = breakout_early_relax_macd_enabled()
    parallel_on = scan_parallel_enabled()
    parallel_workers = scan_parallel_workers()
    momentum_src = scan_momentum_source()
    yf_iv_show = os.getenv("YF_INTERVAL", "15m").strip()
    print(
        f"Scan: intervalle cycle {scan_interval_min} min, bougies {yf_iv_show}, "
        f"parallele={'on' if parallel_on else 'off'}"
        f"{f' ({parallel_workers} workers)' if parallel_on else ''}, "
        f"momentum={momentum_src}."
    )
    buy_imm = buy_execute_immediate_enabled() and not parallel_on
    buy_mode_txt = (
        "immediat si slot libre | file fin de cycle si slots pleins (swap)"
        if buy_imm
        else "apres scan complet (batch)"
    )
    print(f"Achats: {buy_mode_txt}.")
    early_on = buy_early_entry_enabled()
    print(
        f"Breakout: priorise momentum={'on' if scan_prio_on else 'off'}, "
        f"MACD anticipe={'on' if breakout_relax_on else 'off'} "
        f"(lookback {breakout_lookback_bars()} barres, buffer {breakout_buffer_pct():.2f}%, "
        f"volume x{breakout_require_volume_ratio():.2f}, "
        f"bougie verte={'oui' if breakout_require_green_candle() else 'non'}, "
        f"RSI min {breakout_min_rsi():.0f})."
    )
    print(
        f"Entree anticipee: {'on' if early_on else 'off'} "
        f"(conf regles {rules_early_entry_min_conf()}+ si setup, max chase +{buy_max_chase_from_breakout_pct():.2f}% "
        f"vs range 15m, anti-chase re-entry {reentry_block_chase_pct():.1f}% "
        f"(cooldown {reentry_cooldown_hours():.0f}h)."
    )
    print(
        "Prix execution IBKR en priorite (REFERENCE_PRICE_IBKR_FIRST). "
        "Bougies/indicateurs: DATA_PROVIDER (.env). FLASH entre cycles peut utiliser Finnhub."
    )
    buy_confirm_on = os.getenv("BUY_CONFIRMATION_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    buy_rr_show = os.getenv("BUY_MIN_RR", "1.2").strip()
    buy_near_high_on_show = os.getenv("BUY_BLOCK_NEAR_3MO_HIGH", "1").strip().lower() in {"1", "true", "yes", "y"}
    buy_near_high_dist_show = os.getenv("BUY_MAX_DIST_TO_3MO_HIGH_PCT", "1.8").strip()
    print(
        f"Garde-fous achat: {'actifs' if buy_confirm_on else 'desactives'} "
        f"(MACD confirmation, anti-falling-knife, RR min {buy_rr_show}, "
        f"filtre proximite plus haut 3mo: {'on' if buy_near_high_on_show else 'off'} <= {buy_near_high_dist_show}%)."
    )
    mtf_on = os.getenv("MTF_FILTER_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    mtf_interval_show = os.getenv("MTF_INTERVAL", "1h").strip()
    mtf_min_show = os.getenv("MTF_MIN_BUY_SCORE", "0").strip()
    print(
        f"Filtre multi-timeframe: {'actif' if mtf_on else 'desactive'} "
        f"({mtf_interval_show}, score min achat {mtf_min_show})."
    )
    journal_on = os.getenv("JOURNAL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    journal_file_show = os.getenv("TRADE_JOURNAL_FILE", TRADE_JOURNAL_FILE_DEFAULT).strip() or TRADE_JOURNAL_FILE_DEFAULT
    print(f"Journal trading: {'actif' if journal_on else 'desactive'} ({journal_file_show}).")
    flash_on = os.getenv("FLASH_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    flash_check_show = os.getenv("FLASH_CHECK_SEC", "30").strip()
    flash_score_show = os.getenv("FLASH_MIN_SCORE", "72").strip()
    flash_source_show = os.getenv("FLASH_SOURCE", "candles").strip() or "candles"
    confirm_poll_show = os.getenv("TELEGRAM_CONFIRM_POLL_SEC", "3").strip()
    print(
        f"Flash hors cycle: {'active' if flash_on else 'desactive'} "
        f"(source {flash_source_show}, verification toutes les {flash_check_show}s entre 2 cycles, seuil score {flash_score_show}+)."
    )
    premarket_on = os.getenv("PREMARKET_PREP_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    premarket_win_show = os.getenv("PREMARKET_PREP_WINDOW_MIN", "120").strip()
    premarket_top_show = os.getenv("PREMARKET_PREP_MAX_TICKERS", "3").strip()
    daily_report_on = os.getenv("DAILY_REPORT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    objective_show = os.getenv("TRADING_OBJECTIVE_DAYS", "").strip() or os.getenv("TRADING_HORIZON_DAYS", "7").strip()
    print(
        f"Plan pre-ouverture: {'actif' if premarket_on else 'desactive'} "
        f"(fenetre {premarket_win_show} min avant open, top {premarket_top_show})."
    )
    print(
        f"Rapport quotidien: {'actif' if daily_report_on else 'desactive'} "
        f"(objectif {objective_show} jour(s), commandes /daily_report, /weekly, /monthly)."
    )
    print(
        f"Polling confirmations Telegram: toutes les {confirm_poll_show}s entre 2 cycles "
        f"(commandes: /status, /cockpit, /report, /daily_report, /weekly, /monthly, /plan, /report_pf, /report_pf_all, "
        f"/daily_pf, /weekly_pf, /monthly_pf, /fix_ticker, /edit_entry)."
    )
    heartbeat_enabled = os.getenv("CYCLE_HEARTBEAT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "y"}
    try:
        heartbeat_every = int(os.getenv("CYCLE_HEARTBEAT_EVERY_CYCLES", "1"))
    except ValueError:
        heartbeat_every = 1
    heartbeat_every = max(1, heartbeat_every)
    print(
        f"Heartbeat cycle Telegram: {'actif' if heartbeat_enabled else 'desactive'} "
        f"(tous les {heartbeat_every} cycle(s), marche US ouvert uniquement)."
    )
    intraday_flat_on = os.getenv("TRADING_INTRADAY_FLAT_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    if intraday_flat_on:
        try:
            flat_min_before = float(os.getenv("TRADING_INTRADAY_FLAT_MIN_BEFORE_CLOSE", "40"))
        except ValueError:
            flat_min_before = 40.0
        try:
            flat_min_profit = float(os.getenv("TRADING_INTRADAY_FLAT_MIN_PROFIT_PCT", "0.3"))
        except ValueError:
            flat_min_profit = 0.3
        skip_loss = os.getenv("TRADING_INTRADAY_FLAT_SKIP_IF_LOSS", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
        runner_min = intraday_runner_min_pnl_pct()
        runner_max = intraday_runner_max_overnight()
        if runner_max > 0:
            flat_rule = (
                f"flat gains (+{flat_min_profit:.1f}% a +{runner_min:.1f}%) ; "
                f"jusqu'a {runner_max} runner(s) >= {runner_min:.1f}% overnight"
            )
        else:
            flat_rule = (
                f"flat tous gains >= +{flat_min_profit:.1f}% (runners overnight desactives)"
            )
        print(
            f"Intraday option 3: {flat_rule} "
            f"dans les {flat_min_before:.0f} min avant cloture NY "
            f"(poll ~{intraday_flat_poll_sec():.1f}s, retry {intraday_flat_retry_sec():.0f}s, bid IBKR); "
            f"rattrapage scan marche ferme jusqu'a +{intraday_flat_catchup_max_min_after_close():.0f} min apres 16h NY; "
            f"{'pertes conservees' if skip_loss else 'pertes flat aussi'}."
        )
        block_buy_min = block_buy_min_before_close_minutes()
        if block_buy_min > 0:
            print(
                f"Blocage entrees fin de seance: actif ({block_buy_min:.0f} min avant cloture NY "
                f"— pas d'ACHETER ni switch auto)."
            )
    else:
        print("Intraday pur: desactive (positions peuvent rester overnight).")
    swap_on = os.getenv("TRADING_SWAP_ON_FULL_SLOTS", "1").strip().lower() in {"1", "true", "yes", "y"} or os.getenv(
        "TRADING_SWAP_ON_BUDGET", "1"
    ).strip().lower() in {"1", "true", "yes", "y"}
    if swap_on:
        swap_mode = "AUTO IBKR immediat" if swap_auto_execute_enabled() else "confirmation Telegram (/swap_oui)"
        print(
            f"Rotation switch: activee ({swap_mode}, conf +{swap_min_conf_edge()}, "
            f"score +{swap_min_rank_edge():.0f}, protege gain >={swap_protect_profit_pct():.1f}%, "
            f"max {swap_max_per_day()}/jour, cooldown {swap_cooldown_min():.0f} min)."
        )
    else:
        print("Rotation switch: desactivee.")
    stop_ov = os.getenv("TRADING_TICKER_STOP_ATR_MULT", "").strip() or "aucun"
    print(
        f"Stop entree: plancher max({min_stop_distance_pct() * 100:.1f}%, "
        f"{float(os.getenv('TRADING_MIN_STOP_ATR_MULT', '2.5')):.1f}x ATR), "
        f"plafond {max_stop_distance_pct() * 100:.1f}% — overrides: {stop_ov}."
    )
    if ibkr_auto_execute_enabled():
        ibkr_status = None
        try:
            from ibkr_execution import connect_ibkr_at_startup

            ibkr_status = connect_ibkr_at_startup()
        except Exception as exc:
            # Ne pas confondre UnicodeEncodeError print avec echec Gateway.
            print(f"[IBKR] Connexion impossible au demarrage: {exc}")
            require = os.getenv("IBKR_STARTUP_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "y"}
            if require:
                raise SystemExit(1) from exc
        if ibkr_status:
            print(f"[IBKR] {ibkr_status}")
            print(
                "[IBKR] IMPORTANT: dans IB Gateway > Configure > Settings > API > "
                "Master API client ID = valeur de IBKR_CLIENT_ID (paper: 8). "
                "Ferme les autres onglets Client (10, 30, ...) sinon ils peuvent vendre tes positions."
            )
            try:
                from ibkr_execution import guard_ibkr_foreign_exit_orders

                pos_file0 = (
                    os.getenv("POSITION_FILE", POSITION_FILE_DEFAULT).strip()
                    or POSITION_FILE_DEFAULT
                )
                pos0 = load_positions(pos_file0)
                open0 = [
                    normalize_ticker(tk)
                    for tk, raw in pos0.items()
                    if bool(normalize_position_state(raw).get("in_position"))
                ]
                if open0:
                    cancelled0 = guard_ibkr_foreign_exit_orders(open0)
                    if cancelled0:
                        print(
                            f"[IBKR] GARDE demarrage: {len(cancelled0)} vente(s) etrangere(s) annulee(s)."
                        )
            except Exception as guard_exc:
                print(f"[IBKR] GARDE demarrage ignoree: {guard_exc}")
            hybrid_px = _reference_price_ibkr_first_enabled()
            print(
                f"[IBKR] Prix reference hybride: {'IBKR puis Yahoo' if hybrid_px else 'Yahoo uniquement'} | "
                f"auto: 1 action entiere (ticket ajuste si besoin, limite = cash restant)."
            )
            tg_close = "on" if telegram_notify_ibkr_close_enabled() else "off"
            try:
                ibkr_poll_sec = float(os.getenv("IBKR_SYNC_CLOSE_POLL_SEC", "45"))
            except ValueError:
                ibkr_poll_sec = 45.0
            print(
                f"[IBKR] Alerte Telegram cloture TP/SL: {tg_close} "
                f"(sync inter-cycle ~{max(15.0, ibkr_poll_sec):.0f}s)."
            )
            ibkr_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            ibkr_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            if ibkr_token and ibkr_chat:
                try:
                    send_telegram_alert(
                        ibkr_token,
                        ibkr_chat,
                        f"Mode execution IBKR AUTO active.\n{ibkr_status}",
                    )
                except Exception as exc:
                    print(f"[IBKR] Notification Telegram demarrage: {exc}")
    else:
        print("[IBKR] Execution auto desactivee (IBKR_ENABLED=0 ou IBKR_AUTO_EXECUTE=0).")

    # Option debug: envoyer un message unique de test au demarrage
    # pour valider que Telegram est bien configure (sans attendre un signal ACHETER/VENDRE).
    telegram_test = os.getenv("TELEGRAM_TEST", "").strip().lower() in {"1", "true", "yes", "y"}
    if telegram_test:
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if telegram_token and telegram_chat_id:
            try:
                send_telegram_alert(
                    telegram_token,
                    telegram_chat_id,
                    "Test Telegram: le bot est demarre et pret a analyser.",
                )
                print("[Telegram] Message de test envoye.")
            except Exception as exc:
                print(f"[Telegram] Erreur envoi message de test: {exc}")
        else:
            print("[Telegram] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID manquant, test impossible.")

    try:
        from cockpit_web import cockpit_web_enabled, start_cockpit_web_server

        if cockpit_web_enabled():
            from cockpit_web import register_cockpit_price_enricher

            register_cockpit_price_enricher(
                lambda data: enrich_cockpit_data_live_prices(data, skip_ibkr=True)
            )
            start_cockpit_web_server()
            try:
                cockpit_budget = float(os.getenv("TRADING_BUDGET_USD", "1000"))
            except ValueError:
                cockpit_budget = 1000.0
            try:
                cockpit_slots = int(os.getenv("TRADING_MAX_OPEN_POSITIONS", "6"))
            except ValueError:
                cockpit_slots = 6
            publish_cockpit_live_state(
                load_positions(position_file),
                budget_usd=cockpit_budget,
                max_open_positions=cockpit_slots,
                journal_file=journal_file_show,
                cycle_idx=0,
            )
            refresh_cockpit_live_prices_overlay(skip_ibkr=False)
    except ImportError:
        pass
    except OSError as exc:
        # WinError 10013 / port cockpit bloque: ne pas tuer le bot trading.
        print(f"[Cockpit Web] Demarrage ignore (OSError): {exc}")
    except Exception as exc:
        print(f"[Cockpit Web] Demarrage ignore: {exc}")

    interval_sec = float(scan_interval_min * 60)
    align_clock = os.getenv("SCAN_ALIGN_CLOCK", "0").strip().lower() in {"1", "true", "yes", "y"}
    cycle_idx = 0

    while True:
        cycle_start = time.perf_counter()
        stats = run_cycle()
        elapsed = time.perf_counter() - cycle_start
        cycle_idx += 1
        try:
            cockpit_budget = float(os.getenv("TRADING_BUDGET_USD", "1000"))
        except ValueError:
            cockpit_budget = 1000.0
        try:
            cockpit_slots = int(os.getenv("TRADING_MAX_OPEN_POSITIONS", "6"))
        except ValueError:
            cockpit_slots = 6
        publish_cockpit_live_state(
            load_positions(position_file),
            budget_usd=cockpit_budget,
            max_open_positions=cockpit_slots,
            journal_file=journal_file_show,
            cycle_idx=cycle_idx,
        )
        refresh_cockpit_live_prices_overlay(skip_ibkr=False)
        if heartbeat_enabled and (cycle_idx % heartbeat_every == 0) and is_us_market_open():
            hb_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            hb_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            if hb_token and hb_chat_id:
                now_ny_txt = datetime.now(MARKET_TZ).strftime("%H:%M:%S %Z")
                try:
                    hb_budget = float(os.getenv("TRADING_BUDGET_USD", "1000"))
                except ValueError:
                    hb_budget = 1000.0
                try:
                    hb_slots = int(os.getenv("TRADING_MAX_OPEN_POSITIONS", "2"))
                except ValueError:
                    hb_slots = 2
                hb_positions = load_positions(position_file)
                snapshot = build_portfolio_snapshot(
                    hb_positions,
                    budget_usd=hb_budget,
                    max_open_positions=hb_slots,
                    journal_file=journal_file_show,
                )
                hb_msg = (
                    f"Heartbeat cycle #{cycle_idx} ({now_ny_txt})\n"
                    f"Scans tickers: {stats.get('scanned', 0)}\n"
                    f"Duree cycle: {elapsed:.1f}s\n"
                    f"Signaux envoyes: {stats.get('signals_sent', 0)} "
                    f"(BUY {stats.get('buy_sent', 0)} / SELL {stats.get('sell_sent', 0)})\n\n"
                    f"{snapshot}"
                )
                try:
                    send_telegram_alert(hb_token, hb_chat_id, hb_msg)
                except Exception as exc:
                    print(f"[Heartbeat] Echec envoi Telegram: {exc}")

        if align_clock:
            now_ny = datetime.now(MARKET_TZ)
            wait_sec = seconds_until_next_time_slot(now_ny, scan_interval_min)
            next_run = now_ny + timedelta(seconds=wait_sec)
            print(
                f"[Cycle] Scan termine en {elapsed:.1f}s. "
                f"Alignement horloge NY: pause {wait_sec:.1f}s -> prochain cycle vers "
                f"{next_run.strftime('%Y-%m-%d %H:%M:%S %Z')} (creneaux toutes les {scan_interval_min} min)."
            )
            sleep_with_flash(wait_sec)
        else:
            wait_sec = max(0.0, interval_sec - elapsed)
            if wait_sec > 0:
                print(
                    f"[Cycle] Scan termine en {elapsed:.1f}s. "
                    f"Pause {wait_sec:.1f}s (cible {scan_interval_min} min depuis le debut du cycle)."
                )
                sleep_with_flash(wait_sec)
            else:
                print(
                    f"[Cycle] Scan termine en {elapsed:.1f}s (>={scan_interval_min} min). "
                    "Relance immediate du cycle suivant."
                )


if __name__ == "__main__":
    main()
