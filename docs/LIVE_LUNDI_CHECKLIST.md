# Checklist — passage LIVE lundi

> Paper cloture (+102 $, 63 trades, 62 % WR). Config live prete dans `.env.ibkr_live`.

---

## Dimanche soir (ou lundi avant 9h30 NY)

### 1. Archiver le paper
```powershell
cd "C:\Trading_Bot"
.\scripts\archive_paper_ibkr.ps1
```
→ Copie journal/positions dans `archive/ibkr_paper_2026-05-23_2026-06-25/`

### 2. IB Gateway — basculer en LIVE
- Fermer la session **paper** si ouverte
- Ouvrir IB Gateway en mode **Live** (pas Paper Trading)
- API : port **4001**, trusted IP `127.0.0.1`
- Verifier connexion API verte, **Client ID libre** (live = `9` dans `.env.ibkr_live`)

### 3. Compte & capital
- [ ] Compte live IBKR actif, fonds disponibles (~1 000 $ prévus)
- [ ] Pas de positions orphelines non voulues chez IBKR
- [ ] `IBKR_ACCOUNT=` vide OK si un seul compte live dans Gateway

---

## Lundi matin (avant ouverture US 9h30 NY / 15h30 Paris)

### 4. Premier lancement live
```powershell
.\scripts\start_bot_live.ps1
```
→ Demande confirmation `oui` (sécurité anti-clic)

**Première fois uniquement** (journal live vide) :
```powershell
.\.venv\Scripts\python.exe main.py --env-file ".env.ibkr_live" --reset-positions --no-portfolio-prompt
```

### 5. Verifications (5 min)
- [ ] Log : `IBKR connecte (live)` — port **4001**, pas 4002
- [ ] Telegram : message démarrage mode IBKR AUTO
- [ ] Cockpit : http://127.0.0.1:8765 — budget 1 000 $, 0 position
- [ ] Aucune instance paper qui tourne en parallèle (sinon Client ID / prix cassés)

### 6. Pendant la séance
- Telegram à chaque clôture TP/SL
- Ne pas changer la config la semaine 1
- En cas de doute : arrêter le bot, pas de vente manuelle sauf urgence

---

## Fichiers live (séparés du paper)

| Fichier | Rôle |
|---------|------|
| `.env.ibkr_live` | Config live |
| `trades_journal_live.jsonl` | Journal live |
| `positions_live.json` | Positions live |
| `scripts/start_bot_live.ps1` | Lancement |

---

## Semaine 1 — règles

- Même stratégie que paper (sniper, SL -2 % / TP +4 %, 5 slots)
- Capital : **1 000 $** — perte max acceptable = budget labo entier
- Pas de 6ᵉ slot, pas de changement TP cette semaine
- Noter chaque écart paper vs live (slippage, refus ordre) dans un fichier perso

---

## En cas de problème

| Problème | Action |
|----------|--------|
| Port 4002 au lieu de 4001 | Gateway encore en paper → rebasculer live |
| Client ID déjà utilisé | Tuer l'autre `python main.py`, redémarrer Gateway |
| Prix IBKR indisponible | Normal si routing pas prêt — Yahoo prend le relais (config live) |
| Ordre PreSubmitted sans fill | Marché fermé ou Gateway pas routé — attendre l'open |

---

*Post LinkedIn V2 publié. Post V3 = jour du live ou après 1 semaine avec chiffres réels.*
