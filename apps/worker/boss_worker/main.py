import argparse
import logging
import time

from apps.api.app.db import database_is_reachable, get_engine

from .config import get_worker_settings
from .control_plane import ControlPlaneClient
from .runner import run_worker_cycle


LOGGER = logging.getLogger("boss.worker")
QA_BENCHMARK_SWEEP_INTERVAL_SECONDS = 60
# The shepherd watches in-flight work. Often enough that a duplicate is killed before it
# costs anything real, rare enough that it is never the busiest thing on the box.
SHEPHERD_WATCH_INTERVAL_SECONDS = 120
_last_shepherd_watch_monotonic = 0.0
_last_qa_benchmark_sweep_monotonic = 0.0


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

    client = ControlPlaneClient(settings.control_plane_url, api_key=settings.api_key)
    try:
        if args.drain_once:
            run_worker_cycle(client, settings)
            return

        while True:
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
            if not ran_job:
                _sweep_deployment_health(client)
                _watch_running_tasks(client)
                _sweep_qa_benchmark_evaluations(client)
                time.sleep(settings.poll_interval_seconds)
    finally:
        client.close()


if __name__ == "__main__":
    main()
