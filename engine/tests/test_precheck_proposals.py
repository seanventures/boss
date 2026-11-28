"""BOSSAPP-541: Every run failure proposes a pre-check for itself.

Verifies:
  1. Replay of the three 2026-10-14 run-killers (socat missing, disconnected source reported
     connected, caps vs metered billing) each produces a distinct, structured pre-check proposal.
  2. Generic/unclassified failures still classify and propose a pre-check deterministically.
  3. Secrets in raw error text are redacted before storage.
  4. DB round-trip: persistence to `precheck_proposals`, linkage to `boss_recommendations`,
     deduplication on `(project_id, dedupe_key)`, audit events, and explicit human approval
     (never self-modifying `workflow_precheck.CHECKS`).
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import build_failure, precheck_proposals, workflow_precheck
from boss_engine.services.core import make_id


def _test_db_url() -> str:
    return os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


class TestReplay20261014Failures:
    def test_replay_socat_missing_proposes_binary_precheck(self):
        before_checks = set(workflow_precheck.CHECKS.keys())
        prop = precheck_proposals.classify_and_propose(
            run_id="crun_f103065efc52457fb60c",
            engine="claude_agent_sdk",
            error=(
                "Sandbox is required but failed to initialize: "
                "Sandbox dependencies not available: socat not installed. Restart to retry."
            ),
        )
        assert prop.cause_class == "harness.missing_runtime"
        assert prop.proposed_check_id == "sandbox.engine.binary.socat"
        assert prop.existing_check_id == "sandbox.engine"
        assert prop.checkpoint == "pre_run"
        assert prop.probe_spec["binary"] == "socat"
        assert prop.probe_spec["expected_verdict"] == "will_fail"
        assert prop.probe_spec["extends_existing_check"] is True
        assert prop.auto_apply is False
        assert "socat" in prop.title.lower()
        assert set(workflow_precheck.CHECKS.keys()) == before_checks

    def test_replay_disconnected_source_reported_connected_proposes_live_state_precheck(self):
        prop = precheck_proposals.classify_and_propose(
            run_id="crun_20261014_disconnected",
            engine="claude_code",
            source_id="anthropic_claude_code",
            error="Not logged in · Please run /login (401 invalid bearer token) — source reported connected",
            context={"source_reported_connected": True, "source_usable": False},
        )
        assert prop.cause_class == "model.source_disconnected"
        assert prop.proposed_check_id == "sandbox.model.live_source_state"
        assert prop.existing_check_id == "sandbox.model"
        assert prop.checkpoint == "pre_run"
        assert prop.probe_spec["expected_verdict"] == "will_fail"
        assert prop.probe_spec["extends_existing_check"] is True
        assert prop.auto_apply is False

    def test_replay_caps_vs_metered_billing_proposes_metered_fit_precheck(self):
        prop = precheck_proposals.classify_and_propose(
            run_id="crun_20261014_caps_metered",
            engine="claude_agent_sdk",
            source_id="anthropic_api",
            error=(
                "Budget cap exceeded: this needs $12.00 reserved for metered billing, "
                "more than today's whole budget of $10.00. It resets at 00:00 UTC, "
                "or raise the daily cap in Govern › Limits."
            ),
            context={"metered_billing": True, "cap_usd": 10.0, "estimated_cost_usd": 12.0},
        )
        assert prop.cause_class == "budget.cap_vs_metered_billing"
        assert prop.proposed_check_id == "budget.run_cost.metered_fit"
        assert prop.existing_check_id == "budget.run_cost"
        assert prop.checkpoint == "pre_run"
        assert prop.probe_spec["expected_verdict"] == "will_fail"
        assert prop.probe_spec["extends_existing_check"] is True
        assert prop.auto_apply is False

    def test_all_three_2026_10_14_replays_have_distinct_dedupe_keys(self):
        p1 = precheck_proposals.classify_and_propose(
            error="Sandbox dependencies not available: socat not installed",
            engine="claude_agent_sdk",
        )
        p2 = precheck_proposals.classify_and_propose(
            error="OAuth token has expired on disconnected source reported connected",
            source_id="anthropic_claude_code",
        )
        p3 = precheck_proposals.classify_and_propose(
            error="budget reservation denied: daily cap cannot fit metered billing",
            source_id="anthropic_api",
        )
        assert len({p1.dedupe_key, p2.dedupe_key, p3.dedupe_key}) == 3
        assert len({p1.cause_class, p2.cause_class, p3.cause_class}) == 3

    def test_unclassified_failure_still_yields_proposal_and_redacts_secrets(self):
        prop = precheck_proposals.classify_and_propose(
            run_id="crun_unknown_1",
            error="Unexpected widget crash with token sk-ant-api03-SECRETVALUE1234567890abcdef",
            failure_class="infra",
            reason_code="widget_boom",
        )
        assert prop.cause_class == "run.infra.widget_boom"
        assert prop.proposed_check_id == "run.failure_guard.widget_boom"
        assert prop.auto_apply is False
        assert all("SECRETVALUE1234567890" not in line for line in prop.evidence)

    def test_build_failure_helper_returns_card_for_failure_only(self):
        card = build_failure.precheck_proposal_for_failure({
            "id": "crun_1",
            "source": "code_task",
            "error": "Sandbox dependencies not available: socat not installed",
        })
        assert card is not None
        assert card["kind"] == "precheck_proposal"
        assert card["cause_class"] == "harness.missing_runtime"
        assert card["proposed_check_id"] == "sandbox.engine.binary.socat"
        assert card["auto_apply"] is False

        assert build_failure.precheck_proposal_for_failure({"id": "crun_2", "waits_on": {"label": "Merge"}}) is None


class TestPrecheckProposalsDB:
    @pytest.fixture()
    def db_project(self, tmp_path):
        url = _test_db_url()
        if not url:
            pytest.skip("BOSS_TEST_DATABASE_URL / DATABASE_URL not set")
        eng = create_engine(url, future=True)
        org_id = make_id("org")
        ws_id = make_id("ws")
        proj_id = make_id("prj")
        with eng.begin() as c:
            c.execute(text("INSERT INTO orgs (id, name) VALUES (:id, :n)"), {"id": org_id, "n": "t"})
            c.execute(
                text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, :n)"),
                {"id": ws_id, "o": org_id, "n": "t"},
            )
            c.execute(
                text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:id, :w, :n, :s)"),
                {"id": proj_id, "w": ws_id, "n": "precheck-test", "s": f"precheck-{proj_id[-6:]}"},
            )
        settings = SimpleNamespace(artifacts_dir=str(tmp_path))
        yield eng, proj_id, settings
        with eng.begin() as c:
            c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": proj_id})
            c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": proj_id})
            c.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws_id})
            c.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org_id})
        eng.dispose()

    def test_record_dedupe_issue_link_and_human_review(self, db_project):
        eng, proj_id, settings = db_project
        checks_before = set(workflow_precheck.CHECKS.keys())

        with eng.begin() as c:
            rec1 = precheck_proposals.record_failure_proposal(
                c,
                settings,
                project_id=proj_id,
                run_id="crun_fail_1",
                engine="claude_agent_sdk",
                error="Sandbox dependencies not available: socat not installed",
            )
            assert rec1["id"].startswith("pcp_")
            assert rec1["created"] is True
            assert rec1["occurrences"] == 1
            assert rec1["status"] == "proposed"
            assert rec1["recommendation_id"] and rec1["recommendation_id"].startswith("brec_")

            # Second failure of the same class deduplicates and links both runs.
            rec2 = precheck_proposals.record_failure_proposal(
                c,
                settings,
                project_id=proj_id,
                run_id="crun_fail_2",
                engine="claude_agent_sdk",
                error="Sandbox dependencies not available: socat not installed",
            )
            assert rec2["id"] == rec1["id"]
            assert rec2["created"] is False
            assert rec2["occurrences"] == 2
            assert rec2["recommendation_id"] == rec1["recommendation_id"]

            items = precheck_proposals.list_proposals(c, proj_id)
            assert len(items) == 1
            assert items[0]["run_ids"] == ["crun_fail_1", "crun_fail_2"]
            assert items[0]["recommendation_id"] == rec1["recommendation_id"]
            assert items[0]["auto_apply"] is False

            # Linked boss_recommendations row exists.
            brec = c.execute(
                text("SELECT id, signature, occurrences FROM boss_recommendations WHERE id = :i"),
                {"i": rec1["recommendation_id"]},
            ).mappings().first()
            assert brec is not None
            assert brec["occurrences"] == 2

            # System/agent actor cannot self-approve.
            with pytest.raises(ValueError, match="human actor"):
                precheck_proposals.review_proposal(
                    c,
                    project_id=proj_id,
                    proposal_id=rec1["id"],
                    status="approved",
                    actor="boss",
                )

            # Human actor approves; audit event emitted; workflow_precheck.CHECKS untouched.
            approved = precheck_proposals.review_proposal(
                c,
                project_id=proj_id,
                proposal_id=rec1["id"],
                status="approved",
                actor="sean@company.com",
                note="Open PR to add socat check",
            )
            assert approved["status"] == "approved"
            assert approved["reviewed_by"] == "sean@company.com"
            assert approved["review_note"] == "Open PR to add socat check"

            events = c.execute(
                text(
                    "SELECT event_type FROM events "
                    "WHERE project_id = :p AND entity_type = 'precheck_proposal' "
                    "ORDER BY created_at"
                ),
                {"p": proj_id},
            ).scalars().all()
            assert precheck_proposals.EVENT_RECORDED in events
            assert precheck_proposals.EVENT_REVIEWED in events

        assert set(workflow_precheck.CHECKS.keys()) == checks_before
