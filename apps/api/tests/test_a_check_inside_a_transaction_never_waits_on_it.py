"""A permission check made INSIDE a transaction that holds the org's audit lock never waits on that
transaction (2026-11-08, Add a project).

Create and start with a repository hung for good: its transaction wrote `project.created` (taking
the org's audit lock), then project_repos.put → setup_actions.select_repository →
github_setup.org_credentials_refusal → authz.check(db=that transaction). The check's denial was
audited on a second connection, which waited for the lock the first connection held while the
first waited for the check. Now the side write waits briefly and, when the lock is the caller's,
the event is written in the caller's own transaction.
"""
from __future__ import annotations

import concurrent.futures
import time

from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import audit, authz
from boss_engine.identity.permissions_gen import P
from boss_engine.identity.scope import ResolvedScope


def test_a_denial_inside_a_locked_transaction_is_audited_in_it_without_waiting(engine):
    world = IdentityWorld.create(engine)
    viewer = world.person("lock-viewer")
    world.bind(viewer, "viewer")
    ctx = world.ctx(viewer)
    authz._AUDIT_SEEN.clear()

    def inside() -> tuple[bool, int]:
        with engine.begin() as db:
            db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": audit._lock_key(world.org)})  # noqa: SLF001
            decision = authz.check(ctx, P.GITHUB_CONNECT, ResolvedScope.org_level(world.org), db=db)
            seen = db.execute(text(
                "SELECT count(*) FROM identity_security_events WHERE org_id = :o AND "
                "event_type = 'authz.denied' AND permission = :p"),
                {"o": world.org, "p": P.GITHUB_CONNECT.value}).scalar()
            return decision.allowed, int(seen or 0)

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        allowed, seen = pool.submit(inside).result(timeout=30)
    assert time.monotonic() - started < 15, "the check waited on its own caller's lock"
    assert allowed is False
    assert seen == 1, "the denial is audited in the caller's transaction"
    with engine.connect() as db:
        kept = db.execute(text(
            "SELECT count(*) FROM identity_security_events WHERE org_id = :o AND "
            "event_type = 'authz.denied' AND permission = :p"),
            {"o": world.org, "p": P.GITHUB_CONNECT.value}).scalar()
    assert kept == 1
