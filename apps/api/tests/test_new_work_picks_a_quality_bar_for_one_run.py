"""New work picks a quality bar for one run (BOSSAPP-1362 phase 2): the same or a stricter bar is
anyone's who may start the work; a bar below the project's needs settings:update. Real Postgres,
identity enforced.

POST /v1/projects/{id}/work sends `quality_bar`; the start door (start_delivery_workflow) checks it
with quality_bar.lowers and stamps it on the run (`_quality_bar_by`), so every build reads it.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.test_new_work_goes_through_the_one_start_door import (  # noqa: F401 — fixtures
    _consent, _missing, client, project, world,
)


def _start(client, project, body):  # noqa: F811
    """Through consent and readiness, as the console does (the start door's own answers)."""
    r = client.post(f"/v1/projects/{project}/work", json=body)
    for _ in range(4):
        if r.status_code == 200:
            break
        try:
            consented = _consent(client, r)
        except AssertionError:
            # An agent named in the consent body was re-seeded meanwhile (unknown_agent):
            # ask again, as the console's consent sheet does.
            consented = True
        if consented:
            r = client.post(f"/v1/projects/{project}/work", json=body)
            continue
        missing = _missing(r)
        if not missing:
            break
        body = {**body, "start_anyway": missing}
        r = client.post(f"/v1/projects/{project}/work", json=body)
    return r


def _start_only(world, project, name):  # noqa: F811
    person = world.person(name)
    world.bind(person, world.custom_role(f"{name}-role", ("workflow_run:start", "project:read")),
               project=project)
    return person


def _stamped(engine, run_id):
    with engine.begin() as db:
        return db.execute(text("SELECT input FROM workflow_runs WHERE id = :i"),
                          {"i": run_id}).scalar() or {}


def test_a_lower_bar_needs_settings_update(world, client, project):  # noqa: F811
    starter = _start_only(world, project, "qb-start-only")
    with world.acting_as(world.ctx(starter)):
        refused = client.post(f"/v1/projects/{project}/work",
                              json={"kind": "feature", "text": "Add a route.",
                                    "quality_bar": "prototype"})
    assert refused.status_code == 403, refused.text
    assert "settings:update" in refused.text


def test_a_stricter_bar_is_anyones_who_may_start_and_is_stamped(world, client, engine, project,  # noqa: F811
                                                                 monkeypatch):
    from boss_engine.identity import grants

    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: False)
    with world.acting_as(world.ctx(world.owner)):
        r = _start(client, project, {"kind": "feature", "text": "Add a route.",
                                     "quality_bar": "production"})
    assert r.status_code == 200, r.text
    stamped = _stamped(engine, r.json()["run_id"])
    assert stamped.get("quality_bar") == "production"
    assert stamped.get("_quality_bar_by")
    starter = _start_only(world, project, "qb-start-strict")
    with world.acting_as(world.ctx(starter)):
        r = client.post(f"/v1/projects/{project}/work",
                        json={"kind": "feature", "text": "Add a stricter route.",
                              "quality_bar": "production"})
    # Consent or readiness may still answer (the start door's own); never the bar's refusal.
    assert not (r.status_code == 403 and "quality bar" in r.text), r.text


def test_an_owner_may_lower_it_and_a_word_that_is_no_bar_is_refused(world, client, engine, project,  # noqa: F811
                                                                    monkeypatch):
    from boss_engine.identity import grants

    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: False)
    with world.acting_as(world.ctx(world.owner)):
        lowered = _start(client, project, {"kind": "feature", "text": "Add a lower route.",
                                           "quality_bar": "prototype"})
        bad = client.post(f"/v1/projects/{project}/work",
                          json={"kind": "feature", "text": "Add a route.", "quality_bar": "yolo"})
    assert lowered.status_code == 200, lowered.text
    assert _stamped(engine, lowered.json()["run_id"]).get("quality_bar") == "prototype"
    assert bad.status_code == 422, bad.text
    assert "not a quality bar" in bad.text


def test_the_projects_gate_levels_are_a_setting_with_the_floor_refused(world, client, project):  # noqa: F811
    with world.acting_as(world.ctx(world.owner)):
        ok = client.put("/v1/settings/quality_bar_gates",
                        json={"project_id": project, "value": {"code_review_passed": "follow_up"}})
        floor = client.put("/v1/settings/quality_bar_gates",
                           json={"project_id": project, "value": {"security_clean": "follow_up"}})
        cleared = client.put("/v1/settings/quality_bar_gates",
                             json={"project_id": project, "value": None})
    assert ok.status_code == 200, ok.text
    assert ok.json()["value"] == {"code_review_passed": "follow_up"}
    assert floor.status_code == 422 and "always blocks" in floor.text, floor.text
    assert cleared.status_code == 200, cleared.text
    starter = _start_only(world, project, "qb-start-gates")
    with world.acting_as(world.ctx(starter)):
        refused = client.put("/v1/settings/quality_bar_gates",
                             json={"project_id": project, "value": {"consistent": "block"}})
    assert refused.status_code == 403, refused.text


def test_new_works_plan_offers_the_projects_bar_in_words(world, client, project):  # noqa: F811
    with world.acting_as(world.ctx(world.owner)):
        client.put("/v1/settings/quality_bar", json={"project_id": project, "value": "production"})
        r = client.get(f"/v1/projects/{project}/work/plan?kind=feature&detail=true")
        client.put("/v1/settings/quality_bar", json={"project_id": project, "value": None})
    assert r.status_code == 200, r.text
    qb = r.json()["defaults"]["quality_bar"]
    assert qb["bar"] == "production"
    assert [p["key"] for p in qb["presets"]] == ["prototype", "standard", "production"]
    assert "approval" in qb["always"]
