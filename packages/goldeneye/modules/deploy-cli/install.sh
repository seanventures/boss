#!/usr/bin/env bash
# deploy-cli — ensure ge + deploy are on PATH from the installed kit
set -euo pipefail
KIT="${GOLDENEYE_KIT_ROOT:-/opt/goldeneye}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "deploy-cli"

[ -x "$KIT/bin/ge" ] || ge_die "missing $KIT/bin/ge"
install -m 0755 "$KIT/bin/ge" /usr/local/bin/ge

# If a full production deploy already exists, leave it alone.
if [ -x /usr/local/bin/deploy ] && ! grep -q 'exec ge deploy' /usr/local/bin/deploy 2>/dev/null; then
  ge_info "existing full deploy CLI preserved at /usr/local/bin/deploy"
else
  cat >/usr/local/bin/deploy <<'WRAP'
#!/usr/bin/env bash
# Thin kit wrapper — full dogfood host replaces this with the rich deploy pipeline.
exec ge deploy "$@"
WRAP
  chmod 0755 /usr/local/bin/deploy
  ge_info "installed /usr/local/bin/deploy → ge deploy"
fi

ge_info "deploy-cli complete — try: ge help"
