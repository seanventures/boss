"""Bounded, edition-aware API operations for the MCP operator surface.

The mounted API's OpenAPI document is the allowlist.  A caller chooses a stable
operation alias, never a URL, method, header, or credential.  The HTTP API remains
the authority for membership, policy, budgets, approvals, leases, and receipts.
Worker protocols, callbacks, reverse proxies, and secret-material endpoints are
visible in discovery with a reason but cannot be dispatched from MCP.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit

import httpx

from ..services.api_catalog import classify_path
from ..tools.registry import ToolRegistry, ToolSpec

from contextvars import ContextVar

#: THE CALLER'S OWN CREDENTIAL for the API call a tool makes (M8: no confused deputy). The MCP
#: transport sets it per call to the `Authorization` value the CALLER presented (its BOSS API key,
#: or its run token) — over HTTP the request's own bearer, over stdio the key the session was
#: launched with. The bridge sends exactly this to the API, so the route decides as the caller;
#: the key the server was configured with (BOSS_MCP_API_KEY) is used only to read the catalogue.
#: None: no caller identity, and the call is refused — never sent under the server's key.
CALLER_AUTHORIZATION: ContextVar[str | None] = ContextVar("boss_mcp_caller_authorization",
                                                          default=None)

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_RESPONSE_LIMIT = 128_000
_SENSITIVE_FIELD = re.compile(
    r"(secret|password|token|credential|access[_-]?key|api[_-]?key|private)",
    re.IGNORECASE,
)


def _is_secret_material_field(name: str, *, secret_context: bool = False) -> bool:
    normalized = name.lower().replace("-", "_")
    # References and metadata are needed to select a stored secret. The raw
    # value, password, token, or private key must never be returned to MCP.
    if normalized.endswith(("_id", "_ref", "_name", "_type", "_status", "_kind", "_backend")):
        return False
    return (secret_context and normalized in {"value", "key"}) or bool(
        _SENSITIVE_FIELD.search(normalized)
    )


def _secret_route(path: str, path_args: dict) -> bool:
    if any(fragment in path for fragment in ("/secrets", "/secret-refs", "/kms/", "/credential")):
        return True
    if path == "/v1/setup/decision-model/key":
        return True
    setting_key = path_args.get("key") if path == "/v1/settings/{key}" else None
    return isinstance(setting_key, str) and _is_secret_material_field(setting_key)
_EXCLUDED_PREFIXES = (
    ("/v1/worker/", "worker_lease_protocol"),
    ("/v1/webhooks/", "external_webhook_ingress"),
    ("/v1/mcp-confirmations", "human_confirmation_protocol"),
)
_EXCLUDED_EXACT = {
    "/v1/workers/register": "worker_registration_protocol",
    "/v1/channels/{provider}/callback": "oauth_callback",
    "/v1/bootstrap/seed": "operator_bootstrap_boundary",
    "/v1/a2a/poll": "delivery_worker_protocol",
    "/v1/kms/keys/{key_entry_id}/lease": "secret_lease_may_reveal_raw_material",
    "/v1/kms/leases/{lease_id}/resolve": "secret_resolution_returns_raw_material",
    "/v1/github/community/jobs/{job_id}/execute": "github_worker_lease_protocol",
    # The worker hands its leased discovery.run job to the API, where the research agent lives.
    # Every stage spends only under that job's lease token, so the route is a worker protocol,
    # not an operator action. Held as `execute`, it answered the worker 409 and every Community
    # discovery sat queued until its job deadlettered.
    "/v1/discoveries/{run_id}/execute": "discovery_worker_lease_protocol",
    # The worker hands its leased deployment.stack_apply job to the API, which applies the
    # approved ChangeSet. Only the job's live lease may start it: a worker protocol.
    "/v1/deployments/stack/{deployment_id}/execute": "stack_deploy_worker_lease_protocol",
    # The same for an approved host deployment (services/governed_host_deploy.py).
    "/v1/delivery/deployments/{deployment_run_id}/apply": "host_deploy_worker_lease_protocol",
    # The worker hands its leased infra.operation job (platform/operations.py) to the API, which
    # runs one lifecycle hook under that lease: a worker protocol.
    "/v1/infra/platform/operations/execute": "infra_operation_worker_lease_protocol",
    "/v1/github/code-sync-runs/{sync_run_id}/apply": "github_worker_lease_protocol",
    "/v1/github/resolution-plans/{plan_id}/apply": "github_worker_lease_protocol",
    "/v1/github/merge-authorizations/{authorization_id}/apply": "github_worker_lease_protocol",
    "/v1/github/reconciliation-runs/{run_id}/execute": "github_worker_lease_protocol",
    "/v1/docs/update/execute": "docs_worker_lease_protocol",
    "/v1/orchestration/next-move/execute": "orchestration_worker_lease_protocol",
    "/v1/work-orchestration/tracker-sync/execute": "tracker_worker_lease_protocol",
    "/v1/activity/retention/prune": "activity_worker_protocol",
    # The worker hands its leased knowledge.repository.index job to the API, which holds the
    # GitHub credential and the graph (services/knowledge/repo_index.py): a worker protocol.
    "/v1/knowledge/repositories/index/execute": "knowledge_index_worker_lease_protocol",
    # The worker hands its leased change_set.merge job (queued by the set's one approval) to the
    # API, which re-verifies the lease, the job, the decision and the pinned SHAs before merging
    # (multirepo/merge.py execute_job). A person's POST /v1/change-sets/{cs_id}/merge stays held.
    "/v1/change-sets/{cs_id}/merge/execute": "change_set_merge_worker_lease_protocol",
}
_EXCLUDED_FRAGMENTS = (
    ("/steps/claim", "workflow_worker_protocol"),
    ("/compensations/claim", "workflow_worker_protocol"),
)
_WORKER_TAILS = frozenset({"heartbeat", "complete", "fail", "settle", "release"})
#: WHO AN AGENT IS AND WHAT IT MAY REACH. Every route that writes an agent's identity, mission or
#: boundaries (the `roles` row) or hands it an MCP server is held for a person's confirmation
#: when a non-human calls it, exactly as POST /v1/roles always was.
#:
#: The agent resource (routers/agents.py) arrived as new paths, and nothing classified them: an
#: external MCP client could PATCH /v1/agents/{role_key} and strip a bundled agent's must_not
#: boundaries, or grant an agent an MCP server carrying someone's credentials, with no person
#: confirming — while the older POST /v1/roles for the same change needed one. Each is
#: confirmation-eligible, so a reviewed call still goes through.
#: engine/tests/test_every_roster_write_is_held_like_post_roles.py finds every API route whose
#: handler calls a roster writer and fails until it is listed here.
ROSTER_AUTHORITY_WRITES = frozenset({
    ("POST", "/v1/roles"),
    ("POST", "/v1/agents"),
    ("PATCH", "/v1/agents/{role_key}"),
    ("DELETE", "/v1/agents/{role_key}"),
    ("POST", "/v1/role-mcp-servers"),
    ("DELETE", "/v1/role-mcp-servers"),
    # A SKILL GRANT CAN BE AN MCP GRANT (review of milestone/integration): an `mcp_tool` skill
    # calls a server's tool, so giving one was a non-human caller's way round the hold on
    # POST /v1/role-mcp-servers. The route cannot see which skill it is given, so every skill
    # grant from a non-human is held; the run-time gate (agents.mcp_reach_gap) still requires
    # the server itself, so a confirmed skill grant adds no server either.
    ("POST", "/v1/role-skills"),
    ("POST", "/v1/role-skills/sync"),
})
_OPERATOR_ONLY_EXACT = frozenset({
    # A new org or project is tenancy, not work: a non-human caller is held like POST /v1/orgs.
    "/v1/orgs", "/v1/projects", "/v1/members", "/v1/break-glass/grants",
    "/v1/budget/allocations", "/v1/budget/set_intensity",
    "/v1/console/v2/autonomy", "/v1/console/v2/autonomy/apply",
    "/v1/enterprise/identity-providers",
    # Editing a business rule approves the new version in the same act (BOSSAPP-768): a person's
    # review, like .../approve and .../reject beside it, never an MCP tool.
    "/v1/knowledge/view/rules/{rule_id}/edit",
} | {path for _, path in ROSTER_AUTHORITY_WRITES})
_GOVERNED_EFFECT_ALLOWLIST = frozenset({
    ("POST", "/v1/changesets/{changeset_id}/apply"),
    ("POST", "/v1/delivery-workflows/apply"),
    # This DELETE is a project-scoped soft archive: the task and child records
    # remain, and the API requires task_create and emits task.archived.
    ("DELETE", "/v1/tasks/{task_id}"),
    # Only a paused run can roll back, and executed new-version steps block it.
    # The API requires workflow_manage and records the migration event.
    ("POST", "/v1/workflow-runs/{run_id}/migrations/rollback"),
    # Despite its name, this endpoint only renders a standards text block.
    ("POST", "/v1/platform-standards/apply"),
    # A stack rollback only PLANS a new deployment of the previous healthy commit: a ChangeSet
    # held behind its own approval decision. Nothing reaches the host from this call.
    ("POST", "/v1/deployments/stack/{deployment_id}/rollback"),
})
_OPERATOR_ONLY_PATTERNS = (
    r"^/v1/decisions/\{[^/]+\}/(approve|reject|revise|delegate|execute)$",
    r"^/v1/changesets/\{[^/]+\}/approve$",
    r"^/v1/github/merge-authorizations/\{[^/]+\}/authorize$",
    r"^/v1/github/resolution-plans/\{[^/]+\}/approve$",
    r"^/v1/previews/\{[^/]+\}/approve$",
    r"^/v1/break-glass/grants/\{[^/]+\}/revoke$",
    r"^/v1/providers/\{[^/]+\}/revoke$",
    r"^/v1/deployment-hosts/\{[^/]+\}/retire$",
    r"^/v1/deployment-targets/\{[^/]+\}/(purge|teardown)$",
)
_HIGH_RISK_ACTION_TAILS = frozenset({
    "approve", "authorize", "waive", "revoke", "purge", "teardown", "retire",
    "delete", "remove", "merge", "rollback", "revert", "execute", "deploy",
    "promote", "apply", "destroy", "wipe", "reject", "delegate", "revise",
    "redeploy", "auto-deploy", "provision", "bootstrap", "rotate", "adopt-key", "install-key", "grant",
})
_CONFIRMABLE_EXACT = frozenset({
    # Project-scoped authority changes; the API still checks each permission.
    ("POST", "/v1/members"),
    ("POST", "/v1/break-glass/grants"),
    ("POST", "/v1/break-glass/grants/{grant_id}/revoke"),
    ("POST", "/v1/budget/allocations"),
    ("POST", "/v1/budget/set_intensity"),
    ("POST", "/v1/agent-modules/{module_id}/rollback"),
    ("POST", "/v1/agent/threads/{thread_id}/clear"),
    # These lifecycle transitions change local metadata or SecretRefs; they
    # do not reveal raw credentials or call a provider from this endpoint.
    ("POST", "/v1/github/connections/{connection_id}/revoke"),
    ("POST", "/v1/providers/{provider_account_id}/revoke"),
    ("POST", "/v1/providers/{provider_account_id}/rotate"),
    ("POST", "/v1/kms/keys/{key_entry_id}/grant"),
    ("DELETE", "/v1/kms/keys/{key_entry_id}/grants/{grantee_agent}"),
    ("POST", "/v1/kms/leases/{lease_id}/revoke"),
    # Both delivery endpoints plan a ChangeSet; execution remains governed.
    ("POST", "/v1/delivery/deploy"),
    ("POST", "/v1/delivery/deployments/{deployment_run_id}/redeploy"),
    # This queues an already-authorized merge. The worker rechecks current
    # head, reviews, checks, branch policy, and lease before provider write.
    ("POST", "/v1/github/merge-authorizations/{authorization_id}/enqueue"),
    # Promotion creates a proposal from a measured winning trial. It does not
    # adopt the baseline or roll out code; both remain behind their own gates.
    ("POST", "/v1/improvement-programs/trials/{trial_id}/promote"),
    # Local configuration and reversible/archived project records.
    ("DELETE", "/v1/event-subscriptions/{subscription_id}"),
    ("DELETE", "/v1/webhook-endpoints/{endpoint_id}"),
    ("DELETE", "/v1/mcp/servers/{server_id}"),
    ("DELETE", "/v1/role-skills"),
    ("DELETE", "/v1/skill-definitions/{skill_key}"),
    ("DELETE", "/v1/secret-refs/{secret_ref_id}"),
    ("DELETE", "/v1/resource-topology/edges/{edge_id}"),
    ("DELETE", "/v1/delivery-workflows/schedules/{schedule_id}"),
    ("DELETE", "/v1/delivery-workflows/custom-steps/{step_key}"),
    ("DELETE", "/v1/delivery-workflows/runs/{run_id}"),
    ("DELETE", "/v1/delivery-workflows/{definition_id}"),
    ("DELETE", "/v1/deployment-targets/{target_id}"),
    # These only remove project-local planning metadata. The API checks
    # workflow_manage/role_review and records the authenticated actor.
    ("DELETE", "/v1/meeting-roles/{role_key}"),
    ("DELETE", "/v1/console/v2/org/{agent_id}"),
    # Community product routes. Each handler still requires an actor, project
    # scope, and a receipt. Spend, lease, and rollback are enforced there.
    # Metal-kit node and package routes stay out of this set.
    ("DELETE", "/v1/channels/{provider}"),
    ("POST", "/v1/code-tasks/{run_id}/deploy"),
    ("POST", "/v1/delivery/deployments/{deployment_run_id}/execute"),
    ("POST", "/v1/deployment-hosts/{host_id}/adopt-key"),
    ("POST", "/v1/deployment-hosts/{host_id}/install-key"),
    ("POST", "/v1/deployment-hosts/{host_id}/key"),
    ("POST", "/v1/deployment-hosts/{host_id}/provision"),
    ("POST", "/v1/deployment-hosts/{host_id}/retire"),
    ("POST", "/v1/deployment-runs/{deployment_run_id}/rollout/rollback"),
    ("POST", "/v1/deployment-targets/{target_id}/auto-deploy"),
    ("POST", "/v1/deployment-targets/{target_id}/bootstrap"),
    ("POST", "/v1/deployment-targets/{target_id}/purge"),
    ("POST", "/v1/deployment-targets/{target_id}/teardown"),
    ("POST", "/v1/deployments/{deployment_run_id}/promote"),
    ("POST", "/v1/discoveries/{run_id}/promote"),
    ("POST", "/v1/enterprise/identity-providers"),
    ("POST", "/v1/observability/stack/deploy"),
    ("POST", "/v1/orgs"),
    # project:create is step-up for a person: an MCP client asks, a stepped-up person confirms.
    ("POST", "/v1/projects"),
    ("DELETE", "/v1/preview-sandboxes/{preview_id}"),
    ("POST", "/v1/roles"),
    ("DELETE", "/v1/sandboxes/{sandbox_id}"),
    ("POST", "/v1/worker-autoscaling/apply"),
} | ROSTER_AUTHORITY_WRITES)
_HUMAN_ATTRIBUTED_CONFIRMATIONS = {
    # These handlers persist the actor of a human decision. The API may use the
    # separate reviewer as that actor only after consuming an exact confirmation.
    ("POST", "/v1/decisions/{decision_id}/reject"): "decision_resolve",
    ("POST", "/v1/decisions/{decision_id}/revise"): "decision_resolve",
    ("POST", "/v1/decisions/{decision_id}/delegate"): "decision_resolve",
    ("POST", "/v1/decisions/{decision_id}/execute"): "decision_resolve",
    ("POST", "/v1/decisions/{decision_id}/approve"): "decision_resolve",
    ("POST", "/v1/previews/{preview_id}/approve"): "decision_resolve",
    ("POST", "/v1/console/v2/autonomy"): "decision_resolve",
    ("POST", "/v1/console/v2/autonomy/apply"): "decision_resolve",
    ("POST", "/v1/changesets/{changeset_id}/approve"): "providerops_manage",
    ("POST", "/v1/github/merge-authorizations/{authorization_id}/authorize"): "github_manage",
    ("POST", "/v1/github/resolution-plans/{plan_id}/approve"): "github_manage",
}


def operation_alias(method: str, path: str) -> str:
    """Stable alias independent of duplicate FastAPI-generated operation IDs."""
    slug = re.sub(r"[^a-z0-9]+", "_", f"{method}_{path}".lower()).strip("_")
    # Match scripts/generate_openapi_contract.py and the Metatron TUI.
    digest = hashlib.sha256(f"{method.lower()} {path}".encode()).hexdigest()[:8]
    return f"{slug[:68]}_{digest}"


def exclusion_reason(method: str, path: str) -> str | None:
    if not path.startswith("/v1/"):
        return "system_or_identity_protocol"
    if path in _EXCLUDED_EXACT:
        return _EXCLUDED_EXACT[path]
    for prefix, reason in _EXCLUDED_PREFIXES:
        if path.startswith(prefix):
            return reason
    if path.startswith("/v1/model-gateway/leases") and method != "GET":
        return "model_execution_lease_protocol"
    if path.startswith("/v1/workflow-step-runs/") or path.startswith(
        "/v1/workflow-compensation-runs/"
    ):
        return "workflow_worker_protocol"
    if path.startswith("/v1/a2a/deliveries/") or (
        path.startswith("/v1/a2a/subscriptions/") and path.endswith("/poll")
    ):
        return "delivery_worker_protocol"
    if path.startswith("/v1/previews/") and "/web" in path:
        return "reverse_proxy_path"
    if path.startswith("/v1/github/branch-leases/") and path.rsplit("/", 1)[-1] in _WORKER_TAILS:
        return "branch_lease_protocol"
    if path.startswith("/v1/preview-sandboxes/") and path.endswith("/heartbeat"):
        return "preview_worker_protocol"
    for fragment, reason in _EXCLUDED_FRAGMENTS:
        if fragment in path:
            return reason
    return None


def needs_operator_confirmation(method: str, path: str) -> bool:
    if (method, path) in _GOVERNED_EFFECT_ALLOWLIST:
        return False
    if method == "DELETE":
        return True
    if method not in _WRITE_METHODS:
        return False
    if path in _OPERATOR_ONLY_EXACT:
        return True
    if path.startswith("/v1/deployment-hosts/") and path.endswith("/key"):
        return True
    if path.startswith("/v1/activity/retention/"):
        return True
    if path.startswith("/v1/agent/threads/") and path.endswith("/clear"):
        return True
    if "/v1/github/merge-authorizations/" in path and path.endswith("/enqueue"):
        return True
    if path.rsplit("/", 1)[-1].lower() in _HIGH_RISK_ACTION_TAILS:
        return True
    return any(re.match(pattern, path) for pattern in _OPERATOR_ONLY_PATTERNS)


def confirmation_eligible(method: str, path: str) -> bool:
    """Permit reviewed project-scoped transitions with an auditable API effect."""
    return ((method, path) in _CONFIRMABLE_EXACT
            or (method, path) in _HUMAN_ATTRIBUTED_CONFIRMATIONS)


def human_attributed_permission(method: str, path: str) -> str | None:
    """The MCP principal's own grant required in addition to human review."""
    return _HUMAN_ATTRIBUTED_CONFIRMATIONS.get((method, path))


# A non-human without this grant is forbidden. The MCP hold is for a caller
# who would otherwise be allowed to perform the effect.
_GRANT_BEFORE_MCP_HOLD = {
    ("DELETE", "/v1/mcp/servers/{server_id}"): "pack_manage",
    ("POST", "/v1/worker-autoscaling/apply"): "infra_provision",
    ("POST", "/v1/budget/set_intensity"): "budget_manage",
    ("DELETE", "/v1/event-subscriptions/{subscription_id}"): "integration_manage",
}


def grant_before_mcp_hold(method: str, path: str) -> str | None:
    """Project permission to check before returning an MCP hold."""
    return _GRANT_BEFORE_MCP_HOLD.get((method, path))


def _schema_fields(operation: dict, location: str) -> tuple[dict, list[str]]:
    properties: dict[str, dict] = {}
    required: list[str] = []
    for parameter in operation.get("parameters") or []:
        if parameter.get("in") != location or parameter.get("name") in {"project_id", "actor"}:
            continue
        name = parameter.get("name")
        if not isinstance(name, str):
            continue
        properties[name] = parameter.get("schema") or {"type": "string"}
        if parameter.get("required"):
            required.append(name)
    return properties, required


def _object_schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _mounted_body_schema(operation: dict) -> dict:
    return ((operation.get("requestBody") or {}).get("content", {})
            .get("application/json", {}).get("schema") or {})


def _loose_json_body(body: dict) -> bool:
    return (body.get("type") == "object" and not body.get("properties")
            and body.get("additionalProperties") is True)


def _curated_request_bodies() -> dict[str, dict]:
    """Community-safe field maps for mounted handlers with loose dict inputs."""
    source = Path(__file__).with_name("curated_request_bodies.json")
    document = json.loads(source.read_text(encoding="utf-8"))
    if document.get("version") != 1 or not isinstance(document.get("operations"), dict):
        raise ValueError("invalid MCP request-body overlay")
    return document["operations"]


def _rewrite_refs(value: Any, used: set[str]) -> Any:
    if isinstance(value, list):
        return [_rewrite_refs(item, used) for item in value]
    if isinstance(value, dict):
        rewritten = {}
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str) and item.startswith("#/components/schemas/"):
                name = item.rsplit("/", 1)[-1]
                used.add(name)
                rewritten[key] = f"#/$defs/{name}"
            else:
                rewritten[key] = _rewrite_refs(item, used)
        return rewritten
    return value


@dataclass(frozen=True)
class ApiOperation:
    method: str
    path: str
    alias: str
    domain: str
    summary: str
    definition: dict
    excluded_reason: str | None
    operator_confirmation: bool

    @property
    def write(self) -> bool:
        return self.method in _WRITE_METHODS


class ApiSurface:
    """An authenticated, project-pinned view of mounted API operations."""

    def __init__(
        self,
        schema: dict,
        *,
        client: httpx.Client | None = None,
        principal: str | None = None,
        edition: str = "unknown",
        scope_reason: str | None = None,
        pinned_project_id: str | None = None,
        revalidate_scope: bool = False,
    ) -> None:
        self.schema = schema
        self.client = client
        self.principal = principal
        self.edition = edition
        self.scope_reason = scope_reason
        self.pinned_project_id = pinned_project_id
        self.revalidate_scope = revalidate_scope
        #: The Authorization the surface was launched with (stdio: the session's own key). Only
        #: a call made AS that same credential re-runs the launch-time scope check.
        self.launch_authorization: str | None = None
        self.curated_request_bodies = _curated_request_bodies()
        self.operations: dict[str, ApiOperation] = {}
        for path, methods in (schema.get("paths") or {}).items():
            for raw_method, definition in methods.items():
                method = raw_method.upper()
                if method not in _METHODS or not isinstance(definition, dict):
                    continue
                alias = operation_alias(method, path)
                if alias in self.operations:
                    raise ValueError(f"MCP API alias collision: {alias}")
                self.operations[alias] = ApiOperation(
                    method=method,
                    path=path,
                    alias=alias,
                    domain=classify_path(path),
                    summary=str(definition.get("summary") or definition.get("operationId") or path),
                    definition=definition,
                    excluded_reason=exclusion_reason(method, path),
                    operator_confirmation=needs_operator_confirmation(method, path),
                )

    @classmethod
    def connect(
        cls, api_url: str, *, api_key: str | None = None,
        project_id: str = "boss-dogfood",
        client: httpx.Client | None = None,
    ) -> "ApiSurface":
        parsed = urlsplit(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("BOSS_MCP_API_URL must be an HTTP(S) API origin")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("MCP API credentials require HTTPS outside loopback")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("BOSS_MCP_API_URL cannot contain credentials or parameters")
        session = client or httpx.Client(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
        )
        response = session.get("/openapi.json")
        response.raise_for_status()
        try:
            schema = response.json()
        except ValueError as exc:
            raise ValueError("API did not return valid OpenAPI JSON") from exc
        if not isinstance(schema, dict) or not isinstance(schema.get("paths"), dict):
            raise ValueError("API did not return an OpenAPI paths object")
        principal = None
        scope_reason = None
        if api_key:
            principal, scope_reason = _verify_api_scope(session, project_id)
        edition = "unknown"
        try:
            status = session.get("/v1/edition", params={"project_id": project_id})
            if status.is_success:
                edition = str(status.json().get("edition") or "unknown")
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        surface = cls(schema, client=session, principal=principal,
                      edition=edition, scope_reason=scope_reason,
                      pinned_project_id=project_id, revalidate_scope=bool(api_key))
        surface.launch_authorization = f"Bearer {api_key}" if api_key else None
        return surface

    def availability(self, operation: ApiOperation, *, read_only: bool = False) -> str:
        if operation.excluded_reason:
            return "excluded"
        if not self.principal or not self.client:
            return self.scope_reason or "credential_required"
        if read_only and operation.write:
            return "read_only"
        if operation.operator_confirmation:
            if confirmation_eligible(operation.method, operation.path):
                return "confirmation_required"
            return "operator_only_unavailable"
        return "callable"

    def find(
        self, *, query: str = "", domain: str = "", method: str = "",
        limit: int = 30, offset: int = 0, read_only: bool = False,
    ) -> dict:
        needle = query.lower().strip()
        domain = domain.lower().strip()
        method = method.upper().strip()
        matches = [op for op in self.operations.values() if (
            (not needle or needle in f"{op.path} {op.summary} {op.alias}".lower())
            and (not domain or op.domain == domain)
            and (not method or op.method == method)
        )]
        matches.sort(key=lambda op: (op.domain, op.path, op.method))
        return {
            "edition": self.edition,
            "total": len(matches),
            "offset": max(offset, 0),
            "operations": [self.describe_brief(op, read_only=read_only)
                           for op in matches[max(offset, 0):max(offset, 0) + max(1, min(limit, 100))]],
        }

    def describe_brief(self, op: ApiOperation, *, read_only: bool = False) -> dict:
        return {
            "alias": op.alias, "method": op.method, "path": op.path,
            "domain": op.domain, "summary": op.summary,
            "availability": self.availability(op, read_only=read_only),
            "reason": op.excluded_reason or (
                "fresh_human_confirmation_required" if confirmation_eligible(op.method, op.path)
                else "trusted_operator_confirmation_unavailable" if op.operator_confirmation else None
            ),
            "approval": "fresh_human_confirmation" if confirmation_eligible(op.method, op.path)
                        else "operator_only" if op.operator_confirmation else "api_policy",
            "input_schema_quality": self.input_schema_quality(op),
            "mounted_input_schema_quality": self.mounted_input_schema_quality(op),
        }

    @staticmethod
    def _run_auth() -> Any:
        """The verified tool-call identity when it is an agent run (a run token), else None."""
        from ..tools.registry import CURRENT_TOOL_AUTH

        auth = CURRENT_TOOL_AUTH.get()
        return auth if getattr(auth, "run_token", None) is not None else None

    def _run_headers(self, auth: Any, method: str, path: str) -> Optional[dict[str, str]]:
        from ..db import get_engine
        from ..identity import runs

        base = str(getattr(self.client, "base_url", "") or "").rstrip("/")
        try:
            return runs.bridge_headers(auth, method, path, engine=get_engine(), request_origin=base)
        except Exception:  # noqa: BLE001 — a run that cannot be bridged is refused, never widened
            return None

    def _body_schema(self, op: ApiOperation) -> dict:
        mounted = _mounted_body_schema(op.definition)
        if _loose_json_body(mounted):
            return self.curated_request_bodies.get(f"{op.method} {op.path}", mounted)
        return mounted

    def mounted_input_schema_quality(self, op: ApiOperation) -> str:
        return self._schema_quality(_mounted_body_schema(op.definition))

    def input_schema_quality(self, op: ApiOperation) -> str:
        if f"{op.method} {op.path}" in self.curated_request_bodies and _loose_json_body(
            _mounted_body_schema(op.definition)
        ):
            return "curated_json_body"
        return self._schema_quality(self._body_schema(op))

    def _schema_quality(self, body: dict) -> str:
        if not body:
            return "parameters_only"
        if "$ref" in body:
            name = body["$ref"].rsplit("/", 1)[-1]
            body = self.schema.get("components", {}).get("schemas", {}).get(name) or {}
        if body.get("type") == "object" and not body.get("properties"):
            return "loose_json_body"
        return "typed_json_body"

    def describe(self, alias: str, *, read_only: bool = False) -> dict:
        op = self.operations.get(alias)
        if op is None:
            return {"ok": False, "error": "unknown_operation"}
        path_fields, path_required = _schema_fields(op.definition, "path")
        query_fields, query_required = _schema_fields(op.definition, "query")
        body = self._body_schema(op) if op.definition.get("requestBody") else None
        props = {
            "operation": {"type": "string", "const": alias},
            "path": _object_schema(path_fields, path_required),
            "query": _object_schema(query_fields, query_required),
        }
        if confirmation_eligible(op.method, op.path):
            props["confirmation_id"] = {
                "type": "string",
                "description": "Single-use approval issued after separate human review",
            }
        required = ["operation"]
        if path_required:
            required.append("path")
        if query_required:
            required.append("query")
        used: set[str] = set()
        body_ref_name = None
        if body is not None:
            props["body"] = _rewrite_refs(body, used)
            if isinstance(body, dict) and isinstance(body.get("$ref"), str):
                body_ref_name = body["$ref"].rsplit("/", 1)[-1]
            elif isinstance(props["body"], dict):
                _remove_session_authority(props["body"])
            if (op.definition.get("requestBody") or {}).get("required"):
                required.append("body")
        definitions = self.schema.get("components", {}).get("schemas", {})
        rewritten_defs: dict[str, dict] = {}
        pending = list(used)
        while pending:
            name = pending.pop()
            if name in rewritten_defs or name not in definitions:
                continue
            nested: set[str] = set()
            rewritten_defs[name] = _rewrite_refs(definitions[name], nested)
            pending.extend(nested)
        if body_ref_name in rewritten_defs:
            _remove_session_authority(rewritten_defs[body_ref_name])
        input_schema = _object_schema(props, required)
        if rewritten_defs:
            input_schema["$defs"] = rewritten_defs
        return {"ok": True, **self.describe_brief(op, read_only=read_only),
                "inputSchema": input_schema}

    def invoke(
        self, alias: str, *, project_id: str, path: dict | None = None,
        query: dict | None = None, body: Any = None, read_only: bool = False,
        confirmation_id: str | None = None, propose_confirmation: bool = False,
    ) -> dict:
        if self.pinned_project_id is not None and project_id != self.pinned_project_id:
            return {"ok": False, "error": "project_is_session_bound"}
        op = self.operations.get(alias)
        if op is None:
            return {"ok": False, "error": "unknown_operation"}
        availability = self.availability(op, read_only=read_only)
        if availability == "confirmation_required" and not (confirmation_id or propose_confirmation):
            return {"ok": False, "error": "confirmation_required", "operation": alias,
                    "reason": "Use api_request_confirmation, then obtain separate human approval."}
        if availability not in {"callable", "confirmation_required"}:
            return {"ok": False, "error": availability,
                    "reason": self.describe_brief(op, read_only=read_only)["reason"],
                    "operation": alias}
        if propose_confirmation and availability != "confirmation_required":
            return {"ok": False, "error": "confirmation_not_supported", "operation": alias}
        if confirmation_id and (availability != "confirmation_required"
                                or not isinstance(confirmation_id, str)
                                or not re.fullmatch(r"mcpconf_[a-f0-9]{20}", confirmation_id)):
            return {"ok": False, "error": "invalid_confirmation_id", "operation": alias}
        caller = (CALLER_AUTHORIZATION.get() or "").strip()
        run_auth = self._run_auth()
        if run_auth is not None:
            caller = "run-token"          # replaced per request by the run's DPoP headers below
        if not caller:
            return {"ok": False, "error": "caller_identity_required", "operation": alias,
                    "reason": "Present your own BOSS API key or run token; the MCP server's "
                              "key never acts for a caller."}
        if run_auth is None and self.revalidate_scope and caller == (self.launch_authorization or ""):
            assert self.client is not None and self.principal is not None
            try:
                current_principal, scope_reason = _verify_api_scope(self.client, project_id)
            except httpx.HTTPError:
                current_principal, scope_reason = None, "api_principal_binding_unverified"
            if scope_reason or current_principal != self.principal:
                return {"ok": False,
                        "error": scope_reason or "api_principal_changed",
                        "operation": alias}
        if not isinstance(path or {}, dict) or not isinstance(query or {}, dict):
            return {"ok": False, "error": "path_and_query_must_be_objects"}
        assert self.client is not None and self.principal is not None
        path_args, query_args = dict(path or {}), dict(query or {})
        path_schema, path_required = _schema_fields(op.definition, "path")
        query_schema, query_required = _schema_fields(op.definition, "query")
        if set(path_args) - set(path_schema) or set(query_args) - set(query_schema):
            return {"ok": False, "error": "unknown_path_or_query_parameter"}
        for field in path_required:
            if field not in path_args:
                return {"ok": False, "error": "missing_path_parameter", "field": field}
        for field in query_required:
            if field not in query_args:
                return {"ok": False, "error": "missing_query_parameter", "field": field}
        for field, value in (*path_args.items(), *query_args.items()):
            if not _valid_scalar(value, path_schema.get(field) or query_schema.get(field)):
                return {"ok": False, "error": "invalid_parameter", "field": field}
        rendered_path = op.path
        for name in re.findall(r"\{([^}]+)\}", op.path):
            value = (project_id if name == "project_id" else
                     self.principal if name == "actor" else path_args.get(name))
            if (value is None or not isinstance(value, (str, int))
                    or str(value) in {".", ".."} or any(ch in str(value) for ch in "/\\?#")):
                return {"ok": False, "error": "invalid_path_parameter", "field": name}
            rendered_path = rendered_path.replace("{" + name + "}", quote(str(value), safe=""))
        if any(p.get("in") == "query" and p.get("name") == "project_id"
               for p in op.definition.get("parameters") or []):
            query_args["project_id"] = project_id
        if any(p.get("in") == "query" and p.get("name") == "actor"
               for p in op.definition.get("parameters") or []):
            query_args["actor"] = self.principal
        if isinstance(body, dict):
            body = dict(body)
            mismatch = _project_override(body, project_id)
            if mismatch:
                return {"ok": False, "error": "project_is_session_bound", "field": mismatch}
            if "actor" in body and body["actor"] != self.principal:
                return {"ok": False, "error": "actor_is_session_bound"}
            body_schema = self._body_schema(op)
            if _body_has_field(body_schema, self.schema, "project_id"):
                body["project_id"] = project_id
            if _body_has_field(body_schema, self.schema, "actor"):
                body["actor"] = self.principal
        elif body is not None:
            return {"ok": False, "error": "invalid_json_body"}
        if (op.definition.get("requestBody") or {}).get("required") and body is None:
            return {"ok": False, "error": "missing_body"}
        # AS THE CALLER: their credential replaces the client's configured one on this request.
        # An agent run presents a boss:api run token of its own run with a fresh DPoP proof.
        def caller_headers(method: str, path_: str) -> Optional[dict[str, str]]:
            if run_auth is None:
                return {"Authorization": caller}
            return self._run_headers(run_auth, method, path_)

        auth_headers = caller_headers(op.method, unquote(rendered_path))
        if auth_headers is None:
            return {"ok": False, "error": "caller_identity_required", "operation": alias,
                    "reason": "this agent run cannot call the API from here (only a run's first hop "
                              "is bridged)"}
        headers = {"X-BOSS-MCP-Operation": alias, **auth_headers}
        if confirmation_id:
            headers.update({
                "X-BOSS-MCP-Confirmation": confirmation_id,
                "X-BOSS-MCP-Project": project_id,
            })
        try:
            if propose_confirmation:
                response = self.client.post("/v1/mcp-confirmations", json={
                    "project_id": project_id, "operation": alias,
                    "method": op.method, "path": unquote(rendered_path),
                    "query": dict(httpx.QueryParams(query_args).multi_items()),
                    "body": body,
                }, headers=caller_headers("POST", "/v1/mcp-confirmations") or {})
            else:
                response = self.client.request(
                    op.method, rendered_path, params=query_args,
                    json=body if body is not None else None,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            return {"ok": False, "error": "api_transport_error", "detail": type(exc).__name__}
        if len(response.content) > _RESPONSE_LIMIT:
            return {"ok": False, "error": "api_response_too_large", "status": response.status_code}
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        secret_context = _secret_route(op.path, path_args)
        markers = _secret_markers({"path": path_args, "query": query_args, "body": body},
                                  secret_context=secret_context)
        payload = _redact_payload(payload, markers, secret_context=secret_context)
        return {"ok": response.is_success, "status": response.status_code,
                "operation": alias, "result": payload}


def _verify_api_scope(session: httpx.Client, project_id: str) -> tuple[str | None, str | None]:
    """Check live key identity and exclusive project membership before dispatch."""
    auth = session.get("/v1/auth/session")
    try:
        state = auth.json() if auth.is_success else {}
    except ValueError:
        state = {}
    if not isinstance(state, dict) or not (
        state.get("auth_required") is True
        and state.get("transport_authenticated") is True
        and state.get("actor_binding_required") is True
        and state.get("auth_method") == "api_key"
        and isinstance(state.get("principal_member_id"), str)
        and state["principal_member_id"].strip()
    ):
        return None, "api_principal_binding_unverified"
    candidate = state["principal_member_id"].strip()
    # Not the console's key, the worker's, or the host tools': a dedicated MCP key
    # (`make mcp-setup`: a service account pinned to the project).
    if candidate in {"ceo", "system", "executive_console", "sys_legacy_console",
                     "sys_legacy_system", "sva_host_tools"}:
        return None, "dedicated_mcp_principal_required"
    pinned = state.get("key_projects")
    if isinstance(pinned, list) and pinned != [project_id]:
        return None, "exclusive_project_membership_required"
    projects = session.get("/v1/projects")
    try:
        listing = projects.json() if projects.is_success else {}
        items = listing.get("items") if isinstance(listing, dict) else None
        accessible = {item["id"] for item in items
                      if isinstance(item, dict) and isinstance(item.get("id"), str)}
    except (ValueError, TypeError):
        accessible = set()
    if project_id not in accessible:
        return None, "project_membership_unverified"
    if accessible != {project_id}:
        # ID-only API routes cannot be pinned by injecting project_id, so the
        # credential must remain exclusive even after a long-lived MCP session
        # has started.
        return None, "exclusive_project_membership_required"
    return candidate, None


def _valid_scalar(value: Any, schema: dict | None) -> bool:
    if isinstance(value, (dict, list)) or value is None:
        return False
    kind = (schema or {}).get("type")
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    return isinstance(value, (str, int, float, bool))


def _project_override(value: Any, project_id: str, prefix: str = "body") -> str | None:
    if isinstance(value, dict):
        for name, item in value.items():
            field = f"{prefix}.{name}"
            if name == "project_id" and item != project_id:
                return field
            mismatch = _project_override(item, project_id, field)
            if mismatch:
                return mismatch
    elif isinstance(value, list):
        for index, item in enumerate(value):
            mismatch = _project_override(item, project_id, f"{prefix}[{index}]")
            if mismatch:
                return mismatch
    return None


def _body_has_field(body: dict, schema: dict, field: str) -> bool:
    if "$ref" in body:
        name = body["$ref"].rsplit("/", 1)[-1]
        body = schema.get("components", {}).get("schemas", {}).get(name) or {}
    return field in (body.get("properties") or {})


def _remove_session_authority(body_schema: dict) -> None:
    """The API principal and session project are supplied by MCP, not arguments."""
    properties = body_schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("project_id", None)
        properties.pop("actor", None)
    required = body_schema.get("required")
    if isinstance(required, list):
        body_schema["required"] = [x for x in required if x not in {"project_id", "actor"}]


def _secret_markers(value: Any, sensitive: bool = False, *, secret_context: bool = False) -> set[str]:
    """Collect submitted sensitive values so even API error echoes are redacted."""
    if isinstance(value, dict):
        found: set[str] = set()
        for key, item in value.items():
            found.update(_secret_markers(
                item, sensitive or _is_secret_material_field(str(key), secret_context=secret_context),
                secret_context=secret_context,
            ))
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found.update(_secret_markers(item, sensitive, secret_context=secret_context))
        return found
    if sensitive and isinstance(value, str) and value:
        return {value}
    return set()


def _redact_payload(value: Any, markers: set[str], *, secret_context: bool = False) -> Any:
    if isinstance(value, dict):
        return {
            _redact_payload(str(key), markers, secret_context=secret_context): (
                "[redacted]" if _is_secret_material_field(str(key), secret_context=secret_context)
                else _redact_payload(item, markers, secret_context=secret_context)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload(item, markers, secret_context=secret_context) for item in value]
    if isinstance(value, str):
        for marker in sorted(markers, key=len, reverse=True):
            value = value.replace(marker, "[redacted]")
    return value


def register_api_tools(
    registry: ToolRegistry, surface: ApiSurface, *, read_only: bool = False,
    domains: set[str] | None = None,
) -> None:
    """Register compact discovery plus optional domain-scoped, typed tools."""
    def guarded(handler):
        # The generic registry reports exception text. API request data may
        # contain credentials, so this bridge never passes exception text on.
        def invoke_guarded(ctx, *, project_id, actor, args):
            try:
                return handler(ctx, project_id=project_id, actor=actor, args=args)
            except Exception:
                return {"ok": False, "error": "api_bridge_error"}
        return invoke_guarded

    registry.register(ToolSpec(
        "api_find_operations", "Search mounted BOSS API operations by domain, name, or path. Excluded operations remain visible with reasons.",
        _object_schema({
            "query": {"type": "string"}, "domain": {"type": "string"},
            "method": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "offset": {"type": "integer", "minimum": 0},
        }),
        guarded(lambda ctx, *, project_id, actor, args: surface.find(read_only=read_only, **{
            key: args[key] for key in ("query", "domain", "method", "limit", "offset") if key in args
        })), kind="read", permission="system:read",
    ))
    registry.register(ToolSpec(
        "api_describe_operation", "Show the exact input schema and policy status for one BOSS API operation alias.",
        _object_schema({"operation": {"type": "string"}}, ["operation"]),
        guarded(lambda ctx, *, project_id, actor, args: surface.describe(
            str(args.get("operation") or ""), read_only=read_only)),
        kind="read", permission="system:read",
    ))
    call_schema = _object_schema({
        "operation": {"type": "string", "description": "Stable alias from api_find_operations"},
        "path": {"type": "object"}, "query": {"type": "object"},
        "body": {"description": "JSON request body; see api_describe_operation"},
        "confirmation_id": {"type": "string", "description": "Single-use approval from a separate human reviewer"},
    }, ["operation"])
    def call_read(ctx, *, project_id, actor, args):
        op = surface.operations.get(args.get("operation"))
        if op is None or op.method != "GET":
            return {"ok": False, "error": "read_operation_required"}
        return surface.invoke(op.alias, project_id=project_id,
                              path=args.get("path"), query=args.get("query"),
                              body=args.get("body"), read_only=read_only,
                              confirmation_id=args.get("confirmation_id"))
    def request_confirmation(ctx, *, project_id, actor, args):
        op = surface.operations.get(args.get("operation"))
        if op is None or not confirmation_eligible(op.method, op.path):
            return {"ok": False, "error": "confirmation_not_supported"}
        return surface.invoke(op.alias, project_id=project_id,
                              path=args.get("path"), query=args.get("query"),
                              body=args.get("body"), read_only=read_only,
                              propose_confirmation=True)
    def call_write(ctx, *, project_id, actor, args):
        op = surface.operations.get(args.get("operation"))
        if op is None or not op.write:
            return {"ok": False, "error": "write_operation_required"}
        return surface.invoke(op.alias, project_id=project_id,
                              path=args.get("path"), query=args.get("query"),
                              body=args.get("body"), read_only=read_only,
                              confirmation_id=args.get("confirmation_id"))
    registry.register(ToolSpec(
        "api_read_operation", "Read one schema-allowlisted BOSS API operation using the session project and explicitly bound API principal.",
        call_schema, guarded(call_read), kind="read", inherits_route_permission=True,
    ))
    registry.register(ToolSpec(
        "api_write_operation", "Write through one schema-allowlisted BOSS API operation; API authorization, policy, budgets, approvals, and receipts still apply.",
        call_schema, guarded(call_write), kind="write", reversibility="costs_money",
        requires_durable_workspace=False, inherits_route_permission=True,
    ))
    if not read_only:
        registry.register(ToolSpec(
            "api_request_confirmation",
            "Propose one exact sensitive API request for a separate human CEO to review; this does not execute the action.",
            call_schema, guarded(request_confirmation), kind="write",
            reversibility="safe", requires_durable_workspace=False,
            inherits_route_permission=True,
        ))
    for op in surface.operations.values():
        if not domains or (op.domain not in domains and "all" not in domains):
            continue
        if surface.availability(op, read_only=read_only) not in {"callable", "confirmation_required"}:
            continue
        schema = surface.describe(op.alias, read_only=read_only)["inputSchema"]
        schema["properties"].pop("operation", None)
        schema["required"] = [field for field in schema["required"] if field != "operation"]
        if confirmation_eligible(op.method, op.path):
            schema["properties"]["confirmation_id"] = {"type": "string"}

        def handler(ctx, *, project_id, actor, args, alias=op.alias):
            return surface.invoke(
                alias, project_id=project_id,
                path=args.get("path"), query=args.get("query"), body=args.get("body"),
                read_only=read_only, confirmation_id=args.get("confirmation_id"),
            )

        registry.register(ToolSpec(
            name=op.alias,
            description=f"{op.method} {op.path}: {op.summary}. "
                        + ("Fresh human confirmation required. " if confirmation_eligible(op.method, op.path) else "")
                        + "API policy applies.",
            parameters=schema,
            handler=guarded(handler),
            kind="write" if op.write else "read",
            reversibility="costs_money" if op.write else "safe",
            requires_durable_workspace=False, inherits_route_permission=True,
        ))
