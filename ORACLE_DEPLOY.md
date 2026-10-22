# Déploiement gratuit : Oracle Cloud Always Free (ARM) — bot + IB Gateway paper

Le bot et IB Gateway tournent 24/7 dans Docker sur une VM gratuite, sans ton PC.
Fichiers : `deploy/oracle/`.

## 1. Créer le compte et la VM

1. [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) : créer un compte (carte demandée pour vérification, rien n'est débité).
2. **Région d'origine** : elle est définitive, et la VM gratuite ne peut être créée que là. Prendre une région européenne (Frankfurt, Paris, Amsterdam…).
3. Compute → Instances → **Create instance** :
   - Image : **Ubuntu 24.04** (pas « Minimal »)
   - Shape : **VM.Standard.A1.Flex**, **2 OCPU / 12 GB** (le maximum gratuit depuis juin 2026)
   - SSH : télécharger la clé privée (ou coller ta clé publique)
4. Message « Out of host capacity » : réessayer plus tard ou essayer un autre « availability domain ». C'est fréquent.
5. Conseillé : passer le compte en **Pay As You Go** (toujours 0 € si on reste dans le gratuit). Sinon Oracle peut récupérer une VM jugée inactive sur 7 jours.

Ne rien ouvrir d'autre que le port 22 dans la « Security List ».

## 2. Préparer la VM

Depuis ton PC :

```powershell
ssh -i C:\chemin\cle.key ubuntu@IP_DE_LA_VM
```

Sur la VM :

```sh
git clone https://github.com/Zak1206/ibkr-week-trading-bot.git Trading_Bot
cd Trading_Bot
sudo sh deploy/oracle/setup_vm.sh
exit
```

Se reconnecter en SSH (groupe docker).

## 3. Envoyer les fichiers privés

Ils sont gitignorés, donc absents du clone. Depuis ton PC, dans `C:\Trading_Bot` :

```powershell
scp -i C:\chemin\cle.key .env .env.ibkr_paper positions.json trades_journal.jsonl swap_pending.json ubuntu@IP_DE_LA_VM:~/Trading_Bot/
```

Puis les identifiants Gateway, sur la VM :

```sh
cd ~/Trading_Bot/deploy/oracle
cp gateway.env.example .env
nano .env        # TWS_USERID / TWS_PASSWORD du compte paper
chmod 600 .env ../../.env ../../.env.ibkr_paper
```

## 4. Arrêter le bot sur le PC

Un seul bot à la fois : sinon conflit Telegram (409) et conflit de client ID 8 chez IBKR.
Fermer `main.py` et Gateway sur Windows, laisser la tâche `LaunchTradingBots1430` désactivée.

## 5. Lancer

```sh
cd ~/Trading_Bot/deploy/oracle
docker compose up -d --build
docker compose logs -f bot
```

Gateway met environ 1 minute à se connecter ; le bot attend qu'il réponde sur 4002.

## 6. Cockpit et écran Gateway depuis le PC

Rien n'est exposé sur internet. Ouvrir un tunnel :

```powershell
ssh -i C:\chemin\cle.key -L 8765:127.0.0.1:8765 -L 5900:127.0.0.1:5900 ubuntu@IP_DE_LA_VM
```

- Cockpit : http://127.0.0.1:8765
- Écran Gateway (si `VNC_SERVER_PASSWORD` est rempli) : client VNC sur `127.0.0.1:5900`

## Commandes utiles

```sh
docker compose ps                 # état
docker compose logs -f bot        # logs du bot
docker compose logs -f ib-gateway # logs Gateway / IBC
docker compose restart bot        # redémarrer le bot
docker compose down               # tout arrêter
git pull && docker compose up -d --build   # mise à jour du code
```

`watch_gateway.sh` tourne chaque minute (cron de l'utilisateur `ubuntu`). Si le port API 4002 reste fermé 3 minutes, il relance le conteneur Gateway pour refaire un login avec le mot de passe, puis redémarre le bot. Un redémarrage sain de 23h45 (~20 s) ne le déclenche pas. Au plus 2 relances par heure. Il vérifie aussi que le bot voit le port 4004 de la Gateway : si Docker a relancé le conteneur Gateway tout seul (vu le 19/10 à 23h45), le bot reste dans l'ancien réseau (plus de DNS, plus d'IBKR, plus de Telegram) et il le redémarre. Journal : `deploy/oracle/gateway_watch.log`.

`boot_report.sh` (cron `@reboot` de l'utilisateur `ubuntu`) envoie un message Telegram 7 minutes après chaque redémarrage de la VM : état des conteneurs, port API 4002, bot connecté ou non. Sans lui, une Gateway qui ne revient pas passerait inaperçue, puisque le bot n'envoie son message de démarrage qu'une fois connecté. Journal : `deploy/oracle/boot_report.log`.

```sh
(crontab -l; echo "@reboot /home/ubuntu/Trading_Bot/deploy/oracle/boot_report.sh") | crontab -
```

L'état (`positions.json`, `trades_journal.jsonl`…) vit dans `~/Trading_Bot` sur la VM. Pour récupérer le journal sur le PC :

```powershell
scp -i C:\chemin\cle.key ubuntu@IP_DE_LA_VM:~/Trading_Bot/trades_journal.jsonl .
```

## Ce qui ne change pas

- Même profil `.env.ibkr_paper`, même stratégie, même client ID 8. Seule surcharge serveur : le cockpit écoute dans le conteneur (`deploy/oracle/server.overrides`).
- Gateway redémarre tout seul chaque soir à 23h45 (IBC). Le compte paper n'a normalement pas de double authentification. Si IBKR en demande une, la valider sur le téléphone.
- Profil live : pas prévu ici. Ne pas mélanger avec le paper.
