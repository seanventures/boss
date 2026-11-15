"""Two small GitHub effects a workflow step or an agent can perform on a repository bound to the
project: a comment on a pull request or issue, and a fix pull request (AMEX-G5 / G6,
BOSSAPP-1141 / 1142).

WHY THESE EXIST. The API-onboarding workflow validates a tenant's YAML with a CLI and must then
(a) tell the author on their pull request what the validator found and (b) offer the corrected
file as a pull request of its own. BOSS had both halves — `GitHubAdapter.create_issue_comment`,
`ensure_branch`, `upsert_file`, `create_pull_request` — and exposed neither: a PR comment needed
a `github_issues` ticket connection, and a one-file fix needed the whole code-task machinery
(a sandbox, a coding agent, a design pass) to change a YAML file whose content is already known.

WHAT THEY ARE NOT. Not a second coding path: a fix PR carries content the caller already has (a
`--fix` output, a diff). Not a merge: the branch is BOSS's (`boss/<run>-fix-<slug>`, the only
branch shape the egress rules let a run push, identity/egress_rules.py §4) and merging stays a
person's decision. Not a token: the adapter is the project binding's own installation token,
resolved per call by GitHubConnectionService.adapter_for_binding — never a credential a caller
hands in.

EVERY EFFECT IS RECEIPTED (receipts.record_completed) and on the ledger (events), so "did BOSS
already comment on #12" and "which PR did run X open" are questions the database answers.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event

#: Hidden in a comment BOSS may update later. Markdown renders it as nothing; the next run finds
#: it and edits the comment instead of posting a fourth copy of the same report.
MARKER_PREFIX = "<!-- boss:"
MARKER_SUFFIX = " -->"

#: Where a fix branch lives. `boss/<run>-fix-<slug>` matches the one pattern a run token may
#: push to (`refs/heads/boss/<run>-*`, identity/github_app.push_ref_pattern), so the same name
#: works whether the push comes from BOSS's own token or from an agent inside the run.
BRANCH_PREFIX = "boss/"
FIX_INFIX = "-fix-"

#: How much of a validation report the PR body carries verbatim.
REPORT_LIMIT = 20_000


class GitHubActionError(RuntimeError):
    """Something that stops the effect, in a sentence the caller returns as `error`."""

    def __init__(self, message: str, *, code: str = "github_action_failed"):
        super().__init__(message)
        self.code = code


@dataclass
class BoundRepository:
    binding_id: str
    full_name: str
    default_branch: str
    adapter: Any


# ── which repository ─────────────────────────────────────────────────────────────────────────

def _bindings(db: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = db.execute(text(
        "SELECT id, full_name, role, status, default_branch, host, owner, name "
        "FROM github_repository_bindings WHERE project_id = :p AND status = 'active' "
        "ORDER BY (role = 'primary') DESC, updated_at DESC"), {"p": project_id}).mappings().all()
    return [dict(r) for r in rows]


def resolve_binding(db: Connection, project_id: str, repository: Any) -> dict[str, Any]:
    """The project's binding for `repository`: a binding id, an `owner/repo` name, or nothing
    for the project's primary repository. Refuses with the repositories the project HAS."""
    wanted = str(repository or "").strip()
    bindings = _bindings(db, project_id)
    if not bindings:
        raise GitHubActionError(
            "this project has no GitHub repository bound. Connect one under Repositories, "
            "then retry.", code="no_repository")
    if not wanted:
        return bindings[0]
    for b in bindings:
        if wanted == str(b["id"]) or wanted.lower() == str(b["full_name"] or "").lower():
            return b
    have = ", ".join(str(b["full_name"]) for b in bindings[:8])
    raise GitHubActionError(
        f"{wanted!r} is not a repository of this project. This project has: {have}.",
        code="not_this_project")


def adapter_for_repository(db: Connection, settings: Any, project_id: str,
                           repository: Any) -> BoundRepository:
    """The binding's adapter (its installation token, or the local host's). Tests replace this
    function with one that returns a fake adapter."""
    binding = resolve_binding(db, project_id, repository)
    from .github_connections import GitHubConnectionService

    adapter, _conn, bound = GitHubConnectionService(settings).adapter_for_binding(
        db, project_id=project_id, binding_id=str(binding["id"]))
    return BoundRepository(
        binding_id=str(binding["id"]), full_name=str(binding["full_name"]),
        default_branch=str(bound.get("default_branch") or binding.get("default_branch")
                           or "main"),
        adapter=adapter)


def _scope(db: Connection, project_id: str) -> dict[str, str]:
    row = db.execute(text(
        "SELECT w.id AS workspace_id, w.org_id AS org_id FROM projects p "
        "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
        {"p": project_id}).mappings().first()
    if row is None:
        raise GitHubActionError(f"no project {project_id}.", code="no_project")
    return {"org_id": str(row["org_id"]), "workspace_id": str(row["workspace_id"])}


def _receipt(db: Connection, project_id: str, dedupe_key: str, effect_kind: str,
             effect_scope: dict[str, Any], payload: dict[str, Any], provider_ref: str) -> Optional[str]:
    """Write the effect down. A receipt that cannot be written must not undo the effect that
    already happened, so a failure here is logged on the ledger and the result still returns."""
    from .receipts import ReceiptService

    try:
        scope = _scope(db, project_id)
        receipt, _created = ReceiptService().record_completed(
            db, scope["org_id"], scope["workspace_id"], project_id, dedupe_key, effect_kind,
            effect_scope, payload, provider_ref=provider_ref, data_boundary="internal")
        return str(receipt.get("id") or "") or None
    except Exception as error:  # noqa: BLE001 — see the docstring
        emit_event(db, project_id, "github.receipt_failed", "system", "receipt", dedupe_key,
                   {"effect_kind": effect_kind, "error": f"{type(error).__name__}: {error}"[:300]})
        return None


def _scrubbed(value: str, run_id: Optional[str]) -> str:
    from .run_secret_redaction import scrub

    try:
        return str(scrub(value, run_id=run_id or None))
    except Exception:  # noqa: BLE001 — redaction must never block the words
        return value


# ── a comment ────────────────────────────────────────────────────────────────────────────────

def marker_line(marker: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:/-]+", "-", str(marker or "").strip())[:120]
    return f"{MARKER_PREFIX}{safe}{MARKER_SUFFIX}"


def find_marked_comment(comments: Any, marker: str) -> Optional[dict[str, Any]]:
    """The newest existing comment carrying `marker`, or None."""
    needle = marker_line(marker)
    found = [c for c in (comments or []) if isinstance(c, Mapping)
             and needle in str(c.get("body") or "")]
    if not found:
        return None
    found.sort(key=lambda c: str(c.get("updated_at") or c.get("created_at") or ""))
    return dict(found[-1])


def post_comment(db: Connection, settings: Any, *, project_id: str, actor: str, number: Any,
                 body: str, repository: Any = None, marker: Optional[str] = None,
                 run_id: Optional[str] = None,
                 bound: Optional[BoundRepository] = None) -> dict[str, Any]:
    """Post `body` on pull request or issue `number`; with a `marker`, update the comment BOSS
    posted under that marker before instead of adding another."""
    text_body = str(body or "").strip()
    if not text_body:
        raise GitHubActionError("the comment is empty.", code="invalid")
    try:
        issue_number = int(str(number).strip().lstrip("#"))
    except (TypeError, ValueError):
        raise GitHubActionError(f"{number!r} is not a pull request or issue number.",
                                code="invalid") from None
    if issue_number <= 0:
        raise GitHubActionError(f"{number!r} is not a pull request or issue number.",
                                code="invalid")
    bound = bound or adapter_for_repository(db, settings, project_id, repository)
    adapter = bound.adapter
    text_body = _scrubbed(text_body, run_id)
    if marker:
        text_body = f"{text_body}\n\n{marker_line(marker)}"

    existing = None
    if marker and hasattr(adapter, "pull_request_issue_comments"):
        try:
            existing = find_marked_comment(adapter.pull_request_issue_comments(issue_number),
                                           marker)
        except Exception:  # noqa: BLE001 — cannot read: post a new one rather than fail
            existing = None
    try:
        if existing is not None and hasattr(adapter, "update_issue_comment"):
            result = adapter.update_issue_comment(int(existing["id"]), text_body)
            updated = True
        else:
            result = adapter.create_issue_comment(issue_number, text_body)
            updated = False
    except Exception as error:  # noqa: BLE001 — GitHub's answer is the step's result
        raise GitHubActionError(
            f"GitHub refused the comment on {bound.full_name}#{issue_number}: "
            f"{type(error).__name__}: {str(error)[:300]}", code="github_refused") from error

    comment_id = int(getattr(result, "id", 0) or 0)
    comment_url = str(getattr(result, "url", "") or "")
    key_tail = marker or str(comment_id)
    receipt_id = _receipt(
        db, project_id, f"github_comment:{bound.full_name}:{issue_number}:{key_tail}",
        "github.issue_comment",
        {"repository": bound.full_name, "number": issue_number, "marker": marker or None},
        {"comment_id": comment_id, "comment_url": comment_url, "updated": updated,
         "run_id": run_id, "chars": len(text_body)},
        comment_url)
    emit_event(db, project_id, "github.comment_posted", actor or "agent", "pull_request",
               f"{bound.full_name}#{issue_number}",
               {"repository": bound.full_name, "number": issue_number, "comment_id": comment_id,
                "comment_url": comment_url, "updated": updated, "marker": marker or None,
                "run_id": run_id, "receipt_id": receipt_id})
    return {"ok": True, "posted": True, "repository": bound.full_name, "number": issue_number,
            "comment_id": comment_id, "comment_url": comment_url, "updated": updated,
            "receipt_id": receipt_id}


# ── a fix pull request ───────────────────────────────────────────────────────────────────────

def _slug(value: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug[:limit].rstrip("-") or "fix"


def safe_run_id(run_id: Optional[str]) -> str:
    """The run id as it appears in the branch name — the same sanitising the push rule uses
    (identity/github_app.push_ref_pattern), so the two agree character for character."""
    value = str(run_id or "").strip()
    if not value:
        value = "manual-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return value.replace(":", "-").replace("/", "-").replace("#", "-")


def fix_branch_name(run_id: Optional[str], slug: str) -> str:
    return f"{BRANCH_PREFIX}{safe_run_id(run_id)}{FIX_INFIX}{_slug(slug)}"


def _unified_diff_files(diff_text: str) -> dict[str, list[list[str]]]:
    """{path: [hunk lines...]} from a unified diff. A hunk is its lines from `@@` on."""
    files: dict[str, list[list[str]]] = {}
    current: Optional[str] = None
    hunk: Optional[list[str]] = None
    for line in str(diff_text or "").splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            if name.startswith("b/"):
                name = name[2:]
            if "\t" in name:
                name = name.split("\t", 1)[0]
            current = None if name == "/dev/null" else name
            if current is not None:
                files.setdefault(current, [])
            hunk = None
            continue
        if line.startswith("--- ") or line.startswith("diff ") or line.startswith("index "):
            hunk = None
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            hunk = [line]
            files[current].append(hunk)
            continue
        if hunk is not None and (line[:1] in (" ", "+", "-", "\\") or line == ""):
            hunk.append(line if line else " ")
    return files


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_unified_diff(original: str, hunks: list[list[str]]) -> str:
    """Apply one file's hunks to `original`. Strict about context: a hunk whose context does not
    match is refused, because a fix applied in the wrong place is worse than no fix."""
    source = original.splitlines()
    out: list[str] = []
    cursor = 0
    for hunk in hunks:
        header = _HUNK.match(hunk[0])
        if not header:
            raise GitHubActionError(f"malformed hunk header {hunk[0]!r}.", code="bad_diff")
        start = int(header.group(1)) - 1 if int(header.group(1)) > 0 else 0
        if start < cursor:
            raise GitHubActionError("hunks overlap or are out of order.", code="bad_diff")
        out.extend(source[cursor:start])
        cursor = start
        for line in hunk[1:]:
            tag, body = line[:1], line[1:]
            if tag == "\\":
                continue
            if tag in (" ", "-"):
                if cursor >= len(source) or source[cursor] != body:
                    raise GitHubActionError(
                        f"the diff does not apply: expected {body!r} at line {cursor + 1}, "
                        f"found {(source[cursor] if cursor < len(source) else '<end>')!r}.",
                        code="bad_diff")
                if tag == " ":
                    out.append(body)
                cursor += 1
            elif tag == "+":
                out.append(body)
    out.extend(source[cursor:])
    result = "\n".join(out)
    if original.endswith("\n") or not original:
        result += "\n"
    return result


def _files_to_write(files: Any, path: Any, content: Any, diff: Any,
                    read_base: Any) -> list[tuple[str, str]]:
    """[(path, content)] from whichever way the caller gave the change."""
    out: list[tuple[str, str]] = []
    for item in (files or []):
        if not isinstance(item, Mapping):
            raise GitHubActionError("each file is {path, content}.", code="invalid")
        p, c = str(item.get("path") or "").strip(), item.get("content")
        if not p or c is None:
            raise GitHubActionError("each file needs a path and content.", code="invalid")
        out.append((p.lstrip("/"), str(c)))
    if path or content is not None:
        p = str(path or "").strip()
        if not p or content is None:
            raise GitHubActionError("give both path and content.", code="invalid")
        out.append((p.lstrip("/"), str(content)))
    if diff:
        parsed = _unified_diff_files(str(diff))
        if not parsed:
            raise GitHubActionError("the diff names no file (no '+++ b/<path>' line).",
                                    code="bad_diff")
        for p, hunks in parsed.items():
            base = read_base(p)
            if base is None:
                base = ""
            out.append((p, apply_unified_diff(base, hunks) if hunks else base))
    if not out:
        raise GitHubActionError("nothing to change: give files [{path, content}], a path and "
                                "content, or a unified diff.", code="invalid")
    for p, _ in out:
        if p.startswith(".github/workflows/"):
            raise GitHubActionError(f"{p}: a fix PR may not change CI workflows.",
                                    code="not_allowed")
        if ".." in p.split("/"):
            raise GitHubActionError(f"{p}: not a repository path.", code="invalid")
    return out


def pull_request_body(*, title: str, run_id: Optional[str], files: list[str], report: str,
                      summary: str, branch: str, base: str) -> str:
    lines = [summary.strip() or f"BOSS corrected {len(files)} file(s) so the change passes "
                                 "validation.", ""]
    lines.append(f"**Opened by BOSS** from workflow run `{run_id}`." if run_id
                 else "**Opened by BOSS** (no workflow run).")
    lines.append(f"Branch `{branch}` → `{base}`. Merging stays a person's decision.")
    lines.append("")
    lines.append("### Files")
    lines.extend(f"- `{p}`" for p in files)
    if report.strip():
        lines += ["", "### Validation report", "", "```text",
                  report.strip()[:REPORT_LIMIT], "```"]
    return "\n".join(lines)


def open_fix_pr(db: Connection, settings: Any, *, project_id: str, actor: str,
                title: Any = None, repository: Any = None, base_branch: Any = None,
                files: Any = None, path: Any = None, content: Any = None, diff: Any = None,
                report: Any = None, summary: Any = None, slug: Any = None, draft: Any = None,
                run_id: Optional[str] = None,
                bound: Optional[BoundRepository] = None) -> dict[str, Any]:
    """Create `boss/<run>-fix-<slug>` from the base branch, write the file(s), open the PR."""
    bound = bound or adapter_for_repository(db, settings, project_id, repository)
    adapter = bound.adapter
    base = str(base_branch or "").strip() or bound.default_branch
    if base.startswith(BRANCH_PREFIX):
        raise GitHubActionError(f"the base branch {base!r} is one of BOSS's own; a fix PR "
                                "targets the branch the tenant's change is on.", code="invalid")

    def read_base(p: str) -> Optional[str]:
        reader = getattr(adapter, "read_text_file", None)
        return reader(base, p) if reader else None

    changes = _files_to_write(files, path, content, diff, read_base)
    paths = [p for p, _ in changes]
    pr_title = str(title or "").strip() or f"fix: {paths[0]}" + (
        f" (+{len(paths) - 1})" if len(paths) > 1 else "")
    branch = fix_branch_name(run_id, str(slug or "").strip() or pr_title)
    body = pull_request_body(title=pr_title, run_id=run_id, files=paths,
                             report=_scrubbed(str(report or ""), run_id),
                             summary=_scrubbed(str(summary or ""), run_id), branch=branch,
                             base=base)
    commits: list[dict[str, str]] = []
    try:
        adapter.ensure_branch(branch, base)
        for p, c in changes:
            # ONLY WHAT CHANGED. Re-running the step (a retry, the next validation round)
            # finds the file already at this content and writes nothing — the commit list
            # says so, and the PR stays one PR.
            same = read_base_on(adapter, branch, p) == c
            if same:
                commits.append({"path": p, "sha": "", "note": "unchanged"})
                continue
            sha = adapter.upsert_file(branch, p, c, f"fix: {p}\n\n{pr_title}")
            commits.append({"path": p, "sha": str(sha or "")})
        pr = adapter.create_pull_request(branch, pr_title, body, draft=draft, base=base)
    except GitHubActionError:
        raise
    except Exception as error:  # noqa: BLE001 — GitHub's answer is the step's result
        raise GitHubActionError(
            f"GitHub refused the fix PR on {bound.full_name} ({branch}): "
            f"{type(error).__name__}: {str(error)[:300]}", code="github_refused") from error

    pr_url = str(getattr(pr, "url", "") or "")
    pr_number = int(getattr(pr, "number", 0) or 0)
    receipt_id = _receipt(
        db, project_id, f"github_fix_pr:{bound.full_name}:{branch}", "github.fix_pull_request",
        {"repository": bound.full_name, "branch": branch, "base": base},
        {"pull_request_url": pr_url, "pull_request_number": pr_number, "files": paths,
         "commits": commits, "run_id": run_id, "title": pr_title},
        pr_url)
    emit_event(db, project_id, "github.fix_pr_opened", actor or "agent", "pull_request",
               f"{bound.full_name}#{pr_number}",
               {"repository": bound.full_name, "branch": branch, "base": base,
                "pull_request_url": pr_url, "pull_request_number": pr_number,
                "files": paths, "run_id": run_id, "receipt_id": receipt_id})
    return {"ok": True, "repository": bound.full_name, "branch": branch, "base_branch": base,
            "pull_request_url": pr_url, "pull_request_number": pr_number, "title": pr_title,
            "files": paths, "commits": commits, "receipt_id": receipt_id}


def read_base_on(adapter: Any, branch: str, path: str) -> Optional[str]:
    reader = getattr(adapter, "read_text_file", None)
    if reader is None:
        return None
    try:
        return reader(branch, path)
    except Exception:  # noqa: BLE001 — unreadable means "write it"
        return None


def unified_diff(path: str, before: str, after: str) -> str:
    """A diff of one file, for callers that want to show what the fix PR will change."""
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))


__all__ = [
    "BRANCH_PREFIX", "BoundRepository", "FIX_INFIX", "GitHubActionError", "MARKER_PREFIX",
    "adapter_for_repository", "apply_unified_diff", "find_marked_comment", "fix_branch_name",
    "marker_line", "open_fix_pr", "post_comment", "pull_request_body", "resolve_binding",
    "safe_run_id", "unified_diff",
]
