"""An event starting one of the operator's own workflows.

An event could reach a job or an outbound webhook and not the thing an operator had actually
built. Every event-driven example wanted the same missing sink: a meeting ending, a demo
request arriving, a trend detected.

Exercised against a real database, because the failure this code shape produces is silence —
a branch that never runs looks exactly like an event nobody subscribed to.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import event_dispatch as dispatch
from boss_engine.services import event_subscription_service as subs
from boss_engine.services import workflows

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"


def test_workflow_is_an_accepted_sink():
    assert "workflow" in subs.SINK_KINDS
    assert subs.validate_sink_kind("workflow") == "workflow"


@pytest.fixture()
def workflow():
    engine = create_engine(DATABASE_URL)
    key = f"event_probe_{uuid.uuid4().hex[:8]}"
    with engine.begin() as connection:
        made = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name=key, description=None,
            actor="test", namespace=workflows.PRODUCT_NAMESPACE, is_catalogue=False)
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=made["id"], actor="test",
            spec={"entry_step": "hold",
                  "steps": [{"id": "hold", "kind": "timer", "wait_seconds": 60,
                             "binding": {}, "next": []}]})
    yield engine, key
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM event_subscriptions WHERE sink_ref=:k"), {"k": key})
        connection.execute(
            text("""DELETE FROM workflow_step_runs WHERE workflow_run_id IN (
                      SELECT r.id FROM workflow_runs r
                      JOIN workflow_versions v ON v.id = r.workflow_version_id
                      WHERE v.workflow_definition_id = :i)"""), {"i": made["id"]})
        connection.execute(
            text("""DELETE FROM workflow_runs WHERE workflow_version_id IN (
                      SELECT id FROM workflow_versions WHERE workflow_definition_id = :i)"""),
            {"i": made["id"]})
        connection.execute(text("DELETE FROM workflow_definitions WHERE id=:i"),
                           {"i": made["id"]})


def _fire(connection, key, event_id: str, data: dict):
    return dispatch.dispatch_event(
        connection, event_id=event_id, project_id=PROJECT,
        event_type="meeting.completed", actor="test",
        entity_type="meeting", entity_id="m1", data=data)


def test_an_event_starts_the_workflow_and_carries_its_data(workflow):
    """The event's own data becomes the run's input.

    That is what makes "a demo request arrived" carry the company with it, rather than starting
    a workflow that then has to go and look.
    """
    engine, key = workflow
    with engine.begin() as connection:
        subs.create_event_subscription(
            connection, project_id=PROJECT, event_type="meeting.completed",
            sink_kind="workflow", sink_ref=key, actor="test")
        fired = _fire(connection, key, f"ev_{uuid.uuid4().hex[:8]}", {"company": "Acme"})

    started = [f for f in fired if f.get("kind") == "workflow"]
    assert started, f"the event did not start the workflow: {fired}"

    with engine.begin() as connection:
        run = connection.execute(
            text("SELECT input FROM workflow_runs WHERE id=:i"),
            {"i": started[0]["run_id"]},
        ).mappings().one()
    assert run["input"]["company"] == "Acme"
    assert run["input"]["event_type"] == "meeting.completed"


def test_the_same_event_delivered_twice_starts_one_run(workflow):
    """The dispatcher is retried. A meeting ending twice is not two meetings."""
    engine, key = workflow
    event_id = f"ev_{uuid.uuid4().hex[:8]}"
    with engine.begin() as connection:
        subs.create_event_subscription(
            connection, project_id=PROJECT, event_type="meeting.completed",
            sink_kind="workflow", sink_ref=key, actor="test")
        first = _fire(connection, key, event_id, {"company": "Acme"})
        second = _fire(connection, key, event_id, {"company": "Acme"})

    runs = {f["run_id"] for f in (first + second) if f.get("kind") == "workflow"}
    assert len(runs) == 1, f"one event produced {len(runs)} runs"


def test_a_subscription_pointing_at_nothing_does_not_break_the_others(workflow):
    """One misconfigured subscription must not stop the event reaching every other sink."""
    engine, key = workflow
    with engine.begin() as connection:
        subs.create_event_subscription(
            connection, project_id=PROJECT, event_type="meeting.completed",
            sink_kind="workflow", sink_ref="no_such_workflow_at_all", actor="test")
        subs.create_event_subscription(
            connection, project_id=PROJECT, event_type="meeting.completed",
            sink_kind="workflow", sink_ref=key, actor="test")
        fired = _fire(connection, key, f"ev_{uuid.uuid4().hex[:8]}", {"company": "Acme"})
        connection.execute(
            text("DELETE FROM event_subscriptions WHERE sink_ref='no_such_workflow_at_all'"))

    assert [f for f in fired if f.get("kind") == "workflow"], \
        "the good subscription was lost with the bad one"


def test_an_event_nobody_subscribed_to_starts_nothing(workflow):
    engine, key = workflow
    with engine.begin() as connection:
        fired = _fire(connection, key, f"ev_{uuid.uuid4().hex[:8]}", {"company": "Acme"})
    assert not [f for f in fired if f.get("kind") == "workflow"]


# ── An event-started run acts on the subscriber's standing grant (D2 pre-mortem) ─────────────
#
# A workflow started by an event used to start as "event-dispatcher" with no recorded
# authority, so under enforce its build was blocked "No one's authority covers this build: it
# was started by BOSS itself" (crun_9bed8a42 and two more on the laptop). The incident response
# an app's alert starts would have died at its build. A workflow sink now runs like a schedule:
# on the standing grant of the person who set it up, checked at every event.

def test_a_person_setting_up_a_workflow_sink_links_their_standing_grant(workflow, monkeypatch):
    from boss_engine.identity import consent

    engine, key = workflow
    asked = []
    monkeypatch.setattr(consent, "require_for_schedule",
                        lambda *a, **k: asked.append(k) or {"on_behalf_of": "usr_op",
                                                            "org_id": "org_1",
                                                            "grant_id": "grt_live"})
    with engine.begin() as connection:
        made = subs.create_event_subscription(
            connection, project_id=PROJECT, event_type="meeting.completed",
            sink_kind="workflow", sink_ref=key, actor="usr_op")
        listed = [s for s in subs.list_event_subscriptions(connection, PROJECT)
                  if s["id"] == made["id"]][0]
    assert asked and asked[0]["project_id"] == PROJECT
    assert (listed["on_behalf_of"], listed["grant_id"]) == ("usr_op", "grt_live")


def test_the_run_starts_under_that_grant(workflow, monkeypatch):
    from boss_engine.identity import consent, lineage
    from boss_engine.identity.lineage import Acting

    engine, key = workflow
    monkeypatch.setattr(consent, "require_for_schedule",
                        lambda *a, **k: {"on_behalf_of": "usr_op", "grant_id": "grt_live"})
    monkeypatch.setattr(consent, "schedule_acting", lambda db, row: Acting(
        principal_id=row["on_behalf_of"], org_id="org_1", grant_id=row["grant_id"]))
    seen = []
    real = workflows.start_run

    def spy(connection, **kw):
        seen.append((kw.get("actor"), lineage.current()))
        return real(connection, **kw)

    monkeypatch.setattr(workflows, "start_run", spy)
    with engine.begin() as connection:
        subs.create_event_subscription(
            connection, project_id=PROJECT, event_type="meeting.completed",
            sink_kind="workflow", sink_ref=key, actor="usr_op")
        dispatch.invalidate_subscription_cache(PROJECT)
        fired = _fire(connection, key, f"ev_{uuid.uuid4().hex[:8]}", {})
    assert [f for f in fired if f.get("kind") == "workflow" and f.get("run_id")], fired
    actor, acting = seen[0]
    assert actor == "usr_op" and acting is not None and acting.grant_id == "grt_live"


def test_an_ended_grant_turns_the_subscription_off_with_renew_authority(workflow, monkeypatch):
    from boss_engine.identity import consent

    engine, key = workflow
    monkeypatch.setattr(consent, "require_for_schedule",
                        lambda *a, **k: {"on_behalf_of": "usr_op", "grant_id": "grt_gone"})

    def lost(db, row):
        raise consent.AuthorityLost("grant_revoked")

    monkeypatch.setattr(consent, "schedule_acting", lost)
    with engine.begin() as connection:
        made = subs.create_event_subscription(
            connection, project_id=PROJECT, event_type="meeting.completed",
            sink_kind="workflow", sink_ref=key, actor="usr_op")
        dispatch.invalidate_subscription_cache(PROJECT)
        fired = _fire(connection, key, f"ev_{uuid.uuid4().hex[:8]}", {})
        row = connection.execute(text("SELECT enabled, config FROM event_subscriptions "
                                      "WHERE id = :i"), {"i": made["id"]}).mappings().one()
    assert [f for f in fired if f.get("refused")][0]["reason"] == "authority_lost:grant_revoked"
    assert row["enabled"] is False and row["config"]["last_error"].startswith("Renew authority")
