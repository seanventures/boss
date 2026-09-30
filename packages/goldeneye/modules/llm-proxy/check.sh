#!/usr/bin/env bash
# Health/drift check for module llm-proxy — optional OpenAI-compatible gateway.
set -euo pipefail
RUN="${SHARED_SERVER_LLM_RUN:-/opt/shared-server/run/llm-proxy}"
fail=0

if [ -d "$RUN" ]; then
  echo "check: llm-proxy run dir $RUN present"
else
  echo "check: llm-proxy note: not materialised (optional module)"
fi

if command -v docker >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -Eqi 'llm-proxy|openwebui|litellm|vllm'; then
    echo "check: llm-proxy related container running"
  elif [ -f "$RUN/compose.yaml" ] || [ -f "$RUN/docker-compose.yml" ]; then
    echo "check: llm-proxy WARN compose present but container not running" >&2
    fail=1
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "check: llm-proxy FAIL" >&2
  exit 1
fi
echo "check: llm-proxy ok"
exit 0
