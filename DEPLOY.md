# Déploiement du bot (serveur)

Ce guide vise à faire tourner le bot **24h/24** sur une machine Linux (VPS), avec redémarrage auto. Les alertes Telegram et le journal (`trades_journal.jsonl`) restent le même principe qu’en local.

## Option recommandée : Oracle Cloud (Always Free)

Oracle propose une **VM gratuite** (souvent 1 vCPU / 1 Go RAM) suffisante pour un script Python qui poll Telegram et appelle des APIs. Limite : inscription carte bancaire, quotas régionaux.

1. Crée un compte Oracle Cloud, crée une **VM Ubuntu 22.04** (shape Always Free).
2. Ouvre le port **SSH 22** dans les *Network Security Groups* / pare-feu (pas besoin d’ouvrir de port pour Telegram : le bot utilise des connexions sortantes).
3. Connecte-toi en SSH : `ssh ubuntu@IP_DU_SERVEUR`
4. Installe Python et git (si besoin) :

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
```

5. Copie le projet sur le serveur (git clone, ou `scp -r` depuis ton PC).
6. Dans le dossier du projet :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

7. Crée un fichier `.env` sur le serveur (même variables qu’en local : `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, etc.). Ne commite jamais le `.env`.

8. Test manuel :

```bash
source .venv/bin/activate
python main.py --budget-usd 1000 --horizon-days 7
```

9. Service **systemd** pour relance au boot et en cas de crash. Crée `/etc/systemd/system/trading-bot.service` :

```ini
[Unit]
Description=Trading Bot Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Trading_Bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/ubuntu/Trading_Bot/.venv/bin/python /home/ubuntu/Trading_Bot/main.py --budget-usd 1000 --horizon-days 7
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Adapte `User`, `WorkingDirectory` et `ExecStart` à ton chemin réel.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot.service
sudo systemctl status trading-bot.service
```

Logs :

```bash
journalctl -u trading-bot.service -f
```

## Fichiers à préserver sur le serveur

- `.env` (secrets)
- `positions.json` (état des positions)
- `trades_journal.jsonl` (historique pour `/report` et garde-fous)
- `archive/` (bilans des phases paper terminees, ex. eToro)
- `premarket_prep_state.json`, `fundamental_cache.json`, etc. si tu les utilises

Pense aux **sauvegardes** (copie periodique ou sync).

## Changer de phase paper (ex. eToro → IBKR)

1. Copier `positions.json`, `trades_journal.jsonl`, `flash_state.json` dans `archive/<nom_phase>/` + un `BILAN.md`.
2. Mettre `TRADING_CAMPAIGN_ENABLED=0`, nouvelle `TRADING_OBJECTIVE_START_DATE`, `TRADING_BROKER=ibkr`.
3. Repartir avec journal vide et `positions.json` vide (ou `--reset-positions` au premier lancement).

Lancement typique IBKR paper (Windows, venv) :

```powershell
.\.venv\Scripts\python.exe main.py --budget-usd 1000 --horizon-days 7 --no-portfolio-prompt
```

Profil optionnel : `--env-file ".env.ibkr_paper"`.

## Autres hébergeurs

- **VPS low-cost** (Hetzner, OVH, etc.) : souvent 4–5 €/mois, simple et stable.
- **Render / Railway / Replit (gratuit)** : souvent **sommeil** ou quotas ; peu adapté à un bot qui doit tourner en continu sans coupure.
- **GitHub Actions (cron)** : possible pour un scan toutes les X minutes, mais pas idéal pour le mode “flash” / polling Telegram rapide.

## Sécurité

- Ne publie jamais `TELEGRAM_BOT_TOKEN` ni `GROQ_API_KEY`.
- Restreins l’accès SSH (clés, pas mot de passe faible).
- Par défaut l’exécution est **manuelle** (`/confirm_buy`, `/confirm_sell`). Pour **IBKR auto** : IB Gateway/TWS ouvert + `IBKR_ENABLED=1` et `IBKR_AUTO_EXECUTE=1` (voir `.env.ibkr_paper`).

## Commande Telegram `/report`

Une fois le bot en ligne, envoie **`/report`** dans le chat configuré (`TELEGRAM_CHAT_ID`) pour un résumé PnL réalisé (journal), win rate approximatif et PnL latent estimé sur les lignes ouvertes.
