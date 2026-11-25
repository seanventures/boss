# ruff: noqa: F811 — the shared suite's `env` fixture is imported and used by name
"""Every build a person starts gets its delivery workflow, under the ONE consent they gave.

2026-11-19, the operator on Recipe box: "I don't see any workflow getting created ... did we also
skip workflow??" Easy Mode builds (Neighborhood board, Invoice desk, Tile swap) had no
workflow_runs row. The person consented "For this build only" (a grant bound to run_ct:<build>);
the outer run then asked for its OWN consent (scope workflow_run), the 409 was raised inside the
build's start, the retry "without authority" raised it again in the same person's session, and a
broad `except` logged it. The build ran with no workflow: no deploy, no smoke test, no monitoring,
and nobody was told.

The operator's decision: starting a project's delivery workflow never needs its own approval.
The consent given at the start covers the whole delivery workflow; only merge, production,
budget and permission changes ask a person. And a workflow that truly cannot start is a card on
the build, never a log line.

Against the full schema (BOSS_IDENTITY_FULL_TEST_DATABASE_URL), reusing the workflow-consent
suite's world.
"""
from __future__ import annotations

import json
import tempfile
import uuid

import pytest
from sqlalchemy import text

from boss_engine.identity import consent, grants, lineage, runs
from boss_engine.identity.delegation import GrantRequest

from ._identity_agents_support import ISSUER, session_ctx  # noqa: F401
from .test_identity_agents_workflow_consent import ALICE, FORGE, _workflow, env  # noqa: F401

BOB = "usr_bob_wf"


@pytest.fixture(autouse=True)
def _tidy(env):  # noqa: F811 — the shared suite's fixture; this removes what these tests add
    yield
    with env.engine.begin() as c:
        for table in ("decisions", "code_task_runs"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": env.project})


def _needs(c, project, version_id):
    spec = c.execute(text("SELECT spec FROM workflow_versions WHERE id = :v"), {"v": version_id}).scalar()
    _, needs, _ = consent.workflow_needs(c, project, spec, {"repository": "acme/web"})
    return set(needs) | set(consent.code_task_permissions(["acme/web"])) | {"preview:manage"}


def _build_grant(env, needs, task_run_id):
    env.world.grant(ALICE, needs, env.project)
    req = GrantRequest(agent_ids=(FORGE,), permissions=frozenset(needs), project_ids=(env.project,),
                       resources={"github": {"repos": ["acme/web"], "contents": "write",
                                             "pull_requests": "write"}},
                       task_run_id=task_run_id, purpose="Recipe box")
    with env.engine.begin() as c:
        return grants.create_grant(session_ctx(ALICE), req, db=c)


def _build_row(c, project, crun, *, title="Recipe box"):
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, finalize_ctx) "
                   "VALUES (:i, :p, :t, 'running', 'queued', CAST(:s AS JSONB), '{}'::jsonb)"),
              {"i": crun, "p": project, "t": title,
               "s": json.dumps({"owner": "acme", "repo": "web", "instructions": "a recipe box"})})


def test_a_this_build_only_grant_carries_the_build_into_its_workflow_without_asking_again(env, monkeypatch):
    from boss_engine.services import jobs, workflows

    crun = f"crun_{uuid.uuid4().hex[:10]}"
    with env.engine.begin() as c:
        _, version = _workflow(c, env.project)
        needs = _needs(c, env.project, version["id"])
    g = _build_grant(env, needs, grants.PENDING_PREFIX + uuid.uuid4().hex[:8])
    with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)):
        # The person's "For this build only" is claimed by the build, as start_code_task does.
        build = consent.require_authority(org_id=None, project_id=env.project, role_keys=["forge"],
                                          permissions=frozenset(needs), repos=["acme/web"],
                                          task="Recipe box", run_id=f"run_ct:{crun}", db=c)
        assert build is not None and build.grant_id == g.id
        with lineage.acting(build):
            job = jobs.enqueue_job(c, env.project, "code.task", {"run_id": crun})
        # Without the build's authority, this person's session is asked again (the old behaviour).
        with pytest.raises(consent.ConsentRequired):
            with c.begin_nested():
                workflows.start_run(c, project_id=env.project, actor="boss",
                                    input_data={"title": "Recipe box", "repository": "acme/web"},
                                    dedupe_key=f"dk_{uuid.uuid4().hex[:8]}",
                                    workflow_version_id=version["id"])
        # With it: no second consent. The run records the build's person and grant.
        run = workflows.start_run(c, project_id=env.project, actor="boss",
                                  input_data={"title": "Recipe box", "repository": "acme/web"},
                                  dedupe_key=f"dk_{uuid.uuid4().hex[:8]}",
                                  workflow_version_id=version["id"], inherit_authority=build)
    wf = f"run_wf:{run['id']}"
    with env.engine.begin() as c:
        assert grants.load(g.id, db=c).task_run_id == wf              # the grant follows the workflow
        acting = consent.acting_for_workflow_run(c, run["id"])
        assert acting is not None and acting.principal_id == ALICE and acting.grant_id == g.id
        stamped = c.execute(text("SELECT payload->'_identity'->>'run_id' FROM jobs WHERE id = :j"),
                            {"j": job["id"]}).scalar()
        assert stamped == wf                                          # the build is now the run's
    # The build mints its token as a child of the workflow run, under the same grant...
    monkeypatch.setenv("BOSS_EGRESS_URL", "https://egress.boss.internal")
    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))
    shim, _ = runs.start_code_task_run(env.engine, task_run_id=crun, project_id=env.project,
                                       owner="acme", repo="web", role_key="forge", job_id=job["id"])
    assert shim.run_id == f"{wf}/ct:{crun}"
    # ...and the grant does NOT end when the build does: deploy, smoke and monitoring come after.
    runs.finish_code_task_run(env.engine, shim, org_id="org_boss")
    with env.engine.begin() as c:
        assert grants.is_live(grants.load(g.id, db=c))
        assert consent.acting_for_workflow_run(c, run["id"]) is not None


def test_someone_elses_build_is_never_inherited(env):
    from boss_engine.services import workflows

    with env.engine.begin() as c:
        _, version = _workflow(c, env.project)
        needs = _needs(c, env.project, version["id"])
    g = _build_grant(env, needs, None)
    alices_build = lineage.Acting(principal_id=ALICE, org_id="org_boss", grant_id=g.id,
                                  run_id="run_ct:crun_alice")
    with env.engine.begin() as c:
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES (:u, 'org_boss', 'user', 'active', 'Bob') ON CONFLICT DO NOTHING"), {"u": BOB})
    with env.engine.begin() as c, lineage.acting(session_ctx(BOB)), pytest.raises(consent.ConsentRequired):
        workflows.start_run(c, project_id=env.project, actor="boss",
                            input_data={"title": "Recipe box", "repository": "acme/web"},
                            dedupe_key=f"dk_{uuid.uuid4().hex[:8]}", workflow_version_id=version["id"],
                            inherit_authority=alices_build)


def test_a_workflow_that_cannot_start_is_a_card_on_the_build_not_a_log_line(env, monkeypatch):
    from boss_engine.services import code_task_service as cts
    from boss_engine.services import workflows

    crun = f"crun_{uuid.uuid4().hex[:10]}"
    with env.engine.begin() as c:
        _build_row(c, env.project, crun)

    def refused(*_a, **_k):
        raise consent.ConsentRequired({"error": "consent_required", "scope": "workflow_run"})

    monkeypatch.setattr(workflows, "start_project_run", refused)
    with env.engine.begin() as c:
        assert cts.start_delivery_workflow(
            c, project_id=env.project, task_id=None, run_id=crun, title="Recipe box",
            instructions="a recipe box", repo="web", finalize_ctx={}) is None
        # The build's own transaction is still usable: the start of the build carries on.
        assert c.execute(text("SELECT 1")).scalar() == 1
    with env.engine.begin() as c:
        err = c.execute(text("SELECT finalize_ctx->'workflow_start_error' FROM code_task_runs WHERE id = :r"),
                        {"r": crun}).scalar()
        assert err and err["consent_needed"] is True and err["decision_id"]
        card = c.execute(text("SELECT question, recommendation, status FROM decisions WHERE id = :d"),
                         {"d": err["decision_id"]}).mappings().one()
        assert card["question"] == "BOSS needs your OK to run Recipe box's delivery workflow"
        assert card["recommendation"]["action"] == cts.WORKFLOW_NOT_STARTED_ACTION
        assert card["recommendation"]["run_id"] == crun
        assert c.execute(text("SELECT count(*) FROM events WHERE project_id = :p "
                              "AND event_type = 'code_task.workflow_not_started'"),
                         {"p": env.project}).scalar() == 1
    # A second failure for the same build joins the open card instead of raising another.
    with env.engine.begin() as c:
        cts.start_delivery_workflow(c, project_id=env.project, task_id=None, run_id=crun,
                                    title="Recipe box", instructions="a recipe box", repo="web",
                                    finalize_ctx={})
        assert c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p "
                              "AND recommendation->>'run_id' = :r"),
                         {"p": env.project, "r": crun}).scalar() == 1


def test_the_card_starts_the_workflow_and_adopts_the_build(env, monkeypatch):
    """Recovery: attach_delivery_workflow (what approving the card runs) gives a build that ran
    without its outer run one now, adopting it, and records it on the build."""
    from boss_engine.services import code_task_service as cts
    from boss_engine.services import workflows

    crun = f"crun_{uuid.uuid4().hex[:10]}"
    with env.engine.begin() as c:
        _, version = _workflow(c, env.project)
        _build_row(c, env.project, crun)
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = jsonb_build_object('workflow', "
                       "jsonb_build_object('version_id', CAST(:v AS text)), 'workflow_start_error', "
                       "jsonb_build_object('reason', 'x')) WHERE id = :r"), {"v": version["id"], "r": crun})
    seen = {}
    real = workflows.start_project_run

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(workflows, "start_project_run", spy)
    with env.engine.begin() as c:
        run = cts.attach_delivery_workflow(c, crun)
    assert run and run.get("id")
    assert seen["extra_input"]["adopt_code_task_run_id"] == crun
    assert seen["workflow_version_id"] == version["id"]
    with env.engine.begin() as c:
        fctx = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id = :r"), {"r": crun}).scalar()
        assert fctx["workflow_run_id"] == run["id"] and "workflow_start_error" not in fctx
        assert cts.attach_delivery_workflow(c, crun) is None          # once attached, never twice


def test_approving_the_card_is_carried_out_not_just_recorded():
    from boss_engine.services import decision_actions

    assert "start_delivery_workflow" in decision_actions.CARRIED_OUT_ON_APPROVAL


def test_the_one_consent_names_the_workflow_and_says_what_it_covers():
    """start_code_task asks for the build AND its delivery workflow in one sheet, and the sheet
    says so in one line; the outer run is started with the build's authority to inherit."""
    import inspect

    from boss_engine.services import code_task_service as cts

    src = inspect.getsource(cts.start_code_task)
    assert "_delivery_workflow_needs(" in src and "covers=_covers" in src
    assert "inherit_authority=identity_acting" in inspect.getsource(cts.start_delivery_workflow)
    assert "Merging to main, production deploys" in consent.DELIVERY_COVERS
    assert cts._will_start_outer_run({}) is True
    assert cts._will_start_outer_run({"parent_run_id": "crun_x"}) is False
    assert cts._will_start_outer_run({"workflow_run_id": "wfr_x"}) is False
    assert cts._will_start_outer_run({"origin": {"workflow_run_id": "wfr_x"}}) is False
    assert cts._will_start_outer_run({"parent_run_id": "crun_x", "workflow_version_id": "v"}) is True


def test_an_easy_mode_start_asks_once_and_its_workflow_run_adopts_the_build(env):
    """The whole path, as Easy Mode drives it: start the build in the person's session, get ONE
    consent sheet (naming the workflow, saying what it covers), answer "For this build only",
    start again — the build starts AND its delivery workflow run exists, adopting it, under that
    same grant. No second consent, no log line."""
    from boss_engine.config import get_settings
    from boss_engine.services import code_task_service as cts
    from boss_engine.services.jobs import JobLeaseService

    with env.engine.begin() as c:
        _, version = _workflow(c, env.project)
        needs = _needs(c, env.project, version["id"])
    env.world.grant(ALICE, needs, env.project)
    jobs = JobLeaseService(get_settings())

    def start(c):
        return cts.start_code_task(
            c, project_id=env.project, task_id=None, title="Recipe box", instructions="a recipe box",
            acceptance_criteria=[], test_commands=[], owner="acme", repo="web", base_branch="main",
            finalize_ctx={"workflow_version_id": version["id"]}, jobs=jobs)

    with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)), \
            pytest.raises(consent.ConsentRequired) as asked:
        start(c)
    body = asked.value.body
    assert body.get("scope") is None                       # the build's sheet, not a workflow's
    assert body["covers"] == consent.DELIVERY_COVERS
    assert body["durations"][0]["label"] == "For this build only"
    req = GrantRequest(agent_ids=tuple(a["id"] for a in body["agents"]),
                       permissions=frozenset(body["permissions"]), project_ids=(env.project,),
                       resources={"github": {"repos": ["acme/web"], "contents": "write",
                                             "pull_requests": "write"}},
                       task_run_id=body["task_run_id"], purpose="Recipe box")
    with env.engine.begin() as c:
        g = grants.create_grant(session_ctx(ALICE), req, db=c)
    with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)):
        crun = start(c)
    with env.engine.begin() as c:
        fctx = c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id = :r"), {"r": crun}).scalar()
        wfr = fctx.get("workflow_run_id")
        assert wfr, fctx.get("workflow_start_error")
        row = c.execute(text("SELECT grant_id, on_behalf_of, authority_run_id FROM workflow_runs WHERE id = :w"),
                        {"w": wfr}).one()
        assert row == (g.id, ALICE, f"run_wf:{wfr}")
        assert grants.load(g.id, db=c).task_run_id == f"run_wf:{wfr}"
        run_input = c.execute(text("SELECT input FROM workflow_runs WHERE id = :w"), {"w": wfr}).scalar()
        assert run_input["adopt_code_task_run_id"] == crun                # the build step waits on it
