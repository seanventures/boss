from __future__ import annotations

from dataclasses import dataclass
import base64
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)
from urllib.parse import quote

import httpx

from ..config import Settings


class IntegrationConfigurationError(RuntimeError):
    pass


class GitHubMergeConflictError(RuntimeError):
    """GitHub could not merge the requested immutable commits cleanly."""


class GitHubMergeRejectedError(RuntimeError):
    """GitHub rejected a guarded pull-request merge."""


@dataclass
class PullRequestResult:
    url: str
    number: int


@dataclass
class PullRequestMergeResult:
    sha: str
    merged: bool
    message: str


@dataclass
class IssueCommentResult:
    url: str
    id: int


@dataclass
class PullRequestReviewResult:
    url: str
    id: int
    state: str


@dataclass
class DeploymentResult:
    id: int
    environment: str
    sha: str
    url: str


@dataclass
class DeploymentStatusResult:
    state: str
    url: str
    environment_url: Optional[str]


@dataclass
class PullRequestValidationCheck:
    name: str
    status: str
    conclusion: Optional[str]
    html_url: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]


@dataclass
class PullRequestValidationResult:
    status: str
    summary: str
    head_sha: str
    completed: bool
    checks: list[PullRequestValidationCheck]


class GitHubAdapter:
    kind = "github"  # RepoHost conformance (BOSS-267) — the reference plugin

    def __init__(
        self,
        settings: Settings,
        *,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        token: Optional[str] = None,
        api_base_url: str = "https://api.github.com",
    ):
        self.settings = settings
        self.owner = (owner or settings.target_github_owner).strip()
        self.repo = (repo or settings.target_github_repo).strip()
        self._token_override = token
        self.api_base_url = api_base_url.rstrip("/")
        self._app = None

    @property
    def repository_full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def clone_url(self, token: str) -> str:
        from .repo_host import clone_url_for
        return clone_url_for("github", self.settings.target_github_owner,
                             self.settings.target_github_repo, token)

    def _auth_token(self) -> str:
        """Prefer a short-lived, repo-scoped GitHub-App installation token
        (Phase 2 developer impersonation); fall back to the PAT (Phase 0)."""
        from .github_app_adapter import (  # optional dependency
            GitHubAppAdapter, GitHubAppConfigurationError, repo_token,
        )

        if self._app is None:
            self._app = GitHubAppAdapter(self.settings, api_base_url=self.api_base_url)
        if self._token_override:
            return self._token_override
        try:
            # One shared decision: App token where the installation covers this repo, PAT
            # where it does not. Choosing here independently is how BOSS came to be unable to
            # open a PR against its own repository.
            return repo_token(self.settings, self.owner, self.repo,
                              api_base_url=self.api_base_url, app=self._app)
        except GitHubAppConfigurationError as error:
            raise IntegrationConfigurationError(str(error)) from error

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._auth_token()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _send(method: str, url: str, *, headers: dict[str, str], json: Any = None) -> httpx.Response:
        """THE one way this adapter talks to GitHub: one request, raised on an error status, and
        retried with backoff when GitHub is having a moment (a 5xx, a timeout, a reset
        connection: transient_retry). A 401/403/404 is an answer and is raised at once."""
        from .transient_retry import call, is_transient_http_error

        def once() -> httpx.Response:
            response = httpx.request(method, url, headers=headers, json=json, timeout=30.0)
            response.raise_for_status()
            return response

        return call(once, operation=f"GitHub API {method}", transient=is_transient_http_error)

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[dict] = None,
        *,
        accept: Optional[str] = None,
        response_text: bool = False,
        allow_empty: bool = False,
    ) -> Any:
        headers = self._headers()
        if accept:
            headers["Accept"] = accept
        response = self._send(method, f"{self.api_base_url}{path}", headers=headers,
                              json=json_body)
        if response_text:
            return response.text
        if allow_empty and not response.content:
            return {}
        return response.json()

    def repo_metadata(self) -> dict:
        return self._request("GET", f"/repos/{self.repository_full_name}")

    def list_accessible_repositories(
        self,
        *,
        per_page: int = 50,
        max_pages: int = 2,
    ) -> list[dict[str, Any]]:
        """List repositories the current credential can see (PAT user/org scope).

        Never returns tokens. Used by project-link UI to pick owner/repo without
        requiring operators to type names from memory.
        """
        page_size = max(1, min(int(per_page), 100))
        pages = max(1, min(int(max_pages), 5))
        collected: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            payload = self._request(
                "GET",
                (
                    f"/user/repos?per_page={page_size}&page={page}"
                    "&sort=updated&affiliation=owner,collaborator,organization_member"
                ),
            )
            if not isinstance(payload, list) or not payload:
                break
            for item in payload:
                if not isinstance(item, dict):
                    continue
                full_name = str(item.get("full_name") or "").strip()
                if not full_name or "/" not in full_name:
                    continue
                owner = (item.get("owner") or {}) if isinstance(item.get("owner"), dict) else {}
                permissions = item.get("permissions") if isinstance(item.get("permissions"), dict) else {}
                collected.append(
                    {
                        "full_name": full_name,
                        "owner": str(owner.get("login") or full_name.split("/", 1)[0]),
                        "name": str(item.get("name") or full_name.split("/", 1)[1]),
                        "repository_id": item.get("id"),
                        "default_branch": item.get("default_branch"),
                        "visibility": item.get("visibility")
                        or ("private" if item.get("private") else "public"),
                        "archived": bool(item.get("archived", False)),
                        "permissions": {
                            key: bool(permissions.get(key))
                            for key in ("pull", "triage", "push", "maintain", "admin")
                            if key in permissions
                        },
                        "html_url": item.get("html_url"),
                    }
                )
            if len(payload) < page_size:
                break
        return collected

    def rate_limit_status(self) -> dict:
        """Return the current GitHub core API quota without exposing credentials."""
        payload = self._request("GET", "/rate_limit")
        core = (payload.get("resources") or {}).get("core") or payload.get("rate") or {}
        return {
            "resource": "core",
            "limit": max(0, int(core.get("limit") or 0)),
            "remaining": max(0, int(core.get("remaining") or 0)),
            "used": max(0, int(core.get("used") or 0)),
            "reset_epoch": max(0, int(core.get("reset") or 0)),
        }

    def branch_snapshot(self, branch_name: str) -> dict:
        """Read a branch head, tree, parents, and protection without mutation."""
        encoded_branch = quote(branch_name, safe="")
        try:
            ref = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/git/ref/heads/{encoded_branch}",
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return {
                    "ref": branch_name,
                    "exists": False,
                    "commit_sha": None,
                    "tree_sha": None,
                    "parents": [],
                    "protected": False,
                    "protection": {},
                }
            raise
        commit_sha = str((ref.get("object") or {}).get("sha") or "")
        if not commit_sha:
            raise RuntimeError(f"GitHub returned no commit SHA for {branch_name}.")
        commit = self._request(
            "GET",
            f"/repos/{self.repository_full_name}/git/commits/{quote(commit_sha, safe='')}",
        )
        try:
            branch = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/branches/{encoded_branch}",
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            branch = {}
        protection = branch.get("protection") or {}
        return {
            "ref": branch_name,
            "exists": True,
            "commit_sha": commit_sha,
            "tree_sha": str((commit.get("tree") or {}).get("sha") or ""),
            "parents": [
                str(parent.get("sha"))
                for parent in (commit.get("parents") or [])
                if parent.get("sha")
            ],
            "protected": bool(branch.get("protected", False)),
            "protection": {
                "enabled": bool(branch.get("protected", False)),
                "required_status_checks": protection.get("required_status_checks"),
                "required_pull_request_reviews": protection.get(
                    "required_pull_request_reviews"
                ),
                "required_signatures": protection.get("required_signatures"),
                "enforce_admins": protection.get("enforce_admins"),
            },
        }

    def compare_commits(self, base_sha: str, head_sha: str) -> dict:
        """Compare immutable commit identities and return bounded topology data."""
        payload = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/compare/"
                f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
            ),
        )
        merge_base = payload.get("merge_base_commit") or {}
        return {
            "status": str(payload.get("status") or ""),
            "ahead_by": max(0, int(payload.get("ahead_by") or 0)),
            "behind_by": max(0, int(payload.get("behind_by") or 0)),
            "total_commits": max(0, int(payload.get("total_commits") or 0)),
            "merge_base_commit_sha": merge_base.get("sha"),
            "merge_base_tree_sha": (merge_base.get("commit") or {})
            .get("tree", {})
            .get("sha"),
        }

    def compare_summary(self, base_sha: str, head_sha: str, *, limit: int = 100) -> dict:
        """What one release added over another: its commits (sha, first line, author, date)
        and its files with bounded patches — what an incident investigation reads."""
        payload = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/compare/"
                f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
            ),
        )
        commits = []
        for c in (payload.get("commits") or [])[-limit:]:
            info = c.get("commit") or {}
            commits.append({
                "sha": str(c.get("sha") or ""),
                "message": str(info.get("message") or "").splitlines()[0][:200]
                if info.get("message") else "",
                "author": str((info.get("author") or {}).get("name") or "")[:100],
                "date": (info.get("author") or {}).get("date"),
                # Its trailers name the BOSS run that made it (release_scope.attribute).
                "full_message": str(info.get("message") or "")[:4000],
            })
        files = []
        for item in (payload.get("files") or [])[:300]:
            patch = item.get("patch")
            files.append({
                "filename": str(item.get("filename") or ""),
                "status": str(item.get("status") or "modified"),
                "additions": max(0, int(item.get("additions") or 0)),
                "deletions": max(0, int(item.get("deletions") or 0)),
                "patch": str(patch)[:20000] if isinstance(patch, str) else None,
            })
        return {"status": str(payload.get("status") or ""),
                "ahead_by": max(0, int(payload.get("ahead_by") or 0)),
                "behind_by": max(0, int(payload.get("behind_by") or 0)),
                "commits": commits, "files": files}

    def commit_identity(self, commit_sha: str) -> dict:
        """Return the commit and tree identity for an immutable Git object."""
        payload = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/git/commits/"
                f"{quote(commit_sha, safe='')}"
            ),
        )
        return {
            "commit_sha": str(payload.get("sha") or commit_sha),
            "tree_sha": str((payload.get("tree") or {}).get("sha") or ""),
        }

    def compare_changed_files(
        self,
        base_sha: str,
        head_sha: str,
        *,
        limit: int = 300,
    ) -> list[dict]:
        """Return bounded, reviewable file evidence for two immutable commits."""
        if limit < 1 or limit > 300:
            raise ValueError("limit must be between 1 and 300.")
        payload = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/compare/"
                f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
            ),
        )
        files = []
        for item in (payload.get("files") or [])[:limit]:
            patch = item.get("patch")
            files.append(
                {
                    "filename": str(item.get("filename") or ""),
                    "previous_filename": item.get("previous_filename"),
                    "status": str(item.get("status") or "modified"),
                    "additions": max(0, int(item.get("additions") or 0)),
                    "deletions": max(0, int(item.get("deletions") or 0)),
                    "changes": max(0, int(item.get("changes") or 0)),
                    "patch": (
                        str(patch)[:20000]
                        if isinstance(patch, str)
                        else None
                    ),
                }
            )
        return files

    def create_branch_ref(self, branch_name: str, commit_sha: str) -> dict:
        """Create a new branch at an immutable commit; never overwrites a ref."""
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/git/refs",
            {"ref": f"refs/heads/{branch_name}", "sha": commit_sha},
        )
        return {
            "ref": branch_name,
            "commit_sha": str((response.get("object") or {}).get("sha") or ""),
        }

    def fast_forward_ref(
        self,
        branch_name: str,
        *,
        expected_sha: str,
        new_sha: str,
    ) -> dict:
        """Update a branch without force after checking its expected head."""
        current = self.branch_snapshot(branch_name)
        if not current["exists"]:
            raise RuntimeError("GitHub branch no longer exists.")
        if current["commit_sha"] != expected_sha:
            raise RuntimeError("GitHub branch head changed before update.")
        encoded_branch = quote(branch_name, safe="")
        response = self._request(
            "PATCH",
            (
                f"/repos/{self.repository_full_name}/git/refs/heads/"
                f"{encoded_branch}"
            ),
            {"sha": new_sha, "force": False},
        )
        result_sha = str((response.get("object") or {}).get("sha") or "")
        if result_sha != new_sha:
            raise RuntimeError("GitHub returned an unexpected branch head.")
        return {
            "ref": branch_name,
            "previous_sha": expected_sha,
            "commit_sha": result_sha,
            "forced": False,
        }

    def merge_branch(
        self,
        *,
        base_branch: str,
        head_sha: str,
        message: str,
    ) -> dict:
        """Attempt a provider-side merge into an unprotected resolution branch."""
        try:
            response = self._request(
                "POST",
                f"/repos/{self.repository_full_name}/merges",
                {
                    "base": base_branch,
                    "head": head_sha,
                    "commit_message": message,
                },
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 409:
                raise GitHubMergeConflictError(
                    "GitHub reported merge conflicts."
                ) from error
            raise
        commit = response.get("commit") or response
        return {
            "base": base_branch,
            "head": head_sha,
            "commit_sha": str(response.get("sha") or commit.get("sha") or ""),
            "tree_sha": str(
                ((commit.get("tree") or {}).get("sha"))
                or ((commit.get("commit") or {}).get("tree") or {}).get("sha")
                or ""
            ),
        }

    def create_resolution_commit(
        self,
        *,
        base_tree_sha: str,
        parent_shas: list[str],
        resolved_files: list[dict],
        message: str,
    ) -> dict:
        """Create one explicit reviewed resolution commit from complete file bodies."""
        if len(parent_shas) != 2 or any(not str(value).strip() for value in parent_shas):
            raise ValueError("A resolution commit requires exactly two parent SHAs.")
        if not resolved_files:
            raise ValueError("resolved_files must contain at least one file.")
        tree_entries = []
        for item in resolved_files:
            path = str(item.get("path") or "").strip()
            content = item.get("content")
            mode = str(item.get("mode") or "100644")
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise ValueError("Resolved file paths must be repository-relative.")
            if not isinstance(content, str):
                raise ValueError("Resolved file content must be UTF-8 text.")
            if mode not in {"100644", "100755"}:
                raise ValueError("Resolved file mode must be 100644 or 100755.")
            blob = self._request(
                "POST",
                f"/repos/{self.repository_full_name}/git/blobs",
                {
                    "content": base64.b64encode(content.encode("utf-8")).decode(
                        "ascii"
                    ),
                    "encoding": "base64",
                },
            )
            tree_entries.append(
                {
                    "path": path,
                    "mode": mode,
                    "type": "blob",
                    "sha": str(blob["sha"]),
                }
            )
        tree = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/git/trees",
            {"base_tree": base_tree_sha, "tree": tree_entries},
        )
        commit = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/git/commits",
            {
                "message": message,
                "tree": str(tree["sha"]),
                "parents": parent_shas,
            },
        )
        return {
            "commit_sha": str(commit["sha"]),
            "tree_sha": str((commit.get("tree") or {}).get("sha") or tree["sha"]),
            "parents": parent_shas,
            "files": [entry["path"] for entry in tree_entries],
        }

    def commit_checks(self, commit_sha: str) -> list[dict]:
        """Return bounded check-run state for an immutable commit."""
        payload = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/commits/"
                f"{quote(commit_sha, safe='')}/check-runs?per_page=100"
            ),
            accept="application/vnd.github+json",
        )
        return [
            {
                "name": check.get("name"),
                "status": check.get("status"),
                "conclusion": check.get("conclusion"),
                "app": (check.get("app") or {}).get("slug"),
            }
            for check in (payload.get("check_runs") or [])
        ]

    def default_branch_sha(self) -> tuple[str, str]:
        repo = self.repo_metadata()
        default_branch = repo["default_branch"]
        ref = self._request(
            "GET", f"/repos/{self.repository_full_name}/git/ref/heads/{default_branch}"
        )
        return default_branch, ref["object"]["sha"]

    def branch_exists(self, branch_name: str) -> bool:
        """True if the branch already exists on the remote (used to clone prior work
        when a feedback-iteration reuses an existing task branch)."""
        try:
            self._request(
                "GET", f"/repos/{self.repository_full_name}/git/ref/heads/{branch_name}"
            )
            return True
        except httpx.HTTPStatusError:
            return False

    def ensure_branch(self, branch_name: str, base_branch: Optional[str] = None) -> str:
        """Return the branch's sha, creating it from `base_branch` if it does not exist.

        This always cut new branches from the DEFAULT branch, whatever the task said its base
        was. Continuing work on an app therefore silently restarted it: the sandbox checked out
        the app's branch, but the branch the commits landed on began at main, so the existing
        application was not there and the agent wrote it again from nothing. The run reported
        success and the PR looked plausible -- it was simply a different app.

        It went unnoticed because the common case hides it. A follow-up that carries its
        lineage targets a branch that ALREADY exists, so this returns early and never reaches
        the creation path. Only a task with a fresh root and an explicit base_branch fell
        through to it, which is exactly the "change this existing app" case.
        """
        try:
            ref = self._request(
                "GET", f"/repos/{self.repository_full_name}/git/ref/heads/{branch_name}"
            )
            return ref["object"]["sha"]
        except httpx.HTTPStatusError:
            base_sha = None
            if base_branch and base_branch != branch_name:
                try:
                    base_ref = self._request(
                        "GET",
                        f"/repos/{self.repository_full_name}/git/ref/heads/{base_branch}")
                    base_sha = base_ref["object"]["sha"]
                except httpx.HTTPStatusError:
                    # A base that does not exist is a caller error, but failing the run over it
                    # is worse than starting from the default branch and saying so.
                    logger.warning(
                        "github: base branch %s not found in %s; creating %s from the default "
                        "branch instead", base_branch, self.repository_full_name, branch_name)
            if base_sha is None:
                _, base_sha = self.default_branch_sha()
            ref = self._request(
                "POST",
                f"/repos/{self.repository_full_name}/git/refs",
                {"ref": f"refs/heads/{branch_name}", "sha": base_sha},
            )
            return ref["object"]["sha"]

    def upsert_file(self, branch_name: str, path: str, content: str, message: str) -> str:
        """Create or update a UTF-8 text file on a branch."""
        return self.upsert_binary_file(
            branch_name, path, content.encode("utf-8"), message
        )

    def read_text_file(self, branch_name: str, path: str) -> Optional[str]:
        """Read a UTF-8 repository file from a branch, returning ``None`` on 404."""
        try:
            payload = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/contents/{path}?ref={branch_name}",
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise
        encoded = payload.get("content")
        if not isinstance(encoded, str):
            raise RuntimeError(f"GitHub did not return file content for {path}.")
        return base64.b64decode(encoded).decode("utf-8")

    def upsert_binary_file(
        self, branch_name: str, path: str, data: bytes, message: str
    ) -> str:
        """Create or update a file of arbitrary bytes (e.g. a PDF) on a branch."""
        from .agent_acts import sign_commit_message

        message = sign_commit_message(message)
        sha = None
        try:
            existing = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/contents/{path}?ref={branch_name}",
            )
            sha = existing["sha"]
        except httpx.HTTPStatusError:
            sha = None

        response = self._send(
            "PUT",
            f"{self.api_base_url}/repos/{self.repository_full_name}/contents/{path}",
            headers=self._headers(),
            json={
                "message": message,
                "content": base64.b64encode(data).decode("ascii"),
                "branch": branch_name,
                **({"sha": sha} if sha else {}),
            },
        )
        data_json = response.json()
        return data_json["commit"]["sha"]

    #: GitHub has draft pull requests (software_factory opens a checks-failing PR as one).
    supports_draft_pull_requests = True

    def create_pull_request(
        self, branch_name: str, title: str, body: str, draft: Optional[bool] = None,
        base: Optional[str] = None,
    ) -> PullRequestResult:
        """Open (or refresh) the PR for this head. ``draft`` None follows BOSS_OPEN_PR_AS_DRAFT;
        True opens a draft (a build whose checks still fail, checks_failing.py). An already-open
        PR keeps its own draft state: only its body is refreshed."""
        # BOSSAPP-866: a secret value handed to the run never reaches the PR text.
        from .run_secret_redaction import scrub as _scrub_known

        title, body = _scrub_known(title), _scrub_known(body)
        default_branch, _ = self.default_branch_sha()
        # Idempotent for feedback-iterations that reuse a branch: if a PR is already open
        # for this head, refresh its body and return it — GitHub would otherwise 422 on a
        # duplicate head. Lets the same task iterate on one branch + one PR.
        owner = self.repository_full_name.split("/")[0]
        try:
            existing = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/pulls?state=open&head={owner}:{branch_name}",
            )
        except httpx.HTTPStatusError:
            existing = []
        if existing:
            pr = existing[0]
            try:
                self._request(
                    "PATCH",
                    f"/repos/{self.repository_full_name}/pulls/{pr['number']}",
                    {"body": body},
                )
            except httpx.HTTPStatusError:
                pass
            return PullRequestResult(url=pr["html_url"], number=pr["number"])
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/pulls",
            {
                "title": title,
                "head": branch_name,
                # A release fix's PR is opened against the serving commit's branch
                # (release_scope), so it shows only its own diff.
                "base": base or default_branch,
                "body": body,
                "draft": (self.settings.open_pr_as_draft if draft is None else bool(draft)),
                "maintainer_can_modify": False,
            },
        )
        return PullRequestResult(url=response["html_url"], number=response["number"])

    def pull_request_state(self, pull_request_number: int) -> dict:
        """{state: open|closed, merged, merge_commit_sha, head_sha} as GitHub has it now."""
        pr = self._request(
            "GET", f"/repos/{self.repository_full_name}/pulls/{int(pull_request_number)}") or {}
        return {"state": str(pr.get("state") or ""),
                "merged": bool(pr.get("merged") or pr.get("merged_at")),
                "merge_commit_sha": str(pr.get("merge_commit_sha") or ""),
                "head_sha": str((pr.get("head") or {}).get("sha") or "")}

    def close_pull_request(self, pull_request_number: int, comment: str = "") -> None:
        """Close a pull request without merging, saying why in a comment first."""
        if comment:
            self.create_issue_comment(int(pull_request_number), comment)
        self._request("PATCH",
                      f"/repos/{self.repository_full_name}/pulls/{int(pull_request_number)}",
                      {"state": "closed"})

    def changes_already_in(self, pull_request_number: int, base: Optional[str] = None) -> bool:
        """Whether every file this pull request touches already reads, on `base` (default: the
        default branch), exactly as on the pull request's head — a superseded duplicate whose
        change was merged another way (a squash of an earlier PR from the same branch)."""
        head = self.pull_request_state(pull_request_number).get("head_sha")
        if not head:
            return False
        if not base:
            base, _ = self.default_branch_sha()
        files = self.pull_request_files(int(pull_request_number))
        if not files:
            return False
        for f in files:
            name = str(f.get("path") or f.get("filename") or "")
            status = str(f.get("status") or "")
            on_base = self.read_text_file(base, name)
            if status == "removed":
                if on_base is not None:
                    return False
                continue
            if on_base is None or on_base != self.read_text_file(head, name):
                return False
        return True

    def open_pull_request_for(self, branch_name: str) -> Optional[dict]:
        """The open pull request whose head is this branch ({number, url, title}), or None."""
        owner = self.repository_full_name.split("/")[0]
        rows = self._request(
            "GET", f"/repos/{self.repository_full_name}/pulls?state=open&head={owner}:"
                   f"{quote(branch_name, safe='/')}") or []
        if not rows:
            return None
        pr = rows[0]
        return {"number": int(pr["number"]), "url": str(pr.get("html_url") or ""),
                "title": str(pr.get("title") or "")}

    def last_merged_pull_request(self, branch_name: str) -> Optional[dict]:
        """The newest MERGED pull request from this branch ({number, url, head_sha, merged_at,
        merge_commit_sha}), or None. A work branch outlives its first pull request: a retry
        after the merge pushes to the same branch (pr_follow_up)."""
        owner = self.repository_full_name.split("/")[0]
        rows = self._request(
            "GET", f"/repos/{self.repository_full_name}/pulls?state=closed&head={owner}:"
                   f"{quote(branch_name, safe='/')}&sort=updated&direction=desc&per_page=20") or []
        merged = [pr for pr in rows if pr.get("merged_at")]
        if not merged:
            return None
        pr = max(merged, key=lambda p: str(p.get("merged_at") or ""))
        return {"number": int(pr["number"]), "url": str(pr.get("html_url") or ""),
                "head_sha": str((pr.get("head") or {}).get("sha") or ""),
                "merged_at": pr.get("merged_at"),
                "merge_commit_sha": str(pr.get("merge_commit_sha") or "")}

    def restack_after_merge(self, branch_name: str, merged_head_sha: str, *,
                            base: Optional[str] = None, number: Optional[int] = None) -> dict:
        """PUT A WORK BRANCH BACK ON ITS BASE AFTER ITS PULL REQUEST MERGED.

        A squash (or rebase) merge leaves the work branch's own commits off the base: the next
        pull request from it re-proposes everything already merged and GitHub calls it
        conflicting (paramg/boss-sandbox PR #237 after #236, 2026-11-08: 5,629 lines for a
        five-line fix, then "405 Method Not Allowed" on the merge). The branch is reset to the
        base's head (which holds the merged work) and only what changed AFTER the merged head
        is written back, file by file. Returns {"restacked", "paths", "why"?}; refuses (and
        changes nothing) when the merged head is not in the branch, a file was removed or
        renamed, or a file is not text — those need an agent, not a git call."""
        if not merged_head_sha:
            return {"restacked": False, "paths": [], "why": "no merged head on record"}
        base_name = base
        if not base_name:
            base_name, base_sha = self.default_branch_sha()
        else:
            base_sha = self._request(
                "GET", f"/repos/{self.repository_full_name}/git/ref/heads/{base_name}"
            )["object"]["sha"]
        head_sha = self._request(
            "GET", f"/repos/{self.repository_full_name}/git/ref/heads/{branch_name}"
        )["object"]["sha"]
        on_base = self.compare_commits(base_sha, head_sha)
        if on_base["status"] in ("ahead", "identical"):
            return {"restacked": False, "paths": [], "why": "already on its base"}
        since = self.compare_commits(merged_head_sha, head_sha)
        if since["status"] not in ("ahead", "identical"):
            return {"restacked": False, "paths": [],
                    "why": "the merged head is not in the branch"}
        delta = self.compare_changed_files(merged_head_sha, head_sha) \
            if since["status"] == "ahead" else []
        if any(f["status"] in ("removed", "renamed") for f in delta):
            return {"restacked": False, "paths": [],
                    "why": "a file was removed or renamed after the merge"}
        contents: dict[str, str] = {}
        for f in delta:
            try:
                text = self.read_text_file(head_sha, f["filename"])
            except UnicodeDecodeError:
                return {"restacked": False, "paths": [], "why": f"{f['filename']} is not text"}
            if text is None:
                return {"restacked": False, "paths": [],
                        "why": f"{f['filename']} could not be read"}
            contents[f["filename"]] = text
        # ONE move of the branch, never through its base: the restacked commit is built first
        # (base tree + the newer files) and the ref jumps straight to it. Resetting the branch
        # to the base and writing files one by one left the pull request with nothing in it for
        # a moment, and GitHub closes an empty pull request on its own (PR #237, 2026-11-09).
        after = f" after #{number}" if number else " after its merge"
        repo = f"/repos/{self.repository_full_name}"
        base_tree = self._request("GET", f"{repo}/git/commits/{base_sha}")["tree"]["sha"]
        tree = self._request("POST", f"{repo}/git/trees", {
            "base_tree": base_tree,
            "tree": [{"path": path, "mode": "100644", "type": "blob", "content": text}
                     for path, text in sorted(contents.items())]})
        commit = self._request("POST", f"{repo}/git/commits", {
            "message": f"BOSS follow-up{after}: {', '.join(sorted(contents))}",
            "tree": tree["sha"], "parents": [base_sha]})
        self._request(
            "PATCH",
            f"{repo}/git/refs/heads/{quote(branch_name, safe='')}",
            {"sha": commit["sha"], "force": True})
        return {"restacked": True, "paths": sorted(contents), "base": base_name}

    def add_labels(self, pull_request_number: int, labels: list[str]) -> None:
        """Add labels to a pull request (GitHub creates a label it does not have yet)."""
        self._request(
            "POST",
            f"/repos/{self.repository_full_name}/issues/{pull_request_number}/labels",
            {"labels": list(labels)},
        )

    def mark_ready_for_review(self, pull_request_number: int) -> bool:
        """Turn a draft PR into one GitHub will merge. REST cannot: it is the GraphQL
        `markPullRequestReadyForReview` mutation on the PR's node id. True when it is ready
        (or was never a draft)."""
        pull = self._request(
            "GET", f"/repos/{self.repository_full_name}/pulls/{pull_request_number}")
        if not pull.get("draft"):
            return True
        node_id = pull.get("node_id")
        if not node_id:
            return False
        # GitHub Enterprise serves REST at <host>/api/v3 and GraphQL at <host>/api/graphql.
        base = self.api_base_url
        graphql = (base[: -len("/v3")] + "/graphql") if base.endswith("/api/v3") \
            else base + "/graphql"
        answer = self._send(
            "POST", graphql, headers=self._headers(),
            json={"query": "mutation($id: ID!) { markPullRequestReadyForReview("
                           "input: {pullRequestId: $id}) { pullRequest { isDraft } } }",
                  "variables": {"id": node_id}},
        ).json()
        if (answer or {}).get("errors"):
            raise RuntimeError(f"GitHub refused to mark PR #{pull_request_number} ready: "
                               f"{str(answer['errors'])[:200]}")
        ready = ((((answer or {}).get("data") or {}).get("markPullRequestReadyForReview") or {})
                 .get("pullRequest") or {})
        return ready.get("isDraft") is False

    def update_pull_request_branch(self, pull_request_number: int) -> bool:
        """Merge the base branch INTO the PR's head branch (GitHub's "Update branch").

        This is the cheap half of conflict recovery. When several PRs branch from the same
        base and one merges, the others go CONFLICTING and GitHub answers the merge endpoint
        with 405 — even when the drift is trivial and git could reconcile it unattended.
        Measured: merging PR #66 immediately made #67 and #68 unmergeable, and BOSS recorded
        `pull_request_merge_failed` and stopped, so only the first of any batch could ever land.

        Returns True when GitHub accepted the update (the branch now contains the base), False
        when it refused — which means a REAL conflict that needs an agent, not a retry.
        """
        try:
            self._request(
                "PUT",
                f"/repos/{self.repository_full_name}/pulls/{pull_request_number}/update-branch",
                {},
            )
            return True
        except Exception:  # noqa: BLE001 — a refusal is information, not an error to raise
            return False

    def merge_pull_request(
        self, pull_request_number: int, commit_title: str
    ) -> PullRequestMergeResult:
        response = self._request(
            "PUT",
            f"/repos/{self.repository_full_name}/pulls/{pull_request_number}/merge",
            {
                "commit_title": commit_title,
                "merge_method": self.settings.sandbox_merge_method,
            },
        )
        return PullRequestMergeResult(
            sha=response["sha"],
            merged=bool(response["merged"]),
            message=response["message"],
        )

    def pull_request_snapshot(self, pull_number: int) -> dict:
        """Return the immutable identities and merge posture for one pull request."""
        response = self._request(
            "GET",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}",
        )
        head = response.get("head") or {}
        base = response.get("base") or {}
        return {
            "number": int(response["number"]),
            "url": str(response.get("html_url") or ""),
            "state": str(response.get("state") or ""),
            "draft": bool(response.get("draft", False)),
            "mergeable": response.get("mergeable"),
            "mergeable_state": str(response.get("mergeable_state") or ""),
            "head_ref": str(head.get("ref") or ""),
            "head_sha": str(head.get("sha") or ""),
            "head_repository": str(
                ((head.get("repo") or {}).get("full_name")) or ""
            ),
            "base_ref": str(base.get("ref") or ""),
            "base_sha": str(base.get("sha") or ""),
            "base_repository": str(
                ((base.get("repo") or {}).get("full_name")) or ""
            ),
            "updated_at": response.get("updated_at"),
        }

    def pull_request_reviews(self, pull_number: int) -> list[dict]:
        """Return bounded review evidence used by merge authorization."""
        response = self._request(
            "GET",
            (
                f"/repos/{self.repository_full_name}/pulls/{pull_number}/reviews"
                "?per_page=100"
            ),
        )
        return [
            {
                "id": int(review["id"]),
                "reviewer": str((review.get("user") or {}).get("login") or ""),
                "state": str(review.get("state") or "").upper(),
                "commit_sha": str(review.get("commit_id") or ""),
                "submitted_at": review.get("submitted_at"),
            }
            for review in response[:100]
            if review.get("id") is not None
        ]

    def merge_pull_request_guarded(
        self,
        *,
        pull_number: int,
        expected_head_sha: str,
        expected_base_sha: str,
        expected_head_ref: str,
        expected_base_ref: str,
        merge_method: str,
        commit_title: str,
    ) -> dict:
        """Merge only if GitHub still exposes the exact authorized PR snapshot."""
        current = self.pull_request_snapshot(pull_number)
        expected = {
            "head_sha": expected_head_sha,
            "base_sha": expected_base_sha,
            "head_ref": expected_head_ref,
            "base_ref": expected_base_ref,
        }
        if current["state"] != "open":
            raise GitHubMergeRejectedError("Pull request is not open.")
        if current["draft"]:
            raise GitHubMergeRejectedError("Draft pull requests cannot be merged.")
        for field, value in expected.items():
            if current[field] != value:
                raise GitHubMergeRejectedError(
                    "Pull request snapshot changed after authorization."
                )
        response = self._request(
            "PUT",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}/merge",
            {
                "sha": expected_head_sha,
                "commit_title": commit_title,
                "merge_method": merge_method,
            },
        )
        if not bool(response.get("merged")):
            raise GitHubMergeRejectedError(
                str(response.get("message") or "GitHub rejected the merge.")
            )
        return {
            "pull_request_number": pull_number,
            "commit_sha": str(response.get("sha") or ""),
            "merged": True,
            "message": str(response.get("message") or ""),
            "merge_method": merge_method,
            "previous_target_sha": expected_base_sha,
            "authorized_head_sha": expected_head_sha,
        }

    def create_issue_comment(
        self, issue_number: int, body: str
    ) -> IssueCommentResult:
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/issues/{issue_number}/comments",
            {"body": body},
        )
        return IssueCommentResult(url=response["html_url"], id=int(response["id"]))

    def create_review(
        self,
        pull_number: int,
        body: str,
        event: str = "COMMENT",
        comments: Optional[list[dict]] = None,
    ) -> PullRequestReviewResult:
        normalized_event = event.upper()
        if normalized_event not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise ValueError(
                "event must be COMMENT, APPROVE, or REQUEST_CHANGES"
            )
        payload: dict = {"body": body, "event": normalized_event}
        if comments:
            payload["comments"] = comments
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}/reviews",
            payload,
        )
        return PullRequestReviewResult(
            url=response["html_url"],
            id=int(response["id"]),
            state=response["state"],
        )

    # -- the review conversation on a pull request (services/pr_review_post.py) -------------

    def list_paths(self, branch_name: str, *, limit: int = 20000) -> list[str]:
        """Every file path on a branch (one recursive tree call; GitHub truncates very large
        trees, and what it returned is what this returns)."""
        payload = self._request(
            "GET",
            f"/repos/{self.repository_full_name}/git/trees/{branch_name}?recursive=1",
        )
        tree = payload.get("tree") if isinstance(payload, dict) else None
        return [str(t.get("path")) for t in (tree or [])
                if isinstance(t, dict) and t.get("type") == "blob"][:limit]

    def pull_request_files(self, pull_number: int, *, limit: int = 3000) -> list[dict]:
        """Every file the pull request changes: {path, status, additions, deletions}. GitHub
        lists at most 3000; a review is told when it was cut."""
        out: list[dict] = []
        page = 1
        while len(out) < limit:
            batch = self._request(
                "GET",
                f"/repos/{self.repository_full_name}/pulls/{pull_number}/files"
                f"?per_page=100&page={page}",
            )
            if not batch:
                break
            for item in batch:
                out.append({
                    "path": str(item.get("filename") or ""),
                    "status": str(item.get("status") or ""),
                    "additions": int(item.get("additions") or 0),
                    "deletions": int(item.get("deletions") or 0),
                })
            if len(batch) < 100:
                break
            page += 1
        return out[:limit]

    def pull_request_meta(self, pull_number: int) -> dict:
        """Title, body and the head/base the review is about."""
        response = self._request(
            "GET", f"/repos/{self.repository_full_name}/pulls/{pull_number}")
        head = response.get("head") or {}
        base = response.get("base") or {}
        return {"number": int(response["number"]), "url": str(response.get("html_url") or ""),
                "title": str(response.get("title") or ""), "body": str(response.get("body") or ""),
                "state": str(response.get("state") or ""),
                "head_ref": str(head.get("ref") or ""), "head_sha": str(head.get("sha") or ""),
                "base_ref": str(base.get("ref") or ""), "base_sha": str(base.get("sha") or "")}

    def update_pull_request_body(self, pull_number: int, body: str) -> None:
        self._request("PATCH", f"/repos/{self.repository_full_name}/pulls/{pull_number}",
                      {"body": body})

    def review_comments_of(self, pull_number: int, review_id: int) -> list[dict]:
        """The inline comments one review left: {id, path, line, body}."""
        response = self._request(
            "GET",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}/reviews/{review_id}"
            "/comments?per_page=100",
        )
        return [{"id": int(c["id"]), "path": str(c.get("path") or ""),
                 "line": c.get("line") or c.get("original_line"),
                 "body": str(c.get("body") or "")}
                for c in response or [] if c.get("id") is not None]

    def update_review_body(self, pull_number: int, review_id: int, body: str) -> None:
        self._request(
            "PUT",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}/reviews/{review_id}",
            {"body": body},
        )

    def resolve_review_thread(self, pull_number: int, comment_id: int) -> bool:
        """Mark the review thread that holds `comment_id` resolved (GraphQL resolveReviewThread;
        REST cannot). True when GitHub says it is resolved (BOSSAPP-948: Publish carries a
        local review's resolved threads)."""
        base = self.api_base_url
        graphql = (base[: -len("/v3")] + "/graphql") if base.endswith("/api/v3") \
            else base + "/graphql"
        owner, _, repo = self.repository_full_name.partition("/")
        found = self._send(
            "POST", graphql, headers=self._headers(),
            json={"query": "query($owner: String!, $repo: String!, $number: Int!) { repository("
                           "owner: $owner, name: $repo) { pullRequest(number: $number) { "
                           "reviewThreads(first: 100) { nodes { id isResolved comments(first: 1) "
                           "{ nodes { databaseId } } } } } } }",
                  "variables": {"owner": owner, "repo": repo, "number": int(pull_number)}},
        ).json()
        nodes = ((((((found or {}).get("data") or {}).get("repository") or {})
                   .get("pullRequest") or {}).get("reviewThreads") or {}).get("nodes") or [])
        thread = next((n for n in nodes if any(
            int(x.get("databaseId") or 0) == int(comment_id)
            for x in ((n.get("comments") or {}).get("nodes") or []))), None)
        if thread is None:
            return False
        if thread.get("isResolved"):
            return True
        done = self._send(
            "POST", graphql, headers=self._headers(),
            json={"query": "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) "
                           "{ thread { id isResolved } } }",
                  "variables": {"id": thread["id"]}},
        ).json()
        return bool(((((done or {}).get("data") or {}).get("resolveReviewThread") or {})
                     .get("thread") or {}).get("isResolved"))

    def reply_to_review_comment(self, pull_number: int, comment_id: int, body: str) -> int:
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}/comments/{comment_id}"
            "/replies",
            {"body": body},
        )
        return int(response["id"])

    # -- the change page's reads and writes (services/change_review.py, BOSSAPP-954) ----------

    def pull_request_view(self, pull_number: int) -> dict:
        """One read of the pull request for the change page: title, body, refs, state, merged."""
        r = self._request("GET", f"/repos/{self.repository_full_name}/pulls/{pull_number}")
        head = r.get("head") or {}
        base = r.get("base") or {}
        return {"number": int(r["number"]), "url": str(r.get("html_url") or ""),
                "title": str(r.get("title") or ""), "body": str(r.get("body") or ""),
                "state": str(r.get("state") or ""), "draft": bool(r.get("draft", False)),
                "merged": bool(r.get("merged", False)),
                "mergeable_state": str(r.get("mergeable_state") or ""),
                "head_ref": str(head.get("ref") or ""), "head_sha": str(head.get("sha") or ""),
                "base_ref": str(base.get("ref") or ""), "base_sha": str(base.get("sha") or ""),
                "user": str((r.get("user") or {}).get("login") or ""),
                "created_at": r.get("created_at"), "updated_at": r.get("updated_at")}

    def pull_request_commits(self, pull_number: int, *, limit: int = 250) -> list[dict]:
        """The pull request's commits, newest first (like `git log`): {sha, message, author, date}."""
        out: list[dict] = []
        page = 1
        while len(out) < limit:
            batch = self._request(
                "GET", f"/repos/{self.repository_full_name}/pulls/{pull_number}/commits"
                       f"?per_page=100&page={page}")
            if not batch:
                break
            for c in batch:
                info = c.get("commit") or {}
                out.append({"sha": str(c.get("sha") or ""),
                            "message": str(info.get("message") or "").split("\n")[0][:300],
                            "author": str(((c.get("author") or {}).get("login"))
                                          or (info.get("author") or {}).get("name") or ""),
                            "date": (info.get("author") or {}).get("date")})
            if len(batch) < 100:
                break
            page += 1
        return list(reversed(out[:limit]))

    def pull_request_review_comments(self, pull_number: int, *, limit: int = 1000) -> list[dict]:
        """Every inline review comment on the pull request, replies included."""
        out: list[dict] = []
        page = 1
        while len(out) < limit:
            batch = self._request(
                "GET", f"/repos/{self.repository_full_name}/pulls/{pull_number}/comments"
                       f"?per_page=100&page={page}")
            if not batch:
                break
            for c in batch:
                user = c.get("user") or {}
                out.append({"id": int(c["id"]), "in_reply_to_id": c.get("in_reply_to_id"),
                            "path": c.get("path"), "line": c.get("line"),
                            "original_line": c.get("original_line"), "side": c.get("side"),
                            "commit_id": c.get("commit_id"),
                            "original_commit_id": c.get("original_commit_id"),
                            "diff_hunk": str(c.get("diff_hunk") or "")[-4000:],
                            "body": str(c.get("body") or ""),
                            "user": str(user.get("login") or ""),
                            "bot": str(user.get("type") or "") == "Bot",
                            "created_at": c.get("created_at"), "updated_at": c.get("updated_at")})
            if len(batch) < 100:
                break
            page += 1
        return out[:limit]

    def pull_request_issue_comments(self, pull_number: int, *, limit: int = 300) -> list[dict]:
        """The pull request's conversation comments (not on a line)."""
        batch = self._request(
            "GET", f"/repos/{self.repository_full_name}/issues/{pull_number}/comments"
                   "?per_page=100") or []
        return [{"id": int(c["id"]), "body": str(c.get("body") or ""),
                 "user": str((c.get("user") or {}).get("login") or ""),
                 "bot": str((c.get("user") or {}).get("type") or "") == "Bot",
                 "created_at": c.get("created_at"), "updated_at": c.get("updated_at")}
                for c in batch[:limit] if c.get("id") is not None]

    def _graphql(self, query: str, variables: dict) -> dict:
        base = self.api_base_url
        url = (base[: -len("/v3")] + "/graphql") if base.endswith("/api/v3") else base + "/graphql"
        return self._send("POST", url, headers=self._headers(),
                          json={"query": query, "variables": variables}).json() or {}

    def _review_threads(self, pull_number: int) -> list[dict]:
        owner, _, repo = self.repository_full_name.partition("/")
        found = self._graphql(
            "query($owner: String!, $repo: String!, $number: Int!) { repository(owner: $owner, "
            "name: $repo) { pullRequest(number: $number) { reviewThreads(first: 100) { nodes { "
            "id isResolved comments(first: 1) { nodes { databaseId } } } } } } }",
            {"owner": owner, "repo": repo, "number": int(pull_number)})
        return ((((((found.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
                  .get("reviewThreads") or {}).get("nodes")) or [])

    def review_thread_states(self, pull_number: int) -> dict[int, bool]:
        """{the thread's first comment id: resolved?} (GraphQL; REST does not say)."""
        out: dict[int, bool] = {}
        for n in self._review_threads(pull_number):
            first = ((n.get("comments") or {}).get("nodes") or [{}])[0]
            if first.get("databaseId"):
                out[int(first["databaseId"])] = bool(n.get("isResolved"))
        return out

    def set_review_thread_resolved(self, pull_number: int, comment_id: int, resolved: bool) -> bool:
        """Resolve (or unresolve) the review thread that starts with `comment_id`."""
        if resolved:
            return self.resolve_review_thread(pull_number, comment_id)
        thread = next((n for n in self._review_threads(pull_number) if any(
            int(x.get("databaseId") or 0) == int(comment_id)
            for x in ((n.get("comments") or {}).get("nodes") or []))), None)
        if thread is None:
            return False
        if not thread.get("isResolved"):
            return True
        done = self._graphql("mutation($id: ID!) { unresolveReviewThread(input: {threadId: $id}) "
                             "{ thread { id isResolved } } }", {"id": thread["id"]})
        return not bool(((((done.get("data") or {}).get("unresolveReviewThread") or {})
                          .get("thread") or {}).get("isResolved")))

    def create_review_comment(self, pull_number: int, *, body: str, commit_id: str, path: str,
                              line: int, side: str = "RIGHT") -> int:
        """A comment on one line of the pull request's diff."""
        response = self._request(
            "POST", f"/repos/{self.repository_full_name}/pulls/{pull_number}/comments",
            {"body": body, "commit_id": commit_id, "path": path, "line": int(line),
             "side": "LEFT" if str(side).upper() == "LEFT" else "RIGHT"})
        return int(response["id"])

    def branch_commits(self, branch: str, *, limit: int = 50) -> list[dict]:
        """A branch's commits, newest first: {sha, parents, message, author, date}."""
        batch = self._request(
            "GET", f"/repos/{self.repository_full_name}/commits?sha={quote(branch, safe='')}"
                   f"&per_page={max(1, min(int(limit), 100))}") or []
        out = []
        for c in batch:
            info = c.get("commit") or {}
            out.append({"sha": str(c.get("sha") or ""),
                        "parents": [str(p.get("sha") or "") for p in (c.get("parents") or [])],
                        "message": str(info.get("message") or "").split("\n")[0][:300],
                        "author": str(((c.get("author") or {}).get("login"))
                                      or (info.get("author") or {}).get("name") or ""),
                        "date": (info.get("author") or {}).get("date")})
        return out

    def compare_diff(self, base_sha: str, head_sha: str) -> str:
        """The unified diff between two commits (base...head)."""
        return self._request(
            "GET", f"/repos/{self.repository_full_name}/compare/"
                   f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}",
            accept="application/vnd.github.v3.diff", response_text=True)

    def commit_detail(self, sha: str) -> dict:
        """One commit: its message, author, parent, files and unified diff."""
        path = f"/repos/{self.repository_full_name}/commits/{quote(sha, safe='')}"
        r = self._request("GET", path)
        diff = self._request("GET", path, accept="application/vnd.github.v3.diff",
                             response_text=True)
        info = r.get("commit") or {}
        parents = r.get("parents") or []
        return {"sha": str(r.get("sha") or sha),
                "parent": (parents[0] or {}).get("sha") if parents else None,
                "parents": [str((p or {}).get("sha") or "") for p in parents],
                "author": str(((r.get("author") or {}).get("login"))
                              or (info.get("author") or {}).get("name") or ""),
                "date": (info.get("author") or {}).get("date"),
                "message": str(info.get("message") or ""),
                "files": [{"path": str(f.get("filename") or ""), "status": str(f.get("status") or ""),
                           "additions": int(f.get("additions") or 0),
                           "deletions": int(f.get("deletions") or 0)}
                          for f in (r.get("files") or [])[:300]],
                "diff": str(diff)[:400_000]}

    def read_blob(self, ref: str, path: str) -> Optional[bytes]:
        """A file's bytes at a ref, or None when it is not there."""
        where = (f"/repos/{self.repository_full_name}/contents/{quote(path)}"
                 f"?ref={quote(ref, safe='')}")
        try:
            payload = self._request("GET", where)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise
        if isinstance(payload, list):
            return None
        encoded = payload.get("content")
        if isinstance(encoded, str) and encoded:
            return base64.b64decode(encoded)
        if int(payload.get("size") or 0) > 0:
            # Over 1 MB the contents API sends no content: the raw media type does.
            raw = self._request("GET", where, accept="application/vnd.github.raw",
                                response_text=True)
            return str(raw).encode("utf-8")
        return b""

    def get_pull_request_diff(self, pull_number: int) -> str:
        return self._request(
            "GET",
            f"/repos/{self.repository_full_name}/pulls/{pull_number}",
            accept="application/vnd.github.v3.diff",
            response_text=True,
        )

    def create_deployment(
        self, ref: str, environment: str, description: str
    ) -> DeploymentResult:
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/deployments",
            {
                "ref": ref,
                "environment": environment,
                "description": description,
                "auto_merge": False,
                "required_contexts": [],
                "production_environment": False,
                "transient_environment": True,
            },
        )
        return DeploymentResult(
            id=int(response["id"]),
            environment=response["environment"],
            sha=response["sha"],
            url=response["url"],
        )

    def mark_deployment_success(
        self,
        deployment_id: int,
        environment: str,
        *,
        environment_url: Optional[str] = None,
        description: Optional[str] = None,
    ) -> DeploymentStatusResult:
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/deployments/{deployment_id}/statuses",
            {
                "state": "success",
                "environment": environment,
                **(
                    {"environment_url": environment_url}
                    if environment_url
                    else {}
                ),
                **({"description": description} if description else {}),
            },
        )
        return DeploymentStatusResult(
            state=response["state"],
            url=response["url"],
            environment_url=response.get("environment_url"),
        )

    def mark_deployment_failure(
        self,
        deployment_id: int,
        environment: str,
        *,
        environment_url: Optional[str] = None,
        description: Optional[str] = None,
    ) -> DeploymentStatusResult:
        response = self._request(
            "POST",
            f"/repos/{self.repository_full_name}/deployments/{deployment_id}/statuses",
            {
                "state": "failure",
                "environment": environment,
                **(
                    {"environment_url": environment_url}
                    if environment_url
                    else {}
                ),
                **({"description": description} if description else {}),
            },
        )
        return DeploymentStatusResult(
            state=response["state"],
            url=response["url"],
            environment_url=response.get("environment_url"),
        )

    def wait_for_commit_checks(
        self,
        head_sha: str,
        *,
        timeout_seconds: int,
        poll_interval_seconds: int,
    ) -> PullRequestValidationResult:
        from .pipeline_adapter import GitHubActionsPipeline

        return GitHubActionsPipeline(
            self.settings,
            github=self,
        ).wait_for_verdict(
            head_sha,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        ).to_pull_request_validation()

    def _evaluate_check_runs(
        self, head_sha: str, check_runs: list[PullRequestValidationCheck]
    ) -> PullRequestValidationResult:
        if not check_runs:
            return PullRequestValidationResult(
                status="pending",
                summary="GitHub has not published validation checks for this commit yet.",
                head_sha=head_sha,
                completed=False,
                checks=[],
            )

        if any(check.status != "completed" for check in check_runs):
            return PullRequestValidationResult(
                status="pending",
                summary="GitHub validation checks are still running for this proposal.",
                head_sha=head_sha,
                completed=False,
                checks=check_runs,
            )

        failing = [
            check
            for check in check_runs
            if (check.conclusion or "").lower()
            not in {"success", "neutral", "skipped"}
        ]
        if failing:
            failed_names = ", ".join(check.name for check in failing[:3])
            if len(failing) > 3:
                failed_names = f"{failed_names}, and {len(failing) - 3} more"
            return PullRequestValidationResult(
                status="failed",
                summary=f"GitHub validation failed: {failed_names}.",
                head_sha=head_sha,
                completed=True,
                checks=check_runs,
            )

        passed_names = ", ".join(check.name for check in check_runs[:3])
        if len(check_runs) > 3:
            passed_names = f"{passed_names}, and {len(check_runs) - 3} more"
        return PullRequestValidationResult(
            status="passed",
            summary=f"GitHub validation passed: {passed_names}.",
            head_sha=head_sha,
            completed=True,
            checks=check_runs,
        )
