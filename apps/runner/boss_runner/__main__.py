"""`python -m apps.runner.boss_runner` — the boss-runner sidecar (BOSSAPP-828).

Environment:
    BOSS_RUNNER_API_URL     the API as this container reaches it (default http://api:8000)
    BOSS_RUNNER_STATE_DIR   the runner's volume: sqlite outbox/journal + session logs
                            (default /var/lib/boss/runner)
    BOSS_RUNNER_PORT        listen port (default 8095)
    BOSS_RUNNER_PUSH_SECONDS  heartbeat / push interval (default 10)
    BOSS_IDENTITY_ISSUER    when set, a run token must carry this `iss`
"""
from __future__ import annotations

import logging
import os
import signal
import threading

from .auth import JwksKeys
from .daemon import Runner
from .server import RunnerServer


def main() -> None:
    logging.basicConfig(level=os.environ.get("BOSS_RUNNER_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    api_url = os.environ.get("BOSS_RUNNER_API_URL", "http://api:8000").rstrip("/")
    runner = Runner(os.environ.get("BOSS_RUNNER_STATE_DIR", "/var/lib/boss/runner"), api_url=api_url,
                    key_for_kid=JwksKeys(api_url), issuer=os.environ.get("BOSS_IDENTITY_ISSUER") or None,
                    interval=float(os.environ.get("BOSS_RUNNER_PUSH_SECONDS", "10")))
    server = RunnerServer(runner, port=int(os.environ.get("BOSS_RUNNER_PORT", "8095")))
    runner.start()
    server.start()
    logging.getLogger("boss.runner").info("boss-runner %s (%s) listening on %s; replayed %s",
                                          runner.runner_id, runner.boot_id, server.url, runner.replayed)
    done = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: done.set())
    done.wait()
    # The runner going away takes its turns with it (the next boot reports them lost).
    server.stop()
    runner.stop(end_processes=True)


if __name__ == "__main__":
    main()
