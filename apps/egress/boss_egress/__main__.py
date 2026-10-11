"""python -m apps.egress.boss_egress

    BOSS_EGRESS_PORT (8090)          the GitHub reverse proxy (git + REST)
    BOSS_EGRESS_CONNECT_PORT (8091)  the allowlisted CONNECT proxy: the sandbox's model gateway and
                                     package route (connect.py); 0 turns it off
    BOSS_LINK_PORTS (18100-18199)    the link gateway (links.py): one port per active app link;
                                     BOSS_LINK_GATEWAY=off turns it off
"""
from __future__ import annotations

import os
import threading

import uvicorn

from .app import build_app
from .connect import serve_forever
from . import links

if __name__ == "__main__":
    connect_port = int(os.environ.get("BOSS_EGRESS_CONNECT_PORT", "8091"))
    if connect_port:
        threading.Thread(target=serve_forever, args=(os.environ.get("BOSS_EGRESS_HOST", "0.0.0.0"), connect_port),
                         daemon=True, name="egress-connect").start()
    if links.enabled():
        from .app import _engine

        threading.Thread(target=links.run, args=(_engine(), threading.Event()),
                         kwargs={"host": os.environ.get("BOSS_EGRESS_HOST", "0.0.0.0")},
                         daemon=True, name="egress-links").start()
    uvicorn.run(build_app(listen=True), host=os.environ.get("BOSS_EGRESS_HOST", "0.0.0.0"),
                port=int(os.environ.get("BOSS_EGRESS_PORT", "8090")), proxy_headers=False)
