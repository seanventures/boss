"""GitHub effects as tools (AMEX-G5 / G6, BOSSAPP-1141 / 1142): a comment on a pull request or
issue, and a lightweight fix pull request — on a repository bound to the project, through the
binding's own adapter (services/github_actions.py). Never a token in an argument.

  github_comment        write  work_tracker:write   comment on PR/issue; update by marker
  github_open_fix_pr    write  code_task:create     branch boss/<run>-fix-<slug> + file(s) + PR

Both are workflow capabilities too (`github_comment`, `fix_pr` in workflow_capabilities.py): the
runner dispatches the step to the tool here, so a step and an agent's tool call are the same
code under the same permission. The run that names the branch and the PR body is the one the
runner set (github_actions.WORKFLOW_RUN_ID) — never an argument, so no caller can claim a run it
is not; a chat or MCP call has none and the branch says so (`manual-<time>`).

A FIX PR IS TIER 2 WHEREVER IT COMES FROM. As a workflow step it waits at the manual-approval
floor; called directly (chat, an agent, MCP) it is `operator_only`: the model offers it and a
person presses it (registry.invoke refuses an unconfirmed call).
"""
from __future__ import annotations

from typing import Any

from .registry import Exposure, ToolSpec

WRITE_TOOLS = ("github_comment", "github_open_fix_pr")

_EXPOSURE = Exposure(tags=("github", "pull_request"), surfaces=("chat", "agent_step", "mcp"),
                     search_hint="github pull request pr issue comment fix branch open")

_REPOSITORY = {
    "type": "string",
    "description": "owner/repo, or a binding id; leave out for the project's primary repository",
}


def _schema(properties: dict[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
    return {"type": "object",
            "properties": {**properties, "repository": _REPOSITORY,
                           "project_id": {"type": "string"}},
            "required": list(required), "additionalProperties": False}


_COMMENT = _schema({
    "number": {"type": "integer", "minimum": 1,
               "description": "the pull request or issue number"},
    "body": {"type": "string", "maxLength": 60000, "description": "the comment, in markdown"},
    "marker": {"type": "string", "maxLength": 120,
               "description": "a stable key (e.g. validation-report): the comment BOSS posted "
                              "under it before is updated instead of adding another"},
}, ("number", "body"))

_FIX_PR = _schema({
    "title": {"type": "string", "maxLength": 200},
    "base_branch": {"type": "string",
                    "description": "the branch the fix targets; default the repository's "
                                   "default branch"},
    "files": {"type": "array", "maxItems": 50,
              "items": {"type": "object",
                        "properties": {"path": {"type": "string"},
                                       "content": {"type": "string"}},
                        "required": ["path", "content"], "additionalProperties": False},
              "description": "files to write at their new content"},
    "path": {"type": "string", "description": "one file to write (with content)"},
    "content": {"type": "string", "description": "that file's new content"},
    "diff": {"type": "string", "maxLength": 200000,
             "description": "a unified diff against base_branch, instead of files"},
    "report": {"type": "string", "maxLength": 60000,
               "description": "the validation report the PR body carries"},
    "summary": {"type": "string", "maxLength": 4000,
                "description": "one paragraph for the PR body"},
    "slug": {"type": "string", "maxLength": 40, "description": "the branch's suffix"},
    "draft": {"type": "boolean"},
}, ())


def _refusal(error: BaseException) -> dict[str, Any]:
    from ..services.github_actions import GitHubActionError

    code = getattr(error, "code", None) if isinstance(error, GitHubActionError) else None
    return {"ok": False, "error": str(error)[:600], "code": code or "github_action_failed"}


def _comment(ctx, *, project_id, actor, args):
    from ..services.github_actions import WORKFLOW_RUN_ID, GitHubActionError, post_comment

    args = dict(args or {})
    pid = args.get("project_id") or project_id
    try:
        with ctx.begin() as db:
            return post_comment(
                db, getattr(ctx, "settings", None), project_id=pid, actor=actor,
                number=args.get("number"), body=str(args.get("body") or ""),
                repository=args.get("repository"), marker=args.get("marker") or None,
                run_id=WORKFLOW_RUN_ID.get() or None)
    except GitHubActionError as error:
        return _refusal(error)


def _fix_pr(ctx, *, project_id, actor, args):
    from ..services.github_actions import WORKFLOW_RUN_ID, GitHubActionError, open_fix_pr

    args = dict(args or {})
    pid = args.get("project_id") or project_id
    try:
        with ctx.begin() as db:
            return open_fix_pr(
                db, getattr(ctx, "settings", None), project_id=pid, actor=actor,
                title=args.get("title"), repository=args.get("repository"),
                base_branch=args.get("base_branch"), files=args.get("files"),
                path=args.get("path"), content=args.get("content"), diff=args.get("diff"),
                report=args.get("report"), summary=args.get("summary"), slug=args.get("slug"),
                draft=args.get("draft"), run_id=WORKFLOW_RUN_ID.get() or None)
    except GitHubActionError as error:
        return _refusal(error)


def register_github_tools(reg) -> None:
    reg.register(ToolSpec(
        name="github_comment",
        description="Post a markdown comment on a pull request or issue of a repository bound "
                    "to the project. With a marker, the comment BOSS posted under that marker "
                    "before is updated instead of posting another — one validation report per "
                    "PR, kept current. Receipted.",
        parameters=_COMMENT, handler=_comment, kind="write", reversibility="safe",
        permission="work_tracker:write", idempotency="keyed", exposure=_EXPOSURE))
    reg.register(ToolSpec(
        name="github_open_fix_pr",
        description="Open a small fix pull request on a repository bound to the project: "
                    "branch boss/<run>-fix-<slug> from the base branch, the file(s) at their "
                    "corrected content (or a unified diff applied), and a PR whose body names "
                    "the run and carries the validation report. No build, no coding agent; "
                    "merging stays a person's decision. Receipted.",
        parameters=_FIX_PR, handler=_fix_pr, kind="write", reversibility="safe",
        permission="code_task:create", idempotency="idempotent", operator_only=True,
        offer_note="It opens a pull request on the customer's repository; merging stays a "
                   "person's decision.",
        exposure=_EXPOSURE))
