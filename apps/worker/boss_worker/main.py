import argparse
import logging
import time

from sqlalchemy import create_engine

from apps.api.app.db import database_is_reachable

from .config import get_worker_settings


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
    args = parser.parse_args()

    settings = get_worker_settings()
    configure_logging()

    engine = create_engine(settings.database_url, future=True, pool_pre_ping=True)
    db_ready = database_is_reachable(engine)
    LOGGER.info(
        "worker_started worker_id=%s database_connected=%s github_configured=%s openai_configured=%s",
        settings.worker_id,
        db_ready,
        bool(settings.github_token),
        bool(settings.openai_api_key),
    )

    if args.once:
        return

    while True:
        LOGGER.info("worker_idle worker_id=%s", settings.worker_id)
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()

