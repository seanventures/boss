"""A question whose cause has cleared is withdrawn, automatically and with an event.

helpdesk-ai-demo, 2026-11-06: "GitHub integration is not configured" (raised by a merge that
failed) stayed in Needs you after the merge landed, and a merge question stayed pending after
its pull request merged. engine/boss_engine/services/cleared_causes.py owns the rule; these
tests drive it through the places a cause clears — an effect's receipt succeeding, a merge being
recorded, a closed PR being observed, the sweep — on the real test database.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from apps.api.tests import _factory_support as fx

ROOT = Path(__file__).resolve().parents[3]


def _repo() -> str:
    return f"acme/app-{uuid4().hex[:8]}"


def _decision(engine, *, question: str, rec: dict, minutes_ago: int = 10) -> str:
    decision_id = f"dec_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, created_at) "
            "VALUES (:i, :p, :q, CAST(:r AS JSONB), 'pending', "
            "now() - make_interval(mins => :m))"),
            {"i": decision_id, "p": fx.PROJECT, "q": question, "r": json.dumps(rec),
             "m": minutes_ago})
    return decision_id


def _credentials_question(engine, repo: str, **extra) -> str:
    from boss_engine.services.cleared_causes import failed_effect_cause

    return _decision(engine, question="GitHub integration is not configured", rec={
        "action": "provide_github_token", "reason_code": "missing_github_credentials",
        "reason": "missing_github_credentials",
        **failed_effect_cause("github.pull_request.merge", repository=repo, **extra)})


def _merge_question(engine, pr: int) -> str:
    return _decision(engine, question=f"Approve code change: app (PR #{pr})",
                     rec={"action": "review_pull_request", "reason": "merge_decision_required"})


def _status(engine, decision_id: str) -> tuple[str, str]:
    with engine.begin() as c:
        row = c.execute(text("SELECT status, resolution_note FROM decisions WHERE id = :i"),
                        {"i": decision_id}).first()
    return str(row[0]), str(row[1] or "")


def _withdrawal_event(engine, decision_id: str) -> dict:
    with engine.begin() as c:
        data = c.execute(text(
            "SELECT data FROM events WHERE entity_type = 'decision' AND entity_id = :i "
            "AND event_type = 'decision.withdrawn' ORDER BY created_at DESC LIMIT 1"),
            {"i": decision_id}).scalar()
    return data if isinstance(data, dict) else json.loads(data or "{}")


def _effect(engine, kind: str, repo: str, *, succeed: bool = True) -> str:
    from boss_engine.services.receipts import ReceiptService

    receipts = ReceiptService()
    with engine.begin() as c:
        scope = c.execute(text(
            "SELECT w.id AS workspace_id, w.org_id FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
            {"p": fx.PROJECT}).mappings().one()
        receipt, _ = receipts.create_or_reuse(
            c, scope["org_id"], scope["workspace_id"], fx.PROJECT, f"test:{uuid4().hex}", kind,
            {"repository": repo}, {"n": 1})
        if succeed:
            receipts.mark_succeeded(c, receipt["id"], "ref", {"ok": True})
        else:
            receipts.mark_failed(c, receipt["id"], {"message": "no"})
    return str(receipt["id"])


def test_a_github_write_that_succeeds_withdraws_the_credentials_question(client, engine):
    repo = _repo()
    decision_id = _credentials_question(engine, repo)
    _effect(engine, "github.pull_request.merge", repo, succeed=False)
    assert _status(engine, decision_id)[0] == "pending"        # a failure clears nothing
    _effect(engine, "github.pull_request.create", _repo())
    assert _status(engine, decision_id)[0] == "pending"        # another repository's success
    receipt_id = _effect(engine, "github.pull_request.merge", repo)
    status, note = _status(engine, decision_id)
    assert status == "withdrawn"
    assert "github.pull_request.merge succeeded" in note and repo in note
    event = _withdrawal_event(engine, decision_id)
    assert event["cause_cleared"]["kind"] == "effect_succeeded"
    assert event["cause_cleared"]["receipt_id"] == receipt_id


def test_a_merge_recorded_withdraws_the_merge_question_and_the_failed_effects(client, engine):
    from boss_engine.services import pr_merge

    pr = 40 + int(uuid4().hex[:3], 16) % 900
    merge_q = _merge_question(engine, pr)
    run_id = fx.new_run(engine, status="completed", phase="done", pr=pr, decision_id=merge_q)
    url = f"https://github.example/acme/app/pull/{pr}"
    cred_q = _credentials_question(engine, "/", pull_request_url=url, pull_request_number=pr)
    deploy_q = _decision(engine, question=f"Deploy PR #{pr}?",
                         rec={"action": "review_deployment", "pull_request_url": url})
    with engine.begin() as c:
        pr_merge.record(c, run_id, "abc123", source="github")
    status, note = _status(engine, merge_q)
    assert status == "withdrawn" and f"PR #{pr} was already merged" in note
    assert _withdrawal_event(engine, merge_q)["cause_cleared"]["kind"] == "pull_request_merged"
    assert _status(engine, cred_q)[0] == "withdrawn"
    # A question the merge does NOT answer stays: deploying the merged code is still asked.
    assert _status(engine, deploy_q)[0] == "pending"


def test_a_closed_pull_request_withdraws_its_merge_question(client, engine):
    from boss_engine.services import pr_merge

    pr = 1000 + int(uuid4().hex[:3], 16)
    merge_q = _merge_question(engine, pr)
    run_id = fx.new_run(engine, status="completed", phase="done", pr=pr, decision_id=merge_q)

    class Closed:
        def pull_request(self, number):
            return {"number": number, "state": "closed", "merged": False}

    with engine.begin() as c:
        seen = pr_merge.observe(c, None, fx.PROJECT, run_id, reader=Closed())
    assert seen["state"] == pr_merge.CLOSED
    status, note = _status(engine, merge_q)
    assert status == "withdrawn" and "closed without merging" in note


def test_an_open_pull_request_keeps_its_question(client, engine):
    from boss_engine.services import pr_merge

    pr = 5000 + int(uuid4().hex[:3], 16)
    merge_q = _merge_question(engine, pr)
    run_id = fx.new_run(engine, status="completed", phase="done", pr=pr, decision_id=merge_q)

    class Open:
        def pull_request(self, number):
            return {"number": number, "state": "open"}

    with engine.begin() as c:
        pr_merge.observe(c, None, fx.PROJECT, run_id, reader=Open())
    assert _status(engine, merge_q)[0] == "pending"


def test_the_sweep_withdraws_what_cleared_while_nobody_listened_once(client, engine):
    from boss_engine.services.cleared_causes import withdraw_cleared

    pr = 9000 + int(uuid4().hex[:3], 16)
    merge_q = _merge_question(engine, pr)
    # Merged by a person in GitHub and recorded without the hook (an older install).
    fx.new_run(engine, status="completed", phase="done", pr=pr, decision_id=merge_q,
               fctx={"merge": {"sha": "def456", "source": "github"}})
    unmerged_q = _merge_question(engine, pr + 1)
    fx.new_run(engine, status="completed", phase="done", pr=pr + 1, decision_id=unmerged_q)
    with engine.begin() as c:
        withdrawn = withdraw_cleared(c, fx.PROJECT)
    assert merge_q in withdrawn and unmerged_q not in withdrawn
    assert _status(engine, unmerged_q)[0] == "pending"
    with engine.begin() as c:
        assert merge_q not in withdraw_cleared(c, fx.PROJECT)


def test_every_failed_effect_question_says_what_clears_it():
    """A blocking decision raised with a registered failed-effect reason must record its cause
    (cleared_causes.failed_effect_cause), or the question can only be cleared project-wide."""
    from boss_engine.services.cleared_causes import EFFECT_FAILURE_CAUSES

    missing, found = [], 0
    for path in sorted((ROOT / "engine" / "boss_engine").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(code in source for code in EFFECT_FAILURE_CAUSES):
            continue
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", getattr(node.func, "id", "")) in
                    {"create_blocking", "create_blocking_decision"}):
                continue
            codes = [a.value for a in node.args
                     if isinstance(a, ast.Constant) and a.value in EFFECT_FAILURE_CAUSES]
            if not codes:
                continue
            found += 1
            rec = next((a for a in node.args if isinstance(a, ast.Dict)), None)
            spreads = [v for k, v in zip(rec.keys, rec.values) if k is None] if rec else []
            if not any("failed_effect_cause" in ast.unparse(v) for v in spreads):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert found >= 3
    assert not missing, ("These raise a failed-effect question without recording what clears "
                         f"it (cleared_causes.failed_effect_cause): {missing}")
