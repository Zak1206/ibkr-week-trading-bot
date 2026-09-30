# -*- coding: utf-8 -*-
"""
Reglage de la variante swing: balayage du cap de temps et du SL/TP en
multiples d'ATR journalier — AVEC validation train/test.

Pourquoi le train/test: balayer ~20 combinaisons sur une seule fenetre et
garder la meilleure, c'est fabriquer un gagnant par selection. Ici chaque
candidat est mesure separement sur la 1re moitie (train) et la 2e moitie
(test) de la fenetre. Le chiffre qui compte n'est pas "qui gagne sur train"
mais la CORRELATION DE RANG entre train et test: si elle est nulle, le
reglage ne generalise pas et il ne faut rien regler du tout.

Chaque candidat est evalue sur R perturbations (ordre des signaux simultanes
+ date de depart), et on retient la MEDIANE, pas le meilleur tirage.

Usage:
  .\\.venv\\Scripts\\python.exe scripts\\backtest_swing_tune.py
  .\\.venv\\Scripts\\python.exe scripts\\backtest_swing_tune.py --reps 12
"""
import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_swing import (  # noqa: E402
    LIVE_RR_MULT, LIVE_STOP_PCT, TICKERS, Variant,
    build_signals, load_data, simulate,
)


def candidates():
    out = [
        Variant("REF config live", LIVE_STOP_PCT, LIVE_STOP_PCT * LIVE_RR_MULT,
                flat=True, trailing=True),
        Variant("REF Swing B -10/+25 cap10", 10, 25, max_hold_days=10),
    ]
    # 1) balayage du cap de temps autour de 10j, SL/TP figes a -10/+25
    for cap in (4, 6, 8, 10, 12, 15, 20, 30):
        out.append(Variant("cap %2dj  (-10/+25)" % cap, 10, 25, max_hold_days=cap))
    # 2) SL/TP en multiples d'ATR journalier, cap 10j
    #    ATR/j median de l'univers ~5.9% -> 1.75x ~ -10%, 4.25x ~ +25%
    for sl_m, tp_m in ((1.25, 3.0), (1.25, 4.25), (1.75, 3.0), (1.75, 4.25),
                       (1.75, 6.0), (2.5, 4.25), (2.5, 6.0), (2.5, 8.0)):
        out.append(Variant("ATR %.2fx/%.2fx cap10" % (sl_m, tp_m),
                           10, 25, max_hold_days=10,
                           atr_sl_mult=sl_m, atr_tp_mult=tp_m))
    return out


def robust_median(sig, data, v, win, reps):
    nets = []
    for k in range(reps):
        r = simulate(sig, data, v, seed=2000 + k,
                     skip_first_days=(k % 4) * 6, win=win)
        nets.append(sum(t["net"] for t in r["trades"]))
    nets.sort()
    return nets[len(nets) // 2], nets[0], nets[-1], \
        100.0 * sum(1 for x in nets if x > 0) / len(nets)


def spearman(a, b):
    n = len(a)
    ra = {v: i for i, v in enumerate(sorted(a))}
    rb = {v: i for i, v in enumerate(sorted(b))}
    d2 = sum((ra[x] - rb[y]) ** 2 for x, y in zip(a, b))
    return 1 - 6.0 * d2 / (n * (n * n - 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=8)
    args = ap.parse_args()

    data = load_data()
    sig = build_signals(data)
    h0 = data["h1"][TICKERS[0]]
    t0, t1 = h0.index[0], h0.index[-1]
    mid = t0 + (t1 - t0) / 2

    print("=" * 112)
    print("REGLAGE SWING — balayage avec validation train/test")
    print("=" * 112)
    print("TRAIN : %s -> %s" % (t0.date(), mid.date()))
    print("TEST  : %s -> %s" % (mid.date(), t1.date()))
    print("%d perturbations par candidat et par moitie ; on retient la MEDIANE."
          % args.reps)
    print()
    print("%-28s %28s %28s" % ("", "--------- TRAIN ---------",
                               "---------- TEST ---------"))
    print("%-28s %9s %9s %7s %9s %9s %7s"
          % ("candidat", "median $", "pire $", ">0", "median $", "pire $", ">0"))
    print("-" * 112)

    tr_scores, te_scores, names = [], [], []
    for v in candidates():
        tr = robust_median(sig, data, v, (t0, mid), args.reps)
        te = robust_median(sig, data, v, (mid, t1), args.reps)
        tr_scores.append(tr[0])
        te_scores.append(te[0])
        names.append(v.name)
        print("%-28s %+9.1f %+9.1f %6.0f%% %+9.1f %+9.1f %6.0f%%"
              % (v.name, tr[0], tr[1], tr[3], te[0], te[1], te[3]))

    print("-" * 112)
    # on exclut les 2 references pour juger la generalisation du REGLAGE seul
    rho_all = spearman(tr_scores, te_scores)
    rho_tune = spearman(tr_scores[2:], te_scores[2:])
    print("\nCorrelation de rang train vs test:")
    print("  sur tous les candidats      : rho = %+.2f" % rho_all)
    print("  sur les reglages seuls      : rho = %+.2f" % rho_tune)
    print("  (rho proche de 0 => le classement du train ne dit rien du test:")
    print("   le reglage fin est du bruit, seul le choix d'echelle compte)")

    best_tr = max(range(len(names)), key=lambda i: tr_scores[i])
    best_te = max(range(len(names)), key=lambda i: te_scores[i])
    rank_of_train_winner = sorted(te_scores, reverse=True).index(te_scores[best_tr]) + 1
    print("\n  Meilleur sur TRAIN : %s  (%+.1f$)" % (names[best_tr], tr_scores[best_tr]))
    print("     -> son rang sur TEST : %d / %d  (%+.1f$)"
          % (rank_of_train_winner, len(names), te_scores[best_tr]))
    print("  Meilleur sur TEST  : %s  (%+.1f$)" % (names[best_te], te_scores[best_te]))

    # candidat le plus regulier: meilleur rang moyen sur les deux moities
    rk_tr = {n: i for i, n in enumerate(sorted(names, key=lambda n: -tr_scores[names.index(n)]))}
    rk_te = {n: i for i, n in enumerate(sorted(names, key=lambda n: -te_scores[names.index(n)]))}
    steady = sorted(names, key=lambda n: rk_tr[n] + rk_te[n])[:3]
    print("\n  Les 3 plus reguliers (meilleur rang cumule train+test):")
    for n in steady:
        i = names.index(n)
        print("     %-28s train %+8.1f$ (rang %2d)  test %+8.1f$ (rang %2d)"
              % (n, tr_scores[i], rk_tr[n] + 1, te_scores[i], rk_te[n] + 1))


if __name__ == "__main__":
    main()
