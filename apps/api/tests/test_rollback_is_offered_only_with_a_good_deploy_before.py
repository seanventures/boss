"""Roll back is offered only with a good deploy before (spec §4.6, §6.3, §6.5).

`can.rollback` is decided server-side and the route refuses what `can` does not offer:
  stack     the target's latest APPLIED deployment, finished healthy or failed, with an earlier
            healthy commit different from its own (stack_deploy.rollback_option, unchanged);
  delivery  the target's newest delivery run when it failed, with an earlier succeeded run
            (rolled back by redeploying that run, which for an SSH host is a new governed plan
            a person approves — so Community offers it too);
  registry  never here (a registry run rolls back through its rollout).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.infra_seed import clear, seed

HOME = "boss-dogfood"


@pytest.fixture()
def world(engine):
    seed(engine, HOME, samples=False)
    yield
    with engine.begin() as c:
        clear(c, HOME)


def _can(client):
    rows = client.get("/v1/infra/deploys", params={"project_id": HOME}).json()["rows"]
    return {r["ref"]: r["can"]["rollback"] for r in rows}


def test_only_a_failed_newest_with_a_good_one_before(client, engine, world, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise")
    with engine.begin() as c:
        # A target whose only deploy failed: nothing good to go back to.
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            "environment, repo, target_kind, target_ref, status, trigger, created_by, "
            "finished_at) VALUES ('dr_never_fail', :p, 't_never', 'notes', 'staging', "
            "'acme/notes', 'ssh', 'x', 'failed', 'manual', 'ceo', now())"), {"p": HOME})
    can = _can(client)
    assert can["stack:sd_failed"] is True
    assert can["stack:sd_ok"] is False, "an older healthy deploy is not the latest applied"
    assert can["stack:sd_wait"] is False
    assert can["delivery:dr_down_fail"] is True
    assert can["delivery:dr_down_ok"] is False
    assert can["delivery:dr_live"] is False, "a success has nothing to roll back"
    assert can["delivery:dr_never_fail"] is False, "no good deploy before"
    assert can["registry:rr_created"] is False
    to = {r["ref"]: r["rollback_to_sha7"] for r in client.get(
        "/v1/infra/deploys", params={"project_id": HOME}).json()["rows"]}
    assert to["stack:sd_failed"] == "77aa01b" and to["delivery:dr_down_fail"] == "77aa01b"
    assert to["stack:sd_ok"] is None and to["delivery:dr_live"] is None


def test_community_rolls_a_host_back_through_a_governed_plan(client, world, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    can = _can(client)
    assert can["delivery:dr_down_fail"] is True
    assert can["delivery:dr_live"] is False, "a success has nothing to roll back"
    assert can["stack:sd_failed"] is True
    # The press plans a governed rollback; here its host (edge-2) is unreachable, so the plan is
    # refused for THAT reason — never "Paused in Community".
    r = client.post("/v1/infra/deploys/delivery:dr_down_fail/rollback",
                    json={"project_id": HOME, "actor": "ceo"})
    assert r.status_code == 409 and "not verified" in r.text, r.text
    assert "Paused in Community" not in r.text


def test_the_route_refuses_what_can_does_not_offer(client, world, monkeypatch):
    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise")
    for ref in ("stack:sd_ok", "delivery:dr_live", "registry:rr_created"):
        r = client.post(f"/v1/infra/deploys/{ref}/rollback",
                        json={"project_id": HOME, "actor": "ceo"})
        assert r.status_code == 409, (ref, r.text)


def test_a_rollback_goes_to_the_engine_of_its_kind(client, engine, world, monkeypatch):
    from boss_engine.services import delivery_pipeline, stack_deploy

    monkeypatch.setattr(api_main.settings, "boss_edition", "enterprise")
    calls = []

    def fake_stack(engine_, settings, *, deployment_id, actor):
        calls.append(("stack", deployment_id, actor))
        return {"id": "sd_new"}

    def fake_redeploy(engine_, settings, run_id, **kw):
        calls.append(("delivery", run_id, kw.get("actor"), kw.get("execute")))
        return {"deployment_run_id": "dr_new", "status": "planned", "changeset_id": "cs_x"}

    monkeypatch.setattr(stack_deploy, "rollback_stack_deploy", fake_stack)
    monkeypatch.setattr(delivery_pipeline, "redeploy", fake_redeploy)
    r = client.post("/v1/infra/deploys/stack:sd_failed/rollback",
                    json={"project_id": HOME, "actor": "ceo"})
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == "stack:sd_new"
    r = client.post("/v1/infra/deploys/delivery:dr_down_fail/rollback",
                    json={"project_id": HOME, "actor": "ceo"})
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == "delivery:dr_new" and r.json()["redeploy_of"] == "dr_down_ok"
    assert calls == [("stack", "sd_failed", "ceo"), ("delivery", "dr_down_ok", "ceo", False)]


def test_the_failed_deploy_card_rolls_back_through_the_same_rule(client, world, monkeypatch):
    from boss_engine.services import delivery_pipeline, stack_deploy

    monkeypatch.setattr(api_main.settings, "boss_edition", "community")
    monkeypatch.setattr(stack_deploy, "rollback_stack_deploy",
                        lambda e, s, *, deployment_id, actor: {"id": "sd_card"})
    planned = []
    monkeypatch.setattr(
        delivery_pipeline, "redeploy",
        lambda e, s, run_id, **kw: planned.append((run_id, kw.get("execute"))) or {
            "deployment_run_id": "dr_card", "status": "planned", "changeset_id": "cs_card"})
    fleet = client.get("/v1/infra/fleet", params={"project_id": HOME}).json()
    cards = {i["id"]: i for i in fleet["attention"]}
    stack_card = cards["infra:deploy_failed:t_stack"]
    assert [a["key"] for a in stack_card["answers"]] == ["rollback", "open"]
    # Community: a host's card rolls back too, as a governed plan to approve.
    assert [a["key"] for a in cards["infra:deploy_failed:t_down"]["answers"]] == [
        "rollback", "open"]
    r = client.post("/v1/infra/attention/act", json={
        "project_id": HOME, "actor": "ceo", "id": "infra:deploy_failed:t_stack",
        "option": "rollback"})
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == "stack:sd_card"
    r = client.post("/v1/infra/attention/act", json={
        "project_id": HOME, "actor": "ceo", "id": "infra:deploy_failed:t_down",
        "option": "rollback"})
    assert r.status_code == 200, r.text
    assert r.json()["ref"] == "delivery:dr_card"
    assert planned == [("dr_down_ok", False)], "a rollback is planned, never executed directly"
    r = client.post("/v1/infra/attention/act", json={
        "project_id": HOME, "actor": "ceo", "id": "infra:deploy_failed:t_down", "option": "open"})
    assert r.json() == {"open": "?area=infra&view=deploys&deploy=delivery:dr_down_fail"}
