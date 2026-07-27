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
