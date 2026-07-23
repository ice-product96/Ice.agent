#!/bin/sh
set -eu
mkdir -p /app/data/sessions /app/data/backups
# Named Docker volumes are often root-owned; ensure writable before dropping privileges.
if [ "$(id -u)" = "0" ]; then
  chown -R ice:ice /app/data
  exec gosu ice "$@"
fi
exec "$@"
