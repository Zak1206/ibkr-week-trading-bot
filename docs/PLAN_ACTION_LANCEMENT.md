# Plan d'action — Lancer le bot (paper → live)

> Guide pas à pas. Chaque étape est taguée : **code** · **ops** · **juridique** · **acquisition** · **finance**  
> Tu peux avancer dans l'ordre : une phase mélange volontairement les sujets pour éviter de tout faire d'un bloc.

---

## Vue d'ensemble

```
Phase 1 — Paper solide     →  le bot tourne sans surprise
Phase 2 — Observabilité    →  tu vois tout, tu comprends tout
Phase 3 — Infra 24/7       →  plus besoin de laisser ton PC allumé
Phase 4 — Cadre perso      →  tu trades en connaissance de cause
Phase 5 — Live capital     →  vrai argent, petit pas
Phase 6 — (Optionnel)      →  image, audience, monétisation
```

**Budget indicatif**

| Phase | Coût |
|-------|------|
| 1 → 2 | 0 € |
| 3 (PC local) | 0 € |
| 3 (VPS Windows + IB Gateway) | ~10–15 €/mois |
| 4 | 0 € (auto-éducation) · avocat/fiscaliste si besoin : sur devis |
| 5 | ton capital (ex. 1 000 $) · commissions IBKR US ~0 $ |
| 6 | 0 € (organique) |

---

## Phase 1 — Paper solide

**Objectif** : 4–6 semaines de paper IBKR sans bug critique, config figée.

### Étapes

**1.1** · **code** — Fixer la config paper dans `.env` + `.env.ibkr_paper`  
- `IBKR_ENABLED=1`, `IBKR_AUTO_EXECUTE=1`, port `4002`  
- Budget `TRADING_BUDGET_USD=1000`, `TRADING_MAX_OPEN_POSITIONS=5`  
- SL/TP au fill (`recalc_sl_tp_at_fill` déjà en place)  
- Cooldown re-entry à `0` ou valeur assumée  

**1.2** · **ops** — Routine quotidienne (15 min)  
- Ouvrir IB Gateway → paper connecté  
- Lancer le bot (`scripts/start_bot.ps1` ou `python main.py`)  
- Vérifier `cockpit_live.json` : slots, PnL, prix live  

**1.3** · **code** — Corriger le journal `signal_buy` manquant  
- Logger `signal_buy` **avant** l'ordre IBKR  
- Ajouter `decision_source: rules-buy | llm` dans le journal  

**1.4** · **finance** — Noter chaque semaine dans un fichier perso  
- PnL réalisé (`trades_journal.jsonl` → event `trade_closed`)  
- Win rate, gain moyen, perte moyenne  
- Pas de décision live avant **≥ 30 trades** paper stables  

**1.5** · **juridique** — Lecture minimum (1 h)  
- Conditions IBKR paper vs live  
- Différence compte **personnel** vs **pro** (si un jour tu gères de l'argent tiers → autre monde)  

**✓ Phase terminée quand** : aucun désync IBKR non expliqué, journal complet, tu sais pourquoi chaque trade est entré/sorti.

---

## Phase 2 — Observabilité

**Objectif** : ne plus découvrir un stop ou un achat « par surprise ».

### Étapes

**2.1** · **code** — Telegram fiable  
- `TELEGRAM_NOTIFY_IBKR_CLOSE=1`  
- Vérifier alerte à chaque `trade_closed` (TP, SL, flat)  

**2.2** · **code** — Cockpit  
- `python cockpit_web.py` en local pendant les séances US  
- Colonnes : investi, dist SL/TP, confiance entrée  

**2.3** · **ops** — Bilan hebdo automatisé  
- Commande rapide PnL (voir `docs/PARCOURS_CREATION_BOT.md`)  
- Comparer avec l'écran IBKR (source de vérité exécution)  

**2.4** · **acquisition** — Première trace publique (optionnel, 30 min)  
- Brouillon LinkedIn **privé** : « je paper-trade un bot Python + IBKR »  
- Pas de promesse de rendement, pas de screenshot avec solde  

**2.5** · **code** — Durcir le sniper si trop de stops « bruit »  
- Tester `BUY_FIXED_STOP_PCT=2.5` ou `BUY_SNIPER_BREAKOUT_REQUIRED=1`  
- **Une** variable à la fois, noter l'impact 2 semaines  

**✓ Phase terminée quand** : tu reçois Telegram pour chaque clôture et tu peux expliquer chaque perte en une phrase.

---

## Phase 3 — Infra 24/7

**Objectif** : le bot tourne aux heures US sans ton PC.

### Étapes

**3.1** · **ops** — Choisir l'hébergement  

| Choix | Coût | IB Gateway |
|-------|------|------------|
| Ton PC allumé en RDP | 0 € | Facile |
| VPS Windows (Contabo) | ~10–15 €/mois | Facile — voir `CONTABO_DEPLOY.md` |
| VPS Linux + bot seul | ~5–7 €/mois | IB Gateway sur Linux = galère |
| Oracle Always Free | 0 € | Linux, même contrainte |

**Recommandation** : VPS **Windows** si tu veux IB Gateway sur la même machine.

**3.2** · **code** — Déployer sur le VPS  
- Copier le projet (sans `.venv`)  
- Recréer venv, `pip install -r requirements.txt`  
- Copier `.env` et `.env.ibkr_paper` (secrets **jamais** sur GitHub)  

**3.3** · **ops** — IB Gateway en service  
- Install `installers/ibgateway-latest-standalone-windows-x64.exe`  
- API port `4002`, trusted IP `127.0.0.1`  
- Tâche planifiée Windows : relancer Gateway + bot au boot  

**3.4** · **juridique** — Données & sécurité  
- VPS : mot de passe fort, RDP restreint à ton IP si possible  
- Pas de tokens Telegram / Groq dans un repo public  
- Journal = donnée sensible (historique trades)  

**3.5** · **finance** — Budget infra  
- ~15 €/mois VPS + 0–5 €/mois Groq si usage LLM élevé  
- Paper = 0 € de capital à risque  

**✓ Phase terminée quand** : une semaine complète sans intervention manuelle pendant les heures US.

---

## Phase 4 — Cadre personnel (avant le live)

**Objectif** : savoir ce que tu fais légalement et fiscalement **pour toi-même**.

> Ce n'est pas un avis juridique. Pour ton cas (France / autre pays), un fiscaliste reste la bonne finition.

### Étapes

**4.1** · **juridique** — Clarifier le statut  
- Trading **compte perso IBKR** = investissement individuel (le plus simple)  
- Tu ne **vends pas** de signaux à d'autres → pas de régulation PSI/DASP pour l'instant  
- Si un jour tu gères l'argent d'autrui → stop, autre cadre (CIF, société, etc.)  

**4.2** · **juridique** — Fiscalité (France, indicatif)  
- Plus-values mobilières : régime au choix (PFU 30 % ou barème + abattement durée selon ancienneté du portefeuille titres)  
- Tenir un **tableau Excel** : date, ticker, entrée, sortie, PnL, frais  
- Le journal JSONL t'aide déjà — export mensuel  

**4.3** · **finance** — Règle du capital  
- Ne passer live qu'avec de l'argent dont la **perte totale** ne change pas ta vie  
- Ex. 1 000 $ OK si c'est ton budget « labo » ; pas l'épargne d'urgence  

**4.4** · **acquisition** — Storytelling (optionnel)  
- Mettre à jour `docs/PARCOURS_CREATION_BOT.md` : métriques vérifiées uniquement  
- Préparer 3 bullets CV (Python, IBKR, risk) — pas de chiffres inventés  

**4.5** · **code** — Checklist technique pre-live  
- [ ] `IBKR_PORT` live (souvent `4001` / `7496`) dans un `.env.ibkr_live` séparé  
- [ ] `IBKR_STARTUP_REQUIRED=1` pour refuser de trader si Gateway down  
- [ ] Plafond `RISK_DAILY_DD_LIMIT_PCT` testé en paper  
- [ ] Watchlist validée (prix action compatible budget)  

**✓ Phase terminée quand** : tu as répondu par écrit à « combien je peux perdre » et « comment je déclare ».

---

## Phase 5 — Live capital (petit pas)

**Objectif** : premier mois live avec le **même** bot, capital réduit.

### Étapes

**5.1** · **finance** — Ouvrir / activer IBKR live  
- Compte réel, virement minimal  
- Commencer à **300–500 $** ou garder 1 000 $ si c'est déjà prévu  

**5.2** · **code** — Fichier env dédié  
- Dupliquer `.env.ibkr_paper` → `.env.ibkr_live`  
- `IBKR_PORT=4001` (live), compte live dans `IBKR_ACCOUNT`  
- **Ne jamais** mélanger paper et live sur le même `CLIENT_ID` en parallèle  

**5.3** · **ops** — Semaine 1 live en mode prudent  
- `TRADING_MAX_OPEN_POSITIONS=3`  
- `RISK_DAILY_DD_LIMIT_PCT=2`  
- Pas de changement de stratégie la même semaine  

**5.4** · **code** — Comparer paper vs live  
- Slippage, refus d'ordre, SL exécuté au bon prix  
- Noter les écarts dans le journal de bord  

**5.5** · **juridique** — Archivage  
- Export mensuel `trades_journal.jsonl` + relevé IBKR  
- Conservation 6 ans (pratique courante en France pour justificatifs)  

**5.6** · **acquisition** — Post LinkedIn (optionnel, après 1 mois)  
- Format : problème → solution → leçon → **chiffres réels**  
- Jamais : « garanti », « rendement X % »  

**✓ Phase terminée quand** : 1 mois live, drawdown max connu, process fiscal documenté.

---

## Phase 6 — (Optionnel) Audience & monétisation

**Objectif** : capitaliser sur le projet sans vendre du « conseil en investissement ».

### Étapes

**6.1** · **acquisition** — Contenu éducatif  
- Série LinkedIn : architecture bot, journal JSONL, erreurs IBKR  
- GitHub public **sans** `.env`, sans clés  

**6.2** · **juridique** — Ligne rouge  
- OK : « voici mon outil perso », tutoriel technique, portfolio dev  
- Risqué : vendre des signaux, promettre des gains, gérer l'argent des autres  

**6.3** · **acquisition** — Monétisation soft (si un jour)  
- Formation / mentoring **code & infra** (pas « achète ce ticker »)  
- SaaS cockpit pour traders perso = autre projet (+ juridique)  

**6.4** · **code** — Repo portfolio  
- README clair, schéma scan → rules → IBKR → journal  
- Screenshot cockpit flouté  

**✓ Phase terminée quand** : tu as une présence cohérente « dev / automation », pas « guru trading ».

---

## Ordre recommandé (résumé une page)

| # | Faire quoi | Tag |
|---|------------|-----|
| 1 | Config paper stable + routine quotidienne | code, ops |
| 2 | Fix journal + Telegram clôtures | code |
| 3 | 30+ trades paper documentés | finance |
| 4 | Cockpit + bilan hebdo | code, ops |
| 5 | (Option) brouillon LinkedIn privé | acquisition |
| 6 | VPS Windows + Gateway 24/7 | ops, code |
| 7 | Lire fiscalité perso + règle du capital | juridique, finance |
| 8 | `.env.ibkr_live` séparé | code |
| 9 | Live petit capital, 3 slots max | finance, ops |
| 10 | Export mensuel + archive | juridique |
| 11 | (Option) post public avec chiffres réels | acquisition |

---

## Fichiers du projet

| Besoin | Fichier |
|--------|---------|
| Ce plan | `docs/PLAN_ACTION_LANCEMENT.md` |
| Récit CV / LinkedIn | `docs/PARCOURS_CREATION_BOT.md` |
| VPS Windows | `CONTABO_DEPLOY.md` |
| IB Gateway | `IBKR_SETUP.md` · `installers/INSTALL_IB_GATEWAY.md` |
| Vérité PnL | `trades_journal.jsonl` |

---

*Dernière mise à jour : 2026-06-02 — ajuster les seuils (trades, capital) selon tes résultats paper.*
