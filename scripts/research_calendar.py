# -*- coding: utf-8 -*-
"""
Effets de calendrier: les rares "algorithmes" publics qui battent le hasard sur
des decennies, a horizon 1-5 jours, long seulement, sur des ETF d'indice.

  TOM  tournant de mois (Lakonishok & Smidt 1988; McConnell & Xu 2008): du dernier
       jour de bourse du mois au 3e jour du mois suivant. Achat a la cloture de
       l'avant-dernier jour, vente a la cloture du 3e jour (4 seances).
  PRE  veille de jour ferie (Ariel 1990): achat a la cloture 2 jours avant la
       fermeture, vente a la cloture de la veille (1 seance).
Le calendrier est connu a l'avance: aucune anticipation possible.

Test: rendement quotidien moyen des jours de la fenetre contre les autres jours,
IC95% par bootstrap en blocs d'une annee. Sous-periode APRES publication
(McConnell & Xu, 2008 -> 2009-2026) = vrai test hors echantillon.
Frais: 2 x 0.35$ + 2 bps par aller-retour sur 1000$ (~9 bps).

Usage: .\\.venv\\Scripts\\python.exe scripts\\research_calendar.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_data as RD  # noqa: E402

COST = (2 * 0.35 / 1000) + 0.0002


def labels(idx: pd.DatetimeIndex):
    """Pour chaque seance: est-elle dans la fenetre TOM ? est-ce une veille de ferie ?"""
    s = pd.Series(np.arange(len(idx)), index=idx)
    ym = idx.to_period("M")
    last_of_month = s.groupby(ym).transform("max").values == s.values
    first_rank = s.groupby(ym).cumcount().values                # 0 = 1er jour du mois
    tom = np.zeros(len(idx), bool)
    lom = np.where(last_of_month)[0]
    tom[lom] = True
    tom |= first_rank <= 2                                        # jours +1, +2, +3
    # veille de ferie: le jour ouvre suivant (lun-ven) est absent des donnees
    nxt = idx + pd.offsets.BDay(1)
    pre = ~pd.Index(nxt).isin(idx)
    pre[-1] = False
    return tom, pre


def block_boot(x_in, x_out, years_in, years_out, n=2000, seed=0):
    rng = np.random.default_rng(seed)
    yrs = np.unique(np.r_[years_in, years_out])
    gi = {y: x_in[years_in == y] for y in yrs}
    go = {y: x_out[years_out == y] for y in yrs}
    diffs = []
    for _ in range(n):
        pick = rng.choice(yrs, len(yrs), replace=True)
        a = np.concatenate([gi[y] for y in pick]); b = np.concatenate([go[y] for y in pick])
        if len(a) and len(b):
            diffs.append(a.mean() - b.mean())
    return np.percentile(diffs, [2.5, 97.5])


def main():
    rd = RD.load()
    print("=" * 110)
    print("EFFETS DE CALENDRIER — rendement quotidien moyen dans la fenetre vs hors fenetre (bps)")
    print("=" * 110)
    print("%-5s %-6s %-11s | %7s %7s %8s %-18s | %8s %8s %8s"
          % ("ETF", "effet", "periode", "dedans", "dehors", "ecart", "IC95% ecart", "net/tr", "tr/an", "%an net"))
    for tk in ("SPY", "QQQ", "IWM"):
        d = rd["d1"][tk]
        d = d[d.index <= "2026-09-28"]
        r = d["Close"].pct_change()
        tom, pre = labels(d.index)
        for eff, mask, hold in (("TOM", tom, 4), ("PRE", pre, 1)):
            for per, a, b in (("tout", "1900", "2100"), ("1993-2008", "1993", "2008-12-31"),
                              ("2009-2026*", "2009", "2100")):
                m = (d.index >= a) & (d.index <= b) & r.notna().values
                rin, rout = r[m & mask].values, r[m & ~mask].values
                yin, yout = d.index[m & mask].year.values, d.index[m & ~mask].year.values
                lo, hi = block_boot(rin, rout, yin, yout)
                # strategie: gain par fenetre = somme des rendements de la fenetre - frais
                years = max(1e-9, (d.index[m][-1] - d.index[m][0]).days / 365.25)
                n_tr = mask[m].sum() / hold
                per_tr = rin.mean() * hold - COST
                print("%-5s %-6s %-11s | %+7.1f %+7.1f %+8.1f [%+6.1f,%+6.1f] | %+7.2f%% %8.1f %+7.1f%%"
                      % (tk, eff, per, 1e4 * rin.mean(), 1e4 * rout.mean(), 1e4 * (rin.mean() - rout.mean()),
                         1e4 * lo, 1e4 * hi, 100 * per_tr, n_tr / years, 100 * per_tr * n_tr / years))
    print("\n* 2009-2026 = apres la publication de McConnell & Xu (2008): vrai test hors echantillon.")
    print("net/tr = gain moyen par fenetre apres frais; %an net = somme annuelle (capital investi seulement")
    print("pendant les fenetres, le reste du temps libre pour autre chose).")


if __name__ == "__main__":
    main()
