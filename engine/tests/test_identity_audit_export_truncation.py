"""export() must say when its row cap cut the bundle.

The query returns at most `limit` rows (default 100_000). A bundle of exactly
that many is not a proof the range was whole. Fewer rows means the cap did
not cut it. The hash chain is unchanged: a cap is not a break.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

from boss_engine.identity import audit
from boss_engine.identity.audit import Outcome, SecurityEvent
from engine.tests.identity_s5_support import (  # noqa: F401  (db, s5_engine fixtures)
    ORG,
    community_install,
    db,
    fake_claim,
    s5_engine,
    settings,
)

NOT_WHOLE = "A truncated bundle is not a proof the range was whole."
UNDER_LIMIT = "This bundle returned fewer rows than the limit, so the cap did not cut the range."
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _claimed(db):
    community_install(db)
    return fake_claim(db, name="Param", email="param@example.com", via="setup_token", now=NOW,
                      cfg=settings())


def _event(actor: str, i: int) -> SecurityEvent:
    return SecurityEvent(org_id=ORG, event_type="user.updated", outcome=Outcome.SUCCESS,
                         actor_principal_id=actor, actor_kind="user", target_id=f"usr_{i}")


def _chains_from_genesis(bundle: dict, db) -> None:
    prev = audit.GENESIS_HASH
    key = audit.chain_key(ORG, db=db)
    for row in bundle["events"]:
        stored = dict(row, ts=datetime.fromisoformat(row["ts"]))
        alg = row["hash_alg"]
        digest = audit.chain_hash(prev, stored, alg=alg, key=key if alg == 2 else None)
        assert digest.hex() == row["hash"]
        prev = bytes.fromhex(row["hash"])


def test_export_says_when_the_row_cap_cut_the_range(db):
    owner = _claimed(db)
    for i in range(6):
        audit.record(_event(owner.principal_id, i), db=db)

    assert inspect.signature(audit.export).parameters["limit"].default == 100_000

    capped = audit.export(ORG, db=db, ctx=owner.ctx, limit=3)
    assert len(capped["events"]) == 3
    assert capped["truncated"] is True
    assert capped["notice"] == NOT_WHOLE
    assert capped["verification"]["ok"] is True
    assert capped["anchor"]["seq"] > capped["events"][-1]["seq"]
    _chains_from_genesis(capped, db)

    whole = audit.export(ORG, db=db, ctx=owner.ctx, limit=100)
    assert len(whole["events"]) < 100
    assert [e["seq"] for e in whole["events"][:3]] == [e["seq"] for e in capped["events"]]
    assert whole["truncated"] is False
    assert whole["notice"] == UNDER_LIMIT
    assert whole["verification"]["ok"] is True
    assert whole["anchor"]["seq"] == whole["events"][-1]["seq"]

    defaulted = audit.export(ORG, db=db, ctx=owner.ctx)
    assert len(defaulted["events"]) < 100_000
    assert defaulted["truncated"] is False
    assert defaulted["notice"] == UNDER_LIMIT
