#!/bin/sh
set -eu
mkdir -p /app/data/sessions /app/data/backups /app/data/hf /app/data/hf/fastembed
# Named Docker volumes are often root-owned; ensure writable before dropping privileges.
if [ "$(id -u)" = "0" ]; then
  chown -R ice:ice /app/data
  exec gosu ice "$@"
fi
exec "$@"
