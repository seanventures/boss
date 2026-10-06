"""python -m apps.egress.boss_egress  (port BOSS_EGRESS_PORT, default 8090)."""
from __future__ import annotations

import os

import uvicorn

from .app import build_app

if __name__ == "__main__":
    uvicorn.run(build_app(listen=True), host=os.environ.get("BOSS_EGRESS_HOST", "0.0.0.0"),
                port=int(os.environ.get("BOSS_EGRESS_PORT", "8090")), proxy_headers=False)
