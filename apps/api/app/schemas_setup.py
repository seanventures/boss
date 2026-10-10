"""Repo and models: the frozen API contract (mirrors apps/web/lib/setup-types.ts).

`SetupView` is what every Repo and models endpoint returns. The request bodies are typed so the
MCP surface and OpenAPI describe them; every credential field is write-only.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Tone = Literal["green", "amber", "red", "grey", "busy"]


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class Pill(_Open):
    state: str
    tone: Tone
    label: str
    tip: str
    action: Optional[Literal["connect", "test", "replace", "fix_link", "budget",
                             "default_model"]] = None
    fix_url: Optional[str] = None


class CheckResult(_Open):
    ok: Optional[bool] = None
    state: str
    at: Optional[str] = None


class RepositoryChecks(_Open):
    read: CheckResult
    write: CheckResult
    pull_requests: CheckResult


class RepositoryConnection(_Open):
    id: str
    auth: Literal["token", "app"]
    account_login: Optional[str] = None
    expires_at: Optional[str] = None
    source: Literal["connected", "install"]
    #: Whether builds can use this token (they read the org's vault token): {ok, message, next}.
    builds: Optional[dict[str, Any]] = None


class RepositoryEntry(_Open):
    binding_id: Optional[str] = None
    full_name: str
    default: bool
    private: Optional[bool] = None
    default_branch: Optional[str] = None
    checks: RepositoryChecks
    pill: Pill
    checked_at: Optional[str] = None


class RepositorySection(_Open):
    connection: Optional[RepositoryConnection] = None
    repos: list[RepositoryEntry]


class ModelEntry(_Open):
    source_id: str
    provider: str
    label: str
    kind: Literal["subscription", "metered", "local"]
    model: Optional[str] = None
    used_for: list[str]
    pill: Pill
    checked_at: Optional[str] = None
    source: Literal["connected", "install"]
    retry_at: Optional[str] = None


class Detected(_Open):
    provider: str
    label: str
    kind: Literal["subscription", "local"]
    found: Literal["signed_in", "needs_token", "needs_file", "not_on_server", "found"]
    base_url: Optional[str] = None


class CatalogueEntry(_Open):
    provider: str
    label: str
    kind: Literal["subscription", "api_key", "local"]
    key_url: Optional[str] = None
    example_model: Optional[str] = None
    fields: list[Literal["api_key", "token", "file", "base_url", "model"]]
    command: Optional[str] = None


class JobChoice(_Open):
    engine: str
    model: Optional[str] = None


class JobResolved(_Open):
    engine: str
    model: Optional[str] = None
    label: str
    why: str


class JobOption(_Open):
    engine: str
    model: Optional[str] = None
    label: str
    metered: bool


class Job(_Open):
    job: Literal["build", "review", "chat", "decisions", "hard", "routine"]
    label: str
    more: bool
    choice: Optional[JobChoice] = None
    resolved: Optional[JobResolved] = None
    metered: bool
    options: list[JobOption]


class Budget(_Open):
    period: Literal["month", "day"]
    spent_usd: float
    cap_usd: float
    state: Literal["ok", "near", "spent"]
    resets_at: str


class TryRun(_Open):
    run_id: str
    status: str
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None


class TrySection(_Open):
    workflow_key: str
    last_run: Optional[TryRun] = None


class Verdict(_Open):
    label: str
    tone: Tone
    tip: str


class Secrets(_Open):
    backend: str
    needs_consent: bool


class SetupView(_Open):
    project_id: str
    ready: bool
    next: Optional[Literal["repository", "model", "try"]] = None
    verdict: Verdict
    problems: int
    secrets: Secrets
    repository: RepositorySection
    models: list[ModelEntry]
    detected: list[Detected]
    catalogue: list[CatalogueEntry]
    jobs: list[Job]
    budget: Budget
    # `try` is a Python keyword.
    try_: TrySection = Field(alias="try")
    build_database: Optional[dict[str, Any]] = None

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ── Requests ──────────────────────────────────────────────────────────────────────────────────

class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str
    actor: Optional[str] = None


class ChecksBody(_Body):
    target: str = "all"
    idempotency_key: Optional[str] = None


class RepositoryTokenBody(_Body):
    token: str = Field(json_schema_extra={"writeOnly": True})
    connection_id: Optional[str] = None
    accept_database_fallback: bool = False


class RepositoryAppBody(_Body):
    """Use the GitHub App connected in GitHub access (no fields beyond the project)."""


class RepositorySelectBody(_Body):
    connection_id: str
    full_name: str
    make_default: bool = True


class RepositoryDefaultBody(_Body):
    pass


class ModelConnectBody(_Body):
    provider: str
    kind: Literal["subscription", "api_key", "local"]
    api_key: Optional[str] = Field(default=None, json_schema_extra={"writeOnly": True})
    credential: Optional[str] = Field(default=None, json_schema_extra={"writeOnly": True})
    credential_file: Optional[str] = Field(default=None, json_schema_extra={"writeOnly": True})
    base_url: Optional[str] = None
    model: Optional[str] = None
    accept_database_fallback: bool = False
    idempotency_key: Optional[str] = None
    #: Replace: the one connection this credential replaces (never a working one with a worse).
    source_id: Optional[str] = None


class SourceModelBody(_Body):
    model: str


class JobBody(_Body):
    choice: Optional[JobChoice] = None


class RoutingBody(_Body):
    """A change to the model routing policy (docs/product/MODEL_ROUTER.md): any subset of
    mode, tiers, floors, floor_preset, headroom, thresholds, budget, escalation,
    allowed_sources, classifier, weekly_allowance_usd. `version` is the one the edit read."""
    policy: dict[str, Any]
    version: Optional[int] = None


class BudgetBody(_Body):
    monthly_cap_usd: float
    #: Which screen wrote it, for the `budget.monthly_cap_set` record: Setup's row or Govern.
    via: Optional[str] = None


class TryBody(_Body):
    directive: str
    idempotency_key: Optional[str] = None


class DecisionKeyBody(_Body):
    api_key: str = Field(json_schema_extra={"writeOnly": True})
    accept_database_fallback: bool = False


def dump(view: dict[str, Any]) -> dict[str, Any]:
    """Validate a view against the contract and hand it back unchanged in shape."""
    SetupView.model_validate(view)
    return view
