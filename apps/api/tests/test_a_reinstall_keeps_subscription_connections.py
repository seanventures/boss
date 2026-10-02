"""Re-running the installer keeps the subscriptions it connected.

`install.sh --connect-codex` recorded the Codex login; the next run of the installer (an
upgrade, or simply adding --connect-claude) re-seeded the default entitlement sources, and the
seed's upsert overwrote connection_status with "not_connected" — Setup then showed no connected
model while the credential sat untouched in the secret store (found 2026-09-30).
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.entitlements import EntitlementBrokerService

SOURCE = "openai_codex_chatgpt"


def _row(engine, project_id):
    with engine.begin() as connection:
        return connection.execute(text(
            "SELECT connection_status, status FROM entitlement_sources "
            "WHERE project_id=:p AND source_id=:s"), {"p": project_id, "s": SOURCE}).mappings().one()


def test_seeding_again_does_not_disconnect_a_connected_subscription(engine):
    settings = get_settings()
    project = settings.default_project_id
    broker = EntitlementBrokerService(settings)
    with engine.begin() as connection:
        broker.seed_default_sources(connection, project, "install")
        source = dict(connection.execute(text(
            "SELECT * FROM entitlement_sources WHERE project_id=:p AND source_id=:s"),
            {"p": project, "s": SOURCE}).mappings().one())
        broker.upsert_source(connection, {**source, "connection_status": "connected",
                                          "status": "active"}, actor="install", emit_events=False)
    assert _row(engine, project)["connection_status"] == "connected"

    with engine.begin() as connection:  # the next installer run
        broker.seed_default_sources(connection, project, "install")

    assert dict(_row(engine, project)) == {"connection_status": "connected", "status": "active"}


def test_a_real_disconnect_still_disconnects(engine):
    settings = get_settings()
    project = settings.default_project_id
    broker = EntitlementBrokerService(settings)
    with engine.begin() as connection:
        broker.seed_default_sources(connection, project, "install")
        source = dict(connection.execute(text(
            "SELECT * FROM entitlement_sources WHERE project_id=:p AND source_id=:s"),
            {"p": project, "s": SOURCE}).mappings().one())
        broker.upsert_source(connection, {**source, "connection_status": "connected"},
                             actor="install", emit_events=False)
        broker.upsert_source(connection, {**source, "connection_status": "revoked",
                                          "status": "missing_credentials"},
                             actor="operator", emit_events=False)
    assert _row(engine, project)["connection_status"] == "revoked"
