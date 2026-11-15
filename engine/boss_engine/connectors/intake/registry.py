"""The intake-adapter registry: one folder per source (the ticket-connector pattern).

    engine/boss_engine/connectors/intake/<key>/manifest.yaml   what it is, what it can do
    engine/boss_engine/connectors/intake/<key>/adapter.py      the class the manifest names

Loading a folder validates its manifest against :data:`MANIFEST_SCHEMA`, checks that the adapter
implements exactly the capabilities the manifest claims (both directions), and registers the
source's official hosts with the outbound credential guard (env_refs), as tickets do.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .base import IntakeAdapterBase, implements
from .contract import CAPABILITIES, DEDUPE_FIELDS, MODES, ConnectorManifestError

ROOT = Path(__file__).resolve().parent

#: The manifest schema: field -> (type, required). Nested shapes are checked in `validate`.
MANIFEST_SCHEMA: dict[str, tuple[type | tuple[type, ...], bool]] = {
    "key": (str, True),
    "label": (str, True),
    "icon": (str, True),
    "description": (str, True),
    "docs_url": (str, False),
    "adapter": (str, True),
    "order": (int, False),
    "modes": (list, True),
    "capabilities": (dict, True),
    "ack": (dict, False),
    "dedupe": (list, True),
    "auth_fields": (list, True),
    "default_hosts": (list, True),
    "guard_provider": (str, False),
    "operator_base_url_env": (list, False),
    "scopes": (list, False),
    "self_identity": (list, False),
    "event_types": (list, False),
}
_KEY = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_HOST = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")
_ACK_KEYS = {"deadline_ms", "success_status", "retry_signal_headers"}
#: A push route asserts its own budget is at most this share of the source's deadline (§3.2).
ACK_BUDGET_SHARE = 0.10


@dataclass(frozen=True)
class Manifest:
    key: str
    label: str
    icon: str
    description: str
    adapter: str
    modes: tuple[str, ...]
    capabilities: dict[str, bool]
    dedupe: tuple[str, ...]
    auth_fields: tuple[dict[str, Any], ...]
    default_hosts: tuple[str, ...]
    ack: dict[str, Any] = field(default_factory=dict)
    docs_url: str = ""
    order: int = 100
    guard_provider: str = ""
    operator_base_url_env: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    self_identity: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()
    folder: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "icon": self.icon,
            "description": self.description, "docs_url": self.docs_url,
            "modes": list(self.modes), "capabilities": dict(self.capabilities),
            "ack": dict(self.ack), "dedupe": list(self.dedupe),
            "auth_fields": [dict(f) for f in self.auth_fields],
            "default_hosts": list(self.default_hosts), "scopes": list(self.scopes),
            "self_identity": list(self.self_identity), "event_types": list(self.event_types),
        }


def validate(raw: Any, *, where: str = "manifest") -> Manifest:
    """A Manifest from parsed YAML, or ConnectorManifestError naming every problem."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise ConnectorManifestError(f"{where}: a manifest is a mapping")
    for name, (kind, required) in MANIFEST_SCHEMA.items():
        if name not in raw:
            if required:
                problems.append(f"missing '{name}'")
            continue
        if not isinstance(raw[name], kind) or (kind is int and isinstance(raw[name], bool)):
            problems.append(f"'{name}' must be {getattr(kind, '__name__', kind)}")
    for name in raw:
        if name not in MANIFEST_SCHEMA:
            problems.append(f"unknown field '{name}'")
    if problems:
        raise ConnectorManifestError(f"{where}: " + "; ".join(problems))
    if not _KEY.match(raw["key"]):
        problems.append("'key' must be lowercase letters, digits and _")
    modes = [str(m) for m in raw["modes"]]
    if not modes:
        problems.append("'modes' names at least one of " + ", ".join(MODES))
    for mode in modes:
        if mode not in MODES:
            problems.append(f"unknown mode '{mode}' (one of {', '.join(MODES)})")
    if len(set(modes)) != len(modes):
        problems.append("modes repeat")
    caps = raw["capabilities"]
    for cap in CAPABILITIES:
        if not isinstance(caps.get(cap), bool):
            problems.append(f"capabilities.{cap} must be true/false")
    for cap in caps:
        if cap not in CAPABILITIES:
            problems.append(f"unknown capability '{cap}'")
    # A mode promises the hooks that serve it.
    if "push" in modes or "socket" in modes:
        if caps.get("verify") is not True and "push" in modes:
            problems.append("mode push needs capabilities.verify")
        if caps.get("parse") is not True:
            problems.append("mode push/socket needs capabilities.parse")
    if "poll" in modes and caps.get("poll") is not True:
        problems.append("mode poll needs capabilities.poll")
    if "subscribe" in modes and caps.get("subscribe") is not True:
        problems.append("mode subscribe needs capabilities.subscribe")
    if "stream" in modes and caps.get("stream") is not True:
        problems.append("mode stream needs capabilities.stream")
    if caps.get("stream_reply") and not caps.get("reply"):
        problems.append("stream_reply needs reply")
    ack = raw.get("ack") or {}
    for key_ in ack:
        if key_ not in _ACK_KEYS:
            problems.append(f"ack: unknown field '{key_}'")
    if "push" in modes:
        deadline = ack.get("deadline_ms")
        if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline <= 0:
            problems.append("ack.deadline_ms (the source's own limit, > 0) is required for push")
    status = ack.get("success_status", 202)
    if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status < 300:
        problems.append("ack.success_status must be a 2xx code")
    retry_headers = ack.get("retry_signal_headers", [])
    if not isinstance(retry_headers, list) or any(not isinstance(h, str) for h in retry_headers):
        problems.append("ack.retry_signal_headers must be a list of header names")
    dedupe = [str(d) for d in raw["dedupe"]]
    if not dedupe:
        problems.append("'dedupe' names at least one envelope field")
    for name in dedupe:
        if name not in DEDUPE_FIELDS:
            problems.append(f"dedupe field '{name}' is not one of {', '.join(DEDUPE_FIELDS)}")
    fields = []
    for index, item in enumerate(raw["auth_fields"]):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) \
                or not isinstance(item.get("label"), str):
            problems.append(f"auth_fields[{index}] needs a name and a label")
            continue
        unknown = set(item) - {"name", "label", "secret", "placeholder", "required", "help"}
        if unknown:
            problems.append(f"auth_fields[{index}] unknown {sorted(unknown)}")
        for flag in ("secret", "required"):
            if flag in item and not isinstance(item[flag], bool):
                problems.append(f"auth_fields[{index}].{flag} must be true/false")
        fields.append(dict(item))
    names = [f["name"] for f in fields]
    if len(set(names)) != len(names):
        problems.append("auth_fields names repeat")
    if "push" in modes and not any(f.get("secret") for f in fields):
        problems.append("a push source needs a secret auth field to verify with")
    hosts = [str(h).strip().lower() for h in raw["default_hosts"]]
    for host in hosts:
        if not _HOST.match(host):
            problems.append(f"default host '{host}' is not a host name")
    for ident in raw.get("self_identity", []):
        if not isinstance(ident, str) or not re.match(r"^[a-z][a-z0-9_]*$", ident):
            problems.append(f"self_identity entry {ident!r} is not a configuration key")
    for et in raw.get("event_types", []):
        if not isinstance(et, str) or not re.match(r"^[a-z][a-z0-9_.*-]*$", et):
            problems.append(f"event_types entry {et!r} is not a reverse-DNS type")
    if problems:
        raise ConnectorManifestError(f"{where}: " + "; ".join(problems))
    return Manifest(
        key=raw["key"], label=raw["label"], icon=raw["icon"], description=raw["description"],
        adapter=raw["adapter"], modes=tuple(modes), capabilities=dict(caps),
        dedupe=tuple(dedupe), auth_fields=tuple(fields), default_hosts=tuple(hosts),
        ack={"deadline_ms": ack.get("deadline_ms"), "success_status": status,
             "retry_signal_headers": [h.lower() for h in retry_headers]},
        docs_url=raw.get("docs_url", ""), order=int(raw.get("order", 100)),
        guard_provider=raw.get("guard_provider", "") or raw["key"],
        operator_base_url_env=tuple(str(v) for v in raw.get("operator_base_url_env", [])),
        scopes=tuple(str(s) for s in raw.get("scopes", [])),
        self_identity=tuple(str(s) for s in raw.get("self_identity", [])),
        event_types=tuple(str(s) for s in raw.get("event_types", [])), folder=where)


def implemented_capabilities(adapter_cls: type) -> dict[str, bool]:
    return {cap: all(implements(adapter_cls, h) for h in hooks)
            for cap, hooks in CAPABILITIES.items()}


def check_adapter(manifest: Manifest, adapter_cls: type) -> None:
    """The adapter implements exactly what the manifest claims, and is an IntakeAdapterBase."""
    where = manifest.folder or manifest.key
    if not (isinstance(adapter_cls, type) and issubclass(adapter_cls, IntakeAdapterBase)):
        raise ConnectorManifestError(f"{where}: {manifest.adapter} must extend IntakeAdapterBase")
    have = implemented_capabilities(adapter_cls)
    problems = []
    for cap, claimed in manifest.capabilities.items():
        if claimed and not have[cap]:
            missing = [h for h in CAPABILITIES[cap] if not implements(adapter_cls, h)]
            problems.append(f"claims '{cap}' but does not implement {missing}")
        if not claimed and have[cap]:
            problems.append(f"implements '{cap}' but the manifest says false")
    partial = [cap for cap, hooks in CAPABILITIES.items()
               if any(implements(adapter_cls, h) for h in hooks) and not have[cap]]
    for cap in partial:
        if manifest.capabilities.get(cap) is False:
            problems.append(f"implements part of '{cap}' (declare it, or remove the hooks)")
    if problems:
        raise ConnectorManifestError(f"{where}: " + "; ".join(problems))


def _register_with_guard(manifest: Manifest) -> None:
    from ...services import env_refs

    env_refs.register_provider_hosts(manifest.guard_provider, manifest.default_hosts,
                                     operator_vars=manifest.operator_base_url_env)


def load_folder(folder: Path, *, package: Optional[str] = None) -> IntakeAdapterBase:
    folder = Path(folder)
    manifest_path = folder / "manifest.yaml"
    if not manifest_path.is_file():
        raise ConnectorManifestError(f"{folder}: no manifest.yaml")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConnectorManifestError(f"{folder.name}: manifest.yaml is not YAML: {error}") from None
    manifest = validate(raw, where=folder.name)
    if manifest.key != folder.name:
        raise ConnectorManifestError(f"{folder.name}: key '{manifest.key}' must match its folder")
    if package:
        module = importlib.import_module(f"{package}.{folder.name}.adapter")
    else:
        name = f"_boss_intake_adapter_{folder.name}"
        spec = importlib.util.spec_from_file_location(name, folder / "adapter.py")
        if spec is None or spec.loader is None:
            raise ConnectorManifestError(f"{folder.name}: no adapter.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    adapter_cls = getattr(module, manifest.adapter, None)
    if adapter_cls is None:
        raise ConnectorManifestError(f"{folder.name}: adapter.py has no class {manifest.adapter}")
    check_adapter(manifest, adapter_cls)
    adapter = adapter_cls()
    adapter.manifest = manifest
    adapter.intake_capabilities = dict(manifest.capabilities)
    if not getattr(adapter, "key", ""):
        adapter.key = manifest.key
    if adapter.key != manifest.key:
        raise ConnectorManifestError(f"{folder.name}: adapter key '{adapter.key}' != '{manifest.key}'")
    if not getattr(adapter, "display_name", ""):
        adapter.display_name = manifest.label
    _register_with_guard(manifest)
    return adapter


class IntakeRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._loaded = False

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            found = {}
            for folder in sorted(p for p in ROOT.iterdir()
                                 if p.is_dir() and (p / "manifest.yaml").is_file()):
                adapter = load_folder(folder, package=__package__)
                found[adapter.key] = adapter
            ordered = sorted(found.values(), key=lambda a: (a.manifest.order, a.key))
            self._adapters = {a.key: a for a in ordered}
            self._loaded = True

    def add(self, adapter: Any) -> None:
        """Register an extra adapter (a test's fake, a plug-in folder outside the package)."""
        self.load()
        self._adapters[adapter.key] = adapter

    def add_folder(self, folder: Path) -> Any:
        adapter = load_folder(Path(folder))
        self.add(adapter)
        return adapter

    def remove(self, key: str) -> None:
        self._adapters.pop(key, None)

    @property
    def adapters(self) -> dict[str, Any]:
        self.load()
        return self._adapters

    def get(self, key: str) -> Any:
        return self.adapters.get((key or "").strip().lower())

    def manifests(self) -> list[Manifest]:
        return [a.manifest for a in self.adapters.values()]


REGISTRY = IntakeRegistry()


def get_intake_adapter(key: str) -> Any:
    adapter = REGISTRY.get(key)
    if adapter is None:
        raise ValueError(f"Unknown intake source '{key}'. Supported: {', '.join(REGISTRY.adapters)}")
    return adapter


__all__ = ["ACK_BUDGET_SHARE", "MANIFEST_SCHEMA", "Manifest", "REGISTRY", "ROOT",
           "IntakeRegistry", "check_adapter", "get_intake_adapter", "implemented_capabilities",
           "load_folder", "validate"]
