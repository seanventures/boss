"""Comprehensive acceptance tests for Principal Engineer Core (BOSSAPP-784, 785, 786, 787, 788, 792).

Covers every "Done means" acceptance criterion across PE-A, PE-B, PE-C, PE-D, PE-F, and PE-L:
  - PE-A (784): Study stage & StudyRecord via scripted model naming all 5 causes (each reproduced),
                red-first gate enforcement, inverted risk tier for existing code, read-only KG
                tools exposed on mcp_gateway/code_task_mcp, PR claim linking + unverified tag.
  - PE-B (785): Steer in build N recalled in build N+1 on same area (with real Postgres + evidence),
                agent console instructions changing governed build prompt, repeated/confirmed
                learnings proposed as scoped standards, 5 seeded PE engineering opinions + token budget.
  - PE-C (786): Change past 12k chars in a large file fully visible uncut to reviewer + seat split,
                review prompt includes blast-radius callers (no longer task-only), 2026-10-31
                preview-failure-to-not_applicable relabel heuristic flagged.
  - PE-D (787): Transitive dependency test selection catches an untouched caller broken by a change
                (plus P0 tests), replay gate + Replay outcomes section rendered in PR description.
  - PE-F (788): Jev-first 5-rung decision ladder with decider attribution & override rate,
                pre-step applicability decides Preview not_applicable from manifest requires:sudo
                before launch with evidence, reversible deadline applies recommendation while
                irreversible reminds/escalates (never silently expires) in real Postgres.
  - PE-L (792): Versioned done contracts for all 6 output kinds, tools refuse 2+ related tickets
                without a parent and PR without evidence section, fresh-context completion check
                sends gaps back and passes on a parented, ordered, linked ticket set.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

_DB_URL = (
    os.environ.get("BOSS_TEST_DATABASE_URL")
    or os.environ.get("BOSS_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or ""
)
_NEEDS_DB = pytest.mark.skipif(not _DB_URL.startswith("postgres"), reason="Postgres not configured")


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema, get_engine

    e = get_engine()
    ensure_schema(e)
    return e


def _mk_project(eng, tag: str) -> dict[str, str]:
    suf = uuid.uuid4().hex[:8]
    oid = f"org_{tag}_{suf}"
    wid = f"ws_{tag}_{suf}"
    pid = f"proj_{tag}_{suf}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:i, :n)"), {"i": oid, "n": oid})
        c.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, :n)"),
            {"i": wid, "o": oid, "n": wid},
        )
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, :n, :s)"),
            {"i": pid, "w": wid, "n": pid, "s": pid},
        )
    return {"org": oid, "ws": wid, "proj": pid}


# ── PE-A (BOSSAPP-784): Study before change ──────────────────────────────────────────────────

def test_784_study_stage_replay_names_all_five_causes_and_enforces_red_first():
    from boss_engine.services import mcp_gateway, pipeline_profiles, pr_description, study_record

    # Scripted model simulating the §5 replay benchmark producing all 5 reproduced causes.
    five_causes_payload = {
        "symptoms": [
            "Preview step marked not_applicable after container failure",
            "Large file diff truncated past 12k chars",
            "Untouched caller broken without triggering regression tests",
            "Operator steer in build N forgotten in build N+1",
            "Irreversible decision silently expired at deadline",
        ],
        "reproductions": [
            {"command": "pytest tests/test_preview_relabel.py", "observed": "FAILED: status=not_applicable", "passed": False, "cause_id": "C1"},
            {"command": "pytest tests/test_review_truncation.py", "observed": "FAILED: hunk past 12k missing", "passed": False, "cause_id": "C2"},
            {"command": "pytest tests/test_transitive_caller.py", "observed": "FAILED: caller test not selected", "passed": False, "cause_id": "C3"},
            {"command": "pytest tests/test_steer_recall.py", "observed": "FAILED: steer not in prompt", "passed": False, "cause_id": "C4"},
            {"command": "pytest tests/test_deadline_expiry.py", "observed": "FAILED: status=expired", "passed": False, "cause_id": "C5"},
        ],
        "causes": [
            {"id": "C1", "location": "engine/boss_engine/services/preview_sandbox_service.py:412", "summary": "Failure relabelled as not_applicable", "reproduced": True},
            {"id": "C2", "location": "engine/boss_engine/services/review_input.py:129", "summary": "MAX_PER_FILE truncated changed lines past 12k", "reproduced": True},
            {"id": "C3", "location": "engine/boss_engine/services/verify_manifest.py:478", "summary": "Direct-only import matching skipped transitive callers", "reproduced": True},
            {"id": "C4", "location": "engine/boss_engine/services/governed_code_task_service.py:2425", "summary": "Build prompt omitted agent setup and steers", "reproduced": True},
            {"id": "C5", "location": "engine/boss_engine/services/decisions.py:140", "summary": "Hold decisions silently expired on deadline", "reproduced": True},
        ],
        "existing_mechanisms": [
            "review_input.render",
            "verify_manifest._select",
            "learning_writeback.record",
            "DecisionService.expire_overdue",
        ],
        "classification": "boss_bug",
        "blast_radius": {
            "callers": ["governed_code_task_service.execute_governed_code_task", "DecisionService.list_decisions"],
            "flows": ["governed build", "pr review", "decision inbox"],
            "tests": ["engine/tests/test_verify_manifest.py", "engine/tests/test_review_input.py"],
            "risk_level": "high",
        },
        "alternatives": [
            {"option": "Patch symptoms individually", "pros": "Quick", "cons": "Leaves structural gaps", "chosen": False},
            {"option": "Install PE-A..L mechanisms", "pros": "Root-cause fix across pipeline", "cons": "Touches 6 services", "chosen": True},
        ],
    }

    class _ScriptedModel:
        def create_text_response(self, prompt: str):
            assert "Study stage" in prompt
            return ("resp_1", json.dumps(five_causes_payload), {"cost_usd_micros": 0})

    out = study_record.run_study_stage(
        _ScriptedModel(),
        task_title="Replay benchmark: 5 principal engineer gaps",
        task_instructions="Investigate all 5 observed failures before editing.",
        changed_paths=["engine/boss_engine/services/review_input.py"],
        is_fix=True,
    )
    assert out["valid"] is True, f"unexpected study gaps: {out['gaps']}"
    assert len(out["record"]["causes"]) == 5
    assert all(c["reproduced"] for c in out["record"]["causes"])

    # Red-first gate refuses a fix with only passing or empty reproductions.
    bad_rec = {**five_causes_payload, "reproductions": [{"command": "pytest", "observed": "all passed", "passed": True}]}
    ok_red, red_gaps = study_record.enforce_red_first(bad_rec, is_fix=True)
    assert ok_red is False
    assert any("red-first" in g.lower() for g in red_gaps)

    # Risk tier inversion: edit to existing code starts at >= 2 for Study, escalates to 3 on high blast radius.
    profile = pipeline_profiles.resolve_profile("lean")
    assert pipeline_profiles.study_tier_for({"title": "tweak button label"}, profile, is_existing_code=True) >= 2
    assert (
        pipeline_profiles.study_tier_for(
            {"title": "tweak button label"},
            profile,
            is_existing_code=True,
            blast_radius={"risk_level": "high", "callers": ["a", "b", "c", "d", "e"]},
        )
        == 3
    )

    # Read-only KG tools are exposed in build harness MCP gateway KG_TOOLS (boss_kg server).
    tool_names = {t["name"] for t in mcp_gateway.KG_TOOLS}
    for kg_tool in ("kg_callers", "kg_blast_radius", "kg_precedent", "kg_why"):
        assert kg_tool in tool_names

    # PR claim linking: backed claim is verified; unlinked claim is tagged unverified.
    linked = study_record.link_pr_claims(
        [
            "Fixed review_input.py:129 truncation of changed lines past 12k chars",
            "Rewrote the billing webhook retry queue for Stripe v9",
        ],
        out["record"],
    )
    assert linked[0]["verified"] is True and "C2" in linked[0]["study_refs"]
    assert linked[1]["verified"] is False and linked[1]["status"] == "unverified"

    md = pr_description.render(
        title="PE-A Study PR",
        evidence={
            "study_record": out["record"],
            "claims": [
                "Fixed review_input.py:129 truncation of changed lines",
                "Claim with no study backing whatsoever",
            ],
        },
        gates={"code_written": True},
        paths=["engine/boss_engine/services/review_input.py"],
    )
    assert "### Study record" in md
    assert "[verified:" in md
    assert "[unverified]" in md


# ── PE-B (BOSSAPP-785): Corrections become memory; agent setup loaded ────────────────────────

@_NEEDS_DB
def test_785_steer_recalled_in_next_build_and_console_instructions_change_prompt(eng):
    from boss_engine.services import governed_code_task_service as gcts
    from boss_engine.services import pe_memory

    w = _mk_project(eng, "pe785")
    pid, oid = w["proj"], w["org"]

    with eng.begin() as c:
        # 1. Seed a role with console instructions.
        c.execute(
            text(
                "INSERT INTO roles (id, project_id, role_key, display_name, spec) "
                "VALUES (:id, :p, 'builder', 'Builder', CAST(:spec AS JSONB))"
            ),
            {
                "id": f"role_{uuid.uuid4().hex[:8]}",
                "p": pid,
                "spec": json.dumps({
                    "instructions": "Always validate tenant_id before querying billing tables.",
                    "console_instructions": "Prefer explicit SQLAlchemy bind params over string interpolation.",
                }),
            },
        )

        # 2. Build N receives an operator steer on the billing area -> stored with real evidence.
        rec1 = pe_memory.record_correction(
            c,
            project_id=pid,
            source="steer",
            note="Never skip idempotency keys on billing invoice retries.",
            area="billing",
            paths=["engine/boss_engine/services/billing.py"],
            run_id="crun_build_n",
            actor="sean",
        )
        assert rec1["evidence"] and rec1["evidence"][0]["observed"] is True
        assert rec1["memory_id"]

        # Repeating the same steer proposes a scoped standard automatically.
        rec2 = pe_memory.record_correction(
            c,
            project_id=pid,
            source="steer",
            note="Never skip idempotency keys on billing invoice retries.",
            area="billing",
            paths=["engine/boss_engine/services/billing.py"],
            run_id="crun_build_n2",
            actor="sean",
        )
        assert rec2["repeat_count"] >= 2
        assert rec2["proposed_standard"] is not None
        assert "engine/boss_engine/services/billing.py" in rec2["proposed_standard"]["scope_paths"]

        # 3. Build N+1 on the same area recalls the steer + console instructions + seeded opinions!
        prompt_n1, pack_n1 = gcts.build_prompt_with_agent_setup(
            c,
            project_id=pid,
            title="Fix invoice retry bug",
            instructions="Handle transient 503s cleanly.",
            criteria=["Idempotent retries"],
            role_key="builder",
            touched_paths=["engine/boss_engine/services/billing.py"],
            areas=["billing"],
            repo_standards=[
                {"paths": ["engine/boss_engine/services/billing.py"], "step": "implement", "rule": "Use Decimal for money."},
                {"paths": ["apps/web/"], "step": "implement", "rule": "Use Tailwind tokens."},
            ],
            org_id=oid,
        )

    assert "Never skip idempotency keys on billing invoice retries." in prompt_n1
    assert "Always validate tenant_id before querying billing tables." in prompt_n1
    assert "Prefer explicit SQLAlchemy bind params" in prompt_n1
    assert "Use Decimal for money." in prompt_n1
    assert "Use Tailwind tokens." not in prompt_n1  # path-scoped out!
    assert len(pack_n1["opinions"]) == 5

    # Updating console instructions changes the governed build's prompt.
    with eng.begin() as c:
        c.execute(
            text(
                "UPDATE roles SET spec = CAST(:spec AS JSONB) "
                "WHERE project_id = :p AND role_key = 'builder'"
            ),
            {
                "p": pid,
                "spec": json.dumps({
                    "instructions": "CUSTOM CONSOLE DIRECTIVE: emit structured audit events on every write."
                }),
            },
        )
        prompt_after, _ = gcts.build_prompt_with_agent_setup(
            c,
            project_id=pid,
            title="Fix invoice retry bug",
            instructions="Handle transient 503s cleanly.",
            role_key="builder",
            touched_paths=["engine/boss_engine/services/billing.py"],
            areas=["billing"],
            org_id=oid,
        )
    assert "CUSTOM CONSOLE DIRECTIVE: emit structured audit events on every write." in prompt_after


# ── PE-C (BOSSAPP-786): Full diff visibility, blast-radius review, outcome-relabel guard ───────

def test_786_large_change_past_12k_visible_blast_radius_in_prompt_and_relabel_flagged():
    from boss_engine.services import governed_code_task_service as gcts
    from boss_engine.services import review_input

    # Large file > 18,000 chars with a real change PAST char 15,000.
    prefix = "# padding line\n" * 1100  # ~16,500 chars
    sentinel_line = "    return charge_customer_twice_bug(order_id)"
    large_file = prefix + sentinel_line + "\n"
    diff_hunk = f"@@ -1100,3 +1100,4 @@\n # padding line\n+{sentinel_line}\n"

    change = review_input.ChangeUnderReview(
        paths=["engine/boss_engine/services/big_service.py"],
        files={"engine/boss_engine/services/big_service.py": large_file},
        measured="main",
    )
    rendered = review_input.render(change, diffs={"engine/boss_engine/services/big_service.py": diff_hunk})
    assert sentinel_line in rendered, "change past 12k chars must be fully visible in reviewer input"
    assert "=== DIFF HUNKS: engine/boss_engine/services/big_service.py ===" in rendered

    # Seat splitting preserves every file's hunks without truncation.
    change2 = review_input.ChangeUnderReview(
        paths=["a.py", "b.py"],
        files={"a.py": "x" * 2000, "b.py": "y" * 2000},
        measured="main",
    )
    shards = review_input.split_for_seats(
        change2,
        diffs={"a.py": "+MARKER_A_UNCUT\n" + ("+a\n" * 300), "b.py": "+MARKER_B_UNCUT\n" + ("+b\n" * 300)},
        max_total=1200,
    )
    assert len(shards) == 2
    assert any("MARKER_A_UNCUT" in s for s in shards)
    assert any("MARKER_B_UNCUT" in s for s in shards)

    # Review prompt drops "Judge ONLY against the stated task" and includes blast-radius callers.
    prompt = gcts._review_prompt(
        "Update parser",
        ["Handles empty input"],
        change=rendered,
        blast_radius={"callers": ["api.routers.orders.create_order"], "flows": ["checkout"]},
    )
    assert "Judge ONLY against the stated task" not in prompt
    assert "api.routers.orders.create_order" in prompt
    assert "BLAST RADIUS IN SCOPE" in prompt

    # 2026-10-31 replay: preview-failure relabelled to not_applicable is flagged!
    relabel_diff = """\
diff --git a/engine/boss_engine/services/preview_sandbox_service.py b/engine/boss_engine/services/preview_sandbox_service.py
@@ -410,5 +410,7 @@ def run_preview():
     if proc.exit_code != 0:
-        return {"status": "failed", "error": proc.stderr}
+        # heuristic: treat preview startup failure as not_applicable
+        return {"status": "not_applicable", "error": proc.stderr}
"""
    verdict = review_input.detect_outcome_relabel(
        relabel_diff, ["engine/boss_engine/services/preview_sandbox_service.py"]
    )
    assert verdict["flagged"] is True
    assert verdict["requires_blast_radius"] is True
    assert verdict["requires_study_justification"] is True


# ── PE-D (BOSSAPP-787): Transitive test selection + replay gate in PR ────────────────────────

def test_787_transitive_caller_tests_selected_and_replay_outcomes_in_pr(tmp_path: Path):
    from boss_engine.services import pr_description, verify_manifest

    # Create a 3-hop chain: core.py <- helper.py <- untouched_caller.py <- test_untouched_caller.py
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "core.py").write_text("def parse(x): return int(x)\n", encoding="utf-8")
    (tmp_path / "pkg" / "helper.py").write_text("from pkg.core import parse\ndef wrap(x): return parse(x)\n", encoding="utf-8")
    (tmp_path / "pkg" / "untouched_caller.py").write_text("from pkg.helper import wrap\ndef run(x): return wrap(x)\n", encoding="utf-8")
    (tmp_path / "tests" / "test_untouched_caller.py").write_text(
        "from pkg.untouched_caller import run\ndef test_ok(): assert run('1') == 1\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_p0_smoke.py").write_text("def test_smoke(): assert True\n", encoding="utf-8")

    suite = verify_manifest.VerifySuite(
        name="engine",
        paths=("pkg/**", "tests/**"),
        run=("pytest -q {tests}",),
        tests_glob="tests/test_*.py",
        by="imports",
        p0_tests=("tests/test_p0_*.py",),
    )
    selected, reason = verify_manifest.select_tests(tmp_path, suite, ["pkg/core.py"])
    assert "tests/test_untouched_caller.py" in selected, "untouched transitive caller test must be selected"
    assert "tests/test_p0_smoke.py" in selected, "P0 suite test must always be included"
    assert "transitive" in reason

    # Replay gate + PR Replay outcomes section.
    replays = [
        {"run_id": "wfr_fail_001", "before_status": "failed", "after_status": "passed", "after_ok": True, "note": "replayed on fix commit"},
        {"run_id": "wfr_fail_002", "before_status": "failed", "after_status": "passed", "after_ok": True},
    ]
    gate = verify_manifest.evaluate_replay_gate(replays)
    assert gate["ok"] is True and gate["passed"] == 2 and gate["failed"] == 0

    pr_md = pr_description.render(
        title="Fix core parser regression",
        evidence={"replay_outcomes": replays, "tests_passed": True, "test_cmd": "pytest -q"},
        gates={"nothing_broke": True},
        paths=["pkg/core.py"],
    )
    assert "### Replay outcomes" in pr_md
    assert "`wfr_fail_001`: `failed` → `passed`" in pr_md


# ── PE-F (BOSSAPP-788): Jev-first ladder, pre-step applicability, safe deadline defaults ─────

@_NEEDS_DB
def test_788_applicability_before_launch_ladder_and_deadline_policy(eng):
    from boss_engine.config import Settings
    from boss_engine.services import core, decision_ladder
    from boss_engine.services.decisions import DecisionService

    # 1. BOSS's own repo: Preview requires sudo -> decided not_applicable BEFORE launch with evidence.
    app_profile = {
        "capabilities": {"sudo": False, "docker": True},
        "manifest": {
            "steps": {
                "preview": {"requires": {"sudo": True}},
                "unit_tests": {"requires": {"docker": True}},
            }
        },
    }
    prev_verdict = decision_ladder.evaluate_step_applicability("preview", app_profile)
    assert prev_verdict["applicable"] is False
    assert prev_verdict["status"] == "not_applicable"
    assert prev_verdict["before_launch"] is True
    assert prev_verdict["decider"] == "coded_fact"
    assert any("sudo" in e for e in prev_verdict["evidence"])

    unit_verdict = decision_ladder.evaluate_step_applicability("unit_tests", app_profile)
    assert unit_verdict["applicable"] is True

    # 2. Decision ladder: Jev unsure -> escalates to global model -> tracks decider & override rate.
    ladder = decision_ladder.DecisionLadder(confidence_threshold=0.8)
    d1 = ladder.decide(
        "what_next",
        context={"error": "transient 502"},
        coded_check=lambda _ctx: None,
        jev_decider=lambda _ctx: {"answer": "retry", "confidence": 0.55, "evidence": ["jev low conf"]},
        global_decider=lambda _ctx: {"answer": "retry", "confidence": 0.92, "evidence": ["502 is transient"]},
    )
    assert d1.decider == "global_model"
    assert d1.answer == "retry"
    assert d1.escalated_rungs == ["coded_fact", "jev"]
    ladder.record_override(d1, by="operator")
    assert ladder.override_rate(decider="global_model") == 1.0

    # 3. Real Postgres deadline expiry: reversible auto-applies recommendation; irreversible stays open!
    w = _mk_project(eng, "pe788")
    pid = w["proj"]
    settings = Settings()
    svc = DecisionService(settings)
    past = core.now_utc() - timedelta(minutes=10)

    with eng.begin() as c:
        rev_id = core.make_id("dec")
        c.execute(
            text(
                "INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, "
                "default_action, deadline_at, status) "
                "VALUES (:id, :p, 'Auto-retry build?', CAST(:rec AS JSONB), 1, "
                "'apply_recommendation', :dl, 'pending')"
            ),
            {"id": rev_id, "p": pid, "rec": json.dumps({"action": "retry", "reversible": True}), "dl": past},
        )
        irrev_id = core.make_id("dec")
        c.execute(
            text(
                "INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, "
                "default_action, deadline_at, status) "
                "VALUES (:id, :p, 'Drop production table?', CAST(:rec AS JSONB), 3, "
                "'hold', :dl, 'pending')"
            ),
            {"id": irrev_id, "p": pid, "rec": json.dumps({"action": "drop_table", "reversible": False}), "dl": past},
        )
        expired_count = svc.expire_overdue(c, pid)
        assert expired_count == 0, "neither reversible nor irreversible decisions should silently expire"

        rev_row = c.execute(
            text("SELECT status, approved_by FROM decisions WHERE id = :i"), {"i": rev_id}
        ).mappings().one()
        irrev_row = c.execute(
            text("SELECT status, deadline_at FROM decisions WHERE id = :i"), {"i": irrev_id}
        ).mappings().one()

    assert rev_row["status"] == "approved"
    assert rev_row["approved_by"] == "system:deadline_default"
    assert irrev_row["status"] == "pending"  # kept open, reminded & escalated, never expired!
    assert irrev_row["deadline_at"] is None


# ── PE-L (BOSSAPP-792): Done contracts, tool enforcement, completion check ───────────────────

def test_792_done_contracts_tool_enforcement_and_completion_check():
    from boss_engine.services import done_contracts
    from boss_engine.tools import github_tools

    # All 6 output kinds have versioned contracts.
    assert set(done_contracts.DONE_CONTRACTS) == {"ticket_set", "pr", "fix", "decision", "doc", "deploy"}

    # 1. Refuse 2+ related tickets without a parent.
    orphan_tickets = [
        {"title": "Implement StudyRecord", "order": 1, "spec_link": "docs/PE.md"},
        {"title": "Wire KG tools", "order": 2, "spec_link": "docs/PE.md"},
    ]
    ok, gaps = done_contracts.enforce_ticket_set_contract(orphan_tickets)
    assert ok is False
    assert any("parent" in g.lower() for g in gaps)

    # 2. Refuse PR without evidence section — both directly AND at the github_open_fix_pr tool boundary!
    ok_pr, pr_gaps = done_contracts.enforce_pr_contract("Fix login bug", "Changed two lines in auth.py.")
    assert ok_pr is False
    assert any("evidence" in g.lower() for g in pr_gaps)

    tool_res = github_tools._fix_pr(
        ctx=None,
        project_id="proj_1",
        actor="agent",
        args={"title": "Fix login bug", "summary": "No verification section here", "report": ""},
    )
    assert tool_res["ok"] is False
    assert tool_res["code"] == "done_contract_failed"

    # 3. Fresh-context completion check sends gaps back, then passes on parented/ordered/linked set.
    bad_check = done_contracts.run_completion_check(
        "ticket_set",
        request="file a spec and tickets for PE core",
        output={"tickets": orphan_tickets},
    )
    assert bad_check["ok"] is False
    assert bad_check["actionable_feedback"]

    good_tickets = [
        {"title": "PE-A StudyRecord", "parent_id": "BOSSAPP-783", "order": 1, "spec_link": "docs/product/PRINCIPAL_ENGINEER_INTELLIGENCE.md#3a"},
        {"title": "PE-B Memory", "parent_id": "BOSSAPP-783", "order": 2, "depends_on": ["PE-A"], "spec_link": "docs/product/PRINCIPAL_ENGINEER_INTELLIGENCE.md#3b"},
    ]
    good_check = done_contracts.run_completion_check(
        "ticket_set",
        request="file a spec and tickets for PE core",
        output={"tickets": good_tickets},
        adapter=lambda _p: '{"ready": true, "gaps": []}',
    )
    assert good_check["ok"] is True and good_check["gaps"] == []

    # 4. Human structural correction becomes a proposed contract check (extending PE-B).
    prop = done_contracts.propose_contract_check_from_correction(
        "Every deploy contract must name a monitoring dashboard URL",
        output_kind="deploy",
        required_field="dashboard_url",
    )
    ok_deploy, deploy_gaps = done_contracts.check_done_contract(
        "deploy",
        {"target": "prod", "rollback_plan": "revert tag", "health_check": "GET /health"},
        extra_checks=[prop],
    )
    assert ok_deploy is False
    assert any("dashboard" in g.lower() for g in deploy_gaps)
