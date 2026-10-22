# -*- coding: utf-8 -*-
"""
Suivi et VERDICT de la strategie "breakout semaine", selon docs/protocole-verdict-bot.md
(fige le 2026-09-30). Ne rien changer ici sans dater une modification dans le protocole.

1) Trades clos du journal paper dates a partir du 30/09/2026 (protocole section 3: "apres le
   29/09"), corrections du journal (journal_correction) appliquees, dedoublonnes comme le bot.
   Aucun trade exclu en silence (section 7): un trade "void" est compte et signale; un trade sans
   PnL utilisable est signale "a resoudre a la main"; un prix de sortie qui ne vient pas d'un fill
   IBKR est signale "prix de sortie estime". Rendement d'un trade = (PnL $ - commissions estimees)
   / montant investi, commissions = 2 x max(0.35$, 0.0035$/action). Score = somme des rendements.
2) Backtest de la MEME periode, memes regles, y compris le blocage du bot avant les resultats
   (scripts/research_earnings_dates.json). Controle d'execution dans les deux sens (section 6),
   non decisif: trades paper absents du backtest et trades backtest absents du paper.
3) 1000 bots aleatoires (section 4): memes tickers, meme periode (du 30/09/2026), memes sorties,
   memes couts, sans filtre; chaque bot tire N entrees sur toute la periode, N = trades clos du
   paper. Rang = % des bots dont le score est inferieur a celui du paper (idem backtest, N').
4) Decision du protocole (section 5) + journal dans scripts/track_log.jsonl.

Usage:
  .\\.venv\\Scripts\\python.exe scripts\\track_week_strategy.py
  .\\.venv\\Scripts\\python.exe scripts\\track_week_strategy.py --hashes
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import dotenv_values

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import backtest_swing as B  # noqa: E402
import research_engine as E  # noqa: E402

NY = "America/New_York"
LOG = os.path.join(HERE, "track_log.jsonl")
EARN_FILE = os.path.join(HERE, "research_earnings_dates.json")
# Protocole section 3 (trades dates apres le 29/09/2026) et section 4 (bots du 30/09/2026)
PROTOCOL_START = "2026-09-30"
# Sources d'un prix de sortie reel (main.py, synchro IBKR); "reference_price" = cours estime
FILL_SOURCES = {"ibkr_fill", "corrected_ibkr_statement"}
HASHED = ["main.py", "ibkr_execution.py", "broker_hooks.py", ".env.ibkr_paper",
          "scripts/research_engine.py", "scripts/backtest_swing.py", "scripts/track_week_strategy.py",
          "docs/protocole-verdict-bot.md"]


def hashes():
    out = {}
    for rel in HASHED:
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            with open(p, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def _flat(df):
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(1, axis=1)
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])


def _ts_ny(x):
    """Horodatage du journal (UTC) -> heure de New York; None si illisible."""
    try:
        t = pd.Timestamp(x)
    except (TypeError, ValueError):
        return None
    if pd.isna(t):
        return None
    return (t.tz_localize("UTC") if t.tzinfo is None else t).tz_convert(NY)


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def load_journal(journal):
    out = []
    if os.path.exists(journal):
        with open(journal, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    out.append(ev)
    return out


def apply_corrections(closed, events, since, notes):
    """Evenements journal_correction (ecrits a la main): le trade vise est le trade_closed du meme
    ticker dont ts_utc == trade_ts_utc; chaque champ de "corrected" prend la valeur new_<champ>
    (pnl_pct n'a pas de new_: inutilise ici). Ses doublons (meme cle que le bot) sont corriges aussi,
    pour que le dedoublonnage reste coherent. Correction sans trade -> signalee."""
    import main as M
    for c in events:
        if c.get("event") != "journal_correction":
            continue
        tk, t0 = str(c.get("ticker", "")).upper(), _ts_ny(c.get("trade_ts_utc"))
        keys = {M.trade_closed_dedupe_key(e) for e in closed
                if t0 is not None and str(e.get("ticker", "")).upper() == tk and _ts_ny(e.get("ts_utc")) == t0}
        if not keys:
            if t0 is None or t0.date() >= since:
                notes.append("correction SANS trade_closed correspondant: %s %s (%s)"
                             % (tk, c.get("trade_ts_utc"), c.get("reason", "")))
            continue
        fields = [f for f in (c.get("corrected") or [k[4:] for k in c if k.startswith("new_")])
                  if "new_" + f in c]
        for e in closed:
            if M.trade_closed_dedupe_key(e) in keys:
                for f in fields:
                    e[f] = c["new_" + f]
                e.setdefault("_corr_fields", set()).update(fields)
                e.setdefault("_corr", []).append("%s (%s)" % (", ".join(
                    "%s %s -> %s" % (f, c.get("old_" + f, "?"), c["new_" + f]) for f in fields), c.get("reason", "")))


def exit_from_fill(ev, ts, sells):
    """True si le prix de sortie vient d'un fill IBKR. Synchro IBKR (main.py, sync_ticker_closed_at_ibkr):
    champ exit_price_source, "reference_price" quand l'execution est introuvable (cours estime).
    Vente du bot (ibkr_auto, sans ce champ): prix = fill de son ordre, journalise dans la foulee
    (ibkr_order_filled SELL au meme prix); sans fill, record_confirmed_sell prend le cours.
    Un prix corrige a la main (journal_correction) est signale a part."""
    if "exit_price_usd" in ev.get("_corr_fields", ()):
        return True
    src = str(ev.get("exit_price_source") or "").strip()
    if src:
        return src in FILL_SOURCES
    px = _num(ev.get("exit_price_usd"))
    return px is not None and any(abs((t - ts).total_seconds()) <= 120 and round(p, 4) == round(px, 4)
                                  for t, p in sells)


def paper_trades(journal, since):
    """Trades clos a partir de `since` (date NY) + heure d'achat (dernier fill BUY avant la sortie).
    Retourne (trades comptes, anomalies a lire, fills BUY par ticker).
    Protocole section 7, aucun trade exclu en silence:
    - void: COMPTE comme les autres. Marquer void un trade_closed, c'est l'exclure a la main; ca
      demande une ligne datee en section 8, pas un drapeau dans le journal. Signale.
    - sans PnL utilisable (montant, prix d'entree ou PnL absent): pas de rendement calculable,
      NON compte et signale, a resoudre a la main avant tout verdict.
    - dedoublonnage du bot (main.trade_closed_dedupe_key), le trade non void passant en premier.
    - prix de sortie sans fill IBKR: compte, signale "prix de sortie estime"."""
    import main as M
    events = load_journal(journal)
    buys, sells = {}, {}
    for ev in events:
        if ev.get("event") != "ibkr_order_filled":
            continue
        tk, t = str(ev.get("ticker", "")).upper(), _ts_ny(ev.get("ts_utc"))
        side = str(ev.get("side", "")).upper()
        if t is None:
            continue
        if side == "BUY":
            buys.setdefault(tk, []).append(t)
        elif side == "SELL" and (_num(ev.get("fill_price_usd")) or 0) > 0:
            sells.setdefault(tk, []).append((t, float(ev["fill_price_usd"])))
    notes = []
    closed = [dict(e) for e in events if e.get("event") == "trade_closed"]
    apply_corrections(closed, events, since, notes)
    closed = M.dedupe_trade_closed_events([e for e in closed if not e.get("void")]
                                          + [e for e in closed if e.get("void")])
    out = []
    for ev in closed:
        tk, ts = str(ev.get("ticker", "")).upper(), _ts_ny(ev.get("ts_utc"))
        if ts is None:
            notes.append("NON COMPTE, a resoudre a la main: trade_closed %s sans date lisible (%r)"
                         % (tk, ev.get("ts_utc")))
            continue
        if ts.date() < since:
            continue
        lbl = "%s sortie %s" % (tk, ts.strftime("%Y-%m-%d %H:%M"))
        void = bool(ev.get("void"))
        size, px, pnl = _num(ev.get("size_usd")), _num(ev.get("entry_price_usd")), _num(ev.get("pnl_usd"))
        if not (size and size > 0 and px and px > 0 and pnl is not None):
            notes.append("NON COMPTE, a resoudre a la main: %s%s sans PnL utilisable (montant %s, entree %s, "
                         "PnL %s)" % (lbl, " VOID" if void else "", ev.get("size_usd"), ev.get("entry_price_usd"),
                                      ev.get("pnl_usd")))
            continue
        qty = size / px
        fees = 2 * max(0.35, 0.0035 * qty)
        prior = [b for b in buys.get(tk, []) if b <= ts]
        est = not exit_from_fill(ev, ts, sells.get(tk, []))
        if void:
            notes.append("VOID COMPTE dans le score (section 7: pas d'exclusion sans ligne datee en section 8): "
                         "%s | %s" % (lbl, str(ev.get("void_reason", ""))[:160]))
        if est:
            notes.append("prix de sortie estime (pas de fill IBKR, compte quand meme): %s | sortie %.2f, source %s"
                         % (lbl, _num(ev.get("exit_price_usd")) or 0.0,
                            ev.get("exit_price_source") or ev.get("source") or "?"))
        for c in ev.get("_corr", []):
            notes.append("correction du journal appliquee: %s | %s" % (lbl, c))
        out.append({"ticker": tk, "exit_ts": ts, "entry_ts": max(prior) if prior else None,
                    "pnl_usd": pnl, "size": size, "ret": (pnl - fees) / size,
                    "reason": ev.get("exit_reason", ""), "void": void, "estimated": est,
                    "corrected": bool(ev.get("_corr"))})
    return sorted(out, key=lambda t: t["exit_ts"]), notes, buys


def same_entry(a_ts, b_ts):
    """Tolerance du controle paper / backtest, dans les deux sens: entree a +-1 jour."""
    return abs((pd.Timestamp(a_ts).normalize() - pd.Timestamp(b_ts).normalize()).days) <= 1


def missing_in_paper(bt, buys):
    """Trades du backtest sans achat paper (fill BUY) du meme ticker a +-1 jour. Les fills BUY
    couvrent aussi une position paper encore ouverte (trade backtest "END")."""
    return [b for b in bt if not any(same_entry(b["entry_ts"], e) for e in buys.get(b["ticker"], []))]


def load_earnings(path, tickers):
    """Dates de resultats (jour NY) par ticker: historique + dates prevues (fichier du 29/09/2026)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    out = {}
    for tk in tickers:
        ds = {t.date() for t in (_ts_ny(x.get("ts") if isinstance(x, dict) else x) for x in raw.get(tk) or [])
              if t is not None}
        out[tk] = sorted(ds)
    return out


def earnings_filter(sig, earn, block_days):
    """Regle du bot (main.py, filtre ACHETER): entree refusee si 0 <= (resultats - jour) <= block_days,
    donc seulement AVANT la publication (backtest_swing bloque +-2 j: on ne lui passe pas les dates).
    Jour = jour de l'entree: la barre i est lue a sa cloture, achat a l'ouverture de i+1; celle de
    15h30 est relue a la seance suivante. Limite: le bot calcule ce delai avec un cache fondamental
    de 24 h, il peut donc retarder d'un jour; le backtest suit la regle exacte."""
    if not len(sig):
        return sig
    keep = [not any(0 <= (e - pd.Timestamp(ts).date()).days <= block_days for e in earn.get(tk, ()))
            for ts, tk in zip(sig["ts"], sig["ticker"])]
    return sig[np.array(keep, dtype=bool)].reset_index(drop=True)


def earnings_coverage(earn, end_date):
    """Tickers dont le fichier de resultats s'arrete avant la fin de la periode evaluee."""
    return ["%s: aucune date de resultats apres le %s (derniere: %s) dans research_earnings_dates.json, "
            "blocage resultats non couvert" % (tk, end_date, ds[-1] if ds else "aucune")
            for tk, ds in earn.items() if not ds or ds[-1] <= end_date]


def random_trade_net(o, h, l, c, dayidx, lod, j, stop_pct, tp_pct, hold, only_if_profit=False):
    """Rendement NET d'un trade entre a l'ouverture de la barre j, sorties de la strategie
    (stop, objectif, `hold` seances), couts du moteur (0.35$ min/ordre, 0.0035$/action, 3 bps).
    only_if_profit: la sortie duree max ne se fait que si la cloture est au-dessus de l'entree.
    None si le trade n'est pas termine a la derniere barre disponible."""
    hs = 1.5e-4
    entry = o[j] * (1 + hs)
    if not entry > 0:
        return None
    slp, tpp, d0 = entry * (1 - stop_pct / 100), entry * (1 + tp_pct / 100), dayidx[j]
    exit_px = None
    for k in range(j, len(o)):
        if l[k] <= slp:
            exit_px = min(slp, o[k]); break
        if h[k] >= tpp:
            exit_px = max(tpp, o[k]); break
        if lod[k] and dayidx[k] - d0 >= hold and not (only_if_profit and c[k] <= entry):
            exit_px = c[k]; break
    if exit_px is None:
        return None
    qty = max(1, int(1000.0 // entry))
    fees = 2 * max(0.35, 0.0035 * qty)
    return (exit_px * (1 - hs) * qty - entry * qty - fees) / (entry * qty + fees / 2)


def decision(n, rank):
    if n < 30:
        return "AUCUN VERDICT avant 30 trades (%d/30) — protocole section 5" % n
    if n < 60:
        return ("ARRET: ne bat pas le hasard a 30 trades" if rank < 50
                else "ON CONTINUE jusqu'a 60 trades (%d/60)" % n)
    if rank >= 95:
        return "AVANTAGE DEMONTRE (rang >= 95%% a %d trades)" % n
    if rank >= 50:
        return "NON CONCLUANT (accepte d'avance): pas de passage en reel sur ce test"
    return "ARRET: ne bat pas le hasard a 60 trades"


def main():
    env = {**dotenv_values(os.path.join(ROOT, ".env")), **dotenv_values(os.path.join(ROOT, ".env.ibkr_paper"))}
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=PROTOCOL_START,
                    help="date de depart (defaut: celle du protocole, %s)" % PROTOCOL_START)
    ap.add_argument("--placebo", type=int, default=1000)
    ap.add_argument("--hashes", action="store_true")
    ap.add_argument("--no-log", action="store_true", help="essai technique: ne pas ecrire dans track_log.jsonl")
    args = ap.parse_args()
    if args.hashes:
        for k, v in hashes().items():
            print("%-34s %s" % (k, v))
        return
    since = pd.Timestamp(args.since).date()
    tickers = [t.strip().upper() for t in (env.get("TICKERS_OVERRIDE") or "").split(",") if t.strip()]
    stop = float(env.get("BUY_FIXED_STOP_PCT") or 10)
    tp = stop * float(env.get("BUY_RR_REWARD_MULT") or 2.5)
    hold = int(float(env.get("TRADING_MAX_HOLD_SESSIONS") or 5))
    vix_thr = float(env.get("RISK_OFF_VIX_THRESHOLD") or 22)
    paper, notes, buys = paper_trades(
        os.path.join(ROOT, env.get("TRADE_JOURNAL_FILE") or "trades_journal.jsonl"), since)
    n = len(paper)
    score = float(sum(t["ret"] for t in paper))

    data = {"h1": {}, "d1": {}, "earnings": {}}
    now = pd.Timestamp.now(tz=NY)
    for tk in tickers:
        h = _flat(yf.download(tk, period="730d", interval="1h", progress=False, auto_adjust=False))
        h.index = h.index.tz_localize(NY) if h.index.tz is None else h.index.tz_convert(NY)
        if len(h) and min(h.index[-1] + pd.Timedelta(hours=1),
                          h.index[-1].replace(hour=16, minute=0)) > now:
            h = h.iloc[:-1]   # barre en formation
        d = _flat(yf.download(tk, period="5y", interval="1d", progress=False, auto_adjust=False))
        d.index = d.index.tz_localize(None) if d.index.tz is not None else d.index
        data["h1"][tk], data["d1"][tk] = h, d
    vix = _flat(yf.download("^VIX", period="5y", interval="1d", progress=False, auto_adjust=False))
    vix.index = vix.index.tz_localize(None) if vix.index.tz is not None else vix.index
    data["vix"] = vix
    B.TICKERS, B.VIX_THRESHOLD = tickers, vix_thr
    sig = B.build_signals(data)   # data["earnings"] vide: le blocage +-2 j de backtest_swing reste inerte
    sig = sig[sig["breakout"]].reset_index(drop=True)
    # Blocage avant les resultats, regle du bot (protocole section 2: la regle d'entree en entier)
    earn = load_earnings(EARN_FILE, tickers)
    try:
        eb_days = int(env.get("EARNINGS_BLOCK_DAYS") or "1")
    except ValueError:
        eb_days = 1
    fund_on = str(env.get("FUNDAMENTAL_ENABLED") or "1").strip().lower() in {"1", "true", "yes", "y"}
    sig_all = sig
    if fund_on:
        sig = earnings_filter(sig, earn, eb_days)
    sig["kind"], sig["prio"] = "open", 0.0
    bars = E.Bars(data["h1"])
    only_profit = str(env.get("TRADING_MAX_HOLD_ONLY_IF_PROFIT") or "0").strip().lower() in {
        "1", "true", "yes", "y",
    }
    ex = E.Exit(
        "week", sl_pct=stop, tp_pct=tp, max_hold_tdays=hold, max_hold_only_if_profit=only_profit,
    )
    win = (pd.Timestamp(since).tz_localize(NY), bars.all_ts[-1])
    kw = dict(win=win, line_usd=1000.0, max_open=1, budget=1000.0)
    bt = E.simulate(bars, sig, ex, **kw)["trades"]

    end_date = win[1].date()
    print("=" * 104)
    print("VERDICT breakout semaine (protocole du 30/09/2026) — depuis %s | %s" % (since, ",".join(tickers)))
    print("=" * 104)
    if str(since) != PROTOCOL_START:
        print("ATTENTION: --since %s differe de la date du protocole (%s): essai hors protocole"
              % (since, PROTOCOL_START))
    print("Paper   : %3d trades clos | score %+.2f%% (somme des rendements nets) | %+.2f $ brut"
          % (n, 100 * score, sum(t["pnl_usd"] for t in paper)))
    print("Backtest: %3d trades meme periode | score %+.2f%%  (controle d'execution)"
          % (len(bt), 100 * sum(t["net"] / t["cost"] for t in bt)))
    if fund_on:
        n_win = lambda s: int(((s["ts"] >= win[0]) & (s["ts"] <= win[1])).sum()) if len(s) else 0  # noqa: E731
        in_win = ["%s %s" % (tk, e.strftime("%m-%d")) for tk in tickers for e in earn[tk]
                  if since <= e <= end_date + dt.timedelta(days=eb_days)]
        print("          blocage resultats (regle du bot: de 0 a %d j avant): %d signal(s) breakout retire(s) "
              "sur %d dans la periode | resultats: %s"
              % (eb_days, n_win(sig_all) - n_win(sig), n_win(sig_all), ", ".join(in_win) or "aucun"))
        for w in earnings_coverage(earn, end_date):
            print("  ATTENTION %s" % w)
    matched = 0
    for t in paper:
        hit = [b for b in bt if b["ticker"] == t["ticker"] and t["entry_ts"] is not None
               and same_entry(b["entry_ts"], t["entry_ts"])]
        matched += bool(hit)
        flags = "".join(s for s, ok in ((" [VOID]", t["void"]), (" [prix de sortie estime]", t["estimated"]),
                                        (" [corrige]", t["corrected"])) if ok)
        print("  %-5s entree %s  sortie %s  %+6.2f%%  %-18s %s%s"
              % (t["ticker"], t["entry_ts"].strftime("%m-%d %H:%M") if t["entry_ts"] is not None else "?",
                 t["exit_ts"].strftime("%m-%d %H:%M"), 100 * t["ret"], t["reason"],
                 "= backtest" if hit else "ABSENT DU BACKTEST (verifier l'execution)", flags))
    if n:
        print("Trades paper retrouves dans le backtest: %d/%d" % (matched, n))
    # Sens inverse (section 6): trade du backtest sans entree paper, meme tolerance
    missing = missing_in_paper(bt, buys)
    for b in missing:
        print("  %-5s entree %s  sortie %s  %+6.2f%%  %-18s ABSENT DU PAPER (verifier l'execution)"
              % (b["ticker"], pd.Timestamp(b["entry_ts"]).strftime("%m-%d %H:%M"),
                 pd.Timestamp(b["exit_ts"]).strftime("%m-%d %H:%M"), 100 * b["net"] / b["cost"],
                 b["reason"] + (" (en cours)" if b["reason"] == "END" else "")))
    if bt:
        print("Trades backtest retrouves dans le paper: %d/%d" % (len(bt) - len(missing), len(bt)))
    if notes:
        print("\nANOMALIES DU JOURNAL (a lire avant tout verdict):")
        for s in notes:
            print("  - %s" % s)

    # Bots aleatoires (protocole section 4, amende le 30/09): rendement d'un trade qui entrerait a
    # l'OUVERTURE de chaque barre de seance de la periode, memes sorties, memes couts, SANS aucun
    # filtre (ni VIX, ni tendance, ni breakout): on teste toute la regle d'entree. Chaque bot tire
    # exactement N de ces entrees sur toute la periode; le score etant une somme de rendements
    # par trade, un chevauchement de positions ne change rien.
    pool = []
    for tk in tickers:
        h = data["h1"][tk]
        o, hi_, lo_, c = (h[k].values.astype(float) for k in ("Open", "High", "Low", "Close"))
        dayidx, _ = pd.factorize(h.index.normalize())
        lod = np.r_[dayidx[1:] != dayidx[:-1], True]
        for j in np.where(h.index >= win[0])[0]:
            r = random_trade_net(o, hi_, lo_, c, dayidx, lod, int(j), stop, tp, hold, only_profit)
            if r is not None:
                pool.append(r)
    pool = np.array(pool)
    rng = np.random.default_rng(20260930)

    def rank_of(score_x, n_x):
        if n_x <= 0 or len(pool) < n_x:
            return None, None
        sc = np.array([pool[rng.choice(len(pool), n_x, replace=False)].sum() for _ in range(args.placebo)])
        return 100.0 * float((sc < score_x).mean()), sc

    rank_p, sc_p = rank_of(score, n)
    bt_score = float(sum(t["net"] / t["cost"] for t in bt))
    rank_b, _ = rank_of(bt_score, len(bt))
    if rank_p is not None:
        print("\n%d bots aleatoires x %d entrees (parmi %d entrees possibles sur la periode): "
              "score median %+.2f%%, p5 %+.2f%%, p95 %+.2f%%"
              % (args.placebo, n, len(pool), 100 * np.median(sc_p), 100 * np.percentile(sc_p, 5),
                 100 * np.percentile(sc_p, 95)))
        print("RANG DU PAPER   : %5.1f%%" % rank_p)
    if rank_b is not None:
        print("RANG DU BACKTEST: %5.1f%%  (%d trades, meme moteur que les bots)" % (rank_b, len(bt)))
    ranks = [r for r in (rank_p, rank_b) if r is not None]
    rank = min(ranks) if ranks else None
    if rank is not None:
        print("Rang retenu (le plus bas des deux, protocole section 5): %.1f%%" % rank)
    verdict = decision(n, rank if rank is not None else 0.0)
    n_out = sum(s.startswith("NON COMPTE") for s in notes)
    if n_out:
        print("\nATTENTION: %d trade(s) NON COMPTE(S), a resoudre a la main avant tout verdict (voir ANOMALIES)"
              % n_out)
    print("\nDECISION: %s" % verdict)
    if args.no_log:
        return
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": dt.datetime.now().isoformat(timespec="seconds"), "since": str(since),
                            "n_trades": n, "score_pct": round(100 * score, 3),
                            "rank_pct": None if rank is None else round(rank, 1), "decision": verdict,
                            "anomalies": notes, "hashes": hashes()}) + "\n")


if __name__ == "__main__":
    main()
