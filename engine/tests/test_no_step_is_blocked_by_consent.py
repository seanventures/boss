"""No workflow step is blocked by consent: EVERY principal that checks a grant during a run is in
the consent the person gave at start, and the token each one is minted under is not empty.

test_consent_names_every_agent_that_acts covers BUILD steps (the builder the code task runs as).
This file walks EVERY step kind and capability, in every shipped workflow and in one workflow
holding every registered capability, through the runtime's own identity paths — not a restatement
of them:

  * the step's agent (step_agents.resolve after the start card's pick) gets its delegated token
    exactly as the runner mints it (workflow_capability_runner._step_authority): the grant must
    name the agent AND person ∩ ceiling ∩ grant must not be empty;
  * a step that does repository work (preview, deploy, redeploy, scans, reviews) clones through
    runs.repo_access inside that authority;
  * a build step queues its code task inside that authority, and the worker starts it later
    through runs.start_code_task_run (the builder's run token);

in four shapes: as published (catalogue default agents on duty), with no agents named (every step
runs as the person), with a start card naming agents, and the template-apply run input. Agents are
staffed as an install staffs them: the shipped roles (packages/specpack/roles) with the Passport
preset their persona's callsign gets.

Defects this found (2026-10-11): a Document step run by Quill (writer preset) failed
`empty_scope` — no preset but builder/reviewer/planner/operator held workflow_run:read, the one
permission every run's consent grants; and a build step run by its default captain was refused
`parent_run_not_live` — its code task was minted as a child of the step's token, which had ended.
"""
from __future__ import annotations

import json
import tempfile
import types
import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from boss_engine.identity import consent, grants, lineage, passports, runs
from boss_engine.identity.delegation import GrantRequest
from boss_engine.services import step_agents, workflow_capabilities, workflow_catalogue, workflows

from ._identity_agents_support import session_ctx
from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401  (full-schema fixture)

ROLES_DIR = Path(__file__).resolve().parents[2] / "packages" / "specpack" / "roles"
#: Capabilities whose tool clones the repository through runs.repo_access inside the step's
#: authority (start_preview -> preview_service, run_security_scan -> security_scan.service). A
#: deploy, redeploy or teardown runs on the host and clones nothing BOSS-side.
REPO_CAPS = frozenset({"preview", "security_scan"})
#: The step kinds the P0 named, each of which must be walked below.
NAMED_KINDS = frozenset({"review", "wait_for_ci", "preview", "report", "deploy", "smoke_test", "monitor",
                         "architecture", "meeting", "build", "spec_review", "security_decide"})
RUN_INPUT = {"title": "HelpDesk", "repository": "acme/web", "instructions": "go", "directive": "go",
             "app_key": "app1", "notify_channel": "ops"}


def _staff(env) -> dict[str, str]:  # noqa: F811
    """Every shipped role, with the Passport preset an install gives it (persona callsign first).
    The fixture's `forge` and `atlas` stand for the Software Engineer and the Chief Architect."""
    out: dict[str, str] = {}
    with env.engine.begin() as c:
        for f in sorted(ROLES_DIR.glob("*.yaml")):
            rk = f.stem
            if rk in ("software_engineer", "chief_architect"):
                continue
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            callsign = str((doc.get("persona") or {}).get("callsign") or "")
            aid = f"agt_{rk[:10]}{uuid.uuid4().hex[:6]}"
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec, agent_id) "
                           "VALUES (:i, :p, :r, :r, CAST(:s AS JSONB), :a)"),
                      {"i": f"role_{uuid.uuid4().hex[:8]}", "p": env.project, "r": rk,
                       "s": json.dumps({"persona": {"callsign": callsign}}), "a": aid})
            passports.ensure_passport(org_id="org_boss", agent_id=aid, role_key=rk, display_name=callsign or rk,
                                      sponsor_principal_id=ALICE, db=c,
                                      preset=passports.preset_for_role(callsign) or passports.preset_for_role(rk))
            out[rk] = aid
    return out


def _grant(env, body, *, task_run_id="from-body"):  # noqa: F811
    env.world.grant(ALICE, set(body["permissions"]), env.project)
    gh = body.get("github") or None
    req = GrantRequest(agent_ids=tuple(a["id"] for a in body["agents"]),
                       permissions=frozenset(body["permissions"]), project_ids=tuple(body["projects"]),
                       resources=({"github": {"repos": gh["repos"], **gh["permissions"]}} if gh else {}),
                       task_run_id=body["task_run_id"] if task_run_id == "from-body" else task_run_id,
                       purpose=body["task"])
    with env.engine.begin() as c:
        return grants.create_grant(session_ctx(ALICE), req, db=c)


def _publish(env, spec, name):  # noqa: F811
    with env.engine.begin() as c:
        d = workflows.create_definition(c, project_id=env.project, workflow_key=f"w_{uuid.uuid4().hex[:8]}",
                                        name=name, description=None, actor="test", namespace="product")
        return workflows.publish_version(c, project_id=env.project, definition_id=d["id"], actor="test", spec=spec)


def _start_consented(env, version, run_input):  # noqa: F811
    """Start as the person; on 409 consent_required, grant exactly what the sheet asked."""
    def start():
        with env.engine.begin() as c, lineage.acting(session_ctx(ALICE)):
            return workflows.start_run(c, project_id=env.project, actor="ceo", input_data=run_input,
                                       dedupe_key=f"dk_{uuid.uuid4().hex[:8]}", workflow_version_id=version["id"])
    try:
        return start()
    except consent.ConsentRequired as asked:
        _grant(env, asked.body)
        return start()


def _builds(step) -> bool:
    cap = str((step.get("binding") or {}).get("capability") or "")
    tool = getattr(workflow_capabilities.CAPABILITIES.get(cap), "tool", None)
    return cap in consent.BUILD_CAPABILITIES or tool == "create_code_task" or step.get("kind") == "code_task"


def _walk(env, run_id, spec, run_input, label) -> list[tuple]:  # noqa: F811
    """Every step of `spec`, through the runtime's identity paths, under run `run_id`'s authority."""
    from boss_engine.services import jobs
    from boss_engine.services.workflow_capability_runner import _step_authority
    from boss_engine.tools.builtin import _building_agent

    problems: list[tuple] = []
    ctx = types.SimpleNamespace(engine=env.engine)
    for step in spec.get("steps") or []:
        cap = str((step.get("binding") or {}).get("capability") or step.get("kind") or "")
        queued = None
        with env.engine.begin() as c:
            as_run = step_agents.with_run_agent(c, env.project, step, run_input)
            ran = step_agents.resolve(c, None, env.project, as_run, actor="ceo")
            try:
                with _step_authority(ctx, c, env.project, {"run_id": run_id}, as_run, ran["agent"]):
                    if cap in REPO_CAPS:
                        with runs.repo_access(env.engine, host="github", owner="acme", repo="web",
                                              project_id=env.project, token=""):
                            pass
                    if _builds(step):
                        agent = _building_agent(c, env.project, ran["agent"])
                        crun = f"crun_{uuid.uuid4().hex[:10]}"
                        job = jobs.enqueue_job(c, env.project, "code.task",
                                               {"run_id": crun, "workflow_run_id": run_id})
                        queued = (job["id"], runs.code_task_builder({}, {"agent": agent} if agent else {}), crun)
            except Exception as exc:  # noqa: BLE001 — collected: the test names every refusal
                problems.append((label, step.get("id"), cap, ran["agent"], "step", repr(exc)[:240], getattr(exc, "reason", None) or getattr(getattr(exc, "__cause__", None), "reason", None)))
                continue
        if queued:
            try:
                started = runs.start_code_task_run(env.engine, task_run_id=queued[2], project_id=env.project,
                                                   owner="acme", repo="web", role_key=queued[1], job_id=queued[0])
                assert started is not None, "no authority for the build (refused; shadow answers None)"
                runs.finish_code_task_run(env.engine, started[0], org_id="org_boss")
            except Exception as exc:  # noqa: BLE001
                problems.append((label, step.get("id"), cap, ran["agent"], "build", repr(exc)[:240]))
    return problems


def _without_agents(spec):
    spec = json.loads(json.dumps(spec))
    for step in spec.get("steps") or []:
        step.pop("captain", None)
        step.pop("default_captain", None)
    return spec


@pytest.fixture()
def staffed(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("BOSS_EGRESS_URL", "https://egress.boss.internal")
    monkeypatch.setenv("BOSS_SHIM_DIR", tempfile.mkdtemp(prefix="bshim", dir="/tmp"))
    return _staff(env)


def test_every_step_of_every_shipped_workflow_runs_under_the_consent_given_at_start(env, staffed):  # noqa: F811
    problems: list[tuple] = []
    walked: set[str] = set()
    for entry in workflow_catalogue.CATALOGUE:
        published = workflow_catalogue.entry_spec(entry)
        card = {"step_agents": {s["id"]: _card_pick(s, staffed) for s in published.get("steps") or []
                                if _card_pick(s, staffed)}}
        shapes = [("published", published, RUN_INPUT), ("no agents", _without_agents(published), RUN_INPUT),
                  ("start card", published, {**RUN_INPUT, **card})]
        for shape, spec, run_input in shapes:
            version = _publish(env, spec, entry["key"])
            run = _start_consented(env, version, run_input)
            problems += _walk(env, run["id"], version["spec"], run_input, f"{entry['key']} ({shape})")
            walked |= {str((s.get("binding") or {}).get("capability") or s.get("kind"))
                       for s in version["spec"].get("steps") or []}
    assert not problems, "\n".join(map(str, problems))
    assert len(walked) >= 15


def _card_pick(step, staffed) -> str:
    """What a start card offers for a step: ANOTHER agent of the project with the same Passport
    preset as the step's own (a builder for a build, a planner for a panel), never one that
    could not do the work."""
    named = step_agents.named_agent(step)
    preset = passports.preset_for_role(_CALLSIGNS.get(named, named)) if named else None
    if not preset:
        return ""
    return next((rk for rk in sorted(staffed) if rk != named
                 and passports.preset_for_role(_CALLSIGNS.get(rk, rk)) == preset), "")


_CALLSIGNS = {f.stem: str(((yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("persona") or {})
                          .get("callsign") or "") for f in ROLES_DIR.glob("*.yaml")}


def _every_capability_spec(staffed) -> dict:
    steps = []
    for key, cap in workflow_capabilities.CAPABILITIES.items():
        step = {"id": key, "kind": "capability", "binding": {"capability": key, "inputs": {}}}
        captain = workflow_catalogue.DEFAULT_STEP_AGENTS.get(key) or getattr(cap, "captain", None)
        if captain in staffed:
            step.update(captain=captain, default_captain=captain)
        steps.append(step)
    for i, step in enumerate(steps):
        step["next"] = [steps[i + 1]["id"]] if i + 1 < len(steps) else []
    return {"entry_step": steps[0]["id"], "steps": steps}


@pytest.mark.parametrize("shape", ["captains", "no agents"])
def test_every_registered_capability_runs_under_the_consent_its_workflow_asks_for(env, staffed, shape):  # noqa: F811
    """Every capability BOSS registers (wait_for_ci, smoke_test, architecture, report, monitor,
    decision gates, ...), not only the ones a shipped workflow happens to use. The consent is
    exactly workflow_needs of this spec; the run carries the grant made from it."""
    spec = _every_capability_spec(staffed)
    if shape == "no agents":
        spec = _without_agents(spec)
    assert NAMED_KINDS <= {s["binding"]["capability"] for s in spec["steps"]}
    carrier = _publish(env, {"entry_step": "b", "steps": [{"id": "b", "kind": "capability", "next": [],
                                                          "binding": {"capability": "report", "inputs": {}}}]},
                       "carrier")
    with env.engine.begin() as c:
        roles, perms, repos = consent.workflow_needs(c, env.project, spec, RUN_INPUT)
        agents = [{"id": runs.agent_for_role(c, env.project, rk)} for rk in roles]
    body = {"agents": [a for a in agents if a["id"]], "permissions": sorted(
                k for k in perms if consent.catalogue.is_known(k) and consent.catalogue.meta(k).delegable),
            "projects": [env.project], "task": "every capability", "task_run_id": None,
            "github": ({"repos": repos, "permissions": {"contents": "write", "pull_requests": "write"}}
                       if repos else None)}
    grant = _grant(env, body)
    run = _start_consented(env, carrier, RUN_INPUT)
    with env.engine.begin() as c:
        c.execute(text("UPDATE workflow_runs SET grant_id = :g WHERE id = :r"), {"g": grant.id, "r": run["id"]})
    problems = _walk(env, run["id"], spec, RUN_INPUT, f"every capability ({shape})")
    assert not problems, "\n".join(map(str, problems))


def test_a_template_apply_run_consents_for_the_apps_repository(env, staffed):  # noqa: F811
    """templates.apply.plan's run input names the app (template.app_key, extends_run_id), not a
    `repository`: the consent must still name the repository its build pushes to."""
    with env.engine.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, finalize_ctx) "
                       "VALUES ('crun_app1root', :p, 'app1', 'completed', CAST(:s AS JSONB), CAST(:f AS JSONB))"),
                  {"p": env.project, "s": json.dumps({"owner": "acme", "repo": "web"}),
                   "f": json.dumps({"app_id": "app1"})})
    run_input = {"title": "app1: add Login", "directive": "add login", "acceptance_criteria": ["works"],
                 "template": {"id": "tpl_x", "key": "login-oauth", "app_key": "app1", "stack": "node"},
                 "extends_run_id": "crun_app1root"}
    for key in ("fast_fix", "standard_product_delivery"):
        entry = next(e for e in workflow_catalogue.CATALOGUE if e["key"] == key)
        version = _publish(env, workflow_catalogue.entry_spec(entry), key)
        with env.engine.begin() as c:
            _, _, repos = consent.workflow_needs(c, env.project, version["spec"], run_input)
        assert repos == ["acme/web"], (key, repos)
        run = _start_consented(env, version, run_input)
        problems = _walk(env, run["id"], version["spec"], run_input, f"template via {key}")
        assert not problems, "\n".join(map(str, problems))


def test_every_passport_preset_can_read_the_run_it_acts_in():
    """Any agent may be a step's agent, and every run's consent grants workflow_run:read: a preset
    without it gives a tool-less step an empty token (`empty_scope`)."""
    from boss_engine.identity.permissions_gen import PASSPORT_PRESETS

    assert consent.WORKFLOW_BASE_PERMISSIONS == frozenset({"workflow_run:read"})
    for preset in PASSPORT_PRESETS.values():
        assert "workflow_run:read" in preset.permissions, preset.key
