"""THE LOCAL REPO HOST (BOSSAPP-918/919): a local repository behind the same interface as GitHub.

The pipeline talks to "the repository" through one object, `SoftwareFactoryService.github` — branch,
files, pull request, review, merge. `LocalGitAdapter` is that object for a repository BOSS keeps on
this machine (`local_repo`): a stage's commit is a real git commit in the bare repository, a pull
request is a LOCAL CHANGE SET (`local_change_sets`: the branch, its diff against the base, the
review panel's comments, the sandbox checks), and Merge is a real fast-forward or merge commit into
the default branch. Nothing in the pipeline forks for it: GitHub projects keep using GitHub's REST
path through `GitHubAdapter`, local projects use this, and the code between them is the same.

The same adapter serves a GitHub repository's local MIRROR while GitHub is down (BOSSAPP-922):
`ResilientGitHub` hands the build to it after GitHub's retries are spent, and the change set it
opens is marked `pending_push` for the worker's push-pending job.

A local change set's URL is `http://boss-local/<owner>/<repo>/pull/<number>`: every reader that
parses a pull-request URL (ci_gate.parse_pr_url, decisions, factory_view) reads it as it reads
GitHub's, and the console draws the host `boss-local` as the change set's own page.

A GitHub-only operation (Actions, a tarball, a repository's settings) is not faked: the adapter
does not have it (an AttributeError, so `hasattr` feature checks stay honest), and a GitHub REST
path sent to `_request` raises `LocalHostUnsupported` with words.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy import text

from . import local_repo
from .github_adapter import (
    DeploymentResult,
    DeploymentStatusResult,
    IssueCommentResult,
    PullRequestMergeResult,
    PullRequestResult,
    PullRequestReviewResult,
    PullRequestValidationCheck,
    PullRequestValidationResult,
)

log = logging.getLogger(__name__)

LOCAL_PR_HOST = "boss-local"


class LocalHostUnsupported(RuntimeError):
    """A GitHub-only operation asked of a local repository."""


def change_set_url(full_name: str, number: int) -> str:
    return f"http://{LOCAL_PR_HOST}/{full_name}/pull/{int(number)}"


def is_local_pr_url(url: Any) -> bool:
    return f"://{LOCAL_PR_HOST}/" in str(url or "")


def _engine(settings: Any):
    from ..db import get_engine

    return get_engine(settings)


def _head_of(row: Mapping[str, Any], git_dir: Path) -> str:
    """The change set's head: its branch while it is open; once merged or closed, the head it
    had then — a later build on the same branch moves the branch, never a closed change."""
    if row.get("state") == "open" and local_repo.branch_exists(git_dir, row["branch"]):
        return str(row["branch"])
    return str(row.get("head_sha") or row["branch"])


def _note_key(comments: Any) -> str:
    """A note's inline comments as words and places, without the ids each posting mints."""
    if isinstance(comments, str):
        try:
            comments = json.loads(comments)
        except ValueError:
            comments = []
    return json.dumps(sorted(json.dumps({k: v for k, v in (c or {}).items() if k != "id"},
                                        sort_keys=True, default=str)
                             for c in (comments or []) if isinstance(c, dict)))


class _NotFound(Exception):
    """What a GitHub 404 is to the readers that probe for a file (ci_gate.has_workflows)."""

    def __init__(self, message: str):
        super().__init__(message)
        import httpx

        request = httpx.Request("GET", "http://boss-local/")
        self.response = httpx.Response(404, request=request)


class LocalGitAdapter:
    """GitHubAdapter's surface over a bare repository on this machine."""

    kind = "local"
    supports_draft_pull_requests = False

    def __init__(self, settings: Any, *, owner: str, repo: str, project_id: Optional[str] = None,
                 git_dir: Optional[Path] = None, engine: Any = None,
                 pending_push: bool = False):
        self.settings = settings
        self.owner = str(owner)
        self.repo = str(repo)
        self.project_id = project_id or (local_repo.project_of(self.owner)
                                         if local_repo.is_local_owner(self.owner) else None)
        self.git_dir = Path(git_dir) if git_dir else local_repo.bare_path(self.owner, self.repo, settings)
        self._engine = engine
        #: True for a GitHub repository's mirror during an outage: what it opens waits for a push.
        self.pending_push = pending_push
        self.api_base_url = "local"

    # -- identity ---------------------------------------------------------------------------
    @property
    def has_hosted_ci(self) -> bool:
        """THE HOST CAPABILITY ci_gate decides by (BOSSAPP-946): a repository on this machine has
        no hosted CI; BOSS's sandbox checks are its CI. A GitHub repository's MIRROR during an
        outage is still a GitHub repository: its CI runs after the push, so it is hosted."""
        return bool(self.pending_push)

    @property
    def repository_full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def engine(self):
        if self._engine is None:
            self._engine = _engine(self.settings)
        return self._engine

    def __repr__(self) -> str:
        return f"LocalGitAdapter({self.repository_full_name!r}, {str(self.git_dir)!r})"

    def clone_url(self, token: str = "") -> str:
        return local_repo.file_url(self.git_dir)

    def _auth_token(self) -> str:
        return ""

    def _require(self) -> Path:
        if not (self.git_dir / "HEAD").is_file():
            raise local_repo.LocalRepoError(
                f"The local repository {self.repository_full_name} is missing on this machine "
                f"({self.git_dir}). Onboarding can make it again.")
        return self.git_dir

    # -- branches and files -----------------------------------------------------------------
    def default_branch(self) -> str:
        return local_repo.default_branch(self._require())

    def default_branch_sha(self) -> tuple[str, str]:
        branch = self.default_branch()
        return branch, local_repo.branch_sha(self.git_dir, branch) or ""

    def repo_metadata(self) -> dict:
        return {"full_name": self.repository_full_name, "default_branch": self.default_branch(),
                "private": True, "host": "local"}

    def branch_exists(self, branch_name: str) -> bool:
        return local_repo.branch_exists(self._require(), branch_name)

    def ensure_branch(self, branch_name: str, base_branch: Optional[str] = None) -> str:
        return local_repo.ensure_branch(self._require(), branch_name, base_branch)

    def branch_snapshot(self, branch_name: str) -> dict:
        sha = local_repo.branch_sha(self._require(), branch_name)
        if not sha:
            raise _NotFound(f"no branch {branch_name}")
        return {"name": branch_name, "sha": sha, "protected": False}

    def upsert_file(self, branch_name: str, path: str, content: str, message: str) -> str:
        return self.commit_files(branch_name, {path: content}, message)

    def upsert_binary_file(self, branch_name: str, path: str, data: bytes, message: str) -> str:
        return self.commit_files(branch_name, {path: data}, message)

    def commit_files(self, branch_name: str, files: Mapping[str, Any], message: str) -> str:
        """Several files as ONE commit (the batch form GitHub's REST path has no equivalent of)."""
        from .agent_acts import sign_commit_message

        git_dir = self._require()
        local_repo.ensure_branch(git_dir, branch_name)
        return local_repo.commit_files(git_dir, branch_name, files, sign_commit_message(message))

    def delete_file(self, branch_name: str, path: str, message: str) -> str:
        return self.commit_files(branch_name, {path: None}, message)

    def read_text_file(self, branch_name: str, path: str) -> Optional[str]:
        return local_repo.read_file(self._require(), branch_name, path)

    def list_paths(self, branch_name: str, *, limit: int = 20000) -> list[str]:
        return local_repo.list_paths(self._require(), branch_name, limit=limit)

    def compare_changed_files(self, base: str, head: str, *args: Any, **kwargs: Any) -> list[dict]:
        return local_repo.changed_files(self._require(), base, head)

    def commit_checks(self, commit_sha: str) -> list[dict]:
        return []

    # -- change sets (pull requests) --------------------------------------------------------
    def _row(self, c: Any, number: int, *, lock: bool = False) -> dict[str, Any]:
        row = c.execute(text(
            "SELECT * FROM local_change_sets WHERE repository = :r AND number = :n"
            + (" FOR UPDATE" if lock else "")),
            {"r": self.repository_full_name, "n": int(number)}).mappings().first()
        if row is None:
            raise _NotFound(f"There is no change #{number} in {self.repository_full_name}.")
        return dict(row)

    def change_set(self, number: int) -> dict[str, Any]:
        with self.engine.begin() as c:
            return self._row(c, number)

    def create_pull_request(self, branch_name: str, title: str, body: str,
                            draft: Optional[bool] = None, base: Optional[str] = None,
                            run_id: Optional[str] = None) -> PullRequestResult:
        """Open (or refresh) the change set for this branch: one open change set per branch,
        as GitHub keeps one open PR per head."""
        # BOSSAPP-866: a secret value handed to the run never reaches the PR text.
        from .run_secret_redaction import scrub as _scrub_known

        title, body = _scrub_known(title), _scrub_known(body)
        from .core import make_id

        git_dir = self._require()
        base = base or self.default_branch()
        head_sha = local_repo.branch_sha(git_dir, branch_name)
        if not head_sha:
            raise local_repo.LocalRepoError(f"There is no branch {branch_name} to propose.")
        if run_id is None and branch_name.startswith("boss/code/"):
            run_id = branch_name.rsplit("/", 1)[-1]
        if not self.project_id:
            raise local_repo.LocalRepoError("A change set needs the project it belongs to.")
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                      {"k": f"local_change_sets:{self.repository_full_name}"})
            open_row = c.execute(text(
                "SELECT number FROM local_change_sets WHERE repository = :r AND branch = :b "
                "AND state = 'open' ORDER BY number DESC LIMIT 1"),
                {"r": self.repository_full_name, "b": branch_name}).scalar()
            if open_row is not None:
                c.execute(text(
                    "UPDATE local_change_sets SET body = :body, head_sha = :h, updated_at = now() "
                    "WHERE repository = :r AND number = :n"),
                    {"body": body, "h": head_sha, "r": self.repository_full_name, "n": int(open_row)})
                return PullRequestResult(url=change_set_url(self.repository_full_name, open_row),
                                         number=int(open_row))
            number = int(c.execute(text(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM local_change_sets WHERE repository = :r"),
                {"r": self.repository_full_name}).scalar() or 1)
            c.execute(text(
                "INSERT INTO local_change_sets (id, project_id, repository, number, branch, "
                "base_branch, title, body, draft, head_sha, base_sha, run_id, pending_push) "
                "VALUES (:id, :p, :r, :n, :b, :base, :t, :body, :d, :h, :bs, :run, :pp)"),
                {"id": make_id("lcs"), "p": self.project_id, "r": self.repository_full_name,
                 "n": number, "b": branch_name, "base": base, "t": (title or "")[:500],
                 "body": body or "", "d": bool(draft), "h": head_sha,
                 "bs": local_repo.branch_sha(git_dir, base), "run": run_id,
                 "pp": bool(self.pending_push)})
        return PullRequestResult(url=change_set_url(self.repository_full_name, number),
                                 number=number)

    def add_labels(self, pull_request_number: int, labels: list[str]) -> None:
        with self.engine.begin() as c:
            row = self._row(c, pull_request_number, lock=True)
            have = list(row.get("labels") or [])
            have += [label for label in labels if label not in have]
            c.execute(text("UPDATE local_change_sets SET labels = CAST(:l AS JSONB), updated_at = now() "
                           "WHERE id = :id"), {"l": json.dumps(have), "id": row["id"]})

    def mark_ready_for_review(self, pull_request_number: int) -> bool:
        with self.engine.begin() as c:
            c.execute(text("UPDATE local_change_sets SET draft = false, updated_at = now() "
                           "WHERE repository = :r AND number = :n"),
                      {"r": self.repository_full_name, "n": int(pull_request_number)})
        return True

    def update_pull_request_body(self, pull_number: int, body: str) -> None:
        with self.engine.begin() as c:
            c.execute(text("UPDATE local_change_sets SET body = :b, updated_at = now() "
                           "WHERE repository = :r AND number = :n"),
                      {"b": body, "r": self.repository_full_name, "n": int(pull_number)})

    def update_pull_request_branch(self, pull_request_number: int) -> bool:
        """Merge the base INTO the change set's branch (GitHub's "Update branch"). False on a
        real conflict, which needs an agent."""
        row = self.change_set(pull_request_number)
        try:
            local_repo.merge(self._require(), row["base_branch"], row["branch"],
                             f"Merge {row['base_branch']} into {row['branch']}")
            return True
        except local_repo.LocalRepoError:
            return False

    def open_pull_request_for(self, branch_name: str) -> Optional[dict]:
        """The open change set whose head is this branch ({number, url, title}), or None —
        GitHubAdapter.open_pull_request_for, for pr_follow_up.prepare_branch."""
        with self.engine.begin() as c:
            row = c.execute(text(
                "SELECT number, title FROM local_change_sets WHERE repository = :r "
                "AND branch = :b AND state = 'open' ORDER BY number DESC LIMIT 1"),
                {"r": self.repository_full_name, "b": branch_name}).mappings().first()
        if row is None:
            return None
        return {"number": int(row["number"]),
                "url": change_set_url(self.repository_full_name, row["number"]),
                "title": str(row["title"] or "")}

    def last_merged_pull_request(self, branch_name: str) -> Optional[dict]:
        """The newest MERGED change set from this branch, or None. A work branch outlives its
        first change set — the lineage's follow-ups keep committing to it — and the install's
        default squash merge leaves its commits off the base's history, so without
        pr_follow_up's restack every later change set from it is an add/add conflict that can
        never merge (BOSSAPP-980, helpdesk-laptop change #2, 2026-11-10)."""
        with self.engine.begin() as c:
            row = c.execute(text(
                "SELECT number, head_sha, merged_sha, merged_at FROM local_change_sets "
                "WHERE repository = :r AND branch = :b AND state = 'merged' "
                "ORDER BY merged_at DESC NULLS LAST, number DESC LIMIT 1"),
                {"r": self.repository_full_name, "b": branch_name}).mappings().first()
        if row is None:
            return None
        return {"number": int(row["number"]),
                "url": change_set_url(self.repository_full_name, row["number"]),
                "head_sha": str(row["head_sha"] or ""),
                "merged_at": str(row["merged_at"] or ""),
                "merge_commit_sha": str(row["merged_sha"] or "")}

    def restack_after_merge(self, branch_name: str, merged_head_sha: str, *,
                            base: Optional[str] = None, number: Optional[int] = None) -> dict:
        """Put the work branch back on its base after its change set merged
        (local_repo.restack): it then proposes only what changed AFTER the merge. `number` is
        accepted for interface parity with GitHubAdapter and not needed here — the branch's
        own commits are replayed, not re-written into one synthetic commit."""
        if not merged_head_sha:
            return {"restacked": False, "paths": [], "why": "no merged head on record"}
        return local_repo.restack(self._require(), branch_name, merged_head_sha,
                                  base or self.default_branch())

    def pull_request_snapshot(self, pull_number: int) -> dict:
        row = self.change_set(pull_number)
        git_dir = self._require()
        head_sha = local_repo.branch_sha(git_dir, row["branch"]) or row.get("head_sha") or ""
        base_sha = local_repo.branch_sha(git_dir, row["base_branch"]) or ""
        mergeable = None
        if row["state"] == "open" and head_sha and base_sha:
            mergeable = True
        return {"number": int(row["number"]), "url": change_set_url(self.repository_full_name, row["number"]),
                "state": "open" if row["state"] == "open" else "closed",
                "merged": row["state"] == "merged", "draft": bool(row.get("draft")),
                "mergeable": mergeable, "mergeable_state": "clean" if mergeable else "",
                "head_ref": row["branch"], "head_sha": head_sha,
                "head_repository": self.repository_full_name,
                "base_ref": row["base_branch"], "base_sha": base_sha,
                "base_repository": self.repository_full_name,
                "updated_at": str(row.get("updated_at") or "")}

    def pull_request_meta(self, pull_number: int) -> dict:
        row = self.change_set(pull_number)
        snap = self.pull_request_snapshot(pull_number)
        return {"number": int(row["number"]), "url": snap["url"], "title": row["title"],
                "body": row["body"], "state": snap["state"], "head_ref": row["branch"],
                "head_sha": snap["head_sha"], "base_ref": row["base_branch"],
                "base_sha": snap["base_sha"]}

    def pull_request_files(self, pull_number: int, *, limit: int = 3000) -> list[dict]:
        row = self.change_set(pull_number)
        base = row["base_branch"] if row["state"] == "open" else (row.get("base_sha") or row["base_branch"])
        head = _head_of(row, self._require())
        return local_repo.changed_files(self.git_dir, base, head)[:limit]

    def get_pull_request_diff(self, pull_number: int) -> str:
        row = self.change_set(pull_number)
        base = row["base_branch"] if row["state"] == "open" else (row.get("base_sha") or row["base_branch"])
        head = _head_of(row, self._require())
        return local_repo.diff(self.git_dir, base, head)

    def merge_pull_request(self, pull_request_number: int, commit_title: str,
                           *, merged_by: str = "boss") -> PullRequestMergeResult:
        """Merge the change set's branch into its base: a fast-forward when the base has not
        moved, else a merge commit (`squash` when the install's merge method says so). Only a
        person's approved decision reaches this (software_factory.merge_pull_request)."""
        git_dir = self._require()
        with self.engine.begin() as c:
            row = self._row(c, pull_request_number, lock=True)
            if row["state"] == "merged":
                return PullRequestMergeResult(sha=row.get("merged_sha") or "", merged=True,
                                              message="Already merged.")
            if row["state"] != "open":
                raise local_repo.LocalRepoError(f"Change #{pull_request_number} is {row['state']}.")
            method = str(getattr(self.settings, "sandbox_merge_method", "merge") or "merge")
            sha = local_repo.merge(git_dir, row["branch"], row["base_branch"],
                                   commit_title or f"Merge change #{pull_request_number}",
                                   method="squash" if method == "squash" else "merge")
            c.execute(text(
                "UPDATE local_change_sets SET state = 'merged', merged_sha = :s, merged_by = :by, "
                "merged_at = now(), head_sha = :h, updated_at = now() WHERE id = :id"),
                {"s": sha, "by": merged_by, "h": local_repo.branch_sha(git_dir, row["branch"]),
                 "id": row["id"]})
        return PullRequestMergeResult(sha=sha, merged=True,
                                      message=f"Merged into {row['base_branch']} on this machine.")

    def merge_pull_request_guarded(self, *, pull_number: int, expected_head_sha: str,
                                   expected_base_sha: str, expected_head_ref: str,
                                   expected_base_ref: str, merge_method: str,
                                   commit_title: str) -> dict:
        from .github_adapter import GitHubMergeRejectedError

        current = self.pull_request_snapshot(pull_number)
        if current["state"] != "open":
            raise GitHubMergeRejectedError("The change is not open.")
        for field, value in {"head_sha": expected_head_sha, "base_sha": expected_base_sha,
                             "head_ref": expected_head_ref, "base_ref": expected_base_ref}.items():
            if current[field] != value:
                raise GitHubMergeRejectedError("The change moved after it was approved.")
        merged = self.merge_pull_request(pull_number, commit_title)
        return {"pull_request_number": pull_number, "commit_sha": merged.sha, "merged": True,
                "message": merged.message, "merge_method": merge_method,
                "previous_target_sha": expected_base_sha, "authorized_head_sha": expected_head_sha}

    def close_change_set(self, pull_number: int) -> None:
        with self.engine.begin() as c:
            c.execute(text("UPDATE local_change_sets SET state = 'closed', updated_at = now() "
                           "WHERE repository = :r AND number = :n AND state = 'open'"),
                      {"r": self.repository_full_name, "n": int(pull_number)})

    # -- the review conversation ------------------------------------------------------------
    def _note(self, number: int, *, kind: str, body: str, event: Optional[str] = None,
              comments: Optional[list] = None, in_reply_to: Optional[int] = None,
              author: str = "boss") -> int:
        with self.engine.begin() as c:
            row = self._row(c, number)
            # ONE NOTE PER WORDS: a build's review conversation posted again (a retried finalize,
            # a follow-up re-posting the rounds it inherited) is the same note, not a second copy.
            for same in c.execute(text(
                    "SELECT number, comments FROM local_change_set_notes WHERE change_set_id = :cs "
                    "AND kind = :k AND COALESCE(event, '') = COALESCE(:e, '') AND body = :b "
                    "AND in_reply_to IS NOT DISTINCT FROM :r ORDER BY number"),
                    {"cs": row["id"], "k": kind, "e": event, "b": body or "",
                     "r": in_reply_to}).mappings():
                if _note_key(same["comments"]) == _note_key(comments):
                    return int(same["number"])
            note_id = int(c.execute(text("SELECT nextval('local_change_set_note_ids')")).scalar())
            c.execute(text(
                "INSERT INTO local_change_set_notes (id, change_set_id, number, kind, event, body, "
                "comments, in_reply_to, author) VALUES (:id, :cs, :n, :k, :e, :b, CAST(:c AS JSONB), "
                ":r, :a)"),
                {"id": str(note_id), "cs": row["id"], "n": note_id, "k": kind, "e": event,
                 "b": body or "", "c": json.dumps(comments or []), "r": in_reply_to, "a": author})
        return note_id

    def create_issue_comment(self, issue_number: int, body: str) -> IssueCommentResult:
        note = self._note(issue_number, kind="comment", body=body)
        return IssueCommentResult(url=f"{change_set_url(self.repository_full_name, issue_number)}#note-{note}",
                                  id=note)

    def create_review(self, pull_number: int, body: str, event: str = "COMMENT",
                      comments: Optional[list[dict]] = None) -> PullRequestReviewResult:
        normalized = str(event or "COMMENT").upper()
        if normalized not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise ValueError("event must be COMMENT, APPROVE, or REQUEST_CHANGES")
        # Each inline comment gets its own id, as GitHub gives review comments theirs.
        numbered = []
        for comment in comments or []:
            with self.engine.begin() as c:
                cid = int(c.execute(text("SELECT nextval('local_change_set_note_ids')")).scalar())
            numbered.append({**comment, "id": cid})
        note = self._note(pull_number, kind="review", body=body, event=normalized, comments=numbered)
        return PullRequestReviewResult(
            url=f"{change_set_url(self.repository_full_name, pull_number)}#review-{note}", id=note,
            state={"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(normalized, "COMMENTED"))

    def notes(self, pull_number: int) -> list[dict[str, Any]]:
        with self.engine.begin() as c:
            row = self._row(c, pull_number)
            rows = c.execute(text(
                "SELECT id, number, kind, event, body, comments, in_reply_to, author, created_at "
                "FROM local_change_set_notes WHERE change_set_id = :cs ORDER BY created_at, number"),
                {"cs": row["id"]}).mappings().all()
        # Notes written before _note refused a second copy are read once, too.
        seen: set[tuple] = set()
        out = []
        for r in rows:
            key = (r["kind"], r["event"] or "", r["body"] or "", r["in_reply_to"],
                   _note_key(r["comments"]))
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(r))
        return out

    def pull_request_reviews(self, pull_number: int) -> list[dict]:
        row = self.change_set(pull_number)
        return [{"id": int(n["number"]), "reviewer": n["author"], "state": {
                    "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(
                        n.get("event") or "", "COMMENTED"),
                 "commit_sha": row.get("head_sha") or "", "submitted_at": str(n["created_at"])}
                for n in self.notes(pull_number) if n["kind"] == "review"]

    def review_comments_of(self, pull_number: int, review_id: int) -> list[dict]:
        for note in self.notes(pull_number):
            if note["kind"] == "review" and int(note["number"]) == int(review_id):
                return [{"id": int(c.get("id") or 0), "path": str(c.get("path") or ""),
                         "line": c.get("line"), "body": str(c.get("body") or "")}
                        for c in (note.get("comments") or [])]
        return []

    def update_review_body(self, pull_number: int, review_id: int, body: str) -> None:
        with self.engine.begin() as c:
            c.execute(text("UPDATE local_change_set_notes SET body = :b, updated_at = now() "
                           "WHERE number = :n AND kind = 'review'"), {"b": body, "n": int(review_id)})

    def reply_to_review_comment(self, pull_number: int, comment_id: int, body: str) -> int:
        return self._note(pull_number, kind="reply", body=body, in_reply_to=int(comment_id))

    # -- what a hosted CI or deployment API would answer ------------------------------------
    def _request(self, method: str, path: str, payload: Any = None, **kwargs: Any) -> Any:
        """The few read paths generic readers send to `_request` (ci_gate.AdapterCiReader): a
        local repository has no hosted CI, so no check runs, no statuses and no workflows.
        Anything else is a GitHub call this repository cannot answer."""
        tail = path.split(self.repository_full_name, 1)[-1]
        if method.upper() == "GET":
            if "/check-runs" in tail:
                return {"total_count": 0, "check_runs": []}
            if tail.endswith("/status") or "/statuses" in tail:
                return {"state": "success", "statuses": []}
            if tail.startswith("/actions/runs"):
                return {"total_count": 0, "workflow_runs": []}
            if tail.startswith("/contents/"):
                raise _NotFound("not on a local repository")
        raise LocalHostUnsupported(
            f"{method} {path} is a GitHub call; {self.repository_full_name} is a local repository. "
            "Publish it to GitHub first if this needs GitHub.")

    def _evaluate_check_runs(self, head_sha: str, check_runs: list[PullRequestValidationCheck]
                             ) -> PullRequestValidationResult:
        return local_validation(head_sha)

    def create_deployment(self, ref: str, environment: str, description: str) -> DeploymentResult:
        return DeploymentResult(id=0, environment=environment, sha=ref, url="")

    def mark_deployment_success(self, deployment_id: int, environment: str, *,
                                environment_url: Optional[str] = None,
                                description: Optional[str] = None) -> DeploymentStatusResult:
        return DeploymentStatusResult(state="success", url="", environment_url=environment_url)

    def mark_deployment_failure(self, deployment_id: int, environment: str, *,
                                environment_url: Optional[str] = None,
                                description: Optional[str] = None) -> DeploymentStatusResult:
        return DeploymentStatusResult(state="failure", url="", environment_url=environment_url)


LOCAL_CHECKS_SUMMARY = ("No hosted CI for a local repository: BOSS's sandbox ran the checks "
                        "(install, build, typecheck, lint, tests, app start) before this change "
                        "was proposed.")


def local_validation(head_sha: str) -> PullRequestValidationResult:
    return PullRequestValidationResult(status="passed", summary=LOCAL_CHECKS_SUMMARY,
                                       head_sha=head_sha, completed=True, checks=[])


class LocalSandboxPipeline:
    """The pipeline adapter for a local repository: its CI is BOSS's sandbox, which already ran."""

    provider = "boss_sandbox"

    def __init__(self, settings: Any, github: LocalGitAdapter):
        self.settings = settings
        self.github = github

    @property
    def repository(self) -> str:
        return self.github.repository_full_name

    def wait_for_verdict(self, ref: str, *, timeout_seconds: int = 0, poll_interval_seconds: int = 0):
        from .pipeline_adapter import PipelineVerdict

        return PipelineVerdict(provider=self.provider, repository=self.repository, ref=ref,
                               status="passed", summary=LOCAL_CHECKS_SUMMARY, completed=True,
                               checks=[])

    def discover(self, repository: str = "") -> Any:
        """A local repository's pipeline is always configured: it is BOSS's sandbox."""
        from .pipeline_adapter import PipelineDescriptor

        return PipelineDescriptor(provider=self.provider, repository=self.repository,
                                  configured=True, workflows=[])

    def trigger(self, ref: str) -> Any:
        raise LocalHostUnsupported("A local repository has no hosted pipeline to trigger: "
                                   "BOSS's sandbox runs its checks.")


def adapter_for(settings: Any, owner: str, repo: str, **kwargs: Any) -> LocalGitAdapter:
    return LocalGitAdapter(settings, owner=owner, repo=repo, **kwargs)


__all__ = ["LOCAL_PR_HOST", "LocalGitAdapter", "LocalHostUnsupported", "LocalSandboxPipeline",
           "adapter_for", "change_set_url", "is_local_pr_url", "local_validation"]
