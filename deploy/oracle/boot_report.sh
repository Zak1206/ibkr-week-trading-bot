#!/bin/sh
# Rapport Telegram apres un redemarrage de la VM (cron @reboot de l'utilisateur ubuntu).
# Laisse le temps a Gateway (~1 min) et au bot de demarrer, puis dit si tout tourne.
# Sans ce message, une Gateway qui ne revient pas ne se voit pas: le bot n'envoie
# son message de demarrage qu'une fois connecte.
set -u

DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$DIR/../.." && pwd)
PATH="/usr/local/sbin:/usr/local/bin:/usr/bin:/bin"
export PATH

sleep "${BOOT_REPORT_DELAY_SEC:-420}"

env_val() {
    grep -h "^$1=" "$ROOT/.env" "$ROOT/.env.ibkr_paper" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\r\"'"
}
TOKEN=$(env_val TELEGRAM_BOT_TOKEN)
CHAT=$(env_val TELEGRAM_CHAT_ID)

cd "$DIR"
STATUS=$(docker compose ps --format '{{.Name}}: {{.Status}}' 2>&1)
if docker exec oracle-ib-gateway-1 bash -c 'echo >/dev/tcp/127.0.0.1/4002' >/dev/null 2>&1; then
    API="API IBKR 4002: ouverte"
else
    API="API IBKR 4002: FERMEE"
fi
BOT_START=$(docker inspect -f '{{.State.StartedAt}}' oracle-bot-1 2>/dev/null || echo 15m)
if docker compose logs --since "$BOT_START" bot 2>/dev/null | grep -q "IBKR connecte"; then
    BOT="bot: connecte a IBKR"
else
    BOT="bot: PAS connecte a IBKR (verifier)"
fi
MSG="${BOOT_REPORT_TITLE:-VM redemarree} ($(date '+%d/%m %H:%M'))
$STATUS
$API
$BOT"

HTTP="pas de token"
if [ -n "$TOKEN" ] && [ -n "$CHAT" ]; then
    HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
        --data-urlencode "chat_id=$CHAT" --data-urlencode "text=$MSG" \
        "https://api.telegram.org/bot$TOKEN/sendMessage")
fi
printf '%s %s telegram=%s\n' "$(date '+%F %T')" "$(echo "$MSG" | tr '\n' '|')" "$HTTP" >> "$DIR/boot_report.log"
