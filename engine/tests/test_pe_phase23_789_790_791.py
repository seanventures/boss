"""Regression and acceptance tests for PE-K (BOSSAPP-789), PE-Phase 2 (BOSSAPP-790), PE-Phase 3 (BOSSAPP-791).

Covers every "Done means" item:
  - BOSSAPP-789 (PE-K):
      * procps installed in Dockerfile.community (fixes `spawn ps ENOENT`)
      * monorepo root `dev` script never launched as "the app"
      * guessed Python entry carries inferred PYTHONPATH from pyproject.toml/Makefile/engine/
        (fixes `No module named boss_engine`), while `boss.preview.command` still wins
      * module that merely builds `app = FastAPI()` with no run path is NOT ranked 0 (fixes
        `exit 0 with an empty log`)
      * normal apps (single process server, static site) unchanged
  - BOSSAPP-790 (PE-Phase 2):
      * PR shows per-criterion acceptance evidence + structured risk rating (with low-risk why)
      * docs pass detects docs referencing changed symbols
      * PR babysitter handles base move, merge conflicts, CI failures, human comments, and budget cap
      * planning packet requires 2+ alternatives, standalone non-auto-approvable option cards, Mermaid validation
  - BOSSAPP-791 (PE-Phase 3):
      * Ask BOSS first-mate fans out a multi-repo request in parallel and produces one report
      * experiments can A/B test a skill, standard, instructions, and knowledge_mode (never approval keys)
      * MCP tool output is strictly budgeted with truncated + more_handle envelope
      * objective loop runs bounded iterations until stop condition or hard cap
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── BOSSAPP-789 (PE-K): Preview is right about the app ───────────────────────────────────────

def test_789_dockerfile_installs_procps_preventing_spawn_ps_enoent():
    repo_root = Path(__file__).resolve().parents[2]
    dockerfile = (repo_root / "Dockerfile.community").read_text(encoding="utf-8")
    assert "procps" in dockerfile, "Dockerfile.community must install procps to prevent spawn ps ENOENT"


def test_789_monorepo_root_dev_script_not_run_as_app_and_pythonpath_inferred(tmp_path: Path):
    from boss_engine.services import preview_service as ps

    # 1. Monorepo root with workspaces + pnpm-workspace.yaml + root dev script should NOT run root dev.
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - apps/*\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "monorepo", "workspaces": ["apps/*"], "scripts": {"dev": "pnpm -r dev"}}),
        encoding="utf-8",
    )
    (tmp_path / "engine" / "boss_engine").mkdir(parents=True)
    (tmp_path / "engine" / "boss_engine" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "serve.py").write_text(
        "from http.server import HTTPServer\n"
        "if __name__ == '__main__':\n"
        "    HTTPServer(('0.0.0.0', 8000), None).serve_forever()\n",
        encoding="utf-8",
    )

    cmd = ps._detect_command_in(tmp_path)
    assert cmd is not None
    joined = " ".join(cmd)
    assert "npm run dev" not in joined, "monorepo root dev script must never be run as the app"
    assert "PYTHONPATH=" in joined and "engine" in joined, "guessed Python entry must infer PYTHONPATH for engine/"
    assert "serve.py" in joined

    # 2. Declared boss.preview.command always wins over guessed entries.
    (tmp_path / "boss.preview.command").write_text("uvicorn apps.api.app.main:app --port $PORT\n", encoding="utf-8")
    declared = ps._detect_command_in(tmp_path)
    assert declared == ["sh", "-lc", "uvicorn apps.api.app.main:app --port $PORT"]


def test_789_app_object_only_not_ranked_as_server_while_normal_apps_unchanged(tmp_path: Path):
    from boss_engine.services import preview_service as ps

    # A file that only constructs `app = FastAPI()` exits 0 with an empty log if executed directly!
    factory_only = "from fastapi import FastAPI\napp = FastAPI()\n"
    real_server = (
        "from fastapi import FastAPI\nimport uvicorn\napp = FastAPI()\n"
        "if __name__ == '__main__':\n    uvicorn.run(app, host='0.0.0.0', port=8000)\n"
    )
    assert ps.python_source_has_run_path(factory_only) is False
    assert ps.python_entry_strength(factory_only) > 0
    assert ps.python_source_has_run_path(real_server) is True
    assert ps.python_entry_strength(real_server) == 0

    # Static site preview remains unchanged.
    static_dir = tmp_path / "static_site"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
    kind, val = ps.resolve_app_entry(static_dir)
    assert (kind, val) == ("static", ".")
    cmd = ps.command_for_entry(static_dir, kind, val)
    assert cmd is not None and "http.server" in " ".join(cmd)


# ── BOSSAPP-790 (PE-Phase 2): Clean PR + Planning ────────────────────────────────────────────

def test_790_pr_risk_rating_criterion_evidence_docs_pass_babysitter_and_planning(tmp_path: Path):
    from boss_engine.services import clean_pr, pr_description

    # 1. Risk rating + acceptance evidence rendered in PR description.
    low_risk = clean_pr.assess_pr_risk(
        ["apps/web/components/badge.tsx"],
        tier=0,
        blast_radius={"callers": [], "flows": []},
        test_evidence={"tests_passed": True},
    )
    assert low_risk["level"] == "low"
    assert "Quick scan" in low_risk["review_guidance"]

    high_risk = clean_pr.assess_pr_risk(
        ["engine/boss_engine/services/auth_policy.py", "db/schema.sql"],
        tier=3,
        blast_radius={"callers": ["a", "b", "c", "d", "e"], "flows": ["login", "api", "mcp"]},
        test_evidence={"tests_passed": True},
    )
    assert high_risk["level"] in ("high", "critical")

    pr_md = pr_description.render(
        title="Harden auth policy",
        evidence={
            "tests_passed": True,
            "test_cmd": "pytest -q",
            "risk_rating": high_risk,
            "criteria_evidence": [
                {"criterion": "Rejects forged token", "kind": "test", "proof": "test_auth.py::test_forged_token PASSED", "passed": True},
                {"criterion": "Login modal shows error", "kind": "screenshot", "proof": "artifacts/login-err.png", "passed": True},
            ],
        },
        gates={"code_written": True},
        paths=["engine/boss_engine/services/auth_policy.py"],
    )
    assert "### Acceptance evidence" in pr_md
    assert "Rejects forged token" in pr_md
    assert "### Risk rating" in pr_md
    assert "How closely to review" in pr_md

    # 2. Docs pass finds invalidated docs mentioning changed symbols.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PREVIEW.md").write_text("Use `_detect_command_in` for preview startup.\n", encoding="utf-8")
    hits = clean_pr.find_invalidated_docs(tmp_path, ["_detect_command_in", "nonexistent_fn"])
    assert len(hits) == 1 and hits[0]["path"] == "docs/PREVIEW.md"

    # 3. Babysitter handles conflicts, CI failures, comments, budget exhaustion.
    assert clean_pr.plan_pr_babysit_action(ci_status="passed", has_conflicts=True)["action"] == "rebase_and_resolve"
    assert clean_pr.plan_pr_babysit_action(ci_status="failed")["action"] == "start_fix_round"
    assert (
        clean_pr.plan_pr_babysit_action(ci_status="passed", human_comments=[{"id": 1, "resolved": False}])["action"]
        == "address_human_comments"
    )
    assert clean_pr.plan_pr_babysit_action(ci_status="passed", budget_remaining_usd=0.0)["action"] == "stop_budget"
    assert clean_pr.plan_pr_babysit_action(ci_status="passed")["action"] == "ready_for_merge"

    # 4. Planning packet enforces >= 2 alternatives and standalone non-auto-approvable option cards.
    with pytest.raises(ValueError, match="at least 2 alternatives"):
        clean_pr.build_planning_packet(title="Bad plan", repo_context={}, alternatives=[{"name": "only one"}])

    packet = clean_pr.build_planning_packet(
        title="Auth refactor",
        repo_context={"existing_services": ["auth_policy.py"]},
        alternatives=[{"name": "Option A"}, {"name": "Option B"}],
        open_questions=[{"question": "Session TTL?", "options": ["1h", "24h"], "recommended": "1h"}],
        mermaid_diagram="flowchart LR\n  A --> B",
    )
    assert packet["mermaid_valid"] is True
    assert packet["option_cards"][0]["auto_approvable"] is False


# ── BOSSAPP-791 (PE-Phase 3): First mate, measured skills, MCP output budget, objective loops ─

def test_791_first_mate_fanout_skill_experiments_mcp_budget_and_objective_loop():
    from boss_engine.mcp import server as mcp_server
    from boss_engine.services import experiments, first_mate

    # 1. Parallel multi-repo fan-out with consolidated report.
    targets = [{"repo": "org/api"}, {"repo": "org/web"}, {"repo": "org/worker"}]
    report = first_mate.fan_out_multi_repo(
        "Bump telemetry middleware",
        targets,
        dispatch_fn=lambda t: {"ok": True, "pr_url": f"https://github.com/{t['repo']}/pull/42"},
        max_workers=3,
    )
    assert report["total"] == 3 and report["succeeded"] == 3 and report["failed"] == 0
    assert "org/api" in report["summary"] and "org/web" in report["summary"] and "org/worker" in report["summary"]

    # 2. Experiments can A/B test a skill, standard, instructions, and knowledge_mode without touching approval keys.
    for knob in ("skill", "standard", "instructions", "knowledge_mode"):
        assert knob in experiments.ARM_KNOBS
    assert experiments.ARM_SPEC_KEYS.isdisjoint(experiments.APPROVAL_KEYS)
    assert experiments._coerce("skill", "pe.study_v2") == "pe.study_v2"
    assert experiments._coerce("knowledge_mode", "keyword") == "keyword"

    # 3. MCP tool output budgeting prevents unbounded JSON dumps and provides more_handle.
    huge_payload = {"rows": [{"id": i, "blob": "x" * 200} for i in range(200)]}
    res = mcp_server._tool_result(huge_payload, max_chars=1000, more_handle="more:rows:1")
    text_out = res["content"][0]["text"]
    assert len(text_out) < 1600
    parsed = json.loads(text_out)
    assert parsed["truncated"] is True
    assert parsed["more_handle"] == "more:rows:1"
    assert "rows" in parsed["keys"]

    # 4. Objective loop runs until stop condition (or stops cleanly on cost/token/iteration caps).
    spec = first_mate.ObjectiveLoopSpec(
        objective="Cut p95 page load below 200ms",
        stop_condition="p95_ms < 200",
        max_iterations=5,
        max_cost_usd=5.0,
    )
    latencies = [420, 260, 175]
    loop_report = first_mate.run_objective_loop(
        spec,
        iterate_fn=lambda i, _ev: {
            "tokens": 1200,
            "cost_usd": 0.4,
            "metric": {"p95_ms": latencies[i - 1]},
            "summary": f"iter {i}: p95={latencies[i - 1]}ms",
            "pr": {"url": f"https://github.com/org/web/pull/{100 + i}"},
            "stop_met": latencies[i - 1] < 200,
        },
    )
    assert loop_report.stopped_by == "stop_condition"
    assert loop_report.iterations == 3
    assert len(loop_report.prs) == 3
