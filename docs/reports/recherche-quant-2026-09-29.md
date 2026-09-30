# Recherche quant — 29 septembre 2026

Objectif fixé par Zak : améliorer la qualité de trading du bot. Horizon imposé :
day trading, une semaine de détention au maximum. Le buy & hold long terme
est exclu comme objectif.

Toutes les stratégies sont comparées sur la même fenêtre (31/10/2023 → 04/09/2026),
avec 1000 $ de capital, 2 lignes de 520 $, les frais IBKR réels (max(0,35 $ ;
0,0035 $/action) par ordre) et 3 bps de spread. Chaque chiffre est la médiane de
20 perturbations (ordre des signaux simultanés, date de départ), jamais un run
unique. Le juge principal est la période récente, les 18 derniers mois.
Aucun paramètre n'a été optimisé : chaque variante est prise telle que publiée,
ou telle qu'elle tourne en production.

## Résumé

1. **La config en production perd de l'argent sur la période récente** : −176 $
   sur les 18 derniers mois, positive dans seulement 10 % des perturbations,
   drawdown de −44 %. Le harnais de septembre surestimait le résultat, car il
   supposait `RISK_OFF_VIX_THRESHOLD=22` alors que le `.env` est à 26.
2. **82 % des signaux d'entrée n'ont aucun pouvoir prédictif.** Seul le
   chemin *breakout* prédit quelque chose : +30 bps d'excès jusqu'à la clôture,
   de justesse significatif. Le score de confiance ne classe rien (ρ ≈ 0,02).
3. **Aucune stratégie de day trading pur ne survit aux frais sur la période
   récente** : ni breakout avec sortie à la clôture, ni ORB 60 min sur 9 ou
   40 tickers, ni effet overnight, ni momentum intraday.
4. **Les variantes semaine gagnent, mais c'est du bêta.** Des entrées tirées
   au hasard font aussi bien que les signaux du bot (rang placebo 42 à 60 %).
5. **Changer de tickers ne sauve pas la config.** 89 % des univers de
   9 tickers tirés au hasard perdent sur les 18 derniers mois, et le résultat
   passé d'un ticker ne prédit pas son résultat futur (ρ = +0,15).
6. **Contrainte bloquante pour le passage en réel** : 54 % des trades du
   journal paper sont des day trades. Sur un compte sur marge sous 25 000 $,
   la règle PDT (3 day trades par 5 jours ouvrés) serait violée dans 61 % des
   semaines.

**Config recommandée : « breakout semaine »** (section 6). Elle combine breakout
seul, VIX 22, SL −10 %, TP +25 %, une sortie forcée à 5 séances et aucun
flat. Résultat : +1610 $, positive sur les deux moitiés (+1021 $ puis
+646 $ sur les 18 derniers mois) et dans 100 % des perturbations. Elle reste
positive dans 80 % d'univers aléatoires de titres volatils et ne fait
quasiment aucun day trade. Son prix : 5,2 % du compte en risque par trade,
et un gain qui vient surtout du bêta, donc une perte à attendre en marché
baissier.

**Changement minimal si ce risque est refusé** :
`BUY_SNIPER_BREAKOUT_REQUIRED=1` seul, avec la sortie actuelle. Il fait
+132 $ sur les 18 derniers mois, positif dans 100 % des perturbations, avec
un drawdown et un nombre de trades divisés par deux, et trois fois moins de
violations PDT. La première moitié reste à −24 $ : ce n'est pas un edge
prouvé, seulement la suppression d'un bruit mesuré.

## 1. Diagnostic de l'entrée (`scripts/research_signal_edge.py`)

Étude d'événement sur 4581 signaux. On mesure le rendement après le signal,
diminué du rendement moyen du même ticker à la même heure. IC 95 % par
bootstrap en grappes par jour.

| sous-ensemble | n | 1 h | 3 h | clôture | ~1 j | ~5 j |
|---|---|---|---|---|---|---|
| tous les signaux | 4581 | +1,6 | +2,6 | +3,7 | −6,9 | +5,1 |
| chemin breakout | 817 | +8,8 | **+29,4\*** | **+30,3\*** | +32,6 | +47,2 |
| chemin structure | 3764 | +0,0 | −3,2 | −2,1 | −15,5 | −4,0 |

(excès en bps ; \* = IC 95 % strictement positif)

Placebo apparié : mêmes sorties, même nombre de **trades**, mêmes tickers et
mêmes heures, mais dates d'entrée tirées au hasard.

| cas | vrai signal | placebo (médiane) | rang |
|---|---|---|---|
| signaux actuels + sortie live | +213 $ (+3,2 bps/tr) | −447 $ (−13,5 bps/tr) | 87–92 % |
| signaux actuels + sortie semaine | +1417 $ | +1255 à +1493 $ | 42–60 % |
| breakout + sortie semaine | +1649 $ | +862 à +1121 $ | 73–88 % |

Lecture : avec la sortie live, l'entrée apporte environ +16 bps par trade par
rapport au hasard. C'est suggestif mais sous le seuil de 95 %, et ça ne couvre
pas les frais (13,5 bps aller-retour à 520 $ + spread). Avec la sortie semaine,
l'entrée n'apporte rien : le gain vient de la détention de titres à fort bêta
dans un marché haussier.

## 2. Config actuelle contre changements structurels (`scripts/research_recommend.py`)

| variante | total | 1re moitié | 18 derniers mois | > 0 (18 mois) | DD | day trades |
|---|---|---|---|---|---|---|
| **0 production (VIX 26, 2×520)** | −125 $ | +166 $ | **−176 $** | 10 % | −44 % | 64 % |
| 1 VIX 22 | +203 $ | +218 $ | −74 $ | 20 % | −27 % | 64 % |
| 2 breakout seul (VIX 26) | +87 $ | −8 $ | **+135 $** | 100 % | −24 % | 65 % |
| 3 breakout seul + VIX 22 | +103 $ | −24 $ | +132 $ | 100 % | −22 % | 64 % |
| 4 breakout + VIX 22, 1 ligne 1000 $ | +70 $ | +38 $ | +207 $ | 95 % | −39 % | 62 % |
| 5 production en 1 ligne 1000 $ | +340 $ | +298 $ | −28 $ | 45 % | −36 % | 63 % |
| 6 breakout + VIX 22, sortie semaine | +1610 $ | +1021 $ | +646 $ | 100 % | −25 % | 1 % |

La variante 6 est la seule à rester positive sur les deux moitiés, et elle ne
fait quasiment aucun day trade. Mais son gain est du bêta (placebo ci-dessus).
Elle gagne si ces titres montent. En 2022, le panier des 9 a perdu −57 %. Le
filtre de tendance du bot aurait ramené la perte à environ −17 %, et le filtre
SPY > SMA200 n'aurait pas protégé : les titres ont chuté de −38 % pendant les
jours où SPY était encore au-dessus de sa moyenne.

## 3. Nouvelles stratégies courtes (`scripts/research_strategies.py`)

Toutes les règles sont publiées, aucun réglage n'a été fait.

| stratégie | horizon | total | 18 derniers mois | verdict |
|---|---|---|---|---|
| breakout seul, sortie à la clôture | day | −149 $ | +23 $ | réfuté |
| ORB 60 min « stocks in play », 9 tickers | day | +200 $ | −30 $ | réfuté |
| ORB 60 min, pool de 40 tickers volatils | day | train +188 $ | test −77 $ | réfuté |
| RSI(2) Connors, univers du bot | 1–5 j | +701 $ | +79 $ | fragile (t = 1,6 ; perd en 2022) |
| RSI(2) Connors, 14 ETF | 1–5 j | +369 $ | +206 $ | edge réel mais mince, dépend du régime |
| effet overnight (achat au close, vente à l'ouverture) | nuit | — | — | réfuté : 3–9 bps/nuit sur ETF < 10 bps de frais |
| momentum intraday (1re heure → dernière) | 30 min | — | — | réfuté : ±1 bp |

Sur le RSI(2) : son t = 3,45 sur 14 ETF paraît solide. Le test sur 2001-2026
sert uniquement à vérifier si ce score vient du régime, pas à juger le bot
actuel. Résultat : la stratégie perd sur 2010-2019 (−4 bps/trade) et sur
2020-2022 (−9 à −30 bps). La fenêtre 2023-2026 est sa meilleure période en
25 ans. Face au hasard, le signal existe (rang 100 %), mais il vaut environ
+10 bps nets par trade, soit ~25 $ par an sur 1000 $.

## 4. Recherche de tickers (`scripts/research_tickers.py`)

Protocole : pool S&P 500 + 400 + 600 (1512 tickers). Filtre mécanique sur la
période train uniquement : prix entre 5 et 200 $, au moins 100 M$ échangés
par jour, TP +3,5 % atteint dans au moins 15 % des séances. 38 tickers
passent. On choisit 9 tickers avec les données train (2023-10 → 2025-03), et
on juge sur les 18 derniers mois.

| sélection | train | test (18 mois) | rang vs hasard |
|---|---|---|---|
| univers actuel | +215 $ | −322 $ | 63 % |
| sélection mécanique (TP atteignable, décorrélée) | +100 $ | −406 $ | 52 % |
| meilleur backtest train (décorrélé) | **+1586 $** | **−146 $** | 80 % |
| 9 tickers au hasard (100 tirages) | — | −417 $ médiane, 11 % > 0 | — |

- Corrélation de rang train → test des résultats par ticker : ρ = +0,15
  (non significatif sur 42 tickers). Le meilleur quintile train finit à
  −16 $ sur la période test.
- Seul HOOD est positif et important sur les deux moitiés (+267 $ / +317 $).
  Sur 42 tickers, en trouver un par hasard est attendu.

Conclusion : le problème n'est pas l'univers. Choisir des tickers sur
backtest fabrique un gagnant sur la période train qui s'évapore sur la
période test. Si l'univers doit évoluer, il faut le faire sur des critères
mécaniques (liquidité, prix, volatilité, décorrélation), jamais sur le P&L
passé.

## 5. Contraintes réelles ignorées par tout backtest

- **PDT** : 44 des 81 trades clos du journal paper (54 %) sont des day
  trades, avec jusqu'à 18 sur 5 jours ouvrés. Un compte réel sur marge sous
  25 000 $ est limité à 3. À vérifier : type de compte live (cash ou marge)
  et règle PDT en vigueur chez IBKR. En compte cash, il n'y a pas de PDT,
  mais le règlement en T+1 interdit de réutiliser le même jour le produit
  d'une vente.
- **Plancher de frais** : 0,35 $ par ordre représente 13,5 bps aller-retour
  sur 520 $. L'edge brut mesuré de la sortie live (~10 à 20 bps) est
  entièrement absorbé à cette taille.
- **Fragilité des données** : sur une même fenêtre, deux téléchargements
  Yahoo donnent −141 $ et −227 $. Un écart de ±100 $ est du bruit.

## 6. Config recommandée : « breakout semaine »

C'est la seule configuration qui gagne sur les deux moitiés de la fenêtre,
dans 100 % des perturbations, et sur la plupart des autres univers de titres
volatils.

| test | résultat |
|---|---|
| fenêtre complète, univers actuel | +1610 $ (p10 +1285, p90 +1737) |
| 1re moitié / 18 derniers mois | +1021 $ / +646 $ |
| 60 univers aléatoires de 9 titres volatils, train | positive dans 90 % (médiane +640 $) |
| 60 univers aléatoires, 18 derniers mois | positive dans 80 % (médiane +240 $) |
| même entrée avec la sortie live, 18 derniers mois | positive dans 30 % des univers |
| drawdown / day trades | −25 % / 1 % des trades |

**Ce que ça veut dire** : la sortie semaine laisse courir les tendances que
le TP à +3,5 % et le flat coupaient. L'entrée breakout apporte un petit
avantage face au hasard (rang placebo de 73 à 88 %), mais l'essentiel du gain
vient de la détention de titres volatils quand ils montent. En marché
baissier, attends-toi à perdre : en 2022, même avec le filtre de tendance
par ticker, l'exposition au panier aurait perdu environ −17 %.

**Réglages, sur le profil paper uniquement** :

| clé | actuel | recommandé | pourquoi |
|---|---|---|---|
| `BUY_SNIPER_BREAKOUT_REQUIRED` | 0 | 1 | supprime les 82 % de signaux sans edge |
| `RISK_OFF_VIX_THRESHOLD` | 26 | 22 | valeur par défaut du code, c'est celle qui a été testée |
| `BUY_FIXED_STOP_PCT` / `BUY_SNIPER_MAX_STOP_PCT` | 2,0 / 2,0 | 10 / 10 | SL −10 % |
| `BUY_RR_REWARD_MULT` | 1,75 | 2,5 | TP +25 % |
| `TRADING_RISK_PER_TRADE_PCT` | 1,0 | 5,2 | sinon le plafond de risque ramène la ligne à 100 $ |
| `TRADING_INTRADAY_FLAT_ENABLED` | 1 | 0 | pas de flat |
| `TRAILING_TP_ENABLED` | 1 | 0 | pas de trailing |
| `YF_INTERVAL` / `MTF_INTERVAL` / `MTF_PERIOD` | 15m / 1h / 3mo | 1h / 1d / 1y | timeframes du backtest |
| `BUY_BLOCK_NEAR_3MO_HIGH` | 1 (défaut) | 0 | filtre absent du backtest |
| *nouveau* : sortie forcée après 5 séances | — | à coder | n'existe pas dans `main.py` |

**Le vrai prix** : chaque stop coûte environ 52 $, soit 5,2 % du compte,
contre 1 % aujourd'hui. Six stops d'affilée, et le compte perd environ 30 %.

## 7. Recommandations, par ordre

1. **Ne pas passer en réel avec la config actuelle.** Elle est négative sur
   la période récente et violerait la règle PDT.
2. **Si le risque de 5,2 % par trade est accepté**, faire tourner
   « breakout semaine » en paper. Cela suppose d'abord de coder la sortie
   à 5 séances. Sinon, se contenter de `BUY_SNIPER_BREAKOUT_REQUIRED=1`
   avec la sortie actuelle : c'est un flag déjà codé, et c'est la variante 3
   du tableau 2.
3. **Ajouter un garde-fou PDT au code** si la sortie live est conservée :
   compter les day trades sur 5 jours ouvrés glissants, et bloquer les
   nouvelles entrées du jour quand le compteur atteint 3.
4. **Critère d'arrêt avant le réel** : après 60 trades paper (environ
   un an avec la sortie semaine, qui fait ~1,2 trade par semaine), si le
   résultat est inférieur à celui des entrées aléatoires, on arrête.

## 8. Suite : signaux, implémentation, univers, objectif

### 8.1 D'autres signaux que le breakout ? (`scripts/research_features.py`, `research_features_ml.py`)

Base : 2504 breakouts sur 42 tickers volatils, en gardant le premier par ticker
et par séance. On teste 15 caractéristiques connues au moment du signal. Le
sens attendu de chacune est déclaré avant le test, d'après la littérature :

- volume relatif, rendement du jour, gap, force de la cassure ;
- momentum 5, 20 et 60 jours, distance au plus haut 52 semaines ;
- compression des Bollinger, contraction de l'ATR, alignement des moyennes ;
- SPY intraday, tendance SPY, VIX, RSI 1h.

Critère d'adoption : corrélation significative dans le sens attendu sur la
période train, et même sens sur les 18 derniers mois (IC bootstrap en
grappes par semaine).

| résultat | détail |
|---|---|
| signaux adoptés | **aucun** sur 15. Toutes les \|ρ\| < 0,12. |
| seul effet stable | momentum 20 j → rendement jusqu'à la clôture (ρ +0,12 / +0,07, significatif sur les deux périodes), mais de **sens opposé** à l'hypothèse déclarée : post-hoc, non adopté. Piste pour une future recherche intraday. |
| modèle multivarié (ridge, boosting) | ρ in-sample 0,14–0,36 → **ρ sur la période test 0,00–0,03**, IC à cheval sur 0 : surapprentissage |

Conclusion : aucun indicateur supplémentaire, seul ou combiné, n'améliore la
prédiction hors échantillon. La qualité ne viendra pas d'indicateurs en plus.

### 8.2 Implémentation, profil paper uniquement

Nouveau code dans `main.py`, désactivé par défaut. Il n'est actif que dans
`.env.ibkr_paper`, et le profil live est inchangé.

- `TRADING_MAX_HOLD_SESSIONS=5` : vente au marché dans les 30 dernières
  minutes de la 5ᵉ séance détenue, via `attempt_ibkr_sell`, qui annule et
  restaure le bracket. Jours fériés NYSE 2025-2027 intégrés
  (`TRADING_EXTRA_MARKET_HOLIDAYS` pour la suite). Retry toutes les 20 s,
  alerte Telegram après 3 échecs.
- `SCAN_COMPLETED_BARS_ONLY=1` / `MTF_COMPLETED_BARS_ONLY=1` : le bot
  décidait sur la barre 1h en formation et sur la bougie journalière du jour.
  Il décide maintenant, comme le backtest, sur des barres clôturées et sur
  la tendance de la veille.
- `RISK_PDT_GUARD_ENABLED=1` : plus de nouvelle entrée dès 3 day trades sur
  5 jours ouvrés.

Réglages coupés parce qu'ils auraient cassé la stratégie : flat intraday,
trailing, swaps (une ligne perdante pouvait être revendue via
`/swap_oui`), vente « flash », plafond de risque à 1 % (ligne ramenée à
~100 $), garde de drawdown à 2,5 % (un seul stop bloquait la journée).
Sauvegarde avant modification : `archive/backup_2026-09-29/`.

### 8.3 Vérifications

| test | résultat |
|---|---|
| `scripts/test_week_strategy.py` (nouveau) | **40 OK / 0 FAIL** : séances et jours fériés, fenêtre, sortie à 5 séances avec IBKR simulé, barres en formation, PDT, SL/TP, plafond de risque |
| `scripts/test_execution_safety.py` (existant) | **52 OK / 0 FAIL**, aucune régression |
| `scripts/parity_week_strategy.py` : vrai pipeline du bot (profil paper) contre backtest, 644 barres | **100 % d'accord** : 250/250 signaux retrouvés, 0 achat hors backtest |
| contrôle négatif du test de parité (chemin structure réactivé) | 33 désaccords détectés → le test est bien sensible |
| `scripts/dryrun_week_strategy.py` : décision en direct, yfinance réel | chaîne complète OK : barre en formation ignorée, VIX de la veille, fondamentaux et earnings |

Outils :

- **yfinance** : 1h sur 3 mois et journalier sur 1 an vérifiés en direct.
- **IBKR** : brackets SL/TP en GTC, donc ils tiennent plusieurs jours. Ordres
  MIDPRICE à l'entrée, vente au marché pour la sortie à durée max. En paper,
  les données temps réel exigent les abonnements du compte live partagés,
  sinon le bot retombe sur le différé. C'est acceptable à l'échelle 1h,
  les SL/TP étant recalculés au fill.
- **À vérifier de ton côté** : le type de compte live (cash ou marge). Le
  Gateway doit tourner pour le paper.

### 8.4 Univers retenu

Règle mécanique appliquée aux 12 derniers mois : prix entre 5 et 200 $, au
moins 100 M$ échangés par jour, TP +3,5 % atteint dans au moins 15 % des
séances. Les 8 titres volatils passent. **CVS échoue** (3 %) et il est
retiré.

- Sur le P&L, l'effet est neutre (18 derniers mois : +617 $ sans CVS contre
  +608 $ avec), et le drawdown passe de −19 % à −14 %.
- Élargir à un pool de 40 titres n'apporte rien sur la période récente
  (+555 $ contre +607 $).

Univers paper : `PLTR, SMCI, IONQ, HOOD, SOFI, COIN, RIVN, RKLB`.

### 8.5 Combien viser par mois

Backtest de la config retenue (8 tickers, 1000 $, frais réels, 16
perturbations) :

| | fenêtre complète | 18 derniers mois |
|---|---|---|
| moyenne | +41 $/mois | +36 $/mois |
| mois médian | +12 $ | +0 $ |
| mois positifs | 53 % | 46 % |
| mois p10 / pire | −127 $ / −257 $ | −99 $ / −147 $ |
| meilleur mois | +455 $ | +344 $ |
| trades | ~5 par mois | ~4,5 par mois |

**Objectif réaliste : +15 à +25 $ par mois en moyenne sur un an (1,5 à
2,5 %/mois), en marché favorable.** J'abaisse le backtest pour trois
raisons. L'univers a été choisi a posteriori (des univers sans ce biais
font +14 à +31 $/mois). Le gain est du bêta. Et le réel dégrade toujours
un peu le backtest. À prévoir : **un mois sur deux négatif**, des mois à
−10 / −25 %, et une perte en marché baissier. Le résultat vient de
quelques gros mois : c'est le profil d'un suivi de tendance, pas celui
d'une rente.

Validation : à ~5 trades par mois, il faut environ **12 mois de paper** pour
60 trades. Critère d'arrêt : si le paper fait moins bien que des entrées
aléatoires dans les mêmes titres, on arrête.

## 9. Version finale : maximiser l'argent rapporté

### 9.1 Taille des positions (variantes testées sur les deux périodes)

| variante | train | 18 derniers mois | $/mois (complet) | DD | décision |
|---|---|---|---|---|---|
| 2 × 520 $ | +918 $ | +617 $ | +41 $ | −23 % | ancienne |
| **1 × 1000 $** | **+1292 $** | **+848 $** | **+62 $** | −28 % | **retenue** : meilleure sur les deux moitiés, même Sharpe |
| 3 × 340 $ | +794 $ | +414 $ | — | −22 % | rejetée |
| 2 lignes à inverse volatilité | +915 $ | +543 $ | — | −16 % | rejetée |
| 1 ligne réinvestie à 100 % | +1648 $ | +860 $ | +125 $ | **−52 %** | rejetée : rien de plus sur 18 mois, drawdown ×2 |

Avec cette stratégie, le gain est proportionnel au capital : les frais
deviennent négligeables (~170 bps gagnés par trade). Backtest : environ +63 $
par mois pour 1000 $, +320 $ pour 5000 $, +650 $ pour 10 000 $.

### 9.2 Coupe-circuit et suivi

- `RISK_MAX_DRAWDOWN_PCT=35` : plus aucune entrée si le capital réalisé
  depuis le 29/09 tombe à −35 % sous son plus haut, au-delà du pire drawdown
  du backtest (−28 %). Alerte Telegram, reprise manuelle.
- `scripts/track_week_strategy.py` : compare les trades paper au backtest de
  la même période et à 100 bots qui entrent au hasard avec les mêmes sorties.
  À 30 trades, il rend un verdict : continuer si le paper bat le hasard,
  arrêter sinon. Sur la période paper passée (mai → août), la nouvelle
  stratégie aurait fait +161 $ en 11 trades, contre +170 $ en 80 trades pour
  l'ancienne.
- Tests : `test_week_strategy.py` **58/58**, dont la cohérence du vrai
  profil paper. `test_execution_safety.py` **52/52**.

### 9.3 Verdict : est-ce le meilleur bot pour un particulier avec 1000 $ ?

Même fenêtre, 1000 $ au départ, frais réels.

| | gain (35 mois) | $/mois | DD max | Sharpe | gain (18 derniers mois) | DD |
|---|---|---|---|---|---|---|
| **ton bot** | **+2098 $** | **+60 $** | −29 % | **1,58** | **+879 $** | −18 % |
| bot au hasard, mêmes sorties | +1644 $ | +47 $ | −34 % | 1,05 | +659 $ | −36 % |
| SPY acheté et gardé | +898 $ | +26 $ | −19 % | 1,52 | +387 $ | −12 % |
| QQQ acheté et gardé | +1135 $ | +33 $ | −23 % | 1,38 | +569 $ | −13 % |
| les 8 mêmes actions gardées | +5794 $ | +166 $ | −41 % | 1,43 | +952 $ | −43 % |

**Ce qui est vrai :**

- En backtest, le bot rapporte environ 2 fois plus que les indices sur les
  deux périodes, avec le meilleur Sharpe de la fenêtre complète.
- Face au bot au hasard, il fait moitié moins de drawdown sur la période
  récente.
- L'immense majorité des day traders particuliers perd de l'argent (Barber
  et al. sur Taïwan, Chague et al. sur le Brésil). Un bot à espérance
  positive après frais est donc déjà au-dessus de la plupart des
  particuliers.

**Ce qui ne l'est pas encore :**

- La supériorité de ses entrées sur le hasard n'est pas démontrée (rang de
  62 à 68 %, il faudrait 95 %).
- Il rapporte moins que garder les mêmes 8 actions sur la fenêtre complète.
- Ses 8 tickers ont été choisis après leur envolée.
- C'est un backtest : il n'a encore aucun trade réel.
- « Le meilleur du marché » ne peut pas être certifié : les bots commerciaux
  ne publient pas de résultats audités.

**Conclusion** : c'est la meilleure configuration trouvée parmi une trentaine
testées pour 1000 $ chez IBKR. Elle est implémentée à l'identique du
backtest et protégée par un coupe-circuit. Mais c'est une machine qui capte
le mouvement de titres volatils en tendance, pas une machine à prédire. Le
seul juge est le verdict de `track_week_strategy.py` à 30 trades, soit
environ 6 mois de paper à ~5 trades par mois.

## 10. Le test de Zak (période paper) et la tentative d'atteindre 95 %

### 10.1 Sur la période où le bot a tourné en paper (`scripts/compare_paper_period.py`)

Du 26/05 au 27/08/2026, sur 31 jours d'activité. Marché plat : SPY +3 %,
QQQ −1 %, les 8 actions +6 %.

| | jours où le bot tournait | période complète |
|---|---|---|
| **paper réel, ancienne config**, net de frais estimés | **+106 $** (80 trades) | — |
| nouvelle config en backtest (médiane [p10, p90]) | **+104 $** [−62, +506], 6 trades | −152 $ [−211, +237], 9 trades |
| bot au hasard, mêmes sorties | +61 $ | +43 $ |
| ancienne config en backtest | −47 $ | −133 $ |

Lecture :

- Sur ta période, la nouvelle config **fait jeu égal** avec ton paper réel. Elle ne le bat pas.
- 6 à 9 trades ne permettent de rien conclure.
- Le backtest sous-estimait l'ancienne config d'environ 150 $. Elle tournait en 15 minutes en live, mais était imitée en 1h. Cet écart disparaît avec la nouvelle config (parité 100 %).

### 10.2 Peut-on faire battre le hasard à 95 % ? (`research_edge95.py`, `research_pead.py`)

Test puissant : chaque entrée est comparée à 16 entrées au hasard dans le
même titre, à la même heure, sur ±30 séances en excluant ±5 autour de
l'événement. Environ 2500 événements, 42 tickers. Les règles sont tirées de
la littérature et fixées avant le test.

| entrée | avantage face au hasard, train / test | verdict |
|---|---|---|
| breakout 1h du bot | −0,8 % / −0,7 % | ne bat pas le hasard |
| choc de volume, continuation (Chan 2003) | +0,4 % / −1,2 % | idem |
| baisse sans volume, rebond | −1,4 % / −2,4 % (négatif significatif en test) | idem |
| RSI(2) en tendance | −0,9 % / −0,5 % | idem |
| plus haut 52 semaines | −1,4 % / −0,5 % | idem |
| résultats : toutes publications | −0,3 % / −0,3 % | idem |
| résultats : BPA > consensus | −0,3 % / −0,8 % | idem |
| résultats : BPA > consensus + gap haussier | +0,5 % / +0,2 % (IC ±2,7 %) | positif, non significatif |
| résultats : réaction ≥ +5 % | +0,4 % / −0,5 % | ne bat pas le hasard |

Il faudrait environ +1,2 % par trade pour que le portefeuille batte le
hasard à 95 %. Aucun signal n'en approche.

**Piège évité** : un premier témoin tiré sur ±5 séances donnait −3,3 % au
breakout et +3,2 % au RSI(2). C'était un artefact. Les signaux sont définis
par le mouvement qui les précède, donc un témoin tiré juste avant le signal
capture ce mouvement.

**Conclusion** : avec les données gratuites (prix, volumes, dates et
surprises de résultats), sur des titres aussi liquides, 95 % n'est pas
atteignable honnêtement. Continuer à chercher jusqu'à ce qu'un signal passe
95 % par hasard serait du p-hacking, et il s'effondrerait en réel. Un vrai
avantage demanderait une information que le marché n'a pas déjà intégrée :
flux d'options, short interest, révisions d'analystes, sentiment des news.
Ce sont des données payantes, sans garantie de résultat.

### 10.3 Au passage

- Le filtre earnings « inerte » du backtest de septembre n'était pas dû à
  yfinance : c'est `lxml` qui manque dans le venv du bot. Avec `lxml`
  (Anaconda), on récupère l'historique complet (`research_earnings_dates.json`).
- En live, le blocage des entrées 0 à 2 jours avant une publication touche
  2 % des signaux. Effet quasi nul : +59 $/mois au lieu de +62 $, drawdown
  −26 % au lieu de −29 %.

## 11. Nouvelles sources, autres univers, objectif de 10 % par mois

### 11.1 Sources testées (`research_sources_data.py`, `research_sources.py`, `research_analysts.py`)

Toutes sont gratuites et ont un historique, ce qui permet de les tester. Le
protocole est le même : sens fixé avant le test, période train puis 18
derniers mois, 2504 breakouts sur 42 tickers.

| source | accès | résultat |
|---|---|---|
| volatilité implicite / réalisée (variation, écart, rang) | **ton compte IBKR** (API, 3 ans) | rien (\|ρ\| < 0,07) |
| part des ventes à découvert (Boehmer, Jones & Zhang 2008) | FINRA, fichiers quotidiens gratuits | rien (\|ρ\| < 0,05) |
| actions d'analystes Briefing.com | ton compte IBKR (depuis nov. 2024) | rien ; le relèvement comme entrée fait +0,08 % face au hasard |
| recommandations et objectifs de cours | yfinance `upgrades_downgrades` (depuis 2021) | objectifs : rien. **Abaissement récent** : −3,1 % sur la période train (significatif), −1,3 % sur les 18 derniers mois (non significatif). En portefeuille, il aide l'univers actuel mais **dégrade** les 18 derniers mois sur le pool de 42. **Non adopté.** |
| Bitcoin 24 h / 5 j, sur les titres crypto | yfinance | rien |
| dates et surprises de résultats | yfinance (avec `lxml`) | rien (section 10) |

Sources payantes (flux d'options, sentiment, short interest détaillé) :
comptées à ~50 $/mois, elles coûtent **5 % par mois d'un capital de 1000 $**.
La donnée devrait rapporter plus que ça avant même de commencer à gagner.
C'est absurde à ce niveau de capital.

### 11.2 Autres univers (`research_themes.py`)

Chaque thème est défini par les principales lignes de son ETF, sans choix
manuel.

| univers | train | 18 derniers mois | $/mois | DD | bot au hasard (18 mois) |
|---|---|---|---|---|---|
| **univers actuel (8)** | +1266 $ | +896 $ | +61 $ | −29 % | +814 $ |
| biotech | +1081 $ | +711 $ | +62 $ | −26 % | +1342 $ |
| uranium / nucléaire | +476 $ | +605 $ | +32 $ | −29 % | +478 $ |
| mineurs crypto | +910 $ | −138 $ | +26 $ | −35 % | +1316 $ |
| crypto en direct (IBIT, ETHA) | — | +182 $ | ~+10 $ | −22 % | +41 $ |
| semi-conducteurs | −152 $ | +483 $ | +10 $ | −44 % | +1062 $ |
| **cybersécurité** | +223 $ | +134 $ | **+5 $** | −39 % | +454 $ |
| quantique | −308 $ | +50 $ | −9 $ | −64 % | +352 $ |

- La cybersécurité ne colle pas : ces titres sont trop peu volatils pour une
  sortie à +25 % en 5 séances.
- La stratégie a échoué sur les mineurs crypto sur la période récente.
- Le classement train → test entre thèmes est faible (ρ = +0,21), donc
  choisir un thème sur son backtest est un pari.
- **On garde l'univers actuel.**

### 11.3 Objectif de 10 % par mois

Il est impossible à garantir, quelle que soit la stratégie : ce serait
+214 %/an, sans aucun mois perdant. Avec levier, ta stratégie donne en
backtest (35 mois, intérêts de marge à 6,5 %) :

| levier | moyenne / mois | mois médian | mois ≥ +10 % | pire mois | chute max | risque de perdre la moitié |
|---|---|---|---|---|---|---|
| x1 (actuel) | +3,9 % | +0,4 % | 17 % | −10 % | −29 % | 0 % |
| x2 (max IBKR la nuit) | +6,3 % | +0,8 % | 24 % | −15 % | −40 % | 17 % |
| x3 (interdit la nuit) | +7,9 % | +0,6 % | 26 % | −18 % | −47 % | 42 % |

Rendement annualisé de la config actuelle (ligne fixe de 1000 $, gains non
réinvestis) : **+47 %/an composé en backtest** (+53 % sur les 18 derniers
mois). En réel, compter **+30 à +40 %/an en marché favorable**.

## 12. Changer d'univers automatiquement quand ça flop ? (`research_dynamic_universe.py`)

L'univers de 8 tickers est choisi le 1er de chaque mois, avec uniquement les
données passées, dans un pool de 115 titres. La stratégie est identique
partout (1 × 1000 $). Période train : 2024-05 → 2025-03 ; test : 18 derniers
mois.

| règle | train | 18 derniers mois | p10 test | verdict |
|---|---|---|---|---|
| 8 fixes (actuels) | +604 $ | +895 $ | +510 $ | référence (choisis en août 2026, avec du recul) |
| pool entier, sans sélection | −257 $ | +1193 $ | +518 $ | — |
| **R1 : les 8 meilleurs du bot sur 3 mois** (idée de Zak) | **−270 $** | +1537 $ | +1234 $ | **incohérent** (le signe change) : rejeté |
| **R2 : suspension après 2 pertes** | +618 $ | **+650 $** | +412 $ | **moins bien** que sans (−245 $) : rejeté |
| R3 : momentum 6 mois | −42 $ | +974 $ | +529 $ | rejeté |
| R4 : les 8 plus volatils (12 mois) | +498 $ | +1130 $ | +962 $ | positif partout, mais **fragile** (voir ci-dessous) |
| R5 : momentum parmi les volatils | −203 $ | −62 $ | −456 $ | rejeté |

Sensibilité de R4 :

- sur 6 mois de volatilité, la période train devient négative (−585 / −134 $) ;
- avec 12 tickers, les 18 derniers mois tombent à +161 $ (p10 −306 $).

Le bon résultat tient donc à un seul réglage : c'est de la chance, pas une
règle.

**« Que des trades gagnants » est impossible.** Avec SL −10 % / TP +25 %,
environ un trade sur deux perd, quel que soit l'univers.

**Décision** : pas de rotation automatique. La seule raison validée de
changer un ticker, c'est qu'il ne remplit plus la règle mécanique (trop
calme pour la sortie à +25 %, comme CVS). `scripts/universe_review.py`
l'indique chaque mois et propose des remplaçants, sans rien modifier. Au
29/09/2026, les 8 tickers passent la règle.

## 13. Hybride day/week, et « un algorithme qui bat le hasard »

### 13.1 Hybrides (`research_hybrid.py`, 8 tickers, 1000 $)

| variante | train | 18 derniers mois | $/mois | part de day trades |
|---|---|---|---|---|
| **semaine (actuel)** | +1266 $ | +808 $ | +61 $ | 0 % |
| coupe la perte au 1er soir | +980 $ | +355 $ | +44 $ | 48 % |
| coupe si < −3 % au 1er soir | +1378 $ | +815 $ | +66 $ | 9 % |
| semaine + day trade ORB quand le capital dort | +323 $ | +328 $ | +18 $ | 99 % |
| 500 $ semaine + 500 $ day trade ORB | +837 $ | +300 $ | +36 $ | 78 % |

- La partie day trading coûte de l'argent : l'ORB seul fait −11 $ sur les 18 derniers mois.
- Couper la position le 1er soir fait rater les rebonds.
- La variante « −3 % au 1er soir » gagne +7 $ sur 18 mois : c'est du bruit, et elle ajoute des day trades (règle PDT).
- **On garde la stratégie semaine seule.**

### 13.2 Effets de calendrier sur 30 ans (`research_calendar.py`)

Deux effets : le tournant de mois (McConnell & Xu 2008) et la veille de jour
férié (Ariel 1990). La période après la publication (2009-2026) est le vrai
test.

- Tournant de mois, après frais : SPY +2,4 %/an, QQQ +4,2 %/an, IWM +0,7 %/an.
  Ces gains ne demandent d'être investi que 4 jours par mois.
- Mais les IC incluent zéro, et l'effet sur SPY a fondu après publication
  (+6,5 → +1,5 bp/jour).
- Veille de jour férié : +0,2 à +0,8 %/an. Un seul test sur 18 est
  significatif, ce qu'on attend du hasard.
- **Effet réel dans l'histoire, mais il ne bat plus nettement le hasard.**

### 13.3 Réponse : qui bat vraiment le hasard ?

| qui | comment | accessible à un particulier avec 1000 $ ? |
|---|---|---|
| teneurs de marché haute fréquence (ex. Virtu : 1 seul jour perdant sur 1238 d'après son prospectus d'introduction en bourse de 2014) | vitesse, colocation, rabais d'échange | non |
| Renaissance Medallion | données massives, arbitrage statistique, levier, vente à découvert | non (fermé) |
| primes de risque diversifiées (trend following sur 50+ contrats à terme, carry, momentum) | un siècle de preuves (Hurst, Ooi & Pedersen 2017) | seulement via des fonds, à horizon de plusieurs mois |
| analyse technique court terme sur actions liquides | environ 40 variantes testées ici | ne bat pas le hasard après frais, comme dans la littérature (Park & Irwin 2007) |

Ceux qui battent le hasard ont un avantage structurel : vitesse, données,
capital, vente à découvert, diversification sur des dizaines de marchés. Avec
1000 $ en actions US, ce qui reste, ce sont les primes de risque. La
stratégie semaine en capte déjà une : le bêta et le momentum de titres
volatils.

## 14. Réinvestissement par paliers et protocole du verdict (30/09)

Backtest avec 8 tickers et 1 ligne, chaque période repartant de 1000 $ :

| taille | 35 mois | 18 derniers mois | DD max (35 mois) |
|---|---|---|---|
| fixe à 1000 $ | +2254 $ | +820 $ | −26 % |
| paliers +25 %, montants seulement | +4320 $ | +844 $ | −52 % |
| **paliers +25 %, montants et descendants** (retenu, à la demande de Zak) | +4213 $ | +828 $ | −48 % |
| tout réinvesti | +4805 $ | +855 $ | −53 % |

- Les paliers ne paient que dans une longue hausse. Sur la période récente,
  ils n'ajoutent rien et creusent le drawdown : une ligne fixe réduit
  d'elle-même son exposition quand le compte grossit.
- Avec les paliers, le coupe-circuit à −35 % arrête le bot dans 100 % des
  backtests sur 35 mois (+1054 $ au lieu de +4213 $). Il est donc passé à
  −50 % (arrêts dans 6 % des cas).
- Le protocole du verdict est figé avant le premier trade, dans
  `docs/protocole-verdict-bot.md`.

## Audit indépendant des scripts

Un audit en lecture seule n'a trouvé aucun look-ahead dans les générateurs de
signaux ni dans le moteur. Toutes les exécutions tombent dans le range
de leur barre. Points relevés :

- l'univers S0 a d'abord été évalué sur 6 tickers : corrigé, les chiffres
  ci-dessus viennent du run à 9 ;
- les entrées MOC ne réutilisent pas un slot libéré au même close : le RSI(2)
  est sous-estimé de 15 à 20 % (+811 $ au lieu de +701 $ sur le bot), ce qui
  ne change pas son verdict de dépendance au régime ;
- le glissement sur les stops n'est pas modélisé : cela flatte les stratégies
  à stop serré (config live, ORB) plus que la sortie semaine ;
- la sélection S2 exigeait 20 trades sur la période test (fuite) : corrigé,
  sans effet sur la sélection.

## Limites

- Scan 1h avec MTF journalier au lieu de 15m / 1h, `fund_score` neutralisé,
  filtre earnings inerte (yfinance n'a pas l'historique des dates).
- Les filtres anti-chase, plus haut 3 mois et fondamental ne sont pas
  répliqués (écart hérité du harnais de septembre).
- Composition actuelle des indices : biais du survivant, faible à horizon
  d'une semaine.
- Fenêtre de 3 ans dans un marché haussier : le panier des 9 fait +498 %
  sur la fenêtre, ce qui gonfle toute stratégie longue.

## Reproduire

```powershell
.\.venv\Scripts\python.exe scripts\test_week_strategy.py       # tests de la strategie semaine
.\.venv\Scripts\python.exe scripts\parity_week_strategy.py     # bot == backtest ?
.\.venv\Scripts\python.exe scripts\dryrun_week_strategy.py     # que deciderait le bot maintenant ?
.\.venv\Scripts\python.exe scripts\research_features.py        # signaux candidats
%USERPROFILE%\anaconda3\python.exe scripts\research_features_ml.py
.\.venv\Scripts\python.exe scripts\research_data.py            # cache ETF (1 fois)
.\.venv\Scripts\python.exe scripts\research_signal_edge.py     # diagnostic de l'entrée
.\.venv\Scripts\python.exe scripts\research_strategies.py      # comparaison des stratégies
.\.venv\Scripts\python.exe scripts\research_recommend.py       # changements structurels
.\.venv\Scripts\python.exe scripts\research_mr_longhistory.py  # RSI(2) sur 25 ans
.\.venv\Scripts\python.exe scripts\research_tickers.py download
.\.venv\Scripts\python.exe scripts\research_tickers.py eval
```
