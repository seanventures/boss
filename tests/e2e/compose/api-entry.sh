#!/bin/sh
# The api container's entrypoint under build-override.yml: the scripted model on THIS container's
# loopback (the per-run sandbox uids may reach loopback and nothing else but api/egress), then the
# real entrypoint, unchanged (it fences the sandbox network and becomes `boss`).
set -eu
setpriv --reuid=boss --regid=boss --init-groups --inh-caps=-all \
  python3 /fxd/tests/e2e/stubs/model_stub.py --host 127.0.0.1 --port "${FXD_MODEL_STUB_PORT:-58700}" \
  >/tmp/fxd-model-stub.log 2>&1 &
exec /usr/local/bin/boss-entrypoint "$@"
