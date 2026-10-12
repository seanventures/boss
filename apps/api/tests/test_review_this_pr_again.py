"""Review this PR again: the adversarial review on a pull request's REAL diff, posted on the PR.

PR #223 (77 files) was reviewed as "no changes relative to HEAD, nothing to review", recorded as
SURVIVED, and showed "Conversation 0" on GitHub. Here the review reads the pull request's files
from GitHub at its head commit, a reviewer that still says "nothing to review" is asked again
and never counted as a pass, and the round lands on the PR with the description regenerated.
"""
import json
from types import SimpleNamespace

import httpx
from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services.core import make_id
from boss_engine.services import pr_review_again

PROJECT = "boss-dogfood"
HOLLOW = ("No changes exist on disk or in git relative to HEAD — the working tree is clean and "
          "there is no diff to review.\nVERDICT: SURVIVED")
REAL = ("REVIEWED: helpdesk-ai/auth/routes.mjs, helpdesk-ai/server.mjs\n"
        "FINDING: MAJOR helpdesk-ai/auth/routes.mjs:2 — the session is not regenerated on login\n"
        "VERDICT: REFUTED")


class FakeGitHub:
    repository_full_name = "paramg/boss-sandbox"

    def __init__(self, refuse: int = 0):
        self.refuse = refuse
        self.reviews, self.bodies, self.reads = [], [], []

    def pull_request_meta(self, number):
        return {"number": number, "title": "HelpDesk: add Login", "head_sha": "a" * 40,
                "head_ref": "boss/code/crun_x", "base_ref": "main", "base_sha": "b" * 40,
                "url": "", "body": "", "state": "open"}

    def pull_request_files(self, number):
        return [{"path": "helpdesk-ai/auth/routes.mjs", "status": "added"},
                {"path": "helpdesk-ai/server.mjs", "status": "added"},
                {"path": "helpdesk-ai/old.js", "status": "removed"}]

    def read_text_file(self, ref, path):
        self.reads.append((ref, path))
        return {"helpdesk-ai/auth/routes.mjs": "router.post('/login', h)\nreq.session.u = id\n",
                "helpdesk-ai/server.mjs": "import x from 'y'\n"}.get(path)

    def create_review(self, number, body, event="COMMENT", comments=None):
        if self.refuse:
            req = httpx.Request("POST", "https://api.github.com/x")
            raise httpx.HTTPStatusError("no", request=req,
                                        response=httpx.Response(self.refuse, request=req))
        rid = 500 + len(self.reviews)
        self.reviews.append({"id": rid, "body": body, "event": event, "comments": comments or []})
        return SimpleNamespace(url=f"https://github.com/x/pull/{number}#pullrequestreview-{rid}",
                               id=rid, state="COMMENTED")

    def review_comments_of(self, number, review_id):
        r = next(x for x in self.reviews if x["id"] == review_id)
        return [{"id": review_id * 10 + i, "path": c["path"], "line": c["line"], "body": c["body"]}
                for i, c in enumerate(r["comments"])]

    def update_review_body(self, number, review_id, body):
        pass

    def reply_to_review_comment(self, number, comment_id, body):
        return 1

    def update_pull_request_body(self, number, body):
        self.bodies.append(body)


class FakeAgent:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def run_role(self, prompt, cwd, read_only=False, **_):
        assert read_only
        self.prompts.append(prompt)
        return {"summary": self.answers.pop(0), "cost_usd_micros": 1000}


def _build_with_pr(engine) -> str:
    run_id = make_id("crun")
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, "
            "finalize_ctx, pull_request_url, pull_request_number) VALUES (:id, :p, 'completed', "
            "'done', 'HelpDesk', CAST(:spec AS JSONB), CAST(:state AS JSONB), '{}'::jsonb, "
            ":url, 223)"),
            {"id": run_id, "p": PROJECT,
             "spec": json.dumps({"instructions": "add login", "acceptance_criteria": ["sign in"],
                                 "owner": "paramg", "repo": "boss-sandbox"}),
             "state": json.dumps({"review_summary": HOLLOW, "adversarial_verdict": "survived"}),
             "url": "https://github.com/paramg/boss-sandbox/pull/223"})
    return run_id


def _fctx(engine, run_id) -> dict:
    with begin(engine) as c:
        return c.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id=:id"),
                         {"id": run_id}).scalar_one()


def test_the_pr_is_reviewed_on_its_real_diff_and_the_round_is_posted(engine, monkeypatch):
    run_id = _build_with_pr(engine)
    with begin(engine) as c:
        asked = pr_review_again.request(c, project_id=PROJECT, actor="ceo", run_id=run_id)
        again = pr_review_again.request(c, project_id=PROJECT, actor="ceo", run_id=run_id)
    assert asked["status"] == "queued" and again.get("already") is True

    gh, agent = FakeGitHub(), FakeAgent(HOLLOW, REAL)
    out = pr_review_again.execute(engine, None, {"run_id": run_id}, github=gh, agent=agent)

    # The reviewer was handed the PR's files, at its head commit.
    assert ("a" * 40, "helpdesk-ai/auth/routes.mjs") in gh.reads
    assert "=== FILE: helpdesk-ai/auth/routes.mjs ===" in agent.prompts[0]
    assert "helpdesk-ai/old.js" in agent.prompts[0]          # removed: listed, not read
    # The first answer saw nothing; it was asked again, never counted.
    assert len(agent.prompts) == 2 and "DID NOT SEE THE CHANGE" in agent.prompts[1]
    assert out["status"] == "done" and out["verdict"] == "refuted" and out["hollow"] is None
    assert out["findings"] == 1 and out["posted"] is True and out["description_updated"]

    # Posted on the PR: the build's own first review — recorded as HOLLOW, never replayed as a
    # pass — then this one, on request, with its finding at the line.
    bodies = [r["body"] for r in gh.reviews]
    assert "did not see the change" in bodies[0] and "passed — no blocking" not in bodies[0]
    assert "reviewed again on request" in bodies[-1]
    assert gh.reviews[-1]["comments"][0]["path"] == "helpdesk-ai/auth/routes.mjs"
    assert "session is not regenerated" in gh.reviews[-1]["comments"][0]["body"]
    assert "### Review" in gh.bodies[-1] and "repo is clean" not in gh.bodies[-1]

    fctx = _fctx(engine, run_id)
    assert fctx["pr_review_again"]["status"] == "done"
    assert [r["round"] for r in fctx["review_rounds"]] == [0, 1]
    assert fctx["pr_review_posts"]["rounds"]


def test_a_review_that_still_sees_nothing_is_not_a_pass(engine):
    run_id = _build_with_pr(engine)
    out = pr_review_again.execute(engine, None, {"run_id": run_id}, github=FakeGitHub(),
                                  agent=FakeAgent(HOLLOW, HOLLOW))
    assert out["verdict"] == "not_run" and out["hollow"]
    rounds = _fctx(engine, run_id)["review_rounds"]
    assert rounds[-1]["seats"]["review_code"]["hollow"]


def test_a_token_without_pull_request_write_is_said_on_the_build(engine):
    run_id = _build_with_pr(engine)
    out = pr_review_again.execute(engine, None, {"run_id": run_id},
                                  github=FakeGitHub(refuse=403), agent=FakeAgent(REAL))
    assert out["posted"] is False
    assert "Pull requests: Read and write" in out["reason"]
    from boss_engine.services.factory_view import pr_review_line

    line = pr_review_line(_fctx(engine, run_id))
    assert "Pull requests" in (line["reason"] or "")


def test_the_button_route_queues_the_review(client, engine):
    run_id = _build_with_pr(engine)
    r = client.post(f"/v1/code-tasks/{run_id}/review-pr-again", json={"actor": "ceo"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    with begin(engine) as c:
        kinds = c.execute(text("SELECT kind FROM jobs WHERE project_id=:p AND payload->>'run_id'=:r"),
                          {"p": PROJECT, "r": run_id}).scalars().all()
    assert "pull_request.review_again" in kinds
