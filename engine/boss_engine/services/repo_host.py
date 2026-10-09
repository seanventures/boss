"""BOSS-267 — the pluggable repo-host seam: SCM providers as add-ins, not built-ins.

Customers target a repo host (GitHub today, Bitbucket for many enterprises) for the apps BOSS
builds. Until now the host was hardwired at five layers (adapter, clone URLs, config, auth,
webhooks). This module introduces the seam in the house idiom — a ``Protocol`` + a kind-dispatch
factory, exactly like ``TelemetrySource``/``build_source`` and ``make_sandbox_provider`` — so a new
host is **an adapter, never a pipeline change**:

* ``RepoHost`` — the protocol, extracted from the subset of ``GitHubAdapter``'s public surface the
  pipeline actually depends on (branch → files → PR → merge → comment → diff → clone URL).
* ``make_repo_host(settings, host=...)`` — dispatch on the per-task ``spec.repo_host`` override,
  else ``settings.target_repo_host``; **default "github", zero behavior change** for existing
  deployments.
* ``clone_url_for(...)`` — one place that knows each host's authenticated clone-URL form, for the
  call sites (sandbox provision, guest specs, preview, deploy) that need a URL without an adapter.

Adding a host = one adapter module conforming to ``RepoHost`` + one line in ``_HOSTS``.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from .github_adapter import (
    GitHubAdapter,
    IssueCommentResult,
    PullRequestMergeResult,
    PullRequestResult,
)

KNOWN_REPO_HOSTS = ("github", "bitbucket", "mcp")


@runtime_checkable
class RepoHost(Protocol):
    """The one surface every SCM provider implements for customer target repos."""

    kind: str

    @property
    def repository_full_name(self) -> str: ...

    def clone_url(self, token: str) -> str: ...
    def ensure_branch(self, branch_name: str) -> str: ...
    def upsert_file(self, branch_name: str, path: str, content: str, message: str) -> str: ...
    def create_pull_request(self, branch_name: str, title: str, body: str) -> PullRequestResult: ...
    def merge_pull_request(self, pull_request_number: int,
                           commit_title: str) -> PullRequestMergeResult: ...
    def create_issue_comment(self, issue_number: int, body: str) -> IssueCommentResult: ...
    def get_pull_request_diff(self, pull_number: int) -> str: ...


def clone_url_for(host: str, owner: str, repo: str, token: str = "") -> str:
    """The authenticated HTTPS clone URL for a host — the ONE place that knows each host's
    credential form (previously duplicated, GitHub-only, across 4+ call sites)."""
    kind = (host or "github").strip().lower()
    if kind == "github":
        if token:
            return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        return f"https://github.com/{owner}/{repo}.git"
    if kind == "bitbucket":
        # Bitbucket Cloud: workspace/repo_slug; access tokens use the x-token-auth pseudo-user.
        if token:
            return f"https://x-token-auth:{token}@bitbucket.org/{owner}/{repo}.git"
        return f"https://bitbucket.org/{owner}/{repo}.git"
    raise ValueError(f"unsupported repo host {host!r} (known: {', '.join(KNOWN_REPO_HOSTS)})")


# ---- git access that never writes a credential where code can read it ---------------------------------
#
# IDENTITY.md §7.9-§7.10: an agent (or the app an agent wrote, running in a preview or a deploy
# workdir) never holds a raw GitHub or provider token. A URL with the token in it lands in
# `.git/config` of the checkout and in the process table; both are reachable from the code that
# then runs there. `git_access` returns a credential-FREE URL plus an environment that carries the
# credential to git's own process only (GIT_CONFIG_COUNT/KEY/VALUE: an http extraheader), so
# nothing in the workdir or in argv ever names it. With the egress proxy on and a delegated
# principal in context, `identity.runs.repo_access` routes the clone through boss-egress instead
# and no GitHub token exists on this side at all.

_BASIC_USER = {"github": "x-access-token", "bitbucket": "x-token-auth"}
_HOST_ORIGIN = {"github": "https://github.com/", "bitbucket": "https://bitbucket.org/"}


class GitAccess:
    """How to run git against one repo: `url` carries no credential; `env` (to MERGE into the git
    process's environment) carries it. `repr` shows neither the credential nor the env.

    TWO KINDS OF GIT, two environments. Through egress the credential is answered by the run's
    shim socket, which only the run's SANDBOX uid can open: `environ()` is for git that runs as
    that uid (an agent's own clone). BOSS's own git (`owned_process.boss_run`, or any spawn with
    `sandboxed=False`) runs as the control plane's uid, which the socket never answers: it must
    use `boss_environ()`, where the credential rides in its own process environment (the "own
    git" helper) and never reaches the sandbox. Outside egress both are the same."""

    __slots__ = ("url", "env", "via", "own_env")

    def __init__(self, url: str, env: Optional[dict] = None, via: str = "anonymous",
                 own_env: Optional[dict] = None):
        self.url = url
        self.env = dict(env or {})
        self.via = via                     # "egress" | "token" | "anonymous"
        self.own_env = dict(own_env) if own_env is not None else None

    def __repr__(self) -> str:
        return f"GitAccess(url={self.url!r}, via={self.via!r})"

    def environ(self, base: Optional[dict] = None) -> dict:
        import os

        out = dict(os.environ if base is None else base)
        out.update(self.env)
        out.setdefault("GIT_TERMINAL_PROMPT", "0")
        return out

    def boss_environ(self, base: Optional[dict] = None) -> dict:
        """The environment for BOSS's OWN git (run as the control plane's uid)."""
        import os

        out = dict(os.environ if base is None else base)
        out.update(self.env if self.own_env is None else self.own_env)
        out.setdefault("GIT_TERMINAL_PROMPT", "0")
        return out


def git_config_env(pairs) -> dict:
    """`git -c k=v` for every git process, through the environment (git >= 2.31), so the values
    appear neither in argv nor in any `.git/config`."""
    pairs = list(pairs)
    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def git_access(host: str, owner: str, repo: str, token: str = "") -> GitAccess:
    """The legacy (token) path, without leaking the token into the checkout or argv."""
    import base64

    kind = (host or "github").strip().lower()
    url = clone_url_for(kind, owner, repo, "")
    if not token:
        return GitAccess(url, via="anonymous")
    basic = base64.b64encode(f"{_BASIC_USER.get(kind, 'x-access-token')}:{token}".encode()).decode()
    origin = _HOST_ORIGIN.get(kind, "https://github.com/")
    return GitAccess(url, git_config_env([(f"http.{origin}.extraheader", f"AUTHORIZATION: basic {basic}")]),
                     via="token")


def resolve_repo_host_kind(settings, spec: Optional[dict] = None) -> str:
    """Per-task ``spec.repo_host`` wins; else the project-level setting; default github."""
    kind = str(((spec or {}).get("repo_host")) or
               getattr(settings, "target_repo_host", "") or "github").strip().lower()
    if kind not in KNOWN_REPO_HOSTS:
        raise ValueError(f"unsupported repo host {kind!r} (known: {', '.join(KNOWN_REPO_HOSTS)})")
    return kind


def make_repo_host(settings, *, host: Optional[str] = None,
                   owner: Optional[str] = None, repo: Optional[str] = None) -> RepoHost:
    """Factory for the configured repo host. ``host`` overrides settings (per-task routing).
    ``owner``/``repo`` bind the Bitbucket adapter to a specific workspace/slug; the GitHub adapter
    stays settings-bound exactly as today (its multi-repo routing happens at the clone/token layer)."""
    kind = (host or getattr(settings, "target_repo_host", "") or "github").strip().lower()
    if kind == "github":
        return GitHubAdapter(settings)
    if kind == "bitbucket":
        from .bitbucket_adapter import BitbucketAdapter
        return BitbucketAdapter(settings, workspace=owner, repo_slug=repo)
    if kind == "mcp":
        # SSO/OAuth identity path (BOSS-269): operations via the user's MCP gateway.
        from .mcp_repo_host import McpRepoHost
        return McpRepoHost(settings)
    raise ValueError(f"unsupported repo host {kind!r} (known: {', '.join(KNOWN_REPO_HOSTS)})")
