"""Reconnecting a channel must work more than once, and it never did.

WHAT THE OPERATOR SAW, after completing Google's consent screen:

    IntegrityError: duplicate key value violates unique constraint
    "secret_refs_project_id_ref_kind_ref_key_key"
    DETAIL: Key (project_id, ref_kind, ref_key)=(boss-dogfood, local_vault,
            channel/boss-dogfood/youtube_default/refresh_token) already exists.

`channel_oauth.complete()` stored the refresh token with a plain INSERT, and `ref_key` is
derived from the project and the connection key — so it could succeed exactly ONCE per channel,
for the lifetime of that channel. The console's Reconnect button carries a comment calling this
"the weekly path", because a Google testing-mode refresh token expires after seven days. It had
never once worked on a channel that was already connected.

The token itself was already overwritten in the secret backend by then. Only the row describing
it refused to be written twice.
"""
import json
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.core import make_id

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"

#: Exactly the statement `complete()` runs, so this test and the product cannot disagree.
UPSERT = """
    INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key,
                             provider, secret_kind, storage_backend, status, metadata,
                             created_by)
    VALUES (:id, :p, 'project', :p, :kind, :ref_key, 'google', 'oauth_refresh_token',
            :kind, 'active', CAST(:meta AS JSONB), :actor)
    ON CONFLICT (project_id, ref_kind, ref_key) DO UPDATE
       SET status = 'active',
           storage_backend = EXCLUDED.storage_backend,
           secret_kind = EXCLUDED.secret_kind,
           provider = EXCLUDED.provider,
           metadata = EXCLUDED.metadata,
           revoked_at = NULL,
           revoked_by = NULL,
           updated_at = now()
    RETURNING id
"""


@pytest.fixture()
def db():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        yield connection


def _store(db, ref_key, *, actor="ceo"):
    return str(db.execute(text(UPSERT), {
        "id": make_id("secref"), "p": PROJECT, "kind": "local_vault", "ref_key": ref_key,
        "meta": json.dumps({"provider": "google"}), "actor": actor}).scalar())


@needs_db
def test_storing_the_same_channels_token_twice_reuses_its_row(db):
    ref_key = f"channel/{PROJECT}/test_reconnect_{make_id('x')[-6:]}/refresh_token"
    try:
        first = _store(db, ref_key)
        # THE LINE THAT USED TO RAISE. A second authorisation of the same channel.
        second = _store(db, ref_key, actor="someone_else")
        assert first == second, (
            "a reconnect wrote a NEW secret_ref id; integration_connections would then point at "
            "a row that does not hold the token")

        rows = db.execute(
            text("SELECT status, revoked_at FROM secret_refs WHERE project_id=:p AND ref_key=:k"),
            {"p": PROJECT, "k": ref_key}).mappings().all()
        assert len(rows) == 1, "a channel must not accumulate a secret_ref per reconnect"
        assert rows[0]["status"] == "active"
    finally:
        db.execute(text("DELETE FROM secret_refs WHERE project_id=:p AND ref_key=:k"),
                   {"p": PROJECT, "k": ref_key})


@needs_db
def test_reconnecting_a_revoked_channel_brings_it_back(db):
    """Disconnect marks the row revoked. Re-authorising is the opposite of revoking, and a row
    still carrying `revoked_at` would read as revoked while holding a live token."""
    ref_key = f"channel/{PROJECT}/test_revoked_{make_id('x')[-6:]}/refresh_token"
    try:
        _store(db, ref_key)
        db.execute(
            text("UPDATE secret_refs SET status='revoked', revoked_at=now(), revoked_by='ceo' "
                 "WHERE project_id=:p AND ref_key=:k"),
            {"p": PROJECT, "k": ref_key})

        _store(db, ref_key)

        row = db.execute(
            text("SELECT status, revoked_at, revoked_by FROM secret_refs "
                 "WHERE project_id=:p AND ref_key=:k"),
            {"p": PROJECT, "k": ref_key}).mappings().first()
        assert row["status"] == "active"
        assert row["revoked_at"] is None and row["revoked_by"] is None
    finally:
        db.execute(text("DELETE FROM secret_refs WHERE project_id=:p AND ref_key=:k"),
                   {"p": PROJECT, "k": ref_key})


def test_the_product_runs_the_upsert_this_test_pins():
    """A test asserting on its own copy of the SQL proves nothing about the product."""
    import inspect

    from boss_engine.services import channel_oauth

    source = inspect.getsource(channel_oauth.complete)
    assert "ON CONFLICT (project_id, ref_kind, ref_key) DO UPDATE" in source
    assert "RETURNING id" in source
    assert "revoked_at = NULL" in source
