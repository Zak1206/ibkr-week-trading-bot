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
  objectif +25 %, sortie forcée après 5 séances seulement si le latent est positif
  (depuis le 06/10/2026, section 8), 1 position à la fois.
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
  5 séances seulement si la clôture est au-dessus de l'entrée, depuis le 06/10/2026)
  et les mêmes coûts que le moteur de backtest (0,35 $ minimum par
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
| 06/10/2026, 21h | `TRADING_MAX_HOLD_ONLY_IF_PROFIT=1` : la sortie forcée après 5 séances ne vend que si le latent est strictement positif. En perte, à plat ou sans prix, on attend le SL −10 % ou le TP +25 % déjà posés chez IBKR. Même règle dans le suivi backtest (`research_engine.py`, `track_week_strategy.py`). | oui : une ligne en perte n'est plus coupée au bout de 5 séances. Les chiffres du rapport du 29/09 sont ceux de l'ancienne règle (sortie quel que soit le P/L). |
| 08/10/2026 (noté le 17/10) | `ibkr_execution.py` : le port 4004 (port paper du conteneur Gateway, déploiement `deploy/oracle/`) compte comme paper dans le message de connexion au démarrage. | aucun : libellé du message seulement. |
| 12/10/2026 | `main.py` : le message Telegram « Heartbeat cycle » inclut le PnL latent (total et par ligne), même calcul que le cockpit. Empreinte `5786e859…` → `039652d0…` (notée le 17/10). | aucun : texte seulement. |
| 17/10/2026 | Les empreintes de la section 9 portent sur les fichiers en LF, comme dans le dépôt GitHub (`.gitattributes`). Celles de `ibkr_execution.py`, `broker_hooks.py`, `research_engine.py` et `track_week_strategy.py` notées le 30/09 et le 06/10 avaient été calculées sur la copie locale en CRLF : même contenu, seules les fins de ligne diffèrent. Vérification : `git show 1af77d8:ibkr_execution.py \| sed 's/$/\r/' \| sha256sum` donne `eba8f13d…`. | aucun : fins de ligne seulement. |
| 22/10/2026 | `main.py` (correction de parité) : la tendance journalière (MTF) et le VIX d'une barre 1h viennent des séances closes **avant le jour de cette barre**, comme dans le backtest. Avant, la barre de 15h30, lue à 16h00 ou le lendemain avant 10h30, utilisait la bougie du jour même (encore provisoire à 16h00), et le VIX était lu en direct pendant la séance. Une clôture journalière vide renvoyée par Yahoo est ignorée (elle mettait la tendance à −2). Plus d'ordre d'achat après 16h00 (IBKR le rejette, Error 201) : la barre est relue au premier scan de la séance suivante, avec une entrée à l'ouverture comme dans le backtest. Cas réel : ACHETER PLTR le 21/10 à 16h00 (tendance +1 au lieu de 0), rejeté par IBKR. Empreinte `039652d0…` → `785adc4f…`. | oui, retour au comportement testé : sur 3 ans, 21 signaux de 15h30 que le backtest ne prend pas sont supprimés, aucun signal du backtest n'est perdu. Parité 250/250 avec journalier et VIX fournis jusqu'au jour de la barre inclus (le contrôle négatif, correctif retiré, trouve un achat hors backtest). Tests 95 + 52 OK. |

## 9. Empreintes (preuve que rien n'a changé)
`python scripts/track_week_strategy.py --hashes` recalcule ces empreintes.
Si l'une d'elles change sans ligne datée à la section 8, le test n'est plus
valide.

| fichier | SHA-256 au 30/09/2026 |
|---|---|
| main.py | `785adc4ffa42dd36468a303b96efcb8701489b3ea0498b57fc7f56a95d40768e` (le 22/10, voir section 8 ; au 12/10 `039652d06106cdc4dd65c067eef7a1716d18580dbdaf1f2a513ff3d3ff0a8a9c` ; au 06/10 `5786e8598dac5e97778512731734c8c745f70b3e8e475a8f22a7e423f35d937a` ; au 30/09 18h50 `2186d73d54bf838e98bb35e34f4176ace69180a9b5c64a6e2bdf90d826394a54`) |
| ibkr_execution.py | `81a0828f595837a655177e32f10db967bb94ce9e8ac5401c1224e5bde1135cb2` (le 08/10, voir section 8 ; au 30/09 `eba8f13d133a3e7c9253e8efd69a50f43f6898c05046c1aede215c737a2f1eb2` en CRLF) |
| broker_hooks.py | `f8f4124b8fe3ce8f7014a8222d17fdc6d55b44fab3e045ca7491705b1a61c151` (inchangé depuis le 30/09 ; `b5dd0f68338d3541bc67bd0369832673b2a455c349dcab0e96546094d545649e` en CRLF) |
| .env.ibkr_paper | `b663a82fe1f21f1024c579f8b607966e7b25c524b4285f2f666a4239563fe34a` (le 06/10 ; au 30/09 `9ac8b917f833913f1ab21e9aff472bbfb11a0de3e85bf0fc54d11912e55ab3ec`) |
| scripts/research_engine.py | `ec5b166e2f38af4947b1910c326e5a39858da3184b18097a6448f5ab383f296f` (le 06/10 ; en CRLF `64d0f5e4131b0d4a1013202091b2005644b5fa011fce6145719a186d681ce1b4` ; au 30/09 `62578c9a0aba7f43ffe554546921fed07d1d2778e16bc0cb51d8ad7291b9b588` en CRLF) |
| scripts/backtest_swing.py | `b18546626bdccfe56469a7f863e78d0717443dd54c2272b56f158530755e7934` |
| scripts/track_week_strategy.py | `feb952c63328c1f877af05a551db97bd16214b2ceea6abea89589d0388e476bf` (le 06/10 ; en CRLF `db9bce7a8094f4545c6de45edc8945cdf23ca3f77e534bc9f56689e27def204f` ; au 30/09 `958a343f6ca5d534043d8860ff97c14fc1a249d9e5db79f6c9a8ec6fa0e3edfb` en CRLF) |

L'empreinte de ce document est enregistrée dans la ligne du 30/09/2026 de
`scripts/track_log.jsonl`. Un document ne peut pas contenir sa propre
empreinte.

**Pour dater le protocole de façon vérifiable**, envoie-toi aujourd'hui ce
fichier par e-mail (ou colle-le dans ton projet « Opération Quant »).
L'horodatage du message fera foi.

