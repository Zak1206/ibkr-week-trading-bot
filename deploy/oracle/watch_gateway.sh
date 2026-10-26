#!/bin/sh
# Si le port API paper (4002) reste ferme, le jeton de 23h45 a rate
# et IBC reste bloque sur "Unrecognized Username or Password".
# On relance le conteneur : login avec le mot de passe, puis le bot.
# Un redemarrage sain dure ~20 s. On n'agit qu'apres 3 minutes coupes,
# et au plus 2 fois par heure.
# Si Docker relance seul le conteneur Gateway, le bot (network_mode
# service:ib-gateway) reste dans l'ancien reseau : ni DNS ni port 4004.
# On redemarre alors le bot, ce qui le rattache au reseau de la Gateway.
# Si le bot tourne mais n'ecrit plus rien depuis 45 min (cycle bloque,
# 23/10 : 17 h sur une requete IBKR sans reponse), on le redemarre aussi,
# avec une alerte Telegram.
# Si IBKR ne repond plus au bot deux cycles de suite alors que le port 4002
# reste ouvert (26/10 : Gateway bloquee 8 h sur "Unrecognized Username or
# Password" apres le reset des serveurs IBKR), on relance la Gateway.
set -eu

DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$DIR/../.." && pwd)
STATE="$DIR/.gateway_watch.state"
LOG="$DIR/gateway_watch.log"
PATH="/usr/local/sbin:/usr/local/bin:/usr/bin:/bin"
export PATH

FAILS=0
LAST_RESTART=0
RESTARTS=0
WINDOW_START=0
if [ -f "$STATE" ]; then
    # shellcheck disable=SC1090
    . "$STATE"
fi

now=$(date +%s)
if [ $((now - LAST_RESTART)) -lt 300 ]; then
    exit 0
fi

port_up() {
    docker exec oracle-ib-gateway-1 bash -c 'echo >/dev/tcp/127.0.0.1/4002' >/dev/null 2>&1
}

bot_running() {
    [ "$(docker inspect -f '{{.State.Running}}' oracle-bot-1 2>/dev/null)" = "true" ]
}

# Meme port 4004, vu depuis chaque conteneur : une difference = reseaux separes.
bot_cut_off() {
    docker exec oracle-ib-gateway-1 bash -c 'echo >/dev/tcp/127.0.0.1/4004' >/dev/null 2>&1 \
        && ! docker exec oracle-bot-1 python -c "import socket; socket.create_connection(('127.0.0.1', 4004), 2).close()" >/dev/null 2>&1
}

# Le bot ecrit au moins une ligne par cycle de 30 min, bourse ouverte ou non.
bot_stalled() {
    [ -z "$(docker logs --since 45m oracle-bot-1 2>&1 | head -c 1)" ]
}

# Rapprochement IBKR en echec sur les 2 derniers cycles termines. Un reset des
# serveurs IBKR (1 a 6 min) n'en fait echouer qu'un.
ibkr_unresponsive() {
    docker logs --since 75m oracle-bot-1 2>&1 | awk '
        /Rapprochement ignore/ { f = 1 }
        /Scan termine en/ { s = f ? s + 1 : 0; f = 0 }
        END { exit !(s >= 2) }'
}

env_val() {
    grep -h "^$1=" "$ROOT/.env" "$ROOT/.env.ibkr_paper" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\r\"'"
}

# Le jeton n'est jamais ecrit dans le journal, seulement le code HTTP.
alert() {
    TOKEN=$(env_val TELEGRAM_BOT_TOKEN)
    CHAT=$(env_val TELEGRAM_CHAT_ID)
    HTTP="pas de token"
    if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
        HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
            --data-urlencode "chat_id=$CHAT" --data-urlencode "text=$1" \
            "https://api.telegram.org/bot$TOKEN/sendMessage" || true)
    fi
    echo "$(date '+%F %T') alerte Telegram: $HTTP" >> "$LOG"
}

write_state() {
    cat > "$STATE" <<EOF
FAILS=$FAILS
LAST_RESTART=$LAST_RESTART
RESTARTS=$RESTARTS
WINDOW_START=$WINDOW_START
EOF
}

# Relance la Gateway (login avec le mot de passe), puis le bot. Au plus 2 fois
# par heure ; ensuite une seule alerte, puis on attend l'heure suivante.
relaunch_gateway() {
    if [ $((now - WINDOW_START)) -gt 3600 ]; then
        RESTARTS=0
        WINDOW_START=$now
    fi
    if [ "$RESTARTS" -ge 2 ]; then
        if [ "$RESTARTS" -eq 2 ]; then
            echo "$(date '+%F %T') 2 relances en 1 h, arret. Login Gateway a verifier." >> "$LOG"
            alert "Gateway IBKR: 2 relances en 1 h sans succes ($1). Login a verifier a la main."
            RESTARTS=3
        fi
        FAILS=0
        write_state
        return 0
    fi
    echo "$(date '+%F %T') relance Gateway (login mot de passe): $1" >> "$LOG"
    # Etat ecrit avant: la relance dure plusieurs minutes et cron relance ce
    # script chaque minute.
    FAILS=0
    LAST_RESTART=$now
    RESTARTS=$((RESTARTS + 1))
    write_state
    cd "$DIR"
    docker compose stop bot >> "$LOG" 2>&1 || true
    docker compose restart ib-gateway >> "$LOG" 2>&1 || true

    i=0
    while [ "$i" -lt 24 ]; do
        if port_up; then
            break
        fi
        i=$((i + 1))
        sleep 10
    done

    docker compose start bot >> "$LOG" 2>&1 || true
    if port_up; then
        echo "$(date '+%F %T') API 4002 de nouveau ouverte" >> "$LOG"
    else
        echo "$(date '+%F %T') API 4002 toujours fermee apres relance" >> "$LOG"
    fi
    alert "Gateway IBKR relancee ($(date '+%d/%m %H:%M')): $1. Verifier que le bot se reconnecte."
}

if port_up; then
    FAILS=0
    if bot_running && bot_cut_off; then
        echo "$(date '+%F %T') bot coupe du reseau Gateway, redemarrage du bot" >> "$LOG"
        cd "$DIR"
        docker compose restart bot >> "$LOG" 2>&1 || true
        LAST_RESTART=$now
    elif bot_running && bot_stalled; then
        echo "$(date '+%F %T') bot fige (aucun log depuis 45 min), redemarrage du bot" >> "$LOG"
        # Etat ecrit avant: l'arret peut durer jusqu'a 300 s (ordre en cours)
        # et cron relance ce script chaque minute.
        LAST_RESTART=$now
        write_state
        cd "$DIR"
        docker compose restart bot >> "$LOG" 2>&1 || true
        alert "Bot fige: aucun log depuis 45 min. Redemarre automatiquement ($(date '+%d/%m %H:%M')). Verifier qu'il se reconnecte a IBKR."
    elif bot_running && ibkr_unresponsive; then
        relaunch_gateway "IBKR ne repond plus au bot depuis 2 cycles, port 4002 ouvert"
    fi
    write_state
    exit 0
fi

FAILS=$((FAILS + 1))
echo "$(date '+%F %T') port 4002 ferme ($FAILS/3)" >> "$LOG"
if [ "$FAILS" -lt 3 ]; then
    write_state
    exit 0
fi

relaunch_gateway "port 4002 ferme depuis 3 min"
write_state
