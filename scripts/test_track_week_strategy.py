# -*- coding: utf-8 -*-
"""
Tests du suivi du verdict (scripts/track_week_strategy.py) sur de petits journaux temporaires.
Lancement:  .\\.venv\\Scripts\\python.exe scripts\\test_track_week_strategy.py
Couvre: trades void (comptes et signales, non comptes si sans PnL), corrections du journal
(journal_correction), controle backtest -> paper, prix de sortie estime, blocage avant les
resultats (regle du bot), date de depart du protocole. Aucune connexion IBKR ni reseau.
"""
import datetime as dt
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

from test_execution_safety import check  # noqa: E402

import track_week_strategy as TR  # noqa: E402

NY = TR.NY
SINCE = dt.date(2026, 9, 30)


def closed(tk, ts, entry, exit_, size, **kw):
    """trade_closed comme l'ecrit main.record_confirmed_sell (synchro IBKR, fill trouve)."""
    ev = {"event": "trade_closed", "ticker": tk, "source": "ibkr_sync", "portfolio_profile": "current",
          "size_usd": size, "entry_price_usd": entry, "exit_price_usd": exit_,
          "pnl_usd": round((exit_ - entry) * size / entry, 2), "pnl_pct": (exit_ / entry - 1) * 100,
          "entry_source": "ibkr_auto", "exit_reason": "ibkr_sl_tp", "exit_price_source": "ibkr_fill",
          "ts_utc": ts}
    for k, v in kw.items():
        if v is None:
            ev.pop(k, None)
        else:
            ev[k] = v
    return ev


def fill(tk, side, ts, px):
    return {"event": "ibkr_order_filled", "ticker": tk, "side": side, "fill_price_usd": px, "ts_utc": ts}


def run(events, since=SINCE):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    try:
        return TR.paper_trades(path, since)
    finally:
        os.remove(path)


def count(notes, txt):
    return sum(txt in s for s in notes)


def test_void():
    print("\n[1] trades void: comptes et signales, jamais retires en silence")
    ev = [
        fill("PLTR", "BUY", "2026-10-01T14:00:00Z", 100.0),
        closed("PLTR", "2026-10-03T15:00:00Z", 100.0, 110.0, 1000.0),
        # doublon void du trade PLTR (double synchro): dedoublonnage du bot, garde le non void
        closed("PLTR", "2026-10-03T15:05:00Z", 100.0, 110.0, 1000.0, void=True, void_reason="doublon"),
        closed("SOFI", "2026-10-06T15:00:00Z", 20.0, 19.0, 1000.0, void=True,
               void_reason="faux positif: aucune vente reelle"),
        closed("COIN", "2026-10-07T15:00:00Z", 150.0, 151.0, 0.0, pnl_usd=None, void=True,
               void_reason="sans montant"),
        closed("HOOD", "2026-10-08T15:00:00Z", 50.0, 51.0, 1000.0, pnl_usd=None),
        closed("RIVN", "2026-08-13T18:51:39Z", 15.0, 15.1, 600.0, void=True, void_reason="avant le test"),
    ]
    tr, notes, _ = run(ev)
    tks = [t["ticker"] for t in tr]
    check("void avec PnL compte (PLTR + SOFI)", tks == ["PLTR", "SOFI"], str(tks))
    check("doublon void d'un trade compte: un seul PLTR, non void",
          tks.count("PLTR") == 1 and not tr[0]["void"])
    check("SOFI marque void", tr[1]["void"] is True)
    check("SOFI: rendement net avec commissions",
          abs(tr[1]["ret"] - (-50.0 - 2 * 0.35) / 1000.0) < 1e-9, str(tr[1]["ret"]))
    check("void compte signale", count(notes, "VOID COMPTE") == 1, str(notes))
    check("void sans PnL: NON COMPTE, signale",
          any(s.startswith("NON COMPTE") and "COIN" in s and "VOID" in s for s in notes), str(notes))
    check("trade non void sans PnL: NON COMPTE, signale",
          any(s.startswith("NON COMPTE") and "HOOD" in s for s in notes), str(notes))
    check("void avant le depart: hors periode, pas de note", count(notes, "RIVN") == 0)


def test_corrections():
    print("\n[2] journal_correction applique au trade vise")
    # format reel (trades_journal.jsonl, RGTI, 01/06/2026), date deplacee dans la periode
    corr = {"event": "journal_correction", "ticker": "RGTI", "trade_ts_utc": "2026-10-01T14:03:11Z",
            "corrected": ["exit_price_usd", "pnl_usd", "pnl_pct"], "old_exit_price_usd": 25.11,
            "new_exit_price_usd": 24.5, "old_pnl_usd": -0.64, "new_pnl_usd": -3.08,
            "reason": "retrospective ibkr_fill (sync utilisait cours live)", "ts_utc": "2026-10-01T16:30:00Z"}
    base = dict(pnl_usd=-0.64, exit_price_source="reference_price")
    ev = [closed("RGTI", "2026-10-01T14:03:11Z", 25.27, 25.11, 101.08, **base),
          # meme trade journalise deux fois (double synchro, autre horodatage)
          closed("RGTI", "2026-10-01T14:05:00Z", 25.27, 25.11, 101.08, **base),
          corr]
    tr, notes, _ = run(ev)
    check("un seul trade apres correction + dedoublonnage", len(tr) == 1, str(tr))
    check("PnL corrige -3.08", tr and tr[0]["pnl_usd"] == -3.08)
    check("rendement calcule sur le PnL corrige",
          tr and abs(tr[0]["ret"] - (-3.08 - 0.70) / 101.08) < 1e-9)
    check("trade marque corrige + note", tr and tr[0]["corrected"]
          and count(notes, "correction du journal appliquee") == 1, str(notes))
    check("prix corrige a la main: pas 'estime'", tr and not tr[0]["estimated"])
    # trade deja corrige sur place (cas reel): correction idempotente
    ev2 = [closed("RGTI", "2026-10-01T14:03:11Z", 25.27, 24.5, 101.08, pnl_usd=-3.08), corr]
    tr2, _, _ = run(ev2)
    check("correction idempotente (trade deja corrige)", len(tr2) == 1 and tr2[0]["pnl_usd"] == -3.08)
    orphan = dict(corr, trade_ts_utc="2026-10-02T10:00:00Z")
    old = dict(corr, trade_ts_utc="2026-06-01T14:03:11Z")
    _, notes3, _ = run([closed("PLTR", "2026-10-03T15:00:00Z", 100.0, 110.0, 1000.0), orphan, old])
    check("correction sans trade dans la periode: signalee",
          count(notes3, "SANS trade_closed") == 1 and "2026-10-02" in " ".join(notes3), str(notes3))


def test_missing_in_paper():
    print("\n[3] controle inverse: trades backtest absents du paper (+-1 jour)")
    ts = lambda s: pd.Timestamp(s, tz=NY)  # noqa: E731
    bt = [{"ticker": "RKLB", "entry_ts": ts("2026-10-06 09:30"), "reason": "SL"},
          {"ticker": "HOOD", "entry_ts": ts("2026-10-08 10:30"), "reason": "TP"},
          {"ticker": "PLTR", "entry_ts": ts("2026-10-10 11:30"), "reason": "END"}]
    buys = {"RKLB": [ts("2026-10-07 10:30")], "PLTR": [ts("2026-10-13 09:31")]}
    miss = TR.missing_in_paper(bt, buys)
    check("RKLB a +1 jour: retrouve", all(b["ticker"] != "RKLB" for b in miss))
    check("HOOD sans achat paper: absent", any(b["ticker"] == "HOOD" for b in miss))
    check("PLTR a +3 jours: absent", any(b["ticker"] == "PLTR" for b in miss))
    check("meme tolerance que l'autre sens",
          TR.same_entry(ts("2026-10-06 15:30"), ts("2026-10-07 09:30"))
          and not TR.same_entry(ts("2026-10-06 15:30"), ts("2026-10-08 09:30")))
    # position paper encore ouverte (fill BUY sans trade_closed): elle compte comme entree
    _, _, b2 = run([fill("RKLB", "BUY", "2026-10-06T14:30:48Z", 76.16)])
    check("fill BUY d'une position ouverte retrouve le trade backtest",
          TR.missing_in_paper(bt[:1], b2) == [])


def test_estimated_exit():
    print("\n[5] prix de sortie sans fill IBKR: compte, signale 'estime'")
    auto = dict(source="ibkr_auto", exit_reason="max_hold", exit_price_source=None)
    ev = [
        closed("PLTR", "2026-10-01T15:00:00Z", 100.0, 101.0, 1000.0),  # fill IBKR
        closed("SMCI", "2026-10-02T15:00:00Z", 40.0, 42.0, 1000.0, exit_price_source="reference_price"),
        closed("COIN", "2026-10-03T15:00:00Z", 300.0, 310.0, 1200.0, exit_price_source="corrected_ibkr_statement"),
        closed("HOOD", "2026-10-06T19:50:00Z", 50.0, 52.0, 1000.0, **auto),
        fill("HOOD", "SELL", "2026-10-06T19:50:01Z", 52.0),
        closed("SOFI", "2026-10-07T19:50:00Z", 20.0, 21.0, 1000.0, **auto),
        fill("SOFI", "SELL", "2026-10-07T19:50:01Z", 20.9),   # autre prix: le journal a pris le cours
        closed("RIVN", "2026-10-08T19:50:00Z", 15.0, 15.5, 1000.0, **auto),   # aucun fill SELL
    ]
    tr, notes, _ = run(ev)
    est = {t["ticker"]: t["estimated"] for t in tr}
    check("6 trades comptes (estimes inclus)", len(tr) == 6, str(est))
    check("ibkr_fill: pas estime", est.get("PLTR") is False)
    check("releve IBKR corrige: pas estime", est.get("COIN") is False)
    check("reference_price: estime", est.get("SMCI") is True)
    check("vente du bot avec fill SELL au meme prix: pas estime", est.get("HOOD") is False)
    check("vente du bot, fill a un autre prix: estime", est.get("SOFI") is True)
    check("vente du bot sans fill: estime", est.get("RIVN") is True)
    check("3 notes 'prix de sortie estime'", count(notes, "prix de sortie estime") == 3, str(notes))
    smci = [t for t in tr if t["ticker"] == "SMCI"]
    check("rendement d'un trade estime calcule comme les autres",
          smci and abs(smci[0]["ret"] - (50.0 - 2 * 0.35) / 1000.0) < 1e-9, str(smci))


def test_earnings_rule():
    print("\n[4] blocage avant les resultats: regle du bot (0 a 2 j avant), pas +-2 j")
    raw = {"SOFI": [{"ts": "2026-10-27 08:00:00-04:00"}, {"ts": "2026-07-29 07:00:00-04:00"}],
           "COIN": [{"ts": "2026-10-29 21:00:00-04:00"}]}
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    try:
        earn = TR.load_earnings(path, ["SOFI", "COIN", "PLTR"])
    finally:
        os.remove(path)
    check("dates en jour de New York", earn["SOFI"][-1] == dt.date(2026, 10, 27)
          and earn["COIN"] == [dt.date(2026, 10, 29)], str(earn))
    days = ["2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28"]
    sig = pd.DataFrame({"ts": [pd.Timestamp(d + " 10:30", tz=NY) for d in days] + [pd.Timestamp("2026-10-27 10:30", tz=NY)],
                        "ticker": ["SOFI"] * 5 + ["PLTR"]})
    kept = TR.earnings_filter(sig, earn, 2)
    got = [(t.strftime("%m-%d"), tk) for t, tk in zip(kept["ts"], kept["ticker"])]
    check("J-3 garde, J-2/J-1/J bloques, J+1 garde, autre ticker garde",
          got == [("10-24", "SOFI"), ("10-28", "SOFI"), ("10-27", "PLTR")], str(got))
    cov = TR.earnings_coverage(earn, dt.date(2026, 10, 28))
    check("couverture: alerte COIN? non; SOFI et PLTR oui",
          any(s.startswith("SOFI") for s in cov) and any(s.startswith("PLTR") for s in cov)
          and not any(s.startswith("COIN") for s in cov), str(cov))


def test_start_date():
    print("\n[6] date de depart du protocole")
    check("defaut = 30/09/2026", TR.PROTOCOL_START == "2026-09-30")
    ev = [closed("PLTR", "2026-09-30T03:00:00Z", 100.0, 101.0, 1000.0),   # 29/09 23h a New York
          closed("SOFI", "2026-09-30T14:00:00Z", 20.0, 21.0, 1000.0)]
    tr, _, _ = run(ev, pd.Timestamp(TR.PROTOCOL_START).date())
    check("trade du 29/09 (heure NY) exclu, 30/09 compte", [t["ticker"] for t in tr] == ["SOFI"])


def main_tests():
    test_void()
    test_corrections()
    test_missing_in_paper()
    test_estimated_exit()
    test_earnings_rule()
    test_start_date()
    import test_execution_safety as T
    print("\nResultat: %d OK / %d FAIL" % (T.PASS, T.FAIL))
    return T.FAIL


if __name__ == "__main__":
    sys.exit(1 if main_tests() else 0)
