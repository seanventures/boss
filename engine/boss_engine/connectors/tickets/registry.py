"""The ticket-connector registry: one folder per connector.

    engine/boss_engine/connectors/tickets/<key>/manifest.yaml   what it is, what it can do
    engine/boss_engine/connectors/tickets/<key>/adapter.py      the class the manifest names

Loading a folder validates its manifest against :data:`MANIFEST_SCHEMA`, checks that the adapter
implements exactly the capabilities the manifest claims, and registers the connector's official
hosts (and operator base-URL variables) with the outbound credential guard (env_refs).
``work_tracker_adapters.TRACKER_ADAPTERS`` / ``get_adapter`` / ``list_catalog`` read from here.
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

from .base import TicketAdapterBase, implements
from .contract import CAPABILITIES, STATUS_CATEGORIES, ConnectorManifestError

ROOT = Path(__file__).resolve().parent

WEBHOOK_SCHEMES = ("none", "hmac-sha256", "shared-token", "hmac-sha256-or-shared-token")

#: The manifest schema: field -> (type, required). Nested shapes are checked in `validate`.
MANIFEST_SCHEMA: dict[str, tuple[type | tuple[type, ...], bool]] = {
    "key": (str, True),
    "label": (str, True),
    "icon": (str, True),
    "description": (str, True),
    "docs_url": (str, False),
    "adapter": (str, True),
    "order": (int, False),
    "auth_fields": (list, True),
    "capabilities": (dict, True),
    "default_hosts": (list, True),
    "default_base_url": (str, False),
    "base_url_field": (str, False),
    "base_url_fields": (list, False),
    "guard_provider": (str, False),
    "operator_base_url_env": (list, False),
    "key_refs": (bool, False),
    "status_mapping_defaults": (dict, False),
    "webhook": (dict, False),
    "auth_kinds": (list, False),
}
_KEY = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_HOST = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")


@dataclass(frozen=True)
class Manifest:
    key: str
    label: str
    icon: str
    description: str
    adapter: str
    auth_fields: tuple[dict[str, Any], ...]
    capabilities: dict[str, bool]
    default_hosts: tuple[str, ...]
    docs_url: str = ""
    order: int = 100
    default_base_url: str = ""
    base_url_field: str = ""
    base_url_fields: tuple[str, ...] = ()
    guard_provider: str = ""
    operator_base_url_env: tuple[str, ...] = ()
    key_refs: bool = False
    status_mapping_defaults: dict[str, Any] = field(default_factory=dict)
    webhook: dict[str, Any] = field(default_factory=dict)
    auth_kinds: tuple[dict[str, Any], ...] = ()
    folder: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "icon": self.icon,
            "description": self.description, "docs_url": self.docs_url,
            "auth_fields": [dict(f) for f in self.auth_fields],
            "capabilities": dict(self.capabilities), "default_hosts": list(self.default_hosts),
            "base_url_field": self.base_url_field,
            "status_mapping_defaults": dict(self.status_mapping_defaults),
            "webhook": dict(self.webhook), "key_refs": self.key_refs,
            "auth_kinds": [dict(k) for k in self.auth_kinds],
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
    caps = raw["capabilities"]
    for cap in CAPABILITIES:
        if not isinstance(caps.get(cap), bool):
            problems.append(f"capabilities.{cap} must be true/false")
    for cap in caps:
        if cap not in CAPABILITIES:
            problems.append(f"unknown capability '{cap}'")
    hosts = [str(h).strip().lower() for h in raw["default_hosts"]]
    for host in hosts:
        if not _HOST.match(host):
            problems.append(f"default host '{host}' is not a host name")
    base_field = raw.get("base_url_field", "")
    base_fields = [str(f) for f in raw.get("base_url_fields", [])] or ([base_field] if base_field
                                                                        else [])
    if base_field and base_field not in names:
        problems.append(f"base_url_field '{base_field}' is not one of auth_fields")
    for f in base_fields:
        if not re.match(r"^[a-z][a-z0-9_]*$", f):
            problems.append(f"base_url_fields entry '{f}' is not a configuration key")
    if not hosts and not base_fields:
        problems.append("a connector needs default_hosts or a base_url_field")
    mapping = raw.get("status_mapping_defaults", {})
    for category in mapping:
        if category not in STATUS_CATEGORIES:
            problems.append(f"status_mapping_defaults: unknown category '{category}'")
    hook = raw.get("webhook", {})
    if caps.get("webhook"):
        if hook.get("scheme") not in WEBHOOK_SCHEMES[1:]:
            problems.append(f"webhook.scheme must be one of {', '.join(WEBHOOK_SCHEMES[1:])}")
        if "hmac" in str(hook.get("scheme")) and not isinstance(hook.get("header"), str):
            problems.append("webhook.header names the signature header")
    from .auth import default_auth_kinds, validate_auth_kinds

    kinds = raw.get("auth_kinds") or default_auth_kinds(fields)
    problems.extend(validate_auth_kinds(kinds, names, hosts))
    if problems:
        raise ConnectorManifestError(f"{where}: " + "; ".join(problems))
    return Manifest(
        key=raw["key"], label=raw["label"], icon=raw["icon"], description=raw["description"],
        adapter=raw["adapter"], auth_fields=tuple(fields), capabilities=dict(caps),
        default_hosts=tuple(hosts), docs_url=raw.get("docs_url", ""),
        order=int(raw.get("order", 100)), default_base_url=raw.get("default_base_url", ""),
        base_url_field=base_field, base_url_fields=tuple(base_fields),
        guard_provider=raw.get("guard_provider", "") or raw["key"],
        operator_base_url_env=tuple(str(v) for v in raw.get("operator_base_url_env", [])),
        key_refs=bool(raw.get("key_refs", False)),
        status_mapping_defaults=dict(mapping), webhook=dict(hook),
        auth_kinds=tuple(dict(k) for k in kinds), folder=where)


def implemented_capabilities(adapter_cls: type) -> dict[str, bool]:
    out = {}
    for cap, hooks in CAPABILITIES.items():
        if cap == "create":
            out[cap] = (implements(adapter_cls, "create_ticket")
                        and adapter_cls.create_ticket is not TicketAdapterBase.create_ticket) \
                or hasattr(adapter_cls, "create_issue")
        else:
            out[cap] = all(implements(adapter_cls, h) for h in hooks)
    return out


def check_adapter(manifest: Manifest, adapter_cls: type) -> None:
    """The adapter implements exactly what the manifest claims, and is a TicketAdapterBase."""
    where = manifest.folder or manifest.key
    if not (isinstance(adapter_cls, type) and issubclass(adapter_cls, TicketAdapterBase)):
        raise ConnectorManifestError(f"{where}: {manifest.adapter} must extend TicketAdapterBase")
    have = implemented_capabilities(adapter_cls)
    problems = []
    for cap, claimed in manifest.capabilities.items():
        if claimed and not have[cap]:
            missing = [h for h in CAPABILITIES[cap] if not implements(adapter_cls, h)]
            problems.append(f"claims '{cap}' but does not implement {missing or CAPABILITIES[cap]}")
        if not claimed and have[cap]:
            problems.append(f"implements '{cap}' but the manifest says false")
    partial = [cap for cap, hooks in CAPABILITIES.items() if cap != "create"
               and any(implements(adapter_cls, h) for h in hooks) and not have[cap]]
    for cap in partial:
        if manifest.capabilities.get(cap) is False:
            problems.append(f"implements part of '{cap}' (declare it, or remove the hooks)")
    if problems:
        raise ConnectorManifestError(f"{where}: " + "; ".join(problems))


def _register_with_guard(manifest: Manifest) -> None:
    from ...services import env_refs

    env_refs.register_provider_hosts(manifest.guard_provider, manifest.default_hosts,
                                     operator_vars=manifest.operator_base_url_env)


def load_folder(folder: Path, *, package: Optional[str] = None) -> TicketAdapterBase:
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
        name = f"_boss_ticket_connector_{folder.name}"
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
    adapter.ticket_capabilities = dict(manifest.capabilities)
    if not getattr(adapter, "provider_key", ""):
        adapter.provider_key = manifest.key
    if adapter.provider_key != manifest.key:
        raise ConnectorManifestError(
            f"{folder.name}: adapter provider_key '{adapter.provider_key}' != '{manifest.key}'")
    if not getattr(adapter, "display_name", ""):
        adapter.display_name = manifest.label
    _check_legacy_fields(manifest, adapter)
    _register_with_guard(manifest)
    return adapter


def _check_legacy_fields(manifest: Manifest, adapter: Any) -> None:
    """When an adapter keeps its own catalog entry (the five existing ones), its connection
    fields and the manifest's auth fields must say the same thing."""
    entry = adapter.catalog_entry()
    theirs = [(f.key, bool(f.secret)) for f in entry.connection_fields]
    ours = [(f["name"], bool(f.get("secret", False))) for f in manifest.auth_fields]
    if theirs != ours:
        raise ConnectorManifestError(
            f"{manifest.key}: manifest auth_fields {ours} disagree with the adapter's "
            f"connection fields {theirs}")


class TicketRegistry:
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
                found[adapter.provider_key] = adapter
            ordered = sorted(found.values(), key=lambda a: (a.manifest.order, a.provider_key))
            self._adapters = {a.provider_key: a for a in ordered}
            self._loaded = True

    def add(self, adapter: Any) -> None:
        """Register an extra connector (a test's fake, a plug-in folder outside the package)."""
        self.load()
        self._adapters[adapter.provider_key] = adapter

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


REGISTRY = TicketRegistry()


def get_ticket_adapter(provider_key: str) -> Any:
    adapter = REGISTRY.get(provider_key)
    if adapter is None:
        raise ValueError(f"Unknown ticket system '{provider_key}'. "
                         f"Supported: {', '.join(REGISTRY.adapters)}")
    return adapter
