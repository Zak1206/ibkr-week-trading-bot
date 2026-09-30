# -*- coding: utf-8 -*-
"""
Chiffre final, stabilise. Aucune selection de variante ici: on prend la config
live et la variante swing, et on les mesure avec assez de repetitions pour que
le resultat ne bouge plus d'un run a l'autre.

Les runs precedents utilisaient 8-9 perturbations: trop peu, la mediane sautait
de +19$ a +257$ selon la graine. Ici 60 perturbations, et on publie l'intervalle
p10-p90, pas seulement la mediane.

Usage: .\\.venv\\Scripts\\python.exe scripts\\backtest_swing_final.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_swing import (  # noqa: E402
    BUDGET_USD, LIVE_RR_MULT, LIVE_STOP_PCT, TICKERS, Variant,
    build_signals, load_data, simulate,
)

REPS = 60


def dist(sig, data, v, win, months):
    nets = []
    for k in range(REPS):
        r = simulate(sig, data, v, seed=7000 + k,
                     skip_first_days=(k % 6) * 5, win=win)
        nets.append(sum(t["net"] for t in r["trades"]))
    nets.sort()
    n = len(nets)
    return {
        "med": nets[n // 2] / months,
        "p10": nets[int(0.10 * n)] / months,
        "p90": nets[int(0.90 * n)] / months,
        "pos": 100.0 * sum(1 for x in nets if x > 0) / n,
        "tot_med": nets[n // 2],
    }


def main():
    data = load_data()
    sig = build_signals(data)
    h0 = data["h1"][TICKERS[0]]
    t0, t1 = h0.index[0], h0.index[-1]
    mid = t0 + (t1 - t0) / 2
    m_full = (t1 - t0).days / 30.44
    m_half = (t1 - mid).days / 30.44

    variants = [
        ("Config live actuelle",
         Variant("live", LIVE_STOP_PCT, LIVE_STOP_PCT * LIVE_RR_MULT,
                 flat=True, trailing=True)),
        ("Swing -10/+25 cap 10j", Variant("s10", 10, 25, max_hold_days=10)),
        ("Swing -10/+25 cap 20j", Variant("s20", 10, 25, max_hold_days=20)),
    ]

    print("=" * 92)
    print("CHIFFRE FINAL — %d perturbations par ligne, capital 1000$, non compose" % REPS)
    print("=" * 92)
    print("Fenetre complete : %s -> %s  (%.1f mois)" % (t0.date(), t1.date(), m_full))
    print("18 derniers mois : %s -> %s  (%.1f mois)" % (mid.date(), t1.date(), m_half))

    for label, win, months in (("FENETRE COMPLETE (2.9 ans)", (t0, t1), m_full),
                               ("18 DERNIERS MOIS", (mid, t1), m_half)):
        print("\n--- %s ---" % label)
        print("%-24s %11s %24s %8s" % ("", "$/mois", "p10-p90 $/mois", "% >0"))
        for name, v in variants:
            d = dist(sig, data, v, win, months)
            print("%-24s %+11.1f   [%+8.1f , %+8.1f] %7.0f%%"
                  % (name, d["med"], d["p10"], d["p90"], d["pos"]))

    # reference buy & hold sur les memes fenetres
    print("\n--- REFERENCE: panier des 9 achete et garde ---")
    for label, a, b, months in (("Fenetre complete", t0, t1, m_full),
                                ("18 derniers mois", mid, t1, m_half)):
        tot = 0.0
        per = BUDGET_USD / len(TICKERS)
        for tk in TICKERS:
            h = data["h1"][tk]
            seg = h[(h.index >= a) & (h.index <= b)]
            if seg.empty:
                continue
            tot += per * float(seg["Close"].iloc[-1]) / float(seg["Close"].iloc[0])
        gain = tot - BUDGET_USD
        print("%-24s %+11.1f   (%+.0f%% au total)" % (label, gain / months,
                                                      100.0 * gain / BUDGET_USD))


if __name__ == "__main__":
    main()
