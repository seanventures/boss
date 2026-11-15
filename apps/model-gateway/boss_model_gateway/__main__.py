"""python -m boss_model_gateway  (PYTHONPATH includes apps/model-gateway)

    BOSS_MODEL_GATEWAY_PORT (8092)   the streaming listener boss-hostd forwards model calls to
    BOSS_MODEL_GATEWAY_HOST (0.0.0.0)
    BOSS_MODEL_GATEWAY_DIRECT (off)  also accept a handle presented directly (the local lane's
                                     thin client on the compose network)
    DATABASE_URL                     handles, epochs, keys (through resolve_secret_ref), audit
"""
from __future__ import annotations

import os

import uvicorn

from .app import build_app

if __name__ == "__main__":
    uvicorn.run(build_app(), host=os.environ.get("BOSS_MODEL_GATEWAY_HOST", "0.0.0.0"),
                port=int(os.environ.get("BOSS_MODEL_GATEWAY_PORT", "8092")), proxy_headers=False,
                # A model answer streams for minutes: never cut a live stream on idle.
                timeout_keep_alive=75, h11_max_incomplete_event_size=1024 * 1024)
