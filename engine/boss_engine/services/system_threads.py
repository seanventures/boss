"""The threads that keep the host healthy, started once by the API (stability spec §4).

They run in the API because that is where builds run: the worker's loop blocks for the whole of
a job and is blind while one runs, which is exactly when the host needs watching.

* the ownership ledger (owned_process → owned_resources rows);
* the stop watch: Stop ends the build's processes within seconds (stop_watch);
* the reaper: expired, idle and orphaned previews, orphaned process trees, unclaimed temp
  directories, BOSS's unowned containers and images, the disk prune, table retention
  (resource_reaper).
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("boss.system_threads")


def start_all(engine: Any, settings: Any) -> dict[str, bool]:
    started: dict[str, bool] = {}
    try:
        from . import owned_process
        from .resource_ledger import DbLedger

        owned_process.set_ledger(DbLedger(engine))
        started["ledger"] = True
    except Exception:  # noqa: BLE001 — a backstop must never stop the API booting
        _log.warning("ownership ledger not enabled", exc_info=True)
        started["ledger"] = False
    try:
        from . import stop_watch

        stop_watch.start(engine)
        started["stop_watch"] = True
    except Exception:  # noqa: BLE001
        _log.warning("stop watch not started", exc_info=True)
        started["stop_watch"] = False
    try:
        # A deploy drains instead of killing builds mid-turn (drain.py). Needs the main thread,
        # which the API's lifespan runs on.
        from .drain import install_sigterm_drain

        started["drain_on_sigterm"] = install_sigterm_drain(engine, settings)
    except Exception:  # noqa: BLE001
        _log.warning("drain-on-SIGTERM not installed", exc_info=True)
        started["drain_on_sigterm"] = False
    try:
        from . import resource_reaper

        resource_reaper.start(engine, settings)
        started["reaper"] = True
    except Exception:  # noqa: BLE001
        _log.warning("reaper not started", exc_info=True)
        started["reaper"] = False
    return started
