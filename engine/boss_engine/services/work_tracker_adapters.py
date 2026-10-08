"""Work-tracker provider adapters (Plane default + popular alternatives).

BOSS treats the work tracker as a plugin seam: portfolio ranking stays in BOSS;
issue create/update mirrors to the configured system of record. Network writes
belong in leased, receipted jobs — adapters only speak HTTP.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from urllib.parse import quote

logger = logging.getLogger(__name__)

_SSL = ssl.create_default_context()


class WorkTrackerHttpError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class IssueCreateResult:
    external_issue_id: str
    external_key: Optional[str] = None
    external_url: Optional[str] = None
    external_state: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectionField:
    key: str
    label: str
    required: bool = True
    secret: bool = False
    placeholder: str = ""
    help: str = ""


@dataclass(frozen=True)
class TrackerCatalogEntry:
    provider_key: str
    display_name: str
    description: str
    docs_url: str
    capabilities: tuple[str, ...]
    connection_fields: tuple[ConnectionField, ...]
    is_default_option: bool = False


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: Optional[dict[str, Any]] = None,
    timeout: float = 20.0,
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as response:
            raw = response.read().decode()
            if not raw.strip():
                return {"__ok__": response.status}
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:400]
        raise WorkTrackerHttpError(
            f"{method} {url} failed HTTP {error.code}: {detail}",
            status=error.code,
        ) from error
    except Exception as error:  # noqa: BLE001
        raise WorkTrackerHttpError(f"{method} {url} failed: {error}") from error


#: Each tracker's own token variable(s): the ONLY environment a tracker with no token of its own
#: falls back to. (It used to try every provider's, so a Plane test sent BOSS_GITHUB_TOKEN to
#: whatever base URL the configuration named.)
PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "plane": ("PLANE_API_KEY",),
    "jira": ("JIRA_API_TOKEN",),
    "linear": ("LINEAR_API_KEY",),
    "github_issues": ("GITHUB_TOKEN", "BOSS_GITHUB_TOKEN"),
    "azure_devops": ("AZURE_DEVOPS_PAT",),
    "asana": ("ASANA_ACCESS_TOKEN",),
}


def resolve_credential(
    configuration: dict[str, Any],
    *,
    env: Optional[dict[str, str]] = None,
    provider: Optional[str] = None,
) -> Optional[str]:
    """Resolve API token from config: explicit token, env:NAME, or the provider's own env name.

    id-sec-final2: an `env:`/`secret:` name a configuration carries is read only when a secret
    reference may name it (env_refs.env_name_allowed: never DATABASE_URL, the master key or a
    BOSS key — a project Admin chooses this configuration AND the URL it is sent to)."""
    from .env_refs import env_name_allowed

    envmap = env if env is not None else os.environ
    # One-shot / tests
    for key in ("api_token", "access_token", "token", "pat"):
        value = configuration.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    ref = str(configuration.get("credential_ref") or "").strip()
    for prefix in ("env:", "secret:"):
        if ref.startswith(prefix):
            name = ref[len(prefix):]
            if not env_name_allowed(name):
                return None
            return (envmap.get(name) or "").strip() or None
    for key in PROVIDER_ENV.get(str(provider or configuration.get("provider_key") or ""), ()):
        if envmap.get(key):
            return envmap[key].strip()
    return None


class WorkTrackerAdapter(Protocol):
    provider_key: str
    display_name: str
    capabilities: tuple[str, ...]

    def catalog_entry(self) -> TrackerCatalogEntry: ...

    def issue_url(
        self,
        configuration: dict[str, Any],
        *,
        external_project_id: Optional[str],
        external_issue_id: Optional[str],
        external_key: Optional[str] = None,
    ) -> Optional[str]: ...

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]: ...

    def create_issue(
        self,
        configuration: dict[str, Any],
        *,
        title: str,
        description_html: str,
        description_text: str,
    ) -> Optional[IssueCreateResult]: ...


# ---------------------------------------------------------------------------
# Plane (default)
# ---------------------------------------------------------------------------


class PlaneWorkTrackerAdapter:
    provider_key = "plane"
    display_name = "Plane"
    capabilities = (
        "issue.create",
        "issue.read",
        "issue.update",
        "issue.comment",
        "issue.link",
        "webhook.issue",
    )

    def catalog_entry(self) -> TrackerCatalogEntry:
        return TrackerCatalogEntry(
            provider_key=self.provider_key,
            display_name=self.display_name,
            description="Plane — an open-source system of record for issues and PRDs.",
            docs_url="https://docs.plane.so/",
            capabilities=self.capabilities,
            connection_fields=(
                ConnectionField("api_base_url", "API / web base URL", placeholder="https://plane.example.com"),
                ConnectionField("workspace_slug", "Workspace slug", placeholder="your-workspace"),
                ConnectionField("project_id", "Project ID (UUID)", placeholder="cc97d5cc-…"),
                ConnectionField("api_token", "API key", secret=True, help="X-API-Key value"),
            ),
        )

    def issue_url(
        self,
        configuration: dict[str, Any],
        *,
        external_project_id: Optional[str],
        external_issue_id: Optional[str],
        external_key: Optional[str] = None,
    ) -> Optional[str]:
        base = str(
            configuration.get("web_base_url")
            or configuration.get("api_base_url")
            or ""
        ).rstrip("/")
        workspace = str(configuration.get("workspace_slug") or "").strip("/")
        project_id = external_project_id or configuration.get("project_id")
        if not (base and workspace and project_id and external_issue_id):
            return None
        return f"{base}/{workspace}/projects/{project_id}/issues/{external_issue_id}"

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]:
        base = str(configuration.get("api_base_url") or configuration.get("web_base_url") or "").rstrip("/")
        workspace = str(configuration.get("workspace_slug") or "").strip("/")
        project_id = str(configuration.get("project_id") or "").strip()
        token = resolve_credential(configuration, provider=self.provider_key)
        if not (base and workspace and project_id and token):
            return {
                "ok": False,
                "message": "Plane needs api_base_url, workspace_slug, project_id, and API key.",
            }
        url = f"{base}/api/v1/workspaces/{workspace}/projects/{project_id}/"
        try:
            data = _http_json("GET", url, headers={"X-API-Key": token})
        except WorkTrackerHttpError as error:
            return {"ok": False, "message": str(error), "status": error.status}
        name = data.get("name") if isinstance(data, dict) else None
        return {
            "ok": True,
            "message": f"Connected to Plane project {name or project_id}",
            "project_name": name,
        }

    def create_issue(
        self,
        configuration: dict[str, Any],
        *,
        title: str,
        description_html: str,
        description_text: str,
    ) -> Optional[IssueCreateResult]:
        base = str(configuration.get("api_base_url") or configuration.get("web_base_url") or "").rstrip("/")
        workspace = str(configuration.get("workspace_slug") or "").strip("/")
        project_id = str(configuration.get("project_id") or "").strip()
        token = resolve_credential(configuration, provider=self.provider_key)
        if not (base and workspace and project_id and token):
            return None
        url = f"{base}/api/v1/workspaces/{workspace}/projects/{project_id}/issues/"
        try:
            issue = _http_json(
                "POST",
                url,
                headers={"X-API-Key": token},
                body={"name": title, "description_html": description_html},
            )
        except WorkTrackerHttpError as error:
            logger.warning("plane create_issue failed: %s", error)
            return None
        if not isinstance(issue, dict) or not issue.get("id"):
            return None
        ext_id = str(issue["id"])
        seq = issue.get("sequence_id")
        key = str(issue.get("identifier") or "") or (
            f"#{seq}" if seq is not None else None
        )
        return IssueCreateResult(
            external_issue_id=ext_id,
            external_key=key,
            external_url=self.issue_url(
                configuration,
                external_project_id=project_id,
                external_issue_id=ext_id,
                external_key=key,
            ),
            external_state=str(issue.get("state") or "Backlog"),
            raw=issue,
        )


# ---------------------------------------------------------------------------
# Jira Cloud
# ---------------------------------------------------------------------------


class JiraWorkTrackerAdapter:
    provider_key = "jira"
    display_name = "Jira"
    capabilities = (
        "issue.create",
        "issue.read",
        "issue.update",
        "issue.comment",
        "issue.link",
    )

    def catalog_entry(self) -> TrackerCatalogEntry:
        return TrackerCatalogEntry(
            provider_key=self.provider_key,
            display_name=self.display_name,
            description="Atlassian Jira Cloud — issues, bugs, and epics.",
            docs_url="https://developer.atlassian.com/cloud/jira/platform/rest/v3/",
            capabilities=self.capabilities,
            connection_fields=(
                ConnectionField(
                    "base_url",
                    "Site URL",
                    placeholder="https://your-domain.atlassian.net",
                ),
                ConnectionField("email", "Atlassian account email"),
                ConnectionField("project_key", "Project key", placeholder="BOSS"),
                ConnectionField(
                    "api_token",
                    "API token",
                    secret=True,
                    help="Create at id.atlassian.com → Security → API tokens",
                ),
                ConnectionField(
                    "issue_type",
                    "Default issue type",
                    required=False,
                    placeholder="Task",
                ),
            ),
        )

    def _auth_header(self, configuration: dict[str, Any]) -> str:
        email = str(configuration.get("email") or "").strip()
        token = resolve_credential(configuration, provider=self.provider_key) or ""
        raw = f"{email}:{token}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def issue_url(
        self,
        configuration: dict[str, Any],
        *,
        external_project_id: Optional[str],
        external_issue_id: Optional[str],
        external_key: Optional[str] = None,
    ) -> Optional[str]:
        base = str(configuration.get("base_url") or "").rstrip("/")
        key = external_key or external_issue_id
        if not (base and key):
            return None
        return f"{base}/browse/{key}"

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]:
        base = str(configuration.get("base_url") or "").rstrip("/")
        token = resolve_credential(configuration, provider=self.provider_key)
        email = str(configuration.get("email") or "").strip()
        if not (base and token and email):
            return {
                "ok": False,
                "message": "Jira needs base_url, email, and API token.",
            }
        try:
            me = _http_json(
                "GET",
                f"{base}/rest/api/3/myself",
                headers={"Authorization": self._auth_header(configuration)},
            )
        except WorkTrackerHttpError as error:
            return {"ok": False, "message": str(error), "status": error.status}
        display = me.get("displayName") if isinstance(me, dict) else None
        return {
            "ok": True,
            "message": f"Connected to Jira as {display or email}",
            "account": display,
        }

    def create_issue(
        self,
        configuration: dict[str, Any],
        *,
        title: str,
        description_html: str,
        description_text: str,
    ) -> Optional[IssueCreateResult]:
        base = str(configuration.get("base_url") or "").rstrip("/")
        project_key = str(
            configuration.get("project_key") or configuration.get("project_id") or ""
        ).strip()
        issue_type = str(configuration.get("issue_type") or "Task").strip() or "Task"
        if not (base and project_key and resolve_credential(configuration, provider=self.provider_key)):
            return None
        # ADF minimal paragraph for description
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": description_text or title,
                        }
                    ],
                }
            ],
        }
        try:
            issue = _http_json(
                "POST",
                f"{base}/rest/api/3/issue",
                headers={"Authorization": self._auth_header(configuration)},
                body={
                    "fields": {
                        "project": {"key": project_key},
                        "summary": title,
                        "issuetype": {"name": issue_type},
                        "description": adf,
                    }
                },
            )
        except WorkTrackerHttpError as error:
            logger.warning("jira create_issue failed: %s", error)
            return None
        if not isinstance(issue, dict) or not issue.get("id"):
            return None
        key = str(issue.get("key") or "")
        ext_id = str(issue["id"])
        return IssueCreateResult(
            external_issue_id=ext_id,
            external_key=key or None,
            external_url=self.issue_url(
                configuration,
                external_project_id=project_key,
                external_issue_id=ext_id,
                external_key=key,
            ),
            external_state="To Do",
            raw=issue,
        )


# ---------------------------------------------------------------------------
# Linear
# ---------------------------------------------------------------------------


class LinearWorkTrackerAdapter:
    provider_key = "linear"
    display_name = "Linear"
    capabilities = (
        "issue.create",
        "issue.read",
        "issue.update",
        "issue.comment",
        "issue.link",
    )
    _GQL = "https://api.linear.app/graphql"

    def catalog_entry(self) -> TrackerCatalogEntry:
        return TrackerCatalogEntry(
            provider_key=self.provider_key,
            display_name=self.display_name,
            description="Linear — modern issue tracking for product teams.",
            docs_url="https://developers.linear.app/docs/graphql/working-with-the-graphql-api",
            capabilities=self.capabilities,
            connection_fields=(
                ConnectionField(
                    "api_token",
                    "API key",
                    secret=True,
                    help="Linear Settings → API → Personal API keys",
                ),
                ConnectionField(
                    "team_id",
                    "Team ID",
                    placeholder="uuid",
                    help="GraphQL team id (or team key via team_key)",
                ),
                ConnectionField(
                    "team_key",
                    "Team key (optional if team_id set)",
                    required=False,
                    placeholder="ENG",
                ),
            ),
        )

    def issue_url(
        self,
        configuration: dict[str, Any],
        *,
        external_project_id: Optional[str],
        external_issue_id: Optional[str],
        external_key: Optional[str] = None,
    ) -> Optional[str]:
        template = str(configuration.get("issue_url_template") or "")
        if template and external_issue_id:
            return template.format(
                project_id=external_project_id or "",
                issue_id=external_key or external_issue_id,
            )
        if external_key:
            return f"https://linear.app/issue/{external_key}"
        return None

    def _gql(
        self, configuration: dict[str, Any], query: str, variables: Optional[dict] = None
    ) -> dict[str, Any]:
        token = resolve_credential(configuration, provider=self.provider_key)
        if not token:
            raise WorkTrackerHttpError("Linear API key missing")
        data = _http_json(
            "POST",
            self._GQL,
            headers={"Authorization": token},
            body={"query": query, "variables": variables or {}},
        )
        if not isinstance(data, dict):
            raise WorkTrackerHttpError("Linear returned non-object JSON")
        if data.get("errors"):
            raise WorkTrackerHttpError(str(data["errors"])[:400])
        return data.get("data") or {}

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]:
        try:
            data = self._gql(configuration, "{ viewer { id name } }")
        except WorkTrackerHttpError as error:
            return {"ok": False, "message": str(error), "status": error.status}
        viewer = data.get("viewer") or {}
        return {
            "ok": True,
            "message": f"Connected to Linear as {viewer.get('name') or viewer.get('id')}",
            "account": viewer.get("name"),
        }

    def create_issue(
        self,
        configuration: dict[str, Any],
        *,
        title: str,
        description_html: str,
        description_text: str,
    ) -> Optional[IssueCreateResult]:
        team_id = str(configuration.get("team_id") or "").strip()
        team_key = str(configuration.get("team_key") or "").strip()
        try:
            if not team_id and team_key:
                data = self._gql(
                    configuration,
                    "query($key: String!) { teams(filter: { key: { eq: $key } }) { nodes { id key } } }",
                    {"key": team_key},
                )
                nodes = ((data.get("teams") or {}).get("nodes")) or []
                if nodes:
                    team_id = str(nodes[0]["id"])
            if not team_id:
                return None
            data = self._gql(
                configuration,
                """
                mutation($input: IssueCreateInput!) {
                  issueCreate(input: $input) {
                    success
                    issue { id identifier url state { name } }
                  }
                }
                """,
                {
                    "input": {
                        "teamId": team_id,
                        "title": title,
                        "description": description_text or title,
                    }
                },
            )
        except WorkTrackerHttpError as error:
            logger.warning("linear create_issue failed: %s", error)
            return None
        issue = ((data.get("issueCreate") or {}).get("issue")) or {}
        if not issue.get("id"):
            return None
        return IssueCreateResult(
            external_issue_id=str(issue["id"]),
            external_key=str(issue.get("identifier") or "") or None,
            external_url=str(issue.get("url") or "") or None,
            external_state=str((issue.get("state") or {}).get("name") or "Backlog"),
            raw=issue,
        )


# ---------------------------------------------------------------------------
# GitHub Issues
# ---------------------------------------------------------------------------


class GitHubIssuesWorkTrackerAdapter:
    provider_key = "github_issues"
    display_name = "GitHub Issues"
    capabilities = (
        "issue.create",
        "issue.read",
        "issue.update",
        "issue.comment",
        "issue.link",
    )

    def catalog_entry(self) -> TrackerCatalogEntry:
        return TrackerCatalogEntry(
            provider_key=self.provider_key,
            display_name=self.display_name,
            description="GitHub Issues — track work next to the code.",
            docs_url="https://docs.github.com/en/rest/issues",
            capabilities=self.capabilities,
            # The default (skills-mcp spec E10): it reuses the repo's GitHub connection.
            is_default_option=True,
            connection_fields=(
                ConnectionField(
                    "owner",
                    "Repository owner",
                    placeholder="acme",
                ),
                ConnectionField("repo", "Repository name", placeholder="product"),
                ConnectionField(
                    "api_token",
                    "Personal access token",
                    secret=True,
                    help="repo scope (classic) or Issues permissions (fine-grained)",
                ),
                ConnectionField(
                    "api_base_url",
                    "API base (GitHub Enterprise)",
                    required=False,
                    placeholder="https://api.github.com",
                ),
            ),
        )

    def issue_url(
        self,
        configuration: dict[str, Any],
        *,
        external_project_id: Optional[str],
        external_issue_id: Optional[str],
        external_key: Optional[str] = None,
    ) -> Optional[str]:
        owner = str(configuration.get("owner") or "").strip()
        repo = str(configuration.get("repo") or "").strip()
        num = external_key or external_issue_id
        if not (owner and repo and num):
            return None
        return f"https://github.com/{owner}/{repo}/issues/{num}"

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]:
        token = resolve_credential(configuration, provider=self.provider_key)
        owner = str(configuration.get("owner") or "").strip()
        repo = str(configuration.get("repo") or "").strip()
        base = str(configuration.get("api_base_url") or "https://api.github.com").rstrip(
            "/"
        )
        if not (token and owner and repo):
            return {
                "ok": False,
                "message": "GitHub Issues needs owner, repo, and a token.",
            }
        try:
            data = _http_json(
                "GET",
                f"{base}/repos/{quote(owner)}/{quote(repo)}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except WorkTrackerHttpError as error:
            return {"ok": False, "message": str(error), "status": error.status}
        full = data.get("full_name") if isinstance(data, dict) else None
        # WHO the issues are filed as: the token's own login (an App token has none — then
        # nothing is claimed rather than the repo shown as if it were an account).
        login = None
        try:
            me = _http_json("GET", f"{base}/user", headers={
                "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"})
            login = me.get("login") if isinstance(me, dict) else None
        except WorkTrackerHttpError:
            login = None
        return {
            "ok": True,
            "message": f"Connected to GitHub repo {full or f'{owner}/{repo}'}",
            "repository": full,
            "account": f"@{login}" if login else None,
        }

    def create_issue(
        self,
        configuration: dict[str, Any],
        *,
        title: str,
        description_html: str,
        description_text: str,
    ) -> Optional[IssueCreateResult]:
        token = resolve_credential(configuration, provider=self.provider_key)
        owner = str(configuration.get("owner") or "").strip()
        repo = str(configuration.get("repo") or "").strip()
        base = str(configuration.get("api_base_url") or "https://api.github.com").rstrip(
            "/"
        )
        if not (token and owner and repo):
            return None
        try:
            issue = _http_json(
                "POST",
                f"{base}/repos/{quote(owner)}/{quote(repo)}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                body={"title": title, "body": description_text or title},
            )
        except WorkTrackerHttpError as error:
            logger.warning("github_issues create_issue failed: %s", error)
            return None
        if not isinstance(issue, dict) or not issue.get("number"):
            return None
        number = str(issue["number"])
        return IssueCreateResult(
            external_issue_id=str(issue.get("id") or number),
            external_key=number,
            external_url=str(issue.get("html_url") or "")
            or self.issue_url(
                configuration,
                external_project_id=f"{owner}/{repo}",
                external_issue_id=number,
                external_key=number,
            ),
            external_state=str(issue.get("state") or "open"),
            raw=issue,
        )


# ---------------------------------------------------------------------------
# Azure DevOps Work Items
# ---------------------------------------------------------------------------


class AzureDevOpsWorkTrackerAdapter:
    provider_key = "azure_devops"
    display_name = "Azure DevOps"
    capabilities = (
        "issue.create",
        "issue.read",
        "issue.update",
        "issue.comment",
        "issue.link",
    )

    def catalog_entry(self) -> TrackerCatalogEntry:
        return TrackerCatalogEntry(
            provider_key=self.provider_key,
            display_name=self.display_name,
            description="Azure DevOps Boards — work items for enterprise teams.",
            docs_url="https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/",
            capabilities=self.capabilities,
            connection_fields=(
                ConnectionField(
                    "organization",
                    "Organization",
                    placeholder="my-org",
                ),
                ConnectionField("project", "Project name", placeholder="Product"),
                ConnectionField(
                    "api_token",
                    "Personal access token",
                    secret=True,
                    help="PAT with Work Items read/write",
                ),
                ConnectionField(
                    "work_item_type",
                    "Work item type",
                    required=False,
                    placeholder="Task",
                ),
            ),
        )

    def _auth(self, configuration: dict[str, Any]) -> str:
        token = resolve_credential(configuration, provider=self.provider_key) or ""
        raw = f":{token}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def issue_url(
        self,
        configuration: dict[str, Any],
        *,
        external_project_id: Optional[str],
        external_issue_id: Optional[str],
        external_key: Optional[str] = None,
    ) -> Optional[str]:
        org = str(configuration.get("organization") or "").strip()
        project = str(
            configuration.get("project") or external_project_id or ""
        ).strip()
        wid = external_key or external_issue_id
        if not (org and project and wid):
            return None
        return f"https://dev.azure.com/{org}/{quote(project)}/_workitems/edit/{wid}"

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]:
        org = str(configuration.get("organization") or "").strip()
        project = str(configuration.get("project") or "").strip()
        token = resolve_credential(configuration, provider=self.provider_key)
        if not (org and project and token):
            return {
                "ok": False,
                "message": "Azure DevOps needs organization, project, and PAT.",
            }
        url = (
            f"https://dev.azure.com/{quote(org)}/{quote(project)}"
            f"/_apis/wit/workitemtypes?api-version=7.1"
        )
        try:
            data = _http_json(
                "GET", url, headers={"Authorization": self._auth(configuration)}
            )
        except WorkTrackerHttpError as error:
            return {"ok": False, "message": str(error), "status": error.status}
        count = len(data.get("value") or []) if isinstance(data, dict) else 0
        return {
            "ok": True,
            "message": f"Connected to Azure DevOps project {project} ({count} work item types)",
            "project": project,
        }

    def create_issue(
        self,
        configuration: dict[str, Any],
        *,
        title: str,
        description_html: str,
        description_text: str,
    ) -> Optional[IssueCreateResult]:
        org = str(configuration.get("organization") or "").strip()
        project = str(configuration.get("project") or "").strip()
        wit = str(configuration.get("work_item_type") or "Task").strip() or "Task"
        if not (org and project and resolve_credential(configuration, provider=self.provider_key)):
            return None
        url = (
            f"https://dev.azure.com/{quote(org)}/{quote(project)}"
            f"/_apis/wit/workitems/${quote(wit)}?api-version=7.1"
        )
        patch = [
            {
                "op": "add",
                "path": "/fields/System.Title",
                "value": title,
            },
            {
                "op": "add",
                "path": "/fields/System.Description",
                "value": description_html or description_text or title,
            },
        ]
        data = json.dumps(patch).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": self._auth(configuration),
                "Content-Type": "application/json-patch+json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20, context=_SSL) as response:
                issue = json.loads(response.read().decode())
        except Exception as error:  # noqa: BLE001
            logger.warning("azure_devops create_issue failed: %s", error)
            return None
        if not isinstance(issue, dict) or not issue.get("id"):
            return None
        wid = str(issue["id"])
        fields = issue.get("fields") or {}
        return IssueCreateResult(
            external_issue_id=wid,
            external_key=wid,
            external_url=self.issue_url(
                configuration,
                external_project_id=project,
                external_issue_id=wid,
                external_key=wid,
            ),
            external_state=str(fields.get("System.State") or "New"),
            raw=issue,
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TRACKER_ADAPTERS: dict[str, WorkTrackerAdapter] = {
    "plane": PlaneWorkTrackerAdapter(),
    "jira": JiraWorkTrackerAdapter(),
    "linear": LinearWorkTrackerAdapter(),
    "github_issues": GitHubIssuesWorkTrackerAdapter(),
    "azure_devops": AzureDevOpsWorkTrackerAdapter(),
}


def list_catalog() -> list[dict[str, Any]]:
    entries = []
    for key in ("github_issues", "linear", "jira", "plane", "azure_devops"):
        adapter = TRACKER_ADAPTERS[key]
        entry = adapter.catalog_entry()
        entries.append(
            {
                "provider_key": entry.provider_key,
                "display_name": entry.display_name,
                "description": entry.description,
                "docs_url": entry.docs_url,
                "capabilities": list(entry.capabilities),
                "is_default_option": entry.is_default_option,
                "connection_fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "required": f.required,
                        "secret": f.secret,
                        "placeholder": f.placeholder,
                        "help": f.help,
                    }
                    for f in entry.connection_fields
                ],
            }
        )
    return entries


def get_adapter(provider_key: str) -> WorkTrackerAdapter:
    key = (provider_key or "").strip().lower()
    if key not in TRACKER_ADAPTERS:
        raise ValueError(
            f"Unknown work tracker '{provider_key}'. "
            f"Supported: {', '.join(TRACKER_ADAPTERS)}"
        )
    return TRACKER_ADAPTERS[key]


def strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# Updating an issue: a comment, and (where the tracker has one) its open/closed state
# ---------------------------------------------------------------------------
#
# `work_tracker.issue.update` had no executor, so "update the ticket" was a skill an agent could
# hold and never run (skills-mcp spec RC4b, E12). Each tracker's own API, one call each. State is
# changed only where it is a plain open/closed switch (GitHub Issues) or a named state the tracker
# looks up itself (Linear); Jira, Plane and Azure DevOps move issues through workflows of the
# project's own design, so a state there is refused with that reason rather than guessed at.


def update_issue(
    provider_key: str,
    configuration: dict[str, Any],
    *,
    issue: str,
    comment: str = "",
    state: str = "",
) -> dict[str, Any]:
    """Comment on and/or change the state of one issue. Raises WorkTrackerHttpError."""
    key = (provider_key or "").strip().lower()
    issue = str(issue or "").strip()
    if not issue:
        raise WorkTrackerHttpError("which issue: give its number, key or id")
    token = resolve_credential(configuration, provider=key)
    if not token:
        raise WorkTrackerHttpError(f"{key}: no credential")
    done: dict[str, Any] = {"issue": issue, "commented": False, "state": None}
    if key == "github_issues":
        owner = str(configuration.get("owner") or "").strip()
        repo = str(configuration.get("repo") or "").strip()
        base = str(configuration.get("api_base_url") or "https://api.github.com").rstrip("/")
        headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
        number = issue.lstrip("#")
        root = f"{base}/repos/{quote(owner)}/{quote(repo)}/issues/{quote(number)}"
        if comment:
            _http_json("POST", f"{root}/comments", headers=headers, body={"body": comment})
            done["commented"] = True
        if state:
            wanted = "closed" if state.lower() in ("closed", "close", "done") else "open"
            _http_json("PATCH", root, headers=headers, body={"state": wanted})
            done["state"] = wanted
        return done
    if key == "linear":
        adapter = LinearWorkTrackerAdapter()
        if comment:
            adapter._gql(configuration,
                         "mutation($i: CommentCreateInput!) { commentCreate(input: $i) "
                         "{ success } }", {"i": {"issueId": issue, "body": comment}})
            done["commented"] = True
        if state:
            data = adapter._gql(
                configuration,
                "query($id: String!) { issue(id: $id) { team { states { nodes { id name } } } } }",
                {"id": issue})
            nodes = (((data.get("issue") or {}).get("team") or {}).get("states") or {}
                     ).get("nodes") or []
            match = next((n for n in nodes if str(n.get("name")).lower() == state.lower()), None)
            if match is None:
                raise WorkTrackerHttpError(
                    f"Linear has no state {state!r} for this issue's team. States: "
                    + ", ".join(str(n.get("name")) for n in nodes))
            adapter._gql(configuration,
                         "mutation($id: String!, $s: String!) { issueUpdate(id: $id, "
                         "input: {stateId: $s}) { success } }", {"id": issue, "s": match["id"]})
            done["state"] = match["name"]
        return done
    if state:
        raise WorkTrackerHttpError(
            f"{key} moves issues through the project's own workflow; BOSS comments on it and "
            "leaves the state to that workflow")
    if not comment:
        return done
    if key == "jira":
        base = str(configuration.get("base_url") or "").rstrip("/")
        _http_json("POST", f"{base}/rest/api/3/issue/{quote(issue)}/comment",
                   headers={"Authorization": JiraWorkTrackerAdapter()._auth_header(configuration)},
                   body={"body": {"type": "doc", "version": 1, "content": [
                       {"type": "paragraph", "content": [{"type": "text", "text": comment}]}]}})
    elif key == "plane":
        base = str(configuration.get("api_base_url") or configuration.get("web_base_url")
                   or "").rstrip("/")
        workspace = str(configuration.get("workspace_slug") or "").strip("/")
        project = str(configuration.get("project_id") or "").strip()
        _http_json("POST", f"{base}/api/v1/workspaces/{workspace}/projects/{project}/issues/"
                           f"{quote(issue)}/comments/",
                   headers={"X-API-Key": token}, body={"comment_html": f"<p>{comment}</p>"})
    elif key == "azure_devops":
        org = str(configuration.get("organization") or "").strip()
        project = str(configuration.get("project") or "").strip()
        _http_json("POST", f"https://dev.azure.com/{quote(org)}/{quote(project)}/_apis/wit/"
                           f"workItems/{quote(issue)}/comments?api-version=7.1-preview.4",
                   headers={"Authorization": AzureDevOpsWorkTrackerAdapter()._auth(configuration)},
                   body={"text": comment})
    else:
        raise WorkTrackerHttpError(f"Unknown work tracker '{provider_key}'")
    done["commented"] = True
    return done
