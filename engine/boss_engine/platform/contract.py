"""The infrastructure plugin contract, `boss.plugin/v1`. STABLE.

Spec: BOSS_Infrastructure_Platform_and_Plugin_Architecture.md §6; plan: the MVP proposal.
Every provider plugin (official, verified or community) builds against this file. A breaking
change is a new apiVersion (`boss.plugin/v2`), never an edit; `test_platform_contract.py` pins
the field sets and the generated JSON Schemas under `boss_engine/contracts/`.

Four shapes, one hook interface:

    PluginManifest     `boss-plugin.yaml`: id, version, support level, capabilities, features,
                       runtime, permissions, config schema, hooks, the bindings it can produce.
    ResourceRequest    one entry of `resources:` in an app's `boss.deploy.yaml`
                       (`mode: owned|attached`).
    ResourcePlan       what a `plan` hook proposes: changes, ports, publicAccess, bindings,
                       destructive actions, expected cost. No mutation.
    OperationResult    what every hook returns: operationId, resourceId, status, bindings (secret
                       REFERENCES only), evidence, artifacts.

    InfraPlugin        the lifecycle hooks: validate, plan, provision, configure, verify,
                       observe, backup, restore, destroy. Each takes a HookContext and returns an
                       OperationResult. BOSS owns orchestration, approval, secrets, state and
                       evidence; a plugin implements only the provider-specific operation.

Rules a plugin must keep (the contract harness checks each one):
  * validate / plan / verify / observe never mutate anything.
  * provision is idempotent: the same request twice gives the same resourceId and bindings.
  * a secret VALUE never appears in a result, a log line, an artifact name or an error. A plugin
    hands a value to `ctx.secrets.put(...)` and returns the SecretRef it gets back.
  * verify proves a real connection (an Evidence of kind `connection` that passed), never
    "the command returned 0".
  * destroy removes everything provision created (no orphan containers, volumes or secrets),
    and is never called for a resource in `attached` mode.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Protocol, Union, get_args, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

API_VERSION = "boss.plugin/v1"
#: The version of this contract module. Additive changes bump MINOR; nothing here is removed or
#: renamed under boss.plugin/v1.
CONTRACT_VERSION = "1.1.0"
MANIFEST_FILE = "boss-plugin.yaml"

#: `official.postgres`, `community.example-kv`: a namespace, a dot, a name.
PLUGIN_ID = r"^[a-z][a-z0-9-]{1,31}\.[a-z0-9][a-z0-9.-]{1,63}$"
#: semver.org MAJOR.MINOR.PATCH with optional pre-release and build.
SEMVER = (r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
          r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
          r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$")
#: A capability: dotted lower-case words, `database.relational`, `cache`, `object-storage`.
CAPABILITY = r"^[a-z][a-z0-9-]{0,31}(\.[a-z][a-z0-9-]{0,31}){0,3}$"
#: A feature or engine word: `migrations`, `backup`, `postgres`.
WORD = r"^[a-z][a-z0-9_-]{0,47}$"
#: An environment variable the app receives.
ENV_NAME = r"^[A-Z][A-Z0-9_]{0,63}$"
#: A resource name, the key under `resources:`.
RESOURCE_NAME = r"^[a-z][a-z0-9-]{0,47}$"
#: An opaque secret reference: `<scheme>://<path>`. BOSS's broker issues `app_secret://...`.
SECRET_REF = r"^[a-z][a-z0-9_+-]{1,31}://[A-Za-z0-9_./:@-]{1,255}$"
#: A pinned container image: `name@sha256:<64 hex>`.
PINNED_IMAGE = r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$"
#: A Python entrypoint, `package.module:ClassName`.
ENTRYPOINT = r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"


class SupportLevel(StrEnum):
    """What BOSS promises about a plugin (spec §3). Shown on every surface that lists one."""
    CERTIFIED = "certified"        # every lifecycle stage tested in CI against pinned versions
    EXPERIMENTAL = "experimental"  # a documented subset works; some stages unverified
    COMMUNITY = "community"        # third-party; no BOSS-maintainer certification promise


class PluginSource(StrEnum):
    """Where a plugin came from, which decides how far it is trusted (spec §4.1, §7)."""
    BUILTIN = "builtin"      # shipped in this repository, reviewed with BOSS itself
    VERIFIED = "verified"    # third-party, pinned and checksum-verified by a maintainer
    COMMUNITY = "community"  # third-party, unverified: disabled until an operator enables it


class Hook(StrEnum):
    VALIDATE = "validate"
    PLAN = "plan"
    PROVISION = "provision"
    CONFIGURE = "configure"
    VERIFY = "verify"
    OBSERVE = "observe"
    BACKUP = "backup"
    RESTORE = "restore"
    DESTROY = "destroy"


HOOKS: tuple[Hook, ...] = tuple(Hook)
#: Every plugin implements these; the rest are optional and declared in `spec.hooks`.
REQUIRED_HOOKS: frozenset[Hook] = frozenset({Hook.VALIDATE, Hook.PLAN, Hook.PROVISION, Hook.VERIFY,
                                             Hook.DESTROY})
#: Hooks that never change anything; BOSS may run them without approval.
READ_ONLY_HOOKS: frozenset[Hook] = frozenset({Hook.VALIDATE, Hook.PLAN, Hook.VERIFY, Hook.OBSERVE})
#: Hooks that change a resource; they run only against an approved, digest-pinned plan.
MUTATING_HOOKS: frozenset[Hook] = frozenset({Hook.PROVISION, Hook.CONFIGURE, Hook.BACKUP,
                                             Hook.RESTORE, Hook.DESTROY})


class ResourceMode(StrEnum):
    OWNED = "owned"        # BOSS created it and may destroy it
    ATTACHED = "attached"  # it existed before BOSS; BOSS connects to it and NEVER destroys it


class OperationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    PLANNED = "planned"
    PROVISIONED = "provisioned"
    CONFIGURED = "configured"
    VERIFIED = "verified"
    OBSERVED = "observed"
    BACKED_UP = "backed_up"
    RESTORED = "restored"
    DESTROYED = "destroyed"
    UNSUPPORTED = "unsupported"  # the plugin does not implement this hook
    REFUSED = "refused"          # policy or the plugin refused (e.g. destroy of an attached resource)
    CANCELLED = "cancelled"
    FAILED = "failed"


#: The status a hook reports when it succeeded.
SUCCESS_STATUS: dict[Hook, OperationStatus] = {
    Hook.VALIDATE: OperationStatus.VALID,
    Hook.PLAN: OperationStatus.PLANNED,
    Hook.PROVISION: OperationStatus.PROVISIONED,
    Hook.CONFIGURE: OperationStatus.CONFIGURED,
    Hook.VERIFY: OperationStatus.VERIFIED,
    Hook.OBSERVE: OperationStatus.OBSERVED,
    Hook.BACKUP: OperationStatus.BACKED_UP,
    Hook.RESTORE: OperationStatus.RESTORED,
    Hook.DESTROY: OperationStatus.DESTROYED,
}


class ContractError(ValueError):
    """A manifest, request or result that is not valid boss.plugin/v1. One line per problem."""

    def __init__(self, reasons: list[str], what: str = "boss-plugin.yaml") -> None:
        self.reasons = list(reasons)
        super().__init__(f"invalid {what}:\n" + "\n".join(f"  - {r}" for r in self.reasons))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True,
                              use_enum_values=False)


def _unique(values: list[str], what: str) -> list[str]:
    seen = [v for i, v in enumerate(values) if v in values[:i]]
    if seen:
        raise ValueError(f"{what} lists {', '.join(sorted(set(seen)))} more than once")
    return values


# ---------------------------------------------------------------------------------------------
# The manifest: boss-plugin.yaml
# ---------------------------------------------------------------------------------------------

class PluginMetadata(_Strict):
    id: str = Field(pattern=PLUGIN_ID, description="Stable id: <namespace>.<name>, e.g. official.postgres")
    name: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=SEMVER, description="The plugin's own semantic version")
    owner: str = Field(min_length=1, max_length=128)
    license: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    homepage: Optional[str] = Field(default=None, max_length=300)


class PluginRuntime(_Strict):
    """How BOSS runs the hooks. `python`: in-process, from `entrypoint` (built-in plugins, P0
    speed). `container`: an isolated container from a digest-pinned image (third-party)."""
    type: Literal["python", "container"]
    entrypoint: Optional[str] = Field(default=None, pattern=ENTRYPOINT)
    image: Optional[str] = Field(default=None, pattern=PINNED_IMAGE)
    timeoutSeconds: int = Field(default=600, ge=1, le=3600)

    @model_validator(mode="after")
    def _shape(self) -> "PluginRuntime":
        if self.type == "python" and not self.entrypoint:
            raise ValueError("a python runtime needs entrypoint: package.module:ClassName")
        if self.type == "container" and not self.image:
            raise ValueError("a container runtime needs image pinned by digest (name@sha256:...)")
        return self


class PluginPermissions(_Strict):
    """What the plugin may touch. Everything is denied unless declared (spec §7); the operator
    reviews this list when enabling a plugin."""
    secrets: list[Literal["read-scoped", "write-scoped"]] = Field(default_factory=list)
    network: list[str] = Field(default_factory=list,
                               description="'declared-services' or 'egress:<host>' entries")
    filesystem: list[Literal["workspace-artifacts"]] = Field(default_factory=list)
    docker: bool = Field(default=False, description="May start and remove containers")
    subprocess: bool = False
    cloud: list[str] = Field(default_factory=list)

    @field_validator("network")
    @classmethod
    def _network(cls, v: list[str]) -> list[str]:
        for entry in v:
            if entry != "declared-services" and not re.fullmatch(r"egress:[a-z0-9.*-]{1,253}", entry):
                raise ValueError(f"network entry {entry!r} must be 'declared-services' or 'egress:<host>'")
        return v


class PluginSpec(_Strict):
    supportLevel: SupportLevel
    capabilities: list[str] = Field(min_length=1)
    features: list[str] = Field(default_factory=list)
    engines: list[str] = Field(default_factory=list, description="e.g. postgres, valkey, minio")
    engineVersions: list[str] = Field(default_factory=list, description="Pinned, certified versions")
    requires: list[str] = Field(default_factory=list,
                                description="Capabilities this plugin needs first (pgvector needs database.relational)")
    runtime: PluginRuntime
    permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    configSchema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema for ResourceRequest.config")
    hooks: list[Hook] = Field(min_length=1)
    bindings: list[str] = Field(default_factory=list, description="Env names the plugin can bind, e.g. DATABASE_URL")
    ports: list[int] = Field(default_factory=list, description="Container ports the resource listens on")
    limitations: list[str] = Field(default_factory=list)

    @field_validator("capabilities", "requires")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        for c in v:
            if not re.fullmatch(CAPABILITY, c):
                raise ValueError(f"capability {c!r} must be dotted lower-case words, e.g. database.relational")
        return _unique(v, "the list")

    @field_validator("features", "engines")
    @classmethod
    def _words(cls, v: list[str]) -> list[str]:
        for w in v:
            if not re.fullmatch(WORD, w):
                raise ValueError(f"{w!r} must be a lower-case word")
        return _unique(v, "the list")

    @field_validator("bindings")
    @classmethod
    def _bindings(cls, v: list[str]) -> list[str]:
        for b in v:
            if not re.fullmatch(ENV_NAME, b):
                raise ValueError(f"binding {b!r} must be an UPPER_CASE environment name")
        return _unique(v, "bindings")

    @field_validator("hooks")
    @classmethod
    def _hooks(cls, v: list[Hook]) -> list[Hook]:
        missing = sorted(h.value for h in REQUIRED_HOOKS if h not in v)
        if missing:
            raise ValueError(f"every plugin implements {', '.join(missing)}")
        return v

    @field_validator("ports")
    @classmethod
    def _ports(cls, v: list[int]) -> list[int]:
        for p in v:
            if not 1 <= p <= 65535:
                raise ValueError(f"port {p} is out of range")
        return v


class PluginManifest(_Strict):
    apiVersion: Literal["boss.plugin/v1"]
    kind: Literal["Plugin"]
    metadata: PluginMetadata
    spec: PluginSpec

    @property
    def id(self) -> str:
        return self.metadata.id

    @property
    def version(self) -> str:
        return self.metadata.version

    def implements(self, hook: Union[Hook, str]) -> bool:
        return Hook(hook) in self.spec.hooks

    def canonical_json(self) -> str:
        return canonical_json(self.model_dump(mode="json", exclude_none=True))

    def digest(self) -> str:
        """sha256 of the canonical manifest: what a registry pin and a receipt record."""
        return "sha256:" + hashlib.sha256(self.canonical_json().encode()).hexdigest()


# ---------------------------------------------------------------------------------------------
# The request: one entry under `resources:` in boss.deploy.yaml
# ---------------------------------------------------------------------------------------------

class AttachSpec(_Strict):
    """An existing resource BOSS connects to. Its connection values are ALREADY in the app's
    secret vault; `bindings` maps the env name the app gets to that vault secret's name."""
    bindings: dict[str, str] = Field(min_length=1)

    @field_validator("bindings")
    @classmethod
    def _names(cls, v: dict[str, str]) -> dict[str, str]:
        for k, s in v.items():
            if not re.fullmatch(ENV_NAME, k) or not re.fullmatch(ENV_NAME, s):
                raise ValueError(f"attach binding {k}: {s} must map an env name to a vault secret name")
        return v


class ResourceRequest(_Strict):
    """What an app asks for. `db: {capability: database.relational, engine: postgres}`."""
    name: str = Field(pattern=RESOURCE_NAME)
    capability: str = Field(pattern=CAPABILITY)
    provider: Optional[str] = Field(default=None, pattern=PLUGIN_ID, description="Plugin id; resolved by capability when absent")
    version: Optional[str] = Field(default=None, description="Plugin version pin: an exact semver or a ^/~ range")
    mode: ResourceMode = ResourceMode.OWNED
    environment: str = Field(default="deploy", pattern=WORD)
    engine: Optional[str] = Field(default=None, pattern=WORD)
    engineVersion: Optional[str] = Field(default=None, max_length=32)
    features: list[str] = Field(default_factory=list)
    public: bool = Field(default=False, alias="publicAccess", description="Expose a host port publicly; needs approval")
    persistence: bool = True
    backups: bool = False
    dependsOn: list[str] = Field(default_factory=list)
    bindings: dict[str, str] = Field(default_factory=dict,
                                     description="Rename map: plugin binding name -> env name the app gets")
    attach: Optional[AttachSpec] = None
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("features")
    @classmethod
    def _features(cls, v: list[str]) -> list[str]:
        for w in v:
            if not re.fullmatch(WORD, w):
                raise ValueError(f"feature {w!r} must be a lower-case word")
        return v

    @field_validator("dependsOn")
    @classmethod
    def _deps(cls, v: list[str]) -> list[str]:
        for d in v:
            if not re.fullmatch(RESOURCE_NAME, d):
                raise ValueError(f"dependsOn {d!r} must name another resource")
        return v

    @field_validator("bindings")
    @classmethod
    def _rename(cls, v: dict[str, str]) -> dict[str, str]:
        for k, s in v.items():
            if not re.fullmatch(ENV_NAME, k) or not re.fullmatch(ENV_NAME, s):
                raise ValueError(f"binding {k}: {s} must map env names")
        return v

    @model_validator(mode="after")
    def _mode(self) -> "ResourceRequest":
        if self.mode == ResourceMode.ATTACHED and self.attach is None:
            raise ValueError("an attached resource needs attach.bindings naming its vault secrets")
        if self.mode == ResourceMode.OWNED and self.attach is not None:
            raise ValueError("attach is only for mode: attached")
        if self.name in self.dependsOn:
            raise ValueError("a resource cannot depend on itself")
        return self

    def resource_id(self, app_key: str) -> str:
        """Environment-scoped, stable: `deploy/helpdesk/db`."""
        return f"{self.environment}/{app_key}/{self.name}"


# ---------------------------------------------------------------------------------------------
# The plan and the result
# ---------------------------------------------------------------------------------------------

class SecretRef(_Strict):
    """A reference to a value in BOSS's secret vault. The only way a binding leaves a plugin."""
    secretRef: str = Field(pattern=SECRET_REF)


class PortSpec(_Strict):
    name: str = Field(min_length=1, max_length=32)
    containerPort: int = Field(ge=1, le=65535)
    hostPort: Optional[int] = Field(default=None, ge=1, le=65535)
    public: bool = False
    protocol: Literal["tcp", "udp"] = "tcp"


class PlannedChange(_Strict):
    action: Literal["create", "update", "replace", "delete", "attach", "noop"]
    target: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=500)
    destructive: bool = False


class CostEstimate(_Strict):
    monthlyUsd: Optional[float] = Field(default=None, ge=0)
    basis: str = Field(default="", max_length=200, description="How the number was reached, or why unknown")


class ResourcePlan(_Strict):
    """What a `plan` hook proposes for one resource. Declarative; nothing has happened yet."""
    resourceId: str
    pluginId: str = Field(pattern=PLUGIN_ID)
    pluginVersion: str = Field(pattern=SEMVER)
    mode: ResourceMode
    changes: list[PlannedChange] = Field(default_factory=list)
    ports: list[PortSpec] = Field(default_factory=list)
    publicAccess: bool = False
    bindings: list[str] = Field(default_factory=list, description="Env names the app will get, by name only")
    dependsOn: list[str] = Field(default_factory=list)
    cost: Optional[CostEstimate] = None
    risks: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @property
    def destructive(self) -> bool:
        return any(c.destructive or c.action in ("delete", "replace") for c in self.changes)


EvidenceKind = Literal["connection", "healthcheck", "migration", "backup", "restore", "cleanup",
                       "config", "observe", "other"]


class Evidence(_Strict):
    """One observed fact. `connection` evidence is a real round trip to the resource."""
    check: str = Field(min_length=1, max_length=120)
    passed: bool
    kind: EvidenceKind = "other"
    detail: str = Field(default="", max_length=500)

    @classmethod
    def parse(cls, value: Union[str, dict, "Evidence"]) -> "Evidence":
        """Accept the short form `healthcheck:passed` (spec §6) as well as the mapping."""
        if isinstance(value, Evidence):
            return value
        if isinstance(value, str):
            check, _, outcome = value.rpartition(":")
            if not check or outcome not in ("passed", "failed"):
                raise ValueError(f"evidence {value!r} must be '<check>:passed' or '<check>:failed'")
            kind = check if check in get_args(EvidenceKind) else "other"
            return cls(check=check, passed=outcome == "passed", kind=kind)
        return cls.model_validate(value)


class OperationError(_Strict):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(max_length=1000)
    retryable: bool = False


class OperationResult(_Strict):
    """What every hook returns (spec §6 "standardized result")."""
    operationId: str = Field(min_length=1, max_length=128)
    resourceId: str = Field(min_length=1, max_length=200)
    hook: Hook
    status: OperationStatus
    pluginId: Optional[str] = Field(default=None, pattern=PLUGIN_ID)
    pluginVersion: Optional[str] = Field(default=None, pattern=SEMVER)
    bindings: dict[str, SecretRef] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    plan: Optional[ResourcePlan] = None
    state: dict[str, Any] = Field(default_factory=dict,
                                  description="Non-secret observed state BOSS keeps and hands back as ctx.state")
    observed: dict[str, Any] = Field(default_factory=dict, description="observe: normalized health/metrics")
    errors: list[OperationError] = Field(default_factory=list)
    durationMs: Optional[int] = Field(default=None, ge=0)

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, v: Any) -> Any:
        return [Evidence.parse(e) for e in (v or [])]

    @field_validator("bindings")
    @classmethod
    def _binding_names(cls, v: dict[str, SecretRef]) -> dict[str, SecretRef]:
        for k in v:
            if not re.fullmatch(ENV_NAME, k):
                raise ValueError(f"binding {k!r} must be an UPPER_CASE environment name")
        return v

    @property
    def ok(self) -> bool:
        return self.status == SUCCESS_STATUS[self.hook]

    @property
    def connected(self) -> bool:
        """verify proved a real connection: a passed `connection` evidence and nothing failed."""
        return (any(e.kind == "connection" and e.passed for e in self.evidence)
                and all(e.passed for e in self.evidence))


# ---------------------------------------------------------------------------------------------
# The hook interface
# ---------------------------------------------------------------------------------------------

@runtime_checkable
class SecretBroker(Protocol):
    """BOSS's side of the secret boundary. A plugin stores a value and gets a reference back;
    only `resolve` (for the plugin's own scoped resource, e.g. verify reconnecting) reads one."""

    def put(self, name: str, value: str) -> SecretRef: ...

    def resolve(self, ref: Union[SecretRef, str]) -> str: ...

    def delete(self, ref: Union[SecretRef, str]) -> None: ...


@dataclass
class CommandOutcome:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@runtime_checkable
class HostRunner(Protocol):
    """Where a plugin's commands run: the target host of the deploy (over BOSS's governed SSH)
    or, in the harness and for a local install, this machine. A plugin never opens its own
    connection to a host; it calls `ctx.host.run(...)`. `address` is how the host is reached."""
    address: str

    def run(self, command: str, *, timeout: int = 120, stdin_text: Optional[str] = None) -> Any: ...


class LocalHost:
    """This machine. `run` executes through bash, returns a CommandOutcome, never raises on a
    non-zero exit (a timeout is returncode 124)."""
    address = "127.0.0.1"

    def run(self, command: str, *, timeout: int = 120, stdin_text: Optional[str] = None) -> CommandOutcome:
        import subprocess
        try:
            proc = subprocess.run(["bash", "-c", command], input=stdin_text, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return CommandOutcome(124, "", f"timed out after {timeout}s")
        return CommandOutcome(proc.returncode, proc.stdout, proc.stderr)


@dataclass
class HookContext:
    """Everything a hook gets. Built by BOSS (or the contract harness); plugins only read it."""
    hook: Hook
    operation_id: str
    idempotency_key: str
    resource: ResourceRequest
    resource_id: str
    app_key: str
    secrets: SecretBroker
    workdir: Path
    project_id: Optional[str] = None
    plan: Optional[ResourcePlan] = None          # the APPROVED plan, for mutating hooks
    plan_digest: Optional[str] = None
    state: dict[str, Any] = field(default_factory=dict)   # what earlier operations returned as `state`
    labels: dict[str, str] = field(default_factory=dict)  # put on every container/volume created
    is_cancelled: Callable[[], bool] = lambda: False
    deadline: Optional[float] = None             # time.monotonic() deadline
    log: Callable[[str], None] = lambda _line: None  # redacting logger supplied by BOSS
    plugin_id: str = ""
    plugin_version: str = ""
    host: HostRunner = field(default_factory=LocalHost)  # contract 1.1: where commands run

    def result(self, status: Union[OperationStatus, str, None] = None, **kw: Any) -> OperationResult:
        """An OperationResult with the ids filled in; `status` defaults to the hook's success."""
        return OperationResult(
            operationId=self.operation_id, resourceId=self.resource_id, hook=self.hook,
            status=OperationStatus(status) if status else SUCCESS_STATUS[self.hook],
            pluginId=self.plugin_id or None, pluginVersion=self.plugin_version or None, **kw)

    def fail(self, code: str, message: str, *, retryable: bool = False, **kw: Any) -> OperationResult:
        return self.result(OperationStatus.FAILED,
                           errors=[OperationError(code=code, message=message, retryable=retryable)], **kw)


class InfraPlugin:
    """Subclass and implement the hooks your manifest declares. An undeclared hook answers
    `unsupported`; BOSS never calls `destroy` for an attached resource."""

    #: Set by the loader from boss-plugin.yaml.
    manifest: Optional[PluginManifest] = None

    def validate(self, ctx: HookContext) -> OperationResult:
        return _unsupported(ctx)

    def plan(self, ctx: HookContext) -> OperationResult:
        return _unsupported(ctx)

    def provision(self, ctx: HookContext) -> OperationResult:
        return _unsupported(ctx)

    def configure(self, ctx: HookContext) -> OperationResult:
        return _unsupported(ctx)

    def verify(self, ctx: HookContext) -> OperationResult:
        return _unsupported(ctx)

    def observe(self, ctx: HookContext) -> OperationResult:
        return _unsupported(ctx)

    def backup(self, ctx: HookContext) -> OperationResult:
        return _unsupported(ctx)

    def restore(self, ctx: HookContext) -> OperationResult:
        return _unsupported(ctx)

    def destroy(self, ctx: HookContext) -> OperationResult:
        return _unsupported(ctx)

    def run(self, ctx: HookContext) -> OperationResult:
        """Dispatch one hook. Exceptions become a `failed` result; they never escape."""
        try:
            result = getattr(self, ctx.hook.value)(ctx)
        except Exception as exc:  # a plugin bug is a failed operation, not a crashed worker
            return ctx.fail("plugin_error", f"{type(exc).__name__} in {ctx.hook.value}")
        if not isinstance(result, OperationResult):
            return ctx.fail("bad_result", f"{ctx.hook.value} did not return an OperationResult")
        return result


def _unsupported(ctx: HookContext) -> OperationResult:
    return ctx.result(OperationStatus.UNSUPPORTED)


# ---------------------------------------------------------------------------------------------
# Loading and hashing
# ---------------------------------------------------------------------------------------------

def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest_of(value: Any) -> str:
    """sha256 of a JSON-able value in canonical form (a plan digest)."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True, by_alias=False)
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def idempotency_key(plugin_id: str, plugin_version: str, resource_id: str, hook: Union[Hook, str],
                    plan_digest: Optional[str]) -> str:
    """Same plugin, resource, hook and approved plan -> same key -> the receipt dedupes."""
    raw = "|".join([plugin_id, plugin_version, resource_id, Hook(hook).value, plan_digest or "-"])
    return "infra:" + hashlib.sha256(raw.encode()).hexdigest()[:40]


def _reasons(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "extra_forbidden":
            msg = f"unknown field ({API_VERSION} does not define it)"
        elif err["type"] == "missing":
            msg = "is required"
        out.append(f"{loc}: {msg}")
    return out


def _mapping(source: Union[str, bytes, dict[str, Any]], what: str) -> dict[str, Any]:
    data: Any = source
    if isinstance(source, (str, bytes)):
        try:
            data = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise ContractError([f"not valid YAML: {exc}"], what) from None
    if not isinstance(data, dict):
        raise ContractError(["must be a YAML mapping"], what)
    return data


def validate_manifest(source: Union[str, bytes, dict[str, Any]]) -> PluginManifest:
    """Parse and validate a `boss-plugin.yaml` (text or mapping). Raises ContractError."""
    data = _mapping(source, MANIFEST_FILE)
    try:
        return PluginManifest.model_validate(data)
    except ValidationError as exc:
        raise ContractError(_reasons(exc)) from None


def load_manifest(path: Union[str, Path]) -> PluginManifest:
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST_FILE
    return validate_manifest(p.read_text(encoding="utf-8"))


def validate_resources(source: Any) -> list[ResourceRequest]:
    """`resources:` from boss.deploy.yaml: a mapping of name -> request. Raises ContractError."""
    if source is None:
        return []
    if not isinstance(source, dict):
        raise ContractError(["resources must be a mapping of name -> request"], "resources")
    out: list[ResourceRequest] = []
    reasons: list[str] = []
    for name, body in source.items():
        if not isinstance(body, dict):
            reasons.append(f"{name}: must be a mapping")
            continue
        try:
            out.append(ResourceRequest.model_validate({**body, "name": name}))
        except ValidationError as exc:
            reasons.extend(f"{name}.{r}" for r in _reasons(exc))
    names = {r.name for r in out}
    for r in out:
        for d in r.dependsOn:
            if d not in names:
                reasons.append(f"{r.name}.dependsOn: no resource named {d!r}")
    if reasons:
        raise ContractError(reasons, "resources")
    return out


#: The JSON Schemas generated from these models, committed under boss_engine/contracts/.
SCHEMAS: dict[str, type[BaseModel]] = {
    "boss_plugin_v1.schema.json": PluginManifest,
    "boss_resource_request_v1.schema.json": ResourceRequest,
    "boss_operation_result_v1.schema.json": OperationResult,
}


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://boss.dev/schemas/{API_VERSION}/{model.__name__}"
    return schema


def write_schemas(directory: Optional[Path] = None) -> list[Path]:
    """Regenerate the committed schema files: `python -m boss_engine.platform schemas`."""
    directory = directory or Path(__file__).resolve().parent.parent / "contracts"
    written = []
    for filename, model in SCHEMAS.items():
        p = directory / filename
        p.write_text(json.dumps(json_schema(model), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(p)
    return written
