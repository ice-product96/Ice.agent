#!/bin/sh
set -e
mkdir -p /app/data/sessions /app/data/backups
# Named volume often starts as root; fix so app user can write sessions.
if [ "$(id -u)" = "0" ]; then
  chown -R ice:ice /app/data || true
  exec gosu ice "$@"
fi
exec "$@"
