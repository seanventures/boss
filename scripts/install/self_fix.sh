#!/usr/bin/env bash
# The host self-installer (BOSS fixes itself, docs/product/BOSS_FIXES_ITSELF.md §6.5). `boss
# self-fix …` runs this; it needs python3 and nothing else on the host.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$here/self_fix_installer.py" "$@"
