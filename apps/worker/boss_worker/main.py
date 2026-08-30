import argparse
import logging
import signal
import time

from apps.api.app.db import database_is_reachable, get_engine

from .config import get_worker_settings
from .control_plane import ControlPlaneClient
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
            _tick_agent_processes(client, settings)
            _tick_process_manager(client, settings)
            _tick_factory_flow(client, settings)
            if _STOPPING:
                break
            if not ran_job:
                _sweep_deployment_health(client)
                _watch_running_tasks(client)
                _run_shepherd_autopilot(client, settings)
                _sweep_qa_benchmark_evaluations(client)
                _prune_activity_log(client)
                _advance_delivery_workflows(client, settings)
                # Interruptible sleep: an idle worker that is asked to stop should not hold the
                # deploy open for a full poll interval doing nothing.
                for _ in range(max(1, int(settings.poll_interval_seconds))):
                    if _STOPPING:
                        break
                    time.sleep(1)
        LOGGER.info("worker_stopped worker_id=%s", settings.worker_id)
    finally:
        client.close()


if __name__ == "__main__":
    main()
