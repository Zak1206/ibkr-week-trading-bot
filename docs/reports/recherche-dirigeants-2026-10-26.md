# Copier les achats des dirigeants (Form 4) : critère rempli, rien de jouable

26/10/2026. Statut : **terminée**. Les résultats sont en bas, après la méthode, qui n'a pas changé.

## Premier essai (bloqué)

- Source prévue : les « Insider Transactions Data Sets » trimestriels de la SEC (ZIP de fichiers TSV, depuis 2006).
- User-Agent déclaré : `ibkr-week-trading-bot-research https://github.com/Zak1206/ibkr-week-trading-bot`, sans adresse e-mail.
- Réponse : 403, sur la page (`/dera/data/form-345`) comme sur un ZIP (`2024q1_form345.zip`). La page d'erreur renvoie à la politique « Fair Access » de la SEC.
- Trois requêtes en tout, donc ce n'est pas un problème de débit. La SEC demande un User-Agent qui contienne un nom et une adresse e-mail de contact. Le nôtre n'en a pas.
- Rien n'a été contourné : pas d'autre User-Agent, pas de scraping d'un autre site.

Zak a ensuite accepté de déclarer une adresse de contact. Elle passe par une variable d'environnement (`SEC_USER_AGENT`) au lancement, et n'est écrite dans aucun fichier du dépôt.

## Méthode, fixée avant toute donnée

Ces règles s'appliqueront telles quelles quand les données seront accessibles. Tout ce qui sera ajouté après sera étiqueté « post-hoc ».

**Question.** Acheter après l'achat en bourse d'un dirigeant bat-il le hasard après frais ? Horizons décisifs : 1 et 5 séances. Horizons de 20 et 60 séances : pour information seulement.

**Événements.**
- On garde uniquement les formulaires de type `4` (pas les `4/A`), les transactions au code `P` acquises (`A`), avec un prix > 0.
- Titres exclus : ceux qui contiennent PREFERRED, NOTE, WARRANT, UNIT ou DEBENTURE.
- On retient les déclarants qui sont dirigeants (Officer) ou administrateurs (Director). Ceux qui ne sont qu'actionnaires à plus de 10 % sont mis à part.
- Le montant d'un dépôt est la somme des actions × prix des transactions `P`.
- Entrée à l'ouverture de la première séance qui suit `FILING_DATE`. Le jeu de données ne donne pas l'heure de dépôt, donc jamais d'entrée le jour même. On n'utilise jamais la date de transaction.
- Contrôle du ticker : le prix de la transaction doit être à ±20 % de la clôture Yahoo non ajustée du jour de la transaction, reconstruite avec les splits. Sinon l'événement est écarté. Ça protège contre les tickers réattribués à une autre société.
- Un seul événement par titre sur une fenêtre de 5 séances.

**Variantes, trois au maximum :**
- (a) tout achat ≥ 25 k$ d'un dirigeant ou d'un administrateur ;
- (b) achat groupé : au moins 2 déclarants distincts, avec chacun un dépôt ≥ 25 k$, dans les 10 jours calendaires. L'événement est daté du dépôt qui atteint 2 ;
- (c) PDG ou directeur financier ≥ 100 k$. Le titre doit contenir CEO, CHIEF EXECUTIVE, PRINCIPAL EXECUTIVE, CFO, CHIEF FINANCIAL ou PRINCIPAL FINANCIAL.

**Univers tradable**, mesuré à la clôture du jour de dépôt, donc avant l'entrée :
- actions ordinaires US à prix non ajusté > 5 $ ;
- volume moyen sur 20 séances > 5 M$ ;
- prix journaliers Yahoo.

Le biais de survie (titres radiés absents de Yahoo) sera signalé avec la part d'événements perdus par année.

**Frais.**
- Commission IBKR de max(0,35 $ ; 0,0035 $ × actions) par ordre, sur une ligne de 1000 $ en actions entières.
- Spread aller-retour de 3 pb au-dessus de 20 M$ de volume moyen, de 10 pb entre 5 et 20 M$.

**Comparaisons.**
- Témoin apparié : même titre, 16 entrées tirées dans ±30 séances, en excluant ±5 séances (voisinage contaminé, cf. recherche du 29/09), même sortie.
- Excès contre SPY sur les mêmes séances.
- IC95 % par bootstrap groupé par date d'entrée (2000 tirages).
- Train : 2012 → 2019. Test : 2020 → dernière donnée. On ne passe à 2021 que si le train a moins de 200 événements par variante. Cette règle est décidée avant de regarder les résultats.

**Critère d'adoption** (le même qu'à la recherche du 29/09) : IC95 % de l'avantage > 0 sur le train, ET moyenne > 0 sur le test, à 5 séances. Sinon c'est un rejet. Repère : pour qu'environ 170 trades battent le hasard à 95 %, il faut un avantage d'environ +1,2 % par trade.

**Version actionnable.**
- 1 ligne × 1000 $, SL −10 % / TP +25 % / sortie à la clôture de la 5e séance, simulation journalière.
- Si un jour touche à la fois le SL et le TP : on compte le SL.
- En cas de gap : sortie à l'ouverture.
- Plusieurs événements le même jour, ligne libre : on prend le plus gros montant.
- Sorties : $/mois et %/mois, signaux par mois, pire mois.
- Comparaison avec SPY et avec la stratégie semaine. Attention : ses chiffres du 29/09 sortent de la règle « sortie à 5 séances quel que soit le P/L », celle d'avant le 06/10.

## Ce que ça change pour le bot

Rien pendant le test en cours : protocole figé, univers de 8 titres, verdict à 30 trades. Même adoptée, cette piste serait une autre stratégie, avec un autre univers, à tester à part.

## Résultats (26/10/2026)

Scripts : `scripts/research_insider_fetch.py` (ZIP SEC), `research_insider_events.py` (dépôts), `research_insider_prices.py` (cours Yahoo), `research_insider_study.py` (méthode ci-dessus).

**Données.**
- 77 315 dépôts candidats depuis 2012 : dirigeant ou administrateur, au moins 25 k$, prix de transaction d'au moins 3 $.
- 7 028 tickers. 4 031 ont des cours Yahoo, 2 997 n'en ont pas (radiés, rachetés ou rebaptisés).
- Contrôle des absents : aucun n'est coté aujourd'hui d'après les listes Nasdaq Trader du 26/10, sauf PARA, ticker réattribué, sans historique.
- Un premier téléchargement avait pris des limites de débit de Yahoo pour des radiations : 478 titres vivants (GE, JNJ, HD…) marqués absents à tort. Corrigé avant toute analyse.
- Le contrôle du ticker (±20 %) passe pour 89,6 % des dépôts qui ont des cours.

**Biais de survie.** Part des dépôts dont le ticker a des cours : environ 50 % de 2012 à 2017, 61 % en 2019, 67 % en 2020, 80 % en 2023, 97 % en 2026. Les sociétés disparues, souvent en faillite, manquent surtout dans la période d'apprentissage : ses rendements absolus sont gonflés. L'avantage contre le témoin l'est moins, puisque le témoin est le même titre.

**Étude d'événement à 5 séances** (frais compris ; témoin = même titre, ±30 séances hors ±5) :

| variante | événements (train / test) | avantage vs témoin, train [IC95] | avantage vs témoin, test [IC95] | net moyen, test | écart vs SPY, test [IC95] |
|---|---|---|---|---|---|
| (a) dirigeant/admin ≥ 25 k$ | 6 512 / 8 709 | +0,65 % [+0,45 ; +0,87] | +0,31 % [−0,20 ; +0,83] | −0,10 % | −0,12 % [−0,41 ; +0,19] |
| (b) achats groupés | 1 347 / 2 122 | +1,05 % [+0,65 ; +1,43] | +0,75 % [−0,15 ; +1,66] | −0,10 % | +0,04 % [−0,51 ; +0,63] |
| (c) PDG/DAF ≥ 100 k$ | 1 329 / 2 035 | +1,12 % [+0,76 ; +1,48] | +0,60 % [−0,18 ; +1,39] | +0,03 % | +0,07 % [−0,46 ; +0,59] |

**Verdict selon le critère figé : les trois variantes passent** (IC95 train > 0 ET moyenne test > 0 à 5 séances).

À 1 séance, l'avantage contre le témoin est significatif en test pour les trois (+0,35 % à +0,56 %), et l'écart contre SPY aussi pour (c) : +0,32 % [+0,02 ; +0,62]. À 20 et 60 séances, l'avantage contre le témoin grandit (+1,5 % à +6 %), mais l'écart contre SPY reste non significatif en test.

**Ce que le critère ne dit pas.** En test, à 5 séances :
- l'avantage contre le témoin n'est pas significatif à lui seul ;
- le rendement net, sans comparaison, est d'environ 0 ;
- l'écart contre SPY est d'environ 0.

L'avantage vient surtout de ce que le titre fait moins bien autour de l'achat : les dirigeants achètent après une baisse. Il ne vient pas d'un gain sur le marché.

**Version jouable** (1 ligne × 1000 $, SL −10 % / TP +25 % / sortie à 5 séances, frais compris). SPY tenu en permanence rapporte +11,1 $/mois sur la période complète, +11,9 $/mois en test.

| variante | trades | $/mois complet | $/mois test | mois < 0 en test | pire mois |
|---|---|---|---|---|---|
| (a) | 742 | +13,1 $ (+1,3 %) | +16,2 $ (+1,6 %) | 52 % | −674 $ (02/2022) |
| (b) | 581 | +25,8 $ (+2,6 %) | +18,5 $ (+1,9 %) | 46 % | −345 $ (01/2022) |
| (c) | 618 | +4,6 $ (+0,5 %) | −2,7 $ (−0,3 %) | 52 % | −345 $ (08/2019) |

Contrôles post-hoc de la version jouable (IC95 par bootstrap des trades) :
- (b) est la seule significative sur la période complète : +0,80 % par trade [+0,25 ; +1,34], +0,55 % contre SPY sur les mêmes jours [+0,04 ; +1,07]. En test, plus rien de significatif : +0,52 % [−0,36 ; +1,42], et +0,29 % contre SPY [−0,54 ; +1,14] ;
- (a) et (c) : rien de significatif, ni sur la période complète ni en test ;
- le pire trade est réel : Kodiak Sciences (KOD), −80 % en une nuit le 23/02/2022 après l'échec d'un essai clinique. Le stop à −10 % ne protège pas d'un tel gap ;
- la stratégie semaine actuelle fait environ +55 $/mois sur une période de test comparable (rapport ETF inverse), avec un univers choisi a posteriori.

**Conclusion.** L'effet décrit dans les études existe : un titre fait mieux après un achat de dirigeant qu'aux autres dates autour, et le critère figé le confirme. Mais pour la question de Zak, gagner de l'argent sur un horizon d'une semaine avec une ligne, il n'est pas démontré : en test, la meilleure variante fait +18,5 $/mois contre +11,9 $ pour SPY, sans écart significatif, avec un mois sur deux négatif et des gaps de −80 %. **Critère rempli, mais non actionnable.** À ne pas intégrer au bot. À reprendre éventuellement à 20-60 séances, sur un univers plus large, avec des données sans biais de survie (payantes).
