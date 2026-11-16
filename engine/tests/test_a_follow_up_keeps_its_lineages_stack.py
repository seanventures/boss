"""A follow-up keeps the stack its lineage declared (BOSSAPP-1230, PR #142 review).

A follow-up's spec is the findings it was sent to fix, with no `Stack:` line. Read alone it
declares nothing, so its gate fell back to npm and BOSS's Node hardening came back, while the
acceptance gate (items_spec: the lineage's builds) still held it to Go. start_code_task now
stamps the lineage's declared stack on every lineage child as the structured `required_stack`,
read from the same lineage builds the acceptance gate reads.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from boss_engine.services import build_scope, toolchains
from boss_engine.services import code_task_service as cts
from boss_engine.services import verification_target as vt

from .test_a_resumed_build_runs_as_the_person import _Jobs, _ports  # noqa: F401
from .test_identity_agents_workflow_consent import env  # noqa: F401  (full-schema fixture)

REPO = {"owner": "local_northgate", "repo": "northgate-gateway", "base_branch": None}
ROOT_BRIEF = ("Northgate API Gateway\n\nStack: Go. (gateway and gwlint: one Go module)\n\n"
              "A reverse-proxy API gateway configured by YAML.")
FINDINGS = ("FAILING CHECKS AND OPEN FINDINGS (from run crun_x):\n- the README's React-style "
            "dashboard note is stale\n\nFix ONLY these.")


def _root(env, instructions: str) -> str:  # noqa: F811
    rid = f"crun_{uuid.uuid4().hex[:12]}"
    with env.engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, "
            "state, finalize_ctx) VALUES (:id, :p, NULL, 'failed', 'build', :t, "
            "CAST(:spec AS JSONB), '{}'::jsonb, '{}'::jsonb)"),
            {"id": rid, "p": env.project, "t": "KAN-9 Northgate",
             "spec": json.dumps({"instructions": instructions, "acceptance_criteria": [],
                                 "test_commands": [], **REPO})})
    return rid


def _follow_up(env, root: str) -> dict:  # noqa: F811
    with env.engine.begin() as c:
        rid = cts.start_code_task(
            c, project_id=env.project, task_id=None,
            title=f"Follow-up: checks failing — KAN-9 {uuid.uuid4().hex[:6]}",
            instructions=FINDINGS, acceptance_criteria=["Fixed: the README note"],
            test_commands=[], finalize_ctx={"deferred_from_run": root, "is_followup": True,
                                            "root_run_id": root, "parent_run_id": root},
            jobs=_Jobs(), simulated=True, **REPO)
        return c.execute(text("SELECT spec FROM code_task_runs WHERE id = :i"),
                         {"i": rid}).scalar()


def test_a_follow_up_of_a_go_lineage_is_gated_as_go(env, tmp_path, monkeypatch):  # noqa: F811
    spec = _follow_up(env, _root(env, ROOT_BRIEF))
    assert spec["required_stack"] == [{"language": "go", "from": "lineage"}]
    assert vt.required_app_stack(spec) == "go"

    manifest = tmp_path / "toolchains.json"
    manifest.write_text(json.dumps({"go": {"go": "1.27.4"}}))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module northgate\n\ngo 1.22\n")
    (repo / "package.json").write_text(json.dumps({"name": "x", "scripts": {"test": "go test"}}))
    plan = vt.resolve_gate_plan(spec, "Follow-up", repo, pinned=vt.pin_verify_manifest(repo))
    assert plan.commands == list(toolchains.GO_GATE)
    # The findings' words name React; the lineage's stack keeps BOSS's Node hardening out.
    assert build_scope.decide("Follow-up", spec["instructions"], spec=spec)["bundler"] is False


def test_a_lineage_that_declared_no_stack_stamps_none(env):  # noqa: F811
    spec = _follow_up(env, _root(env, "Build a URL shortener with a small React page."))
    assert "required_stack" not in spec


def test_the_lineage_reader_never_raises_on_an_unknown_root(env):  # noqa: F811
    with env.engine.begin() as c:
        assert vt.lineage_required_stack(c, "crun_does_not_exist") is None
    assert vt.lineage_required_stack(None, "crun_x") is None
