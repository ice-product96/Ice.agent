#!/bin/sh
set -eu
mkdir -p /app/data/sessions /app/data/backups /app/data/hf /app/data/hf/fastembed
# Named Docker volumes are often root-owned; ensure writable before dropping privileges.
if [ "$(id -u)" = "0" ]; then
  chown -R ice:ice /app/data
  gosu ice python -m app.migrate
  exec gosu ice "$@"
fi
python -m app.migrate
exec "$@"
