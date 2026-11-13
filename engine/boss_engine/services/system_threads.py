"""The threads that keep the host healthy, started once by the API (stability spec §4).

They run in the API because that is where builds run: the worker's loop blocks for the whole of
a job and is blind while one runs, which is exactly when the host needs watching.

* the ownership ledger (owned_process → owned_resources rows);
* the stop watch: Stop ends the build's processes within seconds (stop_watch);
* the reaper: expired, idle and orphaned previews, orphaned process trees, unclaimed temp
  directories, BOSS's unowned containers and images, the disk prune, table retention
  (resource_reaper);
* the Claude login refresher: the ONE owner that refreshes the Claude CLI's own login, well
  before it expires, so no `claude` process ever refreshes (and wipes) it (claude_login);
* the skill-source sync (skill_sources): upstream skill changes become versions or consents;
* the system doctor: signals → findings → safe automatic fixes, choices on a card, the
  degradation rung admission and Metatron obey (system_doctor);
* the backup scheduler: hourly verified pg_dumps with tiered retention, one holder by DB lease
  (backup_scheduler, BOSSAPP-838);
* the extension scheduler: extensions' scheduled jobs and event feeds, one firing per DB lease
  (extensions/jobs.py, BOSSAPP-861).
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
    try:
        from . import claude_login

        claude_login.start()
        started["claude_login"] = True
    except Exception:  # noqa: BLE001
        _log.warning("Claude login refresher not started", exc_info=True)
        started["claude_login"] = False
    try:
        from . import codex_login

        codex_login.start()
        started["codex_login"] = True
    except Exception:  # noqa: BLE001
        _log.warning("Codex login refresher not started", exc_info=True)
        started["codex_login"] = False
    try:
        from . import system_doctor

        system_doctor.start(engine, settings)
        started["system_doctor"] = True
    except Exception:  # noqa: BLE001
        _log.warning("system doctor not started", exc_info=True)
        started["system_doctor"] = False
    try:
        # Scheduled self-backups (BOSSAPP-838): every API process starts it; the DB lease lets
        # exactly one of them back up.
        from . import backup_scheduler

        if getattr(settings, "backup_schedule_enabled", False):
            backup_scheduler.start(engine, settings)
            started["backup_scheduler"] = True
        else:
            started["backup_scheduler"] = False
    except Exception:  # noqa: BLE001
        _log.warning("backup scheduler not started", exc_info=True)
        started["backup_scheduler"] = False
        # Skill sources (BOSSAPP-841): upstream changes become new versions or parked consents.
        from . import skill_sources

        skill_sources.start(engine, settings)
        started["skill_sources"] = True
    except Exception:  # noqa: BLE001
        _log.warning("skill source sync not started", exc_info=True)
        started["skill_sources"] = False
    try:
        # Extensions (BOSSAPP-861): scheduled jobs and event feeds; every API process ticks, the
        # DB lease lets exactly one of them fire each.
        from ..extensions import jobs as extension_jobs

        if getattr(settings, "extensions_scheduler_enabled", True):
            extension_jobs.start(engine, settings)
            started["extension_scheduler"] = True
        else:
            started["extension_scheduler"] = False
    except Exception:  # noqa: BLE001
        _log.warning("extension scheduler not started", exc_info=True)
        started["extension_scheduler"] = False
    return started
