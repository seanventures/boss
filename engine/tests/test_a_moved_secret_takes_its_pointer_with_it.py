"""When the chosen backend changes, the row that POINTS at the secret has to change too.

THE THIRD PASTE. The operator entered their Google OAuth client for the third time. The save
returned 200. The value went safely into Postgres — the durability fix was working. And the
console still reported "configured but it could not be read: ... is not present in local_vault",
because `save_oauth_client` updated the metadata and the status of the existing `secret_refs`
row and left `ref_kind` and `storage_backend` untouched.

`resolve_secret_ref` dispatches on exactly those two columns. So the value was in the new store
and the pointer still named the old one, and every read looked in the empty place.

Writing a secret somewhere and recording somewhere else is worse than either failing outright:
the write succeeds, the caller is told it succeeded, and the failure surfaces later somewhere
that looks unrelated.
"""
import json
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import channel_oauth

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"
REDIRECT = "https://example.test/v1/channels/youtube/callback"


@pytest.fixture()
def db():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        yield connection


def _row(db):
    return db.execute(
        text("SELECT ref_kind, storage_backend, status FROM secret_refs "
             "WHERE project_id = :p AND ref_key = :k"),
        {"p": PROJECT, "k": channel_oauth._client_ref_key(PROJECT)}).mappings().first()


@needs_db
def test_saving_again_under_a_different_backend_moves_the_pointer(db, monkeypatch):
    settings = Settings()
    stores: dict[str, dict[str, str]] = {"local_vault": {}, "database": {}}

    class Fake:
        def __init__(self, kind): self.kind = kind
        def put(self, key, value): stores[self.kind][key] = value
        def get(self, key): return stores[self.kind][key]

    monkeypatch.setattr(channel_oauth, "backend_for", lambda kind, _s: Fake(kind))

    # FIRST SAVE, on the old install where local_vault was chosen.
    monkeypatch.setattr(channel_oauth, "durable_secret_backend", lambda _s: "local_vault")
    channel_oauth.save_oauth_client(
        db, settings, project_id=PROJECT, actor="ceo",
        client_id="first.apps.googleusercontent.com", client_secret="s1",
        redirect_uri=REDIRECT)
    assert _row(db)["ref_kind"] == "local_vault"

    # SECOND SAVE, after the durability fix moved the choice to the database.
    monkeypatch.setattr(channel_oauth, "durable_secret_backend", lambda _s: "database")
    channel_oauth.save_oauth_client(
        db, settings, project_id=PROJECT, actor="ceo",
        client_id="second.apps.googleusercontent.com", client_secret="s2",
        redirect_uri=REDIRECT)

    row = _row(db)
    # THE LINE THAT WAS MISSING. Both columns, because resolve_secret_ref reads ref_kind and
    # callers that inspect storage report storage_backend; disagreeing is its own bug.
    assert row["ref_kind"] == "database", "the pointer stayed on the store the value left"
    assert row["storage_backend"] == "database"
    assert row["status"] == "active"

    # And the value really is in the new store, under the key the row names.
    key = channel_oauth._client_ref_key(PROJECT)
    assert json.loads(stores["database"][key])["client_id"] == "second.apps.googleusercontent.com"


@needs_db
def test_saving_a_client_clears_a_revocation(db, monkeypatch):
    """Entering a client is the opposite of revoking one, and a row still carrying `revoked_at`
    reads as revoked while holding a live secret."""
    settings = Settings()
    monkeypatch.setattr(channel_oauth, "backend_for",
                        lambda kind, _s: type("F", (), {"put": lambda self, k, v: None})())
    monkeypatch.setattr(channel_oauth, "durable_secret_backend", lambda _s: "database")

    channel_oauth.save_oauth_client(
        db, settings, project_id=PROJECT, actor="ceo",
        client_id="x.apps.googleusercontent.com", client_secret="s", redirect_uri=REDIRECT)
    db.execute(
        text("UPDATE secret_refs SET status='revoked', revoked_at=now(), revoked_by='ceo' "
             "WHERE project_id=:p AND ref_key=:k"),
        {"p": PROJECT, "k": channel_oauth._client_ref_key(PROJECT)})

    channel_oauth.save_oauth_client(
        db, settings, project_id=PROJECT, actor="ceo",
        client_id="y.apps.googleusercontent.com", client_secret="s", redirect_uri=REDIRECT)

    row = db.execute(
        text("SELECT status, revoked_at FROM secret_refs WHERE project_id=:p AND ref_key=:k"),
        {"p": PROJECT, "k": channel_oauth._client_ref_key(PROJECT)}).mappings().first()
    assert row["status"] == "active" and row["revoked_at"] is None
