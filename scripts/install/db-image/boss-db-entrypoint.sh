#!/bin/sh
# Refuse to open a data directory initialised under another C library (collation hazard,
# KNOWLEDGE_GRAPH_SPEC.md §3.1). An Alpine (musl) directory is moved by dump and restore:
# scripts/install/db_image_migrate.py, which install.sh runs on upgrade.
set -eu
data="${PGDATA:-/var/lib/postgresql/data}"
if [ -s "$data/PG_VERSION" ] && [ "${BOSS_DB_ALLOW_UNMARKED:-0}" != 1 ]; then
  marker="$(cat "$data/boss-libc" 2>/dev/null || true)"
  if [ "$marker" != glibc ]; then
    echo "boss-db: this data directory was initialised by ${marker:-an unmarked (Alpine/musl) image}." >&2
    echo "boss-db: opening it under glibc would corrupt text indexes. Nothing was started." >&2
    echo "boss-db: migrate it with scripts/install/db_image_migrate.py (dump and restore); see scripts/install/README.md." >&2
    exit 64
  fi
fi
exec docker-entrypoint.sh "$@"
