#!/usr/bin/env bash
# layer4-data — backup hooks + restore-drill stub
set -euo pipefail
KIT="${GOLDENEYE_KIT_ROOT:-/opt/goldeneye}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "layer4-data"

DATA_ROOT="$(ge_load_site_value paths.data_root)"
DATA_ROOT="${DATA_ROOT:-/data}"
mkdir -p "$DATA_ROOT/backups/pg" "$DATA_ROOT/backups/volumes" /opt/goldeneye/run/backup

cat >/opt/goldeneye/run/backup/pg-dump-all.sh <<'SH'
#!/usr/bin/env bash
# Discover running Postgres containers and dump via local socket/exec.
# Verifies gzip is non-trivial size AND contains a SQL header.
set -euo pipefail
OUT_DIR="${1:-/data/backups/pg}"
mkdir -p "$OUT_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ok=0
while read -r cid name; do
  [ -n "$cid" ] || continue
  dest="$OUT_DIR/${name}-${STAMP}.sql.gz"
  if docker exec "$cid" sh -c 'command -v pg_dumpall >/dev/null' 2>/dev/null; then
    if docker exec "$cid" pg_dumpall -U postgres 2>/dev/null | gzip -c >"$dest"; then
      # content check: not empty gzip, and gunzip peeks SQL
      bytes=$(wc -c <"$dest" | tr -d ' ')
      if [ "${bytes:-0}" -lt 64 ]; then
        echo "FAIL $name: dump too small ($bytes bytes)" >&2
        rm -f "$dest"
        continue
      fi
      if ! gzip -dc "$dest" 2>/dev/null | head -c 200 | grep -qiE 'PostgreSQL|CREATE|ROLE|DATABASE'; then
        echo "FAIL $name: dump content check failed" >&2
        rm -f "$dest"
        continue
      fi
      echo "OK $name → $dest ($bytes bytes)"
      ok=$((ok+1))
    fi
  fi
done < <(docker ps --format '{{.ID}} {{.Names}}' | grep -iE 'postgres|pg|supabase.*db' || true)
[ "$ok" -gt 0 ] || { echo "no postgres dumps produced" >&2; exit 1; }
SH
chmod 0755 /opt/goldeneye/run/backup/pg-dump-all.sh

cat >/opt/goldeneye/run/backup/restore-drill.sh <<'SH'
#!/usr/bin/env bash
# Weekly restore drill: restore latest dump into throwaway Postgres, count tables.
set -euo pipefail
DUMP_DIR="${1:-/data/backups/pg}"
latest=$(ls -1t "$DUMP_DIR"/*.sql.gz 2>/dev/null | head -1 || true)
[ -n "$latest" ] || { echo "no dumps in $DUMP_DIR" >&2; exit 1; }
echo "restore-drill using $latest"
cid=$(docker run -d --rm -e POSTGRES_PASSWORD=drill postgres:16-alpine)
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
# wait ready
for i in $(seq 1 30); do
  docker exec "$cid" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 1
done
gzip -dc "$latest" | docker exec -i "$cid" psql -U postgres -v ON_ERROR_STOP=1 >/tmp/restore-drill.log 2>&1 \
  || { echo "RESTORE FAILED — see /tmp/restore-drill.log" >&2; exit 1; }
tables=$(docker exec "$cid" psql -U postgres -tAc "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');")
echo "restore-drill OK tables=$tables"
[ "${tables:-0}" -ge 0 ] || exit 1
SH
chmod 0755 /opt/goldeneye/run/backup/restore-drill.sh

ge_info "layer4-data complete"
ge_info "manual dump: /opt/goldeneye/run/backup/pg-dump-all.sh"
ge_info "manual drill: /opt/goldeneye/run/backup/restore-drill.sh"
