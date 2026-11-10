"""LOCAL REVIEW (BOSSAPP-948): review a local change set the way a person reviews a pull request —
the whole file at the change or at its base, comments on a line, Request changes, and a fix
round whose required inputs are those comments. Design: docs/product/LOCAL_GIT_REMOTE_AND_REVIEW.md.

ONE STORE, ONE PATH. Every comment — the console's diff and Code tab, `boss review` on a
terminal, git notes pushed to refs/notes/boss-review, MCP `add_review_comment` on a local change,
and the agent's own replies in a fix round — is written by `add_comment` here into
`local_change_set_comments`. Nothing else inserts into that table.

    a thread      its root (reply_to NULL) and the replies under it
    its state     open → addressed | disputed (the agent's reply) → resolved (a person)
    its anchor    (commit, path, side, line); when the head moves, `remap` follows the line
                  through the diff, or marks the thread `outdated` when that line changed
    merge         held while any thread a PERSON started is not resolved (`merge_hold`, read by
                  ci_gate.refuse_approval and ci_gate.approval_hold — the card and the gate agree)

The fix round: `request_changes` sends the review decision back (decisions → revise) and
`start_fix_round` starts a build on the change's own branch whose brief carries the open human
comments first, numbered as `review_comments.render_for_implementer` numbers them, and whose
`finalize_ctx.review_comments` holds them (ids `lcc_<id>`). The build's RESPONSE lines
(review_comments.parse_responses) come back through `sync_agent_replies` as the agent's reply on
each thread ("Fixed in commit abc1234: …").
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

from sqlalchemy import text

from . import local_repo, review_comments
from .core import emit_event

log = logging.getLogger(__name__)

#: A file larger than this is not sent to the browser (the Code tab says so).
FILE_CAP_BYTES = 1_000_000
SEVERITIES = ("blocker", "major", "minor", "nit")
#: The console's three words (wireframe: Must fix · Should fix · Nit) on the one severity ladder
#: (review_comments.PRIORITY: major → P1, minor → P2, nit → P3).
WORD_SEVERITY = {"must": "major", "must fix": "major", "must_fix": "major", "p1": "major",
                 "should": "minor", "should fix": "minor", "should_fix": "minor", "p2": "minor",
                 "nit": "nit", "p3": "nit", "blocker": "blocker", "major": "major",
                 "minor": "minor"}
SEVERITY_WORDS = {"blocker": "must fix", "major": "must fix", "minor": "should fix", "nit": "nit"}
ORIGINS = ("console", "cli", "git_notes", "mcp", "agent")
#: The ids a fix round's finalize_ctx carries for this store's comments.
REF_PREFIX = "lcc_"
NOTES_REF = "refs/notes/boss-review"

_LANG = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
         ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx", ".json": "json", ".md": "markdown",
         ".css": "css", ".scss": "scss", ".html": "html", ".htm": "html", ".yml": "yaml",
         ".yaml": "yaml", ".toml": "toml", ".sh": "bash", ".go": "go", ".rs": "rust",
         ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php", ".sql": "sql",
         ".c": "c", ".h": "c", ".cpp": "cpp", ".cs": "csharp", ".swift": "swift",
         ".vue": "vue", ".svelte": "svelte", ".xml": "xml", ".txt": "text"}
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class ReviewError(ValueError):
    """A refusal in words, with the HTTP status a route answers with."""

    def __init__(self, message: str, status: int = 422, error: str = "review_refused"):
        super().__init__(message)
        self.status = status
        self.error = error

    def body(self) -> dict[str, Any]:
        return {"error": self.error, "message": str(self)}


# ── the change set and its repository ─────────────────────────────────────────────────────────

def change_row(c: Any, project_id: str, number: int) -> dict[str, Any]:
    row = c.execute(text(
        "SELECT * FROM local_change_sets WHERE project_id = :p AND number = :n "
        "ORDER BY (state = 'open') DESC, created_at DESC LIMIT 1"),
        {"p": project_id, "n": int(number)}).mappings().first()
    if row is None:
        raise ReviewError(f"There is no change #{number}.", 404, "no_change")
    return dict(row)


def change_by_id(c: Any, change_set_id: str) -> Optional[dict[str, Any]]:
    row = c.execute(text("SELECT * FROM local_change_sets WHERE id = :i"),
                    {"i": change_set_id}).mappings().first()
    return dict(row) if row else None


def git_dir_of(row: Mapping[str, Any], settings: Any = None) -> Path:
    owner, _, name = str(row["repository"]).partition("/")
    return local_repo.bare_path(owner, name, settings)


def head_sha(row: Mapping[str, Any], git_dir: Path) -> str:
    return (local_repo.branch_sha(git_dir, row["branch"]) or row.get("head_sha") or "")


def base_sha(row: Mapping[str, Any], git_dir: Path) -> str:
    """Where the change starts: the merge base with its base branch while open (what the diff is
    against), the recorded base once merged or closed."""
    head = head_sha(row, git_dir)
    if row.get("state") == "open":
        tip = local_repo.branch_sha(git_dir, row["base_branch"])
        if tip and head:
            return local_repo.merge_base(git_dir, tip, head) or tip
        if tip:
            return tip
    return row.get("base_sha") or local_repo.branch_sha(git_dir, row["base_branch"]) or ""


def resolve_ref(row: Mapping[str, Any], git_dir: Path, ref: Optional[str]) -> str:
    """The commit a file view reads: `head` (default), `base`, or a commit of this change's own
    branch. Anything else — another branch, a tag, an unknown sha — is refused."""
    want = str(ref or "head").strip()
    if want in ("head", ""):
        sha = head_sha(row, git_dir)
    elif want == "base":
        sha = base_sha(row, git_dir)
    elif _SHA.match(want):
        sha = local_repo.rev_parse(git_dir, want) or ""
        head = head_sha(row, git_dir)
        base = base_sha(row, git_dir)
        # A commit OF THIS CHANGE: reachable from its head and not from before its base.
        on_branch = bool(sha) and bool(head) and _is_ancestor(git_dir, sha, head) and (
            not base or _is_ancestor(git_dir, base, sha))
        if not on_branch and sha != base:
            sha = ""
    else:
        sha = ""
    if not sha:
        raise ReviewError(f"{want!r} is not this change's head, its base, or a commit on its "
                          "branch.", 422, "unknown_ref")
    return sha


def _is_ancestor(git_dir: Path, a: str, b: str) -> bool:
    import subprocess

    done = subprocess.run(["git", "--git-dir", str(git_dir), "merge-base", "--is-ancestor", a, b],
                          capture_output=True, env=local_repo._env(), timeout=60)  # noqa: SLF001
    return done.returncode == 0


def clean_path(path: Any) -> str:
    """A path inside the repository: relative, no `..`, no NUL. Refused in words otherwise."""
    raw = str(path or "").strip()
    if not raw or "\x00" in raw or raw.startswith("/") or "\\" in raw:
        raise ReviewError("Give a file path inside the repository.", 422, "bad_path")
    parts = PurePosixPath(raw).parts
    if any(p in ("..", ".") for p in parts) or raw.startswith(".git/") or raw == ".git":
        raise ReviewError("A file path cannot leave the repository.", 422, "bad_path")
    return str(PurePosixPath(*parts))


def language_of(path: str) -> str:
    name = PurePosixPath(path).name.lower()
    if name == "dockerfile":
        return "dockerfile"
    if name == "makefile":
        return "makefile"
    return _LANG.get(PurePosixPath(name).suffix, "text")


def _cat(git_dir: Path, sha: str, path: str) -> Optional[bytes]:
    import subprocess

    done = subprocess.run(["git", "--git-dir", str(git_dir), "cat-file", "blob", f"{sha}:{path}"],
                          capture_output=True, env=local_repo._env(), timeout=60)  # noqa: SLF001
    return done.stdout if done.returncode == 0 else None


def _size(git_dir: Path, sha: str, path: str) -> Optional[int]:
    out = local_repo.git(["cat-file", "-s", f"{sha}:{path}"], git_dir=git_dir, check=False).strip()
    return int(out) if out.isdigit() else None


def _line_of(git_dir: Path, sha: str, path: str, line: Optional[int]) -> Optional[str]:
    if not line or not sha:
        return None
    data = _cat(git_dir, sha, path)
    if data is None:
        return None
    lines = data.decode("utf-8", "replace").split("\n")
    return lines[line - 1][:500] if 1 <= line <= len(lines) else None


# ── the file view (the Code tab) ──────────────────────────────────────────────────────────────

def tree(c: Any, settings: Any, project_id: str, number: int, ref: Optional[str]) -> dict[str, Any]:
    """Every path at the ref, the change's own status (M/A/D) beside the paths it touches, and
    how many threads each file has."""
    row = change_row(c, project_id, number)
    git_dir = git_dir_of(row, settings)
    sha = resolve_ref(row, git_dir, ref)
    paths = local_repo.list_paths(git_dir, sha)
    head, base = head_sha(row, git_dir), base_sha(row, git_dir)
    status: dict[str, str] = {}
    if head and base:
        for line in local_repo.git(["diff", "--name-status", "--no-renames", base, head],
                                   git_dir=git_dir).splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                status[parts[-1]] = parts[0][:1]
    counts: dict[str, dict[str, int]] = {}
    for r in c.execute(text(
            "SELECT path, state FROM local_change_set_comments WHERE change_set_id = :cs "
            "AND reply_to IS NULL AND path IS NOT NULL"), {"cs": row["id"]}).mappings():
        slot = counts.setdefault(r["path"], {"threads": 0, "open": 0})
        slot["threads"] += 1
        slot["open"] += 0 if r["state"] == "resolved" else 1
    present = set(paths)
    items = [{"path": p, "status": status.get(p), "comments": counts.get(p, {}).get("threads", 0),
              "open_comments": counts.get(p, {}).get("open", 0)} for p in paths]
    # A deleted file is not in the head's tree; the tree still lists it, so its threads show.
    for p, s in sorted(status.items()):
        if s == "D" and p not in present:
            items.append({"path": p, "status": "D", "comments": counts.get(p, {}).get("threads", 0),
                          "open_comments": counts.get(p, {}).get("open", 0)})
    return {"ref": str(ref or "head"), "sha": sha, "head_sha": head, "base_sha": base,
            "items": items}


def read_file(c: Any, settings: Any, project_id: str, number: int, path: str,
              ref: Optional[str]) -> dict[str, Any]:
    row = change_row(c, project_id, number)
    git_dir = git_dir_of(row, settings)
    sha = resolve_ref(row, git_dir, ref)
    rel = clean_path(path)
    size = _size(git_dir, sha, rel)
    out: dict[str, Any] = {"path": rel, "ref": str(ref or "head"), "sha": sha,
                           "language": language_of(rel), "size": size}
    if size is None:
        raise ReviewError(f"{rel} is not in this change at {sha[:7]}.", 404, "no_file")
    if size > FILE_CAP_BYTES:
        return {**out, "too_large": True, "binary": False, "text": None,
                "message": f"{rel} is {size // 1024} KB; files over 1 MB are not shown here."}
    data = _cat(git_dir, sha, rel) or b""
    if b"\x00" in data[:8000]:
        return {**out, "binary": True, "too_large": False, "text": None,
                "message": f"{rel} is a binary file."}
    body = data.decode("utf-8", "replace")
    out.update({"binary": False, "too_large": False, "text": body,
                "lines": body.count("\n") + (0 if body.endswith("\n") or not body else 1)})
    return out


# ── where it lives ────────────────────────────────────────────────────────────────────────────

def remote_path(project_id: str, repository: str, primary: bool = True) -> str:
    if primary:
        return f"/git/{project_id}.git"
    return f"/git/{project_id}/{repository.split('/', 1)[-1]}.git"


def is_primary(c: Any, project_id: str, repository: str) -> bool:
    role = c.execute(text(
        "SELECT role FROM github_repository_bindings WHERE project_id = :p AND full_name = :f "
        "AND host = 'local' AND status = 'active'"), {"p": project_id, "f": repository}).scalar()
    return role in (None, "primary")


def where(c: Any, settings: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    """The "where it lives" strip: the bare repository, the branch and its head, the clone URL,
    and the working copies `boss clone` recorded."""
    git_dir = git_dir_of(row, settings)
    head = head_sha(row, git_dir)
    ahead = None
    if head and row.get("state") == "open":
        tip = local_repo.branch_sha(git_dir, row["base_branch"])
        if tip:
            out = local_repo.git(["rev-list", "--count", f"{tip}..{head}"], git_dir=git_dir,
                                 check=False).strip()
            ahead = int(out) if out.isdigit() else None
    path = remote_path(row["project_id"], row["repository"],
                       is_primary(c, row["project_id"], row["repository"]))
    origin = str(getattr(settings, "public_url", "") or "").rstrip("/") or "http://localhost:3000"
    copies = c.execute(text(
        "SELECT path, branch, machine, recorded_by, updated_at FROM local_working_copies "
        "WHERE project_id = :p AND repository = :r ORDER BY updated_at DESC LIMIT 10"),
        {"p": row["project_id"], "r": row["repository"]}).mappings().all()
    return {"storage_path": str(git_dir), "volume": "boss_community_repos",
            "branch": row["branch"], "head_sha": head, "ahead": ahead,
            "remote_path": path, "remote_url": f"{origin}{path}",
            "reach": str(getattr(settings, "git_remote_reach", "local") or "local"),
            "working_copies": [{"path": w["path"], "branch": w["branch"], "machine": w["machine"],
                                "recorded_by": w["recorded_by"],
                                "updated_at": w["updated_at"].isoformat()
                                if hasattr(w["updated_at"], "isoformat") else w["updated_at"]}
                               for w in copies]}


def record_working_copy(c: Any, project_id: str, *, repository: str, path: str,
                        branch: Optional[str], machine: str, actor: str) -> dict[str, Any]:
    from .core import make_id

    raw = str(path or "").strip()
    if not raw or "\x00" in raw or len(raw) > 500:
        raise ReviewError("Give the folder the working copy is in.")
    c.execute(text(
        "INSERT INTO local_working_copies (id, project_id, repository, path, branch, machine, "
        "recorded_by) VALUES (:id, :p, :r, :path, :b, :m, :a) "
        "ON CONFLICT (project_id, repository, machine, path) DO UPDATE SET branch = EXCLUDED.branch, "
        "recorded_by = EXCLUDED.recorded_by, updated_at = now()"),
        {"id": make_id("lwc"), "p": project_id, "r": repository, "path": raw,
         "b": (branch or None), "m": str(machine or "")[:120], "a": actor})
    emit_event(c, project_id, "local_repo.working_copy_recorded", actor, "project", project_id,
               {"repository": repository, "path": raw, "branch": branch})
    return {"repository": repository, "path": raw, "branch": branch, "machine": machine}


# ── comments: the one writer ──────────────────────────────────────────────────────────────────

def severity_of(value: Any) -> str:
    key = str(value or "").strip().lower()
    if not key:
        return "major"
    if key not in WORD_SEVERITY:
        raise ReviewError("Say how much it blocks: must fix, should fix or nit.", 422,
                          "bad_severity")
    return WORD_SEVERITY[key]


def _root_of(c: Any, change_set_id: str, comment_id: int) -> dict[str, Any]:
    row = c.execute(text("SELECT * FROM local_change_set_comments WHERE id = :i AND "
                         "change_set_id = :cs"), {"i": int(comment_id), "cs": change_set_id}
                    ).mappings().first()
    if row is None:
        raise ReviewError(f"There is no comment {comment_id} on this change.", 404, "no_comment")
    row = dict(row)
    if row.get("reply_to"):
        return _root_of(c, change_set_id, int(row["reply_to"]))
    return row


def add_comment(c: Any, settings: Any, *, project_id: str, number: int, body: Any, author: str,
                author_kind: str = "human", path: Any = None, line: Any = None,
                end_line: Any = None, side: str = "new", commit_sha: Optional[str] = None,
                severity: Any = None, origin: str = "console", reply_to: Optional[int] = None,
                run_round: Optional[str] = None, response_commit: Optional[str] = None,
                state: Optional[str] = None, emit: bool = True) -> dict[str, Any]:
    """THE ONE WRITER of local_change_set_comments. A root comment is anchored to a line of a
    commit (the change's head by default; `side=old` reads the base); a reply inherits its
    thread's anchor. Refused, in words, for an unknown file or line."""
    row = change_row(c, project_id, number)
    words = str(body or "").strip()
    if not words:
        raise ReviewError("Write the comment first.", 422, "empty_comment")
    if len(words) > 8000:
        raise ReviewError("A comment is at most 8,000 characters.", 422, "comment_too_long")
    if author_kind not in ("human", "agent"):
        raise ReviewError("author_kind is human or agent.")
    if origin not in ORIGINS:
        raise ReviewError(f"origin is one of {', '.join(ORIGINS)}.")
    side = "old" if str(side or "new").lower() in ("old", "left", "base") else "new"
    git_dir = git_dir_of(row, settings)
    sev = severity_of(severity)
    if reply_to is not None:
        root = _root_of(c, row["id"], int(reply_to))
        values = {"path": root["path"], "line": root["line"], "end_line": root["end_line"],
                  "side": root["side"], "commit_sha": root["commit_sha"],
                  "severity": root["severity"], "reply_to": int(root["id"]),
                  "line_text": None, "original_line": None}
    else:
        rel = clean_path(path) if path not in (None, "") else None
        ln = int(line) if line not in (None, "") else None
        end = int(end_line) if end_line not in (None, "") else None
        if rel is None and ln is not None:
            raise ReviewError("A line needs its file.", 422, "bad_anchor")
        if ln is not None and ln < 1:
            raise ReviewError("Lines start at 1.", 422, "bad_anchor")
        if end is not None and ln is not None and end < ln:
            end = None
        sha = ""
        if rel is not None:
            sha = (local_repo.rev_parse(git_dir, commit_sha) if commit_sha else None) or (
                base_sha(row, git_dir) if side == "old" else head_sha(row, git_dir))
            size = _size(git_dir, sha, rel) if sha else None
            if size is None:
                raise ReviewError(f"{rel} is not in change #{number}"
                                  + (f" at {sha[:7]}" if sha else "") + ".", 422, "no_file")
            if ln is not None:
                text_line = _line_of(git_dir, sha, rel, ln)
                if text_line is None:
                    raise ReviewError(f"{rel} has no line {ln}"
                                      + (f" at {sha[:7]}" if sha else "") + ".", 422, "no_line")
        values = {"path": rel, "line": ln, "end_line": end, "side": side,
                  "commit_sha": sha or head_sha(row, git_dir) or None, "severity": sev,
                  "reply_to": None,
                  "line_text": _line_of(git_dir, sha, rel, ln) if rel and ln else None,
                  "original_line": ln}
    new_id = int(c.execute(text(
        "INSERT INTO local_change_set_comments (change_set_id, project_id, path, line, end_line, "
        "side, commit_sha, author, author_kind, severity, body, state, reply_to, origin, "
        "original_line, line_text, run_round, response_commit) VALUES (:cs, :p, :path, :line, "
        ":end, :side, :sha, :author, :kind, :sev, :body, :state, :reply, :origin, :orig, :lt, "
        ":round, :rc) RETURNING id"),
        {"cs": row["id"], "p": project_id, "path": values["path"], "line": values["line"],
         "end": values["end_line"], "side": values["side"], "sha": values["commit_sha"],
         "author": str(author or "someone")[:200], "kind": author_kind, "sev": values["severity"],
         "body": words, "state": state or "open", "reply": values["reply_to"], "origin": origin,
         "orig": values["original_line"], "lt": values["line_text"], "round": run_round,
         "rc": response_commit}).scalar())
    if emit:
        emit_event(c, project_id, "local_review.comment_added", author, "local_change_set",
                   row["id"], {"number": int(row["number"]), "comment_id": new_id,
                               "reply_to": values["reply_to"], "origin": origin,
                               "author_kind": author_kind, "path": values["path"],
                               "line": values["line"]})
    return comment(c, row["id"], new_id)


def comment(c: Any, change_set_id: str, comment_id: int) -> dict[str, Any]:
    r = c.execute(text("SELECT * FROM local_change_set_comments WHERE id = :i AND "
                       "change_set_id = :cs"), {"i": int(comment_id), "cs": change_set_id}
                  ).mappings().first()
    if r is None:
        raise ReviewError(f"There is no comment {comment_id} on this change.", 404, "no_comment")
    return _view(dict(r))


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _view(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": int(r["id"]), "path": r["path"], "line": r["line"], "end_line": r["end_line"],
            "side": r["side"], "commit_sha": r["commit_sha"], "author": r["author"],
            "author_kind": r["author_kind"], "severity": r["severity"],
            "severity_words": SEVERITY_WORDS.get(r["severity"], r["severity"]),
            "body": r["body"], "state": r["state"], "reply_to": r["reply_to"],
            "origin": r["origin"], "outdated": bool(r["outdated"]),
            "original_line": r["original_line"], "line_text": r["line_text"],
            "run_round": r["run_round"], "response_commit": r["response_commit"],
            "github_comment_id": r["github_comment_id"], "resolved_by": r["resolved_by"],
            "resolved_at": _iso(r["resolved_at"]), "created_at": _iso(r["created_at"]),
            "updated_at": _iso(r["updated_at"])}


def threads(c: Any, change_set_id: str) -> list[dict[str, Any]]:
    """Every thread on the change: its root with `replies`, people's open threads first."""
    rows = [_view(dict(r)) for r in c.execute(text(
        "SELECT * FROM local_change_set_comments WHERE change_set_id = :cs ORDER BY id"),
        {"cs": change_set_id}).mappings()]
    roots = {r["id"]: {**r, "replies": []} for r in rows if not r["reply_to"]}
    for r in rows:
        if r["reply_to"] and r["reply_to"] in roots:
            roots[r["reply_to"]]["replies"].append(r)
    order = {"open": 0, "disputed": 1, "addressed": 2, "resolved": 3}
    return sorted(roots.values(), key=lambda t: (t["state"] == "resolved",
                                                 t["author_kind"] != "human",
                                                 order.get(t["state"], 9), t["id"]))


def summary(c: Any, change_set_id: str) -> dict[str, int]:
    row = c.execute(text(
        "SELECT count(*) FILTER (WHERE reply_to IS NULL) AS threads, "
        "count(*) FILTER (WHERE reply_to IS NULL AND state <> 'resolved') AS unresolved, "
        "count(*) FILTER (WHERE reply_to IS NULL AND state <> 'resolved' AND author_kind = 'human') "
        "AS human_open, "
        "count(*) FILTER (WHERE reply_to IS NULL AND state = 'open' AND author_kind = 'human') "
        "AS human_unreplied "
        "FROM local_change_set_comments WHERE change_set_id = :cs"),
        {"cs": change_set_id}).mappings().first() or {}
    return {k: int(row.get(k) or 0) for k in ("threads", "unresolved", "human_open",
                                               "human_unreplied")}


def resolve(c: Any, *, project_id: str, number: int, comment_id: int, actor: str,
            reopen: bool = False) -> dict[str, Any]:
    """A PERSON resolves a thread (or reopens it). The route checks it is a person."""
    row = change_row(c, project_id, number)
    root = _root_of(c, row["id"], int(comment_id))
    if reopen:
        c.execute(text("UPDATE local_change_set_comments SET state = 'open', resolved_by = NULL, "
                       "resolved_at = NULL, updated_at = now() WHERE id = :i"), {"i": root["id"]})
    else:
        c.execute(text("UPDATE local_change_set_comments SET state = 'resolved', resolved_by = :a, "
                       "resolved_at = now(), updated_at = now() WHERE id = :i"),
                  {"i": root["id"], "a": actor})
    emit_event(c, project_id, "local_review.thread_reopened" if reopen else
               "local_review.thread_resolved", actor, "local_change_set", row["id"],
               {"number": int(row["number"]), "comment_id": int(root["id"])})
    return comment(c, row["id"], int(root["id"]))


# ── the merge hold ────────────────────────────────────────────────────────────────────────────

def change_for_url(c: Any, url: Any) -> Optional[dict[str, Any]]:
    from .local_repo_host import is_local_pr_url

    if not is_local_pr_url(url):
        return None
    m = re.search(r"://[^/]+/([^/]+/[^/]+)/pull/(\d+)", str(url))
    if not m:
        return None
    row = c.execute(text("SELECT * FROM local_change_sets WHERE repository = :r AND number = :n"),
                    {"r": m.group(1), "n": int(m.group(2))}).mappings().first()
    return dict(row) if row else None


def merge_hold(c: Any, change_set_id: str) -> Optional[dict[str, Any]]:
    """Why Merge waits on the review conversation, or None. A person's unresolved thread holds it;
    a thread the agent has not answered yet is said first."""
    s = summary(c, change_set_id)
    if not s["human_open"]:
        return None
    n = s["human_open"]
    if s["human_unreplied"]:
        u = s["human_unreplied"]
        reason = (f"{u} comment{'s' if u != 1 else ''} from people {'have' if u != 1 else 'has'} "
                  "no reply yet: request changes so the agent answers, or resolve "
                  f"{'them' if u != 1 else 'it'}.")
    else:
        reason = (f"{n} open comment{'s' if n != 1 else ''} from people: resolve "
                  f"{'them' if n != 1 else 'it'} or request changes first.")
    return {"state": "comments_open", "label": f"{n} open comment{'s' if n != 1 else ''}",
            "reason": reason, "open_comments": n}


def merge_hold_for_url(c: Any, url: Any) -> Optional[dict[str, Any]]:
    row = change_for_url(c, url)
    if row is None or row.get("state") != "open":
        return None
    return merge_hold(c, row["id"])


# ── re-anchoring when the head moves ──────────────────────────────────────────────────────────

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def map_line(git_dir: Path, old: str, new: str, path: str, line: int) -> Optional[int]:
    """Where line `line` of `path` at `old` is at `new`, or None when that line itself changed
    (or the file is gone). Read from a zero-context diff: hunks wholly before the line shift it."""
    if old == new:
        return line
    if _size(git_dir, new, path) is None:
        return None
    out = local_repo.git(["diff", "--no-color", "-U0", "--no-renames", old, new, "--", path],
                         git_dir=git_dir, check=False)
    shift = 0
    for raw in out.splitlines():
        m = _HUNK.match(raw)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2)) if m.group(2) is not None else 1
        d = int(m.group(4)) if m.group(4) is not None else 1
        if b == 0:
            # Pure insertion after old line `a`.
            if a < line:
                shift += d
            continue
        if a + b - 1 < line:
            shift += d - b
        elif a <= line <= a + b - 1:
            return None
    return line + shift


def remap(c: Any, settings: Any, row: Mapping[str, Any], new_head: Optional[str] = None) -> dict[str, int]:
    """Follow every new-side thread to the new head. A thread whose line changed is `outdated`
    (it stays, with its old line quoted); the others move with the code."""
    git_dir = git_dir_of(row, settings)
    head = new_head or head_sha(row, git_dir)
    moved = outdated = 0
    if not head:
        return {"moved": 0, "outdated": 0}
    for r in c.execute(text(
            "SELECT id, path, line, commit_sha FROM local_change_set_comments WHERE "
            "change_set_id = :cs AND reply_to IS NULL AND side = 'new' AND outdated = false "
            "AND path IS NOT NULL AND line IS NOT NULL AND commit_sha IS NOT NULL "
            "AND commit_sha <> :h"), {"cs": row["id"], "h": head}).mappings().all():
        mapped = map_line(git_dir, r["commit_sha"], head, r["path"], int(r["line"]))
        if mapped is None:
            c.execute(text("UPDATE local_change_set_comments SET outdated = true, updated_at = now() "
                           "WHERE id = :i"), {"i": r["id"]})
            outdated += 1
        else:
            c.execute(text("UPDATE local_change_set_comments SET line = :l, commit_sha = :h, "
                           "updated_at = now() WHERE id = :i"),
                      {"l": mapped, "h": head, "i": r["id"]})
            moved += 1
    return {"moved": moved, "outdated": outdated}


# ── the fix round ─────────────────────────────────────────────────────────────────────────────

def _as_review_comment(r: Mapping[str, Any]) -> review_comments.ReviewComment:
    rc = review_comments.ReviewComment(
        severity=r["severity"], body=r["body"], path=r["path"], line=r["line"],
        end_line=r["end_line"], author=r["author"], author_kind=review_comments.AUTHOR_HUMAN,
        stage="person")
    rc.ref = f"{REF_PREFIX}{int(r['id'])}"
    return rc


def required_inputs(c: Any, change_set_id: str) -> list[dict[str, Any]]:
    """The person's unresolved threads, in the order the implementer is shown them."""
    rows = [dict(r) for r in c.execute(text(
        "SELECT * FROM local_change_set_comments WHERE change_set_id = :cs AND reply_to IS NULL "
        "AND author_kind = 'human' AND state <> 'resolved' ORDER BY id"),
        {"cs": change_set_id}).mappings()]
    order = review_comments.implementer_order([_as_review_comment(r) for r in rows])
    by_ref = {f"{REF_PREFIX}{int(r['id'])}": r for r in rows}
    return [by_ref[rc.ref] for rc in order]


def fix_brief(change: Mapping[str, Any], rows: list[dict[str, Any]], note: str) -> str:
    """The fix round's brief: the person's note, then their line comments as REQUIRED inputs —
    numbered exactly as `render_for_implementer` numbers them — and how to answer each."""
    parts = [f"FIX CHANGE #{change['number']} ON ITS OWN BRANCH {change['branch']} "
             f"(“{change.get('title') or change['branch']}”). A person reviewed it and asked for "
             "changes."]
    if str(note or "").strip():
        parts.append(f"THEIR NOTE:\n{str(note).strip()}")
    if rows:
        rendered = review_comments.render_for_implementer([_as_review_comment(r) for r in rows])
        parts.append("THE PERSON'S LINE COMMENTS ARE REQUIRED INPUTS: answer every one.\n"
                     + rendered)
        parts.append(review_comments.response_clause())
    return "\n\n".join(parts)


def _fctx_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        rc = _as_review_comment(r)
        out.append({**rc.to_dict(), "id": rc.ref, "status": review_comments.STATUS_OPEN,
                    "round": 0, "required": True})
    return out


def request_changes(c: Any, settings: Any, *, project_id: str, number: int, note: Any, actor: str,
                    jobs: Any, decisions: Any = None) -> dict[str, Any]:
    """REQUEST CHANGES: the review decision is sent back (revise), and that starts the fix round
    (`on_review_sent_back`). A change with no pending decision (a pushed branch whose review has
    not asked yet) starts its fix round straight away."""
    row = change_row(c, project_id, number)
    if row["state"] != "open":
        raise ReviewError(f"Change #{number} is {row['state']}.", 409, "not_open")
    words = str(note or "").strip()
    if not words and not required_inputs(c, row["id"]):
        raise ReviewError("Say what to change, or comment on a line first.", 422, "note_required")
    words = words or "Address my line comments."
    from .local_repo_host import change_set_url

    url = change_set_url(row["repository"], row["number"])
    decision = c.execute(text(
        "SELECT id FROM decisions WHERE recommendation->>'pull_request_url' = :u AND "
        "recommendation->>'action' = 'review_pull_request' AND status = 'pending' "
        "ORDER BY created_at DESC LIMIT 1"), {"u": url}).scalar()
    if decision and decisions is not None:
        decisions.transition(c, str(decision), action="revise", actor=actor, note=words)
        started = c.execute(text(
            "SELECT run_id FROM local_change_sets WHERE id = :i"), {"i": row["id"]}).scalar()
        return {"number": int(row["number"]), "decision_id": str(decision),
                "status": "revision_requested", "run_id": started}
    out = start_fix_round(c, settings, change=row, note=words, actor=actor, jobs=jobs)
    return {"number": int(row["number"]), "decision_id": None, "status": "fix_round_started",
            **out}


def on_review_sent_back(c: Any, settings: Any, decision: Mapping[str, Any], *, actor: str,
                        note: Optional[str], jobs: Any) -> Optional[dict[str, Any]]:
    """decisions._handle_non_approved_follow_on, for a local change's review decision sent back."""
    rec = decision.get("recommendation") or {}
    row = change_for_url(c, rec.get("pull_request_url"))
    if row is None or row.get("state") != "open":
        return None
    return start_fix_round(c, settings, change=row, note=str(note or ""), actor=actor, jobs=jobs)


def start_fix_round(c: Any, settings: Any, *, change: Mapping[str, Any], note: str, actor: str,
                    jobs: Any) -> dict[str, Any]:
    """A build on the change's own branch: a follow-up of the build that made it (same lineage
    branch, same open change set), or — for a pushed branch no build made — a build pinned to
    that branch (`finalize_ctx.local_change_branch`)."""
    from . import code_task_service

    rows = required_inputs(c, change["id"])
    # A person asking again re-opens what the agent answered but they did not accept.
    if rows:
        c.execute(text("UPDATE local_change_set_comments SET state = 'open', updated_at = now() "
                       "WHERE id = ANY(:ids) AND state IN ('addressed', 'disputed')"),
                  {"ids": [int(r["id"]) for r in rows]})
    brief = fix_brief(change, rows, note)
    extra = {"local_change_set_id": change["id"], "review_comments": _fctx_entries(rows),
             "local_review_fix": True}
    parent = None
    if change.get("run_id"):
        parent = c.execute(text("SELECT id, status, spec FROM code_task_runs WHERE id = :r"),
                           {"r": change["run_id"]}).mappings().first()
    run_id: Optional[str] = None
    if parent is not None and not code_task_service.is_simulated_run(dict(parent)) and \
            str(change["branch"]).startswith("boss/code/"):
        started = code_task_service.start_followup_task(
            c, parent_run_id=str(parent["id"]), instructions=brief, jobs=jobs, actor=actor,
            title=f"Fix change #{change['number']}: {change.get('title') or change['branch']}"[:160],
            finalize_extra=extra)
        run_id = started["run_id"]
    else:
        owner, _, repo = str(change["repository"]).partition("/")
        run_id = code_task_service.start_code_task(
            c, project_id=change["project_id"], task_id=None,
            title=f"Fix change #{change['number']}: {change.get('title') or change['branch']}"[:160],
            instructions=("Continue work on an EXISTING change. Its code is already on this "
                          "branch: read it first and change only what is asked.\n\n" + brief),
            acceptance_criteria=["Every required line comment is fixed, or disputed with a reason.",
                                 "Existing tests still pass."],
            test_commands=[], owner=owner, repo=repo, base_branch=change["branch"],
            finalize_ctx={**extra, "local_change_branch": change["branch"],
                          "requested_by": actor}, jobs=jobs, allow_duplicate=False)
    c.execute(text("UPDATE local_change_sets SET run_id = :r, updated_at = now() WHERE id = :i"),
              {"r": run_id, "i": change["id"]})
    if rows:
        c.execute(text("UPDATE local_change_set_comments SET run_round = :r, updated_at = now() "
                       "WHERE id = ANY(:ids)"), {"r": run_id, "ids": [int(r["id"]) for r in rows]})
    emit_event(c, change["project_id"], "local_review.fix_round_started", actor,
               "local_change_set", change["id"],
               {"number": int(change["number"]), "run_id": run_id, "required": len(rows),
                "note": str(note or "")[:400]})
    return {"run_id": run_id, "required": [int(r["id"]) for r in rows]}


# ── the agent's replies ───────────────────────────────────────────────────────────────────────

def _agent_name(entry: Mapping[str, Any], fctx: Mapping[str, Any]) -> str:
    return str(fctx.get("builder_name") or "builder")


def sync_agent_replies(c: Any, run_id: str, *, settings: Any = None) -> int:
    """The build's RESPONSE lines on required comments (finalize_ctx.review_comments, ids
    `lcc_<id>`, written by governed_code_task_service._record_comment_responses) become the
    agent's reply on each thread, once per round, and set it addressed or disputed."""
    run = c.execute(text("SELECT project_id, finalize_ctx FROM code_task_runs WHERE id = :r"),
                    {"r": run_id}).mappings().first()
    if run is None:
        return 0
    fctx = run["finalize_ctx"] or {}
    if isinstance(fctx, str):
        fctx = json.loads(fctx)
    cs_id = fctx.get("local_change_set_id")
    entries = [e for e in (fctx.get("review_comments") or []) if isinstance(e, dict)
               and str(e.get("id") or "").startswith(REF_PREFIX) and e.get("response_kind")]
    if not cs_id or not entries:
        return 0
    change = change_by_id(c, str(cs_id))
    if change is None:
        return 0
    head = ""
    try:
        head = head_sha(change, git_dir_of(change, settings))
    except Exception:  # noqa: BLE001 — a reply without its commit is still the reply
        head = change.get("head_sha") or ""
    written = 0
    for e in entries:
        try:
            cid = int(str(e["id"])[len(REF_PREFIX):])
        except ValueError:
            continue
        kind = "disputed" if e.get("response_kind") == "disputed" else "addressed"
        words = str(e.get("response") or "").strip() or ("Fixed." if kind == "addressed" else "Disputed.")
        round_key = f"{run_id}#{int(e.get('response_round') or 0)}"
        seen = c.execute(text(
            "SELECT 1 FROM local_change_set_comments WHERE reply_to = :i AND author_kind = 'agent' "
            "AND run_round = :r"), {"i": cid, "r": round_key}).first()
        if seen:
            continue
        root = c.execute(text("SELECT id FROM local_change_set_comments WHERE id = :i AND "
                              "change_set_id = :cs AND reply_to IS NULL"),
                         {"i": cid, "cs": change["id"]}).scalar()
        if root is None:
            continue
        lead = (f"Changed in commit {head[:7]}: " if kind == "addressed" and head else
                "Fixed: " if kind == "addressed" else "Disputed: ")
        add_comment(c, settings, project_id=change["project_id"], number=int(change["number"]),
                    body=lead + words, author=_agent_name(e, fctx), author_kind="agent",
                    origin="agent", reply_to=cid, run_round=round_key,
                    response_commit=head or None, emit=False)
        c.execute(text("UPDATE local_change_set_comments SET state = :s, response_commit = :h, "
                       "updated_at = now() WHERE id = :i AND state <> 'resolved'"),
                  {"s": kind, "h": head or None, "i": cid})
        written += 1
    if written:
        emit_event(c, change["project_id"], "local_review.agent_replied", "boss",
                   "local_change_set", change["id"],
                   {"number": int(change["number"]), "run_id": run_id, "replies": written})
    return written


def on_change_proposed(c: Any, settings: Any, *, run_id: Optional[str], repository: str,
                       number: int) -> dict[str, Any]:
    """The pipeline opened or refreshed a local change set (software_factory.propose_pull_request):
    the change belongs to this build, its threads follow the new head, and the build's answers to
    a person's comments become its replies."""
    row = c.execute(text("SELECT * FROM local_change_sets WHERE repository = :r AND number = :n"),
                    {"r": repository, "n": int(number)}).mappings().first()
    if row is None:
        return {}
    row = dict(row)
    try:
        with c.begin_nested():
            git_dir = git_dir_of(row, settings)
            head = head_sha(row, git_dir)
            if run_id:
                c.execute(text("UPDATE local_change_sets SET run_id = :r, head_sha = COALESCE(:h, "
                               "head_sha), updated_at = now() WHERE id = :i"),
                          {"r": run_id, "h": head or None, "i": row["id"]})
            moved = remap(c, settings, row, head)
            replied = sync_agent_replies(c, run_id, settings=settings) if run_id else 0
    except Exception as error:  # noqa: BLE001 — review bookkeeping never fails a change set
        log.warning("local review: change #%s not refreshed: %s", number, type(error).__name__)
        return {}
    return {**moved, "replied": replied}


def unanswered_required(fctx: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The required comments of a fix round the build has not answered yet."""
    return [e for e in (fctx.get("review_comments") or []) if isinstance(e, dict)
            and str(e.get("id") or "").startswith(REF_PREFIX) and e.get("required")
            and not e.get("response_kind")]


# ── git notes (refs/notes/boss-review) ────────────────────────────────────────────────────────

def import_notes(c: Any, settings: Any, change: Mapping[str, Any], *, author: str,
                 notes_sha: Optional[str] = None) -> int:
    """Comments written offline as git notes: each note on a commit of this change's branch is
    JSON lines `{"path", "line", "body", "severity"?, "side"?}`; each line becomes a comment
    through `add_comment` (origin git_notes), once (an identical comment is not made twice)."""
    git_dir = git_dir_of(change, settings)
    ref = notes_sha or NOTES_REF
    if not local_repo.rev_parse(git_dir, ref):
        return 0
    head, base = head_sha(change, git_dir), base_sha(change, git_dir)
    if not head:
        return 0
    commits = set(local_repo.git(["rev-list", f"{base}..{head}"] if base else ["rev-list", head],
                                 git_dir=git_dir).split())
    listed = local_repo.git(["notes", f"--ref={ref}", "list"], git_dir=git_dir, check=False)
    made = 0
    for line in listed.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[1] not in commits:
            continue
        note_blob, commit = parts
        body = local_repo.git(["cat-file", "blob", note_blob], git_dir=git_dir, check=False)
        for raw in body.splitlines():
            raw = raw.strip()
            if not raw or not raw.startswith("{"):
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(item, dict) or item.get("from") == "boss" or not item.get("body"):
                continue
            exists = c.execute(text(
                "SELECT 1 FROM local_change_set_comments WHERE change_set_id = :cs AND "
                "origin = 'git_notes' AND body = :b AND path IS NOT DISTINCT FROM :p AND "
                "original_line IS NOT DISTINCT FROM :l"),
                {"cs": change["id"], "b": str(item["body"]).strip(), "p": item.get("path"),
                 "l": item.get("line")}).first()
            if exists:
                continue
            try:
                add_comment(c, settings, project_id=change["project_id"],
                            number=int(change["number"]), body=item["body"], author=author,
                            path=item.get("path"), line=item.get("line"),
                            side=item.get("side") or "new", commit_sha=commit,
                            severity=item.get("severity"), origin="git_notes")
                made += 1
            except ReviewError as refused:
                log.info("git note skipped on %s: %s", commit[:7], refused)
    return made


def export_notes(c: Any, settings: Any, change: Mapping[str, Any]) -> int:
    """BOSS's view of the conversation, as notes on the head commit, for
    `git log --notes=boss-review` (each line marked `"from": "boss"`, never re-imported)."""
    git_dir = git_dir_of(change, settings)
    head = head_sha(change, git_dir)
    if not head:
        return 0
    lines = []
    for t in threads(c, change["id"]):
        lines.append(json.dumps({"from": "boss", "id": t["id"], "path": t["path"],
                                 "line": t["line"], "state": t["state"], "author": t["author"],
                                 "severity": t["severity"], "body": t["body"],
                                 "replies": [r["body"] for r in t["replies"]]},
                                sort_keys=True))
    if not lines:
        return 0
    local_repo.git(["notes", f"--ref={NOTES_REF}", "add", "-f", "-m", "\n".join(lines), head],
                   git_dir=git_dir)
    return len(lines)


__all__ = ["FILE_CAP_BYTES", "ReviewError", "add_comment", "change_for_url", "change_row",
           "clean_path", "comment", "export_notes", "fix_brief", "import_notes", "map_line",
           "merge_hold", "merge_hold_for_url", "on_review_sent_back", "read_file",
           "record_working_copy", "remap", "request_changes", "required_inputs", "resolve",
           "resolve_ref", "start_fix_round", "summary", "sync_agent_replies", "threads", "tree",
           "unanswered_required", "where"]
