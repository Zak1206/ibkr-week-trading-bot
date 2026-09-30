"""Analyse TP ouverts vs strategie intraday."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENV_FILE_ENVVAR", ".env")
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.ibkr_paper", override=True)

import main as m

OPEN = [
    ("SMCI", 49.12, 51.58, 46.66),
    ("IONQ", 71.31, 74.09, 67.39),
    ("RGTI", 25.78, 27.07, 23.73),
    ("QBTS", 30.4, 31.92, 28.37),
    ("XOM", 149.67, 156.66, 143.23),
]
cap = float(os.getenv("BUY_MAX_TP_PCT", "0.05")) * 100
flat_min = float(os.getenv("TRADING_INTRADAY_FLAT_MIN_PROFIT_PCT", "0.3"))
runner = float(os.getenv("TRADING_RUNNER_MIN_PNL_PCT", "3.0"))

print(f"Strategie: cap TP +{cap:.1f}% | flat {flat_min}-{runner}% | runner >= {runner}%")
print(
    f"{'Tk':5} {'Entree':>8} {'TP':>8} {'TP%':>6} {'SL%':>6} {'RR':>5} "
    f"{'Px':>8} {'Reste%':>7} {'RangeJ%':>7} {'ATR%':>6}  Verdict"
)
print("-" * 98)

for tk, entry, tp, sl in OPEN:
    px = m.get_reference_price_usd(tk) or entry
    tp_pct = (tp / entry - 1) * 100
    sl_pct = (entry - sl) / entry * 100
    rr = (tp - entry) / (entry - sl) if entry > sl else 0
    rest = (tp / px - 1) * 100 if px > 0 else 0
    try:
        raw = m.fetch_price_data(tk, period="5d", interval="15m")
        raw = m.normalize_ohlc_columns(raw, tk)
        ind = m.add_indicators(raw)
        last = ind.iloc[-1]
        atr_pct = float(last["ATR_14"]) / float(last["Close"]) * 100
        session = raw.tail(26)
        rng_pct = (session["High"].max() - session["Low"].min()) / session["Low"].min() * 100
    except Exception:
        atr_pct = rng_pct = 0.0

    if tp_pct > cap + 0.3:
        verdict = "TROP HAUT vs cap 5%"
    elif tp_pct < flat_min:
        verdict = "trop serre (< flat min)"
    elif tp_pct <= cap + 0.05 and rng_pct >= tp_pct * 0.7:
        verdict = "OK intraday"
    elif tp_pct <= cap + 0.05:
        verdict = "OK cap, range jour limite"
    elif rest > 7:
        verdict = "encore loin"
    else:
        verdict = "OK"

    print(
        f"{tk:5} {entry:8.2f} {tp:8.2f} {tp_pct:5.2f}% {sl_pct:5.2f}% {rr:5.2f} "
        f"{px:8.2f} {rest:6.2f}% {rng_pct:6.2f}% {atr_pct:5.2f}%  {verdict}"
    )
