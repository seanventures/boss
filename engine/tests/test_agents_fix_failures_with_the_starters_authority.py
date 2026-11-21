"""AGENTS FIX FAILURES THEMSELVES — the laptop's own cards, replayed (operator, 2026-11-21).

"the main issue was Agents were not acting autonomously to fix the issue ... let's grant by
default for all logged in users to fix these failures, so Agents can automatically get the power
of the user ... if it's blocked worst case it should provide the guidance for humans".

Three cards a person was left holding on northgate-gateway, rebuilt row for row and swept once
by the owner loop (failure_owner) with nothing stubbed but the build/merge machinery:

  1. dec_40f3c93578a845cebd27 "Workflow run · API onboarding: 1 of 12 contract case(s) failed:
     payments_rate_limited_11th. Check the BOSS host is healthy (Infra › Doctor) ..." — the case
     sends the same unauthenticated request as ten that pass with 401 (auth runs before the
     limiter), so it can never answer 429. The agent DROPS it with the reason recorded and runs
     the step again; the host card is withdrawn.
  2. dec_c22f866208d046649986 "Reconcile connection proof setup:connection/claude_code" — Claude
     Code said "You've hit your weekly limit"; it served nothing. The agent settles the hold at $0.
  3. dec_5956127a3df14163810d — approving the merge of change #9 (a LOCAL repository) failed on
     a conflict and went back to the person ("GitHub would not merge it"). The conflict goes to
     the build agent (keep both sides, test, re-ask), and nothing says "GitHub".
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import contract_remedy, failure_owner as fo, merge_failures

from ._ambient_support import eng, make_project, purge  # noqa: F401

_TABLES = ("failure_owners", "boss_recommendations", "deadletters", "jobs", "code_task_runs",
           "workflow_step_runs", "workflow_runs", "workflow_versions", "workflow_definitions",
           "events", "decisions", "local_change_sets", "project_settings")


@pytest.fixture()
def project(eng):  # noqa: F811
    pid = make_project(eng)
    yield pid
    with eng.begin() as c:
        for table in _TABLES:
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table this install lacks
                pass
    purge(eng, pid)


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


# ── 1. the contract case that can never pass ─────────────────────────────────────────────────

BASE = "http://2.29.2.202:8080"
SPEC_CASES = ([{"name": "healthz", "path": "/healthz", "method": "GET", "expect_status": 200}]
              + [{"name": f"payments_unauth_{n}", "path": "/v1/payments", "method": "GET",
                  "expect_status": 401} for n in range(1, 11)]
              + [{"name": "payments_rate_limited_11th", "path": "/v1/payments", "method": "GET",
                  "expect_status": 429}])


def _contract_output():
    cases = [{"name": "healthz", "ok": True, "url": f"{BASE}/healthz", "method": "GET",
              "status": 200, "failures": []}]
    cases += [{"name": f"payments_unauth_{n}", "ok": True, "url": f"{BASE}/v1/payments",
               "method": "GET", "status": 401, "failures": []} for n in range(1, 11)]
    cases += [{"name": "payments_rate_limited_11th", "ok": False, "url": f"{BASE}/v1/payments",
               "method": "GET", "status": 401, "failures": ["answered 401, expected 429"]}]
    return {"ok": True, "tool": "contract_test", "capability": "contract_test", "status": "failed",
            "verdict": "failed", "total": 12, "passed": 11, "failed": 1, "cases": cases,
            "failed_cases": ["payments_rate_limited_11th"],
            "error": "1 of 12 contract case(s) failed: payments_rate_limited_11th"}


def _northgate_contract_run(c, pid):
    wd, wv, wr = (f"wdef_{uuid4().hex[:12]}", f"wver_{uuid4().hex[:12]}", f"wfr_{uuid4().hex[:20]}")
    spec = {"entry_step": "contract_test", "steps": [{
        "id": "contract_test", "kind": "capability", "next": [], "captain": "integration_validator",
        "binding": {"capability": "contract_test", "inputs": {"cases": SPEC_CASES, "base_url": BASE}},
        "approval": {"mode": "auto", "risk_tier": 0}, "max_attempts": 3}]}
    err = "1 of 12 contract case(s) failed: payments_rate_limited_11th"
    c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by, "
                   "namespace) VALUES (:i, :p, :k, 'API onboarding', 'test', 'product')"),
              {"i": wd, "p": pid, "k": wd})
    c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
                   "spec, content_hash, created_by) VALUES (:i, :p, :d, 1, CAST(:s AS JSONB), :h, "
                   "'test')"), {"i": wv, "p": pid, "d": wd, "s": json.dumps(spec), "h": wv})
    c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                   "workflow_version_id, actor, status, error, input, updated_at) VALUES (:i, :p, "
                   ":d, :v, 'test', 'failed', :e, CAST(:inp AS JSONB), :at)"),
              {"i": wr, "p": pid, "d": wd, "v": wv, "e": err, "at": _ago(minutes=20),
               "inp": json.dumps({"title": "API onboarding"})})
    c.execute(text("INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
                   "step_kind, status, attempt, last_error, output) VALUES (:i, :p, :r, "
                   "'contract_test', 'capability', 'deadlettered', 3, :e, CAST(:o AS JSONB))"),
              {"i": f"wsr_{uuid4().hex[:12]}", "p": pid, "r": wr, "e": err,
               "o": json.dumps(_contract_output())})
    return wr, err


def _the_host_card(c, pid, wr, err):
    """The card the laptop showed (dec_40f3c93578a845cebd27) and its owner row, as they were."""
    from boss_engine.services.core import create_blocking_decision

    item = fo.Item("workflow_run", wr, wr, pid, "API onboarding", err, _ago(minutes=20), {})
    why = ("BOSS tried 2 times and it failed the same way each time on BOSS's own side; BOSS "
           "filed a fix for itself.")
    card = create_blocking_decision(
        c, pid, None, f"Workflow run · API onboarding: {err}. Check the BOSS host is healthy "
        "(Infra › Doctor), then press Done — retry.",
        {"action": fo.CARD_ACTION, "surface": "workflow_run", "items": [
            {"id": wr, "key": wr, "title": "API onboarding", "surface": "workflow_run"}],
         "cause_code": "platform:spent", "class": "platform", "cause": err, "why": why},
        {}, fo.CARD_REASON, default_action="hold")
    fo._write(c, item, state=fo.NEEDS_PERSON, cls="platform", cause_code="platform:spent",
              cause=err, words="Needs you: Check the BOSS host is healthy", reason=why,
              action="Check the BOSS host is healthy (Infra › Doctor), then press Done — retry.",
              decision_id=card, attempts=2)
    return card


def test_the_unpassable_contract_case_is_dropped_with_its_reason_not_blamed_on_the_host(
        eng, project):  # noqa: F811
    with eng.begin() as c:
        wr, err = _northgate_contract_run(c, project)
        card = _the_host_card(c, project, wr, err)
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        status = c.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": card}).scalar()
        step = c.execute(text("SELECT status, output FROM workflow_step_runs WHERE "
                              "workflow_run_id = :r"), {"r": wr}).mappings().one()
        row = c.execute(text("SELECT * FROM failure_owners WHERE surface = 'workflow_run' AND "
                             "item_key = :k"), {"k": wr}).mappings().one()
        amended = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND "
                                 "event_type = :e"), {"p": project, "e": contract_remedy.EVENT}
                            ).scalar()
        cards = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND status = "
                               "'pending'"), {"p": project}).scalar()
    assert status == "withdrawn", "the 'check the host' card is gone"
    assert cards == 0, "no new card: an agent owns it"
    amendment = step["output"][contract_remedy.AMENDMENT_KEY]
    assert [d["name"] for d in amendment["drop"]] == ["payments_rate_limited_11th"]
    assert "same GET" in amendment["drop"][0]["reason"] and "auth" in amendment["drop"][0]["reason"]
    assert step["status"] == "ready", "the step runs again"
    assert row["state"] == "fixing" and row["attempts"] == 3 and row["class"] == "app"
    assert "dropped the case it can never pass" in row["words"] and amended == 1
    # The next attempt runs the eleven cases that can pass; the workflow's definition is unchanged.
    kept = contract_remedy.apply({"cases": SPEC_CASES}, amendment)["cases"]
    assert len(kept) == 11 and all(k["name"] != "payments_rate_limited_11th" for k in kept)
    assert fo.unowned(eng, project_id=project) == []


def test_a_contract_the_app_really_breaks_gets_a_repair_build_never_a_dropped_case():
    out = _contract_output()
    out["cases"][3] = {**out["cases"][3], "ok": False, "status": 200,
                       "failures": ["answered 200, expected 401"]}
    plan = contract_remedy.diagnose(out, SPEC_CASES)
    assert [f["name"] for f in plan["fix_app"]] == ["payments_unauth_3"]
    assert "payments_unauth_3: expected 401, got 200" in contract_remedy.repair_note(plan["fix_app"])
    every = {"cases": [{**c, "ok": False} for c in _contract_output()["cases"]]}
    assert contract_remedy.diagnose(every, SPEC_CASES)["drop"] == [], "a step is never emptied"


# ── 2. the held proof the provider never served ──────────────────────────────────────────────

def test_a_held_proof_the_provider_refused_is_settled_by_the_agent_at_zero(
        eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import connection_spend
    from boss_engine.services.core import create_blocking_decision

    receipt = f"rcpt_{uuid4().hex[:20]}"
    with eng.begin() as c:
        card = create_blocking_decision(
            c, project, None, "Reconcile connection proof setup:connection/claude_code",
            {"action": "connection_reconciliation", "reason": "connection_reconciliation"},
            {"attempt": 1, "receipt_id": receipt,
             "message": "RuntimeError: Connection provider outcome is unknown."},
            "connection_reconciliation", default_action="hold")
    held = {"receipt_id": receipt, "status": "in_progress", "created_at": _ago(hours=1),
            "invocation_id": "inv_1", "held_usd_micros": 50_000, "source_id": "src",
            "supported_models": ["claude_code"], "result": {
                "connection_provider": "claude_code", "connection_outcome": {
                    "kind": "unknown", "model": "claude_code", "tokens": {}, "http_status": None,
                    "provider_error": "You've hit your weekly limit · resets 6pm (UTC)",
                    "summary": "No reply from Claude Code. BOSS cannot tell whether the call "
                               "was billed, so its hold remains."}}}
    merged, released = [], []
    monkeypatch.setattr(connection_spend, "_hold_rows", lambda *a, **k: [held])
    from boss_engine.services.receipts import ReceiptService

    monkeypatch.setattr(ReceiptService, "merge_result",
                        lambda self, c, rid, result: merged.append(result))

    def release(c, settings, pid, rid, *, actor, reason):
        released.append((rid, actor, reason))
        return {"status": "failed"}

    monkeypatch.setattr(connection_spend, "release_hold", release)
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        status = c.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": card}).scalar()
    assert status == "resolved"
    assert merged and merged[-1]["connection_outcome"]["kind"] == "refused"
    rid, actor, reason = released[-1]
    assert rid == receipt and actor == fo.ACTOR and "$0" in reason and "weekly limit" in reason
    fo.sweep(eng, _settings(), project_id=project)            # the next tick: it is fine now
    with eng.begin() as c:
        state = c.execute(text("SELECT state FROM failure_owners WHERE surface = 'connection_hold' "
                               "AND project_id = :p"), {"p": project}).scalar()
    assert state == "resolved"


# ── 3. the merge conflict on a LOCAL change ──────────────────────────────────────────────────

def _local_change_9(c, pid):
    url = f"http://boss-local/local_{pid}/{pid}/pull/9"
    runs = []
    for n in range(3):
        rid = f"crun_{uuid4().hex[:20]}"
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, pull_request_url, "
                       "created_at, updated_at) VALUES (:i, :p, 'Validate tenant config', "
                       "'completed', :u, :at, :at)"),
                  {"i": rid, "p": pid, "u": url, "at": _ago(minutes=30 - n)})
        runs.append(rid)
    from boss_engine.services.core import create_blocking_decision

    message = (f"boss/code/{runs[0]} does not merge cleanly into main: cmd/gwlint/"
               "cli_edgecases_test.go, config/tenants/lumen-pay.yaml conflict. | the branch "
               "could not be put back on its base")
    reason = merge_failures.reason_words("not_mergeable", message, 9, url)
    card = create_blocking_decision(
        c, pid, None, "Merge change #9?",
        {"action": "review_pull_request", "reason": "merge_decision_required", "run_id": runs[-1],
         "pull_request_url": url, "branch_name": f"boss/code/{runs[0]}",
         "merge_failed": {"kind": "not_mergeable", "reason": reason,
                          "next": "Retry the build to update its branch from the base branch, "
                                  "then merge again."}},
        {}, "merge_decision_required", default_action="hold")
    jid, dl = f"job_{uuid4().hex[:20]}", f"dlq_{uuid4().hex[:20]}"
    c.execute(text(
        "INSERT INTO jobs (id, project_id, kind, status, payload, last_error, on_behalf_of, "
        "on_behalf_system, created_at, updated_at) VALUES (:i, :p, 'github.pull_request.merge', "
        "'deadlettered', CAST(:pl AS JSONB), CAST(:e AS JSONB), 'usr_starter', false, :at, :at)"),
        {"i": jid, "p": pid, "at": _ago(minutes=5), "e": json.dumps({"message": message}),
         "pl": json.dumps({"decision_id": card, "pull_request_url": url, "pull_request_number": 9,
                           "branch_name": f"boss/code/{runs[0]}"})})
    c.execute(text("INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, "
                   "created_at) VALUES (:d, :p, 'job', :j, :r, :at)"),
              {"d": dl, "p": pid, "j": jid, "r": message[:200], "at": _ago(minutes=5)})
    return url, runs, card, reason


def test_a_merge_conflict_on_a_local_change_goes_to_the_build_agent_not_the_person(
        eng, project, monkeypatch):  # noqa: F811
    handed = []

    def builder(connection, settings, run_id, note):
        handed.append((run_id, note))
        return "crun_the_fix"

    monkeypatch.setattr(merge_failures, "hand_to_builder", builder)
    with eng.begin() as c:
        url, runs, card, reason = _local_change_9(c, project)
    assert "GitHub" not in reason and "Change #9" in reason, "a local change is not GitHub's"
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        status, rec = c.execute(text("SELECT status, recommendation FROM decisions WHERE id = :d"),
                                {"d": card}).one()
        row = c.execute(text("SELECT state, words FROM failure_owners WHERE surface = 'job' AND "
                             "project_id = :p"), {"p": project}).mappings().one()
        cards = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                               "status = 'pending'"), {"p": project}).scalar()
    assert handed and handed[0][0] == runs[-1], "the newest build of the change is handed over"
    note = handed[0][1]
    assert "keeping BOTH" in note and "cli_edgecases_test.go" in note and "GitHub" not in note
    assert status == "withdrawn" and cards == 0
    assert row["state"] == "fixing" and "build agent" in row["words"]


def test_a_local_change_set_s_builds_are_found_by_its_branch_and_run(eng, project):  # noqa: F811
    with eng.begin() as c:
        rid = f"crun_{uuid4().hex[:20]}"
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) VALUES "
                       "(:i, :p, 'x', 'completed')"), {"i": rid, "p": project})
        c.execute(text("INSERT INTO local_change_sets (id, project_id, repository, number, branch, "
                       "base_branch, run_id) VALUES (:i, :p, :r, 12, :b, 'main', :run)"),
                  {"i": f"lcs_{uuid4().hex[:12]}", "p": project, "r": f"local_{project}/{project}",
                   "b": "feature/x", "run": rid})
        found = merge_failures._pr_runs(c, project, f"http://boss-local/local_{project}/x/pull/12",
                                        None, "feature/x")
        by_branch = merge_failures._pr_runs(c, project, "", None, f"boss/code/{rid}")
    assert found == [rid] and by_branch == [rid]


def test_with_the_org_default_off_the_conflict_stays_on_its_card_with_exact_steps(
        eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import project_settings

    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda *a, **k: pytest.fail("the default is off: no hand-off"))
    with eng.begin() as c:
        project_settings.set_one(c, project, "agents_fix_failures", 0)
        _url, _runs, card, _reason = _local_change_9(c, project)
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        status = c.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": card}).scalar()
    assert status == "pending"
    assert "keeping both" in merge_failures.conflict_steps(["crun_x"]).lower()
