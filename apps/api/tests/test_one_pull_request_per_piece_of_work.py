"""One pull request per piece of work; a merged PR never asks again; a failed merge is said.

THE CASE (project Hello World, paramg/boss-sandbox, 2026-11-08/09). PR #236 was approved and
merged by BOSS; a deploy fix and a More tests pass then pushed to the same branch and opened a
SECOND PR, #237, "Hello World REST API + sample · retry 1". The run drawer read "Ready to merge ·
Merge PR #236" (the run's first PR beside #237's question) and its Merge row "Only a person
merges" (the newest attempt's record). The person approved #237; its merge got GitHub's 405 and
the job dead-lettered after one attempt, while the decision still read "approved".

What these pin:
  * merged on record → the Work row, its drawer and its PR station say merged; a merged PR never
    waits on a merge, whichever attempt's question still says so;
  * a second PR of merged work is a named follow-up ("Fix: … after #236"), or withdraws itself
    when it changes nothing after the merge;
  * approving a merged PR, or approving twice, is a no-op in words and never a second merge job;
  * a failed merge is classified: superseded closes, a conflict goes to the build agent, a moved
    head retries, anything else reopens the question with "Merge failed: <reason>";
  * an approved merge that died silently is tried once more on the tick.
"""
from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.tests.test_preview_approval_gate import _insert_merge_decision


def _pid() -> str:
    return get_settings().default_project_id


REPO = "https://github.com/acme/hello"


def _decision(engine, *, n: int, branch: str, status: str = "pending", title: str = "Hello World",
              head_sha: str = "") -> str:
    did = f"dec_ppw_{uuid4().hex[:10]}"
    _insert_merge_decision(engine, decision_id=did, branch_name=branch,
                           pull_request_url=f"{REPO}/pull/{n}", project_id=_pid(), status=status)
    with engine.begin() as c:
        c.execute(text(
            "UPDATE decisions SET recommendation = recommendation || CAST(:r AS JSONB), "
            "question = :q WHERE id = :i"),
            {"r": json.dumps({"title": title, "head_sha": head_sha or f"sha{n}",
                              "run_id": None}),
             "q": f"Approve code change: {title} (PR #{n})", "i": did})
    return did


def _build(engine, *, n: int, did: str, root: str | None = None, merged: bool = False,
           title: str = "Hello World") -> str:
    run = f"crun_{uuid4().hex[:12]}"
    fctx = {"root_run_id": root} if root else {}
    if merged:
        fctx["merge"] = {"sha": f"merge{n}", "at": "2026-11-09T00:37:50+00:00", "source": "boss"}
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, decision_id, "
            "pull_request_url, pull_request_number, finalize_ctx) VALUES (:i, :p, :t, "
            "'completed', 'done', :d, :u, :n, CAST(:f AS JSONB))"),
            {"i": run, "p": _pid(), "t": title, "d": did, "u": f"{REPO}/pull/{n}", "n": n,
             "f": json.dumps(fctx)})
    return run


def _status(engine, did: str) -> tuple[str, str, dict]:
    with engine.begin() as c:
        row = c.execute(text("SELECT status, question, recommendation FROM decisions "
                             "WHERE id = :i"), {"i": did}).mappings().one()
    return str(row["status"]), str(row["question"]), dict(row["recommendation"] or {})


def _item(engine, key: str) -> dict:
    from boss_engine.services.project_experience import work

    with engine.begin() as c:
        return next(w for w in work.everything(c, _pid()) if str(w["id"]) == key)


# ── 1. merged on record → every surface says merged ─────────────────────────────────────────

def test_a_merged_pr_never_waits_on_a_merge_whichever_attempt_still_asks(engine):
    branch = f"boss/code/crun_{uuid4().hex[:8]}"
    first = _decision(engine, n=236, branch=branch, status="executed")
    root = _build(engine, n=236, did=first, merged=True)
    # A retry on the same PR whose question was never closed: stale, and merged.
    stale = _decision(engine, n=236, branch=branch)
    _build(engine, n=236, did=stale, root=root, title="Hello World · retry 1")
    item = _item(engine, root)
    assert item["state"] != "needs_you" and item["action_label"] is None
    assert item["links"]["merge_pr"] is None
    assert item["links"]["merged_pr"]["number"] == 236
    assert item["links"]["pr"]["number"] == 236
    assert next(s for s in item["stages"] if s["key"] == "pr")["state"] == "done"

    from boss_engine.services.project_experience.work_story import story

    with engine.begin() as c:
        told = story(c, _pid(), root)
    merge_row = next(s for s in told["stages"] if s["key"] == "pr")
    assert merge_row["sub"].endswith("merged") or merge_row["sub"] == "Merged"
    assert merge_row["link"]["label"] == "PR #236"


def test_a_follow_up_after_the_merge_is_shown_as_one_beside_the_merge(engine):
    branch = f"boss/code/crun_{uuid4().hex[:8]}"
    first = _decision(engine, n=236, branch=branch, status="executed")
    root = _build(engine, n=236, did=first, merged=True)
    follow = _decision(engine, n=237, branch=branch, title="Hello World · retry 1")
    _build(engine, n=237, did=follow, root=root, title="Hello World · retry 1")
    item = _item(engine, root)
    assert item["state"] == "needs_you" and item["action_label"] == "Merge"
    # The PR shown is the one in question — never the run's first PR beside #237's question.
    assert item["links"]["pr"]["number"] == 237
    assert item["links"]["merge_pr"] == {"number": 237, "external_url": f"{REPO}/pull/237",
                                         "follow_up_of": 236, "failed": None}

    from boss_engine.services.project_experience.work_story import story

    with engine.begin() as c:
        told = story(c, _pid(), root)
    merge_row = next(s for s in told["stages"] if s["key"] == "pr")
    assert "follow-up PR #237 waits on you" in merge_row["sub"]
    assert merge_row["sub"].startswith("Merged") or "merged" in merge_row["sub"]
    assert merge_row["link"]["label"] == "PR #236"


# ── 2. a second PR of merged work: relabelled follow-up, or withdrawn duplicate ─────────────

class _Reader:
    def __init__(self, files, head="headsha"):
        self.files, self.head, self.compared = files, head, []

    def pull_request(self, number):
        return {"state": "open", "merged": False, "head": {"sha": self.head}}

    def changed_files(self, base, head):
        self.compared.append((base, head))
        return [{"filename": f, "status": "modified"} for f in self.files]


def _follow_ups(engine, monkeypatch, reader):
    from boss_engine.services import cleared_causes

    monkeypatch.setattr(cleared_causes, "_last_read", {})
    with engine.begin() as c:
        return cleared_causes.reconsider_follow_ups(c, get_settings(), _pid(), reader=reader)


def _pair(engine, *, status: str = "pending") -> tuple[str, str]:
    branch = f"boss/code/crun_{uuid4().hex[:8]}"
    first = _decision(engine, n=236, branch=branch, status="executed", head_sha="head236")
    root = _build(engine, n=236, did=first, merged=True)
    second = _decision(engine, n=237, branch=branch, status=status,
                       title="Hello World REST API + sample · retry 1")
    _build(engine, n=237, did=second, root=root)
    return first, second


def test_a_second_pr_with_a_real_change_is_relabelled_a_named_follow_up(engine, monkeypatch):
    _first, second = _pair(engine)
    reader = _Reader(["hello-api/serve_hello.py", "hello-api/boss.deploy.yaml"])
    assert second not in _follow_ups(engine, monkeypatch, reader)
    assert reader.compared == [("head236", "headsha")]
    status, question, rec = _status(engine, second)
    assert status == "pending"
    assert rec["title"] == "Fix: Hello World REST API + sample after #236"
    assert rec["follow_up_of"] == 236
    assert question == ("Approve follow-up fix after #236: Hello World REST API + sample "
                        "(PR #237)")
    assert rec["follow_up_reason"].startswith("Follow-up fix after #236 merged")
    assert "hello-api/serve_hello.py" in rec["follow_up_reason"]
    # Idempotent: a second sweep reads nothing and changes nothing.
    again = _Reader(["x"])
    _follow_ups(engine, monkeypatch, again)
    assert again.compared == [] and _status(engine, second)[2]["follow_up_of"] == 236


def test_an_approved_second_pr_is_relabelled_too(engine, monkeypatch):
    _first, second = _pair(engine, status="approved")
    _follow_ups(engine, monkeypatch, _Reader(["hello-api/serve_hello.py"]))
    assert _status(engine, second)[2]["follow_up_of"] == 236


def test_a_second_pr_that_changes_nothing_after_the_merge_withdraws_itself(engine, monkeypatch):
    _first, second = _pair(engine)
    gone = _follow_ups(engine, monkeypatch, _Reader([]))
    assert second in gone
    status, _q, _rec = _status(engine, second)
    assert status == "withdrawn"
    with engine.begin() as c:
        note = c.execute(text("SELECT resolution_note FROM decisions WHERE id = :i"),
                         {"i": second}).scalar()
    assert "PR #237 repeats #236, which is merged" in note


# ── 3. approving a merged PR, or approving twice, is a no-op in words ───────────────────────

def _jobs(engine, did: str) -> int:
    with engine.begin() as c:
        return int(c.execute(text(
            "SELECT count(*) FROM jobs WHERE kind = 'github.pull_request.merge' "
            "AND payload->>'decision_id' = :i"), {"i": did}).scalar_one())


def test_approving_a_merge_question_whose_pr_is_merged_is_a_no_op_in_words(client, engine):
    branch = f"boss/code/crun_{uuid4().hex[:8]}"
    did = _decision(engine, n=236, branch=branch)
    _build(engine, n=236, did=did, merged=True)
    first = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo"})
    assert first.status_code == 200, first.text
    assert first.json()["message"].startswith("PR #236 is already merged")
    assert first.json()["status"] == "withdrawn"
    again = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo"})
    assert again.status_code == 200, again.text
    assert "already merged" in again.json()["message"]
    assert _jobs(engine, did) == 0


def test_approving_twice_queues_one_merge(client, engine):
    from apps.api.app import main as api_main

    branch = f"boss/code/crun_{uuid4().hex[:8]}"
    did = _decision(engine, n=240, branch=branch)
    previous = api_main.decisions.settings.enable_sandbox_merge
    api_main.decisions.settings.enable_sandbox_merge = True
    try:
        first = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo"})
        assert first.status_code == 200, first.text
        assert _jobs(engine, did) == 1
        second = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo"})
    finally:
        api_main.decisions.settings.enable_sandbox_merge = previous
    assert second.status_code == 200, second.text
    assert second.json()["message"] == "PR #240 was already approved; its merge is under way."
    assert _jobs(engine, did) == 1


# ── 4. a failed merge is classified, said and acted on ──────────────────────────────────────

class _Receipts:
    def __init__(self):
        self.failed = []

    def mark_failed(self, _c, rid, body):
        self.failed.append((rid, body))


class _Events:
    def __init__(self):
        self.kinds = []

    def emit(self, _c, _p, kind, *a, **k):
        self.kinds.append(kind)


class _GitHub:
    def __init__(self, *, superseded: bool):
        self.superseded, self.closed = superseded, []

    def changes_already_in(self, number):
        return self.superseded

    def last_merged_pull_request(self, branch):
        return {"number": 236, "head_sha": "head236"}

    def close_pull_request(self, number, comment=""):
        self.closed.append((number, comment))


class _Service:
    def __init__(self, github):
        self.github, self.receipts, self.events = github, _Receipts(), _Events()
        self.settings = get_settings()


def _fail(engine, service, did: str, message: str, *, n: int = 237, branch: str = "b") -> dict:
    from boss_engine.services import merge_failures

    with engine.begin() as c:
        return merge_failures.handle(service, c, _pid(), did, n, f"{REPO}/pull/{n}", branch,
                                     "rcpt_x", message)


def test_merge_failures_are_classified():
    from boss_engine.services.merge_failures import classify

    assert classify("Client error '405 Method Not Allowed' for url …/merge") == "not_mergeable"
    assert classify("Client error '409 Conflict' for url …/merge") == "head_changed"
    assert classify("Server error '502 Bad Gateway'") == "transient"
    assert classify("ReadTimeout: timed out") == "transient"
    assert classify("Client error '403 Forbidden'") == "failed"


def test_a_superseded_pr_is_closed_with_words_and_its_question_withdrawn(engine):
    branch = f"boss/code/crun_{uuid4().hex[:8]}"
    did = _decision(engine, n=237, branch=branch, status="approved")
    _build(engine, n=237, did=did)
    service = _Service(_GitHub(superseded=True))
    out = _fail(engine, service, did, "Client error '405 Method Not Allowed'", branch=branch)
    assert out["status"] == "superseded"
    assert service.github.closed and service.github.closed[0][0] == 237
    assert service.github.closed[0][1].startswith("Superseded by #236, already merged")
    status, _q, rec = _status(engine, did)
    assert status == "withdrawn" and rec["merge_failed"]["kind"] == "superseded"


def test_a_real_conflict_goes_to_the_build_agent_and_asks_again_only_when_mergeable(
        engine, monkeypatch):
    from boss_engine.services import merge_failures

    handed = []
    monkeypatch.setattr(merge_failures, "hand_to_builder",
                        lambda c, s, run, note: handed.append((run, note)) or "crun_next")
    branch = f"boss/code/crun_{uuid4().hex[:8]}"
    did = _decision(engine, n=237, branch=branch, status="approved")
    run = _build(engine, n=237, did=did)
    out = _fail(engine, _Service(_GitHub(superseded=False)), did,
                "Client error '405 Method Not Allowed'", branch=branch)
    assert out["status"] == "needs_rebase" and "retryable" not in out
    assert handed and handed[0][0] == run and "conflicts with its base branch" in handed[0][1]
    status, _q, rec = _status(engine, did)
    assert status == "withdrawn"
    assert rec["merge_failed"]["reason"] == ("PR #237 conflicts with its base branch, so GitHub "
                                             "would not merge it")
    assert "crun_next" in rec["merge_failed"]["next"]


def test_a_conflict_boss_may_not_hand_off_reopens_the_question_in_words(engine, monkeypatch):
    from boss_engine.services import merge_failures

    monkeypatch.setattr(merge_failures, "hand_to_builder", lambda *a, **k: None)
    branch = f"boss/code/crun_{uuid4().hex[:8]}"
    did = _decision(engine, n=237, branch=branch, status="approved")
    _build(engine, n=237, did=did)
    out = _fail(engine, _Service(_GitHub(superseded=False)), did, "405 Method Not Allowed",
                branch=branch)
    assert out["status"] == "failed"
    status, _q, rec = _status(engine, did)
    assert status == "pending"
    assert rec["summary"].startswith("Merge failed: PR #237 conflicts with its base branch")
    # The run says it, never "Ready to merge".
    item = next(w for w in _items_with(engine, did))
    assert item["links"]["merge_pr"]["failed"].startswith("PR #237 conflicts")


def _items_with(engine, did):
    from boss_engine.services.project_experience import work

    with engine.begin() as c:
        return [w for w in work.everything(c, _pid()) if did in (w.get("decision_ids") or [])]


def test_a_moved_head_retries_and_says_so(engine):
    did = _decision(engine, n=241, branch="boss/code/x", status="approved")
    out = _fail(engine, _Service(_GitHub(superseded=False)), did, "409 Conflict", n=241)
    assert out["retryable"] is True and out["status"] == "failed"
    status, _q, rec = _status(engine, did)
    assert status == "approved" and rec["merge_failed"]["kind"] == "head_changed"


# ── 5. an approved merge that died silently is tried once more ──────────────────────────────

def test_an_approved_merge_that_died_is_tried_once_more_on_the_tick(engine):
    from boss_engine.services import cleared_causes

    did = _decision(engine, n=242, branch="boss/code/y", status="approved")
    with engine.begin() as c:
        c.execute(text("UPDATE decisions SET approved_at = now() WHERE id = :i"), {"i": did})
        scope = c.execute(text(
            "SELECT w.org_id, w.id AS workspace_id FROM projects p JOIN workspaces w "
            "ON w.id = p.workspace_id WHERE p.id = :p"), {"p": _pid()}).mappings().one()
        c.execute(text(
            "INSERT INTO receipts (id, org_id, workspace_id, project_id, dedupe_key, effect_kind, "
            "effect_scope, payload_hash, status, data_boundary) VALUES (:i, :o, :w, :p, :k, "
            "'github.pull_request.merge', '{}'::jsonb, 'h', 'failed', 'internal')"),
            {"i": f"rcpt_{uuid4().hex[:10]}", "o": scope["org_id"], "w": scope["workspace_id"],
             "p": _pid(), "k": f"approved-pr-merge-{did}"})
    with engine.begin() as c:
        assert did in cleared_causes.retry_stranded_merges(c, get_settings(), _pid())
    with engine.begin() as c:
        assert did not in cleared_causes.retry_stranded_merges(c, get_settings(), _pid())
    assert _jobs(engine, did) == 1
