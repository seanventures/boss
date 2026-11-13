#!/usr/bin/env bash
# Free disk BOSS used, and only what BOSS made (stability spec §4a.8).
#
# Every deploy builds new images and leaves ~1.3 GB of build cache; nothing removed any of it.
# boss-1 reached 80% disk with 46.5 GB of build cache and 8 GB of stale `:local` tags — a full
# disk takes Postgres down, which the operator sees as "random issues". This runs after every
# update (boss.sh, remote.sh) and on demand (`boss prune`).
#
# What it removes:
#   * stopped preview containers (`boss-prev-*`) and preview images no container uses;
#   * dangling images of the boss-community compose project;
#   * boss-community-* image tags beyond the two newest per service (never one in use, never
#     the prev-<tag> rollback point upgrade.sh keeps);
#   * build cache older than 72h, down to 20% of the disk — only above 75% disk AND only with
#     --dedicated (a host that runs nothing but BOSS: Docker's build cache is not per-project,
#     so on a shared machine this would also take other projects' cache).
# Never volumes, never `system prune`, never a container or image without a BOSS name/label.
set -euo pipefail

DEDICATED=0
DRY=0
THRESHOLD=75
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dedicated) DEDICATED=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --threshold) THRESHOLD="${2:?}"; shift 2 ;;
    *) printf 'prune: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ "${BOSS_DEDICATED_HOST:-}" =~ ^(1|true|yes)$ ]] && DEDICATED=1

if docker info >/dev/null 2>&1; then D=(docker)
elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then D=(sudo -n docker)
else printf 'prune: Docker is unavailable to this user\n' >&2; exit 1; fi

run() { if [[ "$DRY" -eq 1 ]]; then printf '  would run: %s\n' "$*"; else "$@" >/dev/null 2>&1 || true; fi; }
used_pct() { df -P / | awk 'NR==2 {gsub("%","",$5); print $5}'; }

before=$(used_pct)
printf 'disk before: %s%% used\n' "$before"

# 1. Stopped preview containers, then preview images nothing uses.
for c in $("${D[@]}" ps -a --filter status=exited --filter status=created --filter status=dead \
             --format '{{.Names}}' | grep '^boss-prev-' || true); do
  run "${D[@]}" rm -f "$c"
done
in_use=$("${D[@]}" ps -a --format '{{.Image}}' | sort -u)
for img in $("${D[@]}" images --format '{{.Repository}}:{{.Tag}}' | grep '^boss-prev-' || true); do
  repo=${img%%:*}
  grep -qxF -e "$img" -e "$repo" <<<"$in_use" || run "${D[@]}" rmi -f "$img"
done

# 2. Dangling images of this compose project.
run "${D[@]}" image prune -f --filter label=com.docker.compose.project=boss-community

# 3. Old BOSS image tags: keep the two newest per repository, and any a container uses.
for repo in $("${D[@]}" images --format '{{.Repository}}' | grep '^boss-community-' | sort -u || true); do
  "${D[@]}" images "$repo" --format '{{.CreatedAt}}|{{.Repository}}:{{.Tag}}' | sort -r | tail -n +3 |
    cut -d'|' -f2 | while read -r img; do
      [[ "$img" == *":<none>" ]] && continue
      # The rollback point upgrade.sh keeps (BOSSAPP-839) is never pruned.
      [[ "$img" == *":prev-"* ]] && continue
      grep -qxF "$img" <<<"$in_use" || run "${D[@]}" rmi "$img"
    done
done

# 4. Build cache, only on a host that is BOSS's alone, and only when the disk needs it.
now=$(used_pct)
if [[ "$now" -ge "$THRESHOLD" ]]; then
  if [[ "$DEDICATED" -eq 1 ]]; then
    total_gb=$(df -BG -P / 2>/dev/null | awk 'NR==2 {gsub("G","",$2); print $2}' || echo 0)
    keep=$(( total_gb / 5 )); [[ "$keep" -lt 1 ]] && keep=1
    run "${D[@]}" builder prune -f --filter until=72h --keep-storage "${keep}gb"
  else
    printf 'disk %s%% used; build cache left alone (not a dedicated BOSS host). On a machine that\n' "$now"
    printf 'runs only BOSS: %s --dedicated\n' "$0"
  fi
fi
printf 'disk after: %s%% used\n' "$(used_pct)"
