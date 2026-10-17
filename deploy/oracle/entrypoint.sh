#!/bin/sh
set -eu
cd /app

PROFILE="${BOT_PROFILE:-.env.ibkr_paper}"
if [ ! -f "$PROFILE" ]; then
    echo "[entrypoint] $PROFILE introuvable dans /app" >&2
    exit 1
fi

# Le profil est charge avec override=True : on lui ajoute les surcharges serveur
# a la fin (la derniere valeur d'une cle gagne) sans toucher au fichier d'origine.
MERGED=/tmp/bot_profile.env
cat "$PROFILE" > "$MERGED"
printf '\n' >> "$MERGED"
cat deploy/oracle/server.overrides >> "$MERGED"

# Gateway met ~1 min a se connecter apres un (re)demarrage.
i=0
until python -c "import socket; socket.create_connection(('127.0.0.1', 4004), 2).close()" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        echo "[entrypoint] Gateway injoignable sur 127.0.0.1:4004 apres 5 min" >&2
        exit 1
    fi
    sleep 5
done

exec python main.py --env-file "$MERGED" --no-portfolio-prompt
