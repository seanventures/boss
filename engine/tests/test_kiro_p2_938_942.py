"""Tests for Kiro Crew P2 gaps (BOSSAPP-938..942; parent BOSSAPP-934):
- BOSSAPP-938 (Kiro gap 5): per-agent harness/engine + model pin routed in Fixed and Smart modes
  and validated in Agent Studio.
- BOSSAPP-939 (Kiro gap 6): zero-token recurring script/command jobs (0 model calls) and
  change-only revision monitors (wake agent only on revision change; enabled by default).
- BOSSAPP-940 (Kiro gap 7): lighter prebuilt-by-default install (pull_or_build.sh) + guided first
  minute and time-to-first-useful-answer measurement (guided_first_minute.py).
- BOSSAPP-941 (Kiro gap 8): repeated tool-call sequence detector -> Markdown skill draft into
  lessons/consent review inbox -> person approval publishes skill.
- BOSSAPP-942 (Kiro gap 9): run supervisor reads structured StageRecords (rejects raw transcripts),
  independently re-checks worker claims against real evidence, and catches false claims.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from ._router_fixtures import claude, metered, snap as router_snap
from boss_engine.services import learned_skills, run_supervisor, zero_token_jobs
from boss_engine.services.model_router.router import (
    LegacyRoute,
    RouteRequest,
    Router,
)
from boss_engine.services.run_supervisor_types import StageRecord

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_conn():
    url = os.environ.get("BOSS_DATABASE_URL")
    if not url:
        pytest.skip("BOSS_DATABASE_URL not set")
    engine = create_engine(url, future=True)
    org_id = f"org_{uuid.uuid4().hex[:8]}"
    ws_id = f"ws_{uuid.uuid4().hex[:8]}"
    proj_id = f"proj_{uuid.uuid4().hex[:8]}"
    with engine.connect() as c:
        trans = c.begin()
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:id, 'Kiro P2 Org')"), {"id": org_id})
        c.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, 'Kiro P2 WS')"),
            {"id": ws_id, "o": org_id},
        )
        c.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:id, :w, 'Kiro P2 App', :s)"
            ),
            {"id": proj_id, "w": ws_id, "s": f"kiro2-{proj_id[-6:]}"},
        )
        try:
            yield c, proj_id, org_id, ws_id
        finally:
            trans.rollback()
    engine.dispose()


# ── BOSSAPP-938 (Kiro gap 5): Pin model and harness per agent in Fixed & Smart ─────────────────

@pytest.mark.parametrize("mode", ["fixed", "smart"])
def test_kiro_gap5_pinned_harness_and_model_honoured_in_fixed_and_smart(mode):
    router = Router()
    snap = router_snap(
        mode=mode,
        sources={"claude_code": claude(), "codex": metered("codex")},
    )
    legacy = lambda: LegacyRoute("claude_code", "claude-sonnet-4-6", "default build lane")  # noqa: E731

    # 1. Pin BOTH harness and model -> routes to codex · gpt-5.2-codex in Fixed AND Smart
    req_both = RouteRequest(
        kind="agent",
        step="implement",
        role="software_engineer",
        agent_policy={
            "selection_mode": "pinned",
            "pinned_harness": "codex",
            "pinned_model": "gpt-5.2-codex",
        },
        legacy=legacy,
    )
    dec_both = router.route(req_both, snap)
    assert dec_both.engine == "codex"
    assert dec_both.model == "gpt-5.2-codex"
    assert "pin:agent_harness" in dec_both.rules
    if mode == "fixed":
        assert dec_both.binding is True

    # 2. Pin HARNESS only (router picks tier model on that harness) -> still routes to codex
    req_harness_only = RouteRequest(
        kind="agent",
        step="implement",
        role="qa",
        agent_policy={"selection_mode": "router", "pinned_harness": "codex"},
        legacy=legacy,
    )
    dec_harness = router.route(req_harness_only, snap)
    assert dec_harness.engine == "codex"
    assert "pin:agent_harness" in dec_harness.rules
    assert any("Pinned harness on the agent" in line for line in dec_harness.why)


# ── BOSSAPP-939 (Kiro gap 6): Zero-token recurring jobs and change-only monitors ───────────────

def test_kiro_gap6_zero_token_command_job_and_change_only_monitor(pg_conn):
    c, project_id, _, _ = pg_conn

    # Disallowed shell operators or executables are refused
    with pytest.raises(zero_token_jobs.ZeroTokenJobError):
        zero_token_jobs.create_job(
            c,
            project_id,
            name="Bad shell",
            kind="command",
            command_spec={"command": "echo hi && rm -rf /"},
        )
    with pytest.raises(zero_token_jobs.ZeroTokenJobError):
        zero_token_jobs.create_job(
            c,
            project_id,
            name="Unknown binary",
            kind="command",
            command_spec={"argv": ["rm", "-rf", "/tmp/foo"]},
        )

    # 1. Script/command job created by an agent (on by default) runs with 0 model calls
    cmd_job = zero_token_jobs.create_job(
        c,
        project_id,
        name="Health check script",
        kind="command",
        command_spec={"argv": ["echo", "healthy"]},
        created_by="agent:sre",
    )
    assert cmd_job["enabled"] is True
    res = zero_token_jobs.run_job(
        c,
        project_id,
        cmd_job["id"],
        actor="agent:sre",
        command_runner=lambda argv: (0, "healthy"),
    )
    assert res["status"] == "ok"
    assert res["model_calls"] == 0
    assert res["token_cost_usd_micros"] == 0
    assert res["receipt_id"] is not None

    # 2. Change-only monitor wakes agent ONLY when watched revision changes
    monitor = zero_token_jobs.create_job(
        c,
        project_id,
        name="PR #42 revision monitor",
        kind="change_monitor",
        watch_target={"repo": "acme/shop", "ref": "pr/42"},
        created_by="agent:shepherd",
    )
    wakes: list[tuple[str, str | None]] = []

    def fake_waker(_job, new_rev, old_rev):
        wakes.append((new_rev, old_rev))
        return f"wake:{new_rev}"

    # First check sees rev_1 -> wakes agent
    r1 = zero_token_jobs.run_job(
        c, project_id, monitor["id"], revision_fetcher=lambda _w: "sha_111", agent_waker=fake_waker
    )
    assert r1["changed"] is True and r1["woke_agent"] is True and r1["status"] == "triggered_agent"
    assert len(wakes) == 1

    # Second check sees same rev_1 -> skips at zero tokens, does NOT wake agent
    r2 = zero_token_jobs.run_job(
        c, project_id, monitor["id"], revision_fetcher=lambda _w: "sha_111", agent_waker=fake_waker
    )
    assert r2["changed"] is False and r2["woke_agent"] is False and r2["status"] == "skipped_unchanged"
    assert r2["model_calls"] == 0 and r2["token_cost_usd_micros"] == 0
    assert len(wakes) == 1

    # Third check sees new rev_2 -> wakes agent again
    r3 = zero_token_jobs.run_job(
        c, project_id, monitor["id"], revision_fetcher=lambda _w: "sha_222", agent_waker=fake_waker
    )
    assert r3["changed"] is True and r3["woke_agent"] is True
    assert wakes == [("sha_111", None), ("sha_222", "sha_111")]
    assert len(zero_token_jobs.list_runs(c, project_id, monitor["id"])) == 3


# ── BOSSAPP-940 (Kiro gap 7): Lighter prebuilt install & guided first minute ───────────────────

def test_kiro_gap7_prebuilt_default_and_guided_first_minute_timing():
    pull_script = REPO_ROOT / "scripts" / "install" / "pull_or_build.sh"
    assert pull_script.is_file()

    # Default mode is prebuilt published images
    p_default = subprocess.run(
        ["bash", str(pull_script), "--dry-run", "--", "docker", "compose"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "DRY_RUN mode=prebuilt" in p_default.stdout

    # --from-source stays available as an explicit option
    p_source = subprocess.run(
        ["bash", str(pull_script), "--from-source", "--dry-run", "--", "docker", "compose"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "DRY_RUN mode=source" in p_source.stdout

    # Guided first minute measures time to first useful answer
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "install"))
    try:
        import guided_first_minute as gfm

        ticks = iter([100.0, 112.4])

        def fake_http(url, *, method="GET", payload=None, headers=None, timeout=10.0):
            if url.endswith("/health"):
                return 200, {"status": "ok"}
            if url.endswith("/v1/projects"):
                return 200, {"projects": [{"id": "proj_demo"}]}
            if url.endswith("/v1/assistant/ask"):
                return 200, {"answer": "Your repo is indexed and ready for your first quick fix."}
            raise AssertionError(f"unexpected URL {url}")

        report = gfm.run_guided_first_minute(
            api_base="http://127.0.0.1:8000",
            install_mode="prebuilt",
            install_seconds=18.5,
            requester=fake_http,
            clock=lambda: next(ticks),
        )
        assert report.install_mode == "prebuilt"
        assert report.api_healthy is True
        assert report.time_to_first_answer_seconds == 12.4
        assert report.under_60s is True
        assert report.steps_completed == ["health_verified", "workspace_checked", "first_useful_answer"]
        assert "ready for your first quick fix" in report.first_answer_preview
    finally:
        sys.path.pop(0)


# ── BOSSAPP-941 (Kiro gap 8): Skills learned from repeated work, reviewed like lessons ─────────

def test_kiro_gap8_repeated_sequence_drafts_skill_and_approval_publishes(pg_conn):
    c, project_id, _, _ = pg_conn

    traces = [
        {"run_id": "run_1", "role_key": "sre", "tools": ["read_logs", "kg_callers", "staging_rollback"]},
        {"run_id": "run_2", "role_key": "sre", "tools": ["read_logs", "kg_callers", "staging_rollback"]},
        {"run_id": "run_3", "role_key": "sre", "tools": ["read_logs", "kg_callers", "staging_rollback"]},
        {"run_id": "run_4", "role_key": "qa", "tools": ["run_tests", "read_diff"]},  # only 1 occurrence -> ignored
    ]
    res = learned_skills.draft_learned_skills(c, project_id, traces=traces, min_occurrences=3)
    assert res["detected"] == 1
    assert len(res["drafted"]) == 1
    draft = res["drafted"][0]
    assert draft["occurrences"] == 3
    assert draft["steps"] == ["read_logs", "kg_callers", "staging_rollback"]

    # Draft appears in the consent/lessons review inbox, NOT yet published as an active skill
    pending = learned_skills.list_pending_learned_skills(c, project_id)
    assert any(p["decision_id"] == draft["decision_id"] for p in pending)

    # Re-running detector deduplicates pending draft
    res2 = learned_skills.draft_learned_skills(c, project_id, traces=traces, min_occurrences=3)
    assert res2["drafted"] == []
    assert any(s["reason"] == "already_pending_review" for s in res2["skipped"])

    # Agent cannot self-approve a learned skill
    with pytest.raises(PermissionError, match="Only a person"):
        learned_skills.approve_and_publish_learned_skill(
            c, project_id, draft["decision_id"], actor="agent:sre"
        )

    # Person approves -> skill is published and enabled
    published = learned_skills.approve_and_publish_learned_skill(
        c, project_id, draft["decision_id"], actor="person:sean"
    )
    assert published["skill"]["skill_key"] == draft["skill_key"]
    assert published["skill"]["enabled"] is True
    assert "# Learned Procedure:" in published["skill"]["knowledge"]


# ── BOSSAPP-942 (Kiro gap 9): Supervisor reads structured records & re-checks worker claims ────

def test_kiro_gap9_supervisor_reads_structured_records_and_catches_false_claims():
    # Rejects raw transcripts — requires schema-bounded StageRecords
    with pytest.raises(TypeError, match="StageRecord"):
        run_supervisor.evaluate_stage_records(["raw worker transcript blob"])  # type: ignore[list-item]

    # False claims: worker claims tests passed, files changed, and repro succeeded — evidence disagrees!
    bad_record = StageRecord(
        stage="verify",
        round_no=1,
        status="passed",
        faults={"unit_tests": "WORK"},
        claims={
            "tests_passed": True,
            "files_changed": ["src/checkout.py", "src/ghost.py"],
            "reproduced": True,
            "gates_passed": ["unit_tests"],
        },
        checks={"pytest": 1},
        artifacts=("src/checkout.py",),
        spend_usd_micros=45_000,
        elapsed_seconds=30,
    )
    signal, checks = run_supervisor.evaluate_stage_records(
        [bad_record],
        evidence={"diff_files": ["src/checkout.py"], "repro_exit_code": 0},
    )
    assert signal.tripped is True
    assert signal.reason == "unverified_claim"
    failed_kinds = {ch.claim_kind for ch in checks if not ch.verified}
    assert failed_kinds == {"tests_passed", "files_changed", "reproduced", "gate_passed"}

    digest = run_supervisor.build_evidence_digest(
        run_id="crun_test1",
        project_id="proj_1",
        signal=signal,
        history=[],
        claim_checks=checks,
    )
    assert len(digest["claim_discrepancies"]) == 4

    # Honest record with verified claims does NOT trip
    good_record = StageRecord(
        stage="verify",
        round_no=1,
        status="passed",
        faults={},
        claims={
            "tests_passed": True,
            "files_changed": ["src/checkout.py"],
            "reproduced": True,
            "gates_passed": ["unit_tests"],
        },
        checks={"pytest": 0},
        artifacts=("src/checkout.py",),
    )
    ok_signal, ok_checks = run_supervisor.evaluate_stage_records(
        [good_record],
        evidence={"diff_files": ["src/checkout.py"], "repro_exit_code": 1},
    )
    assert ok_signal.tripped is False
    assert all(ch.verified for ch in ok_checks)
