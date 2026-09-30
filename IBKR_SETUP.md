# IBKR — execution automatique

Le bot peut envoyer des **ordres au marché** via **IB Gateway** ou **TWS** (API locale).

## Prérequis

1. Compte IBKR + **IB Gateway** (ou TWS) installé sur le même PC que le bot.
2. API activée : *Configuration → API → Settings*  
   - Cocher **Enable ActiveX and Socket Clients**  
   - Port socket = celui du `.env` (paper **4002**, live **4001**)
3. Mode **Paper Trading** pour les tests.
4. Login Gateway paper : ton identifiant IBKR paper (non stocké dans le dépôt).

## Activer l’auto

Dans `.env` (ou au lancement) :

```env
IBKR_ENABLED=1
IBKR_AUTO_EXECUTE=1
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_CLIENT_ID=8
```

**Anti-interference (obligatoire)**

Dans IB Gateway → *Configure → Settings → API* :
1. **Master API client ID** = `8` (même valeur que `IBKR_CLIENT_ID`)
2. Un seul bot connecté — ferme les autres scripts / onglets Client 10, 30, etc.
3. Le bot tague ses ordres (`IBKR_ORDER_REF=TradingBot`) et annule les ventes étrangères (`IBKR_GUARD_FOREIGN_ORDERS=1`)

Sans Master client ID, un autre `clientId` peut vendre tes positions (ex. COIN liquidé par client 10).

Ou profil dédié :

```powershell
.\.venv\Scripts\python.exe main.py --env-file ".env.ibkr_paper" --no-portfolio-prompt
```

## Comportement

| Signal | Action |
|--------|--------|
| ACHETER (cycle LLM) | Achat marché USD → position confirmée auto |
| VENDRE | Vente totale de la ligne IBKR |
| Stop loss / Take profit | Vente auto quand le prix touche le niveau |
| FLASH achat / vente pic | Idem si filtres OK |

- **Garde-fous inchangés** (confiance, MACD, RR, drawdown journalier, etc.).
- **Sizing intelligent** : pas de plafond fixe par défaut (`IBKR_MAX_NOTIONAL_USD=0`). Le montant vient du moteur dynamique (budget ~1000 USD, max 2 lignes, ATR, confiance, cash restant).
- **SL / TP** : après chaque achat, le bot pose un **stop** et un **take profit** chez IBKR (`IBKR_PLACE_SL_TP=1`, ordres GTC en OCA). Si invalide, repli sur alertes locales.
- Si l’ordre IBKR échoue et `IBKR_AUTO_FALLBACK_MANUAL=1` : retour au mode `/confirm_buy` / `/confirm_sell`.
- `IBKR_MAX_NOTIONAL_USD` : optionnel uniquement si tu veux un plafond manuel (> 0).
- `IBKR_REQUIRE_MARKET_OPEN=1` : pas d’ordre hors séance US.

## Sécurité

- Commence en **paper** (`IBKR_PORT=4002`).
- Ne passe en live (`4001`) qu’après validation.
- Garde Telegram : chaque fill envoie un message *ACHAT/VENTE AUTO IBKR*.

## Dépannage

| Problème | Piste |
|----------|--------|
| Connexion refusée | Gateway ouvert ? Bon port ? `clientId` unique ? |
| Ordre non rempli | Marché fermé ? Liquidité ? Augmenter `IBKR_FILL_TIMEOUT_SEC` |
| Quantité 0 | `notional` trop petit vs prix de l’action |
