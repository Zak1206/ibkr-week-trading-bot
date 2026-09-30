# Parcours de creation — Trading Bot

> **Usage** : source pour posts LinkedIn, CV, portfolio.  
> **Mise a jour** : ajouter une entree dans *Journal de bord* (plus recent en haut) a chaque jalon notable.  
> **Agents Cursor** : lors d'une session significative (nouvelle feature, migration, bilan perf, bug critique resolu), enrichir ce fichier — ne pas inventer de chiffres : preferer `trades_journal.jsonl`, `archive/*/BILAN.md`, ou mesures verifiees.

---

## Pitch (1 ligne)

Bot Python de trading momentum US : signaux multi-timeframe (15m + 1h), execution auto IBKR paper, risk management, cockpit web live et alertes Telegram — iterer depuis paper eToro vers execution broker reelle.

---

## Stack & competences (CV / LinkedIn)

- **Langage** : Python 3
- **Marche** : actions US (watchlist momentum : HOOD, SMCI, PLTR, IONQ, etc.)
- **Brokers** : eToro (phase 1, manuel) → IBKR Gateway paper (auto)
- **APIs / libs** : ib_insync, Yahoo/Finnhub, Telegram Bot API, Groq (LLM optionnel) + moteur de regles deterministe
- **Data** : journal JSONL, positions JSON, cockpit live JSON
- **UI / ops** : dashboard web local (`cockpit_web.py`), notifications Telegram
- **Risk** : stops ATR, cap TP, drawdown journalier, slots max, rotation swap, flat intraday fin de seance

---

## Timeline (grandes etapes)

| Periode | Phase | Courtier | Resume |
|---------|-------|----------|--------|
| 2026-04-07 → 2026-05-22 | Paper eToro | eToro (manuel) | Signaux bot + confirmations Telegram ; campagne 30j ; archive figee |
| 2026-05-23+ | Paper IBKR | IBKR (auto) | Execution SL/TP chez broker, sync positions, sizing actions entieres |
| 2026-06 (en cours) | Optimisation qualite | IBKR | Moins de churn, pack « profit max », cockpit investi/line, flat bid IBKR, swap budget |

---

## Metriques cles (verifiees)

### Phase eToro (archive `archive/etoro_paper_2026-04-07_2026-05-22/`)

| Metrique | Valeur |
|----------|--------|
| Trades clos | 37 |
| PnL cumule | **+157,61 USD** |
| Win rate | **64,9 %** |
| Gain moyen (win) | +8,40 USD (+5,14 %) |
| Perte moyenne (loss) | -3,38 USD (-2,10 %) |
| Budget ref. | 1 000 USD |

### Phase IBKR paper (journal racine, maj 2026-06-04)

| Metrique | Valeur |
|----------|--------|
| Trades clos | ~38 |
| PnL cumule | **~+86 USD** |
| Win rate | **~66 %** |
| Gain moyen (win) | ~+5,50 USD |
| Perte moyenne (loss) | ~-3,94 USD |
| Frequence | **beaucoup plus elevee** qu'eToro sur periode comparable |

*(Bilan final paper IBKR : voir table provisoire ci-dessous — a finaliser fin de semaine avant Post 2.)*

### Phase IBKR paper — bilan final (maj 2026-06-26, trades dedup)

| Metrique | Valeur |
|----------|--------|
| Periode | 2026-05-23 → 2026-06-25 (cloture phase paper) |
| Trades clos | **63** |
| PnL cumule | **+102,42 USD (+10,2 %)** |
| Win rate | **61,9 %** (39 W / 24 L) |
| Gain moyen (win) | +5,42 USD (+3,41 %) |
| Perte moyenne (loss) | -4,54 USD (-2,91 %) |
| Budget effectif | 1 102,42 USD |
| Report HTML | `docs/reports/v2-ibkr-performance-report.html` |

*(Bilan arrete au 25 juin 2026 — trade en cours non inclus.)*

**Lecon narrative** : WR proche de la V1 (~62 % vs ~65 %), mais IBKR trade **beaucoup plus souvent** (63 vs 37 trades) avec gains moyens plus petits (flat fin de seance, TP ~3,5-4 %) — la perf depend autant de la **frequence** et du **sizing** que du win rate.

---

## Difficultes & rebonds (grandes lignes)

> Garder **synthetique** ici. Le detail technique vit dans le *Journal de bord* ou le code — pas besoin de tout lister pour LinkedIn/CV.

| # | Difficulte | Rebond (en bref) |
|---|------------|------------------|
| 1 | **Signal ≠ execution** — ACHETER valide mais rien ne passe (slots, sizing, swap) | Tracer pourquoi ; achat immediat / swap / logs clairs |
| 2 | **eToro → IBKR** — bon WR mais autre rythme (plus de trades, gains plus petits) | Archive + metriques ; re-regler risk/churn, pas copier la config |
| 3 | **Broker ≠ bot** — TP chez IBKR, pas de notif / etat local en retard | Sync + Telegram explicites ; observabilite |
| 4 | **Regles ≠ fill reel** — flat fin de seance, sizing actions entieres, ecart prix ref / vente | Cockpit lisible ; bid IBKR ; swap budget |

**Fil conducteur** : chaque galerie = *diagnostiquer avec le journal* → *fix cible* → *mesurer avant/apres*.

**Lecon globale** : un bot trading, c'est signal + **execution** + **observabilite** — pas juste un bon indicateur.

*(Exemples concrets si besoin en interview : UNH/Telegram, HOOD flat, PLTR sizing — voir journal de bord.)*

### Templates LinkedIn (optionnel)

> Probleme → ce que j'ai cru → cause → fix → lecon *(1 par grande ligne ci-dessus)*

### A enrichir plus tard (si utile au recit)

- Passage live / capital reel  
- Retour perso « ce que je referais differemment »

---

## Decisions techniques marquantes

1. **Journal JSONL append-only** — trace auditable de chaque signal, fill, swap, cloture (`ibkr_sync`, `intraday_flat`, etc.)
2. **MTF / HTF** — scan 15m + filtre tendance 1h (`trend_score` -2..+2) ; pas « HTF seul » vs « MTF » : c'est deja du multi-timeframe
3. **Execution IBKR** — actions entieres, SL/TP OCA, sync « plus de qty chez IBKR » → cloture locale + alerte Telegram
4. **Swap / rotation** — slots pleins ou budget insuffisant → vendre la ligne la plus faible, acheter le meilleur candidat (garde-fous conf/score/jour)
5. **Achat immediat vs file** — slot libre = achat tout de suite ; slots pleins = classement fin de cycle
6. **Cockpit live** — budget deploye, PnL latent, montant investi par ligne (% budget, qty)
7. **Intraday flat** — fin de seance : securiser petits gains (+0,8 % à +2,5 %), runners >= +2,5 % overnight, pertes conservees (stop IBKR)

---

## Fil narratif LinkedIn (valide)

1. **Probleme initial** : trading sans systeme trace, decisions difficiles a reproduire  
2. **V1 (eToro)** : bot signaux + alertes Telegram ; execution manuelle sur eToro (pas toujours devant le PC) ; consignes du bot suivies a la lettre  
3. **Resultats V1** : 37 trades, **+157 USD (+15,8 %)**, WR **65 %** sur ~6 semaines — resultat solide, strategie validee  
4. **Pivot V2 (IBKR auto)** : pas parce que la V1 echouait — pour **plus de rapidite** (reagir des qu'un signal sort) et **tester la strategie a 100 %** sans etre bloque par la disponibilite  
5. **V2 en cours** : execution auto IBKR + iteration strategie (sniper, anti-chase, flat, rotation) — journal + cockpit  
6. **V3 (a venir)** : passage live, petit capital, meme process  

**A ne pas dire en public** : « j'ai triche sur les regles », « resultats pas extraordinaires », ton trop casual (« ding »), « 50 tickers » (V1 = ~10 actions en watchlist).

---

## Posts LinkedIn (serie 3 — versions courtes)

> Publier V1 maintenant. **Post 2 fin de semaine** apres bilan paper IBKR (report). Post 3 le jour du passage live.  
> Ton : pro, accessible, quelques termes techniques. Pas de promesse de rendement.

### Post 1 — V1 eToro (pret a publier)

J'ai code un bot de trading. Au debut, il ne passait pas les ordres — il analysait le marche et m'alertait sur **Telegram**.

Chaque signal indiquait : **ACHETER** ou **VENDRE**, le ticker, un **stop loss** et un **take profit**. J'executais ensuite sur **eToro**, en suivant les consignes du bot. L'execution restait manuelle parce que je n'etais pas toujours devant mon PC au moment du signal.

Le bot scannait une dizaine d'actions US (TSLA, HOOD, COIN…) toutes les 15 minutes, en croisant tendance **1h**, **RSI** et **MACD**. Chaque trade etait journalise.

**6 semaines de paper trading :**
→ 37 trades  
→ **+157 $ (+15,8 %)** sur 1 000 $  
→ **65 % de win rate**

Un resultat qui m'a confirme que la strategie tenait la route. J'ai decide de passer en **execution automatique** : reagir plus vite et tester la strategie a 100 %, sans dependre de ma disponibilite.

Prochaine etape : IBKR, ordres et stops geres par le bot.

#BuildInPublic #Trading #Python #AlgoTrading

### Post 2 — V2 IBKR auto (**pret a publier — suite Post 1**)

Etape suivante, comme promis : j'ai branche le bot sur **IBKR**.

Des qu'un signal **ACHETER** sort, le bot envoie l'ordre au marche, pose le **stop loss** et le **take profit** chez le broker, et me previent sur **Telegram** quand la ligne se ferme. Plus d'execution manuelle — le bot trade pendant les seances US, que je sois devant l'ecran ou non.

Meme univers (actions US momentum, HOOD, SOFI, SMCI…), scan toutes les 15 minutes, tendance **1h**, **RSI**, **MACD**. Mais en paper IBKR, j'ai aussi serre les regles : entrees plus selectives, stops calibres sur le prix reel de fill, sync broker/bot, cockpit web pour suivre le portefeuille en live.

**5 semaines de paper trading IBKR :**
→ 63 trades  
→ **+102 $ (+10,2 %)** sur 1 000 $  
→ **62 % de win rate**

Win rate proche de la V1 (~65 %), plus de trades, gains moyens un peu plus petits par ligne — normal quand le bot execute seul, 15 min apres 15 min.

La mecanique tient. Prochaine etape : le **live**, petit capital, meme journal.

#BuildInPublic #Trading #Python #AlgoTrading

---

**Checklist avant publication Post 2**
- [x] Generer bilan paper (`trades_journal.jsonl` → metriques finales dedup)
- [x] Remplacer les `[X]` dans le post
- [x] Report HTML V2
- [ ] Screenshot du dashboard pour la carousel LinkedIn
- [ ] Verifier qu'aucun chiffre ne contredit le journal

### Post 3 — Live (brouillon — remplir au jour J)

V1 : signaux + eToro manuel. V2 : auto IBKR paper.  
Aujourd'hui : **meme bot, argent reel.**

[X] semaines de paper, [X] trades, journal complet. Je sais expliquer chaque entree et chaque sortie.

Capital de depart : **[X] $** — montant que je peux perdre sans impact. Meme config, memes garde-fous (SL -2 %, TP +4 %, max 5 positions).

Le code ne change pas. Ce qui change : le slippage est reel, et chaque erreur a un cout.

Je continue a documenter la journey ici — process auditable, pas promesse de rendement.

#BuildInPublic #LiveTrading #IBKR

---

## Bullets CV (maj 2026-08 — dernieres modifs)

- Concu un bot Python bout-en-bout : scan MTF (15m + 1h), sniper/breakout, journal JSONL, cockpit web + Telegram
- Integre IBKR (ib_insync) : auto paper, midprice, SL/TP OCA, sync broker, alertes de cloture
- Risk/exits iteres : tickets ~400–520 USD (frais), 2 slots + themes, flat fin de seance en **limite** (plancher de gain), runners overnight, DD journalier
- Paper verifie : eToro +157 USD / +15,8 % (37 trades, WR 65 %) → IBKR +128 USD cumules (73 clos, WR 60 %)

---

## Journal de bord

*(Plus recent en haut — format libre : date, contexte, changement, chiffre, lecon)*

### 2026-06-04 — Flat intraday + swap budget + cockpit + recit

- **Flat fin de seance** : poll ~1 s (vs ~3 s), verification **bid IBKR** avant vente — evite vendre a +0,49 % quand seuil = +0,8 % (cas HOOD)
- **Swap budget** : si cash insuffisant pour 1 action mais signal ACHETER valide → rotation (plus de `Decision ACHETER ignoree` silencieuse)
- **Cockpit** : colonne **Investi** (USD, qty, % budget) ; build `2026-06-04-investi`
- **Telegram clotures IBKR** : alerte TP/SL explicite + sync inter-cycle ~45 s
- **Discussion MTF/HTF/TP** : cap TP 3,5 % volontaire ; comparaison eToro gains ~5 % vs IBKR ~3,8 % ; piste 4,5 % + recalage TP au fill
- **PnL IBKR cumule** ~+86 USD, WR ~66 % (38 trades)

### 2026-06-03 / 04 — UNH, notifications, deploy budget

- UNH TP +3 % chez IBKR journalise mais Telegram absent (sync silencieux) → fix alertes `POSITION FERMEE`
- Rebuy UNH 2 min apres TP a prix plus haut → lecon : cooldown re-entry a envisager
- 4/6 slots mais ~99 % budget deploye (actions entieres UNH/GE ~300-400 USD)

### 2026-05-23 — Transition IBKR

- Archive eToro figee (`archive/etoro_paper_2026-04-07_2026-05-22/`, BILAN +157 USD)
- Repart journal/positions vides ; profil `.env.ibkr_paper`

### 2026-04-07 — Demarrage paper eToro

- Budget 1 000 USD, horizon campagne 30j, signaux LLM + regles + alertes Telegram
- Watchlist ~10 tickers (override archive : TSLA, NVDA, COIN, OXY, FCX, HOOD)
- Execution manuelle sur eToro : consignes bot suivies ; contrainte = pas toujours devant le PC au signal
- Bilan : +157 USD (+15,8 %), WR 65 %, 37 trades → strategie validee, pivot auto pour rapidite + test 100 %

---

## A completer plus tard

- [ ] Screenshot cockpit / Telegram (avec floutage si live)
- [ ] Schema architecture (scan → decision → IBKR → journal → cockpit)
- [ ] Date passage live / capital reel (si applicable)
- [ ] Retour d'experience « 3 choses que je ne referais pas » → voir *Difficultes a documenter plus tard*

---

## Fichiers utiles pour reconstruire l'histoire

| Fichier | Contenu |
|---------|---------|
| `archive/etoro_paper_2026-04-07_2026-05-22/BILAN.md` | Bilan phase eToro |
| `trades_journal.jsonl` | Historique complet IBKR |
| `.env` / `.env.ibkr_paper` | Config au fil du temps |
| `docs/PARCOURS_CREATION_BOT.md` | Ce document |
| Transcripts Cursor | Sessions agent (decisions, debug) |
