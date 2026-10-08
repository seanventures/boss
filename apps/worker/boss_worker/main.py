import argparse
import logging
import signal
import threading
import time

from apps.api.app.db import database_is_reachable, get_engine
from boss_engine.services.self_observability import StallTracker

from .config import get_worker_settings
from .control_plane import ControlPlaneClient
from . import runner
from .runner import run_worker_cycle


LOGGER = logging.getLogger("boss.worker")

#: Set when the container is asked to stop. A deploy recreates the worker, and Docker sends
#: SIGTERM then SIGKILLs after its grace period — with no handler at all, Python's default
#: terminated the process instantly, mid-job, with nothing logged about what was interrupted.
#:
#: This does NOT try to finish or hand back the running job. The lease reclaimer already covers
#: that: the lease expires within one TTL and the job is claimed again. Releasing it faster
#: from a signal handler means reaching into a main thread that is blocked deep inside
#: execute_job, and getting that wrong costs more than the minutes it saves. See R12.
#:
#: What it does buy: an idle worker stops immediately instead of sleeping out its poll
#: interval, and a busy one says exactly which job the deploy interrupted rather than
#: vanishing silently.
_STOPPING = False


def _request_stop(signum, _frame) -> None:
    global _STOPPING
    _STOPPING = True
    # And tell the RUNNER, whose lease-heartbeat thread is the only thing still able to act:
    # the main thread is blocked inside execute_job. It hands the running job back by
    # shortening the lease, so a deploy costs seconds of recovery instead of a full 300s TTL.
    runner.SHUTDOWN.set()
    LOGGER.info("worker_stop_requested signal=%s", signum)


def _install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            # Not the main thread, or a platform without the signal — never fatal.
            LOGGER.debug("signal_handler_unavailable signal=%s", sig, exc_info=True)
QA_BENCHMARK_SWEEP_INTERVAL_SECONDS = 60
#: Hourly. Retention is a slow-moving fact — nothing ages out between two minutes — and the
#: delete is batched, so running it often would be pure lock churn on a table the console reads.
ACTIVITY_RETENTION_INTERVAL_SECONDS = 3600
#: Delivery workflows advance on the worker loop. Frequent enough that a customer watching a
#: workflow sees it move, cheap enough to run every idle pass: the endpoint returns immediately
#: when nothing is ready.
DELIVERY_WORKFLOW_INTERVAL_SECONDS = 20
# The shepherd watches in-flight work. Often enough that a duplicate is killed before it
# costs anything real, rare enough that it is never the busiest thing on the box.
SHEPHERD_WATCH_INTERVAL_SECONDS = 120
SHEPHERD_AUTOPILOT_INTERVAL_SECONDS = 180
METATRON_WATCH_INTERVAL_SECONDS = 60
FOCUSED_AGENT_INTERVAL_SECONDS = 60
AGENT_PROCESS_INTERVAL_SECONDS = 30
PROCESS_MANAGER_INTERVAL_SECONDS = 60
FACTORY_FLOW_INTERVAL_SECONDS = 45
#: How often to look for monitors and schedules that are due. A minute is fine: the wakeups
#: themselves carry the real timing, and this only decides how late a due one can be. Checking
#: more often would not make anything happen sooner — the minimum interval is 60s — and this
#: is the one sweep that can start model turns.
AGENT_WAKEUPS_INTERVAL_SECONDS = 60
_last_shepherd_watch_monotonic = 0.0
_last_shepherd_autopilot_monotonic = 0.0
_last_qa_benchmark_sweep_monotonic = 0.0
_last_activity_retention_monotonic = 0.0
_last_delivery_workflow_monotonic = 0.0
_last_metatron_watch_monotonic = 0.0
_last_focused_agent_monotonic = 0.0
_last_agent_process_monotonic = 0.0
_last_process_manager_monotonic = 0.0
_last_factory_flow_monotonic = 0.0
_last_agent_wakeups_monotonic = 0.0


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _sweep_deployment_health(client: ControlPlaneClient) -> None:
    """Take health samples for deployed apps, on the worker's idle path.

    Monitoring reported every app as "unknown" because nothing ever probed one: the sweep was
    written, its docstring said "runs on the worker's idle path", and no caller was ever added.
    A monitoring feature that only samples when a human presses a button is not monitoring —
    uptime, latency and incidents all need a series, and a series needs a clock.

    Idle path, not a timer: the worker is here because it has no job to run, so a probe costs
    nothing that mattered. The sweep decides for itself which targets are DUE, so calling it
    every poll interval does not mean probing every poll interval.

    Swallows everything. A stale dashboard is a small problem; a worker that dies taking health
    samples is a large one.
    """
    try:
        client.sweep_deployment_health()
    except Exception:  # noqa: BLE001
        LOGGER.debug("health_sweep_failed", exc_info=True)


def _run_shepherd_autopilot(client: ControlPlaneClient, settings) -> None:
    """Apply what the shepherd has already decided, on the idle path.

    Watching and proposing were wired long ago; applying never was. Without this tick the whole
    remedy ledger is bookkeeping — an operator still has to notice the failure and press the
    button, which is the opposite of what it exists for.

    Swallowed like the other sweeps: a shepherd that kills the worker is worse than one that
    misses a repair.
    """
    global _last_shepherd_autopilot_monotonic
    now = time.monotonic()
    if now - _last_shepherd_autopilot_monotonic < SHEPHERD_AUTOPILOT_INTERVAL_SECONDS:
        return
    project_id = (
        getattr(settings, "default_project_id", None)
        or getattr(settings, "project_id", None)
        or "boss-dogfood"
    )
    try:
        result = client.shepherd_autopilot(str(project_id))
        _last_shepherd_autopilot_monotonic = now
        applied = (result or {}).get("applied") or []
        if applied:
            LOGGER.info("shepherd_autopilot applied=%s deferred=%s",
                        len(applied), len((result or {}).get("deferred") or []))
    except Exception:  # noqa: BLE001
        _last_shepherd_autopilot_monotonic = now
        LOGGER.debug("shepherd_autopilot skipped", exc_info=True)


def _watch_running_tasks(client: ControlPlaneClient) -> None:
    """Let the Task Shepherd see work while it is still running.

    Everything the shepherd could do until now happened after a run had already failed, so
    the expensive cases — seven identical runs, a lease nobody holds, an hour wedged in one
    phase — were nobody's job. Running this on the idle path makes watching continuous
    instead of something a human remembers to do.

    Swallowed like the health sweep: a shepherd that kills the worker is worse than one that
    misses a duplicate.
    """
    global _last_shepherd_watch_monotonic
    now = time.monotonic()
    if now - _last_shepherd_watch_monotonic < SHEPHERD_WATCH_INTERVAL_SECONDS:
        return
    try:
        result = client.watch_running_tasks()
        _last_shepherd_watch_monotonic = now
        if result.get("acted") or result.get("escalated"):
            LOGGER.info("shepherd_watch acted=%s escalated=%s in_flight=%s",
                        len(result.get("acted") or []), len(result.get("escalated") or []),
                        result.get("in_flight"))
    except Exception:  # noqa: BLE001
        LOGGER.debug("shepherd_watch_failed", exc_info=True)


def _report_host_stall(client, stalled_seconds: float) -> None:
    """Record that the machine stopped scheduling BOSS, so a slow run can be explained.

    Best effort and silent on failure: a diagnostic that breaks the loop it is diagnosing is
    worse than no diagnostic — and this fires precisely when the host is already in trouble.
    """
    try:
        client.report_host_stall(round(stalled_seconds, 2))
    except Exception:  # noqa: BLE001
        pass


def _tick_metatron_watch(client: ControlPlaneClient, settings) -> None:
    """Always-on Metatron overwatch: observe, measure, validate, apply safe, verify.

    Runs even while a job is leased so goals keep being scored. Apply is still
    fail-closed (budget, duplicates, human-only kinds). Swallowed like the
    shepherd: a captain that kills the worker is worse than none.
    """
    global _last_metatron_watch_monotonic
    now = time.monotonic()
    if now - _last_metatron_watch_monotonic < METATRON_WATCH_INTERVAL_SECONDS:
        return
    project_id = (
        getattr(settings, "default_project_id", None)
        or getattr(settings, "project_id", None)
        or "boss-dogfood"
    )
    try:
        result = client.tick_metatron_overwatch(str(project_id))
        _last_metatron_watch_monotonic = now
        applied = result.get("applied") if isinstance(result, dict) else None
        verify = (result or {}).get("verify") if isinstance(result, dict) else None
        if applied or (isinstance(verify, dict) and verify.get("ok") is False):
            LOGGER.info(
                "metatron_overwatch status=%s applied=%s verify_ok=%s",
                (result or {}).get("status"),
                len(applied or []),
                (verify or {}).get("ok"),
            )
    except Exception:  # noqa: BLE001
        LOGGER.debug("metatron_overwatch_failed", exc_info=True)


def _tick_focused_agents(client: ControlPlaneClient, settings) -> None:
    """Always-on focused agents. Fail-closed apply. Never kill the worker."""
    global _last_focused_agent_monotonic
    now = time.monotonic()
    if now - _last_focused_agent_monotonic < FOCUSED_AGENT_INTERVAL_SECONDS:
        return
    project_id = (
        getattr(settings, "default_project_id", None)
        or getattr(settings, "project_id", None)
        or "boss-dogfood"
    )
    try:
        result = client.tick_focused_agents(str(project_id))
        _last_focused_agent_monotonic = now
        ran = result.get("ran") if isinstance(result, dict) else None
        if ran:
            LOGGER.info("focused_agents ran=%s active=%s", len(ran), (result or {}).get("active"))
    except Exception:  # noqa: BLE001
        LOGGER.debug("focused_agents_failed", exc_info=True)


#: How often to look for a due schedule. Not every poll: the poll is 15s and the finest
#: schedule BOSS offers is hourly, so checking four times a minute is three hundred queries an
#: hour to answer a question that changes once. A minute is well inside the resolution.
WORKFLOW_SCHEDULE_INTERVAL_SECONDS = 60
_last_workflow_schedule_monotonic = 0.0


def _tick_workflow_schedules(client: ControlPlaneClient, settings) -> None:
    """Start workflows whose schedule has come round. Never kill the worker."""
    global _last_workflow_schedule_monotonic
    now = time.monotonic()
    if now - _last_workflow_schedule_monotonic < WORKFLOW_SCHEDULE_INTERVAL_SECONDS:
        return
    project_id = (
        getattr(settings, "default_project_id", None)
        or getattr(settings, "project_id", None)
        or "boss-dogfood"
    )
    try:
        result = client.tick_workflow_schedules(str(project_id))
        _last_workflow_schedule_monotonic = now
        started = (result or {}).get("started") or []
        # Logged only when something happened. A line every minute saying "nothing was due"
        # is how a log stops being read.
        for entry in started:
            if entry.get("error"):
                LOGGER.warning("workflow_schedule_failed name=%s error=%s disabled=%s",
                               entry.get("name"), entry.get("error"), entry.get("disabled"))
            else:
                LOGGER.info("workflow_schedule_started name=%s workflow=%s run=%s",
                            entry.get("name"), entry.get("workflow_key"), entry.get("run_id"))
    except Exception:  # noqa: BLE001
        LOGGER.debug("workflow_schedules_failed", exc_info=True)


#: Repo and models' daily re-check. Asked hourly; only rows whose last check is over a day old
#: are re-proved (`target: stale`), so a token that expired or was revoked overnight shows on
#: the Command Center without anybody opening the page.
SETUP_RECHECK_INTERVAL_SECONDS = 3600
_last_setup_recheck_monotonic = 0.0


def _tick_setup_checks(client: ControlPlaneClient, settings) -> None:
    """Re-check stale repository and model rows. Never kill the worker."""
    global _last_setup_recheck_monotonic
    now = time.monotonic()
    if now - _last_setup_recheck_monotonic < SETUP_RECHECK_INTERVAL_SECONDS:
        return
    _last_setup_recheck_monotonic = now
    project_id = (
        getattr(settings, "default_project_id", None)
        or getattr(settings, "project_id", None)
        or "boss-dogfood"
    )
    try:
        view = client.recheck_setup(str(project_id))
        LOGGER.info("setup_rechecked verdict=%s", ((view or {}).get("verdict") or {}).get("label"))
    except Exception:  # noqa: BLE001
        LOGGER.debug("setup_recheck_failed", exc_info=True)


#: Improve BOSS and run scorecards (docs/product/RUN_SCORECARDS.md). Every 10 minutes the API
#: is asked; it does scorecard upkeep at most hourly and the one daily send only when due (a
#: random time each UTC day). No thread, no retry: a failed send waits for tomorrow.
IMPROVE_TICK_INTERVAL_SECONDS = 600
_last_improve_tick_monotonic = 0.0


def _tick_improve(client: ControlPlaneClient, settings) -> None:
    """Never kill the worker; never retry here."""
    global _last_improve_tick_monotonic
    now = time.monotonic()
    if now - _last_improve_tick_monotonic < IMPROVE_TICK_INTERVAL_SECONDS:
        return
    _last_improve_tick_monotonic = now
    try:
        got = client.improve_tick()
        LOGGER.debug("improve_tick send=%s", (got or {}).get("send"))
    except Exception:  # noqa: BLE001
        LOGGER.debug("improve_tick_failed", exc_info=True)


CONTROL_PLANE_TOKEN_SWEEP_SECONDS = 60
_last_token_sweep_monotonic = 0.0


def _tick_control_plane_tokens() -> None:
    """Revoke this process's unscoped GitHub App tokens that are due (id-sec-final2): the list is
    in-process, so every process that mints one sweeps its own. Never kills the worker."""
    global _last_token_sweep_monotonic
    now = time.monotonic()
    if now - _last_token_sweep_monotonic < CONTROL_PLANE_TOKEN_SWEEP_SECONDS:
        return
    _last_token_sweep_monotonic = now
    try:
        from boss_engine.identity.github_setup import sweep_control_plane_tokens

        sweep_control_plane_tokens()
    except Exception:  # noqa: BLE001
        LOGGER.debug("control_plane_token_sweep_failed", exc_info=True)


def _tick_agent_processes(client: ControlPlaneClient, settings) -> None:
    global _last_agent_process_monotonic
    now = time.monotonic()
    if now - _last_agent_process_monotonic < AGENT_PROCESS_INTERVAL_SECONDS:
        return
    project_id = (
        getattr(settings, "default_project_id", None)
        or getattr(settings, "project_id", None)
        or "boss-dogfood"
    )
    try:
        result = client.tick_agent_processes(str(project_id))
        _last_agent_process_monotonic = now
        if isinstance(result, dict) and result.get("ran"):
            LOGGER.info("agent_processes ran=%s", result.get("ran"))
    except Exception:  # noqa: BLE001
        LOGGER.debug("agent_processes_failed", exc_info=True)


def _tick_factory_flow(client: ControlPlaneClient, settings) -> None:
    """Observe factory/swarm stalls. Record findings. Never edit BOSS."""
    global _last_factory_flow_monotonic
    now = time.monotonic()
    if now - _last_factory_flow_monotonic < FACTORY_FLOW_INTERVAL_SECONDS:
        return
    project_id = (
        getattr(settings, "default_project_id", None)
        or getattr(settings, "project_id", None)
        or "boss-dogfood"
    )
    try:
        result = client.tick_factory_flow(str(project_id))
        _last_factory_flow_monotonic = now
        if isinstance(result, dict) and (result.get("recorded") or result.get("attached")):
            LOGGER.info(
                "factory_flow recorded=%s attached=%s stalled=%s",
                result.get("recorded"),
                result.get("attached"),
                result.get("stalled"),
            )
    except Exception:  # noqa: BLE001
        LOGGER.debug("factory_flow_failed", exc_info=True)


def _tick_agent_wakeups(client: ControlPlaneClient, settings) -> None:
    """Fire monitors and schedules whose time has come.

    Errors are swallowed like every other tick: a sweep that raises must not take the worker
    down, and the wakeup itself records its own failure against its own row.
    """
    global _last_agent_wakeups_monotonic
    now = time.monotonic()
    if now - _last_agent_wakeups_monotonic < AGENT_WAKEUPS_INTERVAL_SECONDS:
        return
    project_id = (
        getattr(settings, "default_project_id", None)
        or getattr(settings, "project_id", None)
        or "boss-dogfood"
    )
    try:
        result = client.tick_agent_wakeups(str(project_id))
        _last_agent_wakeups_monotonic = now
        if isinstance(result, dict) and result.get("ran"):
            LOGGER.info("agent_wakeups due=%s ran=%s",
                        result.get("due"), len(result.get("ran") or []))
    except Exception:  # noqa: BLE001
        LOGGER.debug("agent_wakeups_failed", exc_info=True)


def _tick_process_manager(client: ControlPlaneClient, settings) -> None:
    """Always-on Project Manager steward. Swallow errors. Never kill the worker."""
    global _last_process_manager_monotonic
    now = time.monotonic()
    if now - _last_process_manager_monotonic < PROCESS_MANAGER_INTERVAL_SECONDS:
        return
    project_id = (
        getattr(settings, "default_project_id", None)
        or getattr(settings, "project_id", None)
        or "boss-dogfood"
    )
    try:
        result = client.tick_process_manager(str(project_id))
        _last_process_manager_monotonic = now
        last_tick = result.get("last_tick") if isinstance(result, dict) else None
        if last_tick:
            LOGGER.info(
                "process_manager status=%s",
                last_tick.get("status") if isinstance(last_tick, dict) else last_tick,
            )
    except Exception:  # noqa: BLE001
        LOGGER.debug("process_manager_failed", exc_info=True)


def _sweep_qa_benchmark_evaluations(client: ControlPlaneClient) -> None:
    """Keep the benchmark evidence ledger current while the worker is idle.

    The service compares report hashes and emits an event only when durable evidence
    changed. It also reconciles a frozen measurement baseline only after the linked
    proposal is already rolled out, so invoking it at the poll interval cannot create
    a tuning change or an external side effect.
    """
    global _last_qa_benchmark_sweep_monotonic
    now = time.monotonic()
    if now - _last_qa_benchmark_sweep_monotonic < QA_BENCHMARK_SWEEP_INTERVAL_SECONDS:
        return
    try:
        client.sweep_qa_benchmark_evaluations()
        _last_qa_benchmark_sweep_monotonic = now
    except Exception:  # noqa: BLE001
        LOGGER.debug("qa_benchmark_sweep_failed", exc_info=True)


def _advance_delivery_workflows(client: ControlPlaneClient, settings) -> None:
    """Move customer delivery workflows forward.

    The runtime schedules and leases steps but has never executed one itself — an outside
    caller has always claimed and completed them. For customer workflows this is that caller.

    Swallowed like every other sweep: a workflow that cannot advance must not take the worker
    down with it, and the endpoint already isolates one failing run from the rest.
    """
    global _last_delivery_workflow_monotonic
    now = time.monotonic()
    if now - _last_delivery_workflow_monotonic < DELIVERY_WORKFLOW_INTERVAL_SECONDS:
        return
    # Same resolution as every other per-project tick in this file, so one worker
    # configuration drives them all consistently.
    project_id = (
        getattr(settings, "default_project_id", None)
        or getattr(settings, "project_id", None)
        or "boss-dogfood"
    )
    try:
        client.run_delivery_capabilities(str(project_id))
        _last_delivery_workflow_monotonic = now
    except Exception:  # noqa: BLE001
        LOGGER.debug("delivery_workflow_tick_failed", exc_info=True)



def _run_background_sweeps(settings) -> None:
    """Sweeps that must keep running WHILE the worker is busy, on their own thread.

    `run_worker_cycle` blocks for the entire duration of a job — a governed code task is
    forty-five minutes of blocking — and every periodic sweep sat below it in the same loop,
    behind `if not ran_job`. So the two things whose whole purpose is to watch work in flight
    could only run when there was no work in flight.

    Measured on wfr_c8578ea1abbf4a1383cc: its `build` step waited 46m47s on a code task that had
    already finished, opened its pull request and passed its tests. Driving the same tick by
    hand completed the step in 6 seconds. Nothing was wrong with the tick; it was never called.

    Its own client, because the main loop is using the other one on another thread and an HTTP
    session shared across threads is a race nobody wants to debug at 2am. Daemon, so a stop
    signal does not wait on it.
    """
    client = ControlPlaneClient(settings.control_plane_url, api_key=settings.api_key)
    try:
        while not _STOPPING:
            # Each of these keeps its own interval guard, so this loop can tick often and
            # cheaply without changing how frequently any individual sweep actually runs.
            try:
                _advance_delivery_workflows(client, settings)
                _watch_running_tasks(client)
            except Exception:  # noqa: BLE001 — a sweep must never take the worker down
                LOGGER.debug("background_sweep_failed", exc_info=True)
            for _ in range(5):
                if _STOPPING:
                    break
                time.sleep(1)
    finally:
        client.close()


def _prune_activity_log(client: ControlPlaneClient) -> None:
    """Age out captured transcripts past the retention window.

    Per-turn capture writes hundreds of rows per build and BOSS builds continuously, so this is
    the one log in the system that grows without a natural bound. Deletes ROWS only — no files,
    no directories, and nothing that records what BOSS built.

    Swallowed like every other sweep: housekeeping that can kill the worker is worse than
    housekeeping that skips an hour.
    """
    global _last_activity_retention_monotonic
    now = time.monotonic()
    if now - _last_activity_retention_monotonic < ACTIVITY_RETENTION_INTERVAL_SECONDS:
        return
    try:
        client.prune_activity_log()
        _last_activity_retention_monotonic = now
    except Exception:  # noqa: BLE001
        LOGGER.debug("activity_retention_failed", exc_info=True)


#: Never more lease threads than this, whatever admission measures: the control plane runs
#: every build in its own process and is the thing being protected.
MAX_WORKER_SLOTS = 4


def worker_slots(client, settings) -> int:
    """`BOSS_WORKER_SLOTS`: a number, or `auto` — the control plane's "Builds at once"."""
    raw = str(getattr(settings, "worker_slots", "1") or "1").strip().lower()
    if raw != "auto":
        try:
            return max(1, min(MAX_WORKER_SLOTS, int(raw)))
        except ValueError:
            return 1
    try:
        response = client._client.get("/v1/system/health", timeout=10.0)
        response.raise_for_status()
        value = int(((response.json() or {}).get("admission") or {})
                    .get("builds_at_once", {}).get("value") or 1)
    except Exception:  # noqa: BLE001 — an older API, or none yet: one slot, as before
        return 1
    return max(1, min(MAX_WORKER_SLOTS, value))


def _extra_lease_loop(settings, index: int) -> None:
    """A second (third, …) lease loop: its own client and worker id, jobs only — the ticks and
    sweeps stay on the main loop."""
    slot_settings = settings.model_copy(update={"worker_id": f"{settings.worker_id}-s{index}"})
    client = ControlPlaneClient(slot_settings.control_plane_url, api_key=slot_settings.api_key)
    try:
        while not _STOPPING:
            try:
                ran = run_worker_cycle(client, slot_settings)
            except Exception:  # noqa: BLE001
                LOGGER.exception("worker_slot_cycle_failed worker_id=%s", slot_settings.worker_id)
                ran = False
            if not ran:
                idle_wait(float(slot_settings.poll_interval_seconds))
    finally:
        client.close()


def idle_wait(seconds: float, *, stopping=lambda: _STOPPING, sleep=time.sleep,
              clock=time.monotonic) -> tuple[float, float]:
    """Sleep `seconds` in one-second steps, stopping early when asked. Returns
    (planned, actual): how long the steps that ran were meant to take, and how long they did."""
    planned = 0.0
    started = clock()
    for _ in range(max(1, int(seconds))):
        if stopping():
            break
        sleep(1)
        planned += 1.0
    return planned, clock() - started


def observe_idle_wait(tracker: StallTracker, seconds: float, **kw) -> float:
    """The stall clock: lateness of the idle wait, never time spent working (C5)."""
    planned, actual = idle_wait(seconds, **kw)
    if planned <= 0:
        return 0.0
    return tracker.observe(planned, actual, at=time.time(), source="worker.idle_wait")


def main() -> None:
    parser = argparse.ArgumentParser(description="BOSS worker shell")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Start, log readiness, and exit without entering the idle loop.",
    )
    parser.add_argument(
        "--drain-once",
        action="store_true",
        help="Poll once, execute at most one leased job, and exit.",
    )
    args = parser.parse_args()

    settings = get_worker_settings()
    configure_logging()

    engine = get_engine(settings)
    db_ready = database_is_reachable(engine)
    if db_ready:
        # A BOSS_GITHUB_TOKEN already moved into the vault (Setup › Repo) is not read from this
        # process's environment under enforce: builds reach GitHub only through boss-egress.
        try:
            from boss_engine.identity import github_setup as _identity_github

            if _identity_github.forget_env_token_if_moved(engine, settings):
                settings.github_token = None
        except Exception:  # noqa: BLE001 — never block the worker on this
            pass
    # The worker is where the coding pipeline actually runs, so it is where the interesting
    # lines are written — which command took four minutes, how long a stage queued. Capturing
    # them only in the API would consolidate the half that matters least.
    if db_ready:
        try:
            from boss_engine.services.log_capture import install as _install_log_capture
            _install_log_capture(engine, service="worker")
        except Exception:  # noqa: BLE001 — never block the worker on a log handler
            LOGGER.warning("activity-log capture unavailable", exc_info=True)
        # The model router's OpenRouter catalogue (prices, tier suggestions), in Fixed too.
        from boss_engine.services.model_router.store import load_catalogue_at_boot

        load_catalogue_at_boot(engine)
    LOGGER.info(
        "worker_started worker_id=%s database_connected=%s control_plane_url=%s github_configured=%s openai_configured=%s",
        settings.worker_id,
        db_ready,
        settings.control_plane_url,
        bool(settings.github_token),
        bool(settings.openai_api_key),
    )

    if args.once:
        return

    _install_signal_handlers()
    client = ControlPlaneClient(settings.control_plane_url, api_key=settings.api_key)
    try:
        if args.drain_once:
            run_worker_cycle(client, settings)
            return

        # WATCHING WORK IN FLIGHT CANNOT WAIT FOR THE WORKER TO BE IDLE. See
        # _run_background_sweeps: the main loop blocks for the whole of a job, so anything
        # sharing it only runs between jobs.
        threading.Thread(target=_run_background_sweeps, args=(settings,),
                         name="boss-worker-sweeps", daemon=True).start()

        # MORE THAN ONE BUILD AT A TIME, when the machine has room (stability spec §4b.2).
        extra_slots = [
            threading.Thread(target=_extra_lease_loop, args=(settings, i),
                             name=f"boss-worker-slot-{i}", daemon=True)
            for i in range(1, worker_slots(client, settings))
        ]
        for t in extra_slots:
            t.start()

        # WAS BOSS ABLE TO RUN? Measured over the IDLE WAIT ONLY (stability spec C5).
        #
        # This used to time the whole loop — `now - last_cycle - poll_interval` — and a cycle
        # blocks for an entire job (a code task is one HTTP call with a two-hour timeout). So
        # every job's duration was reported as the HOST stalling: on boss-1, 35 `host.stalled`
        # events in 3.7 days, each one a build finishing (3553 s ↔ a code task that ran
        # 01:19→02:28), on a box with 13 GB free, zero swap and 2 s of memory pressure in total.
        # Host advice and retrospectives then blamed an idle machine. Only the sleep has a
        # known length; only its lateness is the host's.
        _stalls = StallTracker()
        while not _STOPPING:
            try:
                ran_job = run_worker_cycle(client, settings)
            except Exception:
                LOGGER.exception(
                    "worker_cycle_failed worker_id=%s control_plane_url=%s",
                    settings.worker_id,
                    settings.control_plane_url,
                )
                time.sleep(settings.poll_interval_seconds)
                continue
            _tick_metatron_watch(client, settings)
            _tick_focused_agents(client, settings)
            _tick_workflow_schedules(client, settings)
            _tick_setup_checks(client, settings)
            _tick_agent_processes(client, settings)
            _tick_process_manager(client, settings)
            _tick_factory_flow(client, settings)
            _tick_agent_wakeups(client, settings)
            _tick_improve(client, settings)
            _tick_control_plane_tokens()
            if _STOPPING:
                break
            if not ran_job:
                _sweep_deployment_health(client)
                _run_shepherd_autopilot(client, settings)
                _sweep_qa_benchmark_evaluations(client)
                _prune_activity_log(client)
                # _advance_delivery_workflows and _watch_running_tasks now run on their own
                # thread — see _run_background_sweeps. Leaving them here too would double every
                # call for no benefit.
                # Interruptible sleep: an idle worker that is asked to stop should not hold the
                # deploy open for a full poll interval doing nothing.
                late = observe_idle_wait(_stalls, float(settings.poll_interval_seconds))
                if late:
                    LOGGER.warning(
                        "host_stall worker_id=%s stalled_seconds=%.0f — BOSS was not scheduled; "
                        "this time is the machine's, not the pipeline's",
                        settings.worker_id, late)
                    _report_host_stall(client, late)
        # The extra slots hand their jobs back on stop like the main loop; give them the same
        # grace before the process exits under them.
        for t in extra_slots:
            t.join(timeout=50)
        LOGGER.info("worker_stopped worker_id=%s", settings.worker_id)
    finally:
        client.close()


if __name__ == "__main__":
    main()
