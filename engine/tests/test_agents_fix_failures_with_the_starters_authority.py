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

BASE = "http://203.0.113.10:8080"
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

    def builder(connection, settings, run_id, note, **_k):
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


def test_in_a_multi_repo_project_a_number_never_picks_another_repository_s_build(
        eng, project):  # noqa: F811
    """Review of #264: repo A's change #9 must never go to repo B's #9 build. Only a CERTAIN
    match (exact URL, the run the question names in this repository, this repository's local
    change set, a boss/code/<run> branch in this repository) hands it over."""
    repo_a, repo_b = f"local_{project}/alpha", f"local_{project}/beta"
    with eng.begin() as c:
        runs = {}
        for repo in (repo_a, repo_b):
            rid = f"crun_{uuid4().hex[:20]}"
            owner, name = repo.split("/")
            c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec) VALUES "
                           "(:i, :p, 'x', 'completed', CAST(:s AS JSONB))"),
                      {"i": rid, "p": project, "s": json.dumps({"owner": owner, "repo": name})})
            c.execute(text("INSERT INTO local_change_sets (id, project_id, repository, number, "
                           "branch, base_branch, run_id) VALUES (:i, :p, :r, 9, :b, 'main', :run)"),
                      {"i": f"lcs_{uuid4().hex[:12]}", "p": project, "r": repo,
                       "b": f"boss/code/{rid}", "run": rid})
            runs[repo] = rid
        url_a = f"http://boss-local/{repo_a}/pull/9"
        assert merge_failures._pr_runs(c, project, url_a) == [runs[repo_a]]
        assert merge_failures._pr_runs(c, project, url_a, None,
                                       f"boss/code/{runs[repo_b]}") == [runs[repo_a]]
        # Repo A's number with no change set of its own, and only repo B's evidence: nothing.
        c.execute(text("DELETE FROM local_change_sets WHERE repository = :r"), {"r": repo_a})
        assert merge_failures._pr_runs(c, project, url_a, None,
                                       f"boss/code/{runs[repo_b]}") == []


def test_with_no_certain_build_the_conflict_card_stays_with_its_steps(
        eng, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda *a, **k: pytest.fail("no certain build: nothing is handed over"))
    with eng.begin() as c:
        _url, _runs, card, _reason = _local_change_9(c, project)
        c.execute(text("UPDATE code_task_runs SET pull_request_url = NULL WHERE project_id = :p"),
                  {"p": project})
        c.execute(text("UPDATE decisions SET recommendation = recommendation - 'run_id' - "
                       "'branch_name' WHERE id = :d"), {"d": card})
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        status = c.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": card}).scalar()
    assert status == "pending", "never withdrawn without a certain hand-off"


def test_a_dead_merge_job_is_never_rerun_for_its_person(eng, project):  # noqa: F811
    """Review of #264 (MUST): a change_set.merge (github:merge, IRREVERSIBLE) that died is never
    re-queued by an agent "for its person"."""
    from boss_engine.services.deadletters import (DeadletterRemediationBlocked,
                                                  DeadletterRemediationService)

    jid, dl = f"job_{uuid4().hex[:20]}", f"dlq_{uuid4().hex[:20]}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, payload, last_error, on_behalf_of, "
            "on_behalf_system, created_at, updated_at) VALUES (:i, :p, 'change_set.merge', "
            "'deadlettered', '{}'::jsonb, CAST(:e AS JSONB), 'usr_starter', false, :at, :at)"),
            {"i": jid, "p": project, "at": _ago(minutes=5),
             "e": json.dumps({"message": "HTTP 502 from the merge"})})
        c.execute(text("INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, "
                       "created_at) VALUES (:d, :p, 'job', :j, 'HTTP 502', :at)"),
                  {"d": dl, "p": project, "j": jid, "at": _ago(minutes=5)})
    with eng.begin() as c, pytest.raises(DeadletterRemediationBlocked):
        DeadletterRemediationService(_settings()).requeue_job_deadletter(
            c, deadletter_id=dl, actor=fo.ACTOR, as_boss=True, for_its_person=True)
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        status = c.execute(text("SELECT status FROM deadletters WHERE id = :d"), {"d": dl}).scalar()
        row = c.execute(text("SELECT state, history FROM failure_owners WHERE surface = 'job' "
                             "AND project_id = :p"), {"p": project}).mappings().one()
    assert status == "open" and row["state"] == "needs_person"
    assert row["history"][-1]["exit"] == "job_never_rerun_by_an_agent"


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


# ── review of #264: what is NOT dropped, said on the verdict, and not settled at $0 ─────────

def test_a_duplicate_post_answering_409_is_the_app_s_to_fix_never_dropped():
    spec = [{"name": "create_1", "method": "POST", "path": "/v1/tenants", "json": {"id": "a"},
             "expect_status": 201},
            {"name": "create_2", "method": "POST", "path": "/v1/tenants", "json": {"id": "a"},
             "expect_status": 201}]
    out = {"cases": [{"name": "create_1", "ok": True, "url": f"{BASE}/v1/tenants",
                      "method": "POST", "status": 201},
                     {"name": "create_2", "ok": False, "url": f"{BASE}/v1/tenants",
                      "method": "POST", "status": 409, "failures": ["answered 409"]}]}
    plan = contract_remedy.diagnose(out, spec)
    assert plan["drop"] == [] and [f["name"] for f in plan["fix_app"]] == ["create_2"]


def test_an_11th_request_with_no_limiter_is_the_app_s_to_fix_never_dropped():
    for headers in ({"Authorization": {"secret_ref": "sref_x"}}, {}):
        spec = [{"name": f"r{n}", "path": "/v1/payments", "method": "GET", "expect_status": 200,
                 "headers": headers} for n in range(1, 11)]
        spec.append({"name": "r11", "path": "/v1/payments", "method": "GET",
                     "expect_status": 429, "headers": headers})
        cases = [{"name": f"r{n}", "ok": True, "url": f"{BASE}/v1/payments", "method": "GET",
                  "status": 200} for n in range(1, 11)]
        cases.append({"name": "r11", "ok": False, "url": f"{BASE}/v1/payments", "method": "GET",
                      "status": 200, "failures": ["answered 200, expected 429"]})
        plan = contract_remedy.diagnose({"cases": cases}, spec)
        assert plan["drop"] == [] and [f["name"] for f in plan["fix_app"]] == ["r11"], headers


def test_an_auth_refused_case_that_carries_a_credential_is_never_dropped():
    spec = [dict(s, headers={"Authorization": "Bearer x"}) for s in SPEC_CASES]
    plan = contract_remedy.diagnose(_contract_output(), spec)
    assert plan["drop"] == [] and plan["fix_app"]


def test_the_dropped_case_is_said_on_the_step_s_own_verdict():
    from boss_engine.services import workflow_outcomes

    drop = contract_remedy.diagnose(_contract_output(), SPEC_CASES)["drop"]
    result = {"ok": True, "total": 11, "passed": 11, "failed": 0, "dropped_cases": drop,
              "recovery_amendment": {"drop": drop}}
    out = workflow_outcomes.apply("contract_test", result, {})
    assert out["verdict"] == "succeeded"
    assert out["note"].startswith("11/12 — 1 case dropped: payments_rate_limited_11th:")
    assert out["recovery_amendment"]["drop"] == drop, "kept when the step's output is replaced"


def test_a_held_proof_that_was_served_is_never_settled_at_zero(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import connection_spend
    from boss_engine.services.receipts import ReceiptService

    released = []
    monkeypatch.setattr(ReceiptService, "merge_result", lambda *a, **k: None)
    monkeypatch.setattr(connection_spend, "release_hold",
                        lambda c, s, p, r, *, actor, reason: released.append(reason) or {})
    for outcome in (
            {"kind": "unknown", "provider_error": "rate limit", "tokens": {"prompt_tokens": 12}},
            {"kind": "unknown", "provider_error": "429 too many requests", "usage_present": True},
            {"kind": "unknown", "provider_error": "", "summary": "Claude Code replied OK, but "
             "BOSS could not settle its cost: rate limit"}):
        held = {"receipt_id": "rcpt_x", "status": "in_progress", "created_at": _ago(hours=1),
                "invocation_id": "inv", "held_usd_micros": 50_000, "source_id": "s",
                "supported_models": ["claude_code"],
                "result": {"connection_provider": "claude_code",
                           "connection_outcome": {"model": "claude_code", **outcome}}}
        monkeypatch.setattr(connection_spend, "_hold_rows", lambda *a, h=held, **k: [h])
        with eng.begin() as c:
            connection_spend.agent_settle(c, None, project, "rcpt_x", actor=fo.ACTOR)
        assert "$0)" not in released[-1] and "conservative estimate" in released[-1], outcome
