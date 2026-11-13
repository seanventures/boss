"""BOSS-269 — the MCP-backed repo host: SCM operations through the **user's identity**.

Most enterprises don't hand out raw API keys — engineers authenticate via SSO/OAuth, and access
is brokered by a gateway (e.g. an Atlassian MCP server) that holds the user's session. This
adapter makes that first-class: every ``RepoHost`` operation becomes an **MCP tool call** against
a configured server, so BOSS never touches SCM credentials at all — identity lives at the gateway.

* Server: ``BOSS_REPO_MCP_URL`` (+ optional ``BOSS_REPO_MCP_AUTH_ENV`` — the env-var *name*
  holding the bearer for the gateway itself, same indirection as every BOSS credential).
* Tools: sensible default names (``create_pull_request``, ``merge_pull_request``, …) with
  discovery against ``tools/list`` (a server's ``bitbucket_create_pull_request`` matches by
  suffix) and explicit overrides via ``BOSS_REPO_MCP_TOOLMAP`` (JSON).
* Clones: git-over-HTTPS still needs a credential. If the server exposes a token-minting tool
  (``get_access_token``), we use it (OAuth access tokens work as ``x-token-auth`` for Bitbucket);
  else we fall back to the configured static token. ``BOSS_REPO_MCP_PROVIDER`` picks the URL form.

The three access modes now supported for Bitbucket, uniformly behind ``RepoHost``:
direct API key → ``BitbucketAdapter`` · SSO/OAuth via gateway → ``McpRepoHost`` · any custom
MCP server an enterprise already runs → ``McpRepoHost`` with a toolmap.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from .github_adapter import (
    IntegrationConfigurationError,
    IssueCommentResult,
    PullRequestMergeResult,
    PullRequestResult,
)
from .mcp_client import MCPClient

# operation -> default tool name; servers with prefixed names (bitbucket_create_pull_request)
# match by suffix during discovery.
_DEFAULT_TOOLS = {
    "ensure_branch": "create_branch",
    "upsert_file": "commit_file",
    "create_pull_request": "create_pull_request",
    "merge_pull_request": "merge_pull_request",
    "create_issue_comment": "add_pull_request_comment",
    "get_pull_request_diff": "get_pull_request_diff",
    "get_access_token": "get_access_token",
}


class McpRepoHost:
    kind = "mcp"

    def __init__(self, settings, *, url: Optional[str] = None,
                 client: Optional[MCPClient] = None):
        self.settings = settings
        self.url = (url or getattr(settings, "repo_mcp_url", "") or "").strip()
        if not self.url:
            raise IntegrationConfigurationError(
                "MCP repo host requires BOSS_REPO_MCP_URL (the SCM gateway endpoint).")
        self.provider = (getattr(settings, "repo_mcp_provider", "") or "bitbucket").lower()
        self.client = client or MCPClient(settings)
        overrides = {}
        raw = getattr(settings, "repo_mcp_toolmap", None)
        if raw:
            try:
                overrides = {k: str(v) for k, v in json.loads(raw).items()}
            except (ValueError, TypeError):
                overrides = {}
        self._toolmap = {**_DEFAULT_TOOLS, **overrides}
        self._discovered: Optional[dict[str, str]] = None

    # --- identity -------------------------------------------------------------
    @property
    def repository_full_name(self) -> str:
        if self.provider == "bitbucket":
            ws = getattr(self.settings, "target_bitbucket_workspace", None) or \
                self.settings.target_github_owner
            slug = getattr(self.settings, "target_bitbucket_repo", None) or \
                self.settings.target_github_repo
            return f"{ws}/{slug}"
        return f"{self.settings.target_github_owner}/{self.settings.target_github_repo}"

    def clone_url(self, token: str) -> str:
        from .repo_host import clone_url_for
        owner, repo = self.repository_full_name.split("/", 1)
        tok = token or self._minted_token()
        return clone_url_for(self.provider, owner, repo, tok)

    def _minted_token(self) -> str:
        """An OAuth access token minted by the gateway (works as x-token-auth for Bitbucket
        clones); empty when the server doesn't offer one — callers fall back to config."""
        try:
            out = self._call("get_access_token", {})
            if isinstance(out, dict):
                return str(out.get("token") or out.get("access_token") or "")
            return str(out or "")
        except Exception:
            return ""

    # --- wire -----------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        env = getattr(self.settings, "repo_mcp_auth_env", None) or ""
        token = os.environ.get(env, "") if env else ""
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _tool(self, op: str) -> str:
        name = self._toolmap[op]
        if self._discovered is None:
            try:
                tools = self.client.list_tools(self.url, headers=self._headers())
                names = [str(t.get("name", "")) for t in tools]
                self._discovered = {}
                for key, want in self._toolmap.items():
                    exact = next((n for n in names if n == want), None)
                    suffix = next((n for n in names if n.endswith(f"_{want}") or n.endswith(want)), None)
                    if exact or suffix:
                        self._discovered[key] = exact or suffix
            except Exception:
                self._discovered = {}
        return self._discovered.get(op, name)

    def _call(self, op: str, args: dict[str, Any]) -> Any:
        out = self.client.call_tool(self.url, self._tool(op), args, headers=self._headers())
        if isinstance(out, str):
            try:
                return json.loads(out)
            except (ValueError, TypeError):
                return out
        return out

    # --- RepoHost surface -----------------------------------------------------
    def ensure_branch(self, branch_name: str) -> str:
        out = self._call("ensure_branch", {"repository": self.repository_full_name,
                                           "branch": branch_name})
        return str((out or {}).get("hash", "")) if isinstance(out, dict) else str(out or "")

    def upsert_file(self, branch_name: str, path: str, content: str, message: str) -> str:
        from .agent_acts import sign_commit_message

        message = sign_commit_message(message)
        self._call("upsert_file", {"repository": self.repository_full_name,
                                   "branch": branch_name, "path": path,
                                   "content": content, "message": message})
        return path

    def create_pull_request(self, branch_name: str, title: str, body: str) -> PullRequestResult:
        # BOSSAPP-866: a secret value handed to the run never reaches the PR text.
        from .run_secret_redaction import scrub as _scrub_known

        title, body = _scrub_known(title), _scrub_known(body)
        out = self._call("create_pull_request", {
            "repository": self.repository_full_name, "source_branch": branch_name,
            "title": title, "description": body})
        data = out if isinstance(out, dict) else {}
        return PullRequestResult(url=str(data.get("url") or data.get("html_url") or ""),
                                 number=int(data.get("id") or data.get("number") or 0))

    def merge_pull_request(self, pull_request_number: int,
                           commit_title: str) -> PullRequestMergeResult:
        out = self._call("merge_pull_request", {
            "repository": self.repository_full_name, "id": pull_request_number,
            "message": commit_title})
        data = out if isinstance(out, dict) else {}
        return PullRequestMergeResult(
            sha=str(data.get("sha") or data.get("hash") or ""),
            merged=bool(data.get("merged", True)), message=commit_title)

    def create_issue_comment(self, issue_number: int, body: str) -> IssueCommentResult:
        out = self._call("create_issue_comment", {
            "repository": self.repository_full_name, "id": issue_number, "body": body})
        data = out if isinstance(out, dict) else {}
        return IssueCommentResult(url=str(data.get("url") or ""),
                                  id=int(data.get("id") or 0))

    def get_pull_request_diff(self, pull_number: int) -> str:
        out = self._call("get_pull_request_diff", {
            "repository": self.repository_full_name, "id": pull_number})
        return out if isinstance(out, str) else str((out or {}).get("diff", ""))
