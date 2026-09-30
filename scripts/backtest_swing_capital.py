# -*- coding: utf-8 -*-
"""
Le resultat scale-t-il avec le capital, ou le plancher de frais l'ecrase-t-il ?

Meme strategie (swing -10/+25, cap 10j, sans flat), seul le capital change.
On mesure le rendement en % ET en $/mois, sur la fenetre complete et sur la
2e moitie (plus representative du regime actuel).

Usage: BT_BUDGET_USD / BT_LINE_USD sont lus par backtest_swing.
  .\\.venv\\Scripts\\python.exe scripts\\backtest_swing_capital.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(os.path.dirname(HERE), ".venv", "Scripts", "python.exe")

RUNNER = r'''
import os, sys
sys.path.insert(0, r"{here}")
import backtest_swing as B
import pandas as pd

data = B.load_data()
sig = B.build_signals(data)
h0 = data["h1"][B.TICKERS[0]]
t0, t1 = h0.index[0], h0.index[-1]
mid = t0 + (t1 - t0) / 2
months_full = (t1 - t0).days / 30.44
months_half = (t1 - mid).days / 30.44

v = B.Variant("swing B cap10", 10, 25, max_hold_days=10)

def robust(win, months):
    nets = []
    for k in range(9):
        r = B.simulate(sig, data, v, seed=3000 + k, skip_first_days=(k % 3) * 7, win=win)
        nets.append(sum(t["net"] for t in r["trades"]))
    nets.sort()
    med = nets[len(nets) // 2]
    return med, med / months, 100.0 * med / B.BUDGET_USD, 100.0 * sum(1 for x in nets if x > 0) / len(nets)

full = robust((t0, t1), months_full)
half = robust((mid, t1), months_half)
print("%8.0f|%8.0f|%9.1f|%9.1f|%7.1f|%9.1f|%9.1f|%7.1f|%5.0f" % (
    B.BUDGET_USD, B.TARGET_LINE_USD,
    full[0], full[1], full[2],
    half[0], half[1], half[2], half[3]))
'''.format(here=HERE)


def main():
    configs = [
        (1000, 520), (2500, 1300), (5000, 2600),
        (10000, 5200), (25000, 13000), (50000, 26000),
    ]
    print("=" * 96)
    print("SCALING EN CAPITAL — strategie identique (swing -10/+25, cap 10j)")
    print("=" * 96)
    print("Mediane de 9 perturbations. 2 lignes = 104%% du capital deploye (comme aujourd'hui).")
    print()
    print("%8s %8s | %9s %9s %7s | %9s %9s %7s %5s"
          % ("capital", "ligne", "net $", "$/mois", "%", "net $", "$/mois", "%", ">0"))
    print("%8s %8s | %29s | %33s"
          % ("", "", "---- fenetre complete ----", "------- 2e moitie (regime actuel) -------"))
    print("-" * 96)
    for budget, line in configs:
        env = dict(os.environ)
        env["BT_BUDGET_USD"] = str(budget)
        env["BT_LINE_USD"] = str(line)
        r = subprocess.run([PY, "-c", RUNNER], capture_output=True, text=True, env=env)
        out = [l for l in r.stdout.strip().splitlines() if "|" in l]
        if not out:
            print("%8d %8d | ECHEC: %s" % (budget, line, (r.stderr or "")[-200:]))
            continue
        f = [x.strip() for x in out[-1].split("|")]
        print("%8d %8d | %9s %9s %7s | %9s %9s %7s %4s%%"
              % (budget, line, f[2], f[3], f[4], f[5], f[6], f[7], f[8]))

    print()
    print("Si le %% monte avec le capital, c'est le plancher de 0.35$/ordre qui")
    print("etouffe les petits comptes. S'il est plat, la taille n'est pas le sujet.")


if __name__ == "__main__":
    main()
