"""Owned real-PG/identity transitions; fixtures are not a delivered useful result."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.tests import test_deliverable_review as review_tests
from boss_engine.identity.principal import PrincipalKind
from boss_engine.services import acceptance_templates as checks, deliverable_review as reviews

view, body = review_tests.view, review_tests.body


@pytest.fixture()
def delivery(engine):
    return review_tests.delivery.__wrapped__(engine)


@pytest.fixture()
def accepted(engine, delivery):
    world, owner, ids = delivery
    current = view(engine, world, ids)
    with engine.begin() as c:
        saved = reviews.record(c, project_id=world.project_a, run_id=ids["run"], actor=owner.id,
                               **body(current))
        # Deliberately private data must never be copied into the reusable artifact.
        c.execute(text("UPDATE code_task_runs SET spec=spec || CAST(:private AS JSONB) WHERE id=:r"),
                  {"r": ids["run"], "private": json.dumps({"private_brief": "owned-private-brief-marker"})})
    payload = {"project_id": world.project_a, "run_id": ids["run"], "review_id": saved["id"],
               "snapshot_hash": current["snapshot_hash"], "title": "CSV checks", "submission_id": uuid4().hex}
    return world, owner, ids, payload


def create(engine, accepted):
    world, owner, _, payload = accepted
    with engine.begin() as c:
        return checks.create(c, actor=owner.id, **payload)


def revision(accepted, saved, **extra):
    return {"project_id": accepted[0].project_a, "expected_revision_hash": saved["revision_hash"],
            "title": "Edited CSV checks", "criteria": ["Export selected records", "Include the header"],
            "submission_id": uuid4().hex, **extra}


def test_owner_creates_replays_edits_and_lists_bounded_immutable_checks(client, engine, accepted):
    world, owner, ids, payload = accepted
    with world.acting_as(world.ctx(owner)):
        first = client.post("/v1/acceptance-checks", json=payload)
        assert first.status_code == 200, first.text
        saved = first.json()
        again = client.post("/v1/acceptance-checks", json=payload)
        assert again.status_code == 200 and again.json()["id"] == saved["id"]
        edit = revision(accepted, saved)
        path = f"/v1/acceptance-checks/{saved['template_id']}/revisions"
        second = client.post(path, json=edit)
        assert second.status_code == 200, second.text
        assert second.json()["revision"] == 2
        assert client.post(path, json=edit).json()["id"] == second.json()["id"]
        got = client.get("/v1/acceptance-checks", params={"project_id": world.project_a})
        assert got.status_code == 200 and len(got.json()["items"]) == 1
        assert got.json()["items"][0]["criteria"] == edit["criteria"]
        assert "owned-private-brief-marker" not in got.text
        assert "receipts" not in got.text and "note" not in got.json()["items"][0]
    assert saved["source"]["review_id"] == payload["review_id"]
    with engine.begin() as c:
        rows = c.execute(text("SELECT content FROM artifacts WHERE project_id=:p AND artifact_type=:t ORDER BY created_at"),
                         {"p": world.project_a, "t": checks.TYPE}).scalars().all()
        assert len(rows) == 2 and rows[0]["criteria"] == ["Export the selected records"]
        assert rows[1]["parent_id"] == saved["id"]
        assert rows[0]["actor"] == owner.id
        assert c.execute(text("SELECT count(*) FROM events WHERE project_id=:p AND event_type='acceptance_checks.saved'"), {"p": world.project_a}).scalar() == 2
        assert c.execute(text("SELECT count(*) FROM jobs WHERE project_id=:p"), {"p": world.project_a}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM receipts WHERE project_id=:p"), {"p": world.project_a}).scalar() == 1
        assert c.execute(text("SELECT status FROM budget_reservations WHERE id=:id"), {"id": ids["reserve"]}).scalar() == "active"
        assert c.execute(text("SELECT status FROM decisions WHERE id=:id"), {"id": ids["decision"]}).scalar() == "pending"


@pytest.mark.parametrize("mutation", ["head", "checks", "outcome", "criteria"])
def test_source_changes_refuse_create_and_edit(engine, accepted, mutation):
    world, owner, ids, payload = accepted
    saved = create(engine, accepted)
    with engine.begin() as c:
        if mutation == "head":
            c.execute(text("UPDATE decisions SET recommendation=jsonb_set(recommendation,'{head_sha}',to_jsonb(CAST(:h AS TEXT))) WHERE id=:id"), {"id": ids["decision"], "h": "b" * 40})
        elif mutation == "checks":
            c.execute(text("UPDATE artifacts SET content=content || '{\"status\":\"failed\"}' WHERE id=:id"), {"id": ids["artifact"]})
        elif mutation == "criteria":
            c.execute(text("UPDATE code_task_runs SET spec=jsonb_set(spec,'{acceptance_criteria}','[\"Changed criterion\"]') WHERE id=:id"), {"id": ids["run"]})
        else:
            reviews.record(c, project_id=world.project_a, run_id=ids["run"], actor=owner.id,
                           **body(view(engine, world, ids), outcome="changes_requested", note="Revise the output"))
    with pytest.raises(checks.TemplateConflict), engine.begin() as c:
        checks.create(c, actor=owner.id, **{**payload, "submission_id": uuid4().hex})
    with pytest.raises(checks.TemplateConflict), engine.begin() as c:
        checks.revise(c, actor=owner.id, template_id=saved["template_id"], **revision(accepted, saved))


@pytest.mark.parametrize("kind", [PrincipalKind.SERVICE_ACCOUNT, PrincipalKind.AGENT, PrincipalKind.SYSTEM])
def test_machine_cannot_save_even_with_owner_role(client, engine, accepted, kind):
    world, _, _, payload = accepted
    machine = world.principal("template machine", kind=kind)
    world.bind(machine, "owner", project=world.project_a)
    with world.acting_as(world.ctx(machine)):
        got = client.post("/v1/acceptance-checks", json=payload)
    assert got.status_code == 403, got.text


def test_cross_project_viewer_and_actor_spoof_refuse(client, engine, accepted):
    world, owner, _, payload = accepted
    saved = create(engine, accepted)
    foreign = world.person("foreign template owner")
    world.bind(foreign, "owner", project=world.project_b)
    viewer = world.person("template viewer")
    world.bind(viewer, "viewer", project=world.project_a)
    with world.acting_as(world.ctx(foreign)):
        assert client.get("/v1/acceptance-checks", params={"project_id": world.project_a}).status_code in (403, 404)
        assert client.post("/v1/acceptance-checks", json=payload).status_code in (403, 404)
        # An authorized other-project body still cannot read a foreign run or artifact.
        assert client.post("/v1/acceptance-checks", json={**payload, "project_id": world.project_b}).status_code == 404
        got = client.post(f"/v1/acceptance-checks/{saved['template_id']}/revisions", json=revision(accepted, saved, project_id=world.project_b))
        assert got.status_code == 404, got.text
    with world.acting_as(world.ctx(viewer)):
        assert client.get("/v1/acceptance-checks", params={"project_id": world.project_a}).status_code == 200
        assert client.post("/v1/acceptance-checks", json=payload).status_code == 403
    with world.acting_as(world.ctx(owner)):
        assert client.post("/v1/acceptance-checks", json={**payload, "actor": foreign.id}).status_code == 422


def test_parallel_create_dedup_and_edit_revision_fence(engine, accepted):
    world, owner, _, payload = accepted
    barrier = threading.Barrier(2)
    def save(_):
        with engine.begin() as c:
            barrier.wait(timeout=10)
            return checks.create(c, actor=owner.id, **payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(save, range(2)))
    assert a["id"] == b["id"] and a["reused"] != b["reused"]
    barrier = threading.Barrier(2)
    def edit(index):
        try:
            with engine.begin() as c:
                barrier.wait(timeout=10)
                return checks.revise(c, actor=owner.id, template_id=a["template_id"],
                                     **revision(accepted, a, title=f"Concurrent {index}"))
        except checks.TemplateConflict:
            return "stale"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(edit, range(2)))
    assert sum(item == "stale" for item in outcomes) == 1


@pytest.mark.parametrize("criteria", [[], [""], ["x" * 501], ["x"] * 21, ["a\x00b"], ["x" * 500] * 13])
def test_edit_content_bounds_refuse_without_revision(engine, accepted, criteria):
    saved = create(engine, accepted)
    with pytest.raises(ValueError), engine.begin() as c:
        checks.revise(c, actor=accepted[1].id, template_id=saved["template_id"],
                      **revision(accepted, saved, criteria=criteria))
    with engine.begin() as c:
        assert checks.list_latest(c, accepted[0].project_a)[0]["revision"] == 1


def test_conflicting_submission_and_audit_failure_do_not_write(engine, accepted, monkeypatch):
    world, owner, _, payload = accepted
    saved = create(engine, accepted)
    with pytest.raises(checks.TemplateConflict), engine.begin() as c:
        checks.create(c, actor=owner.id, **{**payload, "title": "Conflicting replay"})
    def fail_audit(*args, **kwargs):
        raise RuntimeError("owned audit refusal")
    monkeypatch.setattr(checks, "emit_event", fail_audit)
    with pytest.raises(RuntimeError), engine.begin() as c:
        checks.revise(c, actor=owner.id, template_id=saved["template_id"], **revision(accepted, saved))
    with engine.begin() as c:
        assert checks.list_latest(c, world.project_a)[0]["revision"] == 1


def test_template_needs_current_accepted_review_and_recorded_criteria(engine, delivery):
    world, owner, ids = delivery
    current = view(engine, world, ids)
    payload = {"project_id": world.project_a, "run_id": ids["run"], "review_id": "not-reviewed",
               "snapshot_hash": current["snapshot_hash"], "title": "Checks", "submission_id": uuid4().hex}
    with pytest.raises(checks.TemplateConflict), engine.begin() as c:
        checks.create(c, actor=owner.id, **payload)
    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET spec=spec-'acceptance_criteria' WHERE id=:r"), {"r": ids["run"]})
    current = view(engine, world, ids)
    with engine.begin() as c:
        review = reviews.record(c, project_id=world.project_a, run_id=ids["run"], actor=owner.id, **body(current))
    with pytest.raises(ValueError), engine.begin() as c:
        checks.create(c, actor=owner.id, **{**payload, "review_id": review["id"], "snapshot_hash": current["snapshot_hash"]})


def test_latest_edited_template_stays_visible_beyond_100_lexical_ids(engine, accepted):
    world, owner, _, _ = accepted
    saved = create(engine, accepted)
    with engine.begin() as c:
        # Owned list fixtures: 100 stable IDs sort before the real saved template.
        source = {"run_id": accepted[2]["run"], "review_id": accepted[3]["review_id"],
                  "snapshot_hash": accepted[3]["snapshot_hash"], "delivered_head": "a" * 40,
                  "check_state": "passed"}
        for i in range(100):
            content = {"template_id": f"a_fixture_{i:03}", "revision": 1, "title": "Older owned checks",
                       "criteria": ["Owned criterion"], "source": source, "actor": owner.id}
            c.execute(text("INSERT INTO artifacts(id,project_id,artifact_type,title,content,content_hash,created_at) "
                           "VALUES(:id,:p,:t,'Older owned checks',CAST(:body AS JSONB),'fixture',now()-interval '1 day')"),
                      {"id": content["template_id"], "p": world.project_a, "t": checks.TYPE,
                       "body": json.dumps(content)})
        revised = checks.revise(c, actor=owner.id, template_id=saved["template_id"], **revision(accepted, saved))
        result = checks.list_latest(c, world.project_a)
        assert len(result) == 100
        assert result[0]["template_id"] == saved["template_id"]
        assert result[0]["revision_hash"] == revised["revision_hash"]
        assert sum(row["template_id"] == saved["template_id"] for row in result) == 1


@pytest.mark.parametrize("bad_title", ["owned\x00name", "owned\nname"])
def test_title_control_character_is_api_422_without_artifact(client, engine, accepted, bad_title):
    world, owner, _, payload = accepted
    with world.acting_as(world.ctx(owner)):
        response = client.post("/v1/acceptance-checks", json={**payload, "title": bad_title})
    assert response.status_code == 422, response.text
    with engine.begin() as c:
        assert checks.list_latest(c, world.project_a) == []
