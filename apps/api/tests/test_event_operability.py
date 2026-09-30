from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services.canaries import (
    _canary_reactive_dispatch_exactly_once,
)


PROJECT_ID = "boss-dogfood"


def test_reactive_dispatch_canary_proves_dedupe_and_leased_completion(engine):
    ok, detail = _canary_reactive_dispatch_exactly_once(
        engine, get_settings(), PROJECT_ID
    )

    assert ok is True, detail
    assert "created_once=true" in detail
    assert "replay_reused=true" in detail
    assert "disabled_ignored=true" in detail
    with engine.begin() as connection:
        event_id = detail.split("event=", 1)[1].split(" ", 1)[0]
        jobs = connection.execute(
            text(
                """
                SELECT status, payload
                FROM jobs
                WHERE project_id = :project_id
                  AND payload->>'event_id' = :event_id
                """
            ),
            {"project_id": PROJECT_ID, "event_id": event_id},
        ).mappings().all()
        subscriptions = connection.execute(
            text(
                """
                SELECT count(*)
                FROM event_subscriptions
                WHERE project_id = :project_id
                  AND sink_ref LIKE 'canary.reactive_dispatch.%'
                """
            ),
            {"project_id": PROJECT_ID},
        ).scalar_one()

    assert len(jobs) == 1
    assert jobs[0]["status"] == "succeeded"
    assert subscriptions == 0
