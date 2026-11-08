"""PUBLISH TO GITHUB LATER (BOSSAPP-921): a local project's repository goes to GitHub — its whole
history, every branch and tag, its open change sets as pull requests — and the project switches
to the GitHub repository. The local repository stays as a mirror.

    POST /v1/projects/{id}/publish {owner, repo, private, create}

A person's decision (the compliance boundary "Talking to the outside"): the route requires a
signed-in person holding `repository:manage` on the project, and nothing here runs on its own.

IDEMPOTENT AND RESUMABLE. Each step records itself in the bare repository's marker
(`publish`: target, the step reached, the change sets already turned into pull requests). A
publish that stopped half way (GitHub went away, a push was refused) is run again with the same
call and starts where it stopped; a publish that finished answers with what it did.

  1. the repository: made on GitHub (`create_repository`, below) or an existing EMPTY one, or one
     that already holds exactly this history (a re-run);
  2. push: every branch and tag, never forced (a GitHub repository with other history is refused
     in words);
  3. pull requests: each open local change set becomes a GitHub pull request (GitHub's own
     one-PR-per-branch rule makes a re-run reuse it); its build, its review decision and its
     change set now name the pull request;
  4. switch: the project's primary repository becomes the GitHub one (host github, through the
     project's GitHub connection); the local binding stays as a `mirror`.

`create_repository` is the minimal repository-creation call BOSSAPP-917's UI will use. It
answers in words when the GitHub App lacks Administration permission, or when the account is a
personal one an App cannot create repositories in.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

import httpx
from sqlalchemy import text

from . import local_repo
from .core import emit_event

log = logging.getLogger(__name__)

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class PublishError(RuntimeError):
    def __init__(self, message: str, status: int = 422, error: str = "publish_refused",
                 step: str = ""):
        super().__init__(message)
        self.status, self.error, self.step = status, error, step

    def body(self) -> dict[str, Any]:
        return {"error": self.error, "message": str(self), "step": self.step}


# ── making the GitHub repository (the call BOSSAPP-917's UI uses) ────────────────────────────

@dataclass
class CreateRepoResult:
    ok: bool
    full_name: str
    created: bool = False
    exists: bool = False
    url: str = ""
    words: str = ""
    reason: str = ""            # "" | no_admin_permission | personal_account | not_allowed | failed
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "full_name": self.full_name, "created": self.created,
                "exists": self.exists, "url": self.url, "words": self.words, "reason": self.reason}


def create_repository(*, owner: str, name: str, private: bool = True, token: str,
                      api_base_url: str = "https://api.github.com", token_kind: str = "app",
                      description: str = "", client: Optional[httpx.Client] = None) -> CreateRepoResult:
    """Make `owner/name` on GitHub, empty (no README: the history comes from BOSS).

    `token_kind` "app" (an installation token) can create only in an ORGANIZATION that granted
    the App Administration: write; a personal account needs a person's own token ("user").
    An already existing repository is reported as `exists` (the caller decides whether it may be
    used). Never raises for GitHub's answers: they come back as words."""
    full = f"{owner}/{name}"
    if not _OWNER.match(owner or "") or not _NAME.match(name or ""):
        return CreateRepoResult(False, full, reason="not_allowed",
                                words="A GitHub owner is letters, digits and hyphens; a repository "
                                      "name is letters, digits, '.', '_' and '-'.")
    base = api_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "boss-publish"}
    own = client or httpx.Client(timeout=20)
    try:
        found = own.get(f"{base}/repos/{full}", headers=headers)
        if found.status_code == 200:
            data = found.json()
            return CreateRepoResult(True, full, exists=True, url=str(data.get("html_url") or ""),
                                    words=f"{full} already exists on GitHub.",
                                    details={"private": data.get("private"),
                                             "default_branch": data.get("default_branch")})
        account = own.get(f"{base}/users/{owner}", headers=headers)
        kind = str((account.json() if account.status_code == 200 else {}).get("type") or "")
        if account.status_code == 404:
            return CreateRepoResult(False, full, reason="not_allowed",
                                    words=f"There is no GitHub account {owner}.")
        body = {"name": name, "private": bool(private), "auto_init": False,
                "description": description[:300]}
        if kind == "Organization":
            made = own.post(f"{base}/orgs/{owner}/repos", headers=headers, json=body)
        elif token_kind == "user":
            made = own.post(f"{base}/user/repos", headers=headers, json=body)
        else:
            return CreateRepoResult(
                False, full, reason="personal_account",
                words=(f"{owner} is a personal GitHub account, and a GitHub App cannot create "
                       f"repositories in one. Create {full} on GitHub (empty: no README, no "
                       ".gitignore), then publish to it as an existing repository."))
        if made.status_code in (200, 201):
            data = made.json()
            return CreateRepoResult(True, str(data.get("full_name") or full), created=True,
                                    url=str(data.get("html_url") or ""),
                                    words=f"Made {full} on GitHub ({'private' if private else 'public'}).")
        said = ""
        try:
            said = str(made.json().get("message") or "")
        except ValueError:
            said = made.text[:200]
        if made.status_code == 422 and "already exists" in said.lower():
            return CreateRepoResult(True, full, exists=True, words=f"{full} already exists on GitHub.")
        if made.status_code in (401, 403, 404):
            return CreateRepoResult(
                False, full, reason="no_admin_permission",
                words=(f"GitHub refused to create {full}: the GitHub App needs the "
                       "'Administration: Read and write' permission on "
                       f"{owner} (GitHub › Settings › GitHub Apps › BOSS › Permissions), or create "
                       "the repository on GitHub and publish to it as an existing one."),
                details={"status": made.status_code, "github": said[:200]})
        return CreateRepoResult(False, full, reason="failed",
                                words=f"GitHub did not create {full} ({made.status_code}): {said[:200]}",
                                details={"status": made.status_code})
    except httpx.HTTPError as error:
        return CreateRepoResult(False, full, reason="failed",
                                words=f"GitHub did not answer ({type(error).__name__}); try again.")
    finally:
        if client is None:
            own.close()


# ── how BOSS reaches GitHub for this project ─────────────────────────────────────────────────

@dataclass
class GitHubReach:
    """What publish needs: an adapter for owner/repo (pull requests), BOSS's own git access (the
    push), a token for creating the repository and what kind it is, and the connection a new
    binding hangs off."""
    adapter_for: Callable[[str, str], Any]
    access_for: Callable[[str, str], Any]
    create_token: Optional[str]
    create_token_kind: str
    api_base_url: str
    connection_id: Optional[str]


def default_reach(c: Any, settings: Any, project_id: str, *, connection_id: Optional[str] = None,
                  actor: str = "operator", owner: str = "", repo: str = "") -> GitHubReach:
    from .github_adapter import GitHubAdapter
    from .github_connections import github_token_for
    from .repo_host import git_access

    def token(o: str, r: str) -> str:
        return github_token_for(c, settings, project_id, o, r) or ""

    def adapter(o: str, r: str) -> Any:
        return GitHubAdapter(settings, owner=o, repo=r, token=token(o, r) or None)

    create_token, kind = None, "app"
    try:
        from .github_app_adapter import GitHubAppAdapter

        app = GitHubAppAdapter(settings)
        if app.configured:
            create_token = app.installation_token(permissions={"administration": "write",
                                                               "contents": "write"}).token
    except Exception:  # noqa: BLE001 — no App, or not granted: a person's own token may do
        create_token = None
    if not create_token and getattr(settings, "github_token", ""):
        create_token, kind = str(settings.github_token), "user"
    conn_id = connection_id
    if conn_id is None:
        try:
            from ..multirepo.project_repos import _connection_for

            conn_id = _connection_for(c, settings, project_id, actor, owner=owner, repo=repo)
        except Exception:  # noqa: BLE001 — said at the switch step
            conn_id = None
    return GitHubReach(adapter_for=adapter,
                       access_for=lambda o, r: git_access("github", o, r, token(o, r)),
                       create_token=create_token, create_token_kind=kind,
                       api_base_url=str(getattr(settings, "github_api_base_url", "")
                                        or "https://api.github.com"),
                       connection_id=conn_id)


# ── publish ──────────────────────────────────────────────────────────────────────────────────

def _state(path: Any) -> dict[str, Any]:
    return dict(local_repo.marker_of(path).get("publish") or {})


def _save(path: Any, state: Mapping[str, Any]) -> None:
    local_repo.update_marker(path, publish=dict(state))


def publish(c: Any, settings: Any, project_id: str, *, owner: str, repo: str,
            private: bool = True, create: bool = True, actor: str,
            reach: Optional[GitHubReach] = None) -> dict[str, Any]:
    """Publish the project's local repository to github.com/owner/repo (see the module doc)."""
    from .local_projects import local_binding

    owner, repo = str(owner or "").strip(), str(repo or "").strip()
    if not _OWNER.match(owner) or not _NAME.match(repo):
        raise PublishError("Give the GitHub owner and a repository name (letters, digits, '.', "
                           "'_', '-').", 422, "bad_target", "input")
    binding = local_binding(c, project_id, include_mirror=True)
    if binding is None:
        done = c.execute(text(
            "SELECT full_name FROM github_repository_bindings WHERE project_id = :p AND host = 'github' "
            "AND status = 'active' AND (metadata->>'published_from') IS NOT NULL LIMIT 1"),
            {"p": project_id}).scalar()
        if done:
            return {"project_id": project_id, "status": "published", "full_name": done,
                    "words": f"Already published to {done}.", "steps": []}
        raise PublishError("This project's code is not local: there is nothing to publish.", 409,
                           "not_local", "input")
    path = local_repo.bare_path(binding["owner"], binding["name"], settings)
    if not local_repo.exists(binding["owner"], binding["name"], settings):
        raise PublishError(f"The local repository {binding['full_name']} is missing on this "
                           "machine.", 409, "local_repo_missing", "input")
    state = _state(path)
    target = f"{owner}/{repo}"
    if state.get("target") and state["target"].lower() != target.lower() and state.get("step") != "switched":
        raise PublishError(f"A publish to {state['target']} is in progress; finish it (publish "
                           f"to {state['target']} again) before choosing another repository.", 409,
                           "publish_in_progress", "input")
    reach = reach or default_reach(c, settings, project_id, actor=actor, owner=owner, repo=repo)
    steps: list[dict[str, Any]] = []
    state.update({"target": target, "private": bool(private), "by": actor})
    _save(path, state)

    # 1. the repository on GitHub
    if state.get("step") not in ("pushed", "pull_requests", "switched"):
        if create:
            if not reach.create_token:
                raise PublishError(
                    "BOSS has no GitHub credential that can create repositories. Connect the GitHub "
                    "App (with Administration: Read and write) or create the repository on GitHub "
                    "and publish to it as an existing one.", 409, "no_github_credential", "repository")
            made = create_repository(owner=owner, name=repo, private=private,
                                     token=reach.create_token, api_base_url=reach.api_base_url,
                                     token_kind=reach.create_token_kind)
            if not made.ok:
                raise PublishError(made.words, 409 if made.reason != "failed" else 502,
                                   made.reason or "create_failed", "repository")
            steps.append({"key": "repository", "ok": True, "words": made.words})
        state["step"] = "repository"
        _save(path, state)

    # 2. push every branch and tag (never forced)
    access = reach.access_for(owner, repo)
    env = access.boss_environ() if hasattr(access, "boss_environ") else dict(getattr(access, "env", {}) or {})
    if state.get("step") in ("repository", None):
        remote = local_repo.git(["ls-remote", "--heads", access.url], git_dir=path, env=env,
                                check=False, timeout=120)
        for line in remote.splitlines():
            sha, _, ref = line.partition("\t")
            if sha and local_repo.rev_parse(path, sha) is None:
                raise PublishError(
                    f"{target} already holds other history ({ref.replace('refs/heads/', '')} is not "
                    "from this project). Publish to an empty repository.", 409,
                    "target_not_empty", "push")
        try:
            local_repo.push(path, access.url, ["refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*"],
                            env=env)
        except local_repo.LocalRepoError as error:
            raise PublishError(f"Pushing to {target} failed: {error}", 502, "push_failed", "push") from None
        state["step"] = "pushed"
        _save(path, state)
        steps.append({"key": "push", "ok": True,
                      "words": f"Pushed {len(local_repo.branches(path))} branch(es) with their history."})

    # 3. open change sets → pull requests
    if state.get("step") in ("pushed", "pull_requests"):
        adapter = reach.adapter_for(owner, repo)
        opened = dict(state.get("prs") or {})
        rows = c.execute(text(
            "SELECT * FROM local_change_sets WHERE project_id = :p AND repository = :r AND "
            "state = 'open' ORDER BY number"), {"p": project_id, "r": binding["full_name"]}).mappings().all()
        from .github_outage import repoint_pull_request
        from .local_repo_host import change_set_url

        for row in rows:
            key = str(row["number"])
            try:
                pr = adapter.create_pull_request(row["branch"], row["title"] or row["branch"],
                                                 (row["body"] or "") + "\n\n> Opened on this machine "
                                                 "before the project was published to GitHub.",
                                                 base=row["base_branch"])
            except Exception as error:  # noqa: BLE001 — resumable: the next run continues here
                state["step"], state["prs"] = "pull_requests", opened
                _save(path, state)
                raise PublishError(f"Opening the pull request for change #{row['number']} failed: "
                                   f"{error}", 502, "pull_request_failed", "pull_requests") from None
            opened[key] = pr.url
            c.execute(text(
                "UPDATE local_change_sets SET state = 'published', github_pr_url = :u, "
                "github_pr_number = :n, updated_at = now() WHERE id = :i"),
                {"u": pr.url, "n": int(pr.number), "i": row["id"]})
            repoint_pull_request(c, old_url=change_set_url(binding["full_name"], row["number"]),
                                 new_url=pr.url, number=int(pr.number), run_id=row["run_id"])
            state["prs"] = opened
            _save(path, state)
        state["step"] = "pull_requests"
        _save(path, state)
        if rows:
            steps.append({"key": "pull_requests", "ok": True,
                          "words": f"Opened {len(rows)} pull request(s) from open changes."})

    # 4. the project now builds on GitHub; the local repository stays as a mirror
    if state.get("step") == "pull_requests":
        if not reach.connection_id:
            raise PublishError("Connect GitHub for this project (Setup › GitHub access) so BOSS can "
                               f"build on {target}; the code is already on GitHub.", 409,
                               "github_not_connected", "switch")
        default = local_repo.default_branch(path)
        gh_id = c.execute(text(
            "SELECT id FROM github_repository_bindings WHERE project_id = :p AND lower(full_name) = "
            "lower(:f)"), {"p": project_id, "f": target}).scalar()
        meta = json.dumps({"published_from": binding["full_name"], "setup": "publish"})
        c.execute(text(
            "UPDATE github_repository_bindings SET role = 'mirror', updated_at = now(), "
            "metadata = metadata || CAST(:m AS JSONB) WHERE id = :i"),
            {"m": json.dumps({"published_to": target}), "i": binding["id"]})
        if gh_id:
            c.execute(text(
                "UPDATE github_repository_bindings SET status = 'active', role = 'primary', host = "
                "'github', connection_id = :c, default_branch = :d, metadata = metadata || "
                "CAST(:m AS JSONB), updated_at = now() WHERE id = :i"),
                {"c": reach.connection_id, "d": default, "m": meta, "i": gh_id})
        else:
            from .core import make_id

            gh_id = make_id("ghrepo")
            c.execute(text(
                "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
                "full_name, role, default_branch, visibility, host, metadata, created_by) VALUES "
                "(:id, :p, :c, :o, :n, :f, 'primary', :d, :v, 'github', CAST(:m AS JSONB), :a)"),
                {"id": gh_id, "p": project_id, "c": reach.connection_id, "o": owner, "n": repo,
                 "f": target, "d": default, "v": "private" if private else "public", "m": meta,
                 "a": actor})
        state["step"] = "switched"
        _save(path, state)
        local_repo.update_marker(path, published={"to": target, "by": actor})
        steps.append({"key": "switch", "ok": True,
                      "words": f"The project now builds on {target}; the local copy stays as a mirror."})
        emit_event(c, project_id, "project.published_to_github", actor, "project", project_id,
                   {"from": binding["full_name"], "to": target, "pull_requests": state.get("prs") or {}})
    return {"project_id": project_id, "status": "published", "full_name": target,
            "pull_requests": state.get("prs") or {}, "steps": steps,
            "words": f"Published to {target}." if steps else f"Already published to {target}."}


__all__ = ["CreateRepoResult", "GitHubReach", "PublishError", "create_repository",
           "default_reach", "publish"]
