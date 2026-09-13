"""The audit panel's counts, without the 192 MB export.

The console's audit panel shows eight integers. It got them from /v1/audit/export, which
serialises every task, job, decision, artifact, receipt, reservation, routing log and event
in the project so the caller can call len() on each list. Measured on the live deployment:
191,937,957 bytes in 62.1 seconds — and it was the whole reason the Ops area took ~25s to
render, because every other feed on that page answers in about 0.22s.

/v1/audit/summary counts in SQL instead. These tests pin the two things that make that
substitution safe: the numbers must be the same ones, and asking for them must not write an
audit record claiming an export was taken.
"""

from sqlalchemy import text

from apps.api.app import main as api_main


def test_summary_reports_the_same_counts_as_an_unscoped_export(client):
    project_id = "boss-dogfood"
    # Summary first, deliberately. The export emits `audit.export_generated` after building
    # its payload, so exporting first makes the events count legitimately differ by one — the
    # export would be measuring the trail as it was before it wrote to it. Reading the cheap
    # side first compares the same moment.
    summary = client.get("/v1/audit/summary", params={"project_id": project_id})
    assert summary.status_code == 200
    export = client.get("/v1/audit/export", params={"project_id": project_id})
    assert export.status_code == 200

    exported = export.json()["counts"]
    counted = summary.json()["counts"]
    # Every key, not a sampled few: a count that silently drifts is worse than a slow one,
    # because the panel keeps rendering and nobody looks again.
    assert set(counted) == set(exported)
    assert counted == exported
    assert summary.json()["project_id"] == project_id
    assert summary.json()["generated_at"]


def test_summary_covers_every_collection_the_export_counts(client):
    """The map lives next to export(); this fails if a collection is added to one only."""
    export = client.get("/v1/audit/export", params={"project_id": "boss-dogfood"})
    assert set(api_main.audit.COUNT_TABLES) == set(export.json()["counts"])


def test_counting_is_not_exporting(client, engine):
    """Opening Ops must not write `audit.export_generated`.

    export() emits that event whenever an actor is supplied, and the console supplied one on
    every page view — so merely looking at the Ops area recorded an export in the audit trail.
    A summary is a read.
    """
    project_id = "boss-dogfood"
    with engine.begin() as connection:
        before = connection.execute(
            text(
                "SELECT COUNT(*) FROM events WHERE project_id = :p "
                "AND event_type = 'audit.export_generated'"
            ),
            {"p": project_id},
        ).scalar()

    assert client.get(
        "/v1/audit/summary", params={"project_id": project_id}
    ).status_code == 200

    with engine.begin() as connection:
        after = connection.execute(
            text(
                "SELECT COUNT(*) FROM events WHERE project_id = :p "
                "AND event_type = 'audit.export_generated'"
            ),
            {"p": project_id},
        ).scalar()
    assert after == before


def test_summary_is_gated_exactly_like_the_export(client):
    """Same information, so the same gate. A cheaper route must not be a looser one.

    Asserted as a comparison rather than against a fixed status, because the point is that
    the two routes agree — whatever `require_project_actor` decides, it must decide alike.
    """
    params = {"project_id": "boss-dogfood", "actor": "not-a-member"}
    summary = client.get("/v1/audit/summary", params=params)
    export = client.get("/v1/audit/export", params=params)
    assert summary.status_code == export.status_code
    assert summary.status_code != 200
    # An unknown project is deliberately not asserted here: `require_project_actor` tries to
    # seed default members for it and dies on the projects foreign key before either handler
    # is reached. That predates this endpoint and both routes share it.
