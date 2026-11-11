"""Give a test run its own schema, so its reset can only ever reach its own tables.

The API suite clears its tables before each test to get a deterministic starting state. That is
reasonable. What was not reasonable is that it did so in whatever schema DATABASE_URL happened
to point at — and on 2026-09-02, running inside the deployed container, that was production.
204 tables.

Confinement rather than curation. The suite creates a schema that exists only for this run,
pins every connection to it, and drops it at the end. The reset statements are unchanged; they
simply cannot name production's tables any more, because those live in `public` and this run
never looks there.

Applied at IMPORT time, before `apps.api.app.main` is imported. The application builds its own
engine from DATABASE_URL on import, so anything that rewrites the variable later leaves the app
working in one schema and the tests in another.
"""

from __future__ import annotations

import atexit
import logging
import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import create_engine, text

log = logging.getLogger(__name__)


def with_search_path(url: str, schema: str) -> str:
    """Pin every connection made from `url` to `schema`.

    Carried as a libpq `options` parameter on the URL rather than through connect_args, because
    the confinement has to survive being passed as a string: anything that reads DATABASE_URL
    and builds its own engine inherits it automatically.
    """
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def confine_to_private_schema(base_url: str) -> str | None:
    """Create this run's schema, point DATABASE_URL at it, and arrange for it to be dropped.

    Returns the schema name, or None if the database is unreachable — in which case the suite
    skips anyway, and failing here would replace a clear skip with an import error.
    """
    schema = f"boss_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base_url, future=True, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    except Exception as exc:  # noqa: BLE001 — no database is a skip, not a crash
        log.warning("could not create a private test schema (%s); tests will skip", exc)
        return None
    finally:
        admin.dispose()

    os.environ["DATABASE_URL"] = with_search_path(base_url, schema)

    def _drop() -> None:
        cleanup = create_engine(base_url, future=True, isolation_level="AUTOCOMMIT")
        try:
            with cleanup.connect() as connection:
                # A connection this process leaked "idle in transaction" (it holds a lock on a
                # table in the schema) made the DROP wait forever at exit, after every test had
                # passed: an API shard of the CI p0 job hung for 18 minutes. Wait a bounded time;
                # a schema left behind is tidy-up, not a failure.
                connection.execute(text("SET lock_timeout = '20s'"))
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        except Exception:  # noqa: BLE001 — a leaked schema is tidy-up, not a failure
            log.warning("could not drop test schema %s", schema)
        finally:
            cleanup.dispose()

    # atexit rather than a fixture: the schema is created before any fixture exists, and it has
    # to go away even if collection fails.
    atexit.register(_drop)
    return schema
