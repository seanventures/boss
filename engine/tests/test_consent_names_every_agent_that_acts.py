"""The consent sheet names EVERY agent that will act under the person's grant during the run.

The defect class (D1 HelpDesk delivery, 2026-10-10): a workflow's `build` step named no agent. The
consent at start (consent.workflow_needs) asked for the build capability's captain (Engineering
Captain); the step ran as the person, the build tool handed the code task to nobody in particular,
and the code task ran as BOSS's default builder (Forge, the shipped software_engineer). The
person consented, the meeting, PRD and architecture passed, and then the build was refused:
"grant_does_not_name_agent". Two places decided "who builds" by their own rules.

Now both ask ONE function (runs.resolved_builder), and these tests walk the RUNTIME's own path —
step_agents.resolve -> builtin._building_agent -> runs.resolved_builder -> the run token — for every
shipped workflow, as published and as a project with no captains on its steps (the D1 shape),
and require the agent it lands on to be in the consent. A run consented before this fix asks
again on Retry instead of failing the same way, and the refusal names the agent.
"""
from __future__ import annotations

import copy
import tempfile
import uuid

import pytest
from sqlalchemy import text

from boss_engine.identity import consent, grants, lineage, passports, runs, tokens
from boss_engine.identity.delegation import GrantRequest
from boss_engine.services import step_agents, workflow_catalogue, workflows

from ._identity_agents_support import session_ctx
from .test_identity_agents_workflow_consent import ALICE, FORGE, NEEDS, env  # noqa: F401  (full-schema fixture)


def _staff(env, role_keys) -> dict[str, str]:  # noqa: F811
    """Give the project an agent (with a Passport) for every role key; forge/atlas already exist."""
    out = {"forge": FORGE}
    with env.engine.begin() as c:
        for rk in sorted(set(role_keys) - {"forge", "atlas", "human", ""}):
            aid = f"agt_{rk[:10]}{uuid.uuid4().hex[:6]}"
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec, agent_id) "
                           "VALUES (:i, :p, :r, :r, '{}'::jsonb, :a)"),
                      {"i": f"role_{uuid.uuid4().hex[:8]}", "p": env.project, "r": rk, "a": aid})
            passports.ensure_passport(org_id="org_boss", agent_id=aid, role_key=rk, display_name=rk,
                                      sponsor_principal_id=ALICE, db=c)
            out[rk] = aid
    return out


def _is_build(step) -> bool:
    cap = str((step.get("binding") or {}).get("capability") or "")
    return cap in consent.BUILD_CAPABILITIES or step.get("kind") == "code_task" or consent._capability_builds(cap)  # noqa: SLF001


def _runtime_builder(c, project, step) -> str:
    """Who the build step's code task runs as, by the runtime's own path (no consent code)."""
    from boss_engine.tools.builtin import _building_agent

    ran_as = step_agents.resolve(c, None, project, step, actor=ALICE)
    agent = _building_agent(c, project, ran_as["agent"])
    return runs.resolved_builder({}, {"agent": agent} if agent else {})


def _without_captains(spec):
    spec = copy.deepcopy(spec)
    for step in spec.get("steps") or []:
        step.pop("captain", None)
        step.pop("default_captain", None)
    return spec


def test_every_shipped_build_step_consents_for_the_agent_its_build_runs_as(env):  # noqa: F811
    specs = [(e["key"], workflow_catalogue.entry_spec(e)) for e in workflow_catalogue.CATALOGUE]
    named = {step_agents.named_agent(s) for _, spec in specs for s in spec.get("steps") or []}
    _staff(env, named)
    checked = 0
    with env.engine.begin() as c:
        for key, published in specs:
            for shape, spec in (("published", published), ("no captains", _without_captains(published))):
                roles, _, _ = consent.workflow_needs(c, env.project, spec, {})
                for step in spec.get("steps") or []:
                    if not _is_build(step):
                        continue
                    builder = _runtime_builder(c, env.project, step)
                    assert builder in roles, (key, shape, step.get("id"), builder, roles)
                    checked += 1
                if any(_is_build(s) for s in spec.get("steps") or []):
                    # repository work outside a delegated chain (preview, deploy) runs as the default builder
                    assert runs.resolved_builder() in roles, (key, shape)
    assert checked >= 4


def test_a_start_card_builder_is_the_one_consented(env):  # noqa: F811
    agents = _staff(env, {"frontend_engineer"})
    spec = {"entry_step": "b", "steps": [{"id": "b", "kind": "capability",
                                          "binding": {"capability": "build", "inputs": {}}, "next": []}]}
    with env.engine.begin() as c:
        roles, _, _ = consent.workflow_needs(c, env.project, spec, {"step_agents": {"b": "frontend_engineer"}})
        as_run = step_agents.with_run_agent(c, env.project, spec["steps"][0], {"step_agents": {"b": "frontend_engineer"}})
        assert _runtime_builder(c, env.project, as_run) in roles
    assert "frontend_engineer" in roles and agents["frontend_engineer"]


def _d1_version(env):  # noqa: F811
    """The D1 shape: a build step naming nobody, the build capability's captain on the project."""
    _staff(env, {"engineering_captain"})
    spec = {"entry_step": "b", "steps": [
        {"id": "b", "kind": "capability",
         "binding": {"capability": "build", "inputs": {"title": "X", "instructions": "go"}}, "next": []}]}
    with env.engine.begin() as c:
        d = workflows.create_definition(c, project_id=env.project, workflow_key=f"d1_{uuid.uuid4().hex[:6]}",
                                        name="HelpDesk Delivery", description=None, actor="test", namespace="product")
        return workflows.publish_version(c, project_id=env.project, definition_id=d["id"], actor="test", spec=spec)


def _start(env, version_id):  # noqa: F811
    with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)):
        return workflows.start_run(c, project_id=env.project, actor="ceo",
                                   input_data={"title": "HelpDesk", "repository": "acme/web"},
                                   dedupe_key=f"dk_{uuid.uuid4().hex[:8]}", workflow_version_id=version_id)


def _grant_from(env, body, *, without=None):  # noqa: F811
    env.world.grant(ALICE, set(body["permissions"]), env.project)
    req = GrantRequest(agent_ids=tuple(a["id"] for a in body["agents"]),
                       permissions=frozenset(body["permissions"]), project_ids=tuple(body["projects"]),
                       resources={"github": {"repos": body["github"]["repos"], **body["github"]["permissions"]}},
                       task_run_id=body["task_run_id"], purpose=body["task"])
    with env.engine.begin() as c:
        g = grants.create_grant(session_ctx(ALICE), req, db=c)
        if without:
            # A grant made before the consent named this agent (as D1's was).
            c.execute(text("UPDATE identity_delegation_grants SET agent_ids = array_remove(agent_ids, :a) "
                           "WHERE id = :g"), {"a": without, "g": g.id})
        return g


def test_the_d1_build_consents_for_the_builder_and_its_run_token_is_minted(env, monkeypatch):  # noqa: F811
    from boss_engine.services import jobs

    version = _d1_version(env)
    with pytest.raises(consent.ConsentRequired) as asked:
        _start(env, version["id"])
    body = asked.value.body
    assert FORGE in [a["id"] for a in body["agents"]]          # the agent the build runs as
    _grant_from(env, body)
    run = _start(env, version["id"])
    with env.engine.begin() as c:
        job = jobs.enqueue_job(c, env.project, "code.task", {"run_id": "crun_d1", "workflow_run_id": run["id"]})
    monkeypatch.setenv("BOSS_EGRESS_URL", "https://egress.boss.internal")
    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))
    # the step ran as the person (no agent named): no finalize_ctx.agent, no spec.role_key
    shim, _ = runs.start_code_task_run(env.engine, task_run_id="crun_d1", project_id=env.project, owner="acme",
                                       repo="web", role_key=runs.code_task_builder({}, {}), job_id=job["id"])
    runs.finish_code_task_run(env.engine, shim, org_id="org_boss")


def test_a_grant_missing_the_builder_is_refused_by_name_and_retry_asks_again(env, monkeypatch):  # noqa: F811
    from boss_engine.services import jobs

    version = _d1_version(env)
    with pytest.raises(consent.ConsentRequired) as asked:
        _start(env, version["id"])
    body = asked.value.body
    # A run consented BEFORE the fix: its grant names every agent but the builder.
    old = _grant_from(env, body, without=FORGE)
    standing = _grant_from(env, {**body, "task_run_id": None})       # lets the start through
    run = _start(env, version["id"])
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET task_run_id = :r WHERE id = :g"),
                  {"r": f"run_wf:{run['id']}", "g": old.id})
        c.execute(text("UPDATE identity_delegation_grants SET status = 'revoked', revoked_at = now() WHERE id = :g"),
                  {"g": standing.id})
        c.execute(text("UPDATE workflow_runs SET grant_id = :g WHERE id = :r"), {"g": old.id, "r": run["id"]})
        job = jobs.enqueue_job(c, env.project, "code.task", {"run_id": "crun_old", "workflow_run_id": run["id"]})

    # The build under that grant is refused, and the refusal names the agent.
    monkeypatch.setenv("BOSS_EGRESS_URL", "https://egress.boss.internal")
    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))
    with pytest.raises(tokens.TokenError) as refused:
        runs.start_code_task_run(env.engine, task_run_id="crun_old", project_id=env.project, owner="acme",
                                 repo="web", role_key=None, job_id=job["id"])
    assert refused.value.reason == f"grant_does_not_name_agent:{FORGE}"
    from boss_engine.services.code_task_service import authority_refusal_words

    words = authority_refusal_words(refused.value.reason, f"Forge ({FORGE})")
    assert "Forge" in words and "consent again" in words and "Retry" in words

    # Retry asks again (the start's own sheet, naming the missing agent) ...
    with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)), pytest.raises(consent.ConsentRequired) as again:
        consent.require_for_retry(c, project_id=env.project, run_id=run["id"],
                                  spec=version["spec"], input_data={"repository": "acme/web"})
    body2 = again.value.body
    assert FORGE in [a["id"] for a in body2["agents"]] and "forge" in body2["hint"].lower()
    # ... and once granted, the run carries the covering grant.
    new = _grant_from(env, body2)
    with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)):
        acting = consent.require_for_retry(c, project_id=env.project, run_id=run["id"],
                                           spec=version["spec"], input_data={"repository": "acme/web"})
        gid = c.execute(text("SELECT grant_id FROM workflow_runs WHERE id = :r"), {"r": run["id"]}).scalar()
    assert acting.grant_id == new.id == gid
    # A grant that covers asks nothing; the worker (no session) is never asked.
    with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)):
        assert consent.require_for_retry(c, project_id=env.project, run_id=run["id"], spec=version["spec"],
                                         input_data={"repository": "acme/web"}) is None
    with env.engine.begin() as c:
        assert consent.require_for_retry(c, project_id=env.project, run_id=run["id"], spec=version["spec"],
                                         input_data={"repository": "acme/web"}) is None


def test_retry_failed_step_asks_for_consent_when_the_runs_grant_does_not_cover(env):  # noqa: F811
    version = _d1_version(env)
    with pytest.raises(consent.ConsentRequired) as asked:
        _start(env, version["id"])
    body = asked.value.body
    first = _grant_from(env, body)
    run = _start(env, version["id"])
    narrow = _grant_from(env, {**body, "task_run_id": None}, without=FORGE)
    with env.engine.begin() as c:
        c.execute(text("UPDATE identity_delegation_grants SET status = 'revoked', revoked_at = now() WHERE id = :g"),
                  {"g": first.id})
        c.execute(text("UPDATE workflow_runs SET grant_id = :g, status = 'failed' WHERE id = :r"),
                  {"g": narrow.id, "r": run["id"]})
        c.execute(text("UPDATE workflow_step_runs SET status = 'failed', last_error = 'refused' "
                       "WHERE workflow_run_id = :r AND step_id = 'b'"), {"r": run["id"]})
    with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)), pytest.raises(consent.ConsentRequired):
        workflows.retry_failed_step(c, project_id=env.project, run_id=run["id"], step_id="b", actor="ceo")
