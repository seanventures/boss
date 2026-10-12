"""A reviewer judges the change, a review that never saw it is not a pass, and the review
conversation reaches the pull request.

PR #223 (77 files, 13,830 lines) was reviewed as:

    No changes exist on disk or in git relative to HEAD — the working tree is clean and there
    is no diff to review. …

and recorded ``adversarial_verdict: survived``, every review gate PASSED, and the pull request
showed "Conversation 0 · No reviews".
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import httpx
import pytest

from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import pr_description, pr_review_post, review_comments, review_input
from boss_engine.services.factory_view import gate_reason, pr_review_line

PR_223_SUMMARY = (
    "No changes exist on disk or in git relative to HEAD — the working tree is clean and there "
    "is no diff to review. Since the task statement itself says \"(no file changes detected)\" "
    "and this is confirmed by `git status`/`git diff` showing nothing, there is no "
    "implementation to evaluate against the acceptance criteria.\n\nVERDICT: SURVIVED")
PR_223_PATHS = [f"helpdesk-ai/src/file_{i}.ts" for i in range(77)]


# ── 1. the reviewer is given THE change ──────────────────────────────────────────────────────

def test_a_seeded_file_the_live_diff_lost_is_still_in_front_of_the_reviewer():
    live = {"helpdesk-ai/server.mjs": "export const x = 1;\n"}
    on_disk = {"helpdesk-ai/auth/routes.mjs": "router.get('/auth/providers')\n"}
    change = review_input.build(live, ["helpdesk-ai/server.mjs", "helpdesk-ai/auth/routes.mjs",
                                       "helpdesk-ai/node_modules/x/index.js"],
                                read=on_disk.get, keep=gov.workspace_keepable)
    assert change.paths == ["helpdesk-ai/auth/routes.mjs", "helpdesk-ai/server.mjs"]
    block = change.block()
    assert "2 file(s) changed" in block
    assert "=== FILE: helpdesk-ai/auth/routes.mjs ===" in block
    assert "does NOT mean there is nothing to review" in block
    assert "(no file changes detected)" not in block


def test_every_changed_path_is_listed_even_past_the_inline_cap():
    files = {f"a/f{i}.py": "x" * 20000 for i in range(10)}
    block = review_input.render(review_input.build(files))
    for i in range(10):
        assert f"  - a/f{i}.py" in block
    assert "every changed file is listed above" in block


def test_every_reviewer_seat_reads_the_one_builder_and_names_what_it_read():
    src = inspect.getsource(gov._run_governed_code_task)
    # The panel and every re-review take their input from review_input, never a bare diff.
    assert src.count("_review_change_now()") >= 3
    assert "_render_change_for_review(\n                accumulated" not in src
    for seat in ("review_code", "security_check", "consistency_check"):
        assert f'_judge_seat("{seat}"' in src or f"_judge_seat(\n                            \"{seat}\"" in src
    assert "REVIEWED:" in gov._change_clause("x")


# ── 2. a hollow review is not a pass ──────────────────────────────────────────────────────────

def test_pr_223s_review_is_hollow():
    reason = review_input.hollow_reason(PR_223_SUMMARY, PR_223_PATHS)
    assert reason and "nothing to review" in reason and "77" in reason


def test_a_review_that_names_none_of_the_files_is_hollow_and_one_that_reads_them_is_not():
    assert review_input.hollow_reason("Looks good to me.\nVERDICT: SURVIVED", PR_223_PATHS)
    assert review_input.hollow_reason("", PR_223_PATHS)
    assert review_input.hollow_reason(
        "REVIEWED: helpdesk-ai/src/file_3.ts\nThe handler validates input.\nVERDICT: SURVIVED",
        PR_223_PATHS) is None
    assert review_input.hollow_reason(
        "FINDING: MAJOR src/x.ts:4 — token never expires\nVERDICT: REFUTED", PR_223_PATHS) is None
    # An empty change is not reviewed for hollowness: there is nothing to have missed.
    assert review_input.hollow_reason(PR_223_SUMMARY, []) is None


def test_a_hollow_review_never_passes_the_gate_and_reruns_the_check_not_the_coder():
    # The seat records a hollow vote as not_run, so the panel's verdict is not `survived`.
    verdict, _conf = gov._aggregate_panel(["not_run"], 0.5)
    assert verdict != "survived"
    passed, _b, _d = gov._review_gate_from_findings(PR_223_SUMMARY, verdict)
    assert passed is False
    evidence = {"review_summary": PR_223_SUMMARY, "adversarial_verdict": verdict,
                "review_hollow": {"review_code": "the reviewer said there was nothing to review"},
                "consistency_verdict": "hollow"}
    faults = gov.classify_gate_faults(failing=["code_review_passed", "consistent"],
                                      evidence={**evidence, "review_hollow": {
                                          "review_code": "x", "consistency_check": "y"}})
    assert faults == {"code_review_passed": "HARNESS", "consistent": "HARNESS"}
    assert gate_reason("code_review_passed", False, evidence) == review_input.HOLLOW_GATE_REASON
    assert review_input.HOLLOW_GATE_REASON == "review did not see the change"


def test_the_consistency_and_security_gates_fail_on_a_hollow_check():
    src = inspect.getsource(gov._run_governed_code_task)
    assert 'gates["consistent"] = cons_verdict not in ("drift", "hollow")' in src
    assert '"hollow" if hollow' in src            # security: "hollow" is never "clean"


# ── 3. the conversation reaches the pull request ─────────────────────────────────────────────

class FakeGitHub:
    repository_full_name = "paramg/boss-sandbox"

    def __init__(self, refuse: int = 0, reject_inline: bool = False):
        self.reviews: list[dict] = []
        self.updated: list[tuple[int, str]] = []
        self.replies: list[tuple[int, str]] = []
        self.refuse = refuse
        self.reject_inline = reject_inline

    def _refused(self, code):
        req = httpx.Request("POST", "https://api.github.com/x")
        raise httpx.HTTPStatusError("refused", request=req,
                                    response=httpx.Response(code, request=req))

    def create_review(self, number, body, event="COMMENT", comments=None):
        if self.refuse:
            self._refused(self.refuse)
        if comments and self.reject_inline:
            self._refused(422)
        rid = 1000 + len(self.reviews)
        self.reviews.append({"id": rid, "body": body, "event": event, "comments": comments or []})
        return SimpleNamespace(url=f"https://github.com/x/pull/{number}#r{rid}", id=rid,
                               state="COMMENTED")

    def review_comments_of(self, number, review_id):
        review = next(r for r in self.reviews if r["id"] == review_id)
        return [{"id": review_id * 10 + i, "path": c["path"], "line": c["line"], "body": c["body"]}
                for i, c in enumerate(review["comments"])]

    def update_review_body(self, number, review_id, body):
        self.updated.append((review_id, body))

    def reply_to_review_comment(self, number, comment_id, body):
        self.replies.append((comment_id, body))
        return 9000 + len(self.replies)


def _comments():
    return [
        {"id": "rc_1", "round": 0, "stage": "review_code", "severity": "major",
         "path": "helpdesk-ai/auth/routes.mjs", "line": 47, "body": "session not regenerated",
         "response_kind": "fixed", "response": "regenerate the session on login",
         "response_round": 1, "response_files": ["helpdesk-ai/auth/routes.mjs"]},
        {"id": "rc_2", "round": 0, "stage": "review_code", "severity": "minor", "path": None,
         "line": None, "body": "no rate limit on login", "response_kind": "disputed",
         "response": "out of scope for this PRD", "response_round": 1},
        {"id": "rc_3", "round": 1, "stage": "review_code", "severity": "nit",
         "path": "helpdesk-ai/server.mjs", "line": 3, "body": "unused import"},
    ]


def _rounds():
    return [{"round": 0, "seats": {"review_code": {"verdict": "refuted",
                                                   "summary": "REVIEWED: routes.mjs …"}}},
            {"round": 1, "seats": {"review_code": {"verdict": "survived",
                                                   "summary": "REVIEWED: server.mjs …"}}}]


def test_every_round_is_a_review_with_inline_findings_and_the_implementers_replies():
    gh = FakeGitHub()
    out = pr_review_post.post(gh, 223, comments=_comments(), rounds=_rounds(),
                              deferred=[{"severity": "nit", "text": "rename x"}],
                              repository=gh.repository_full_name)
    assert out["posted"] and out["reviews_created"] == 2 and out["inline"] == 2
    first, second = gh.reviews
    assert all(r["event"] == "COMMENT" for r in gh.reviews)
    assert "Round 1 of 2" in first["body"] and "Round 2 of 2" in second["body"]
    assert first["comments"][0]["path"] == "helpdesk-ai/auth/routes.mjs"
    assert "no rate limit on login" in first["body"]            # unanchored → in the body
    assert "disputed in fix round 1" in first["body"]
    assert "Deferred" in second["body"] and "rename x" in second["body"]
    assert "Deferred" not in first["body"]
    # The fixed finding's answer is a reply UNDER that comment.
    assert len(gh.replies) == 1
    assert "fixed in fix round 1" in gh.replies[0][1] and "routes.mjs" in gh.replies[0][1]

    # A re-run updates the same reviews and never replies twice.
    again = pr_review_post.post(gh, 223, comments=_comments(), rounds=_rounds(),
                                state=out["state"], repository=gh.repository_full_name)
    assert again["reviews_created"] == 0 and again["reviews_updated"] == 2
    assert len(gh.reviews) == 2 and len(gh.replies) == 1


def test_an_old_hollow_review_is_never_replayed_as_a_pass():
    legacy = pr_review_post.legacy_round(
        {"review_summary": PR_223_SUMMARY, "adversarial_verdict": "survived"}, PR_223_PATHS)
    seat = legacy["seats"]["review_code"]
    assert seat["verdict"] == "hollow" and "nothing to review" in seat["hollow"]
    body = pr_review_post.round_body(legacy, [], ordinal=1, total=2)
    assert "did not see the change" in body and "passed — no blocking" not in body
    again = pr_review_post.round_body({"round": 1, "kind": "review_again", "seats": {}}, [],
                                      ordinal=2, total=2)
    assert "reviewed again on request" in again


def test_a_token_that_cannot_write_reviews_is_said_in_words():
    gh = FakeGitHub(refuse=403)
    out = pr_review_post.post(gh, 223, comments=_comments(), rounds=_rounds(),
                              repository="paramg/boss-sandbox")
    assert out["posted"] is False and out["permission"] is True
    assert "Pull requests: Read and write" in out["reason"]
    line = pr_review_line({"pr_review_post_result": out})
    assert line["word"] == "Review not posted" and "Pull requests" in line["reason"]


def test_a_finding_outside_the_diff_still_posts_the_round():
    gh = FakeGitHub(reject_inline=True)
    out = pr_review_post.post(gh, 223, comments=_comments()[:1], rounds=_rounds()[:1],
                              repository="r")
    assert out["posted"] and gh.reviews[0]["comments"] == []
    assert "session not regenerated" in gh.reviews[0]["body"]


def test_the_implementer_answers_by_number_in_the_order_it_was_shown():
    shown = review_comments.implementer_order([
        review_comments.ReviewComment.from_dict({"severity": "minor", "body": "b", "id": "rc_b"}),
        review_comments.ReviewComment.from_dict({"severity": "blocker", "body": "a",
                                                 "id": "rc_a"})])
    assert [c.ref for c in shown] == ["rc_a", "rc_b"]
    parsed = review_comments.parse_responses(
        "Done.\nRESPONSE: 1 FIXED — moved the check into the handler\n"
        "**RESPONSE: 2 DISPUTED** — the spec does not ask for it")
    assert parsed == {1: ("fixed", "moved the check into the handler"),
                      2: ("disputed", "the spec does not ask for it")}
    assert "RESPONSE: <number> FIXED" in review_comments.response_clause()


def test_the_description_is_generated_from_records_never_agent_chat():
    body = pr_description.render(
        title="HelpDesk: add Login", evidence={"test_cmd": "npm test", "tests_passed": True,
                                               "review_hollow": {"review_code": "nothing seen"}},
        gates={"code_review_passed": False, "written_tests_pass": True},
        paths=PR_223_PATHS, additions=13830, deletions=0, rounds=_rounds(),
        comments=_comments(), run_id="crun_x", build_url="http://boss/?area=code_tasks&build=x")
    for section in ("### What changed", "### Tests", "### Checks", "### Review", "### In BOSS"):
        assert section in body
    assert "77 files (+13830 −0)" in body and "`helpdesk-ai/` — 77 files" in body
    assert "❌ Review passed" in body and "did not see the change" in body
    assert "repo is clean" not in body and "Governed pipeline" not in body
    src = inspect.getsource(gov._run_governed_code_task)
    assert "r2.get('summary','')}" not in src     # the implementer's chat is not the description


def test_the_description_reads_the_runs_recorded_tests_and_the_final_review_round():
    """PR #223 said "No test command was recorded" over a run whose `tests` list held the
    passing build/lint/e2e command, and "✅ Review passed" from the round that saw nothing."""
    body = pr_description.render(
        title="HelpDesk", evidence={"tests": [{"command": "npm run test:e2e", "ok": True,
                                               "exit_code": 0}]},
        gates={"code_review_passed": True},
        rounds=[{"seats": {"review_code": {"verdict": "hollow"}}},
                {"seats": {"review_code": {"verdict": "refuted"}}}])
    assert "No test command was recorded" not in body and "✅ `npm run test:e2e`" in body
    assert "❌ Review passed" in body
    passing = pr_description.render(
        title="HelpDesk", evidence={"test_command": "npm test", "tests_passed": True},
        gates={"code_review_passed": True},
        rounds=[{"seats": {"review_code": {"verdict": "hollow"}}},
                {"seats": {"review_code": {"verdict": "survived"}}}])
    assert "✅ `npm test`" in passing and "✅ Review passed" in passing
    hollow_last = pr_description.render(
        title="HelpDesk", evidence={}, gates={"code_review_passed": True},
        rounds=[{"seats": {"review_code": {"verdict": "hollow"}}}])
    assert "❌ Review passed" in hollow_last


# ── 4. Review this PR again exists everywhere a person or agent would look ──────────────────

def test_review_again_is_an_api_a_tool_a_job_and_a_button():
    from pathlib import Path

    from boss_engine.identity.job_permissions import permission_for
    from boss_engine.tools.builtin import build_default_registry

    root = Path(__file__).resolve().parents[2]
    assert "review_pull_request_again" in build_default_registry().names()
    assert permission_for("pull_request.review_again")
    assert '"/v1/code-tasks/{run_id}/review-pr-again"' in (root / "apps/api/app/main.py").read_text()
    runner = (root / "apps/worker/boss_worker/runner.py").read_text()
    assert '"pull_request.review_again"' in runner and "run_pr_review_again" in runner
    outputs = (root / "apps/web/app/factory/outputs.tsx").read_text()
    assert 'data-testid="fx-pr-review-again"' in outputs and "Review this PR again" in outputs
