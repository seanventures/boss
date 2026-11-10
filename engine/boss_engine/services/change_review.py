"""CHANGE REVIEW (BOSSAPP-954; wireframe docs/product/wireframes/agentic-factory/6-git-review.html):
one git-style review of a change, the same for a LOCAL change set and a GITHUB pull request.

ONE SERVICE, TWO SOURCES. `source_for` returns a LocalSource (git on BOSS's bare repository,
through local_review's public functions) or a GitHubSource (the GitHub adapter, with the
project binding's installation token). Both answer the same questions:

    summary           the change, its branch, base, head, decision, checks, comments
    files(base, head) the changed files and the unified diff, for the change or any base…head
    tree(ref)         every path at a ref, with the change's status and comment counts
    blob(path, ref)   one file at a ref
    commits()         the change's commits, each with the comments it fixed
    commit(sha)       one commit's diff
    checks()          BOSS's sandbox build, and GitHub's check runs for a GitHub change
    threads()         review comment threads (one shape for both)
    add / reply / resolve, request_changes (the fix run), merge (the review decision)

GITHUB IS READ BACK, NOT TRUSTED TO BE UP. Every GitHub read goes through `_cached`: a short
cache (`change_review_cache`) answers while it is fresh; when GitHub does not answer, the last
copy is returned marked `stale`. A comment written while GitHub is down is queued
(`change_review_outbox`) and shown as queued; the next read that reaches GitHub posts it.

WHO A GITHUB COMMENT IS FROM. Posted as the person when their GitHub account is linked
(vault, GITHUB_AS_USER); otherwise by the BOSS app, its body led by "_for <name> (via BOSS)_",
which this module reads back as the person's comment.

PER PERSON: "Viewed" ticks and "Since my review" (`change_review_viewed`). THE PR AGENT: the
chat with the task that built the change (`change_review_chat`): a message to a running task is
steered into it (code_task_service.queue_guidance, the steer endpoint's own path); to an idle one
it is kept and sent with the next fix run.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

from . import local_repo, local_review
from .core import emit_event

log = logging.getLogger(__name__)

#: Seconds a GitHub read is reused before the page asks GitHub again.
CACHE_TTL = {"summary": 15, "threads": 10, "files": 60, "commits": 30, "checks": 20,
             "tree": 120, "blob": 300, "compare": 600, "commit": 3600}
#: GitHub commits whose +/− and file count the Commits tab reads (one call each, cached).
STAT_COMMITS = 30
#: The fix run's ids for GitHub review comments (local ones are local_review.REF_PREFIX).
GH_REF_PREFIX = "ghc_"
_FOR = re.compile(r"^_for (.+?) \(via BOSS\)_\s*\n+", re.S)
#: How local_review.publish_comments signs a comment it carried to the pull request.
_CARRIED = re.compile(r"^\*\*(agent )?(.+?)\*\* · local review · [^\n]*\n\n", re.S)
_GH_PR = re.compile(r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")
#: A local thread that did not reach the pull request is shown with an id far from GitHub's.
KEPT_ID_BASE = 1_000_000_000
_SEV_LEAD = {"minor": "**Should fix:** ", "nit": "**Nit:** "}
_SEV_READ = re.compile(r"^\*\*(Should fix|Nit|Must fix):\*\*\s*", re.I)
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class ChangeReviewError(ValueError):
    def __init__(self, message: str, status: int = 422, error: str = "change_review_refused"):
        super().__init__(message)
        self.status = status
        self.error = error

    def body(self) -> dict[str, Any]:
        return {"error": self.error, "message": str(self)}


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lines_of(diff: str) -> int:
    return diff.count("\n")


# ── runs, the agent and the chat (both sources) ───────────────────────────────────────────────

def queue_guidance(c: Any, run_id: str, guidance: str, actor: str) -> dict[str, Any]:
    """Steer a RUNNING task: the guidance is read at its next stage boundary. The steer endpoint
    (POST /v1/code-tasks/{id}/steer) and the PR agent's chat both come here."""
    entry = {"text": str(guidance)[:4000], "actor": actor, "at": _now().isoformat()}
    row = c.execute(text("SELECT project_id, status FROM code_task_runs WHERE id = :r"),
                    {"r": run_id}).mappings().first()
    if row is None:
        raise ChangeReviewError(f"There is no task {run_id}.", 404, "no_run")
    if row["status"] in ("completed", "failed", "cancelled"):
        raise ChangeReviewError(f"The task is {row['status']}; steering only applies to a running "
                                "task.", 409, "not_running")
    c.execute(text(
        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
        "|| jsonb_build_object('pending_guidance', "
        "COALESCE(finalize_ctx->'pending_guidance','[]'::jsonb) || CAST(:e AS JSONB)), "
        "updated_at = now() WHERE id = :r AND project_id = :p"),
        {"e": json.dumps([entry]), "r": run_id, "p": row["project_id"]})
    return entry


_RUNNING = ("running", "queued", "pending", "leased", "in_progress", "provisioning", "starting")


def _run_view(c: Any, run_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not run_id:
        return None
    r = c.execute(text("SELECT id, title, status, phase, usage, finalize_ctx, updated_at, "
                       "created_at FROM code_task_runs WHERE id = :r"), {"r": run_id}
                  ).mappings().first()
    if r is None:
        return None
    usage = r["usage"] or {}
    fctx = r["finalize_ctx"] or {}
    if isinstance(usage, str):
        usage = json.loads(usage or "{}")
    if isinstance(fctx, str):
        fctx = json.loads(fctx or "{}")
    micros = int(usage.get("cost_usd_micros") or 0)
    return {"run_id": r["id"], "title": r["title"], "status": r["status"], "phase": r["phase"],
            "running": str(r["status"] or "").lower() in _RUNNING,
            "builder": str(fctx.get("builder_name") or fctx.get("agent") or "builder"),
            "cost_usd": round(micros / 1_000_000, 2), "updated_at": _iso(r["updated_at"]),
            "created_at": _iso(r["created_at"])}


def chat_messages(c: Any, project_id: str, key: str) -> list[dict[str, Any]]:
    return [{"id": int(r["id"]), "author": r["author"], "author_kind": r["author_kind"],
             "body": r["body"], "delivery": r["delivery"], "run_id": r["run_id"],
             "created_at": _iso(r["created_at"])}
            for r in c.execute(text(
                "SELECT * FROM change_review_chat WHERE project_id = :p AND change_key = :k "
                "ORDER BY id"), {"p": project_id, "k": key}).mappings()]


def take_queued_notes(c: Any, project_id: str, key: str, run_id: Optional[str] = None) -> str:
    """The chat messages a person left while the agent was idle: they go with the fix run."""
    rows = c.execute(text(
        "SELECT id, author, body FROM change_review_chat WHERE project_id = :p AND change_key = :k "
        "AND delivery = 'queued' AND author_kind = 'human' ORDER BY id"),
        {"p": project_id, "k": key}).mappings().all()
    if not rows:
        return ""
    c.execute(text("UPDATE change_review_chat SET delivery = 'sent', run_id = :r WHERE id = ANY(:i)"),
              {"r": run_id, "i": [int(r["id"]) for r in rows]})
    return "\n".join(f"- {r['author']}: {r['body']}" for r in rows)


def mark_notes_sent(c: Any, project_id: str, key: str, run_id: Optional[str]) -> None:
    if run_id:
        c.execute(text("UPDATE change_review_chat SET run_id = :r WHERE project_id = :p AND "
                       "change_key = :k AND delivery = 'sent' AND run_id IS NULL"),
                  {"r": run_id, "p": project_id, "k": key})


def viewed(c: Any, project_id: str, key: str, person: str) -> dict[str, Any]:
    rows = c.execute(text(
        "SELECT path, sha, updated_at FROM change_review_viewed WHERE project_id = :p AND "
        "change_key = :k AND person = :w"), {"p": project_id, "k": key, "w": person}).mappings()
    paths: dict[str, str] = {}
    reviewed = None
    for r in rows:
        if r["path"] == "":
            reviewed = r["sha"]
        else:
            paths[r["path"]] = r["sha"]
    return {"person": person, "paths": paths, "reviewed_sha": reviewed}


def set_viewed(c: Any, project_id: str, key: str, person: str, *, path: str, sha: str,
               viewed_: bool = True) -> dict[str, Any]:
    """A person ticks a file Viewed at a head (path "" records the head they last reviewed)."""
    if viewed_:
        c.execute(text(
            "INSERT INTO change_review_viewed (project_id, change_key, person, path, sha) VALUES "
            "(:p, :k, :w, :path, :sha) ON CONFLICT (project_id, change_key, person, path) DO "
            "UPDATE SET sha = EXCLUDED.sha, updated_at = now()"),
            {"p": project_id, "k": key, "w": person, "path": path, "sha": sha or ""})
    else:
        c.execute(text("DELETE FROM change_review_viewed WHERE project_id = :p AND change_key = :k "
                       "AND person = :w AND path = :path"),
                  {"p": project_id, "k": key, "w": person, "path": path})
    return viewed(c, project_id, key, person)


def _decision_for(c: Any, url: str) -> Optional[dict[str, Any]]:
    row = c.execute(text(
        "SELECT id, status, question FROM decisions WHERE recommendation->>'pull_request_url' = :u "
        "AND (recommendation->>'action') = 'review_pull_request' ORDER BY created_at DESC LIMIT 1"),
        {"u": url}).mappings().first()
    return dict(row) if row else None


def _merge_offer(c: Any, project_id: str, url: str, head: str, is_open: bool
                 ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    decision = _decision_for(c, url)
    merge = ({"how": "approve the review decision", "decision_id": decision["id"]}
             if decision and decision["status"] == "pending" and is_open else None)
    if merge is not None:
        from . import ci_gate

        merge["held"] = ci_gate.approval_hold(c, project_id, {
            "action": "review_pull_request", "pull_request_url": url, "head_sha": head or ""})
    return decision, merge


# ── the local source ──────────────────────────────────────────────────────────────────────────

class LocalSource:
    """A local change set: git on the bare repository, comments in local_review's one store."""

    kind = "local"

    def __init__(self, c: Any, settings: Any, project_id: str, number: int, *, engine: Any = None):
        self.c, self.settings, self.project_id, self.number = c, settings, project_id, int(number)
        self.engine = engine
        try:
            self.row = local_review.change_row(c, project_id, number)
        except local_review.ReviewError as error:
            raise ChangeReviewError(str(error), error.status, error.error) from None
        self.git_dir = local_review.git_dir_of(self.row, settings)
        self.key = f"local:{self.row['id']}"
        from .local_repo_host import change_set_url

        self.url = change_set_url(self.row["repository"], self.row["number"])

    # refs
    def head(self) -> str:
        return local_review.head_sha(self.row, self.git_dir)

    def base(self) -> str:
        return local_review.base_sha(self.row, self.git_dir)

    def ref(self, ref: Optional[str]) -> str:
        try:
            return local_review.resolve_ref(self.row, self.git_dir, ref)
        except local_review.ReviewError as error:
            raise ChangeReviewError(str(error), error.status, error.error) from None

    def run_id(self) -> Optional[str]:
        return self.row.get("run_id")

    # reads
    def summary(self) -> dict[str, Any]:
        from .local_repo_host import LocalGitAdapter

        row = self.row
        owner, _, name = row["repository"].partition("/")
        adapter = LocalGitAdapter(self.settings, owner=owner, repo=name, project_id=self.project_id,
                                  git_dir=self.git_dir, engine=self.engine)
        view: dict[str, Any] = {
            "source": "local", "number": int(row["number"]), "url": self.url,
            "repository": row["repository"], "branch": row["branch"],
            "base_branch": row["base_branch"], "title": row["title"], "state": row["state"],
            "draft": bool(row["draft"]), "labels": row["labels"] or [], "run_id": row["run_id"],
            "head_sha": row["head_sha"], "merged_sha": row["merged_sha"],
            "merged_by": row["merged_by"], "merged_at": _iso(row["merged_at"]),
            "pending_push": bool(row["pending_push"]), "github_pr_url": row["github_pr_url"],
            "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
            "body": row["body"], "author": row.get("author"), "stale": False}
        try:
            view["files"] = adapter.pull_request_files(row["number"])
            view["diff"] = adapter.get_pull_request_diff(row["number"])[:400_000]
            view["commits"] = self.commits()
        except Exception as error:  # noqa: BLE001 — the record still shows
            view.update({"files": [], "diff": "", "commits": [],
                         "diff_error": f"{type(error).__name__}: {str(error)[:200]}"})
        try:
            view["where"] = local_review.where(self.c, self.settings, row)
        except Exception as error:  # noqa: BLE001
            view["where"] = {"error": f"{type(error).__name__}: {str(error)[:200]}"}
        try:
            view["base_sha"] = self.base()
        except Exception:  # noqa: BLE001
            view["base_sha"] = row.get("base_sha")
        view["comments"] = self.threads()
        view["comment_summary"] = local_review.summary(self.c, row["id"])
        try:
            notes = adapter.notes(row["number"])
        except Exception:  # noqa: BLE001
            notes = []
        view["review"] = [{"id": n["number"], "kind": n["kind"], "event": n["event"],
                           "body": n["body"], "comments": n["comments"] or [],
                           "in_reply_to": n["in_reply_to"], "author": n["author"],
                           "created_at": _iso(n["created_at"])} for n in notes]
        view["checks"] = self.checks()
        view["decision"], view["merge"] = _merge_offer(
            self.c, self.project_id, self.url, row.get("head_sha") or "", row["state"] == "open")
        return view

    def _range(self, base: Optional[str], head: Optional[str]) -> tuple[str, str]:
        if not base and not head:
            row = self.row
            b = row["base_branch"] if row["state"] == "open" else (row["base_sha"] or row["base_branch"])
            b = local_repo.rev_parse(self.git_dir, b) or self.base()
            return b, self.head()
        return self.ref(base or "base"), self.ref(head or "head")

    def files(self, base: Optional[str] = None, head: Optional[str] = None) -> dict[str, Any]:
        b, h = self._range(base, head)
        return {"base": b, "head": h, "files": local_repo.changed_files(self.git_dir, b, h),
                "diff": local_repo.diff(self.git_dir, b, h)[:400_000], "stale": False}

    def commits(self) -> list[dict[str, Any]]:
        row = self.row
        base = row["base_branch"] if row["state"] == "open" else (row["base_sha"] or row["base_branch"])
        head = (row["branch"] if row["state"] == "open"
                and local_repo.branch_exists(self.git_dir, row["branch"])
                else row["head_sha"] or row["branch"])
        out = local_repo.git(["log", "--max-count=200", "--numstat",
                              "--format=\x1e%H%x09%an%x09%aI%x09%s", f"{base}..{head}"],
                             git_dir=self.git_dir, check=False)
        fixed = self._fixed_by_commit()
        commits = []
        for block in out.split("\x1e"):
            lines = [x for x in block.splitlines() if x.strip()]
            if not lines:
                continue
            parts = lines[0].split("\t", 3)
            if len(parts) < 4:
                continue
            sha, author, date, subject = parts
            adds = dels = files = 0
            for stat in lines[1:]:
                a, _, rest = stat.partition("\t")
                d, _, _path = rest.partition("\t")
                files += 1
                adds += int(a) if a.isdigit() else 0
                dels += int(d) if d.isdigit() else 0
            commits.append({"sha": sha, "message": subject, "author": author, "date": date,
                            "additions": adds, "deletions": dels, "files": files,
                            "fixes": fixed.get(sha, [])})
        return commits

    def _fixed_by_commit(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for r in self.c.execute(text(
                "SELECT id, response_commit FROM local_change_set_comments WHERE change_set_id = :cs "
                "AND reply_to IS NULL AND response_commit IS NOT NULL AND state IN "
                "('addressed', 'resolved')"), {"cs": self.row["id"]}).mappings():
            sha = local_repo.rev_parse(self.git_dir, r["response_commit"]) or r["response_commit"]
            out.setdefault(sha, []).append(int(r["id"]))
        return out

    def commit(self, sha: str) -> dict[str, Any]:
        full = self.ref(sha)
        parent = local_repo.rev_parse(self.git_dir, f"{full}^") or ""
        info = local_repo.git(["log", "-1", "--format=%an%x09%aI%x09%B", full],
                              git_dir=self.git_dir, check=False).split("\t", 2)
        if parent:
            diff = local_repo.git(["diff", "--no-color", parent, full], git_dir=self.git_dir)
            files = local_repo.changed_files(self.git_dir, parent, full)
        else:
            diff = local_repo.git(["show", "--no-color", "--format=", full], git_dir=self.git_dir)
            files = []
        return {"sha": full, "parent": parent or None,
                "author": info[0] if len(info) > 2 else "", "date": info[1] if len(info) > 2 else None,
                "message": (info[2] if len(info) > 2 else "").strip(), "files": files,
                "diff": diff[:400_000], "fixes": self._fixed_by_commit().get(full, []),
                "stale": False}

    def tree(self, ref: Optional[str]) -> dict[str, Any]:
        try:
            return {**local_review.tree(self.c, self.settings, self.project_id, self.number, ref),
                    "stale": False}
        except local_review.ReviewError as error:
            raise ChangeReviewError(str(error), error.status, error.error) from None

    def blob(self, path: str, ref: Optional[str]) -> dict[str, Any]:
        try:
            return {**local_review.read_file(self.c, self.settings, self.project_id, self.number,
                                             path, ref), "stale": False}
        except local_review.ReviewError as error:
            raise ChangeReviewError(str(error), error.status, error.error) from None

    def checks(self) -> Optional[dict[str, Any]]:
        run = self.run_id()
        if not run:
            return None
        r = self.c.execute(text(
            "SELECT status, phase, state->'gates' AS gates, state->>'tests_passed' AS tests "
            "FROM code_task_runs WHERE id = :r"), {"r": run}).mappings().first()
        if not r:
            return None
        return {"where": "BOSS's sandbox", "run_status": r["status"], "tests_passed": r["tests"],
                "gates": r["gates"], "items": []}

    def threads(self) -> list[dict[str, Any]]:
        out = local_review.threads(self.c, self.row["id"])
        texts: dict[tuple[str, str], Optional[list[str]]] = {}
        for t in out:
            t["snippet"] = self._snippet(t, texts)
            t["fixed_in"] = t.get("response_commit") if t["state"] in ("addressed", "resolved") \
                and any(r.get("author_kind") == "agent" for r in t.get("replies") or []) else None
        return out

    def _snippet(self, t: Mapping[str, Any], texts: dict) -> Optional[dict[str, Any]]:
        """The comment's code: 2 lines either side of its line, AS IT WAS at the commit it was
        made on (an outdated comment keeps its old code)."""
        if not t.get("path") or not t.get("line") or not t.get("commit_sha"):
            return None
        key = (str(t["commit_sha"]), str(t["path"]))
        if key not in texts:
            out = local_repo.git(["cat-file", "blob", f"{key[0]}:{key[1]}"], git_dir=self.git_dir,
                                 check=False, raw=True)
            data = out if isinstance(out, (bytes, bytearray)) else b""
            texts[key] = None if not data or b"\x00" in data[:8000] else \
                data.decode("utf-8", "replace").split("\n")
        return snippet_of(texts[key], int(t["line"]), str(t["commit_sha"]))

    def comment_summary(self) -> dict[str, int]:
        return local_review.summary(self.c, self.row["id"])

    # writes
    def add_comment(self, *, body: str, author: str, author_kind: str, path: Any, line: Any,
                    side: str, commit_sha: Optional[str], severity: Any, origin: str = "console",
                    end_line: Any = None, person_token: Any = None,
                    principal_id: Optional[str] = None) -> dict[str, Any]:
        try:
            return local_review.add_comment(
                self.c, self.settings, project_id=self.project_id, number=self.number, body=body,
                author=author, author_kind=author_kind, path=path, line=line, end_line=end_line,
                side=side, commit_sha=commit_sha, severity=severity, origin=origin,
                principal_id=principal_id)
        except local_review.ReviewError as error:
            raise ChangeReviewError(str(error), error.status, error.error) from None

    def reply(self, comment_id: int, *, body: str, author: str, author_kind: str,
              origin: str = "console", person_token: Any = None,
              principal_id: Optional[str] = None) -> dict[str, Any]:
        try:
            return local_review.add_comment(
                self.c, self.settings, project_id=self.project_id, number=self.number, body=body,
                author=author, author_kind=author_kind, origin=origin, reply_to=comment_id,
                principal_id=principal_id)
        except local_review.ReviewError as error:
            raise ChangeReviewError(str(error), error.status, error.error) from None

    def resolve(self, comment_id: int, *, actor: str, reopen: bool = False,
                person_token: Any = None) -> dict[str, Any]:
        try:
            return local_review.resolve(self.c, project_id=self.project_id, number=self.number,
                                        comment_id=comment_id, actor=actor, reopen=reopen)
        except local_review.ReviewError as error:
            raise ChangeReviewError(str(error), error.status, error.error) from None

    def request_changes(self, *, note: str, actor: str, jobs: Any, decisions: Any = None
                        ) -> dict[str, Any]:
        queued = take_queued_notes(self.c, self.project_id, self.key)
        words = "\n\n".join(p for p in (str(note or "").strip(),
                                         f"From the chat:\n{queued}" if queued else "") if p)
        try:
            out = local_review.request_changes(
                self.c, self.settings, project_id=self.project_id, number=self.number,
                note=words, actor=actor, jobs=jobs, decisions=decisions)
        except local_review.ReviewError as error:
            raise ChangeReviewError(str(error), error.status, error.error) from None
        mark_notes_sent(self.c, self.project_id, self.key, out.get("run_id"))
        return out

    def agent_run_id(self) -> Optional[str]:
        return self.run_id()


# ── the GitHub source ─────────────────────────────────────────────────────────────────────────

def github_adapter_for(c: Any, settings: Any, project_id: str, binding_id: str) -> Any:
    """The project binding's adapter (its installation token). Tests replace this function."""
    from .github_connections import GitHubConnectionService

    adapter, _conn, _binding = GitHubConnectionService(settings).adapter_for_binding(
        c, project_id=project_id, binding_id=binding_id)
    return adapter


def person_adapter(base: Any, token: str) -> Any:
    """The same repository, as the person (their linked GitHub account's token)."""
    from .github_adapter import GitHubAdapter

    return GitHubAdapter(base.settings, owner=base.owner, repo=base.repo, token=token,
                         api_base_url=getattr(base, "api_base_url", "https://api.github.com"))


def github_binding(c: Any, project_id: str, owner: str, repo: str) -> Optional[dict[str, Any]]:
    row = c.execute(text(
        "SELECT id, full_name, project_id FROM github_repository_bindings WHERE project_id = :p "
        "AND lower(full_name) = lower(:f) AND status = 'active' AND COALESCE(host, 'github') <> "
        "'local' ORDER BY (role = 'primary') DESC LIMIT 1"),
        {"p": project_id, "f": f"{owner}/{repo}"}).mappings().first()
    return dict(row) if row else None


def projects_for_repository(c: Any, owner: str, repo: str) -> list[str]:
    """The BOSS projects a GitHub repository is bound to (a PR link opens BOSS's page)."""
    return [str(r[0]) for r in c.execute(text(
        "SELECT DISTINCT project_id FROM github_repository_bindings WHERE lower(full_name) = "
        "lower(:f) AND status = 'active' AND COALESCE(host, 'github') <> 'local'"),
        {"f": f"{owner}/{repo}"}).all()]


def _http_status(error: BaseException) -> Optional[int]:
    resp = getattr(error, "response", None)
    code = getattr(resp, "status_code", None)
    return int(code) if isinstance(code, int) else None


class GitHubSource:
    """A GitHub pull request of a BOSS project."""

    kind = "github"

    def __init__(self, c: Any, settings: Any, project_id: str, owner: str, repo: str,
                 number: int, *, engine: Any = None):
        self.c, self.settings, self.project_id, self.number = c, settings, project_id, int(number)
        self.owner, self.repo, self.engine = owner, repo, engine
        binding = github_binding(c, project_id, owner, repo)
        if binding is None:
            raise ChangeReviewError(f"{owner}/{repo} is not a GitHub repository of this project.",
                                    404, "not_this_project")
        self.full_name = str(binding["full_name"])
        self.binding_id = str(binding["id"])
        self.key = f"gh:{self.full_name.lower()}#{self.number}"
        self.url = f"https://github.com/{self.full_name}/pull/{self.number}"
        self._adapter: Any = None
        self._meta: Optional[dict[str, Any]] = None
        self.stale = False
        self.fetched_at: Optional[str] = None
        #: The local change set this PR was published from (its old /changes/<p>/<n> link).
        self.carried_from: Optional["LocalSource"] = None

    # the adapter and the cache
    @property
    def adapter(self) -> Any:
        if self._adapter is None:
            try:
                self._adapter = github_adapter_for(self.c, self.settings, self.project_id,
                                                   self.binding_id)
            except Exception as error:  # noqa: BLE001 — a read still answers from the cache
                raise _Unreachable(f"{type(error).__name__}: {str(error)[:200]}") from error
        return self._adapter

    def _cached(self, kind: str, extra: str, fetch: Callable[[], Any]) -> Any:
        key = f"{self.key}|{kind}|{extra}"
        row = self.c.execute(text(
            "SELECT payload, fetched_at, now() - fetched_at < make_interval(secs => :ttl) AS fresh "
            "FROM change_review_cache WHERE project_id = :p AND cache_key = :k"),
            {"p": self.project_id, "k": key, "ttl": CACHE_TTL.get(kind, 30)}).mappings().first()
        if row is not None and row["fresh"]:
            self.fetched_at = self.fetched_at or _iso(row["fetched_at"])
            return row["payload"]
        try:
            value = fetch()
        except ChangeReviewError:
            raise
        except Exception as error:  # noqa: BLE001 — GitHub is down, slow or refusing
            status = _http_status(error)
            if status == 404:
                raise ChangeReviewError(f"GitHub has no {kind} for PR #{self.number} of "
                                        f"{self.full_name}.", 404, "not_on_github") from None
            if row is None:
                raise ChangeReviewError(
                    f"GitHub did not answer, and BOSS has no copy of this yet: "
                    f"{type(error).__name__}.", 503, "github_unreachable") from None
            self.stale = True
            self.fetched_at = _iso(row["fetched_at"])
            return row["payload"]
        self.c.execute(text(
            "INSERT INTO change_review_cache (project_id, cache_key, payload, fetched_at) VALUES "
            "(:p, :k, CAST(:v AS JSONB), now()) ON CONFLICT (project_id, cache_key) DO UPDATE SET "
            "payload = EXCLUDED.payload, fetched_at = now()"),
            {"p": self.project_id, "k": key, "v": json.dumps(value, default=str)})
        self.fetched_at = self.fetched_at or _now().isoformat()
        return value

    def _drop(self, kind: str) -> None:
        self.c.execute(text("DELETE FROM change_review_cache WHERE project_id = :p AND "
                            "cache_key LIKE :k"), {"p": self.project_id, "k": f"{self.key}|{kind}|%"})

    def meta(self) -> dict[str, Any]:
        if self._meta is None:
            self._meta = self._cached("summary", "meta",
                                      lambda: self.adapter.pull_request_view(self.number))
        return self._meta

    def head(self) -> str:
        return str(self.meta().get("head_sha") or "")

    def base(self) -> str:
        return str(self.meta().get("base_sha") or "")

    def ref(self, ref: Optional[str]) -> str:
        want = str(ref or "head").strip()
        if want in ("head", ""):
            return self.head()
        if want == "base":
            return self.base()
        if _SHA.match(want):
            return want
        raise ChangeReviewError(f"{want!r} is not this change's head, its base or a commit.", 422,
                                "unknown_ref")

    def run_id(self) -> Optional[str]:
        found = self.c.execute(text(
            "SELECT id FROM code_task_runs WHERE project_id = :p AND pull_request_url = :u "
            "ORDER BY created_at DESC LIMIT 1"), {"p": self.project_id, "u": self.url}).scalar()
        if not found and self.carried_from is not None:
            return self.carried_from.run_id()
        return found

    def agent_run_id(self) -> Optional[str]:
        """The latest run on this PR: the fix run when one was started, else the builder."""
        fix = self.c.execute(text(
            "SELECT id FROM code_task_runs WHERE project_id = :p AND "
            "finalize_ctx->'github_review_fix'->>'url' = :u ORDER BY created_at DESC LIMIT 1"),
            {"p": self.project_id, "u": self.url}).scalar()
        return fix or self.run_id()

    # reads
    def summary(self) -> dict[str, Any]:
        meta = self.meta()
        state = "merged" if meta.get("merged") else str(meta.get("state") or "open")
        view: dict[str, Any] = {
            "source": "github", "number": self.number, "url": self.url,
            "repository": self.full_name, "branch": meta.get("head_ref") or "",
            "base_branch": meta.get("base_ref") or "main", "title": meta.get("title") or "",
            "state": state, "draft": bool(meta.get("draft")), "labels": [],
            "run_id": self.run_id(), "head_sha": meta.get("head_sha"),
            "base_sha": meta.get("base_sha"), "merged_sha": None, "merged_by": None,
            "merged_at": None, "pending_push": False, "github_pr_url": self.url,
            "created_at": meta.get("created_at"), "updated_at": meta.get("updated_at"),
            "body": meta.get("body") or "", "author": meta.get("user"),
            "where": None,
            "clone": {"url": f"https://github.com/{self.full_name}.git", "branch": meta.get("head_ref"),
                      "web_url": self.url,
                      "fetch": f"git fetch origin pull/{self.number}/head:{meta.get('head_ref') or 'pr'}"}}
        try:
            got = self.files()
            view["files"], view["diff"] = got["files"], got["diff"]
        except ChangeReviewError as error:
            view.update({"files": [], "diff": "", "diff_error": str(error)})
        try:
            view["commits"] = self.commits()
        except ChangeReviewError:
            view["commits"] = []
        view["comments"] = self.threads()
        view["comment_summary"] = self.comment_summary(view["comments"])
        try:
            view["review"] = self._cached("summary", "reviews", lambda: [
                {"id": r["id"], "kind": "review", "event": r["state"], "body": "",
                 "comments": [], "in_reply_to": None, "author": r["reviewer"],
                 "created_at": r.get("submitted_at")}
                for r in self.adapter.pull_request_reviews(self.number)])
        except ChangeReviewError:
            view["review"] = []
        try:
            view["checks"] = self.checks()
        except ChangeReviewError:
            view["checks"] = None
        view["decision"], view["merge"] = _merge_offer(
            self.c, self.project_id, self.url, str(meta.get("head_sha") or ""), state == "open")
        view["stale"] = self.stale
        view["fetched_at"] = self.fetched_at
        if self.carried_from is not None:
            view["published_from"] = {"number": int(self.carried_from.row["number"]),
                                      "repository": self.carried_from.row["repository"]}
        view["queued"] = len([o for o in self._outbox() if o["status"] == "queued"])
        return view

    def files(self, base: Optional[str] = None, head: Optional[str] = None) -> dict[str, Any]:
        if not base and not head:
            def fetch() -> dict[str, Any]:
                return {"files": self.adapter.pull_request_files(self.number),
                        "diff": str(self.adapter.get_pull_request_diff(self.number))[:400_000]}
            got = self._cached("files", self.head() or "pr", fetch)
            return {"base": self.base(), "head": self.head(), **got, "stale": self.stale}
        b, h = self.ref(base or "base"), self.ref(head or "head")

        def fetch_compare() -> dict[str, Any]:
            files = [{"path": f["filename"], "status": f["status"], "additions": f["additions"],
                      "deletions": f["deletions"]}
                     for f in self.adapter.compare_changed_files(b, h)]
            return {"files": files, "diff": str(self.adapter.compare_diff(b, h))[:400_000]}
        got = self._cached("compare", f"{b}...{h}", fetch_compare)
        return {"base": b, "head": h, **got, "stale": self.stale}

    def _fixed(self) -> dict[int, dict[str, Any]]:
        """GitHub comments a fix run answered: {comment id: {kind, sha, run_id}}."""
        out: dict[int, dict[str, Any]] = {}
        for r in self.c.execute(text(
                "SELECT id, finalize_ctx FROM code_task_runs WHERE project_id = :p AND "
                "finalize_ctx->'github_review_fix'->>'url' = :u ORDER BY created_at"),
                {"p": self.project_id, "u": self.url}).mappings():
            fctx = r["finalize_ctx"] or {}
            if isinstance(fctx, str):
                fctx = json.loads(fctx or "{}")
            for e in fctx.get("review_comments") or []:
                if not isinstance(e, dict) or not str(e.get("id") or "").startswith(GH_REF_PREFIX):
                    continue
                if not e.get("response_kind"):
                    continue
                try:
                    cid = int(str(e["id"])[len(GH_REF_PREFIX):])
                except ValueError:
                    continue
                out[cid] = {"kind": e.get("response_kind"), "run_id": r["id"],
                            "sha": e.get("response_commit") or fctx.get("head_sha"),
                            "response": e.get("response")}
        return out

    def commits(self) -> list[dict[str, Any]]:
        got = self._cached("commits", self.head() or "pr",
                           lambda: self.adapter.pull_request_commits(self.number))
        fixed = self._fixed()
        out = []
        for i, c in enumerate(got):
            stats: dict[str, Any] = {"additions": None, "deletions": None, "files": None}
            if i < STAT_COMMITS:
                # +/− and files per commit: GitHub's list has none; each commit is read once
                # (cached for an hour) — the newest STAT_COMMITS of them.
                try:
                    one = self._cached("commit", c["sha"], lambda sha=c["sha"]: self.adapter.commit_detail(sha))
                    fl = one.get("files") or []
                    stats = {"additions": sum(int(f.get("additions") or 0) for f in fl),
                             "deletions": sum(int(f.get("deletions") or 0) for f in fl),
                             "files": len(fl)}
                except ChangeReviewError:
                    pass
            out.append({**c, **stats, "fixes": [cid for cid, f in fixed.items()
                                                if f.get("sha") and str(c["sha"]).startswith(str(f["sha"]))]})
        return out

    def commit(self, sha: str) -> dict[str, Any]:
        full = self.ref(sha)
        got = self._cached("commit", full, lambda: self.adapter.commit_detail(full))
        return {**got, "fixes": [cid for cid, f in self._fixed().items() if f.get("sha")
                                 and str(got.get("sha") or "").startswith(str(f["sha"]))],
                "stale": self.stale}

    def tree(self, ref: Optional[str]) -> dict[str, Any]:
        sha = self.ref(ref)
        paths = self._cached("tree", sha, lambda: self.adapter.list_paths(sha))
        try:
            changed = {f["path"]: f["status"] for f in self.files()["files"]}
        except ChangeReviewError:
            changed = {}
        letter = {"added": "A", "removed": "D", "deleted": "D", "renamed": "R"}
        counts: dict[str, dict[str, int]] = {}
        for t in self.threads():
            if t.get("path"):
                slot = counts.setdefault(t["path"], {"threads": 0, "open": 0})
                slot["threads"] += 1
                slot["open"] += 0 if t["state"] == "resolved" else 1
        items = [{"path": p, "status": letter.get(changed[p], "M") if p in changed else None,
                  "comments": counts.get(p, {}).get("threads", 0),
                  "open_comments": counts.get(p, {}).get("open", 0)} for p in paths]
        present = set(paths)
        for p, s in changed.items():
            if s in ("removed", "deleted") and p not in present:
                items.append({"path": p, "status": "D", "comments": 0, "open_comments": 0})
        return {"ref": str(ref or "head"), "sha": sha, "head_sha": self.head(),
                "base_sha": self.base(), "items": items, "stale": self.stale}

    def blob(self, path: str, ref: Optional[str]) -> dict[str, Any]:
        rel = local_review.clean_path(path) if path else ""
        if not rel:
            raise ChangeReviewError("Give a file path inside the repository.", 422, "bad_path")
        sha = self.ref(ref)

        def fetch() -> dict[str, Any]:
            data = self.adapter.read_blob(sha, rel)
            if data is None:
                return {"missing": True}
            size = len(data)
            if size > local_review.FILE_CAP_BYTES:
                return {"size": size, "too_large": True}
            if b"\x00" in data[:8000]:
                return {"size": size, "binary": True}
            return {"size": size, "text": data.decode("utf-8", "replace")}
        got = self._cached("blob", f"{sha}:{rel}", fetch)
        out: dict[str, Any] = {"path": rel, "ref": str(ref or "head"), "sha": sha,
                               "language": local_review.language_of(rel), "size": got.get("size"),
                               "stale": self.stale}
        if got.get("missing"):
            raise ChangeReviewError(f"{rel} is not in this change at {sha[:7]}.", 404, "no_file")
        if got.get("too_large"):
            return {**out, "too_large": True, "binary": False, "text": None,
                    "message": f"{rel} is {int(got['size']) // 1024} KB; files over 1 MB are not "
                               "shown here."}
        if got.get("binary"):
            return {**out, "binary": True, "too_large": False, "text": None,
                    "message": f"{rel} is a binary file."}
        body = str(got.get("text") or "")
        return {**out, "binary": False, "too_large": False, "text": body,
                "lines": body.count("\n") + (0 if body.endswith("\n") or not body else 1)}

    def checks(self) -> Optional[dict[str, Any]]:
        head = self.head()
        items = self._cached("checks", head or "pr",
                             lambda: self.adapter.commit_checks(head) if head else [])
        run = self.run_id()
        r = self.c.execute(text(
            "SELECT status, state->'gates' AS gates, state->>'tests_passed' AS tests "
            "FROM code_task_runs WHERE id = :r"), {"r": run}).mappings().first() if run else None
        if not r and not items:
            return None
        return {"where": "BOSS's sandbox and GitHub" if r else "GitHub",
                "run_status": r["status"] if r else None, "tests_passed": r["tests"] if r else None,
                "gates": r["gates"] if r else None,
                "items": [{"name": i.get("name"), "status": i.get("status"),
                           "conclusion": i.get("conclusion"), "app": i.get("app")} for i in items]}

    # comments
    def _outbox(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.c.execute(text(
            "SELECT * FROM change_review_outbox WHERE project_id = :p AND change_key = :k "
            "AND status IN ('queued', 'failed') ORDER BY id"),
            {"p": self.project_id, "k": self.key}).mappings()]

    @staticmethod
    def _read_body(body: str, login: str, bot: bool) -> tuple[str, str, str, str]:
        """(author, author_kind, severity, body) of a GitHub comment as BOSS shows it."""
        author, kind = login, ("agent" if bot else "human")
        m = _FOR.match(body or "")
        if m:
            author, kind, body = m.group(1), "human", body[m.end():]
        carried = _CARRIED.match(body or "")
        if carried:
            author, kind = carried.group(2), ("agent" if carried.group(1) else "human")
            body = body[carried.end():]
        sev = "major"
        s = _SEV_READ.match(body or "")
        if s:
            sev = {"should fix": "minor", "nit": "nit"}.get(s.group(1).lower(), "major")
            body = body[s.end():]
        return author, kind, sev, body

    def threads(self) -> list[dict[str, Any]]:
        def fetch() -> dict[str, Any]:
            comments = self.adapter.pull_request_review_comments(self.number)
            try:
                resolved = self.adapter.review_thread_states(self.number)
            except Exception:  # noqa: BLE001 — GraphQL refused: every thread reads open
                resolved = {}
            try:
                talk = self.adapter.pull_request_issue_comments(self.number)
            except Exception:  # noqa: BLE001
                talk = []
            return {"comments": comments, "resolved": {str(k): v for k, v in resolved.items()},
                    "conversation": talk}
        self._flush_outbox()
        try:
            got = self._cached("threads", "all", fetch)
        except ChangeReviewError as error:
            if error.status != 503:
                raise
            got = {"comments": [], "resolved": {}, "conversation": []}
            self.stale = True
        fixed = self._fixed()
        rows: list[dict[str, Any]] = []
        for cm in got.get("comments") or []:
            author, kind, sev, body = self._read_body(str(cm.get("body") or ""),
                                                      str(cm.get("user") or ""), bool(cm.get("bot")))
            line = cm.get("line")
            rows.append({
                "id": int(cm["id"]), "path": cm.get("path"), "line": line or cm.get("original_line"),
                "end_line": None, "side": "old" if str(cm.get("side") or "").upper() == "LEFT" else "new",
                "commit_sha": cm.get("commit_id"), "author": author, "author_kind": kind,
                "severity": sev, "severity_words": local_review.SEVERITY_WORDS.get(sev, sev),
                "body": body, "state": "open", "reply_to": cm.get("in_reply_to_id"),
                "origin": "github", "outdated": line is None, "original_line": cm.get("original_line"),
                "line_text": None, "run_round": None, "response_commit": None,
                "github_comment_id": int(cm["id"]), "resolved_by": None, "resolved_at": None,
                "created_at": cm.get("created_at"), "updated_at": cm.get("updated_at"),
                "kind": "line", "queued": False,
                # An outdated comment links the commit it was made on.
                "made_on": cm.get("original_commit_id") or cm.get("commit_id"),
                "snippet": hunk_snippet(str(cm.get("diff_hunk") or ""),
                                        cm.get("original_line") or line,
                                        cm.get("original_commit_id") or cm.get("commit_id"))})
        for cm in got.get("conversation") or []:
            author, kind, sev, body = self._read_body(str(cm.get("body") or ""),
                                                      str(cm.get("user") or ""), bool(cm.get("bot")))
            rows.append({
                "id": int(cm["id"]), "path": None, "line": None, "end_line": None, "side": "new",
                "commit_sha": None, "author": author, "author_kind": kind, "severity": sev,
                "severity_words": local_review.SEVERITY_WORDS.get(sev, sev), "body": body,
                "state": "open", "reply_to": None, "origin": "github", "outdated": False,
                "original_line": None, "line_text": None, "run_round": None,
                "response_commit": None, "github_comment_id": int(cm["id"]), "resolved_by": None,
                "resolved_at": None, "created_at": cm.get("created_at"),
                "updated_at": cm.get("updated_at"), "kind": "conversation", "queued": False})
        roots = {r["id"]: {**r, "replies": []} for r in rows if not r["reply_to"]}
        for r in rows:
            if r["reply_to"] and r["reply_to"] in roots:
                roots[r["reply_to"]]["replies"].append(r)
        for rid, t in roots.items():
            if got.get("resolved", {}).get(str(rid)):
                t["state"] = "resolved"
            f = fixed.get(rid)
            t["fixed_in"] = None
            if f:
                t["response_commit"] = f.get("sha")
                if t["state"] != "resolved":
                    t["state"] = "disputed" if f.get("kind") == "disputed" else "addressed"
                if f.get("kind") != "disputed":
                    t["fixed_in"] = f.get("sha")
        # Written here while GitHub did not answer: shown as queued until it is posted.
        for o in self._outbox():
            p = o["payload"] or {}
            if isinstance(p, str):
                p = json.loads(p or "{}")
            if o["kind"] == "comment":
                roots[-int(o["id"])] = {
                    "id": -int(o["id"]), "path": p.get("path"), "line": p.get("line"),
                    "end_line": None, "side": p.get("side") or "new",
                    "commit_sha": p.get("commit_sha"), "author": o["author"],
                    "author_kind": "human", "severity": p.get("severity") or "major",
                    "severity_words": local_review.SEVERITY_WORDS.get(p.get("severity") or "major",
                                                                      "must fix"),
                    "body": p.get("body") or "", "state": "open", "reply_to": None,
                    "origin": "console", "outdated": False, "original_line": p.get("line"),
                    "line_text": None, "run_round": None, "response_commit": None,
                    "github_comment_id": None, "resolved_by": None, "resolved_at": None,
                    "created_at": _iso(o["created_at"]), "updated_at": _iso(o["created_at"]),
                    "kind": "line" if p.get("path") else "conversation", "queued": True,
                    "queue_error": o.get("last_error"), "replies": [], "fixed_in": None}
            elif o["kind"] == "reply" and int(p.get("reply_to") or 0) in roots:
                roots[int(p["reply_to"])]["replies"].append({
                    "id": -int(o["id"]), "author": o["author"], "author_kind": "human",
                    "body": p.get("body") or "", "origin": "console", "queued": True,
                    "created_at": _iso(o["created_at"]), "reply_to": int(p["reply_to"]),
                    "path": None, "line": None, "side": "new", "state": "open",
                    "severity": "major", "severity_words": "must fix", "outdated": False})
        # PUBLISHED FROM A LOCAL CHANGE: what the publish could not post as a line comment (an
        # outdated line, a line outside the diff — carried in the review body — or not carried
        # at all) is still shown, marked "kept from before publish".
        if self.carried_from is not None:
            for t in local_review.threads(self.c, self.carried_from.row["id"]):
                gid = t.get("github_comment_id")
                if gid and int(gid) > 0:
                    continue
                kept_id = -(KEPT_ID_BASE + int(t["id"]))
                roots[kept_id] = {**t, "id": kept_id, "kept": True, "queued": False,
                                  "snippet": self.carried_from._snippet(t, {}),  # noqa: SLF001
                                  "kind": "line" if t.get("path") else "conversation",
                                  "fixed_in": None, "origin": t.get("origin") or "console",
                                  "in_review_body": bool(gid and int(gid) < 0),
                                  "replies": [{**r, "kept": True} for r in t.get("replies") or []]}
        order = {"open": 0, "disputed": 1, "addressed": 2, "resolved": 3}
        return sorted(roots.values(), key=lambda t: (t["state"] == "resolved",
                                                     t["author_kind"] != "human",
                                                     order.get(t["state"], 9), abs(t["id"])))

    def comment_summary(self, threads: Optional[list[dict[str, Any]]] = None) -> dict[str, int]:
        ts = threads if threads is not None else self.threads()
        human = [t for t in ts if t["author_kind"] == "human"]
        return {"threads": len(ts), "unresolved": len([t for t in ts if t["state"] != "resolved"]),
                "human_open": len([t for t in human if t["state"] != "resolved"]),
                "human_unreplied": len([t for t in human if t["state"] == "open"])}

    def _poster(self, person_token: Optional[Callable[[], Optional[str]]]) -> tuple[Any, bool]:
        token = None
        if person_token is not None:
            try:
                token = person_token()
            except Exception:  # noqa: BLE001 — no linked account: the app posts it for them
                token = None
        return (person_adapter(self.adapter, token), True) if token else (self.adapter, False)

    @staticmethod
    def _signed(body: str, author: str, as_person: bool, severity: Optional[str] = None) -> str:
        lead = _SEV_LEAD.get(str(severity or ""), "")
        text_ = f"{lead}{body}"
        return text_ if as_person else f"_for {author} (via BOSS)_\n\n{text_}"

    def _queue(self, kind: str, payload: dict[str, Any], author: str, person: Optional[str],
               error: str) -> dict[str, Any]:
        oid = int(self.c.execute(text(
            "INSERT INTO change_review_outbox (project_id, change_key, repository, number, kind, "
            "payload, author, person, status, last_error) VALUES (:p, :k, :r, :n, :kind, "
            "CAST(:pl AS JSONB), :a, :w, 'queued', :e) RETURNING id"),
            {"p": self.project_id, "k": self.key, "r": self.full_name, "n": self.number,
             "kind": kind, "pl": json.dumps(payload), "a": author, "w": person, "e": error[:300]}
        ).scalar())
        emit_event(self.c, self.project_id, "change_review.comment_queued", author, "pull_request",
                   self.url, {"outbox_id": oid, "kind": kind})
        return {"id": -oid, "queued": True, "state": "open", "author": author,
                "body": payload.get("body"), "path": payload.get("path"),
                "line": payload.get("line"), "message": "GitHub did not answer: BOSS posts it "
                                                        "when GitHub is back."}

    def _post(self, kind: str, payload: dict[str, Any], author: str,
              person_token: Optional[Callable[[], Optional[str]]]) -> Any:
        poster, as_person = self._poster(person_token)
        if kind == "comment":
            body = self._signed(payload["body"], author, as_person, payload.get("severity"))
            if payload.get("path"):
                return poster.create_review_comment(
                    self.number, body=body, commit_id=payload.get("commit_sha") or self.head(),
                    path=payload["path"], line=int(payload["line"]),
                    side="LEFT" if payload.get("side") == "old" else "RIGHT")
            return poster.create_issue_comment(self.number, body).id
        if kind == "reply":
            body = self._signed(payload["body"], author, as_person)
            if payload.get("conversation"):
                return poster.create_issue_comment(self.number, body).id
            return poster.reply_to_review_comment(self.number, int(payload["reply_to"]), body)
        if kind in ("resolve", "reopen"):
            return poster.set_review_thread_resolved(self.number, int(payload["comment_id"]),
                                                     kind == "resolve")
        raise ChangeReviewError(f"Unknown outbox kind {kind}.")

    def _flush_outbox(self) -> int:
        posted = 0
        for o in self._outbox():
            if o["status"] != "queued":
                continue
            p = o["payload"] or {}
            if isinstance(p, str):
                p = json.loads(p or "{}")
            try:
                got = self._post(o["kind"], p, o["author"], None)
            except Exception as error:  # noqa: BLE001 — still down: try on the next read
                status = _http_status(error)
                self.c.execute(text(
                    "UPDATE change_review_outbox SET attempts = attempts + 1, last_error = :e, "
                    "status = CASE WHEN :final THEN 'failed' ELSE status END, updated_at = now() "
                    "WHERE id = :i"),
                    {"e": f"{type(error).__name__}: {str(error)[:200]}", "i": o["id"],
                     "final": status is not None and 400 <= status < 500 and status != 429})
                if status is None or status >= 500:
                    break
                continue
            self.c.execute(text("UPDATE change_review_outbox SET status = 'posted', posted_id = :g, "
                                "updated_at = now() WHERE id = :i"),
                           {"g": str(got) if got is not None else None, "i": o["id"]})
            posted += 1
        if posted:
            self._drop("threads")
        return posted

    def _write(self, kind: str, payload: dict[str, Any], author: str, person: Optional[str],
               person_token: Optional[Callable[[], Optional[str]]]) -> Any:
        try:
            got = self._post(kind, payload, author, person_token)
        except ChangeReviewError as error:
            if error.status != 503:
                raise
            return self._queue(kind, payload, author, person, str(error))
        except Exception as error:  # noqa: BLE001
            status = _http_status(error)
            if status is not None and 400 <= status < 500 and status != 429:
                raise ChangeReviewError(f"GitHub refused it ({status}): {str(error)[:200]}",
                                        422 if status == 422 else 409, "github_refused") from None
            return self._queue(kind, payload, author, person,
                               f"{type(error).__name__}: {str(error)[:200]}")
        self._drop("threads")
        return got

    def add_comment(self, *, body: str, author: str, author_kind: str, path: Any, line: Any,
                    side: str, commit_sha: Optional[str], severity: Any, origin: str = "console",
                    end_line: Any = None, person_token: Any = None, person: Optional[str] = None,
                    principal_id: Optional[str] = None) -> dict[str, Any]:
        if local_review.is_host_tools(author, principal_id):
            raise ChangeReviewError(local_review.HOST_TOOLS_REFUSAL, 403, "host_tools_author")
        words = str(body or "").strip()
        if not words:
            raise ChangeReviewError("Write the comment first.", 422, "empty_comment")
        rel = local_review.clean_path(path) if path not in (None, "") else None
        if rel is None and line not in (None, ""):
            raise ChangeReviewError("A line needs its file.", 422, "bad_anchor")
        if rel is not None and line in (None, ""):
            raise ChangeReviewError("A comment on a GitHub file needs its line.", 422, "bad_anchor")
        try:
            sev = local_review.severity_of(severity)
        except local_review.ReviewError as error:
            raise ChangeReviewError(str(error), error.status, error.error) from None
        payload = {"body": words, "path": rel, "line": int(line) if rel else None,
                   "side": "old" if str(side or "new").lower() in ("old", "left") else "new",
                   "commit_sha": commit_sha or None, "severity": sev}
        got = self._write("comment", payload, author, person, person_token)
        if isinstance(got, dict):
            return got
        set_viewed(self.c, self.project_id, self.key, person or author, path="",
                   sha=self.head())
        return {"id": int(got) if got is not None else None, "queued": False, "state": "open",
                "author": author, "body": words, "path": rel, "line": payload["line"]}

    def reply(self, comment_id: int, *, body: str, author: str, author_kind: str,
              origin: str = "console", person_token: Any = None, person: Optional[str] = None,
              principal_id: Optional[str] = None) -> dict[str, Any]:
        if local_review.is_host_tools(author, principal_id):
            raise ChangeReviewError(local_review.HOST_TOOLS_REFUSAL, 403, "host_tools_author")
        words = str(body or "").strip()
        if not words:
            raise ChangeReviewError("Write the reply first.", 422, "empty_comment")
        root = next((t for t in self.threads() if t["id"] == int(comment_id)), None)
        if root is None:
            raise ChangeReviewError(f"There is no comment {comment_id} on this PR.", 404,
                                    "no_comment")
        payload = {"body": words, "reply_to": int(comment_id),
                   "conversation": root.get("kind") == "conversation"}
        if payload["conversation"]:
            payload["body"] = f"> {root['body'].splitlines()[0][:200] if root['body'] else ''}\n\n{words}"
        got = self._write("reply", payload, author, person, person_token)
        if isinstance(got, dict):
            return got
        return {"id": int(got) if got is not None else None, "queued": False, "author": author,
                "body": words, "reply_to": int(comment_id)}

    def resolve(self, comment_id: int, *, actor: str, reopen: bool = False,
                person_token: Any = None, person: Optional[str] = None) -> dict[str, Any]:
        root = next((t for t in self.threads() if t["id"] == int(comment_id)), None)
        if root is None:
            raise ChangeReviewError(f"There is no comment {comment_id} on this PR.", 404,
                                    "no_comment")
        if root.get("kind") == "conversation":
            raise ChangeReviewError("GitHub resolves line comments only; reply to this one "
                                    "instead.", 422, "not_resolvable")
        got = self._write("reopen" if reopen else "resolve", {"comment_id": int(comment_id)},
                          actor, person, person_token)
        if isinstance(got, dict):
            return got
        emit_event(self.c, self.project_id, "change_review.thread_reopened" if reopen else
                   "change_review.thread_resolved", actor, "pull_request", self.url,
                   {"comment_id": int(comment_id)})
        return {"id": int(comment_id), "state": "open" if reopen else "resolved",
                "resolved_by": None if reopen else actor}

    def request_changes(self, *, note: str, actor: str, jobs: Any, decisions: Any = None
                        ) -> dict[str, Any]:
        """FIX N OPEN COMMENTS on a GitHub PR: a fix run on the PR's own branch (a follow-up of
        the build that opened it), the people's open comments its required inputs."""
        from . import code_task_service, review_comments

        meta = self.meta()
        if meta.get("merged") or str(meta.get("state")) != "open":
            raise ChangeReviewError(f"PR #{self.number} is not open.", 409, "not_open")
        parent = self.run_id()
        if not parent:
            raise ChangeReviewError("No BOSS task built this PR, so there is no agent to fix it: "
                                    "start a build from Factory.", 409, "no_builder")
        open_threads = [t for t in self.threads() if t["author_kind"] == "human"
                        and t["state"] not in ("resolved",) and not t.get("queued")
                        and t["id"] > 0]
        queued = take_queued_notes(self.c, self.project_id, self.key)
        words = "\n\n".join(p for p in (str(note or "").strip(),
                                         f"From the chat:\n{queued}" if queued else "") if p)
        if not words and not open_threads:
            raise ChangeReviewError("Say what to change, or comment on a line first.", 422,
                                    "note_required")
        rcs = []
        for t in open_threads:
            rc = review_comments.ReviewComment(
                severity=t["severity"], body=t["body"], path=t.get("path"), line=t.get("line"),
                end_line=None, author=t["author"], author_kind=review_comments.AUTHOR_HUMAN,
                stage="person")
            rc.ref = f"{GH_REF_PREFIX}{int(t['id'])}"
            rcs.append(rc)
        ordered = review_comments.implementer_order(rcs)
        parts = [f"FIX PR #{self.number} ON ITS OWN BRANCH {meta.get('head_ref')} "
                 f"(“{meta.get('title') or ''}”). A person reviewed it and asked for changes."]
        if words:
            parts.append(f"THEIR NOTE:\n{words}")
        if ordered:
            parts.append("THE PEOPLE'S LINE COMMENTS ARE REQUIRED INPUTS: answer every one.\n"
                         + review_comments.render_for_implementer(ordered))
            parts.append(review_comments.response_clause())
        entries = [{**rc.to_dict(), "id": rc.ref, "status": review_comments.STATUS_OPEN,
                    "round": 0, "required": True} for rc in ordered]
        # The PR's review decision stays as it is: a GitHub review is not sent back
        # (decision_actions offers no revise for it); the fix run's new head asks it again.
        decision = _decision_for(self.c, self.url)
        try:
            started = code_task_service.start_followup_task(
                self.c, parent_run_id=str(parent), instructions="\n\n".join(parts), jobs=jobs,
                actor=actor, title=f"Fix PR #{self.number}: {meta.get('title') or ''}"[:160],
                finalize_extra={"review_comments": entries,
                                "github_review_fix": {"url": self.url, "number": self.number,
                                                      "repository": self.full_name}})
        except ValueError as error:
            raise ChangeReviewError(str(error), 409, "fix_refused") from None
        mark_notes_sent(self.c, self.project_id, self.key, started.get("run_id"))
        emit_event(self.c, self.project_id, "change_review.fix_run_started", actor, "pull_request",
                   self.url, {"run_id": started.get("run_id"), "required": len(entries)})
        return {"number": self.number, "decision_id": decision["id"] if decision else None,
                "status": "fix_run_started", "run_id": started.get("run_id"),
                "required": [int(t["id"]) for t in open_threads]}


def snippet_of(lines: Optional[list[str]], line: int, commit: str, around: int = 2
               ) -> Optional[dict[str, Any]]:
    """Up to 5 lines around `line` (1-based) of a file's text: {start, line, lines, commit}."""
    if not lines or line < 1 or line > len(lines):
        return None
    start = max(1, line - around)
    end = min(len(lines), line + around)
    return {"start": start, "line": line, "lines": [x[:400] for x in lines[start - 1:end]],
            "commit": commit}


def hunk_snippet(hunk: str, line: Any, commit: Any) -> Optional[dict[str, Any]]:
    """A GitHub review comment's code from its diff_hunk: the hunk ends at the commented line,
    so its last lines are the snippet (the +/− marks dropped), the last one highlighted."""
    rows = [r for r in hunk.split("\n") if r and not r.startswith("@@")]
    if not rows:
        return None
    tail = [r[1:] if r[:1] in "+- " else r for r in rows[-5:]]
    ln = int(line) if isinstance(line, int) or str(line or "").isdigit() else None
    return {"start": (ln - len(tail) + 1) if ln else None, "line": ln, "lines": [x[:400] for x in tail],
            "commit": commit, "marks": [r[:1] for r in rows[-5:]]}


class _Unreachable(RuntimeError):
    """The adapter could not be made (no token, the connection is down): read from the cache."""


def published_pr(row: Mapping[str, Any]) -> Optional[tuple[str, str, int]]:
    """The GitHub pull request a local change set was published as, if any."""
    m = _GH_PR.match(str(row.get("github_pr_url") or ""))
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def source_for(c: Any, settings: Any, project_id: str, number: int, *, owner: Optional[str] = None,
               repo: Optional[str] = None, engine: Any = None, follow_publish: bool = True) -> Any:
    """The change's source. A local change set that was PUBLISHED to GitHub is read from its
    pull request (same page, same old link), its local comments carried along."""
    if owner and repo:
        return GitHubSource(c, settings, project_id, owner, repo, number, engine=engine)
    local = LocalSource(c, settings, project_id, number, engine=engine)
    pr = published_pr(local.row) if follow_publish else None
    if pr and github_binding(c, project_id, pr[0], pr[1]) is not None:
        gh = GitHubSource(c, settings, project_id, pr[0], pr[1], pr[2], engine=engine)
        gh.carried_from = local
        return gh
    return local


def summary_of(src: Any) -> dict[str, Any]:
    """The change page's summary. A published change whose PR GitHub cannot answer for (and BOSS
    has no copy of) still shows, from the local record, marked so."""
    try:
        return src.summary()
    except ChangeReviewError as error:
        local = getattr(src, "carried_from", None)
        if local is None or error.status != 503:
            raise
        view = local.summary()
        view.update({"stale": True, "published_unreachable": str(error),
                     "github_pr_url": local.row.get("github_pr_url")})
        return view


def agent_pane(c: Any, src: Any, person: str) -> dict[str, Any]:
    """The PR agent: the task that built the change (or its latest fix run), what it said on the
    comments, and the chat."""
    run = _run_view(c, src.agent_run_id())
    builder = _run_view(c, src.run_id())
    threads = src.threads()
    people = [t for t in threads if t["author_kind"] == "human"]
    msgs = chat_messages(c, src.project_id, src.key)
    for t in threads:
        for r in t.get("replies") or []:
            if r.get("author_kind") == "agent":
                msgs.append({"id": f"r{r['id']}", "author": r["author"], "author_kind": "agent",
                             "body": r["body"], "delivery": "reply", "run_id": None,
                             "created_at": r.get("created_at"),
                             "about": {"path": t.get("path"), "line": t.get("line"),
                                       "thread": t["id"]}})
    msgs.sort(key=lambda m: str(m.get("created_at") or ""))
    return {"run": run, "builder": builder, "agent": (builder or run or {}).get("builder"),
            "comments": [{"id": t["id"], "path": t.get("path"), "line": t.get("line"),
                          "body": t["body"].split("\n")[0][:200], "state": t["state"],
                          "fixed_in": t.get("fixed_in"), "author": t["author"],
                          "queued": bool(t.get("queued"))} for t in people],
            "open": len([t for t in people if t["state"] != "resolved"]),
            "fixable": len([t for t in people if t["state"] == "open" and t["id"] > 0]),
            "messages": msgs, "viewed": viewed(c, src.project_id, src.key, person)}


def send_message(c: Any, src: Any, *, body: str, author: str, actor: str) -> dict[str, Any]:
    """A person's message to the PR agent: steered into a running task, else kept for the next
    fix run (the pane says which)."""
    words = str(body or "").strip()
    if not words:
        raise ChangeReviewError("Write the message first.", 422, "empty_message")
    if len(words) > 4000:
        raise ChangeReviewError("A message is at most 4,000 characters.", 422, "too_long")
    run = _run_view(c, src.agent_run_id())
    delivery = "queued"
    if run and run["running"]:
        queue_guidance(c, run["run_id"], words, actor)
        delivery = "steered"
    mid = int(c.execute(text(
        "INSERT INTO change_review_chat (project_id, change_key, author, author_kind, body, "
        "delivery, run_id) VALUES (:p, :k, :a, 'human', :b, :d, :r) RETURNING id"),
        {"p": src.project_id, "k": src.key, "a": author, "b": words, "d": delivery,
         "r": run["run_id"] if run and delivery == "steered" else None}).scalar())
    emit_event(c, src.project_id, "change_review.agent_message", actor, "change", src.key,
               {"delivery": delivery, "run_id": run["run_id"] if run else None})
    return {"id": mid, "delivery": delivery, "run_id": run["run_id"] if run else None,
            "message": ("Sent to the running task: it reads it at its next step."
                        if delivery == "steered" else
                        "Kept for the agent: it goes with the next fix run.")}


# ── the project's history: the default branch, merged work included ───────────────────────────

_MERGED_PR = re.compile(r"(?:Merge pull request #|\(#)(\d+)")


def _history_target(c: Any, project_id: str, repo: Optional[str]) -> dict[str, Any]:
    """Which repository the history reads: the one named, else the project's primary."""
    rows = [dict(r) for r in c.execute(text(
        "SELECT full_name, owner, name, COALESCE(host, 'github') AS host, role, default_branch, id "
        "FROM github_repository_bindings WHERE project_id = :p AND status = 'active' "
        "ORDER BY (role = 'primary') DESC, created_at"), {"p": project_id}).mappings()]
    if repo:
        rows = [r for r in rows if str(r["full_name"]).lower() == str(repo).lower()]
    if not rows:
        raise ChangeReviewError("This project has no repository to show history for.", 404,
                                "no_repository")
    return rows[0]


def _local_change_for_merge(c: Any, project_id: str, sha: str) -> Optional[dict[str, Any]]:
    row = c.execute(text(
        "SELECT number, github_pr_url FROM local_change_sets WHERE project_id = :p AND "
        "merged_sha = :s ORDER BY created_at DESC LIMIT 1"), {"p": project_id, "s": sha}
    ).mappings().first()
    if row is None:
        return None
    return {"source": "local", "number": int(row["number"]),
            "href": f"/changes/{project_id}/{int(row['number'])}"}


def history(c: Any, settings: Any, project_id: str, *, repo: Optional[str] = None,
            branch: Optional[str] = None, limit: int = 50) -> dict[str, Any]:
    """The default branch's commits, newest first; a merge says which change or PR it merged."""
    t = _history_target(c, project_id, repo)
    limit = max(1, min(int(limit or 50), 200))
    if t["host"] == "local":
        git_dir = local_repo.bare_path(t["owner"], t["name"], settings)
        ref = branch or local_repo.default_branch(git_dir)
        if not local_repo.branch_exists(git_dir, ref):
            raise ChangeReviewError(f"There is no branch {ref}.", 404, "no_branch")
        out = local_repo.git(["log", f"--max-count={limit}", "--format=%H%x09%P%x09%an%x09%aI%x09%s",
                              f"refs/heads/{ref}"], git_dir=git_dir, check=False)
        items = []
        for line in out.splitlines():
            parts = line.split("\t", 4)
            if len(parts) < 5:
                continue
            sha, parents, author, date, subject = parts
            ps = parents.split()
            change = _local_change_for_merge(c, project_id, sha)
            # A merge: two parents, or the commit a change landed as (a squash or fast-forward).
            items.append({"sha": sha, "parents": ps, "merge": len(ps) > 1 or change is not None,
                          "author": author, "date": date, "message": subject, "change": change})
        return {"project_id": project_id, "repository": t["full_name"], "source": "local",
                "branch": ref, "items": items, "stale": False}
    adapter = github_adapter_for(c, settings, project_id, str(t["id"]))
    ref = branch or t.get("default_branch") or "main"
    rows = adapter.branch_commits(ref, limit=limit)
    items = []
    for r in rows:
        change = _local_change_for_merge(c, project_id, r["sha"])
        m = _MERGED_PR.search(r.get("message") or "")
        if change is None and m:
            change = {"source": "github", "number": int(m.group(1)),
                      "href": f"/changes/{project_id}/{int(m.group(1))}?repo={t['full_name']}"}
        items.append({**r, "merge": len(r.get("parents") or []) > 1 or change is not None,
                      "change": change})
    return {"project_id": project_id, "repository": t["full_name"], "source": "github",
            "branch": ref, "items": items, "stale": False}


def history_commit(c: Any, settings: Any, project_id: str, sha: str, *,
                   repo: Optional[str] = None) -> dict[str, Any]:
    """One commit of the project's repository: its own changes (a merge: what it brought in,
    against its first parent), and the change or PR it merged."""
    if not _SHA.match(str(sha or "")):
        raise ChangeReviewError("Give a commit sha.", 422, "bad_sha")
    t = _history_target(c, project_id, repo)
    if t["host"] == "local":
        git_dir = local_repo.bare_path(t["owner"], t["name"], settings)
        full = local_repo.rev_parse(git_dir, sha)
        if not full:
            raise ChangeReviewError(f"There is no commit {sha[:7]} in {t['full_name']}.", 404,
                                    "no_commit")
        parents = local_repo.git(["log", "-1", "--format=%P", full], git_dir=git_dir).split()
        info = local_repo.git(["log", "-1", "--format=%an%x09%aI%x09%B", full],
                              git_dir=git_dir).split("\t", 2)
        if parents:
            diff = local_repo.git(["diff", "--no-color", parents[0], full], git_dir=git_dir)
            files = local_repo.changed_files(git_dir, parents[0], full)
        else:
            diff = local_repo.git(["show", "--no-color", "--format=", full], git_dir=git_dir)
            files = []
        view = {"sha": full, "parent": parents[0] if parents else None, "parents": parents,
                "author": info[0] if len(info) > 2 else "", "date": info[1] if len(info) > 2 else None,
                "message": (info[2] if len(info) > 2 else "").strip(), "files": files,
                "diff": diff[:400_000]}
    else:
        adapter = github_adapter_for(c, settings, project_id, str(t["id"]))
        view = adapter.commit_detail(sha)
        view.setdefault("parents", [view["parent"]] if view.get("parent") else [])
    change = _local_change_for_merge(c, project_id, view["sha"])
    m = _MERGED_PR.search(view.get("message") or "")
    if change is None and m and t["host"] != "local":
        change = {"source": "github", "number": int(m.group(1)),
                  "href": f"/changes/{project_id}/{int(m.group(1))}?repo={t['full_name']}"}
    return {**view, "merge": len(view.get("parents") or []) > 1 or change is not None,
            "change": change,
            "repository": t["full_name"], "source": t["host"]}
