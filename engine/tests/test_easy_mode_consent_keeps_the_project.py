"""EASY MODE ASKS FOR CONSENT AND THEN BUILDS IN THE SAME PROJECT.

THE CASE (2026-11-17, release validation, identity enforced): Command Center › "CSV quality desk"
› Easy Mode answered 409 consent_required. The launch made the project, seeded its role agents
and started the build in ONE transaction, so the ConsentRequired rolled all of it back: the agents
the consent body named no longer existed, the next press made a different project and asked
again, forever.

Now the start alone is undone (a savepoint), the project, its agents and Passports and its
workflows are kept with one in-progress receipt, and the 409 names the project. The same launch
sent again after the grant (the console re-sends it unchanged) builds in THAT project:

  - the first launch answers 409 with `project_id`, and the project and its agents exist after it;
  - a real grant for the agents the body named, then the same launch: the build starts in the
    same project under that grant;
  - exactly one project and one receipt for the launch afterwards;
  - another retry is idempotent (`reused`, no second build).

Against the full schema (BOSS_IDENTITY_FULL_TEST_DATABASE_URL); the consent is the real
require_authority under enforced identity; only the code-task start around it is a stand-in.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import consent, grants, lineage
from boss_engine.identity.delegation import GrantRequest
from boss_engine.services import factory_launch

from ._identity_agents_support import session_ctx
from .test_identity_agents_workflow_consent import ALICE, NEEDS, env  # noqa: F401  (full-schema fixture)


@pytest.fixture(autouse=True)
def _local_repos_in_tmp(monkeypatch, tmp_path):
    """Each project's local repository is made under this test's folder, never the install's."""
    monkeypatch.setattr(get_settings(), "local_repo_root", str(tmp_path / "repos"))


@pytest.fixture(autouse=True)
def _a_model_is_connected(monkeypatch):
    """The builder here is a stand-in that needs no model; the model-first check is not what
    these consent tests exercise."""
    from boss_engine.services import model_readiness

    monkeypatch.setattr(model_readiness, "usable_model", lambda *a, **kw: True)


class _Budget:
    """Reserves nothing real: the reservation's own release is the savepoint's (asserted by the
    API suite against the real governor)."""

    def __init__(self) -> None:
        self.reserved: list[str] = []

    def reserve(self, connection, project_id, **_kw):
        rid = f"res_{uuid.uuid4().hex[:8]}"
        self.reserved.append(project_id)
        return {"status": "reserved", "reservation_id": rid}


def _resources(body):
    """As the consent sheet sends them (lib/identity/consent.ts grantBodyFor): the run's
    repositories on this machine as `local`."""
    local = (body.get("local") or {}).get("repos") or []
    return {"local": {"repos": list(local)}} if local else {}


def _template(tmp_path, template_id: str):
    root = tmp_path / "factory_templates"
    root.mkdir()
    (root / "csv.yaml").write_text(
        f"id: {template_id}\ntitle: CSV quality desk\nblurb: Check a CSV.\ncategory: app\n"
        "prompt: Build a CSV quality desk.\nstack: vite-react\ntoolchain: node\nresources: []\n"
        "workflows:\n  - name: Build the desk\n    steps:\n      - Read the CSV\n",
        encoding="utf-8")
    return root


def _builder(starts: list):
    """The code-task start's consent, as start_code_task asks it (the resolved builder, the
    code-task permissions, the run's own id), then a run id."""

    def build(connection, *, project_id, spec, actor, settings, jobs, reservation_id):
        run_id = f"crun_{uuid.uuid4().hex[:8]}"
        acting = consent.require_authority(
            org_id=None, project_id=project_id, role_keys=[consent._resolved_builder()],  # noqa: SLF001
            permissions=consent.CODE_TASK_PERMISSIONS,
            repos=[spec.get("github_repo") or spec.get("local_repo")], task=str(spec.get("title")),
            run_id=f"run_ct:{run_id}", db=connection)
        starts.append({"project_id": project_id, "run_id": run_id,
                       "grant_id": acting.grant_id if acting else None})
        return {"project_id": project_id, "run_id": run_id}

    return build


def _launch(env, root, template_id, starts, budget, who=ALICE):  # noqa: F811
    """As the API route runs it: one transaction, committed even when consent is asked."""
    with lineage.acting(session_ctx(who)), env.engine.begin() as c:
        try:
            return factory_launch.launch(
                c, get_settings(), org_id="org_boss", actor="ceo", auth=session_ctx(who),
                template_id=template_id, template_dir=root, builder=_builder(starts),
                budget=budget)
        except consent.ConsentRequired as asked:
            return {"__consent__": asked.body}


def _cleanup(env, project_ids, dedupe_key):  # noqa: F811
    with env.engine.begin() as c:
        c.execute(text("SET LOCAL session_replication_role = replica"))
        c.execute(text("DELETE FROM receipts WHERE dedupe_key = :k"), {"k": dedupe_key})
        for pid in project_ids:
            for table in ("events", "workflow_versions", "workflow_definitions", "roles",
                          "project_settings", "resource_quotas"):
                try:
                    with c.begin_nested():
                        c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
                except Exception:  # noqa: BLE001 — a table this schema does not have
                    pass
            c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def test_consent_keeps_the_project_and_the_retry_builds_in_it(env, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(lineage, "_enforced", lambda: True)
    monkeypatch.setattr(consent, "_first_owner", lambda db, org: ALICE)
    template_id = f"csv-desk-{uuid.uuid4().hex[:6]}"
    root = _template(tmp_path, template_id)
    starts: list = []
    budget = _Budget()
    made: set[str] = set()
    dedupe_key = ""
    try:
        first = _launch(env, root, template_id, starts, budget)
        asked = first.get("__consent__")
        assert asked, f"the first launch must ask for consent, got {first}"
        assert asked["error"] == "consent_required"
        project_id = asked["project_id"]
        dedupe_key = asked["dedupe_key"]
        made.add(project_id)
        assert asked["retry"] == {"project_id": project_id, "dedupe_key": dedupe_key}
        assert asked["task_run_id"].startswith(grants.PENDING_PREFIX)
        assert asked["projects"] == [project_id]
        # The chip names the project's real repository on this machine, never a placeholder.
        assert asked["local"]["repos"] == [f"local_{project_id}/csv-quality-desk"]
        assert starts == []                                   # no build started
        agent_ids = [a["id"] for a in asked["agents"]]
        assert agent_ids
        with env.engine.connect() as c:
            # KEPT: the project and the agents the body names exist after the 409.
            assert c.execute(text("SELECT count(*) FROM projects WHERE id = :p"),
                             {"p": project_id}).scalar() == 1
            assert c.execute(text("SELECT count(*) FROM roles WHERE project_id = :p AND agent_id = ANY(:a)"),
                             {"p": project_id, "a": agent_ids}).scalar() == len(agent_ids)
            assert c.execute(text("SELECT count(*) FROM identity_agent_passports WHERE agent_id = ANY(:a)"),
                             {"a": agent_ids}).scalar() == len(agent_ids)
            receipt = c.execute(text("SELECT status, project_id FROM receipts WHERE dedupe_key = :k"),
                                {"k": dedupe_key}).mappings().all()
        assert [dict(r) for r in receipt] == [{"status": "in_progress", "project_id": project_id}]

        # A second press before consenting asks again for the SAME project, never a new one.
        again = _launch(env, root, template_id, starts, budget)
        assert again["__consent__"]["project_id"] == project_id
        assert [a["id"] for a in again["__consent__"]["agents"]] == agent_ids

        # The person consents, as the sheet does: a grant for exactly what the body asked.
        env.world.grant(ALICE, set(NEEDS) | {"delegation:grant"}, project_id)
        body = again["__consent__"]
        with env.engine.begin() as c:
            grant = grants.create_grant(session_ctx(ALICE), GrantRequest(
                agent_ids=tuple(a["id"] for a in body["agents"]),
                permissions=frozenset(body["permissions"]), project_ids=(project_id,),
                task_run_id=body["task_run_id"], purpose=body["task"],
                resources=_resources(body)), db=c)

        built = _launch(env, root, template_id, starts, budget)
        assert "__consent__" not in built, built
        assert built["project_id"] == project_id                # the SAME project
        assert built["run_id"] and built["reused"] is False
        assert [s["project_id"] for s in starts] == [project_id]
        assert starts[0]["grant_id"] == grant.id                 # under the new grant

        # Idempotent: the same launch again is the same build, nothing new.
        repeat = _launch(env, root, template_id, starts, budget)
        assert repeat["reused"] is True and repeat["run_id"] == built["run_id"]
        assert repeat["project_id"] == project_id
        assert len(starts) == 1

        with env.engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM projects WHERE name = 'CSV quality desk' "
                                  "AND id = ANY(:ids)"), {"ids": list(made)}).scalar() == 1
            # Every project a launch of this template made: exactly one.
            slug = factory_launch._project_slug("CSV quality desk", dedupe_key)  # noqa: SLF001
            assert c.execute(text("SELECT count(*) FROM projects WHERE slug LIKE :s"),
                             {"s": slug + "%"}).scalar() == 1
            rows = c.execute(text("SELECT status, project_id, result->>'run_id' AS run_id "
                                  "FROM receipts WHERE dedupe_key = :k"), {"k": dedupe_key}).mappings().all()
        assert [dict(r) for r in rows] == [
            {"status": "succeeded", "project_id": project_id, "run_id": built["run_id"]}]
        assert built["receipt_id"] == repeat["receipt_id"]
    finally:
        if dedupe_key:
            _cleanup(env, made, dedupe_key)


BOB = "usr_bob_easy"


def test_two_people_pressing_the_same_template_never_share_a_project(env, monkeypatch, tmp_path):  # noqa: F811
    """A's launch waits for A's consent; B presses the same template and gets B's own project,
    asked of B, never A's kept one (nor A's run once A has built)."""
    monkeypatch.setattr(lineage, "_enforced", lambda: True)
    monkeypatch.setattr(consent, "_first_owner", lambda db, org: ALICE)
    with env.engine.begin() as c:
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES (:u, 'org_boss', 'user', 'active', 'Bob') ON CONFLICT DO NOTHING"),
                  {"u": BOB})
    template_id = f"csv-desk-{uuid.uuid4().hex[:6]}"
    root = _template(tmp_path, template_id)
    starts: list = []
    made: set[str] = set()
    keys: list[str] = []
    try:
        a = _launch(env, root, template_id, starts, _Budget(), who=ALICE)["__consent__"]
        made.add(a["project_id"])
        keys.append(a["dedupe_key"])
        b = _launch(env, root, template_id, starts, _Budget(), who=BOB)
        assert "__consent__" in b, b
        b = b["__consent__"]
        made.add(b["project_id"])
        keys.append(b["dedupe_key"])
        assert b["project_id"] != a["project_id"]
        assert b["dedupe_key"] != a["dedupe_key"]
        assert b["projects"] == [b["project_id"]]
        # A builds; B's next press is still B's own project, never A's reused run.
        env.world.grant(ALICE, set(NEEDS) | {"delegation:grant"}, a["project_id"])
        with env.engine.begin() as c:
            grants.create_grant(session_ctx(ALICE), GrantRequest(
                agent_ids=tuple(x["id"] for x in a["agents"]),
                permissions=frozenset(a["permissions"]), project_ids=(a["project_id"],),
                task_run_id=a["task_run_id"], purpose=a["task"],
                resources=_resources(a)), db=c)
        built = _launch(env, root, template_id, starts, _Budget(), who=ALICE)
        assert built["project_id"] == a["project_id"] and built["reused"] is False
        b_again = _launch(env, root, template_id, starts, _Budget(), who=BOB)
        assert b_again["__consent__"]["project_id"] == b["project_id"]
        assert [s["project_id"] for s in starts] == [a["project_id"]]
    finally:
        for key in keys:
            _cleanup(env, made, key)
        with env.engine.begin() as c:
            c.execute(text("DELETE FROM identity_pending_consents WHERE principal_id = :u"), {"u": BOB})
            c.execute(text("DELETE FROM identity_principals WHERE id = :u"), {"u": BOB})


def test_a_launch_that_needs_no_consent_is_unchanged(env, monkeypatch, tmp_path):  # noqa: F811
    """No person to ask (system work): one transaction, one project, one succeeded receipt."""
    template_id = f"csv-desk-{uuid.uuid4().hex[:6]}"
    root = _template(tmp_path, template_id)
    starts: list = []
    made: set[str] = set()
    dedupe_key = ""
    try:
        with env.engine.begin() as c:
            out = factory_launch.launch(
                c, get_settings(), org_id="org_boss", actor="ceo", auth=session_ctx(ALICE),
                template_id=template_id, template_dir=root, builder=_builder(starts),
                budget=_Budget())
        made.add(out["project_id"])
        dedupe_key = out["dedupe_key"]
        assert out["run_id"] and starts[0]["grant_id"] is None
        with env.engine.connect() as c:
            assert c.execute(text("SELECT status FROM receipts WHERE dedupe_key = :k"),
                             {"k": dedupe_key}).scalars().all() == ["succeeded"]
    finally:
        if dedupe_key:
            _cleanup(env, made, dedupe_key)


def test_agent_for_role_prefers_own_project_callsign_and_isolates_orgs(env):  # noqa: F811
    """Project B having role_key='forge' must not steal 'forge' from Project A's callsign Forge,
    and cross-org projects must never be consulted (while same-org fallback still works)."""
    from boss_engine.identity import runs

    suf = uuid.uuid4().hex[:8]
    org2 = f"org_iso_{suf}"
    ws2 = f"ws_iso_{suf}"
    p_a = f"p_a_{suf}"
    p_other_org = f"p_o_{suf}"
    agt_a = f"agt_a_{suf}"
    agt_other_org = f"agt_o_{suf}"
    try:
        with env.engine.begin() as c:
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                           "VALUES (:p, 'ws_wf', 'Proj A', :p)"), {"p": p_a})
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec, agent_id) "
                           "VALUES (:i, :p, 'software_engineer', 'SE', "
                           "'{\"persona\": {\"callsign\": \"Forge\"}}'::jsonb, :a)"),
                      {"i": f"role_a_{suf}", "p": p_a, "a": agt_a})
            c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'Other Org')"), {"o": org2})
            c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'ws2')"),
                      {"w": ws2, "o": org2})
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                           "VALUES (:p, :w, 'Other Org Proj', :p)"), {"p": p_other_org, "w": ws2})
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec, agent_id) "
                           "VALUES (:i, :p, 'only_there', 'Only There', '{}'::jsonb, :a)"),
                      {"i": f"role_o_{suf}", "p": p_other_org, "a": agt_other_org})

        with env.engine.connect() as c:
            # env.project has role_key='forge', but p_a's own callsign 'Forge' must win.
            assert runs.agent_for_role(c, p_a, "forge") == agt_a
            # Same-org fallback finds role_key='atlas' from env.project when p_a lacks it.
            expected_atlas = c.execute(text("SELECT agent_id FROM roles WHERE project_id = :p AND role_key = 'atlas'"),
                                       {"p": env.project}).scalar()
            assert runs.agent_for_role(c, p_a, "atlas") == expected_atlas
            # Cross-org role is never returned.
            assert runs.agent_for_role(c, p_a, "only_there") is None
            # Missing project has no org row -> safe None.
            assert runs.agent_for_role(c, f"p_missing_{suf}", "forge") is None
    finally:
        with env.engine.begin() as c:
            c.execute(text("DELETE FROM roles WHERE project_id IN (:pa, :po)"), {"pa": p_a, "po": p_other_org})
            c.execute(text("DELETE FROM projects WHERE id IN (:pa, :po)"), {"pa": p_a, "po": p_other_org})
            c.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws2})
            c.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org2})
