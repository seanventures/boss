#!/usr/bin/env bash
set -euo pipefail
KIT="${SHARED_SERVER_KIT_ROOT:-/opt/shared-server}"
# shellcheck source=/dev/null
source "$KIT/lib/common.sh"
ge_require_root
ge_step "optional/llm-proxy"
RUN=/opt/shared-server/run/llm-proxy
mkdir -p "$RUN"
cat >"$RUN/README.md" <<'MD'
# LLM proxy (LiteLLM / OpenAI-compatible)

Chat + transcription only on many self-hosted gateways — **no embeddings**.
Point apps at OPENAI_BASE_URL + OPENAI_API_KEY injected by deploy.

Production dogfood: infra/llm-proxy. Wire vault keys before enabling.
MD
ge_info "llm-proxy skeleton at $RUN — configure provider keys before bringing up"
