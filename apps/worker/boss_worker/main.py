import argparse
import logging
import time

from apps.api.app.db import database_is_reachable, get_engine

from .config import get_worker_settings
from .control_plane import ControlPlaneClient
from .runner import run_worker_cycle


LOGGER = logging.getLogger("boss.worker")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


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

    client = ControlPlaneClient(settings.control_plane_url)
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
                time.sleep(settings.poll_interval_seconds)
    finally:
        client.close()


if __name__ == "__main__":
    main()
