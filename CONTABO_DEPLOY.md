# Deploiement Contabo (VPS Windows) — Bot + IB Gateway

Objectif : bot + IB Gateway 24/7 sans ton PC.

## 1. Commander le VPS

1. [contabo.com](https://contabo.com) → **VPS** → **Cloud VPS**
2. Choisis **Windows Server** (pas Linux)
3. Config minimale OK : **4 vCPU / 8 GB RAM** (ou le moins cher en Windows si dispo)
4. Region : **EU** (proche de toi)
5. Note l’**IP**, le **login admin** et le **mot de passe** (mail Contabo)

## 2. Connexion (Bureau a distance)

Sur ton PC Windows :

1. `Win + R` → `mstsc` → Entree
2. **Ordinateur** : IP du VPS
3. Utilisateur : `Administrator` (ou celui fourni)
4. Mot de passe Contabo

Tu es sur un Windows distant (comme ton PC).

## 3. Installer les outils

PowerShell **admin** sur le VPS :

```powershell
# Python 3.12
winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements

# Git (optionnel, pour clone)
winget install Git.Git --accept-package-agreements --accept-source-agreements
```

Ferme/rouvre PowerShell, puis :

```powershell
cd C:\
mkdir Trading_Bot
cd Trading_Bot
```

**Copie le projet** depuis ton PC (OneDrive) :
- Option A : `git clone` si repo GitHub prive
- Option B : zip `Trading_Bot` sur ton PC → envoyer via RDP (copier-coller dans la session) ou Google Drive
- Option C : `scp` depuis ton PC

Fichiers **obligatoires** sur le VPS :
- tout le dossier sauf `.venv` (on recree)
- `.env` et `.env.ibkr_paper` (secrets)
- `installers\ibgateway-latest-standalone-windows-x64.exe`

```powershell
cd C:\Trading_Bot
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## 4. IB Gateway (paper)

1. Installe : `installers\ibgateway-latest-standalone-windows-x64.exe`
2. Lance **IB Gateway**
3. Login : **IB API** + **Trading simule**
4. **Configure → Settings → API** :
   - Port **4002**
   - Socket clients : ON
   - Read-Only : OFF
   - Trusted IP : `127.0.0.1`
5. Laisse Gateway **connecte**

## 5. Config bot

Verifie `C:\Trading_Bot\.env.ibkr_paper` :

```env
IBKR_ENABLED=1
IBKR_AUTO_EXECUTE=1
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_CLIENT_ID=7
PREMARKET_PREP_ENABLED=0
```

Test manuel :

```powershell
cd C:\Trading_Bot
.\.venv\Scripts\python.exe main.py --env-file ".env.ibkr_paper" --no-portfolio-prompt
```

Tu dois voir : `[IBKR] IBKR connecte (paper)...`

## 6. Lancer au demarrage (24/7)

### A. IB Gateway au login Windows

1. Raccourci IB Gateway → clic droit → **Copier**
2. `Win + R` → `shell:startup` → coller le raccourci
3. Gateway s’ouvre a chaque reboot (re-login IBKR manuel si session expire)

### B. Bot en tache planifiee

PowerShell admin :

```powershell
$action = New-ScheduledTaskAction -Execute "C:\Trading_Bot\.venv\Scripts\python.exe" -Argument "main.py --env-file .env.ibkr_paper --no-portfolio-prompt" -WorkingDirectory "C:\Trading_Bot"
$trigger = New-ScheduledTaskTrigger -AtStartup -Delay (New-TimeSpan -Minutes 2)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "TradingBot" -Action $action -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest
```

**Ordre** : Gateway doit etre connecte **avant** le bot (delay 2 min au boot).

### C. Relance manuelle rapide

```powershell
cd C:\Trading_Bot
.\scripts\start_bot.ps1
```

## 7. Securite VPS

- Change le mot de passe admin apres 1ere connexion
- **Pare-feu Windows** : pas besoin d’ouvrir le port 4002 vers Internet (bot + Gateway en local `127.0.0.1`)
- Ne commite **jamais** `.env` sur GitHub
- Sauvegarde : `positions.json`, `trades_journal.jsonl`, `.env`

## 8. Telegram depuis le VPS

Meme `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` dans `.env` → `/status` et `/report` fonctionnent comme en local.

## Depannage

| Probleme | Piste |
|----------|--------|
| Bot ne connecte pas IBKR | Gateway ouvert ? Paper + port 4002 ? |
| Gateway deconnecte apres reboot | Re-login IBKR ; envisager IB Key / session persistante |
| Bot arrete | Gestionnaire des taches → TradingBot → Historique |
| Marche ferme | Normal — scan reprend a l’open US |

## Cout

~**8–12 €/mois** selon offre Windows Contabo (verifier prix actuel sur le site).
