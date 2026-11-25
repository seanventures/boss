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


def _send_base(token: Any, api_base_url: str) -> tuple[str, str]:
    """(REST base, refusal words). The CREDENTIAL'S OWN host when it carries one, else the base
    asked for — and the send-point check (BOSSAPP-1286): a token never goes to another host."""
    from ..identity import github_urls

    base = (github_urls.credential_api_base(token) if isinstance(token, github_urls.GitHubCredential)
            else github_urls.api_base(api_base_url)).rstrip("/")
    try:
        github_urls.check_send(token, base, where="local_publish")
    except github_urls.CrossHostCredential:
        return base, ("The credential BOSS holds for this is for another GitHub host; connect a "
                      "credential for this host in Setup › GitHub access.")
    return base, ""


def create_repository(*, owner: str, name: str, private: bool = True, token: str,
                      api_base_url: str = "", token_kind: str = "app",
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
    base, refused = _send_base(token, api_base_url)
    if refused:
        return CreateRepoResult(False, full, reason="credential_host_mismatch", words=refused)
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
            lacks = (f"the access token for {owner} needs the repo scope (and, for an organisation, "
                     f"its user must be a member of {owner})" if token_kind == "user" else
                     "the GitHub App needs the 'Administration: Read and write' permission on "
                     f"{owner} (GitHub › Settings › GitHub Apps › BOSS › Permissions)")
            return CreateRepoResult(
                False, full, reason="no_admin_permission",
                words=(f"GitHub refused to create {full}: {lacks}, or create "
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


#: Said when nothing resolved a credential and the caller gave no better words (repo_creation
#: names the missing permission per credential; this is the no-credential-at-all fallback).
NO_CREDENTIAL_WORDS = ("BOSS has no GitHub credential that can create repositories: connect the "
                       "GitHub App with Administration: Read and write.")


def check_repository(*, owner: str, name: str, token: Optional[str], purpose: str = "create",
                     api_base_url: str = "", token_kind: str = "app",
                     client: Optional[httpx.Client] = None, credential_words: str = "",
                     via: str = "") -> dict[str, Any]:
    """BEFORE A PERSON PRESSES CREATE (BOSSAPP-917/921's sheets): can BOSS make `owner/name` (or,
    `purpose="existing"`, publish into it)? Reads only — the same answers create_repository
    gives, in the same words, so the sheet says them while the person is still choosing.

    {ok, reason, words, exists, owner_type, via}; reason "" | no_github_credential | not_allowed |
    personal_account | exists | not_found | failed. `credential_words`: the resolver's precise
    reason when there is no token (which credential lacks what; BOSSAPP-1160); `via`: the label
    of the credential that will create ("will create as …")."""
    full = f"{owner}/{name}"
    out: dict[str, Any] = {"ok": False, "full_name": full, "reason": "", "words": "",
                           "exists": False, "owner_type": None, "via": via or None}
    if not _OWNER.match(owner or "") or not _NAME.match(name or ""):
        return {**out, "reason": "not_allowed",
                "words": "A GitHub owner is letters, digits and hyphens; a repository name is "
                         "letters, digits, '.', '_' and '-'."}
    if not token:
        return {**out, "reason": "no_github_credential",
                "words": credential_words or NO_CREDENTIAL_WORDS}
    base, refused = _send_base(token, api_base_url)
    if refused:
        return {**out, "reason": "credential_host_mismatch", "words": refused}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "boss-publish"}
    own = client or httpx.Client(timeout=15)
    try:
        found = own.get(f"{base}/repos/{full}", headers=headers)
        if found.status_code == 200:
            out["exists"] = True
            if purpose == "existing":
                return {**out, "ok": True, "words": f"{full} exists: BOSS publishes into it if it is empty."}
            return {**out, "reason": "exists",
                    "words": f"{full} already exists on GitHub: pick another name, or publish to it "
                             "as an existing (empty) repository."}
        if found.status_code >= 500:
            return {**out, "reason": "failed", "words": f"GitHub did not answer ({found.status_code}); try again."}
        if purpose == "existing":
            return {**out, "reason": "not_found",
                    "words": (f"{via or 'The credential BOSS used'} cannot see {full}: it may exist, "
                              "and GitHub shows a repository only to a credential with access to it. "
                              "Give the BOSS GitHub App's installation (or a token) access to it in "
                              "Setup › GitHub access.")}
        account = own.get(f"{base}/users/{owner}", headers=headers)
        if account.status_code == 404:
            return {**out, "reason": "not_allowed", "words": f"There is no GitHub account {owner}."}
        if account.status_code >= 400:
            return {**out, "reason": "failed",
                    "words": f"GitHub did not answer ({account.status_code}); try again."}
        kind = str((account.json() or {}).get("type") or "")
        out["owner_type"] = kind or None
        if kind != "Organization" and token_kind != "user":
            return {**out, "reason": "personal_account",
                    "words": (f"{owner} is a personal GitHub account, and a GitHub App cannot create "
                              f"repositories in one. Create {full} on GitHub (empty: no README, no "
                              ".gitignore), then publish to it as an existing repository.")}
        return {**out, "ok": True,
                "words": f"{full} is free on GitHub." + (f" BOSS will create it as {via}." if via else "")}
    except httpx.HTTPError as error:
        return {**out, "reason": "failed", "words": f"GitHub did not answer ({type(error).__name__}); try again."}
    finally:
        if client is None:
            own.close()


def registry_refusal(c: Any, project_id: str, full_name: str) -> Optional[str]:
    """Words when the organisation's repository registry was told NOT to use `full_name` by a
    person (unticked in Repositories), else None. A repository the registry merely has not
    allowed yet is allowed by publishing to it (registry.register_binding: picking it is the
    allow), so it never blocks the publish silently (BOSSAPP-1338)."""
    try:
        from ..multirepo import registry as repo_registry

        org_id = repo_registry.org_of_project(c, project_id)
        if not org_id:
            return None
        with c.begin_nested():
            row = c.execute(text(
                "SELECT allowed, allowed_by, archived FROM github_repos WHERE org_id = :o AND "
                "lower(full_name) = lower(:f)"), {"o": org_id, "f": full_name}).mappings().first()
    except Exception:  # noqa: BLE001 — no registry on this schema
        return None
    if row is None or row["allowed"]:
        return None
    if row.get("archived"):
        return f"{full_name} is archived on GitHub: BOSS cannot publish into it."
    if row.get("allowed_by"):
        return (f"{full_name} is switched off in Repositories (by {row['allowed_by']}): tick it there "
                "to let BOSS use it, then publish.")
    return None


def check_existing(c: Any, settings: Any, project_id: str, *, owner: str, name: str,
                   client: Optional[httpx.Client] = None,
                   actor: Optional[str] = None) -> dict[str, Any]:
    """Publish INTO an existing repository: can BOSS reach AND push to `owner/name`, and with
    which credential (repo_access.resolve: the App's installation, this project's binding, the
    token connections, the server token)? Never "there is no" when the truth is "cannot see"
    (BOSSAPP-1338): the words name every credential tried and the fix."""
    from .repo_access import resolve

    full = f"{owner}/{name}"
    out: dict[str, Any] = {"ok": False, "full_name": full, "reason": "", "words": "",
                           "exists": False, "owner_type": None, "via": None, "tried": []}
    if not _OWNER.match(owner or "") or not _NAME.match(name or ""):
        return {**out, "reason": "not_allowed",
                "words": "A GitHub owner is letters, digits and hyphens; a repository name is "
                         "letters, digits, '.', '_' and '-'."}
    got = resolve(c, settings, project_id, owner, name, need="push", client=client, actor=actor)
    out.update(tried=got.tried, via=got.label or None, exists=bool(got.exists))
    if not got.ok:
        return {**out, "reason": got.reason or "not_visible", "words": got.words}
    refused = registry_refusal(c, project_id, full)
    if refused:
        return {**out, "reason": "registry_off", "words": refused}
    return {**out, "ok": True,
            "words": f"{full} exists: BOSS publishes into it if it is empty, as {got.label}."}


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
    #: Which credential creates the repository ("boss-localhost App", "paramg (access token, …)")
    #: and, when none can, the resolver's precise reason (BOSSAPP-1160).
    create_label: str = ""
    create_words: str = ""
    create_reason: str = ""
    #: Words when no credential reaches owner/repo to push (repo_access), else "" — None: not
    #: asked (a caller's own reach).
    reach_words_for: Optional[Callable[[str, str], str]] = None


def default_reach(c: Any, settings: Any, project_id: str, *, connection_id: Optional[str] = None,
                  actor: str = "operator", owner: str = "", repo: str = "") -> GitHubReach:
    """How BOSS reaches GitHub for this project. The repository-CREATING credential comes from
    the one resolver (repo_creation.repo_creation_credential): the Identity-registered App's
    installation on `owner` when it holds administration, the environment App, a project
    connection's token for its own account, the install token — in that order, with the reason."""
    from .github_adapter import GitHubAdapter
    from .github_connections import api_base_for_repository, github_access_for
    from .repo_creation import repo_creation_credential
    from .repo_host import git_access
    from ..identity import github_urls

    reached: dict[tuple[str, str], Any] = {}

    def resolved(o: str, r: str) -> Any:
        """The resolver's answer for o/r, asked once per reach (#179 review): the push, the pull
        requests and the words all read the same answer."""
        key = (o.lower(), r.lower())
        if key not in reached:
            from .repo_access import resolve

            try:
                reached[key] = resolve(c, settings, project_id, o, r, need="push", actor=actor)
            except Exception:  # noqa: BLE001 — the bound credential below
                log.debug("repo access: the resolver failed", exc_info=True)
                reached[key] = None
        return reached[key]

    def access(o: str, r: str) -> tuple[str, str]:
        """(token, REST base) that REACH o/r to push and open pull requests
        (repo_access.resolve), else the project's bound pair: never the creating credential
        borrowed for a read or a push (BOSSAPP-1338), and never a token on another host's base
        (BOSSAPP-1286: the resolver chooses both together)."""
        got = resolved(o, r)
        if getattr(got, "token", None):
            return got.token, got.api_base or api_base_for_repository(c, project_id, o, r)
        return github_access_for(c, settings, project_id, o, r)

    def token(o: str, r: str) -> str:
        return access(o, r)[0] or ""

    def adapter(o: str, r: str) -> Any:
        tok, base = access(o, r)
        return GitHubAdapter(settings, owner=o, repo=r, token=tok or None, api_base_url=base)

    create_token, kind, label, words, reason = None, "app", "", "", ""
    if owner:
        try:
            got = repo_creation_credential(c, settings, owner, project_id=project_id, actor=actor)
            create_token, kind, label, words, reason = got.token, got.token_kind, got.label, got.words, got.reason
        except Exception:  # noqa: BLE001 — said at the repository step
            log.debug("repo creation: the credential resolver failed", exc_info=True)
            create_token, reason, words = None, "no_github_credential", NO_CREDENTIAL_WORDS
    conn_id = connection_id
    if conn_id is None:
        try:
            from ..multirepo.project_repos import _connection_for

            conn_id = _connection_for(c, settings, project_id, actor, owner=owner, repo=repo)
        except Exception:  # noqa: BLE001 — said at the switch step
            conn_id = None
    def reach_words(o: str, r: str) -> str:
        if token(o, r):
            return ""
        got = resolved(o, r)
        return (getattr(got, "words", "") or f"BOSS holds no GitHub credential that reaches {o}/{r}.")

    return GitHubReach(adapter_for=adapter, reach_words_for=reach_words,
                       access_for=lambda o, r: git_access(
                           "github", o, r, access(o, r)[0],
                           web_base=github_urls.web_base_for(access(o, r)[1])),
                       create_token=create_token, create_token_kind=kind,
                       api_base_url=api_base_for_repository(c, project_id, owner, repo),
                       connection_id=conn_id, create_label=label, create_words=words, create_reason=reason)


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
    # Another target while one is half done is refused once anything reached it (a push, pull
    # requests). Before that — the first target was refused, or only made — the person may pick
    # another repository (the Publish sheet: "it already holds other history" → a new name).
    if state.get("target") and state["target"].lower() != target.lower() and \
            state.get("step") not in (None, "repository", "switched"):
        raise PublishError(f"A publish to {state['target']} is in progress; finish it (publish "
                           f"to {state['target']} again) before choosing another repository.", 409,
                           "publish_in_progress", "input")
    reach = reach or default_reach(c, settings, project_id, actor=actor, owner=owner, repo=repo)
    steps: list[dict[str, Any]] = []
    if state.get("target") and state["target"].lower() != target.lower() and state.get("step") == "repository":
        state["step"] = None    # a new target starts at its own repository step
    state.update({"target": target, "private": bool(private), "by": actor})
    _save(path, state)

    # 1. the repository on GitHub
    if state.get("step") not in ("pushed", "pull_requests", "switched"):
        if create:
            if not reach.create_token:
                raise PublishError(
                    (reach.create_words or
                     "BOSS has no GitHub credential that can create repositories. Connect the GitHub "
                     "App (with Administration: Read and write)") +
                    " Or create the repository on GitHub and publish to it as an existing one.",
                    409, "no_github_credential", "repository")
            made = create_repository(owner=owner, name=repo, private=private,
                                     token=reach.create_token, api_base_url=reach.api_base_url,
                                     token_kind=reach.create_token_kind)
            if not made.ok:
                raise PublishError(made.words, 409 if made.reason != "failed" else 502,
                                   made.reason or "create_failed", "repository")
            steps.append({"key": "repository", "ok": True, "words": made.words})
        state["step"] = "repository"
        _save(path, state)

    # 2. push every branch and tag (never forced) — with the credential that reaches the
    #    repository (the App's installation, the project's binding, a token that can push),
    #    never an anonymous URL when none does: the person reads which were tried and the fix.
    refused = registry_refusal(c, project_id, target)
    if refused and state.get("step") in ("repository", None):
        raise PublishError(refused, 409, "registry_off", "push")
    if reach.reach_words_for is not None and state.get("step") in ("repository", None):
        cannot = reach.reach_words_for(owner, repo)
        if cannot:
            raise PublishError(cannot, 409, "github_not_reachable", "push")
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

        # Safe only because each push and GitHub call happens BEFORE `c` locks that change set's
        # row (the UPDATE below), and the GitHub adapter never touches local_change_sets: a local
        # adapter write from a second connection here would wait on our own lock
        # (local_repo_host.writes_on).
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
        # THE REVIEW CONVERSATION GOES ALONG (BOSSAPP-948): each published change's line
        # comments, replies and resolved threads, on its pull request (local_review). Idempotent:
        # a re-run posts only what is not there yet; a failure is said and the next run resumes.
        from . import local_review

        totals = {"posted": 0, "replies": 0, "resolved": 0, "total": 0}
        failed: list[str] = []
        for row in c.execute(text(
                "SELECT * FROM local_change_sets WHERE project_id = :p AND repository = :r AND "
                "state = 'published' AND github_pr_number IS NOT NULL ORDER BY number"),
                {"p": project_id, "r": binding["full_name"]}).mappings().all():
            try:
                with c.begin_nested():
                    got = local_review.publish_comments(c, settings, dict(row), adapter,
                                                        int(row["github_pr_number"]))
                for k in totals:
                    totals[k] += int(got.get(k) or 0)
            except Exception as error:  # noqa: BLE001 — the code is published; comments resume
                failed.append(f"change #{row['number']}: {type(error).__name__}: "
                              f"{str(error)[:160]}")
        if totals["total"] or failed:
            steps.append({"key": "comments", "ok": not failed, **totals,
                          "words": (f"Carried {totals['posted']} comment thread(s), "
                                    f"{totals['replies']} repl(ies) and {totals['resolved']} "
                                    "resolved thread(s) to the pull requests."
                                    + (" Not yet: " + "; ".join(failed) + ". Publish again to "
                                       "finish them." if failed else ""))})

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
        # The just-published repository is now actively bound: a registry in use hears about it
        # (BOSSAPP-1030), or egress refuses every mint for it (repo_not_allowed).
        from ..multirepo import registry as repo_registry

        repo_registry.register_binding(c, project_id, target, actor=actor,
                                       connection_id=reach.connection_id)
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


__all__ = ["CreateRepoResult", "GitHubReach", "NO_CREDENTIAL_WORDS", "PublishError", "check_existing",
           "check_repository", "create_repository", "default_reach", "publish", "registry_refusal"]
