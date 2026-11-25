"""A PR comment and a fix pull request as tools and workflow steps (BOSSAPP-1141 / 1142), against a fake GitHub adapter.

What is claimed:

* `github_comment` posts on a PR or issue of a repository bound to the project, updates the
  comment it posted under a marker instead of adding another, is receipted, and refuses (does
  not raise) when the project has no repository;
* `github_open_fix_pr` creates `boss/<run>-fix-<slug>` — the one branch shape the egress rules
  let a run push — writes the file(s) (content or a unified diff applied), opens one PR whose
  body names the run and the report, is receipted and idempotent;
* both sit behind their catalogue permission at the registry door;
* as capabilities, their verdicts fail the step on a refusal and `fix_pr` is at the approval
  floor;
* `notify` has a `github:` channel.
"""
from __future__ import annotations

import fnmatch
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.identity import egress_rules
from boss_engine.services import github_actions as ga
from boss_engine.services import workflow_capabilities as caps
from boss_engine.services import workflow_outcomes as outcomes
from boss_engine.tools.builtin import build_default_registry

from .onboarding_workflow_fixtures import eng, project  # noqa: F401
from .test_workflow_custom_steps import Ctx


def _as_run(run_id):
    """What the workflow runner does around a step's tool call: the run is set on the server."""
    import contextlib

    @contextlib.contextmanager
    def scope():
        token = ga.WORKFLOW_RUN_ID.set(run_id)
        try:
            yield
        finally:
            ga.WORKFLOW_RUN_ID.reset(token)
    return scope()


class FakeGitHub:
    """Enough of GitHubAdapter for the two effects: comments, branches, files, PRs."""

    def __init__(self, default_branch="main", files=None):
        self.default_branch = default_branch
        self.comments: dict[int, list[dict]] = {}
        self.branches: dict[str, dict[str, str]] = {default_branch: dict(files or {})}
        self.pulls: list[dict] = []
        self.calls: list[tuple] = []
        self._next = 100

    # comments
    def pull_request_issue_comments(self, number, *, limit=300):
        return list(self.comments.get(int(number), []))

    def create_issue_comment(self, number, body):
        self._next += 1
        row = {"id": self._next, "body": body, "user": "boss[bot]", "bot": True,
               "created_at": f"2026-11-14T00:00:{self._next % 60:02d}Z", "updated_at": None}
        self.comments.setdefault(int(number), []).append(row)
        self.calls.append(("create_comment", int(number)))
        return SimpleNamespace(url=f"https://github.com/o/r/pull/{number}#issuecomment-{row['id']}",
                               id=row["id"])

    def update_issue_comment(self, comment_id, body):
        for rows in self.comments.values():
            for row in rows:
                if row["id"] == int(comment_id):
                    row["body"] = body
                    self.calls.append(("update_comment", int(comment_id)))
                    return SimpleNamespace(url=f"https://github.com/o/r/issues/comments/{comment_id}",
                                           id=int(comment_id))
        raise RuntimeError("no such comment")

    # branches and files
    def ensure_branch(self, branch, base=None):
        self.calls.append(("ensure_branch", branch, base))
        if branch not in self.branches:
            self.branches[branch] = dict(self.branches.get(base or self.default_branch, {}))
        return "sha-" + branch

    def read_text_file(self, branch, path):
        return self.branches.get(branch, {}).get(path)

    def upsert_file(self, branch, path, content, message):
        self.calls.append(("upsert", branch, path))
        self.branches.setdefault(branch, {})[path] = content
        return f"commit-{len(self.calls)}"

    def create_pull_request(self, branch, title, body, draft=None, base=None):
        self.calls.append(("pr", branch, base))
        for pr in self.pulls:
            if pr["head"] == branch and pr["state"] == "open":
                pr["body"] = body
                return SimpleNamespace(url=pr["url"], number=pr["number"])
        number = len(self.pulls) + 1
        pr = {"number": number, "head": branch, "base": base, "title": title, "body": body,
              "state": "open", "url": f"https://github.com/o/r/pull/{number}", "draft": draft}
        self.pulls.append(pr)
        return SimpleNamespace(url=pr["url"], number=number)


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub(files={"tenants/lumen-pay.yaml": "tenant: lumen-pay\nupstream: http://x\n"})

    def bound(db, settings, project_id, repository):
        # The binding itself still resolves through the database: the fake is the ADAPTER only.
        if repository not in (None, "", "northgate/gateway", "bind_fake"):
            raise ga.GitHubActionError(f"{repository!r} is not a repository of this project.",
                                       code="not_this_project")
        return ga.BoundRepository("bind_fake", "northgate/gateway", "main", fake)

    monkeypatch.setattr(ga, "adapter_for_repository", bound)
    return fake


def _events(c, project_id, kind):
    return c.execute(text("SELECT data FROM events WHERE project_id=:p AND event_type=:t "
                          "ORDER BY created_at"), {"p": project_id, "t": kind}).scalars().all()


def _receipts(c, project_id, prefix):
    return c.execute(text("SELECT dedupe_key, effect_kind, status, provider_ref FROM receipts "
                          "WHERE project_id=:p AND dedupe_key LIKE :k ORDER BY created_at"),
                     {"p": project_id, "k": prefix + "%"}).mappings().all()


def _tools():
    return {s.name: s for s in build_default_registry().specs()}


# ── the comment ────────────────────────────────────────────────────────────────────────────

def test_a_comment_is_posted_receipted_and_on_the_ledger(eng, project, github):
    tool = _tools()["github_comment"]
    with _as_run("wfr_1"):
        out = tool.handler(Ctx(eng), project_id=project, actor="usr_a",
                           args={"number": 12, "body": "## Validation\n\n1 violation"})
    assert out["ok"] and out["posted"] and out["updated"] is False
    assert out["repository"] == "northgate/gateway" and out["number"] == 12
    assert out["comment_url"].endswith(f"#issuecomment-{out['comment_id']}")
    assert github.comments[12][0]["body"].startswith("## Validation")
    with eng.begin() as c:
        receipts = _receipts(c, project, "github_comment:northgate/gateway:12:")
        posted = _events(c, project, "github.comment_posted")
    assert [r["effect_kind"] for r in receipts] == ["github.issue_comment"]
    assert receipts[0]["status"] == "succeeded" and receipts[0]["provider_ref"] == out["comment_url"]
    assert posted[-1]["run_id"] == "wfr_1" and posted[-1]["receipt_id"] == out["receipt_id"]


def test_a_marked_comment_is_updated_not_repeated(eng, project, github):
    tool = _tools()["github_comment"]
    first = tool.handler(Ctx(eng), project_id=project, actor="usr_a",
                         args={"number": 7, "body": "round 1", "marker": "validation-report"})
    second = tool.handler(Ctx(eng), project_id=project, actor="usr_a",
                          args={"number": 7, "body": "round 2", "marker": "validation-report"})
    assert first["updated"] is False and second["updated"] is True
    assert second["comment_id"] == first["comment_id"]
    assert len(github.comments[7]) == 1, "one report per PR, kept current"
    assert github.comments[7][0]["body"].startswith("round 2")
    assert ga.marker_line("validation-report") in github.comments[7][0]["body"]
    # A different marker is a different comment.
    third = tool.handler(Ctx(eng), project_id=project, actor="usr_a",
                         args={"number": 7, "body": "tests", "marker": "contract-tests"})
    assert third["updated"] is False and len(github.comments[7]) == 2


def test_a_persons_comment_carrying_the_marker_is_never_overwritten(eng, project, github):
    """A marker is predictable: anyone can paste it. BOSS updates only its own comment."""
    github.comments[9] = [{"id": 1, "user": "mallory", "bot": False,
                           "body": "hi " + ga.marker_line("validation-report"),
                           "created_at": "2026-11-14T00:00:00Z", "updated_at": None}]
    out = _tools()["github_comment"].handler(
        Ctx(eng), project_id=project, actor="a",
        args={"number": 9, "body": "report", "marker": "validation-report"})
    assert out["updated"] is False and out["comment_id"] != 1
    assert github.comments[9][0]["body"].startswith("hi "), "a person's comment was edited"
    assert ("update_comment", 1) not in github.calls


def test_another_bots_comment_or_an_unrecorded_one_is_not_ours(eng, project, github):
    marker = ga.marker_line("validation-report")
    other_bot = {"id": 2, "user": "renovate[bot]", "bot": True, "body": "x " + marker,
                 "created_at": "2026-11-14T00:00:01Z", "updated_at": None}
    assert ga.is_boss_comment(other_bot, "boss-app[bot]") is False
    assert ga.is_boss_comment({**other_bot, "user": "BOSS-App[bot]"}, "boss-app[bot]") is True
    github.comments[4] = [other_bot]
    first = _tools()["github_comment"].handler(
        Ctx(eng), project_id=project, actor="a",
        args={"number": 4, "body": "r1", "marker": "validation-report"})
    assert first["updated"] is False and github.comments[4][0]["body"] == "x " + marker
    # Only the comment BOSS recorded under the marker is updated, even among BOSS's own.
    assert ga.find_marked_comment(github.comments[4], "validation-report",
                                  comment_id=first["comment_id"])["id"] == first["comment_id"]
    second = _tools()["github_comment"].handler(
        Ctx(eng), project_id=project, actor="a",
        args={"number": 4, "body": "r2", "marker": "validation-report"})
    assert second["updated"] is True and second["comment_id"] == first["comment_id"]


def test_no_repository_is_a_refusal_not_a_crash(eng, project, monkeypatch):
    """The project has no binding: the tool says so (the shape FAILURE_SHAPES records)."""
    out = _tools()["github_comment"].handler(Ctx(eng), project_id=project, actor="usr_a",
                                             args={"number": 1, "body": "x"})
    assert out["ok"] is False and out["code"] == "no_repository"
    assert "no GitHub repository bound" in out["error"]
    assert outcomes.judge("github_comment", out, {}).outcome == outcomes.FAILED


def test_an_empty_comment_or_a_bad_number_is_refused(eng, project, github):
    tool = _tools()["github_comment"]
    assert tool.handler(Ctx(eng), project_id=project, actor="a",
                        args={"number": 1, "body": "  "})["code"] == "invalid"
    assert tool.handler(Ctx(eng), project_id=project, actor="a",
                        args={"number": "zero", "body": "x"})["code"] == "invalid"
    assert tool.handler(Ctx(eng), project_id=project, actor="a",
                        args={"number": 1, "body": "x", "repository": "other/repo"}
                        )["code"] == "not_this_project"
    assert github.calls == []


def test_a_refusal_from_github_is_the_steps_result(eng, project, github):
    def boom(number, body):
        raise RuntimeError("422 Validation Failed")
    github.create_issue_comment = boom
    out = _tools()["github_comment"].handler(Ctx(eng), project_id=project, actor="a",
                                             args={"number": 3, "body": "x"})
    assert out["ok"] is False and out["code"] == "github_refused"
    assert "422" in out["error"]


# ── the fix PR ─────────────────────────────────────────────────────────────────────────────

def _fix(eng, project, **args):
    run_id = args.pop("workflow_run_id", "")
    with _as_run(run_id):
        return _tools()["github_open_fix_pr"].handler(Ctx(eng), project_id=project,
                                                      actor="usr_a", args=args)


def test_a_fix_pr_is_a_run_branch_the_file_and_one_pull_request(eng, project, github):
    out = _fix(eng, project, title="fix: lumen-pay upstream must be https",
               path="tenants/lumen-pay.yaml", content="tenant: lumen-pay\nupstream: https://x\n",
               report="SEC-004 upstream must be https:// (line 2)", workflow_run_id="wfr_abc",
               slug="lumen-pay")
    assert out["ok"], out
    assert out["branch"] == "boss/wfr_abc-fix-lumen-pay"
    assert out["base_branch"] == "main"
    assert out["pull_request_url"] == "https://github.com/o/r/pull/1"
    assert out["files"] == ["tenants/lumen-pay.yaml"]
    assert github.branches["boss/wfr_abc-fix-lumen-pay"]["tenants/lumen-pay.yaml"].endswith(
        "https://x\n")
    body = github.pulls[0]["body"]
    assert "wfr_abc" in body and "SEC-004" in body and "tenants/lumen-pay.yaml" in body
    assert "Merging stays a person's decision" in body
    with eng.begin() as c:
        receipts = _receipts(c, project, "github_fix_pr:northgate/gateway:boss/wfr_abc")
        opened = _events(c, project, "github.fix_pr_opened")
    assert [r["effect_kind"] for r in receipts] == ["github.fix_pull_request"]
    assert receipts[0]["provider_ref"] == out["pull_request_url"]
    assert opened[-1]["run_id"] == "wfr_abc" and opened[-1]["branch"] == out["branch"]


def test_the_branch_is_one_the_egress_rules_let_the_run_push(eng, project, github):
    """`boss/<run>-fix-<slug>` matches `refs/heads/boss/<run>-*` (github_app.push_ref_pattern),
    so the same push is allowed from BOSS's token and from an agent inside the run."""
    out = _fix(eng, project, title="t", path="a.yaml", content="a: 1\n",
               workflow_run_id="wfr_x:1/2#3", slug="Lumen Pay!")
    assert out["branch"] == "boss/wfr_x-1-2-3-fix-lumen-pay"
    pattern = f"refs/heads/boss/{ga.safe_run_id('wfr_x:1/2#3')}-*"
    assert fnmatch.fnmatchcase(f"refs/heads/{out['branch']}", pattern)
    perms = {"contents": "write", "pull_requests": "write", "metadata": "read"}
    put = egress_rules.check_api("PUT", "/repos/o/r/contents/a.yaml", perms=perms,
                                 repos=["o/r"], body={"branch": out["branch"]},
                                 push_pattern=pattern)
    assert put.allowed, put
    pr = egress_rules.check_api("POST", "/repos/o/r/pulls", perms=perms, repos=["o/r"],
                                body={"head": out["branch"], "base": "main"},
                                push_pattern=pattern)
    assert pr.allowed, pr
    # Editing a comment is NOT in a run's rule table: an agent inside a run could otherwise
    # edit anybody's comment. BOSS edits its own marker comment through the tool only.
    patch = egress_rules.check_api("PATCH", "/repos/o/r/issues/comments/55", perms=perms,
                                   repos=["o/r"], body={"body": "x"}, push_pattern=pattern)
    assert not patch.allowed and patch.reason == "not_in_rule_table", patch


def test_a_fix_pr_is_idempotent(eng, project, github):
    args = dict(title="t", path="a.yaml", content="a: 1\n", workflow_run_id="wfr_r", slug="s")
    first = _fix(eng, project, **args)
    second = _fix(eng, project, **args)
    assert first["pull_request_url"] == second["pull_request_url"]
    assert len(github.pulls) == 1
    assert second["commits"] == [{"path": "a.yaml", "sha": "", "note": "unchanged"}]
    with eng.begin() as c:
        assert len(_receipts(c, project, "github_fix_pr:")) == 1


def test_a_unified_diff_is_applied_to_the_base_branch_file(eng, project, github):
    diff = ("--- a/tenants/lumen-pay.yaml\n+++ b/tenants/lumen-pay.yaml\n"
            "@@ -1,2 +1,2 @@\n tenant: lumen-pay\n-upstream: http://x\n+upstream: https://x\n")
    out = _fix(eng, project, title="https", diff=diff, workflow_run_id="wfr_d")
    assert out["ok"], out
    assert github.branches[out["branch"]]["tenants/lumen-pay.yaml"] == (
        "tenant: lumen-pay\nupstream: https://x\n")
    bad = ("--- a/tenants/lumen-pay.yaml\n+++ b/tenants/lumen-pay.yaml\n"
           "@@ -1,2 +1,2 @@\n tenant: orbit\n-upstream: http://x\n+upstream: https://x\n")
    refused = _fix(eng, project, title="https", diff=bad, workflow_run_id="wfr_d2")
    assert refused["ok"] is False and refused["code"] == "bad_diff"
    assert "does not apply" in refused["error"]


def test_a_fix_pr_refuses_what_it_must_not_do(eng, project, github):
    assert _fix(eng, project, title="t", workflow_run_id="w")["code"] == "invalid"
    assert _fix(eng, project, title="t", path=".github/workflows/ci.yml", content="x",
                workflow_run_id="w")["code"] == "not_allowed"
    assert _fix(eng, project, title="t", path="../etc/passwd", content="x",
                workflow_run_id="w")["code"] == "invalid"
    assert _fix(eng, project, title="t", path="a", content="x", base_branch="boss/other",
                workflow_run_id="w")["code"] == "invalid"
    assert github.pulls == []


@pytest.mark.parametrize("path", [
    ".github/workflows/ci.yml", "./.github/workflows/x", ".github//workflows/x",
    ".github/./workflows/x", "/.github/workflows/x", ".GitHub/Workflows/x.yml",
    "docs/../.github/workflows/x", ".git/config",
])
def test_ci_workflows_cannot_be_reached_by_any_spelling(eng, project, github, path):
    out = _fix(eng, project, title="t", path=path, content="x", workflow_run_id="w")
    assert out["ok"] is False and out["code"] == "not_allowed", (path, out)
    assert github.pulls == [] and not any(c[0] == "upsert" for c in github.calls)


@pytest.mark.parametrize("path", [
    '".github/workflows/x"', "'.github/workflows/x'", ".github%2Fworkflows/x",
    "%2egithub/workflows/x", ".github\\workflows\\x", "a\tb", "../outside", "..", ".", "/",
])
def test_quoted_encoded_and_escaping_paths_are_refused(eng, project, github, path):
    out = _fix(eng, project, title="t", path=path, content="x", workflow_run_id="w")
    assert out["ok"] is False and out["code"] in ("invalid", "not_allowed"), (path, out)
    assert github.pulls == []


@pytest.mark.parametrize("header", [
    "+++ b/.github/workflows/ci.yml", "+++ /.github/workflows/ci.yml",
    "+++ b//.github/workflows/ci.yml", "+++ b/./.github/workflows/ci.yml",
    '+++ "b/.github/workflows/ci.yml"', "+++ b/.GITHUB/workflows/ci.yml",
])
def test_a_diff_cannot_reach_ci_workflows_either(eng, project, github, header):
    diff = f"--- a/x\n{header}\n@@ -0,0 +1,1 @@\n+evil: true\n"
    out = _fix(eng, project, title="t", diff=diff, workflow_run_id="w")
    assert out["ok"] is False and out["code"] in ("invalid", "not_allowed"), (header, out)
    assert github.pulls == []


def test_a_normalised_path_is_what_gets_written(eng, project, github):
    out = _fix(eng, project, title="t", path="/tenants/./lumen-pay.yaml", content="a: 1\n",
               workflow_run_id="wfr_n")
    assert out["files"] == ["tenants/lumen-pay.yaml"]


def test_the_run_comes_from_the_server_never_the_arguments(eng, project, github):
    """`workflow_run_id` is not an argument: a caller cannot name a run it is not."""
    schema = _tools()["github_open_fix_pr"].parameters
    assert "workflow_run_id" not in schema["properties"]
    assert schema["additionalProperties"] is False
    out = _tools()["github_open_fix_pr"].handler(
        Ctx(eng), project_id=project, actor="a",
        args={"title": "t", "path": "a.yaml", "content": "x", "workflow_run_id": "wfr_forged"})
    assert "wfr_forged" not in out["branch"] and out["branch"].startswith("boss/manual-")


def test_a_direct_fix_pr_waits_for_a_person(eng, project, github):
    """Tier 2 wherever it comes from: from chat, an agent or MCP the model offers it and a
    person presses it; a workflow step is held at the approval floor instead."""
    from boss_engine.tools.builtin import build_default_registry

    reg = build_default_registry()
    spec = reg.get("github_open_fix_pr")
    assert spec.operator_only and not spec.self_executable
    out = reg.invoke("github_open_fix_pr", Ctx(eng), project_id=project, actor="a",
                     args={"title": "t", "path": "a.yaml", "content": "x"})
    assert out["ok"] is False and out["error"] == "operator_confirmation_required"
    assert github.pulls == []


def test_a_fix_pr_without_a_repository_is_a_refusal(eng, project):
    out = _fix(eng, project, title="t", path="a.yaml", content="x")
    assert out["ok"] is False and out["code"] == "no_repository"
    assert outcomes.judge("fix_pr", out, {}).outcome == outcomes.FAILED


def test_without_a_run_the_branch_says_so(eng, project, github):
    out = _fix(eng, project, title="t", path="a.yaml", content="x")
    assert out["branch"].startswith("boss/manual-") and "-fix-t" in out["branch"]


# ── permissions at the registry door ──────────────────────────────────────────────────────

def test_the_tools_sit_behind_their_catalogue_permissions(monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.identity import authz
    from boss_engine.identity.authz import Decision, DenyReason
    from boss_engine.identity.enforcement import LEDGER
    from boss_engine.identity.principal import (
        AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus)
    from boss_engine.tools.registry import ToolRegistry
    from boss_engine.tools.github_tools import register_github_tools

    alice = AuthContext(Principal("usr_a", PrincipalKind.USER, "org_x", PrincipalStatus.ACTIVE,
                                  "A"), AuthMethod.SESSION)
    settings = get_settings()
    monkeypatch.setattr(settings, "authz_mode", "enforce")
    monkeypatch.setattr(settings, "authz_router_modes", "")
    LEDGER.clear()
    reg = ToolRegistry()
    register_github_tools(reg)
    assert reg.get("github_comment").permission == "work_tracker:write"
    assert reg.get("github_open_fix_pr").permission == "code_task:create"
    asked: list = []

    def refuse_all(ctx, permission, scope, **_):
        asked.append(permission)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)

    monkeypatch.setattr(authz, "check", refuse_all)
    comment = reg.invoke("github_comment", None, project_id="p1", actor="usr_a",
                         args={"number": 1, "body": "x"}, auth=alice)
    fix = reg.invoke("github_open_fix_pr", None, project_id="p1", actor="usr_a",
                     args={"title": "t", "path": "a", "content": "b"}, auth=alice,
                     operator_confirmed=True)
    assert comment["ok"] is False and comment["permission"] == "work_tracker:write"
    assert fix["ok"] is False and fix["permission"] == "code_task:create"
    assert asked == ["work_tracker:write", "code_task:create"]
    # Without a verified identity nothing runs either.
    bare = reg.invoke("github_comment", None, project_id="p1", actor="usr_a",
                      args={"number": 1, "body": "x"})
    assert bare["ok"] is False and bare["error"] == "identity_unavailable"
    LEDGER.clear()


def test_the_tools_are_registered_declared_and_pressable_from_chat():
    from boss_engine.kernel.idempotency import declared
    from boss_engine.tools.builtin import _DISCUSS_OPERATIONS_TOOLS

    tools = _tools()
    for name in ("github_comment", "github_open_fix_pr", "contract_test"):
        assert tools[name].kind == "write"
        assert tools[name].exposure is not None and "mcp" in tools[name].exposure.surfaces
        assert name in _DISCUSS_OPERATIONS_TOOLS
    assert declared("github_comment") == "keyed"
    assert declared("github_open_fix_pr") == "idempotent"


# ── as workflow steps ─────────────────────────────────────────────────────────────────────

def test_the_capabilities_are_catalogued_with_their_tools_and_tiers():
    comment, fix, contract = caps.get("github_comment"), caps.get("fix_pr"), caps.get("contract_test")
    assert comment.tool == "github_comment" and comment.risk_tier == 1
    assert "repository" in comment.requires_project
    assert fix.tool == "github_open_fix_pr"
    # At the floor: a person releases a write to the customer's repository unless the project
    # lowers it deliberately.
    assert fix.risk_tier >= caps.MANUAL_APPROVAL_RISK_FLOOR
    assert caps.approval_mode_for("fix_pr", "auto") == "manual"
    assert caps.approval_mode_for("github_comment", "auto") == "auto"
    assert contract.tool == "contract_test" and contract.risk_tier == 0
    keys = {entry["key"] for entry in caps.palette()}
    assert {"github_comment", "fix_pr", "contract_test"} <= keys


def test_the_verdicts_read_the_tools_own_shapes():
    ok_comment = {"ok": True, "posted": True, "comment_url": "https://x/1", "comment_id": 1,
                  "updated": False}
    assert outcomes.judge("github_comment", ok_comment, {}).outcome == outcomes.SUCCEEDED
    assert outcomes.judge("github_comment", {"ok": True, "posted": False}, {}).outcome == outcomes.FAILED
    assert outcomes.judge("fix_pr", {"ok": True, "pull_request_url": "https://x/pull/2"},
                          {}).outcome == outcomes.SUCCEEDED
    assert outcomes.judge("fix_pr", {"ok": True}, {}).outcome == outcomes.FAILED


def test_notify_knows_the_github_channel():
    assert caps.notify_channel_need("github:northgate/gateway#12") == "repository"
    assert caps.notify_channel_need("github:#12") == "repository"
    assert caps.notify_channel_need("telegram:x") == "channel:telegram:x"


def test_a_notify_step_posts_on_the_pull_request(eng, project, github, monkeypatch):
    from boss_engine.services import workflow_capability_runner as runner

    with eng.begin() as c:
        run_id = f"wfr_{uuid.uuid4().hex[:8]}"
        out = runner._notify(
            c, Ctx(eng), project, {"id": "tell"},
            {"run_id": run_id, "workflow_name": "API onboarding",
             "input": {"title": "lumen-pay"}},
            {"channel": "github:northgate/gateway#12", "message": "Validation passed; deployed "
                                                                  "to staging."})
    assert out["status"] == "succeeded" and out["posted"] is True
    assert out["receipt"].startswith("https://github.com/o/r/pull/12#issuecomment-")
    body = github.comments[12][0]["body"]
    assert "API onboarding: Validation passed" in body and ga.marker_line(
        f"notify:{run_id}:tell") in body
    # The second send of the same step updates, never repeats.
    with eng.begin() as c:
        c.execute(text("DELETE FROM events WHERE event_type='workflow.notified' AND entity_id=:r"),
                  {"r": run_id})
        runner._notify(c, Ctx(eng), project, {"id": "tell"},
                       {"run_id": run_id, "workflow_name": "API onboarding", "input": {}},
                       {"channel": "github:northgate/gateway#12", "message": "again"})
    assert len(github.comments[12]) == 1


def test_a_notify_to_a_repository_the_project_lacks_fails_the_step(eng, project):
    from boss_engine.services import workflow_capability_runner as runner

    with eng.begin() as c:
        out = runner._notify(c, Ctx(eng), project, {"id": "tell"},
                             {"run_id": "wfr_none", "workflow_name": "w", "input": {}},
                             {"channel": "github:#3", "message": "x"})
    assert out["status"] == "failed" and out["posted"] is False
    assert "no GitHub repository bound" in out["error"]
