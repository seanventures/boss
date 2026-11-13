"""The general-purpose extension contract, `boss.extension/v1` (BOSSAPP-861). STABLE.

`boss.plugin/v1` (platform/contract.py) is for infrastructure providers only and is not touched
by this file. An EXTENSION adds an integration, a tool, a scheduled job, a webhook or a console
panel to BOSS without a fork. It declares, in `boss-extension.yaml`:

    metadata      id (`<namespace>.<name>`), name, version (semver), description, publisher
    spec.entrypoint   `module:ClassName`, a subclass of extensions.sdk.Extension
    spec.checksum     sha256 of the bundle (every file but this manifest), see bundle_checksum
    spec.capabilities the CLOSED list below; anything else is refused at validation
    spec.runtime      timeoutSeconds for one broker call
    spec.tools / events / jobs / webhooks / ui / state
                      the declarations each capability needs; declaring a section without its
                      capability (or a capability that needs a section without it) is refused

Every capability is one small, versioned RPC over the broker (extensions/broker.py), which runs the
extension in the SAME isolated child process shape as boss.plugin/v1 (platform/runner.py): an empty
environment, JSON lines over stdin/stdout, a timeout, and a checksum gate that refuses a bundle whose
files changed since an operator approved them.

A breaking change is `boss.extension/v2`, never an edit; test_extension_sdk.py pins the field sets.
"""
from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..platform.contract import ENTRYPOINT, SEMVER

API_VERSION = "boss.extension/v1"
KIND = "Extension"
#: Additive changes bump MINOR; nothing is removed or renamed under boss.extension/v1.
CONTRACT_VERSION = "1.0.0"
MANIFEST_FILE = "boss-extension.yaml"

#: `acme.deploy-notes`: a namespace, a dot, a name (the same shape as a plugin id).
EXTENSION_ID = r"^[a-z][a-z0-9-]{1,31}\.[a-z0-9][a-z0-9-]{1,63}$"
#: A tool, job, webhook or slot name: lower-case words joined by `_` or `-`.
NAME = r"^[a-z][a-z0-9_-]{0,47}$"
#: An event kind filter: `deploy.succeeded`, `deploy.*`, `*`.
EVENT_KIND = r"^(\*|[a-z][a-z0-9_]*(\.[a-z0-9_]+)*(\.\*)?)$"
CHECKSUM = r"^sha256:[0-9a-f]{64}$"
_IGNORED = {"__pycache__", ".pytest_cache", ".DS_Store", ".mypy_cache", ".ruff_cache"}


class Capability(StrEnum):
    """THE CLOSED LIST. A manifest naming anything else is refused; a broker call needing a
    capability the manifest did not declare is refused at the broker, before the child runs."""
    TOOLS_REGISTER = "tools.register"
    EVENTS_SUBSCRIBE = "events.subscribe"
    JOBS_SCHEDULE = "jobs.schedule"
    WEBHOOKS_RECEIVE = "webhooks.receive"
    STATE_READ = "state.read"
    STATE_WRITE = "state.write"
    UI_SLOT_REGISTER = "ui.slot.register"


CAPABILITIES: tuple[str, ...] = tuple(c.value for c in Capability)

#: What each capability lets the extension do, in the words the install consent shows.
CAPABILITY_WORDS: dict[str, str] = {
    "tools.register": "Add tools agents can call (each call goes through tool policy, a receipt "
                      "and the audit log)",
    "events.subscribe": "Read BOSS events of the kinds it names, in the projects it is enabled in",
    "jobs.schedule": "Run on a schedule",
    "webhooks.receive": "Receive webhooks at a BOSS URL (each signed with a per-hook secret)",
    "state.read": "Read its own stored data",
    "state.write": "Store its own data (size-capped, visible to no other extension)",
    "ui.slot.register": "Show a panel in the console, inside a sandboxed frame",
}


class SlotType(StrEnum):
    """The console places an extension may draw in. Two to start (BOSSAPP-861)."""
    PROJECT_PANEL = "project_panel"
    SETTINGS_PANEL = "settings_panel"


#: The RPC methods the broker speaks, each with the capability it needs. Versioned with the
#: contract: `rpc` on every request is RPC_VERSION.
RPC_VERSION = 1
RPC_METHODS: dict[str, str] = {
    "tool.call": Capability.TOOLS_REGISTER.value,
    "events.deliver": Capability.EVENTS_SUBSCRIBE.value,
    "job.run": Capability.JOBS_SCHEDULE.value,
    "webhook.receive": Capability.WEBHOOKS_RECEIVE.value,
}
#: The requests a child may send back while a call runs, with the capability each needs
#: (None: any extension may).
CHILD_OPS: dict[str, Optional[str]] = {
    "state.get": Capability.STATE_READ.value,
    "state.list": Capability.STATE_READ.value,
    "state.put": Capability.STATE_WRITE.value,
    "state.delete": Capability.STATE_WRITE.value,
    "log": None,
}


class ExtensionContractError(ValueError):
    """A manifest that is not valid boss.extension/v1. One line per problem."""

    def __init__(self, reasons: list[str], what: str = MANIFEST_FILE) -> None:
        self.reasons = list(reasons)
        super().__init__(f"invalid {what}:\n" + "\n".join(f"  - {r}" for r in self.reasons))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


class Metadata(_Strict):
    id: str = Field(pattern=EXTENSION_ID)
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(pattern=SEMVER)
    description: str = Field(default="", max_length=500)
    publisher: str = Field(default="", max_length=120)
    license: str = Field(default="", max_length=60)


class Runtime(_Strict):
    timeoutSeconds: int = Field(default=20, ge=1, le=300)


class ToolSpec(_Strict):
    name: str = Field(pattern=NAME)
    description: str = Field(min_length=1, max_length=1000)
    inputSchema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    #: What the tool CLAIMS (0 read … 3 irreversible). It may raise the tier BOSS applies and
    #: never lower it (mcp_registry.tool_tier).
    riskTier: int = Field(default=1, ge=0, le=3)

    @field_validator("inputSchema")
    @classmethod
    def _object_schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        if v.get("type") != "object":
            raise ValueError("inputSchema must be a JSON Schema with type: object")
        return v


class EventsSpec(_Strict):
    kinds: list[str] = Field(min_length=1, max_length=32)
    #: Which projects' events it sees: only those it is enabled in. Declared so the consent says it.
    projects: Literal["enabled"] = "enabled"
    batchSize: int = Field(default=50, ge=1, le=500)

    @field_validator("kinds")
    @classmethod
    def _kinds(cls, v: list[str]) -> list[str]:
        bad = [k for k in v if not re.fullmatch(EVENT_KIND, k)]
        if bad:
            raise ValueError(f"event kinds must look like deploy.succeeded or deploy.*: {bad}")
        return v


class JobSpec(_Strict):
    name: str = Field(pattern=NAME)
    #: A five-field cron (`*/15 * * * *`) or an interval (`every 10m`, `every 2h`).
    schedule: str = Field(min_length=1, max_length=64)

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, v: str) -> str:
        from .jobs import parse_schedule

        parse_schedule(v)  # raises ValueError with the reason
        return v


class WebhookSpec(_Strict):
    name: str = Field(pattern=NAME)
    description: str = Field(default="", max_length=300)
    maxBodyBytes: int = Field(default=256 * 1024, ge=1, le=1024 * 1024)


class SlotSpec(_Strict):
    type: SlotType
    name: str = Field(pattern=NAME)
    title: str = Field(min_length=1, max_length=60)
    #: A file inside the bundle, served as the frame's document.
    entry: str = Field(min_length=1, max_length=200)

    @field_validator("entry")
    @classmethod
    def _inside(cls, v: str) -> str:
        p = Path(v)
        if p.is_absolute() or ".." in p.parts or p.suffix.lower() not in (".html", ".htm"):
            raise ValueError("entry is a relative .html path inside the bundle")
        return v


class UiSpec(_Strict):
    slots: list[SlotSpec] = Field(min_length=1, max_length=8)


class StateSpec(_Strict):
    maxValueBytes: int = Field(default=16 * 1024, ge=1, le=64 * 1024)
    maxKeys: int = Field(default=1000, ge=1, le=10_000)


class Spec(_Strict):
    entrypoint: str = Field(pattern=ENTRYPOINT)
    checksum: str = Field(pattern=CHECKSUM)
    capabilities: list[str] = Field(min_length=1)
    runtime: Runtime = Field(default_factory=Runtime)
    tools: list[ToolSpec] = Field(default_factory=list)
    events: Optional[EventsSpec] = None
    jobs: list[JobSpec] = Field(default_factory=list)
    webhooks: list[WebhookSpec] = Field(default_factory=list)
    ui: Optional[UiSpec] = None
    state: StateSpec = Field(default_factory=StateSpec)

    @field_validator("capabilities")
    @classmethod
    def _closed(cls, v: list[str]) -> list[str]:
        unknown = [c for c in v if c not in CAPABILITIES]
        if unknown:
            raise ValueError(f"unknown capabilities {unknown}; {API_VERSION} allows only "
                             f"{', '.join(CAPABILITIES)}")
        if len(set(v)) != len(v):
            raise ValueError("a capability is listed twice")
        return v

    @model_validator(mode="after")
    def _sections_match(self) -> "Spec":
        caps = set(self.capabilities)
        problems: list[str] = []
        pairs = [("tools.register", bool(self.tools), "tools"),
                 ("events.subscribe", self.events is not None, "events"),
                 ("jobs.schedule", bool(self.jobs), "jobs"),
                 ("webhooks.receive", bool(self.webhooks), "webhooks"),
                 ("ui.slot.register", self.ui is not None, "ui")]
        for cap, present, section in pairs:
            if present and cap not in caps:
                problems.append(f"spec.{section} is declared but {cap} is not in capabilities")
            if cap in caps and not present:
                problems.append(f"{cap} needs a non-empty spec.{section}")
        for label, names in (("tools", [t.name for t in self.tools]),
                             ("jobs", [j.name for j in self.jobs]),
                             ("webhooks", [w.name for w in self.webhooks]),
                             ("ui.slots", [s.name for s in (self.ui.slots if self.ui else [])])):
            if len(set(names)) != len(names):
                problems.append(f"spec.{label}: names must be unique")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class ExtensionManifest(_Strict):
    apiVersion: Literal["boss.extension/v1"]
    kind: Literal["Extension"]
    metadata: Metadata
    spec: Spec

    @property
    def id(self) -> str:
        return self.metadata.id

    @property
    def version(self) -> str:
        return self.metadata.version

    def has(self, capability: Union[str, Capability]) -> bool:
        return str(getattr(capability, "value", capability)) in self.spec.capabilities

    def tool(self, name: str) -> Optional[ToolSpec]:
        return next((t for t in self.spec.tools if t.name == name), None)

    def webhook(self, name: str) -> Optional[WebhookSpec]:
        return next((w for w in self.spec.webhooks if w.name == name), None)

    def slot(self, name: str) -> Optional[SlotSpec]:
        return next((s for s in (self.spec.ui.slots if self.spec.ui else []) if s.name == name), None)

    def consent(self) -> list[dict[str, str]]:
        """What the install screen shows before an operator accepts: one line per capability."""
        return [{"capability": c, "words": CAPABILITY_WORDS[c]} for c in self.spec.capabilities]


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


def validate_manifest(source: Union[str, bytes, dict[str, Any]]) -> ExtensionManifest:
    """Parse and validate a boss-extension.yaml (text or mapping). Raises ExtensionContractError."""
    if isinstance(source, (str, bytes)):
        try:
            data = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise ExtensionContractError([f"not YAML: {exc}"]) from None
    else:
        data = source
    if not isinstance(data, dict):
        raise ExtensionContractError(["the manifest must be a mapping"])
    if data.get("apiVersion") == "boss.plugin/v1":
        raise ExtensionContractError(["this is a boss.plugin/v1 infrastructure plugin, not an "
                                      "extension; install it under plugins/ instead"])
    try:
        return ExtensionManifest.model_validate(data)
    except ValidationError as exc:
        raise ExtensionContractError(_reasons(exc)) from None


def load_manifest(path: Union[str, Path]) -> ExtensionManifest:
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST_FILE
    if not p.is_file():
        raise ExtensionContractError([f"no {MANIFEST_FILE} at {p.parent}"])
    return validate_manifest(p.read_text(encoding="utf-8"))


def bundle_checksum(directory: Union[str, Path]) -> str:
    """sha256 over every bundle file's relative path and bytes, in sorted order, EXCEPT the
    manifest (which carries this value, so cannot be inside it). Same scheme as the plugin
    registry's tree_checksum."""
    root = Path(directory)
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if (any(part in _IGNORED for part in rel.parts) or p.suffix == ".pyc" or not p.is_file()
                or rel.as_posix() == MANIFEST_FILE):
            continue
        h.update(rel.as_posix().encode() + b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return "sha256:" + h.hexdigest()
