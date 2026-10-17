"""The agent release gate's own logic (scripts/e2e_gate_agent.py): what fails a watched build,
what the final checks demand, and that the committed briefs carry no repository or credential.
No Docker, no network, no model."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("e2e_gate_agent", ROOT / "scripts" / "e2e_gate_agent.py")
gate = importlib.util.module_from_spec(_spec)
sys.modules["e2e_gate_agent"] = gate          # dataclasses look their module up
_spec.loader.exec_module(gate)  # type: ignore[union-attr]


def ev(kind: str, **data):
    return {"event_type": kind, "data": data, "created_at": "t"}


GOOD = [ev("code_task.started"), ev("code_task.test_budget", written=10, budget=20, over=False),
        ev("code_task.app_serve_checked", ok=True), ev("code_task.pr_opened"), ev("code_task.build_profile", phase="done")]
DONE = {"status": "completed", "pull_request_url": "https://github.com/o/r/pull/1"}


def failing(checks):
    return [name for name, ok, _ in checks if not ok]


def test_a_clean_completed_build_passes_every_check():
    assert failing(gate.final_checks(DONE, GOOD)) == []


def test_each_final_check_fails_on_its_own_evidence():
    assert failing(gate.final_checks({**DONE, "pull_request_url": None}, GOOD)) == ["completed with a PR"]
    assert failing(gate.final_checks(DONE, GOOD + [ev("code_task.gate_advisory", advisory_failing_gates=["consistent"])])) \
        == ["no advisory-failing gates"]
    assert failing(gate.final_checks(DONE, GOOD + [ev("code_task.app_serve_checked", ok=False, error="exit 1")])) \
        == ["the app started"]
    assert failing(gate.final_checks(DONE, [e for e in GOOD if e["event_type"] != "code_task.build_profile"])) \
        == ["build profile recorded"]
    assert failing(gate.final_checks(DONE, GOOD + [ev("code_task.test_budget", over=True)])) == ["test budget not over"]
    # A PR that opened with its checks failing (checks_failing.py) is not a passing build.
    assert failing(gate.final_checks(DONE, GOOD + [ev("code_task.pr_opened_with_checks_failing",
                                                      failing_gates=["security_clean"])])) \
        == ["no checks failing on the PR"]
    assert failing(gate.final_checks(DONE, GOOD + [ev("code_task.shipped_with_dissent",
                                                      unresolved_gates=["code_review_passed"])])) \
        == ["no checks failing on the PR"]


def test_an_empty_advisory_event_is_not_a_failing_gate():
    assert failing(gate.final_checks(DONE, GOOD + [ev("code_task.gate_advisory", advisory_failing_gates=[])])) == []


def test_an_install_failure_counts_only_when_nothing_later_fixed_it():
    broken = GOOD + [ev("code_task.install_failed", summary="ERESOLVE")]
    assert failing(gate.final_checks(DONE, broken)) == ["the install works"]
    fixed = broken + [ev("code_task.install_fixed", rounds=1)]
    assert failing(gate.final_checks(DONE, fixed)) == []
    started_clean = broken + [ev("code_task.app_serve_checked", ok=True)]
    assert failing(gate.final_checks(DONE, started_clean)) == []


def test_no_test_budget_event_is_not_over():
    assert failing(gate.final_checks(DONE, [e for e in GOOD if e["event_type"] != "code_task.test_budget"])) == []


@pytest.mark.parametrize("status", ["blocked", "failed", "cancelled"])
def test_the_watch_stops_at_once_on_a_dead_build(status):
    why = gate.watch_verdict(run={"status": status, "phase": "implement", "error": "boom"}, now=100, started=0,
                             last_turn=90, budget_min=25, pending=[])
    assert why and status in why and "boom" in why


def test_the_watch_stops_on_a_pending_decision_idle_agents_and_the_budget():
    run = {"status": "running", "phase": "implement"}
    assert "dec_1" in gate.watch_verdict(run=run, now=60, started=0, last_turn=50, budget_min=25,
                                         pending=[{"id": "dec_1", "question": "Approve?"}])
    assert "no agent activity" in gate.watch_verdict(run=run, now=9 * 60, started=0, last_turn=30, budget_min=25, pending=[])
    assert "over the time budget" in gate.watch_verdict(run=run, now=26 * 60, started=0, last_turn=26 * 60 - 5,
                                                        budget_min=25, pending=[])
    assert gate.watch_verdict(run=run, now=10 * 60, started=0, last_turn=10 * 60 - 30, budget_min=25, pending=[]) is None
    assert "awaiting_gate_override" in gate.watch_verdict(run={"status": "running", "phase": "awaiting_gate_override"},
                                                          now=60, started=0, last_turn=50, budget_min=25, pending=[])


def test_a_build_that_has_not_started_yet_is_idle_from_the_start():
    assert "no agent activity" in gate.watch_verdict(run=None, now=8 * 60 + 1, started=0, last_turn=None,
                                                     budget_min=25, pending=[])


def test_the_briefs_name_no_repository_owner_or_credential():
    for path in sorted((ROOT / "tests" / "e2e" / "briefs").glob("*.json")):
        raw = path.read_text()
        doc = json.loads(raw)
        assert not {"owner", "repo", "base_branch", "token"} & set(doc), path.name
        for needle in ("ghp_", "github_pat_", "sk-", "paramg"):
            assert needle not in raw, (path.name, needle)


def test_the_plain_brief_starts_a_code_task_and_the_connector_variant_applies_login_oauth():
    plain = gate.load_brief("onboarding-path-builder")
    body = gate.code_task_body(plain, base_branch="b", stamp="s")
    assert body["instructions"].startswith("A buildable FULL-STACK scaffold") and "THE APP TO BUILD:\n" in body["instructions"]
    assert body["open_pr"] is True and body["test_commands"] == ["npm run verify"] and body["base_branch"] == "b"
    variant = gate.load_brief("onboarding-path-builder-login-oauth")
    assert variant["start"] == "template_apply" and variant["connector"]["key"] == "login-oauth"
    assert variant["budget_min"] == 30 and plain["budget_min"] == 25
    applied = gate.apply_body(variant, egress=["github.com"], stamp="s")
    assert applied["note"].endswith(plain["app"]) and len(applied["note"]) <= 20000
    assert applied["accept_egress"] == ["github.com"] and applied["new_app"]["name"]


def test_the_gate_signs_in_only_to_localhost():
    gate._localhost_only("http://localhost:8000")
    with pytest.raises(gate.GateFail):
        gate._localhost_only("https://boss.example.com")
