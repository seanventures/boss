"""End-to-end acceptance tests for Quick Lane (BOSSAPP-795..799, QUICK_LANE.md).

Covers:
- QL-1 (BOSSAPP-795): dedicated per-host queue (runs alongside full builds without stalling),
  1 quick job per host, person-first priority, memory-aware admission, budget/receipt/merge-gate.
- QL-2 (BOSSAPP-796): all 5 context sources loaded & recorded on prompt + run record, read-only
  KG tools in build harness, reproduce-first gate blocking fixes without reproduction.
- QL-3 (BOSSAPP-797): independent validator on a different model/fresh context, rejecting
  synthetic fixtures, unread callers, unreproduced causes, out-of-scope permission edits, and
  reused builder tests; bounded warm-session revisions; decision-ladder escalation.
- QL-4 (BOSSAPP-798): 20-min wall-clock, cost, diff, protected-path, and blast-radius caps,
  lossless handoff to full workflow, 5-section PR body, bounded CI-after-open reopen.
- QL-5 (BOSSAPP-799): Ask BOSS offer, MCP create_code_task(lane="quick"), boss:quick label,
  HTTP API endpoints, worker runner registration.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import code_task_mcp, ticket_intake
from boss_engine.services.core import make_id
from boss_engine.services.quick_lane import (
    DEFAULT_COST_CAP_USD_MICROS,
    JOB_KIND,
    MAX_CI_REOPENS,
    MAX_WALL_CLOCK_SECONDS,
    QUICK_LANE_LABEL,
    IndependentTestResult,
    ValidationFinding,
    admit_quick_job,
    check_quick_limits,
    claim_next_quick_job,
    execute_quick_job,
    get_quick_run,
    handle_ci_result,
    lane_from_labels,
    load_full_context,
    render_context_prompt,
    select_validator_model,
    start_quick_fix,
    suggest_quick_fix_offer,
    triage_quick_lane,
    validate_quick_change,
    validate_reproduction,
)


def _pg_url() -> str | None:
    return os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.fixture
def pg_conn():
    url = _pg_url()
    if not url:
        pytest.skip("Postgres test database not configured (BOSS_TEST_DATABASE_URL)")
    eng = create_engine(url, future=True)
    with eng.begin() as conn:
        sfx = uuid.uuid4().hex[:8]
        org_id = f"org_ql_{sfx}"
        ws_id = f"ws_ql_{sfx}"
        proj_id = f"prj_ql_{sfx}"
        conn.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, :name) ON CONFLICT DO NOTHING"),
            {"id": org_id, "name": "QL Org"},
        )
        conn.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org_id, :name) ON CONFLICT DO NOTHING"),
            {"id": ws_id, "org_id": org_id, "name": "QL Workspace"},
        )
        conn.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:id, :ws_id, :name, :slug) ON CONFLICT DO NOTHING"
            ),
            {"id": proj_id, "ws_id": ws_id, "name": "QL Project", "slug": proj_id},
        )
        conn.execute(
            text(
                "INSERT INTO budget_policies ("
                "  id, scope_type, scope_id, daily_cap_usd_micros, monthly_cap_usd_micros, burst_cap_usd_micros_per_min"
                ") VALUES (:id, 'project', :p, 50000000, 100000000, 10000000) ON CONFLICT DO NOTHING"
            ),
            {"id": make_id("bpol"), "p": proj_id},
        )
        yield conn, proj_id
    eng.dispose()


def _valid_repro(checked_callers: tuple[str, ...] = ("app/views.py#render_card",)) -> dict:
    return {
        "command": "pytest -q tests/test_card.py -k test_empty_slug",
        "exit_code": 1,
        "output": "AssertionError: expected 'Untitled', got None at app/cards.py:42",
        "causes": [
            {"file": "app/cards.py", "line": 42, "explanation": "missing null guard on card.slug"}
        ],
        "existing_mechanism_checked": True,
        "existing_mechanism_note": "Reused normalize_title() in app/cards.py",
        "blast_radius_checked": True,
        "checked_callers": list(checked_callers),
        "real_data_shape_source": "code_index.symbol_key",
    }


def _good_independent_test() -> IndependentTestResult:
    return IndependentTestResult(
        command="pytest -q tests/validator_replay_card.py",
        passed=True,
        output="1 passed in 0.12s (replayed real symbol shape)",
        replayed_failing_run=True,
        used_real_data_shapes=True,
        test_paths=("tests/validator_replay_card.py",),
    )


# ---------------------------------------------------------------------------
# QL-1 (BOSSAPP-795): Queue isolation, per-host slot, priority, memory gate
# ---------------------------------------------------------------------------


def test_ql1_quick_job_runs_alongside_long_build_and_enforces_one_per_host(pg_conn):
    conn, pid = pg_conn
    # Simulate an active long build occupying code_task_runs on host-a.
    conn.execute(
        text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase) "
            "VALUES (:id, :p, 'Long feature build', 'running', 'implement')"
        ),
        {"id": make_id("crun"), "p": pid},
    )

    # Enqueue a system quick fix first, then a person-asked quick fix second.
    sys_run = start_quick_fix(
        conn,
        project_id=pid,
        title="System scheduled cleanup",
        instructions="Fix lint warning in helper",
        source="ticket_label",
        requested_by_person=False,
    )
    person_run = start_quick_fix(
        conn,
        project_id=pid,
        title="Person urgent bug fix",
        instructions="Fix crash on null slug",
        source="console",
        requested_by_person=True,
    )

    # Person-asked job claims FIRST even though system job was enqueued earlier,
    # AND is admitted on host-a despite the running long build.
    claimed1, dec1 = claim_next_quick_job(conn, host_id="host-a", project_id=pid, available_memory_mb=4096)
    assert dec1.admitted is True
    assert claimed1 is not None
    assert claimed1["id"] == person_run["run_id"]

    # A second quick job on the SAME host-a must wait (1 per host).
    claimed2, dec2 = claim_next_quick_job(conn, host_id="host-a", project_id=pid, available_memory_mb=4096)
    assert claimed2 is None
    assert dec2.admitted is False
    assert dec2.reason == "host_quick_slot_busy"

    # Meanwhile host-b has its own reserved quick slot and claims the system job!
    claimed3, dec3 = claim_next_quick_job(conn, host_id="host-b", project_id=pid, available_memory_mb=4096)
    assert dec3.admitted is True
    assert claimed3 is not None
    assert claimed3["id"] == sys_run["run_id"]


def test_ql1_memory_aware_admission_defers_with_stated_reason(pg_conn):
    conn, pid = pg_conn
    started = start_quick_fix(
        conn,
        project_id=pid,
        title="OOM guard test",
        instructions="Fix empty parser",
        source="console",
    )

    # Low host memory -> waits in waiting_memory with explicit reason.
    low = admit_quick_job(
        conn,
        run_id=started["run_id"],
        project_id=pid,
        host_id="host-tight",
        available_memory_mb=512,
        required_memory_mb=1536,
    )
    assert low.admitted is False
    assert low.reason == "insufficient_host_memory"
    assert "512 MB" in low.detail and "1536 MB" in low.detail

    row = get_quick_run(conn, project_id=pid, run_id=started["run_id"])
    assert row is not None
    assert row["status"] == "waiting_memory"
    assert "512 MB" in (row["waiting_reason"] or "")

    # Once memory recovers -> admitted cleanly.
    ok = admit_quick_job(
        conn,
        run_id=started["run_id"],
        project_id=pid,
        host_id="host-tight",
        available_memory_mb=2048,
        required_memory_mb=1536,
    )
    assert ok.admitted is True
    row2 = get_quick_run(conn, project_id=pid, run_id=started["run_id"])
    assert row2["status"] == "running"
    assert row2["waiting_reason"] is None


# ---------------------------------------------------------------------------
# QL-2 (BOSSAPP-796): Full context in + repro-first enforcement
# ---------------------------------------------------------------------------


def test_ql2_full_context_loaded_into_prompt_and_record_and_repro_enforced(pg_conn):
    conn, pid = pg_conn
    spec = {
        "title": "Fix card slug crash",
        "instructions": "Handle null slug without throwing.",
        "area": "cards",
        "touched_paths": ["app/cards.py"],
        "study_record": {"area": "cards", "summary": "Card slug normalization lives in app/cards.py"},
        "kg_context": {
            "callers": [{"symbol_key": "app/views.py#render_card"}],
            "blast_radius": ["app/views.py"],
            "precedent": [{"id": "prec_1", "summary": "Earlier slug null fix in PR #102"}],
            "why": [{"id": "why_1", "summary": "Slugs are optional for draft cards"}],
            "consulted_node_ids": ["kn_1", "kn_2"],
        },
        "learnings": [{"id": "lrn_1", "title": "Always guard optional card fields"}],
        "standards": [
            {"key": "cards.nulls", "title": "Guard nulls", "body": "Never assume slug is non-null", "scopes": ["app/cards"]},
            {"key": "billing.only", "title": "Billing rule", "body": "Out of scope", "scopes": ["billing/"]},
        ],
        "repro": "pytest tests/test_card.py fails with AttributeError",
        "failing_run_id": "run_fail_99",
    }

    bundle = load_full_context(conn, project_id=pid, spec=spec)
    sources = bundle.sources_used()
    for name in ("study_notes", "kg", "learnings", "standards", "request_evidence"):
        assert sources[name]["loaded"] is True, f"missing context source {name}"
    assert any("billing.only" in o for o in bundle.standards_omitted)

    prompt = render_context_prompt(bundle, spec)
    assert "study_notes" in prompt
    assert "kg" in prompt
    assert "learnings" in prompt
    assert "standards" in prompt
    assert "request_evidence" in prompt
    assert "app/views.py#render_card" in prompt
    assert "run_fail_99" in prompt

    # Build harness read-only KG tools exposed cleanly.
    assert "kg_callers" in code_task_mcp.READONLY_KG_TOOLS
    assert "kg_blast_radius" in code_task_mcp.READONLY_KG_TOOLS
    assert "mcp__boss_kg__kg_callers" in code_task_mcp.readonly_kg_tool_names()

    # A fix WITHOUT a reproduction must NOT proceed.
    started = start_quick_fix(conn, project_id=pid, title=spec["title"], instructions=spec["instructions"], spec_extra=spec)
    res = execute_quick_job(
        conn,
        project_id=pid,
        run_id=started["run_id"],
        host_id="host-ctx",
        available_memory_mb=4096,
        builder_session=lambda prompt_text, meta: {
            "diff_files": {"app/cards.py": "+    return slug or 'Untitled'\n"},
            "reproduction": None,  # missing reproduction!
        },
    )
    assert res["status"] == "failed"
    assert res["reason"] == "repro_missing"
    saved = get_quick_run(conn, project_id=pid, run_id=started["run_id"])
    assert saved["pull_request_url"] is None
    assert saved["context_bundle"]["sources_used"]["study_notes"]["loaded"] is True


# ---------------------------------------------------------------------------
# QL-3 (BOSSAPP-797): Independent validation catches 2026-10-31 mistakes
# ---------------------------------------------------------------------------


def test_ql3_validator_selects_different_model_and_catches_2026_10_31_mistakes():
    assert select_validator_model("anthropic/claude-sonnet", ["anthropic/claude-opus", "openai/gpt-5"]) == "openai/gpt-5"
    assert select_validator_model("claude-sonnet", ["claude-opus"]) == "claude-opus"

    bundle = load_full_context(
        None,
        project_id="p1",
        spec={
            "title": "Fix KG symbol lookup",
            "instructions": "Fix key mismatch in symbol lookup",
            "kg_context": {"callers": ["engine/kg_view.py#load_symbol"]},
        },
    )

    # Mistake 1: synthetic fixture + unread caller + reused builder tests + out-of-scope permission edit.
    bad_repro = validate_reproduction(_valid_repro(checked_callers=()))
    verdict = validate_quick_change(
        spec={"title": "Fix KG symbol lookup", "instructions": "Fix key mismatch"},
        context_bundle=bundle,
        reproduction=bad_repro,
        diff_files={
            "engine/kg_lookup.py": "+@permit(P.ADMIN)\n+def lookup(): pass\n",
            "engine/tests/test_lookup.py": "+# invented shape\n+data = synthetic_fixture()\n",
        },
        pr_claims=[{"claim": "All callers pass", "evidence": ""}],
        builder_test_paths=["engine/tests/test_lookup.py"],
        independent_test=IndependentTestResult(
            command="pytest engine/tests/test_lookup.py",
            passed=True,
            output="1 passed",
            replayed_failing_run=False,
            test_paths=("engine/tests/test_lookup.py",),
        ),
        builder_model="anthropic/claude-sonnet",
        available_models=["anthropic/claude-sonnet", "openai/gpt-5"],
    )
    assert verdict.approved is False
    assert verdict.validator_model == "openai/gpt-5"
    codes = {f.code for f in verdict.blocking_findings}
    assert {"synthetic_fixture", "unread_caller", "out_of_scope_change", "unverified_pr_claim", "reused_builder_tests"} <= codes


def test_ql3_warm_session_revision_passes_and_opens_pr_with_evidence(pg_conn):
    conn, pid = pg_conn
    spec = {
        "title": "Fix null card slug",
        "instructions": "Return fallback when slug is None",
        "touched_paths": ["app/cards.py"],
        "kg_context": {"callers": ["app/views.py#render_card"]},
        "study_record": {"area": "cards", "summary": "Cards use slug for URLs"},
        "repro": "pytest tests/test_card.py failed",
    }
    started = start_quick_fix(conn, project_id=pid, title=spec["title"], instructions=spec["instructions"], spec_extra=spec)
    sessions_seen: list[str] = []

    def builder(prompt_text: str, meta: dict) -> dict:
        sessions_seen.append(meta["session_id"])
        return {
            "diff_files": {"app/cards.py": "+    return slug or 'Untitled'\n"},
            "reproduction": _valid_repro(checked_callers=()),  # round 1 forgot to check caller!
            "pr_claims": [{"claim": "Guards null slug at app/cards.py:42", "evidence": "app/cards.py:42"}],
            "builder_test_paths": ["tests/test_card.py"],
            "targeted_test_output": "tests/test_card.py: 3 passed",
            "cost_usd_micros": 250_000,
            "elapsed_seconds": 180.0,
        }

    def revise(findings: list[ValidationFinding], round_no: int, session_id: str) -> dict:
        sessions_seen.append(session_id)
        assert any(f.code == "unread_caller" for f in findings)
        return {
            "reproduction": validate_reproduction(_valid_repro(checked_callers=("app/views.py#render_card",))),
        }

    res = execute_quick_job(
        conn,
        project_id=pid,
        run_id=started["run_id"],
        host_id="host-val",
        available_memory_mb=4096,
        builder_session=builder,
        builder_revise=revise,
        independent_tester=lambda state, rnd: _good_independent_test(),
        pr_opener=lambda req: {"url": "https://github.com/org/repo/pull/77", "number": 77},
        builder_model="anthropic/claude-sonnet",
        available_models=["anthropic/claude-sonnet", "openai/gpt-5"],
    )
    assert res["status"] == "pr_opened"
    assert res["pull_request_url"] == "https://github.com/org/repo/pull/77"
    assert res["receipt_id"] is not None
    assert res["decision_id"] is not None  # human review_pull_request merge gate
    # Builder and revision ran in the SAME warm session!
    assert len(sessions_seen) == 2 and sessions_seen[0] == sessions_seen[1]

    saved = get_quick_run(conn, project_id=pid, run_id=started["run_id"])
    assert saved["validation_rounds"] == 2
    body_md = saved["pr_evidence"]["body_markdown"]
    for heading in ("## Reproduction", "## Causes", "## Targeted Test Output", "## Independent Validation", "## What Was Left Out"):
        assert heading in body_md


def test_ql3_unresolved_disagreement_escalates_via_decision_ladder(pg_conn):
    conn, pid = pg_conn
    spec = {
        "title": "Fix parser edge case",
        "instructions": "Fix parser crash",
        "kg_context": {"callers": ["app/caller.py#use_parser"]},
        "repro": "parser crashes on empty string",
    }
    started = start_quick_fix(conn, project_id=pid, title=spec["title"], instructions=spec["instructions"], spec_extra=spec)
    launched_full: list[dict] = []

    res = execute_quick_job(
        conn,
        project_id=pid,
        run_id=started["run_id"],
        host_id="host-ladder",
        available_memory_mb=4096,
        builder_session=lambda prompt_text, meta: {
            "diff_files": {"app/parser.py": "+if not s: return ''\n"},
            "reproduction": _valid_repro(checked_callers=()),  # never fixes unread_caller
            "builder_test_paths": ["tests/test_parser.py"],
        },
        builder_revise=lambda findings, rnd, sid: {},  # refuses to fix -> disagreement!
        independent_tester=lambda state, rnd: _good_independent_test(),
        full_launcher=lambda payload: (launched_full.append(payload), "crun_full_escalated")[1],
    )
    assert res["status"] == "escalated"
    assert res["reason"] == "validation_unresolved"
    assert res["validation"]["decision_ladder"]["tier"] == "person"
    assert res["validation"]["decision_ladder"]["decision_id"]
    assert len(launched_full) == 1
    assert launched_full[0]["reproduction"]["command"] == _valid_repro()["command"]


# ---------------------------------------------------------------------------
# QL-4 (BOSSAPP-798): Caps, lossless hand-off, CI-after-open bounded reopen
# ---------------------------------------------------------------------------


def test_ql4_caps_and_lossless_escalation_and_ci_reopen(pg_conn):
    conn, pid = pg_conn

    # 1. Wall-clock, cost, protected path, diff size, blast radius checks.
    assert check_quick_limits({"kind": "bug", "repro": "x"}, files=1, lines=10, touched_paths=["a.py"], elapsed_seconds=MAX_WALL_CLOCK_SECONDS + 1).reason == "wall_clock_exceeded"
    assert check_quick_limits({"kind": "bug", "repro": "x"}, files=1, lines=10, touched_paths=["a.py"], cost_usd_micros=DEFAULT_COST_CAP_USD_MICROS + 1).reason == "cost_cap_exceeded"
    assert check_quick_limits({"kind": "bug", "repro": "x"}, files=1, lines=10, touched_paths=["engine/identity/auth.py"]).reason == "protected_path_touch"
    assert check_quick_limits({"kind": "bug", "repro": "x"}, files=5, lines=200, touched_paths=["a.py", "b.py", "c.py", "d.py", "e.py"]).reason == "diff_too_large"
    assert check_quick_limits({"kind": "bug", "repro": "x"}, files=1, lines=10, touched_paths=["a.py"], blast_radius_callers=20).reason == "blast_radius_too_wide"

    # 2. End-to-end lossless hand-off when diff exceeds cap.
    spec = {
        "title": "Fix sprawling bug",
        "instructions": "Fix bug across 4 files",
        "study_record": {"area": "core", "summary": "Core study notes preserved"},
        "repro": "pytest failed",
    }
    started = start_quick_fix(conn, project_id=pid, title=spec["title"], instructions=spec["instructions"], spec_extra=spec)
    handoff_received: list[dict] = []
    res = execute_quick_job(
        conn,
        project_id=pid,
        run_id=started["run_id"],
        host_id="host-cap",
        available_memory_mb=4096,
        builder_session=lambda prompt_text, meta: {
            "diff_files": {
                "a.py": "+x\n", "b.py": "+y\n", "c.py": "+z\n", "d.py": "+w\n",
            },
            "reproduction": _valid_repro(),
            "carried_test": {"path": "tests/test_red.py", "command": "pytest tests/test_red.py"},
        },
        full_launcher=lambda payload: (handoff_received.append(payload), "crun_handed_off")[1],
    )
    assert res["status"] == "escalated"
    assert res["reason"] == "diff_too_large"
    assert len(handoff_received) == 1
    pkg = handoff_received[0]
    assert pkg["study_notes"] and pkg["study_notes"][0]["summary"] == "Core study notes preserved"
    assert pkg["reproduction"]["exit_code"] == 1
    assert set(pkg["partial_diff"].keys()) == {"a.py", "b.py", "c.py", "d.py"}
    assert pkg["carried_test"]["path"] == "tests/test_red.py"

    # 3. CI-after-open: bounded reopen (MAX_CI_REOPENS = 2), then escalate on 3rd failure.
    pr_run = start_quick_fix(conn, project_id=pid, title="CI reopen test", instructions="Fix bug", spec_extra={"repro": "boom"})
    opened = execute_quick_job(
        conn,
        project_id=pid,
        run_id=pr_run["run_id"],
        host_id="host-ci",
        available_memory_mb=4096,
        builder_session=lambda prompt_text, meta: {
            "diff_files": {"app/x.py": "+return 1\n"},
            "reproduction": _valid_repro(checked_callers=()),
            "builder_test_paths": ["tests/test_x.py"],
        },
        independent_tester=lambda state, rnd: _good_independent_test(),
        pr_opener=lambda req: {"url": "https://github.com/org/repo/pull/88", "number": 88},
    )
    assert opened["status"] == "pr_opened"

    for expected_round in range(1, MAX_CI_REOPENS + 1):
        r = handle_ci_result(conn, project_id=pid, run_id=pr_run["run_id"], ci_status="failed", ci_logs=f"CI fail #{expected_round}")
        assert r["status"] == "ci_reopened"
        assert r["reopened"] is True
        assert r["ci_reopen_count"] == expected_round
        assert r["receipt_id"]

    # Next failure exceeds cap -> escalates with blocking decision.
    exhausted = handle_ci_result(conn, project_id=pid, run_id=pr_run["run_id"], ci_status="failed", ci_logs="CI fail #3")
    assert exhausted["status"] == "escalated"
    assert exhausted["reopened"] is False
    assert exhausted["decision_id"]


# ---------------------------------------------------------------------------
# QL-5 (BOSSAPP-799): Entry points (Ask BOSS, MCP, boss:quick, Console/API)
# ---------------------------------------------------------------------------


def test_ql5_entry_points_ask_boss_mcp_label_and_worker(pg_conn):
    conn, pid = pg_conn

    # 1. Ask BOSS offer: small evidenced bug -> one-button create_code_task(lane="quick")
    small = suggest_quick_fix_offer(
        title="Fix null slug",
        instructions="Handle null slug in card header",
        project_id=pid,
        repro="TypeError on null slug",
        touched_paths=["app/cards.py"],
    )
    assert small["lane"] == "quick"
    assert small["tool"] == "create_code_task"
    assert small["args"]["lane"] == "quick"

    # Bigger feature ask -> stays on start_workflow!
    big = suggest_quick_fix_offer(
        title="Build multi-tenant billing portal",
        instructions="Create a full SaaS billing portal with invoices and webhooks",
        project_id=pid,
    )
    assert big["lane"] == "full"
    assert big["tool"] == "start_workflow"

    # 2. boss:quick label detection & triage override
    assert lane_from_labels(["bug", QUICK_LANE_LABEL]) == "quick"
    assert lane_from_labels(["bug", "BOSS:QUICK"]) == "quick"
    assert ticket_intake.ticket_lane(["boss:quick"]) == "quick"
    assert lane_from_labels(["enhancement"]) is None
    triaged_label = triage_quick_lane({"title": "Fix bug"}, labels=["boss:quick"])
    assert triaged_label["lane"] == "quick"
    assert triaged_label["forced"] is True

    # 3. MCP create_code_task tool schema exposes lane="quick" and launches a quick job!
    from boss_engine.tools import build_default_registry

    reg = build_default_registry()
    spec_obj = reg.get("create_code_task")
    assert "lane" in spec_obj.parameters["properties"]
    assert "quick" in spec_obj.parameters["properties"]["lane"]["enum"]

    class _Ctx:
        def begin(self):
            class _CM:
                def __enter__(self_inner):
                    return conn

                def __exit__(self_inner, *exc):
                    return False

            return _CM()

    mcp_out = spec_obj.handler(
        _Ctx(),
        project_id=pid,
        actor="mcp_user",
        args={
            "title": "MCP quick fix",
            "instructions": "Fix typo in error message",
            "lane": "quick",
            "repro": "pytest test_err.py failed",
        },
    )
    assert mcp_out["lane"] == "quick"
    assert mcp_out["status"] == "queued"
    assert mcp_out["source"] == "mcp"
    saved_mcp = get_quick_run(conn, project_id=pid, run_id=mcp_out["run_id"])
    assert saved_mcp is not None and saved_mcp["source"] == "mcp"

    # 4. Worker runner advertises quick.fix in its supported kinds.
    import pathlib

    runner_src = (
        pathlib.Path(__file__).resolve().parents[2]
        / "apps"
        / "worker"
        / "boss_worker"
        / "runner.py"
    ).read_text(encoding="utf-8")
    assert f'"{JOB_KIND}"' in runner_src
