"""WHICH CREDENTIAL REACHES owner/repo, AND FOR WHAT (BOSSAPP-1338, AR-41).

FXQuote (a local project) was published to an existing empty repository, paramg/bank-fxquote.
The Publish sheet said "There is no paramg/bank-fxquote on GitHub that BOSS can see" and kept
Publish disabled — while the BOSS GitHub App's installation could reach the repository
(github_repos: source=app, reachable=true). The check, and the push after it, used the
repository-CREATING credential (repo_creation.repo_creation_credential): a personal token
borrowed from another project (the BOSS dogfood bootstrap), which cannot see that repository.
And "there is no" was the wrong sentence: the truth was "this token cannot see it".

THE ONE RESOLVER for an existing repository: every credential BOSS holds that might reach
`owner/repo`, in the order they are tried, each asked of GitHub (GET /repos/owner/repo — and,
for a push, whether it may push), the first that reaches it used:

  1. the BOSS GitHub App's installation on `owner` (a token for this repository only);
  2. the project's own binding of `owner/repo` (its connection's credential);
  3. personal-access-token connections — this project's first, then its organisation's;
  4. the server's own token (BOSS_GITHUB_TOKEN).

`bound_only=True` is the build's rule (github_connections.github_token_for): the environment
token, else the project's own binding — never another repository's token, and no GitHub call.
Publishing names its target explicitly (a person's decision), so it may use any credential that
REACHES the repository; a new repository is created with the creating credential and then pushed
to with whatever reaches it.

When nothing reaches it the words name every credential tried, what GitHub said about each, and
the fix — never "does not exist".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    source: str                        # app | binding | connection | install | env
    label: str                         # "boss-localhost App (installation on paramg)", …
    project_id: Optional[str] = None
    project_name: str = ""
    mint: Optional[Callable[[], Optional[str]]] = field(default=None, repr=False)
    token_kind: str = "user"           # "app" | "user"


@dataclass
class RepoAccess:
    owner: str
    repo: str
    ok: bool
    token: Optional[str] = field(default=None, repr=False)
    token_kind: str = "user"
    source: str = ""
    label: str = ""
    project_id: Optional[str] = None
    exists: Optional[bool] = None      # True: a credential saw it; None: nobody could tell
    can_push: Optional[bool] = None
    reason: str = ""                   # "" | no_github_credential | not_visible | read_only | failed
    words: str = ""
    tried: list[dict[str, Any]] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def as_dict(self) -> dict[str, Any]:
        """Everything but the token."""
        return {"full_name": self.full_name, "ok": self.ok, "source": self.source,
                "label": self.label, "project_id": self.project_id, "exists": self.exists,
                "can_push": self.can_push, "reason": self.reason, "words": self.words,
                "tried": list(self.tried)}


def _project_name(db: Any, project_id: Optional[str]) -> str:
    if not project_id:
        return ""
    try:
        from sqlalchemy import text

        with db.begin_nested():
            return str(db.execute(text("SELECT name FROM projects WHERE id = :p"),
                                  {"p": project_id}).scalar() or project_id)
    except Exception:  # noqa: BLE001
        return str(project_id)


def _app_candidates(db: Any, owner: str, repo: str) -> list[Candidate]:
    from ..identity import github_setup

    try:
        cfg = github_setup.app_config(db)
    except Exception:  # noqa: BLE001 — no identity tables on this schema
        return []
    if cfg.get("source") != "setup" or not cfg.get("app_id"):
        return []
    name = f"{cfg.get('slug') or 'BOSS'} App"

    def mint() -> Optional[str]:
        try:
            with db.begin_nested():
                return github_setup.control_plane_token(db, owner, repo)
        except Exception:  # noqa: BLE001 — refused: the next candidate may do
            log.debug("repo access: the App's installation token was not minted", exc_info=True)
            return None

    return [Candidate("app", f"the {name} (its installation on {owner})", mint=mint,
                      token_kind="app")]


def _binding_candidates(db: Any, settings: Any, project_id: str, owner: str,
                        repo: str, *, check_bound: bool = True) -> list[Candidate]:
    from .github_connections import GitHubConnectionService

    service = GitHubConnectionService(settings)

    def mint() -> Optional[str]:
        try:
            return service.token_for_repository(db, project_id=project_id, owner=owner, repo=repo)
        except Exception:  # noqa: BLE001
            return None

    if not check_bound:     # token_for_repository holds the exact-binding rule itself
        return [Candidate("binding", f"this project's connection for {owner}/{repo}",
                          project_id=project_id, mint=mint)]
    try:
        from sqlalchemy import text

        with db.begin_nested():
            bound = db.execute(text(
                "SELECT 1 FROM github_repository_bindings WHERE project_id = :p AND status = 'active' "
                "AND lower(full_name) = lower(:f) LIMIT 1"),
                {"p": project_id, "f": f"{owner}/{repo}"}).first()
    except Exception:  # noqa: BLE001 — an older schema: try it anyway
        bound = True
    if not bound:
        return []
    return [Candidate("binding", f"this project's connection for {owner}/{repo}",
                      project_id=project_id, project_name=_project_name(db, project_id), mint=mint)]


def _connection_candidates(db: Any, settings: Any, project_id: str,
                           actor: Optional[str] = None) -> list[Candidate]:
    from .github_connections import GitHubConnectionService
    from .repo_creation import _connection_rows, token_label

    service = GitHubConnectionService(settings)
    out: list[Candidate] = []
    for row in _connection_rows(db, project_id, actor):
        def mint(row=row) -> Optional[str]:
            try:
                return service.token_for_connection(db, project_id=str(row["project_id"]),
                                                    connection_id=str(row["id"]))
            except Exception:  # noqa: BLE001
                return None

        # Named only as far as the acting person may know whose it is (#179 review).
        visible = bool(row.get("visible"))
        out.append(Candidate("connection", token_label(row),
                             project_id=str(row.get("project_id")) if visible else None,
                             project_name=str(row.get("project_name") or "") if visible else "",
                             mint=mint))
    return out


def _install_candidates(settings: Any) -> list[Candidate]:
    token = str(getattr(settings, "github_token", "") or "").strip()
    if not token:
        return []
    return [Candidate("install", "the server's token (BOSS_GITHUB_TOKEN)", mint=lambda: token)]


def candidates(db: Any, settings: Any, project_id: str, owner: str, repo: str, *,
               bound_only: bool = False, actor: Optional[str] = None) -> list[Candidate]:
    """Every credential that might reach owner/repo, in the order they are tried."""
    if bound_only:
        env = str(getattr(settings, "github_token", "") or "").strip()
        out = [Candidate("env", "the server's token (BOSS_GITHUB_TOKEN)", mint=lambda: env)] if env else []
        return out + _binding_candidates(db, settings, project_id, owner, repo, check_bound=False)
    out: list[Candidate] = []
    for source in (lambda: _app_candidates(db, owner, repo),
                   lambda: _binding_candidates(db, settings, project_id, owner, repo),
                   lambda: _connection_candidates(db, settings, project_id, actor),
                   lambda: _install_candidates(settings)):
        try:
            out.extend(source())
        except Exception:  # noqa: BLE001 — one source failing never hides the others
            log.debug("repo access: a credential source failed", exc_info=True)
    return out


_PROBES: dict[tuple[str, str, str], tuple[float, tuple[str, Optional[bool]]]] = {}
#: How long one (credential, repository) answer is reused (#179 review): the check, the publish
#: and its words ask the same question within seconds.
PROBE_TTL_S = 300.0


def reset_probe_cache() -> None:
    _PROBES.clear()


def _probe(token: str, owner: str, repo: str, api_base: str,
           client: Optional[httpx.Client]) -> tuple[str, Optional[bool]]:
    """("reaches" | "read_only" | "not_visible" | "rejected" | "failed:<n>", can_push), cached
    for PROBE_TTL_S per (credential digest, repository, API) — a failure to answer is not."""
    import hashlib
    import time

    key = (hashlib.sha256(token.encode()).hexdigest()[:24], f"{owner}/{repo}".lower(), api_base)
    hit = _PROBES.get(key)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    got = _probe_once(token, owner, repo, api_base, client)
    if not got[0].startswith("failed"):
        _PROBES[key] = (time.monotonic() + PROBE_TTL_S, got)
    return got


def _probe_once(token: str, owner: str, repo: str, api_base: str,
                client: Optional[httpx.Client]) -> tuple[str, Optional[bool]]:
    own = client or httpx.Client(timeout=15)
    try:
        got = own.get(f"{api_base.rstrip('/')}/repos/{owner}/{repo}",
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "boss-publish"})
    except httpx.HTTPError as error:
        return f"failed:{type(error).__name__}", None
    finally:
        if client is None:
            own.close()
    if got.status_code == 200:
        perms = (got.json() or {}).get("permissions") if got.content else None
        if isinstance(perms, dict) and perms:
            push = bool(perms.get("push") or perms.get("admin") or perms.get("maintain"))
            return ("reaches" if push else "read_only"), push
        return "reaches", None          # an installation token: the mint was for writing
    if got.status_code == 401:
        return "rejected", None
    if got.status_code in (403, 404):
        return "not_visible", None
    return f"failed:{got.status_code}", None


_OUTCOME_WORDS = {
    "reaches": "reaches it",
    "read_only": "can read it but not push to it",
    "not_visible": "cannot see it",
    "rejected": "was rejected by GitHub (expired or revoked)",
    "no_token": "gave no token (not installed there, or its secret could not be read)",
}


def resolve(db: Any, settings: Any, project_id: str, owner: str, repo: str, *,
            need: str = "push", bound_only: bool = False, verify: bool = True,
            actor: Optional[str] = None,
            client: Optional[httpx.Client] = None,
            api_base_url: Optional[str] = None) -> RepoAccess:
    """THE credential that reaches owner/repo for `need` ("read" | "push"), and why — or every
    credential tried and what GitHub said about each. `verify=False` (the build's bound lookup):
    the first credential that yields a token, asking GitHub nothing."""
    owner, repo = str(owner or "").strip(), str(repo or "").strip()
    from ..identity import github_urls

    api = github_urls.api_base(api_base_url or getattr(settings, "github_api_base_url", ""))
    out = RepoAccess(owner, repo, False)
    found = candidates(db, settings, project_id, owner, repo, bound_only=bound_only, actor=actor)
    read_only: Optional[tuple[Candidate, str]] = None
    for cand in found:
        token = cand.mint() if cand.mint else None
        tried = {"source": cand.source, "label": cand.label, "project_id": cand.project_id}
        if not token:
            out.tried.append({**tried, "outcome": "no_token", "words": _OUTCOME_WORDS["no_token"]})
            continue
        if not verify:
            out.tried.append({**tried, "outcome": "used", "words": "used without asking GitHub"})
            return RepoAccess(owner, repo, True, token=token, token_kind=cand.token_kind,
                              source=cand.source, label=cand.label, project_id=cand.project_id,
                              words=f"Uses {cand.label}.", tried=out.tried)
        outcome, push = _probe(token, owner, repo, api, client)
        out.tried.append({**tried, "outcome": outcome,
                          "words": _OUTCOME_WORDS.get(outcome, f"GitHub did not answer ({outcome[7:]})")})
        if outcome == "reaches" or (outcome == "read_only" and need == "read"):
            return RepoAccess(owner, repo, True, token=token, token_kind=cand.token_kind,
                              source=cand.source, label=cand.label, project_id=cand.project_id,
                              exists=True, can_push=push if push is not None else (need == "push"),
                              words=f"BOSS reaches {owner}/{repo} with {cand.label}.",
                              tried=out.tried)
        if outcome == "read_only" and read_only is None:
            read_only = (cand, token)
    said = "; ".join(f"{t['label']} {t['words']}" for t in out.tried)
    if read_only is not None:
        cand, _ = read_only
        out.exists, out.reason = True, "read_only"
        out.words = (f"BOSS can see {owner}/{repo} but no credential it holds may push to it: {said}. "
                     f"Give {cand.label} write access to {owner}/{repo} (Contents: Read and write), "
                     "or add a token that can push to it, in Setup › GitHub access.")
        return out
    if not found:
        out.reason = "no_github_credential"
        out.words = (f"BOSS holds no GitHub credential that could reach {owner}/{repo}. Install the "
                     f"BOSS GitHub App on {owner} with access to {repo}, or add a personal access "
                     f"token for {owner} that can reach it, in Setup › GitHub access.")
        return out
    if any(t["outcome"].startswith("failed") for t in out.tried) and \
            not any(t["outcome"] in ("not_visible", "rejected") for t in out.tried):
        out.reason = "failed"
        out.words = f"GitHub did not answer about {owner}/{repo} ({said}); try again."
        return out
    out.reason = "not_visible"
    out.words = (f"None of the credentials BOSS holds can see {owner}/{repo} (it may exist; GitHub "
                 f"shows a repository only to a credential with access to it): {said}. Fix: give "
                 f"the BOSS GitHub App's installation on {owner} access to {repo} (or to all "
                 f"repositories), or add a personal access token for {owner} that can reach "
                 f"{repo}, in Setup › GitHub access.")
    return out


__all__ = ["Candidate", "RepoAccess", "candidates", "resolve"]
