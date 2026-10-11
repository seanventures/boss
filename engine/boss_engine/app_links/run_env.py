"""The link environment of a BUILD (F5 §5.3 "at build"): computed once when the build starts,
held in this process for the build's life, and added by agent_env.agent_environment to every
process the build spawns (the coding agent, its commands, the verify step), after that function's
credential filter: these are values BOSS computed for this run, not anything inherited.

Keyed by the owner scope (owned_process), so only processes of THAT build get them. When the build
ends, they are dropped here and the build's link passes are revoked.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

_ENV: dict[str, dict[str, str]] = {}
_LOCK = threading.Lock()


def current() -> dict[str, str]:
    from ..services import owned_process

    owner = owned_process.current_owner()
    if owner is None:
        return {}
    with _LOCK:
        for oid in reversed(owner.ids()):
            if oid in _ENV:
                return dict(_ENV[oid])
    return {}


@contextlib.contextmanager
def for_run(engine: Any, run_id: str, project_id: Optional[str]) -> Iterator[dict[str, str]]:
    """Give build `run_id` its links' environment for the block. Never fails the build: a link
    that cannot be resolved is logged (by name, never a value) and the build runs without it."""
    from . import service

    env: dict[str, str] = {}
    if project_id:
        # A new app's start may have asked for links ("Talks to"): its first build creates them,
        # before its own link environment is computed. A refusal is recorded on the request (and
        # audited), never raised: the build runs, without that link.
        try:
            with engine.begin() as db:
                for made in service.create_requested_links(db, project_id=project_id, run_id=run_id):
                    log.info("links: build %s %s link %s (%s)%s", run_id, made["outcome"], made["alias"],
                             made["env"], f": {made['detail']}" if made["detail"] else "")
        except Exception as exc:  # noqa: BLE001
            log.warning("links: build %s could not create its requested links (%s)", run_id, type(exc).__name__)
        try:
            with engine.begin() as db:
                env = service.workload_environment(db, project_id=project_id, run_id=run_id, env="preview",
                                                   consumer_kind="run", consumer_id=run_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("links: build %s runs without its link environment (%s)", run_id, type(exc).__name__)
    if env:
        log.info("links: build %s gets %s", run_id, ", ".join(sorted(env)))
        with _LOCK:
            _ENV[run_id] = env
    try:
        yield env
    finally:
        with _LOCK:
            _ENV.pop(run_id, None)
        if env:
            with contextlib.suppress(Exception):
                with engine.begin() as db:
                    service.revoke_passes_of(db, "run", run_id)
