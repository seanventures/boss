"""A workflow step that BUILDS asks for build authority at start, whatever the capability is called.

The defect class (the tester's P0-4 and the Captain's 500, 2026-10-09): consent decided "is this a
build?" from a hardcoded list (build, retry_build, code_task, …) that did not include
`security_fix`. A security review's consent therefore asked for no code-task permissions and no
repository, the person granted it, and the review's fix build was refused at start under enforce
("authority refused: no_grant"). Now any capability whose tool starts a build is a build step.

Also the contract the console's consent sheet renders (lib/identity/consent.ts): a person starting
the security review without a covering grant gets the build's structured 409, and a grant made of
exactly what that body asks for (grantBodyFor) lets the same start through.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.identity import consent, grants, lineage
from boss_engine.identity.delegation import GrantRequest
from boss_engine.services import workflow_capabilities, workflow_catalogue

from ._identity_agents_support import session_ctx
from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401  (the full-schema fixture)


def _review_entry() -> dict:
    return next(e for e in workflow_catalogue.CATALOGUE if e["key"] == "security_review")


def test_every_capability_that_starts_a_build_is_a_build_step():
    builders = [k for k, c in workflow_capabilities.CAPABILITIES.items() if c.tool in consent.BUILD_TOOLS]
    assert "security_fix" in builders and "build" in builders
    for key in builders:
        assert consent._capability_builds(key), key  # noqa: SLF001


def test_the_security_reviews_consent_asks_for_the_fix_builds_authority(env):  # noqa: F811
    with env.engine.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, finalize_ctx) VALUES "
                        "(:i, :p, 'shop', 'completed', CAST(:s AS JSONB), CAST(:f AS JSONB))"),
                   {"i": f"crun_{uuid.uuid4().hex[:8]}", "p": env.project,
                    "s": json.dumps({"owner": "acme", "repo": "shop"}), "f": json.dumps({"app_id": "shop"})})
        roles, perms, repos = consent.workflow_needs(db, env.project, workflow_catalogue.entry_spec(_review_entry()),
                                                     {"app_key": "shop"})
    assert consent.CODE_TASK_PERMISSIONS <= perms and "code_task:create" in perms
    assert repos == ["acme/shop"]                     # the app's own repository, not every one


def test_a_person_starting_the_review_gets_the_builds_consent_contract_and_its_grant_lets_it_start(env):  # noqa: F811
    from boss_engine.services import workflows

    from boss_engine.identity import passports

    spec = workflow_catalogue.entry_spec(_review_entry())
    # The crew as a project has it: the security lens (a reviewer Passport) for the review steps,
    # the builder for the fix builds (the catalogue's captains are not seeded in this schema).
    sentry = f"agt_sentry{uuid.uuid4().hex[:6]}"
    with env.engine.begin() as c:
        c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec, agent_id) "
                       "VALUES (:i, :p, 'sentry', 'Sentry', '{}'::jsonb, :a)"),
                  {"i": f"role_{uuid.uuid4().hex[:8]}", "p": env.project, "a": sentry})
        passports.ensure_passport(org_id="org_boss", agent_id=sentry, role_key="sentry", display_name="Sentry",
                                  sponsor_principal_id=ALICE, db=c)
    for step in spec["steps"]:
        cap = (step.get("binding") or {}).get("capability")
        step["captain"] = "forge" if consent._capability_builds(cap) else "sentry"  # noqa: SLF001
    with env.engine.begin() as c:
        d = workflows.create_definition(c, project_id=env.project, workflow_key=f"sr_{uuid.uuid4().hex[:6]}",
                                        name="Security Review", description=None, actor="test", namespace="product")
        v = workflows.publish_version(c, project_id=env.project, definition_id=d["id"], actor="test", spec=spec)

    def start():
        with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)):
            return workflows.start_run(c, project_id=env.project, actor="ceo",
                                       input_data={"app_key": "shop", "repository": "acme/web"},
                                       dedupe_key=f"dk_{uuid.uuid4().hex[:8]}", workflow_version_id=v["id"])

    with pytest.raises(consent.ConsentRequired) as e:
        start()
    body = e.value.body
    # the build's contract, field for field (lib/identity/consent.ts consentFromBody)
    assert body["error"] == "consent_required" and body["scope"] == "workflow_run"
    assert body["agents"] and all({"id", "name", "role_key"} <= set(a) for a in body["agents"])
    assert "code_task:create" in body["permissions"] and body["projects"] == [env.project]
    assert body["github"]["repos"] == ["acme/web"] and body["github"]["permissions"]["contents"] == "write"
    assert body["task_run_id"].startswith(grants.PENDING_PREFIX)
    assert [d["kind"] for d in body["durations"]] == ["task", "standing"]
    # grantBodyFor(request, durations[0]) -> POST /v1/identity/me/delegations (Alice, an Operator
    # here, holds what she delegates: the review's scans and its panel)
    env.world.grant(ALICE, set(body["permissions"]), env.project)
    req = GrantRequest(agent_ids=tuple(a["id"] for a in body["agents"]), permissions=frozenset(body["permissions"]),
                       project_ids=tuple(body["projects"]),
                       resources={"github": {"repos": body["github"]["repos"], **body["github"]["permissions"]}},
                       task_run_id=body["task_run_id"], purpose=body["task"])
    with env.engine.begin() as c:
        grants.create_grant(session_ctx(ALICE), req, db=c)
    run = start()
    with env.engine.begin() as c:
        gid = c.execute(text("SELECT grant_id FROM workflow_runs WHERE id = :r"), {"r": run["id"]}).scalar()
    assert gid


def test_a_preset_ceiling_grows_with_its_preset_and_a_custom_one_never_does(env):  # noqa: F811
    """The reviewer preset gained meeting:convene (it chairs the security review's panel). A
    reviewer Passport made before that must gain it at the next upgrade; a ceiling a person set by
    hand (`custom`) is never touched."""
    from boss_engine.identity import passports

    agents = {}
    with env.engine.begin() as c:
        for name in ("sentry", "rook"):
            aid = f"agt_{name}{uuid.uuid4().hex[:6]}"
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec, agent_id) "
                           "VALUES (:i, :p, :r, :r, '{}'::jsonb, :a)"),
                      {"i": f"role_{uuid.uuid4().hex[:8]}", "p": env.project, "r": name, "a": aid})
            passports.ensure_passport(org_id="org_boss", agent_id=aid, role_key=name, display_name=name,
                                      sponsor_principal_id=ALICE, db=c)
            agents[name] = aid
        # an install from before the preset grew: no meeting permissions on either
        c.execute(text("DELETE FROM identity_agent_ceilings WHERE agent_id = ANY(:a) AND permission_key LIKE 'meeting:%'"),
                  {"a": list(agents.values())})
        c.execute(text("UPDATE identity_agent_passports SET ceiling_preset = 'custom' WHERE agent_id = :a"),
                  {"a": agents["rook"]})
    with env.engine.begin() as c:
        out = passports.upgrade_from_roles(org_id="org_boss", db=c, default_sponsor=ALICE)
        grown = set(c.execute(text("SELECT permission_key FROM identity_agent_ceilings WHERE agent_id = :a"),
                              {"a": agents["sentry"]}).scalars())
        custom = set(c.execute(text("SELECT permission_key FROM identity_agent_ceilings WHERE agent_id = :a"),
                               {"a": agents["rook"]}).scalars())
    assert out[agents["sentry"]].startswith("ceiling grew") and {"meeting:read", "meeting:convene"} <= grown
    assert out[agents["rook"]] == "exists" and not {"meeting:read", "meeting:convene"} & custom
