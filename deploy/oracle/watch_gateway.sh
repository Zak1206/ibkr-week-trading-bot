#!/bin/sh
# Si le port API paper (4002) reste ferme, le jeton de 23h45 a rate
# et IBC reste bloque sur "Unrecognized Username or Password".
# On relance le conteneur : login avec le mot de passe, puis le bot.
# Un redemarrage sain dure ~20 s. On n'agit qu'apres 3 minutes coupes,
# et au plus 2 fois par heure.
set -eu

DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
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

write_state() {
    cat > "$STATE" <<EOF
FAILS=$FAILS
LAST_RESTART=$LAST_RESTART
RESTARTS=$RESTARTS
WINDOW_START=$WINDOW_START
EOF
}

if port_up; then
    FAILS=0
    write_state
    exit 0
fi

FAILS=$((FAILS + 1))
echo "$(date '+%F %T') port 4002 ferme ($FAILS/3)" >> "$LOG"
if [ "$FAILS" -lt 3 ]; then
    write_state
    exit 0
fi

if [ $((now - WINDOW_START)) -gt 3600 ]; then
    RESTARTS=0
    WINDOW_START=$now
fi
if [ "$RESTARTS" -ge 2 ]; then
    echo "$(date '+%F %T') 2 relances en 1 h, arret. Login Gateway a verifier." >> "$LOG"
    FAILS=0
    write_state
    exit 0
fi

echo "$(date '+%F %T') relance Gateway (login mot de passe)" >> "$LOG"
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

FAILS=0
LAST_RESTART=$now
RESTARTS=$((RESTARTS + 1))
write_state
