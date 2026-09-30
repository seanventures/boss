#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time

from sqlalchemy import create_engine, text


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required.", file=sys.stderr)
        return 2

    deadline = time.time() + 30
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            engine = create_engine(database_url, future=True, pool_pre_ping=True)
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return 0
        except Exception as error:  # pragma: no cover - timing dependent
            last_error = error
            time.sleep(1)

    if last_error is not None:
        print(f"Postgres did not become ready: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
