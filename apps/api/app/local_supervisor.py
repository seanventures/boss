"""Keep a worker alive whenever BOSS runs on someone's machine.

WHY
---
The worker holds no port and prints nothing to the screen, so its absence looks exactly like
slowness. A meeting sat at "running" for twenty minutes and was diagnosed twice as a meeting
regression before anyone asked whether a worker existed; it had been gone for six days, and
`.env` pointed it at a control-plane port nothing served, so it also failed silently when it
WAS started. Both failures produced healthy-looking screens and a queue that quietly filled.

"Your worker wasn't running" is a diagnosis, not a fix. On a laptop there is no orchestrator
to notice, so the API -- the process that is definitely running, because the console is
talking to it -- adopts the job: it checks for a worker, starts one if missing, restarts it
if it dies, and requeues work that a stall already failed.

Deliberately local-only. In production a real supervisor owns process lifecycle and a second
opinion from inside the API would fight it, so this refuses to run unless BOSS_ENV says local
(or BOSS_LOCAL_SUPERVISOR is set explicitly).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_CHECK_SECONDS = float(os.getenv("BOSS_SUPERVISOR_INTERVAL", "20"))
_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKER_MODULE = "apps.worker.boss_worker.main"


def enabled() -> bool:
    """OPT-IN, deliberately.

    This used to default to on whenever BOSS_ENV was unset — which reads as "a laptop" and is
    equally true of a server whose compose file simply never set the variable. Getting that
    wrong starts a SECOND worker inside the API container next to the real one, and two workers
    leasing the same jobs is a worse failure than the missing worker this was written to fix.
    A default that is safe on a laptop and dangerous in production is not a safe default.
    """
    explicit = os.getenv("BOSS_LOCAL_SUPERVISOR")
    if explicit is not None:
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    env = (os.getenv("BOSS_ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    # Only an explicitly local environment. Unset means "unknown", which is not local.
    return env in {"local", "dev", "development"}


def worker_running() -> bool:
    """A worker started by ANY means counts -- `pnpm dev`, stack.sh, or a bare shell.
    Starting a second one would double-lease jobs, which is worse than none."""
    try:
        out = subprocess.run(["pgrep", "-f", _WORKER_MODULE],
                             capture_output=True, text=True, timeout=5)
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001 — pgrep is absent on some platforms; assume not running
        return False


def _spawn() -> subprocess.Popen | None:
    env = dict(os.environ)
    env["PYTHONPATH"] = f".:engine:{env.get('PYTHONPATH', '')}".rstrip(":")
    # The worker dials this to lease jobs. When it pointed at a dead port the worker ran
    # happily and did nothing at all, so a wrong value must be loud, not inherited in silence.
    env.setdefault("BOSS_CONTROL_PLANE_URL", f"http://127.0.0.1:{os.getenv('PORT', '8000')}")
    logs = Path(os.getenv("BOSS_LOG_DIR", "/tmp/boss-stack"))
    logs.mkdir(parents=True, exist_ok=True)
    handle = (logs / "worker.log").open("a")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", _WORKER_MODULE],
            cwd=str(_REPO_ROOT), env=env, stdout=handle, stderr=handle,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[supervisor] could not start a worker: {exc}")
        return None


def start() -> None:
    """Begin supervising. Safe to call when a worker is already up: it does nothing."""
    if not enabled():
        print("[supervisor] not local — leaving process lifecycle to the orchestrator")
        return

    def _loop() -> None:
        child: subprocess.Popen | None = None
        while True:
            try:
                if child is not None and child.poll() is not None:
                    print(f"[supervisor] worker exited ({child.returncode}) — restarting")
                    child = None
                if child is None and not worker_running():
                    child = _spawn()
                    if child is not None:
                        print(f"[supervisor] started worker pid={child.pid}")
                        # Work that a stall already failed is recoverable now that something
                        # is consuming the queue again; leaving it failed makes the operator
                        # relaunch by hand for an outage that was never theirs.
                        _requeue_stalled()
            except Exception as exc:  # noqa: BLE001 — supervision must never kill the API
                print(f"[supervisor] check failed: {exc}")
            time.sleep(_CHECK_SECONDS)

    threading.Thread(target=_loop, name="boss-local-supervisor", daemon=True).start()


def _requeue_stalled() -> None:
    """Re-arm runs that a watchdog failed only because nothing was consuming their jobs.

    Strictly limited to that case: the run must have made NO progress (its step job is still
    queued at attempt 0) and its error must be a stall. A run that failed on its own merits is
    left alone -- retrying real failures automatically is how a bug becomes a spend loop.
    """
    try:
        from boss_engine.db import get_engine
        engine = get_engine()
        with engine.begin() as connection:
            from sqlalchemy import text
            rows = connection.execute(text("""
                UPDATE meeting_runs m SET status='running', error=NULL
                WHERE m.status='failed' AND m.error LIKE '%stalled%'
                  AND EXISTS (SELECT 1 FROM jobs j
                              WHERE j.status='queued' AND j.attempt=0
                                AND j.payload::text LIKE '%' || m.id || '%')
                RETURNING m.id
            """)).fetchall()
        if rows:
            print(f"[supervisor] re-armed stalled meetings: {[r[0] for r in rows]}")
    except Exception as exc:  # noqa: BLE001
        print(f"[supervisor] requeue skipped: {exc}")
