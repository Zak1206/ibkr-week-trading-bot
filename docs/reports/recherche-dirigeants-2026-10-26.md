# Copier les achats des dirigeants (Form 4) : méthode figée, données bloquées

26/10/2026. Statut : **non commencée**. La SEC refuse le téléchargement.

## Ce qui s'est passé

- Source prévue : les « Insider Transactions Data Sets » trimestriels de la SEC (ZIP de fichiers TSV, depuis 2006).
- User-Agent déclaré : `ibkr-week-trading-bot-research https://github.com/Zak1206/ibkr-week-trading-bot`, sans adresse e-mail.
- Réponse : 403, sur la page (`/dera/data/form-345`) comme sur un ZIP (`2024q1_form345.zip`). La page d'erreur renvoie à la politique « Fair Access » de la SEC.
- Trois requêtes en tout, donc ce n'est pas un problème de débit. La SEC demande un User-Agent qui contienne un nom et une adresse e-mail de contact. Le nôtre n'en a pas.
- Rien n'a été contourné : pas d'autre User-Agent, pas de scraping d'un autre site.

**Pour débloquer, il faut une décision de Zak :** déclarer une adresse e-mail de contact dans le User-Agent. Ça peut être son adresse, ou une adresse créée pour la recherche. Sans son accord explicite, on ne met pas son adresse.

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
