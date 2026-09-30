# Protocole du verdict — stratégie « breakout semaine » (paper IBKR)

**Rédigé et figé le 30 septembre 2026, avant le premier trade de la stratégie.**
Ce protocole fixe à l'avance la mesure, le groupe de comparaison et les seuils.
Il ne pourra plus être choisi après avoir vu les résultats. Toute modification
invalide le test en cours et oblige à en redémarrer un nouveau, avec un
nouveau protocole daté.

## 1. Question posée
En conditions réelles (paper IBKR), les entrées de la stratégie font-elles
mieux que des entrées tirées au hasard, avec les mêmes actions, les mêmes
sorties et sur la même période ?

## 2. Ce qui est testé (figé)
- La configuration est le bloc « BREAKOUT SEMAINE » de `.env.ibkr_paper`,
  avec le code du 30/09/2026 (empreintes SHA-256 à la section 9).
- **Stratégie** : breakout 1h sur barres clôturées, VIX < 22, stop −10 %,
  objectif +25 %, sortie forcée après 5 séances, 1 position à la fois.
- **La règle d'entrée est testée en entier**, avec tous ses filtres : breakout,
  VIX < 22, tendance journalière, blocage autour des résultats, garde-fous.
  Les bots aléatoires n'appliquent **aucun** de ces filtres (section 4).
- **Univers figé** pendant tout le test : PLTR, SMCI, IONQ, HOOD, SOFI,
  COIN, RIVN, RKLB. La revue mensuelle (`universe_review.py`) signale, mais
  aucun ticker n'est remplacé avant le verdict final.
- **La taille des positions (paliers de +25 %) n'intervient pas dans le
  verdict**, qui est calculé en rendement par trade.

## 3. Mesure
- **Unité** : un trade clos (événement `trade_closed` du journal), daté après
  le 29/09/2026.
- **Rendement d'un trade paper** = (PnL en $ − commissions estimées) / montant
  investi. Les commissions estimées valent 2 ordres × max(0,35 $ ; 0,0035 $
  par action). Le spread est déjà inclus dans les prix réellement exécutés.
- **Score** = somme des rendements de tous les trades clos.

## 4. Groupe de comparaison : 1000 bots aléatoires
- **Mêmes 8 tickers, même période** : du 30/09/2026 à la dernière barre
  clôturée à la date de l'examen.
- **Entrées possibles** : l'ouverture de **chaque** barre 1h de séance de la
  période, sans aucun filtre (ni VIX, ni tendance, ni breakout).
- **Rendement de chaque entrée possible** : un trade entré à l'ouverture de
  la barre, avec les mêmes sorties (stop −10 %, objectif +25 %, sortie après
  5 séances) et les mêmes coûts que le moteur de backtest (0,35 $ minimum par
  ordre, 0,0035 $ par action, 3 bps de spread, une position de 1000 $). Une
  entrée dont le trade n'est pas terminé à la date de l'examen est exclue.
- **Chaque bot tire exactement N entrées** au hasard parmi toutes les entrées
  possibles de la période, N étant le nombre de trades clos du paper. Son
  score est la somme de leurs rendements. Tous les bots couvrent donc toute
  la période avec le même nombre de trades que le paper. Le score étant une
  somme de rendements par trade, un chevauchement de positions ne le change
  pas.
- **Rang du paper** = % des 1000 bots dont le score est inférieur à celui du
  paper.
- **Rang du backtest** : la stratégie est simulée sur la même période, avec
  le même moteur (`scripts/research_engine.py`). Son score, sur ses N'
  trades, est comparé de la même façon à 1000 bots tirant N' entrées. Ce
  second rang neutralise l'écart d'exécution entre les vrais fills du paper et
  le spread modélisé des bots.
- **Rang retenu = le plus bas des deux.**

## 5. Examens et décisions (seuils fixés aujourd'hui)
Le « rang » est le rang retenu de la section 4 : le plus bas entre celui du
paper et celui du backtest. Le nombre de trades est celui du paper.

L'examen à 30 trades ne peut qu'arrêter le test, jamais conclure à un
avantage. Regarder deux fois ne gonfle donc pas le risque de faux positif,
qui reste sous 5 % à 60 trades.

| examen | condition | décision |
|---|---|---|
| avant 30 trades | — | **aucun verdict**, quel que soit le résultat (sauf coupe-circuit) |
| **à 30 trades** | rang < 50 % | **ARRÊT** : la stratégie ne bat pas le hasard. Pas de réglage pour la « sauver ». |
| à 30 trades | rang ≥ 50 % | **on continue** jusqu'à 60 trades |
| **à 60 trades** | rang ≥ 95 % | **avantage démontré** : passage envisageable en réel, avec 1000 $ propres |
| à 60 trades | 50 % ≤ rang < 95 % | **NON CONCLUANT, accepté d'avance** : pas de passage en réel sur la base de ce test |
| à 60 trades | rang < 50 % | **ARRÊT** |
| à tout moment | coupe-circuit configuré du bot déclenché (`RISK_MAX_DRAWDOWN_PCT` : 50 % sous le plus haut au 30/09/2026, valeur liée aux paliers de taille) | **ARRÊT**, verdict « échec » |

## 6. Contrôle secondaire (non décisif)
On compare le paper au backtest de la même période, avec les mêmes règles,
trade par trade (`track_week_strategy.py`). Un trade paper absent du backtest,
ou l'inverse, signale un **problème d'exécution** à corriger, pas un
résultat de la stratégie. Une correction de bug qui ne change pas les
décisions est autorisée et notée dans ce document, avec la date.

## 7. Ce qu'on s'interdit pendant le test
- Changer un paramètre de la stratégie, l'univers, la mesure, les seuils ou
  la date de départ.
- Exclure des trades (« celui-là ne compte pas »).
- Regarder le rang avant 30 trades pour décider de quoi que ce soit.
- Relancer le test sur une autre période choisie après coup.

## 8. Journal des modifications autorisées
| date | modification | effet sur les décisions |
|---|---|---|
| 30/09/2026 | création du protocole | — |
| 30/09/2026, avant tout trade | bots : N entrées tirées sur toute la période, au lieu des « N premiers trades » | évite de comparer des bots sur une période plus courte, ou sur moins de trades, que le paper |
| 30/09/2026, avant tout trade | bots explicitement sans filtre (VIX, tendance, breakout) | on teste toute la règle d'entrée |
| 30/09/2026, avant tout trade | rang retenu = min(rang du paper, rang du backtest) | neutralise l'écart d'exécution réel / simulé |
| 30/09/2026, avant tout trade | coupe-circuit désigné par sa clé de config (50 %) | lève l'ambiguïté avec l'ancienne valeur de 35 % |
| 30/09/2026, 18h50, avant tout trade | `main.py` : les textes de log et de justification affichent l'intervalle scanné (`YF_INTERVAL`, 1h en paper) au lieu de « 15m » écrit en dur. Empreinte `b21c9931…` → `2186d73d…` | aucun : texte seulement. Parité 250/250, tests 78 + 52 OK |

## 9. Empreintes (preuve que rien n'a changé)
`python scripts/track_week_strategy.py --hashes` recalcule ces empreintes.
Si l'une d'elles change sans ligne datée à la section 8, le test n'est plus
valide.

| fichier | SHA-256 au 30/09/2026 |
|---|---|
| main.py | `2186d73d54bf838e98bb35e34f4176ace69180a9b5c64a6e2bdf90d826394a54` (le 30/09 à 18h50, voir section 8 ; au départ `b21c993139f6624a4a51f0797e9ec794814a9f030781e81ec65463f25104b784`) |
| ibkr_execution.py | `eba8f13d133a3e7c9253e8efd69a50f43f6898c05046c1aede215c737a2f1eb2` |
| broker_hooks.py | `b5dd0f68338d3541bc67bd0369832673b2a455c349dcab0e96546094d545649e` |
| .env.ibkr_paper | `9ac8b917f833913f1ab21e9aff472bbfb11a0de3e85bf0fc54d11912e55ab3ec` |
| scripts/research_engine.py | `62578c9a0aba7f43ffe554546921fed07d1d2778e16bc0cb51d8ad7291b9b588` |
| scripts/backtest_swing.py | `b18546626bdccfe56469a7f863e78d0717443dd54c2272b56f158530755e7934` |
| scripts/track_week_strategy.py | `958a343f6ca5d534043d8860ff97c14fc1a249d9e5db79f6c9a8ec6fa0e3edfb` |

L'empreinte de ce document est enregistrée dans la ligne du 30/09/2026 de
`scripts/track_log.jsonl`. Un document ne peut pas contenir sa propre
empreinte.

**Pour dater le protocole de façon vérifiable**, envoie-toi aujourd'hui ce
fichier par e-mail (ou colle-le dans ton projet « Opération Quant »).
L'horodatage du message fera foi.

