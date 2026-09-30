# -*- coding: utf-8 -*-
"""
Les 15 caracteristiques de research_features.py, combinees par un modele
(ridge et gradient boosting), predisent-elles le trade semaine HORS echantillon ?
Entraine sur TRAIN (2023-10 -> 2025-03), juge sur TEST (18 derniers mois).
Hyperparametres fixes a l'avance (pas de reglage sur TEST).

Resultat du 2026-09-29: rho in-sample 0.14-0.36, rho TEST 0.00-0.03 (IC a cheval
sur 0) -> surapprentissage, rien a exploiter.

Necessite scikit-learn, absent du venv du bot (volontairement non installe):
  %USERPROFILE%\\anaconda3\\python.exe scripts\\research_features_ml.py
Prerequis: scripts\\research_features.py (produit research_features_events.csv).
"""
import os
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
F = ["rvol", "dayret", "gap", "brk_atr", "ret5", "ret20", "ret60", "dist52", "squeeze",
     "atr_ratio", "trend_al", "spy_day", "spy_trend", "vix", "rsi1h"]
SPLIT = pd.Timestamp("2025-04-01", tz="UTC")


def boot(p, y, w, n=1000):
    rng = np.random.default_rng(0)
    uw, inv = np.unique(w, return_inverse=True)
    g = [np.where(inv == k)[0] for k in range(len(uw))]
    rs = []
    for _ in range(n):
        idx = np.concatenate([g[k] for k in rng.integers(0, len(uw), len(uw))])
        rs.append(pd.Series(p[idx]).rank().corr(pd.Series(y[idx]).rank()))
    rs = np.sort(rs)
    return rs[int(0.025 * n)], rs[int(0.975 * n)]


def main():
    ev = pd.read_csv(os.path.join(HERE, "research_features_events.csv"), parse_dates=["week"])
    ev["ts"] = pd.to_datetime(ev["ts"], utc=True)
    tr, te = ev[ev.ts < SPLIT].copy(), ev[ev.ts >= SPLIT].copy()
    med = tr[F].median()
    xtr, xte = tr[F].fillna(med), te[F].fillna(med)
    mu, sd = xtr.mean(), xtr.std().replace(0, 1)
    for target in ["y_week", "y_week_x", "y_eod"]:
        for name, model in [("ridge", Ridge(alpha=10.0)),
                            ("boosting", HistGradientBoostingRegressor(
                                max_depth=2, learning_rate=0.05, max_iter=100,
                                min_samples_leaf=50, random_state=0))]:
            a = (xtr - mu) / sd if name == "ridge" else xtr
            b = (xte - mu) / sd if name == "ridge" else xte
            model.fit(a, tr[target])
            rho_in = pd.Series(model.predict(a)).rank().corr(tr[target].reset_index(drop=True).rank())
            p = model.predict(b)
            rho = pd.Series(p).rank().corr(te[target].reset_index(drop=True).rank())
            lo, hi = boot(p, te[target].values, te.week.values.astype("int64"))
            top = te[target].values[p >= np.median(p)].mean()
            bot = te[target].values[p < np.median(p)].mean()
            print("%-9s %-8s in-sample rho %+.3f | TEST rho %+.3f [%+.3f,%+.3f] | moitie haute %+.2f%% vs basse %+.2f%%"
                  % (target, name, rho_in, rho, lo, hi, 100 * top, 100 * bot))


if __name__ == "__main__":
    main()
