#!/bin/sh
# Läuft als Unraid-Standardnutzer nobody:users (99:100), damit Dateirechte passen.
set -e
umask "${UMASK:-002}"
mkdir -p "$CONFIG_DIR"
if [ "$(id -u)" = "0" ]; then
  chown "${PUID}:${PGID}" "$CONFIG_DIR" "$CONFIG_DIR"/* 2>/dev/null || true
  exec su-exec "${PUID}:${PGID}" python -m app.server
fi
exec python -m app.server
