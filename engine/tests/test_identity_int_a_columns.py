"""int-a · Who work acts for lives in real columns (IDENTITY.md §7.14 R8, §7.3 3, §7.11).

Jobs carry `on_behalf_of`, `grant_id` and `chain`; a workflow run carries its authority in
`on_behalf_of` / `grant_id` / `authority_org_id` / `authority_run_id`; a schedule links its standing
grant in `on_behalf_of` / `grant_id`. The JSON copies (`payload._identity`, `policy_snapshot._identity`,
`input._identity`) are read only for rows written before the columns. Each test fails if the
protection it names is removed.

Identity-only throwaway database (BOSS_IDENTITY_TEST_DATABASE_URL), as the other S3 suites.
"""
from __future__ import annotations

from boss_engine.config import get_settings
import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.identity import cascade, consent, lineage
from boss_engine.identity.delegation import CascadeTrigger

from ._identity_agents_support import ROOT, build_env, session_ctx


@pytest.fixture()
def env(monkeypatch):
    return build_env(monkeypatch)


def _insert_job(c, job_id: str, payload: dict, columns: dict, project: str = "prj_web") -> None:
    c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, on_behalf_of, grant_id, chain) "
                   "VALUES (:i, :p, 'code.task', CAST(:pl AS JSONB), :on_behalf_of, :grant_id, CAST(:chain AS JSONB))"),
              {"i": job_id, "p": project, "pl": json.dumps(payload), **columns})


# ---- every job enqueue path stamps the row ----------------------------------------------------------------

def _functions_with_job_inserts() -> list[tuple[str, str, ast.AST]]:
    found = []
    for base in (ROOT / "engine" / "boss_engine", ROOT / "apps"):
        for path in base.rglob("*.py"):
            if "/tests/" in str(path) or "node_modules" in str(path):
                continue
            source = path.read_text(encoding="utf-8")
            if "INSERT INTO jobs" not in source:
                continue
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = ast.get_source_segment(source, node) or ""
                    inner = [n for n in ast.walk(node) if n is not node
                             and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    if "INSERT INTO jobs" in body and not any(
                            "INSERT INTO jobs" in (ast.get_source_segment(source, n) or "") for n in inner):
                        found.append((str(path.relative_to(ROOT)), node.name, body))
    return found


def test_every_insert_into_jobs_goes_through_the_stamping_path_and_writes_the_columns():
    sites = _functions_with_job_inserts()
    # enqueue_job, a2a dispatch, comms delivery, and the two infra droplet jobs (at least).
    assert len(sites) >= 5, sites
    for path, name, body in sites:
        if path.endswith("identity/lineage.py"):
            continue
        assert "stamp_job_row" in body, f"{path}:{name} inserts a job without lineage.stamp_job_row (R8)"
        for column in (":on_behalf_of", ":grant_id", "CAST(:chain AS JSONB)", ":on_behalf_system"):
            assert column in body, f"{path}:{name} does not write the job's {column} column"


def test_stamp_job_row_never_keeps_a_payload_identity_and_never_leaves_a_job_unattributed(monkeypatch):
    who = lineage.Acting(principal_id="usr_alice", org_id="org_boss", grant_id="dlg_1",
                         chain=("agt_forge#run_ct:1",), run_id="run_ct:1")
    payload, cols = lineage.stamp_job_row({"x": 1, "_identity": {"on_behalf_of": "usr_root"}}, who)
    assert cols == {"on_behalf_of": "usr_alice", "grant_id": "dlg_1", "chain": '["agt_forge#run_ct:1"]',
                    "on_behalf_system": False}
    assert payload["_identity"]["on_behalf_of"] == "usr_alice"
    for mode in ("shadow", "enforce"):
        monkeypatch.setattr(get_settings(), "authz_mode", mode)
        # Outside any request or run (a system thread, a webhook): BOSS's own work, said so.
        payload, cols = lineage.stamp_job_row({"_identity": {"on_behalf_of": "usr_root", "grant_id": "dlg_x"}})
        assert cols == {"on_behalf_of": "sys_worker", "grant_id": None, "chain": "[]", "on_behalf_system": True}
        assert payload["_identity"]["on_behalf_of"] == "sys_worker" and payload["_identity"]["system"] is True


def test_under_enforce_a_background_enqueue_is_system_and_a_request_enqueue_carries_the_person(monkeypatch):
    from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus

    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    _, background = lineage.stamp_job_row({"tick": 1})
    assert background["on_behalf_of"] == "sys_worker" and background["on_behalf_system"] is True
    alice = AuthContext(Principal("usr_alice", PrincipalKind.USER, "org_boss", PrincipalStatus.ACTIVE, "A"),
                        AuthMethod.SESSION)
    import contextvars

    def request():                     # what the guard does for every authenticated request
        lineage.enter(alice)
        return lineage.stamp_job_row({"click": 1})

    payload, cols = contextvars.copy_context().run(request)
    assert cols["on_behalf_of"] == "usr_alice" and cols["on_behalf_system"] is False
    assert payload["_identity"]["org_id"] == "org_boss" and "system" not in payload["_identity"]
    acting = lineage.acting_for_job({"payload": {}, "on_behalf_of": "sys_worker", "on_behalf_system": True,
                                     "grant_id": None, "chain": []})
    assert acting.system is True and acting.principal_id == "sys_worker" and acting.grant_id is None
    with pytest.raises(lineage.PrincipalRequired):            # a row with no principal at all: fail closed
        lineage.acting_for_job({"payload": {}})


# ---- the run path reads the columns ------------------------------------------------------------------------

def test_a_leased_job_acts_for_its_columns_not_for_its_payload(env):
    who = lineage.Acting(principal_id=env.alice, org_id="org_boss", grant_id="dlg_a", chain=("agt_forge#run_ct:1",),
                         run_id="run_ct:1")
    payload, cols = lineage.stamp_job_row({"run_id": "crun_1"}, who)
    with env.db() as c:
        _insert_job(c, "job_cols", payload, cols)
        # Something rewrites the payload copy: the row's columns still decide.
        c.execute(text("UPDATE jobs SET payload = jsonb_set(payload, '{_identity}', CAST(:f AS JSONB)) WHERE id = 'job_cols'"),
                  {"f": json.dumps({"on_behalf_of": "usr_root", "org_id": "org_boss", "grant_id": "dlg_forged"})})
        acting = lineage.acting_for_job_id(c, "job_cols")
    assert acting.principal_id == env.alice and acting.grant_id == "dlg_a" and acting.chain == ("agt_forge#run_ct:1",)
    assert acting.run_id is None and acting.jti is None            # nothing taken from a payload that disagrees


def test_a_job_written_before_the_columns_still_reads_its_payload(env):
    with env.db() as c:
        _insert_job(c, "job_old", {"_identity": {"on_behalf_of": env.alice, "org_id": "org_boss", "grant_id": "dlg_o",
                                                 "chain": [], "run_id": "run_ct:9"}},
                    {"on_behalf_of": None, "grant_id": None, "chain": None})
        acting = lineage.acting_for_job_id(c, "job_old")
    assert acting.principal_id == env.alice and acting.grant_id == "dlg_o" and acting.run_id == "run_ct:9"


# ---- the cascade finds work by its columns -----------------------------------------------------------------

def test_revoking_a_grant_cancels_queued_jobs_halts_runs_and_pauses_schedules_by_their_columns(env):
    g = env.grant()
    other = env.grant()
    with env.db() as c:
        _insert_job(c, "job_g", {"k": 1}, {"on_behalf_of": env.alice, "grant_id": g.id, "chain": "[]"})
        _insert_job(c, "job_other", {"k": 1}, {"on_behalf_of": env.alice, "grant_id": other.id, "chain": "[]"})
        # A column says another grant; a stale payload copy names this one: the column decides.
        _insert_job(c, "job_stale", {"_identity": {"on_behalf_of": env.alice, "grant_id": g.id}},
                    {"on_behalf_of": env.alice, "grant_id": other.id, "chain": "[]"})
        _insert_job(c, "job_legacy", {"_identity": {"on_behalf_of": env.alice, "grant_id": g.id}},
                    {"on_behalf_of": None, "grant_id": None, "chain": None})
        c.execute(text("INSERT INTO workflow_runs (id, status, on_behalf_of, grant_id, authority_org_id, authority_run_id) "
                       "VALUES ('wr_cols', 'running', :a, :g, 'org_boss', 'run_wf:wr_cols')"), {"a": env.alice, "g": g.id})
        c.execute(text("INSERT INTO workflow_schedules (id, on_behalf_of, grant_id) VALUES ('sch_cols', :a, :g)"),
                  {"a": env.alice, "g": g.id})
        c.execute(text("INSERT INTO workflow_schedules (id, on_behalf_of, grant_id, input) VALUES "
                       "('sch_stale', :a, :o, CAST(:i AS JSONB))"),
                  {"a": env.alice, "o": other.id, "i": json.dumps({"_identity": {"grant_id": g.id}})})
    with env.db() as c:
        report = cascade.revoke_cascade(CascadeTrigger.GRANT_REVOKED, g.id, actor=session_ctx(env.alice),
                                        reason="done", db=c)
        status = dict(c.execute(text("SELECT id, status FROM jobs")).all())
        run_status = c.execute(text("SELECT status FROM workflow_runs WHERE id = 'wr_cols'")).scalar()
        enabled = dict(c.execute(text("SELECT id, enabled FROM workflow_schedules")).all())
    assert status == {"job_g": "cancelled", "job_other": "queued", "job_stale": "queued", "job_legacy": "cancelled"}
    assert "run_wf:wr_cols" in report.runs_halted and run_status == "paused"
    assert set(report.schedules_paused) == {"sch_cols"} and enabled == {"sch_cols": False, "sch_stale": True}


# ---- a workflow run's authority --------------------------------------------------------------------------

def test_a_workflow_runs_authority_is_its_columns_and_old_runs_fall_back_to_the_snapshot(env):
    g = env.grant()
    assert consent.record_on_run({"_identity": {"grant_id": "dlg_forged"}, "x": 1},
                                 lineage.Acting(env.alice, "org_boss", g.id)) == {"x": 1}
    cols = consent.authority_columns(lineage.Acting(env.alice, "org_boss", g.id, run_id="run_wf:wr_a"))
    assert cols == {"on_behalf_of": env.alice, "grant_id": g.id, "authority_org_id": "org_boss",
                    "authority_run_id": "run_wf:wr_a"}
    with env.db() as c:
        c.execute(text("INSERT INTO workflow_runs (id, status, policy_snapshot, on_behalf_of, grant_id, authority_org_id, "
                       "authority_run_id) VALUES ('wr_a', 'running', CAST(:s AS JSONB), :on_behalf_of, :grant_id, "
                       ":authority_org_id, :authority_run_id)"),
                  {"s": json.dumps({"_identity": {"on_behalf_of": "usr_mallory", "org_id": "org_other",
                                                  "grant_id": "dlg_forged", "run_id": "run_wf:wr_a"}}), **cols})
        c.execute(text("INSERT INTO workflow_runs (id, status, policy_snapshot) VALUES ('wr_old', 'running', "
                       "CAST(:s AS JSONB))"),
                  {"s": json.dumps({"_identity": {"on_behalf_of": env.alice, "org_id": "org_boss", "grant_id": g.id,
                                                  "run_id": "run_wf:wr_old"}})})
        new = consent.acting_for_workflow_run(c, "wr_a")
        old = consent.acting_for_workflow_run(c, "wr_old")
    assert (new.principal_id, new.grant_id, new.run_id, new.org_id) == (env.alice, g.id, "run_wf:wr_a", "org_boss")
    assert (old.principal_id, old.grant_id, old.run_id) == (env.alice, g.id, "run_wf:wr_old")


def test_a_schedule_link_is_its_columns_and_an_old_schedule_falls_back_to_its_input(env):
    g = env.grant()
    with env.db() as c:
        new = consent.schedule_acting(c, {"on_behalf_of": env.alice, "grant_id": g.id,
                                          "input": {"_identity": {"on_behalf_of": env.alice, "grant_id": "dlg_x"}}})
        old = consent.schedule_acting(c, {"on_behalf_of": None, "grant_id": None,
                                          "input": {"_identity": {"on_behalf_of": env.alice, "grant_id": g.id}}})
        with pytest.raises(consent.AuthorityLost):
            consent.schedule_acting(c, {"on_behalf_of": "usr_mallory", "grant_id": g.id, "input": {}})
    assert new.grant_id == g.id and old.grant_id == g.id and new.principal_id == env.alice


def test_the_schema_has_the_columns():
    sql = (Path(ROOT) / "db" / "schema.sql").read_text(encoding="utf-8")
    for stmt in ("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS on_behalf_of", "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS grant_id",
                 "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS chain JSONB",
                 "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS grant_id",
                 "ALTER TABLE workflow_schedules ADD COLUMN IF NOT EXISTS grant_id"):
        assert stmt in sql


# ---- the ledger keeps what the token SAID -------------------------------------------------------------------

def test_the_claims_view_shows_the_minted_claims_not_todays_live_intersection(env):
    from boss_engine.identity import claims_view, grants, tokens

    from ._identity_agents_support import start, verify

    g = env.grant()
    shim = start(env, g.id, run="run_ct:said", scope=("code_task:read", "code_task:steer", "repository:read"))
    said = verify(env, shim)
    with env.db() as c:
        task, scope, stored = c.execute(text("SELECT task, scope, claims FROM identity_run_credentials WHERE jti = :j"),
                                        {"j": said.jti}).one()
    assert sorted(scope) == sorted(said.scope) and task["run"] == "run_ct:said" and task["project"] == env.project
    assert stored["cnf"] == {"jkt": "[redacted]"} and shim.key.jkt not in json.dumps(stored)
    assert shim.tokens["boss:api"].access_token not in json.dumps(stored)
    # Alice loses code_task:steer: live authority narrows at once; what the token SAID does not change.
    env.world.revoke(env.alice, {"code_task:steer"}, env.project)
    with env.db() as c:
        view = claims_view.for_grant(grants.load(g.id, db=c), run_id="run_ct:said", db=c)
    assert view["scope_source"] == "token"
    assert view["claims"]["scope"] == " ".join(sorted(said.scope)) and "code_task:steer" in view["token_scope"]
    assert "code_task:steer" not in view["live_scope"] and view["claims"]["jti"] == said.jti
    assert view["claims"]["cnf"] == {"jkt": tokens.REDACTED_CLAIM}
