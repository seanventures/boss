"""A host deploy asks a person once — or not at all where the environment's policy says so.

With no policy (or requires_approval on), an Infra deploy opens exactly one decision and waits.
With staging set to deploy without asking, it opens none: its ChangeSet is approved by
"policy:<id>", its apply is queued at once, and an event names the policy and who set it — the
ChangeSet, reservation and receipt are all still there.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests.test_a_host_deploys_only_through_an_approved_changeset import (  # noqa: F401
    HOME, _governance, _job_for, hosted,
)


def _decisions_for(engine, run_id):
    with engine.begin() as c:
        return c.execute(text("SELECT count(*) FROM decisions WHERE recommendation->>"
                              "'deployment_run_id' = :r"), {"r": run_id}).scalar()


def test_an_infra_deploy_with_no_policy_asks_exactly_once(client, engine, hosted):  # noqa: F811
    r = client.post("/v1/delivery/deploy", json={"target_id": hosted["target_id"],
                                                  "actor": "ceo", "ref": "main"})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["next_action"] == "approve_decision" and plan["decision_id"]
    assert _decisions_for(engine, plan["deployment_run_id"]) == 1
    _cs, _res, _rc, run = _governance(engine, plan)
    assert run["status"] == "awaiting_approval"


def test_a_staging_policy_off_deploys_without_a_card_and_says_whose_rule(client, engine, hosted):  # noqa: F811
    set_ = client.put("/v1/deployment-environment-policies/staging/approval",
                      json={"project_id": HOME, "actor": "ceo", "requires_approval": False,
                            "note": "set at the operator's request"})
    assert set_.status_code == 200, set_.text
    policy = set_.json()
    assert policy["requires_approval"] is False and policy["set_by"]
    try:
        got = client.get("/v1/deployment-environment-policies/staging/approval",
                         params={"project_id": HOME}).json()
        assert got["policy_id"] == policy["policy_id"]
        r = client.post("/v1/delivery/deploy", json={"target_id": hosted["target_id"],
                                                      "actor": "ceo", "ref": "main"})
        assert r.status_code == 200, r.text
        plan = r.json()
        run_id = plan["deployment_run_id"]
        assert _decisions_for(engine, run_id) == 0
        with engine.begin() as c:
            run = c.execute(text("SELECT status, decision_id, changeset_id, job_id FROM "
                                 "deployment_runs WHERE id = :i"), {"i": run_id}).mappings().one()
            changeset = c.execute(text("SELECT status, approved_by, reservation_id FROM "
                                       "changesets WHERE id = :c"),
                                  {"c": run["changeset_id"]}).mappings().one()
            said = c.execute(text("SELECT data FROM events WHERE event_type = "
                                  "'deployment.approved_without_asking' AND entity_id = :i"),
                             {"i": run_id}).scalar_one()
        assert run["status"] == "approved" and run["job_id"] and run["decision_id"] is None
        assert changeset["status"] == "approved" and changeset["reservation_id"]
        assert changeset["approved_by"] == f"policy:{policy['policy_id']}"
        assert said["source"] == "policy" and said["policy_id"] == policy["policy_id"]
        assert said["policy_set_by"] == policy["set_by"]
        assert _job_for(engine, run_id)["job_id"] == run["job_id"]
        assert hosted["host"].commands == []          # queued, not run inside the request
        # Production is untouched: it still asks.
        prod = client.get("/v1/deployment-environment-policies/production/approval",
                          params={"project_id": HOME}).json()
        assert prod["requires_approval"] is True
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM deployment_environment_policies WHERE project_id = :p"),
                      {"p": HOME})
