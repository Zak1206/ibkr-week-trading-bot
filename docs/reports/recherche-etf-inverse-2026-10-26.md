# ETF inverse quand les 8 tickers sont baissiers ? (26/10/2026)

Idée de Zak : quand la stratégie semaine reste sans position parce que les 8
tickers sont baissiers, acheter un ETF inverse pour gagner pendant la baisse.
Recherche en backtest uniquement. Le bot et le test du protocole ne changent
pas.

Script : `scripts/research_inverse_etf.py`.

## 1. Règles figées avant le premier calcul

Écrites le 26/10/2026, avant tout calcul de résultat. Le cadre est celui de la section 15 du rapport du
29/09 (univers de secours, rejeté).

**Fenêtre et découpage.** TRAIN du 01/05/2024 au 31/03/2025, TEST du
01/04/2025 au 28/09/2026, comme la section 15.

**Stratégie longue.** La même que « FIXE-8 » en section 15 :
- signaux breakout semaine des 8 tickers, VIX 22 ;
- SL −10 %, TP +25 %, sortie après 5 séances seulement si en gain ;
- 1 ligne × 1000 $ fixe, sans paliers ;
- frais IBKR (max(0,35 $ ; 0,0035 $ par action)) et 3 bps de spread aller-retour.

Le moteur est `research_engine.py`, sans modification (son empreinte est
dans le protocole).

**Régime baissier.** Le jour D est « baissier » si au moins K des 8 tickers ont
une tendance journalière ≤ 0 à la clôture de D−1. C'est le même score que le
filtre de tendance de la stratégie. K = 6 est la règle testée ; K = 5 et
K = 7 sont des contrôles de sensibilité, pas des candidats.

**ETF inverse, trois variantes figées.**
- (a) PSQ, −1× Nasdaq-100.
- (b) SH, −1× S&P 500.
- (c) SQQQ, −3× Nasdaq-100, surtout pour mesurer l'érosion quotidienne d'un
  levier inverse.

**Quand l'ETF inverse entre et sort.**
- Entrée à l'ouverture de 9h30 d'une séance baissière, seulement si aucune
  position longue n'est ouverte et qu'aucun signal long n'arrive à cette
  barre.
- Si une position longue se ferme en cours de séance, l'inverse attend
  l'ouverture de la séance suivante, si le régime dure encore.
- Sortie à l'ouverture de la première séance qui n'est plus baissière.
- Le long a la priorité. Si un signal long arrive pendant que l'inverse est
  tenu, l'inverse est vendu au même instant et le long prend la ligne. Le
  long garde donc exactement les trades de la stratégie seule.
- Pas de stop sur l'inverse.
- En fin de fenêtre, liquidation au dernier cours.

**Taille et prix.**
- Même règle de taille que les longs : min(1000 $, cash − 0,35 $), en actions
  entières, mêmes frais, mêmes 3 bps.
- Prix journaliers yfinance ajustés : les frais de gestion, les
  distributions et l'érosion sont déjà dans les cours.
- Pour une vente en cours de séance (le long arrive à 10h30, 11h30…), le
  prix est la clôture ajustée de la veille × (1 − L × variation de l'indice
  depuis la clôture de la veille). L'indice est QQQ (PSQ, L = 1 ; SQQQ,
  L = 3) ou SPY (SH, L = 1), en barres 1h. C'est le fonctionnement d'un ETF
  à levier remis à zéro chaque jour.

**Perturbations.** 12, comme en section 15 : ordre des signaux simultanés
mélangé et date de départ décalée de 0 à 16 jours. Chaque perturbation sert à
la fois à la stratégie seule et à la stratégie avec l'inverse, ce qui donne
une comparaison appariée.

**Critère d'adoption.** Il s'applique à une variante à K = 6. Il faut
remplir les quatre conditions :
1. gain médian supérieur à la stratégie seule, en TRAIN et en TEST ;
2. gain apparié (avec l'inverse − seule) positif dans au moins 80 % des
   12 perturbations, en TRAIN et en TEST ;
3. chute max du run médian pas pire de plus de 5 points que la stratégie
   seule, sur chaque période ;
4. aucun trade long perdu : mêmes entrées que la stratégie seule, dans chaque
   perturbation.

Si plusieurs variantes passent, on retient la −1×, moins risquée. Si la
variante passe à K = 6 mais que K = 5 et K = 7 font tous deux moins bien que
la stratégie seule en TEST, le verdict devient « non concluant ».

**Diagnostic, qui ne change pas le verdict.**
- Pour chaque période baissière, rendement de l'ETF inverse seul, de
  l'ouverture qui suit la confirmation à l'ouverture qui suit la fin :
  moyenne, médiane, % de périodes gagnantes, et la même chose pour l'indice.
  Le but : savoir si « 6 sur 8 baissiers » annonce une baisse de l'indice ou
  arrive trop tard.
- Part des séances baissières, et part de ces séances où la ligne était
  libre.
- Contrôle de cohérence : sans inverse, le script doit retrouver au centime
  le résultat du moteur pour la stratégie seule.

## 2. Résultats

**Contrôle.** Sans inverse, le rejeu retrouve le moteur au centime : écart
de 0,000000 sur les 24 runs, en $ comme en points de chute.

**Stratégie seule, référence.**

| période | gain médian | chute max |
|---|---|---|
| TRAIN | +682 $ (+68 %) | −24,6 % |
| TEST | +992 $ (+99 %) | −15,7 % |

La section 15 donnait +759 / +926 $ pour la même stratégie. Les signaux sont
identiques (739 de part et d'autre) ; seul l'ordre de départ des signaux
simultanés diffère, et le moteur les mélange avec une graine fixe. Avec
l'ordre de la section 15, on retrouve +759 / +926 $, et le verdict ne change
pas. L'écart médian dû à l'inverse, à K = 6, devient en TEST −203 $ (PSQ),
−145 $ (SH) et −686 $ (SQQQ), avec 0 % de perturbations où l'inverse aide.
L'ordre des ex-aequo pèse donc à lui seul environ ±70 $ : c'est du bruit.

**Stratégie avec l'ETF inverse.** Médiane de 12 perturbations. Les
pourcentages sont rapportés aux 1000 $ de budget. « Perturbations où
l'inverse aide » compte les runs, appariés, où la combinaison bat la
stratégie seule.

| ETF | K | TRAIN | écart | TEST | écart | chute max TEST | perturbations où l'inverse aide (TRAIN / TEST) | trades inverses, TEST, run médian |
|---|---|---|---|---|---|---|---|---|
| PSQ | **6** | +658 $ | −24 $ (−2 %) | +728 $ | −264 $ (−26 %) | −22,8 % | 8 % / 0 % | 19, −301 $, 21 % gagnants |
| PSQ | 5 | +612 $ | −70 $ | +775 $ | −217 $ | −22,0 % | 0 % / 0 % | 20, −255 $, 35 % gagnants |
| PSQ | 7 | +654 $ | −28 $ | +671 $ | −321 $ | −24,9 % | 42 % / 0 % | 23, −344 $, 26 % gagnants |
| SH | **6** | +647 $ | −35 $ (−4 %) | +791 $ | −201 $ (−20 %) | −21,0 % | 0 % / 0 % | 19, −224 $, 16 % gagnants |
| SH | 5 | +628 $ | −54 $ | +840 $ | −152 $ | −20,2 % | 0 % / 0 % | 19, −176 $, 21 % gagnants |
| SH | 7 | +649 $ | −33 $ | +719 $ | −273 $ | −22,7 % | 0 % / 0 % | 21, −187 $, 19 % gagnants |
| SQQQ | **6** | +594 $ | −88 $ (−9 %) | +82 $ | −910 $ (−91 %) | −42,0 % | 8 % / 0 % | 22, −903 $, 18 % gagnants |
| SQQQ | 5 | +506 $ | −176 $ | +368 $ | −624 $ | −33,3 % | 0 % / 0 % | 21, −706 $, 33 % gagnants |
| SQQQ | 7 | +642 $ | −40 $ | −19 $ | −1011 $ | −40,8 % | 42 % / 0 % | 22, −696 $, 27 % gagnants |

**Verdict : REJETÉ pour les trois ETF.**

| critère (K = 6) | PSQ | SH | SQQQ |
|---|---|---|---|
| 1. meilleur que seul en TRAIN et en TEST | non | non | non |
| 2. au moins 80 % de perturbations où l'inverse aide | non (8 % / 0 %) | non (0 % / 0 %) | non (8 % / 0 %) |
| 3. chute max pas pire de plus de 5 points | non (TEST : +7,1 points) | non (TEST : +5,3 points) | non (TEST : +26,3 points) |
| 4. aucun trade long perdu | non (1, artefact, voir plus bas) | oui | non (1, artefact) |

Le résultat ne dépend pas du seuil : les 9 combinaisons ETF × seuil font
toutes moins bien que la stratégie seule, sur les deux périodes.

## 3. Pourquoi : le régime arrive trop tard

Le calcul porte sur l'ETF seul, pendant chaque période baissière, de
l'ouverture qui suit la confirmation à l'ouverture qui suit la fin. La
fenêtre couvre TRAIN et TEST.

| seuil | périodes | séances concernées | durée médiane | QQQ pendant la période | PSQ | SH | SQQQ |
|---|---|---|---|---|---|---|---|
| 6/8 | 38 | 34 % | 2 séances | +0,84 % en moyenne, en hausse 74 % du temps | −0,79 %, 29 % gagnantes, cumul −26,8 % | −0,46 %, 37 % gagnantes, cumul −16,6 % | −2,88 %, 26 % gagnantes, cumul −69,9 % |
| 5/8 | 51 | 49 % | 2 séances | +0,73 %, en hausse 75 % du temps | cumul −28,8 % | cumul −20,6 % | cumul −73,9 % |
| 7/8 | 33 | 24 % | 2 séances | +0,99 %, en hausse 73 % du temps | cumul −28,2 % | cumul −21,9 % | cumul −69,9 % |

Le jour où 6 des 8 tickers basculent en tendance baissière, l'indice a déjà
baissé, puis il rebondit dans près de 3 cas sur 4. Les périodes sont
courtes, 2 séances en médiane, et un inverse acheté à ce moment-là achète le
creux. Ce n'est pas une affaire de frais : environ 20 trades × 1 à 1,5 $, soit
une trentaine de dollars, contre 200 à 900 $ de pertes. Ce n'est pas non plus
un manque d'occasions : en TEST, sur 140 séances baissières (run médian),
105 avaient la ligne libre à 9h30.

SQQQ perd 2,6 fois plus que PSQ en cumul : le levier ×3 amplifie un pari
déjà perdant.

**Le trade long « perdu ».** Il apparaît dans une perturbation de TRAIN pour
PSQ et pour SQQQ : RKLB à 5,47 $ et 4,75 $, soit 170 à 210 actions. La règle
de taille du moteur réserve 0,35 $ pour les frais, mais à ce nombre
d'actions les frais font 0,60 à 0,74 $. Dès que les pertes de l'inverse font
passer le cash sous 1000 $, l'achat échoue à quelques centimes près. C'est
un artefact du moteur, pas un vrai conflit avec le long. Il ne change pas le
verdict : les critères 1 à 3 échouent déjà.

## 4. Contrainte pratique : les ETF américains sont interdits à Zak

Zak est résident français, avec un compte IBKR en EUR. IBKR bloque, pour les
particuliers de l'EEE, tout produit PRIIPs sans document d'information clé
(KID). Les émetteurs américains n'en publient en général pas, donc PSQ, SH
et SQQQ ne sont pas achetables par Zak. Le backtest sur ces ETF ne sert que
d'indicateur.

Équivalents UCITS cotés en Europe :
- **Xtrackers S&P 500 Inverse Daily Swap UCITS ETF 1C**, ISIN LU0322251520,
  frais 0,50 %/an. Coté à Londres (XSPD, en USD), à Milan (XSPS, en EUR) et
  sur BX Swiss.
- **Amundi Nasdaq-100 Daily (−1x) Inverse UCITS ETF Acc**, ISIN LU1327051279,
  frais 0,60 %/an. Coté sur Xetra et d'autres places allemandes. Petit fonds
  (environ 33 à 47 M€).

Ce que je n'ai pas vérifié :
- l'achat effectif de ces lignes depuis le compte de Zak (KID en anglais ou
  en français, droits de commercialisation en France) ;
- leurs spreads réels.

Il y a aussi un obstacle de calendrier. Ces bourses ferment à 17h30 (Xetra,
Milan) ou 16h30 heure de Londres, soit 11h30 à New York. Elles ne
recouvrent que les deux premières heures de la séance américaine, alors que
la règle testée vend l'inverse au moment où un long arrive, à n'importe
quelle heure.

## 5. Conclusion

**Rejeté.** Acheter un ETF inverse quand les 8 tickers sont baissiers fait
perdre entre 152 et 1011 $ (−15 à −101 %) en TEST selon l'ETF et le seuil,
et aggrave la chute max de 5 à 26 points. Le régime « 6 sur 8 baissiers »
ne prédit pas une baisse de l'indice, il en signale plutôt la fin. Ne pas
re-proposer un inverse ou un short déclenché par ce régime. Pour jouer la
baisse, il faudrait un signal qui l'anticipe, et aucun des signaux de prix
testés depuis septembre n'y arrive.

## Limites

- La fenêtre fait 29 mois, dans un marché haussier. Un inverse y perd par
  construction ; une longue baisse comme celle de 2022 pourrait donner autre
  chose. On ne peut pas la tester : pas de barres 1h avant 2023, et RKLB,
  IONQ, HOOD, SOFI et RIVN sont entrés en bourse en 2020-2021. Le diagnostic
  de la section 3 ne dépend pourtant pas du sens du marché : le régime
  arrive après la baisse.
- Prix de vente en cours de séance reconstruits à partir de QQQ ou SPY, avec
  une faible erreur de suivi. Les ventes de 9h30 et les clôtures utilisent
  les vrais cours.
- Ligne fixe de 1000 $, sans paliers, comme la section 15.

Sources :
- [justETF, Xtrackers S&P 500 Inverse Daily Swap UCITS ETF 1C](https://www.justetf.com/uk/etf-profile.html?isin=LU0322251520)
- [DWS, fiche du fonds](https://etf.dws.com/download/asset/033cb1ec-632f-42a9-bc47-7c1730928d4e)
- [justETF, Amundi Nasdaq-100 Daily (−1x) Inverse UCITS ETF Acc](https://www.justetf.com/en-be/etf-profile.html?isin=LU1327051279)
- [BAWAG, Amundi Nasdaq-100 Daily (−1x) Inverse, cotations](https://markets.bawag.at/etf/tts-313178678/MUL-AN1D1IUE-A)
- [IBKR, PRIIPs Regulation](https://ibkb.interactivebrokers.com/node/2993)
