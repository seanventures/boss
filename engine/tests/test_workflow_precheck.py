"""The workflow pre-check (services.workflow_precheck): the registry every capability declares
into, each check's verdict logic on fixtures, and the start door's accept / skip recording.

No database: the checks read through `Context.memo` (pre-filled here) and fixture trees."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from boss_engine.services import workflow_capabilities as caps
from boss_engine.services import workflow_precheck as wp


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────

def _step(sid: str, capability: str, nxt: Any = (), **extra: Any) -> dict[str, Any]:
    return {"id": sid, "kind": "capability", "next": list(nxt),
            "binding": {"capability": capability, "inputs": extra.pop("inputs", {})}, **extra}


def _spec(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"steps": list(steps), "entry_step": steps[0]["id"]}


class _Settings:
    plan_budget_usd_micros = 1_500_000
    code_task_budget_usd_micros = 2_000_000
    code_agent_max_concurrency = 1


def _ctx(spec: dict[str, Any] | None = None, *, app_dir: Path | None = None,
         repo_root: Path | None = None, checkpoint: str = wp.PRE_DEPLOY,
         memo: dict[str, Any] | None = None, run_input: dict | None = None) -> wp.Context:
    ctx = wp.Context(connection=None, settings=_Settings(), project_id="p1", actor="ceo",
                     spec=spec or _spec(_step("build", "build")),
                     run_input=run_input or {}, checkpoint=checkpoint, app_dir=app_dir,
                     repo_root=repo_root, offline=True)
    ctx.memo.update({"template_declared": {}, "binding": None, "identity_applies": True,
                     "_target_results": []})
    ctx.memo.update(memo or {})
    return ctx


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


HELPDESK_LIKE = {
    "package.json": json.dumps({"name": "hd", "scripts": {"build": "vite build", "test": "vitest run"},
                                "dependencies": {"express": "5", "pg": "8"},
                                "devDependencies": {"vite": "6", "typescript": "5", "eslint": "9",
                                                    "@playwright/test": "1",
                                                    "@types/react": "18",
                                                    "@types/react-dom": "18"}}),
    ".env.example": "DATABASE_URL=\nSESSION_SECRET=\nDEMO_PASSWORD=\n",
    "boss.preview.command": "npx tsx src/server/index.ts\n",
    "src/server/config.ts": (
        'const s = get("SESSION_SECRET");\n'
        'if (s.length < 32) { throw new Error("SESSION_SECRET must be set to 32 chars"); }\n'
        'if (!get("DEMO_PASSWORD")) {\n'
        '  throw new Error("DEMO_PASSWORD must be set (the seed refuses to run without it)");\n'
        '}\n'),
    "src/server/app.ts": ('const clientDist = path.resolve(process.cwd(), "dist/client");\n'
                          'app.use(express.static(clientDist));\n'
                          'app.get("/api/tickets", list);\n'),
    "src/server/index.ts": "app.listen(port, host);\n",
}


# ── the registry ──────────────────────────────────────────────────────────────────────────────

def test_every_capability_declares_its_prechecks():
    """A new capability must say what is checked before a run — or say, in words, that there is
    nothing to check. Silence is not a declaration."""
    assert set(wp.CAPABILITY_PRECHECKS) == set(caps.CAPABILITIES)
    for key, entry in wp.CAPABILITY_PRECHECKS.items():
        assert entry.checks or entry.none_because, f"{key}: no checks and no reason for none"
        assert not (entry.checks and entry.none_because), key
        for check_id in entry.checks:
            assert check_id in wp.CHECKS, f"{key} names an unknown check {check_id}"
        assert entry.on_refusal in ("fails", "skips"), key
        if entry.reserves:
            assert hasattr(_Settings, entry.reserves[0]) or entry.reserves[0].endswith(
                "_usd_micros"), key


def test_every_tool_the_runner_calls_itself_is_declared():
    """The consent check is only as good as the list of calls it compares: every tool the
    capability runner calls by name must be some capability's tool or declared invoked."""
    source = Path(wp.__file__).with_name("workflow_capability_runner.py").read_text()
    called = set(re.findall(r"_invoke_tool\(\s*ctx,\s*\"([a-z_]+)\"", source))
    assert called, "the scan found no tool calls — the pattern has drifted"
    declared = {c.tool for c in caps.CAPABILITIES.values() if c.tool}
    for entry in wp.CAPABILITY_PRECHECKS.values():
        declared |= {t.partition("@")[0] for t in entry.invokes_tools}
    assert called <= declared, f"undeclared: {sorted(called - declared)}"


def test_every_check_has_a_label_and_a_checkpoint():
    for check in wp.CHECKS.values():
        assert check.label and set(check.checkpoints) <= set(wp.CHECKPOINTS)


# ── authority ─────────────────────────────────────────────────────────────────────────────────

def _consent_memo(granted: list[str], ceilings: dict[str, list[str]] | None = None) -> dict:
    memo = {"consent": {"roles": ["qa"], "permissions": granted, "repos": ["o/r"]}}
    for role, perms in (ceilings or {}).items():
        memo[f"ceiling:{role}"] = frozenset(perms)
    return memo


def test_a_step_whose_runner_calls_a_tool_the_consent_does_not_grant_will_fail():
    step = _step("more_tests", "more_tests")
    ctx = _ctx(_spec(step), memo=_consent_memo(["code_task:create", "workflow_run:read"]))
    found = wp.check_consent(ctx, step)
    assert found["verdict"] == "will_fail"
    assert "code_task:retry" in found["details"]["missing"]
    assert "extend_code_task" in found["evidence"]


def test_a_step_the_consent_covers_is_ready():
    step = _step("build", "build")
    ctx = _ctx(_spec(step), memo=_consent_memo(["code_task:create"]))
    assert wp.check_consent(ctx, step)["verdict"] == "ready"


def test_an_extend_build_also_needs_what_extend_code_task_needs():
    plain = _step("build", "build")
    extend = _step("build", "build", inputs={"extend": True})
    assert [p for _, p in wp.step_permissions(plain)] == ["code_task:create"]
    assert "code_task:retry" in [p for _, p in wp.step_permissions(extend)]


def test_a_passport_ceiling_gap_is_fixable_and_a_tolerated_refusal_never_fails():
    step = _step("preview", "preview")
    ctx = _ctx(_spec(step), memo=_consent_memo(["preview:manage"], {"qa": ["preview:read"]}))
    found = wp.check_consent(ctx, step)
    assert found["verdict"] == "fixable"
    assert "qa's Passport" in found["fix"]["action"]


def test_without_passports_the_gap_is_reported_but_not_a_failure():
    step = _step("more_tests", "more_tests")
    ctx = _ctx(_spec(step), memo={**_consent_memo(["code_task:create"]),
                                  "identity_applies": False})
    assert wp.check_consent(ctx, step)["verdict"] == "unknown"


# ── app environment ───────────────────────────────────────────────────────────────────────────

def test_code_env_requirements_reads_the_apps_own_startup_errors(tmp_path):
    app = _write(tmp_path / "hd", HELPDESK_LIKE)
    found = wp.code_env_requirements(app)
    assert "DEMO_PASSWORD" in found and found["DEMO_PASSWORD"]["required"]
    assert found["DEMO_PASSWORD"]["required"][0].startswith("src/server/config.ts:")


def test_binding_refusal_matches_the_api():
    assert wp.binding_refusal("DEMO_PASSWORD", "deploy", {}) is None
    assert "secret_not_declared" in wp.binding_refusal("DEMO_PASSWORD", "build", {})
    assert wp.binding_refusal("STRIPE_KEY", "build", {"STRIPE_KEY": {}}) is None
    assert "reserved_name" in wp.binding_refusal("BOSS_X", "deploy", {})


def _env_ctx(app: Path, step: dict) -> wp.Context:
    from boss_engine.templates import run_env

    return _ctx(_spec(step), app_dir=app,
                memo={"declared_needs": run_env.needs_from_app_dir(app), "pg": []})


def test_a_variable_the_app_requires_and_nobody_may_bind_for_a_build_will_fail(tmp_path):
    app = _write(tmp_path / "hd", HELPDESK_LIKE)
    step = _step("build", "build")
    found = wp.check_app_env(_env_ctx(app, step), step)
    assert found["id"] == "app_env.build" and found["verdict"] == "will_fail"
    assert "DEMO_PASSWORD" in found["evidence"] and "secret_not_declared" in found["evidence"]
    # SESSION_SECRET is a key BOSS mints for a throwaway run: never a problem.
    assert not any("SESSION_SECRET" in p for p in found["details"]["problems"])


def test_the_same_variable_at_deploy_is_fixable_by_a_person(tmp_path):
    app = _write(tmp_path / "hd", HELPDESK_LIKE)
    step = _step("deploy", "deploy")
    found = wp.check_app_env(_env_ctx(app, step), step)
    assert found["id"] == "app_env.deploy" and found["verdict"] == "fixable"
    assert "DEMO_PASSWORD" in found["evidence"]
    assert "SESSION_SECRET" not in " ".join(found["details"]["fixable"])


def test_an_app_that_declares_nothing_it_requires_is_ready(tmp_path):
    app = _write(tmp_path / "ok", {"package.json": "{}", "src/a.ts": "export const a = 1;\n"})
    step = _step("build", "build")
    assert wp.check_app_env(_env_ctx(app, step), step)["verdict"] == "ready"


# ── the app tree ──────────────────────────────────────────────────────────────────────────────

def test_a_preview_that_serves_an_unbuilt_dist_will_fail(tmp_path):
    app = _write(tmp_path / "hd", HELPDESK_LIKE)
    step = _step("preview", "preview")
    found = wp.check_preview_build(_ctx(_spec(step), app_dir=app), step)
    assert found["verdict"] == "will_fail" and "dist/client" in found["evidence"]
    (app / "boss.preview.command").write_text("npm run build && npx tsx src/server/index.ts\n")
    assert wp.check_preview_build(_ctx(_spec(step), app_dir=app), step)["verdict"] == "ready"


def test_a_server_app_with_no_manifest_of_its_own_will_fail_and_names_the_root_one(tmp_path):
    repo = _write(tmp_path / "repo", {"boss.deploy.yaml": "version: 1\nname: stack-demo\n"})
    app = _write(repo / "hd", HELPDESK_LIKE)
    step = _step("deploy", "deploy")
    found = wp.check_manifest(_ctx(_spec(step), app_dir=app, repo_root=repo), step)
    assert found["verdict"] == "will_fail"
    assert "stack-demo" in found["evidence"] and "hd/boss.deploy.yaml" in found["fix"]["action"]


MANIFEST = ("version: 1\nname: hd\nkind: commands\ninstall: [\"npm ci\"]\n"
            "health: [{url: \"{target.public_url}/healthz\", expect: 200}]\n")


def test_a_postgres_app_needs_a_database_where_it_is_deployed(tmp_path):
    app = _write(tmp_path / "hd", HELPDESK_LIKE)
    step = _step("deploy", "deploy")
    ctx = _ctx(_spec(step), app_dir=app, memo={"pg": ["package.json depends on pg"]})
    assert wp.check_database(ctx, step)["verdict"] == "will_fail"
    (app / "boss.deploy.yaml").write_text(
        MANIFEST + "resources:\n  db: {capability: database.relational, engine: postgres}\n")
    ctx = _ctx(_spec(step), app_dir=app, memo={"pg": ["package.json depends on pg"]})
    assert wp.check_database(ctx, step)["verdict"] == "ready"


def test_a_declared_health_path_the_app_does_not_serve_will_fail(tmp_path):
    app = _write(tmp_path / "hd", {**HELPDESK_LIKE, "boss.deploy.yaml": MANIFEST})
    step = _step("deploy", "deploy")
    assert wp.check_health(_ctx(_spec(step), app_dir=app), step)["verdict"] == "will_fail"
    (app / "src/server/health.ts").write_text('app.get("/healthz", ok);\n')
    assert wp.check_health(_ctx(_spec(step), app_dir=app), step)["verdict"] == "ready"


def test_only_test_star_scripts_mean_no_tests_run(tmp_path):
    pkg = json.loads(HELPDESK_LIKE["package.json"])
    pkg["scripts"] = {"build": "vite build", "test:auth": "vitest run auth"}
    app = _write(tmp_path / "hd", {**HELPDESK_LIKE, "package.json": json.dumps(pkg)})
    step = _step("build", "build")
    found = wp.check_test_plan(_ctx(_spec(step), app_dir=app), step)
    assert found["verdict"] == "will_fail" and "test:*" in found["evidence"]


def test_the_gates_own_package_edits_are_reported(tmp_path):
    pkg = {"name": "x", "scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "2"}}
    app = _write(tmp_path / "x", {"package.json": json.dumps(pkg)})
    step = _step("build", "build")
    found = wp.check_test_plan(_ctx(_spec(step), app_dir=app), step)
    assert found["verdict"] == "fixable" and "add:vite" in found["evidence"]
    assert json.loads((app / "package.json").read_text()) == pkg, "the app tree was modified"


# ── smoke, budget ─────────────────────────────────────────────────────────────────────────────

def test_smoke_reports_a_deployment_kind_it_cannot_read(tmp_path, monkeypatch):
    spec = _spec(_step("deploy", "deploy", ["smoke"]), _step("smoke", "smoke_test"))
    kinds = [k for k, _ in wp._handle_kinds("deploy")]
    assert {"deployment", "stack_deployment"} <= set(kinds)
    app = _write(tmp_path / "hd", {**HELPDESK_LIKE, "boss.deploy.yaml": MANIFEST})
    from boss_engine.services import deployment_smoke

    monkeypatch.setattr(deployment_smoke, "run_deployment",
                        lambda c, r: None if "stack_deployment" in json.dumps(
                            c.output) else {"id": "d"})
    found = wp.check_smoke(_ctx(spec, app_dir=app), spec["steps"][1])
    assert found["verdict"] == "will_fail" and "stack_deployment" in found["evidence"]
    found = wp.check_smoke(_ctx(spec, checkpoint=wp.PRE_RUN), spec["steps"][1])
    assert found["verdict"] == "unknown"
    monkeypatch.setattr(deployment_smoke, "run_deployment", lambda c, r: {"id": "d"})
    assert wp.check_smoke(_ctx(spec, app_dir=app), spec["steps"][1])["verdict"] == "ready"


def _budget(cap: int, actual: int = 0, reserved: int = 0) -> dict:
    period = {"cap_usd_micros": cap, "actual_usd_micros": actual,
              "reserved_usd_micros": reserved}
    return {"periods": {"day": period}}


@pytest.mark.parametrize("snapshot,verdict", [
    (_budget(100_000_000), "ready"),
    (_budget(10_000_000), "unknown"),                     # $6 twice does not fit $10
    (_budget(10_000_000, reserved=6_000_000), "unknown"),  # headroom held by other work
    (_budget(10_000_000, actual=5_000_000), "will_fail"),  # spent money leaves no room
])
def test_budget_headroom(snapshot, verdict):
    step = _step("meeting", "meeting")
    settings = _Settings()
    settings.plan_budget_usd_micros = 1_500_000
    ctx = _ctx(_spec(step), memo={"budget": snapshot, "active_runs": 1})
    assert wp.check_budget(ctx, step)["verdict"] == verdict


# ── run lifecycle ─────────────────────────────────────────────────────────────────────────────

def _parallel_spec(policy: str = "all") -> dict[str, Any]:
    return {"entry_step": "build", "steps": [
        _step("build", "build", ["pass_2"]),
        {"id": "pass_2", "kind": "parallel", "branches": ["more_tests", "deploy"],
         "join": "joined"},
        _step("more_tests", "more_tests", ["joined"]),
        _step("deploy", "deploy", ["joined"]),
        {"id": "joined", "kind": "join", "wait_for": ["more_tests", "deploy"],
         "success_policy": policy, "next": []},
    ]}


def test_a_failing_branch_with_a_live_sibling_is_named():
    ctx = _ctx(_parallel_spec(), checkpoint=wp.PRE_RUN)
    out = {c["id"]: c for c in wp.lifecycle_checks(
        ctx, {"more_tests": [{"verdict": "will_fail"}]})}
    assert out["lifecycle.branch_failure"]["verdict"] == "will_fail"
    assert out["lifecycle.advisory_start"]["verdict"] == "will_fail"
    calm = {c["id"]: c for c in wp.lifecycle_checks(ctx, {})}
    assert calm["lifecycle.branch_failure"]["verdict"] == "unknown"


def test_an_any_join_tolerates_a_failed_branch():
    ctx = _ctx(_parallel_spec("any"), checkpoint=wp.PRE_RUN)
    ids = {c["id"] for c in wp.lifecycle_checks(ctx, {"more_tests": [{"verdict": "will_fail"}]})}
    assert "lifecycle.branch_failure" not in ids


# ── skipping, accepting, recording ────────────────────────────────────────────────────────────

def test_a_skip_never_bypasses_a_gate_that_protects_a_deploy():
    spec = _spec(_step("review", "review", ["gate"]), _step("gate", "spec_review", ["deploy"]),
                 _step("deploy", "deploy", ["more_tests"]), _step("more_tests", "more_tests"))
    by_id = {s["id"]: s for s in spec["steps"]}
    assert "gate" in wp.skip_refusal(spec, by_id["gate"])
    assert "unverified" in wp.skip_refusal(spec, by_id["review"])
    assert wp.skip_refusal(spec, by_id["more_tests"]) is None
    assert wp.skip_refusal(spec, by_id["deploy"]) is None


class _Nested:
    def rollback(self):
        pass


class _DoorConnection:
    """What gate_start asks the database: the version's namespace."""

    def execute(self, *_a, **_k):
        class _R:
            def scalar(self_inner):
                return "product"
        return _R()

    def begin_nested(self):
        return _Nested()


@pytest.fixture
def door(monkeypatch):
    spec = _spec(_step("build", "build", ["review"]), _step("review", "review", ["deploy"]),
                 _step("deploy", "deploy", ["more_tests"]), _step("more_tests", "more_tests"))
    report = {"steps": [], "summary": {"blocking": ["more_tests:authority.consent",
                                                    "build:budget.headroom"],
                                       "sentence": "2 will fail"}}
    monkeypatch.setattr(wp, "build_context", lambda *a, **k: _ctx(spec))
    monkeypatch.setattr(wp, "run_checks", lambda ctx, only_fast=False: report)
    monkeypatch.setenv(wp.GATE_ENV, "enforce")
    return lambda **kw: wp.gate_start(_DoorConnection(), None, project_id="p1", version_id="v1",
                                      run_input={}, actor="alice", **kw)


def test_the_door_refuses_what_the_operator_did_not_accept(door):
    with pytest.raises(wp.PrecheckRefused) as refused:
        door()
    assert refused.value.unaccepted == ["more_tests:authority.consent", "build:budget.headroom"]
    assert refused.value.detail()["error"] == "precheck_failed"


def test_accepting_and_skipping_are_recorded_with_who_and_why(door):
    record = door(accept=["budget.headroom"],
                  skip_steps=[{"step_id": "more_tests", "reason": "pass 2 later"}])
    assert record["by"] == "alice"
    assert record["accepted"] == ["build:budget.headroom"]
    assert record["skip_steps"] == [{"step_id": "more_tests", "reason": "pass 2 later"}]


def test_a_readiness_choice_accepts_the_same_precheck(door):
    record = door(start_anyway=["budget"], skip_steps=["more_tests"], reason="later")
    assert record["accepted"] == ["build:budget.headroom"]
    assert record["skip_steps"] == [{"step_id": "more_tests", "reason": "later"}]


def test_the_door_refuses_a_skip_of_a_step_that_protects_the_deploy(door):
    with pytest.raises(wp.SkipRefused):
        door(accept=["more_tests:authority.consent", "build:budget.headroom"],
             skip_steps=["review"])
    with pytest.raises(wp.SkipRefused):
        door(skip_steps=["nope"])


def test_the_gate_can_be_turned_off(door, monkeypatch):
    monkeypatch.setenv(wp.GATE_ENV, "off")
    assert door() is None


def test_report_is_the_default_and_never_refuses(door, monkeypatch):
    monkeypatch.delenv(wp.GATE_ENV)
    assert wp.gate_mode() == "report"
    record = door()
    assert record["mode"] == "report"
    assert record["unaccepted"] == ["more_tests:authority.consent", "build:budget.headroom"]
    assert record["message"].startswith("Pre-check: 2 will_fail, 0 fixable")
    assert "more_tests:authority.consent" in record["message"]
    assert record["report"]["summary"]["sentence"] == "2 will fail"


def test_report_mode_still_records_skips_and_refuses_a_gate_skip(door, monkeypatch):
    monkeypatch.setenv(wp.GATE_ENV, "report")
    record = door(skip_steps=[{"step_id": "more_tests", "reason": "later"}])
    assert record["skip_steps"] == [{"step_id": "more_tests", "reason": "later"}]
    with pytest.raises(wp.SkipRefused):
        door(skip_steps=["review"])


def test_the_app_env_checks_run_on_a_builds_tree(tmp_path):
    app = _write(tmp_path / "hd", HELPDESK_LIKE)
    found = wp.app_env_on_tree(None, None, project_id="p1", app_key=None, app_dir=app)
    verdicts = {c["id"]: c["verdict"] for c in found["checks"]}
    assert verdicts == {"app_env.build": "will_fail", "app_env.preview": "will_fail",
                        "app_env.deploy": "fixable"}
    assert found["message"].startswith("Pre-check: 2 will_fail, 1 fixable")


def test_the_runner_honours_a_recorded_skip():
    from boss_engine.services import workflow_capability_runner as runner

    context = {"run_id": "r", "input": {wp.RECORD_KEY: {
        "by": "alice", "skip_steps": [{"step_id": "more_tests", "reason": "pass 2 later"}]}}}
    out = runner._execute_step(None, None, project_id="p1", actor="alice",
                               step=_step("more_tests", "more_tests"), context=context)
    assert out["status"] == "not_applicable"
    assert out["skipped_by_precheck"] == {"by": "alice", "reason": "pass 2 later"}
    assert wp.skipped_by_precheck(context, "deploy") is None


def test_run_checks_shapes_the_report(tmp_path):
    step = _step("build", "build")
    ctx = _ctx(_spec(step), app_dir=_write(tmp_path / "x", {"package.json": "{}"}),
               memo={**_consent_memo(["code_task:create"]), "budget": _budget(100_000_000),
                     "active_runs": 0, "declared_needs": {}, "pg": []})
    ctx.offline = True
    report = wp.run_checks(ctx, only_fast=True)
    assert report["steps"][0]["step_id"] == "build"
    for check in report["steps"][0]["checks"]:
        assert check["verdict"] in wp.VERDICTS
        assert {"id", "evidence", "fix", "skip_allowed"} <= set(check)
    assert set(wp.VERDICTS) <= set(report["summary"])
