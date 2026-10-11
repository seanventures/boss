"""The plugin registry: discovery, trust, version pinning, checksums, enable/disable, search.

Sources (spec §4.1, §7), each a directory under the repository's `plugins/`:

    plugins/official/<name>/    builtin    reviewed with BOSS itself; enabled by default
    plugins/verified/<name>/    verified   third-party; MUST match the version and checksum pinned
                                           in plugins/registry.lock.yaml, or it is refused
    plugins/community/<name>/   community  unverified; disabled until an operator enables it, and
                                           disabled again if its files change after that

Extra community directories come from BOSS_PLUGIN_PATHS (colon-separated). A plugin's checksum
is the sha256 of its directory tree (paths + bytes, caches excluded): what a lock pins, what an
enable approves, and what every receipt records.

Support level is what the manifest claims, clamped by source: only a builtin or verified plugin
may claim `certified` or `experimental`; a community plugin is `community` whatever it says.

Enable/disable and an operator's version pin are install-wide rows in `infra_plugin_state`;
this module's pure part (`discover`, `search`, `resolve_version`) needs no database.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .contract import (
    MANIFEST_FILE, SEMVER, ContractError, Hook, InfraPlugin, PluginManifest, PluginSource,
    SupportLevel, load_manifest,
)

PLUGINS_DIR_ENV = "BOSS_PLUGINS_DIR"
EXTRA_PATHS_ENV = "BOSS_PLUGIN_PATHS"
LOCK_FILE = "registry.lock.yaml"

SOURCE_DIRS: dict[str, PluginSource] = {
    "official": PluginSource.BUILTIN,
    "verified": PluginSource.VERIFIED,
    "community": PluginSource.COMMUNITY,
}
_IGNORED = {"__pycache__", ".pytest_cache", ".DS_Store", ".mypy_cache", ".ruff_cache"}


def plugins_root() -> Path:
    configured = os.environ.get(PLUGINS_DIR_ENV)
    if configured:
        return Path(configured)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "plugins"
        if candidate.is_dir() and any((candidate / d).is_dir() for d in SOURCE_DIRS):
            return candidate
    return here.parents[3] / "plugins"


def tree_checksum(directory: Path) -> str:
    """sha256 over every file's relative path and bytes, in sorted order."""
    h = hashlib.sha256()
    for p in sorted(directory.rglob("*")):
        rel = p.relative_to(directory)
        if any(part in _IGNORED for part in rel.parts) or p.suffix == ".pyc" or not p.is_file():
            continue
        h.update(rel.as_posix().encode() + b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------------------------------------
# semver
# ---------------------------------------------------------------------------------------------

def parse_version(v: str) -> tuple:
    m = re.fullmatch(SEMVER, v)
    if not m:
        raise ValueError(f"{v!r} is not a semantic version")
    pre = m.group(4)
    # A release sorts after its pre-releases.
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), (1,) if pre is None else (0, pre))


def version_matches(version: str, pin: Optional[str]) -> bool:
    """`1.2.3` exact, `^1.2` same major (>=), `~1.2` same minor (>=), `*`/None anything."""
    if not pin or pin == "*":
        return True
    v = parse_version(version)
    if pin[0] in "^~":
        parts = pin[1:].split(".")
        base = tuple(int(x) for x in (parts + ["0", "0"])[:3])
        if v[:3] < base:
            return False
        return v[0] == base[0] if pin[0] == "^" else v[:2] == base[:2]
    return v == parse_version(pin)


# ---------------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------------

@dataclass
class RegisteredPlugin:
    manifest: Optional[PluginManifest]
    source: PluginSource
    directory: Path
    checksum: str
    problems: list[str] = field(default_factory=list)
    enabled: bool = False
    pinned_version: Optional[str] = None
    approved_checksum: Optional[str] = None

    @property
    def id(self) -> str:
        return self.manifest.id if self.manifest else f"invalid:{self.directory.name}"

    @property
    def version(self) -> str:
        return self.manifest.version if self.manifest else "0.0.0"

    @property
    def support_level(self) -> SupportLevel:
        claimed = self.manifest.spec.supportLevel if self.manifest else SupportLevel.COMMUNITY
        if self.source == PluginSource.COMMUNITY:
            return SupportLevel.COMMUNITY
        return claimed

    @property
    def trusted(self) -> bool:
        """Loadable at all: a valid manifest and no trust problem."""
        return self.manifest is not None and not self.problems

    @property
    def usable(self) -> bool:
        return self.trusted and self.enabled

    def view(self) -> dict[str, Any]:
        m = self.manifest
        return {
            "id": self.id, "version": self.version, "name": m.metadata.name if m else self.directory.name,
            "description": m.metadata.description if m else "",
            "owner": m.metadata.owner if m else "", "license": m.metadata.license if m else "",
            "source": self.source.value, "supportLevel": self.support_level.value,
            "claimedSupportLevel": m.spec.supportLevel.value if m else None,
            "capabilities": list(m.spec.capabilities) if m else [],
            "features": list(m.spec.features) if m else [],
            "engines": list(m.spec.engines) if m else [],
            "hooks": [h.value for h in m.spec.hooks] if m else [],
            "bindings": list(m.spec.bindings) if m else [],
            "permissions": m.spec.permissions.model_dump(mode="json") if m else {},
            "runtime": m.spec.runtime.type if m else None,
            "checksum": self.checksum, "enabled": self.enabled, "usable": self.usable,
            "pinnedVersion": self.pinned_version, "problems": list(self.problems),
            "limitations": list(m.spec.limitations) if m else [],
        }


def _lock(root: Path) -> dict[str, dict[str, str]]:
    p = root / LOCK_FILE
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, str]] = {}
    for entry in data.get("plugins") or []:
        if isinstance(entry, dict) and entry.get("id"):
            out[f"{entry['id']}@{entry.get('version', '')}"] = entry
    return out


def _candidates(root: Path) -> Iterable[tuple[PluginSource, Path]]:
    for dirname, source in SOURCE_DIRS.items():
        base = root / dirname
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if (d / MANIFEST_FILE).is_file():
                    yield source, d
    for extra in filter(None, os.environ.get(EXTRA_PATHS_ENV, "").split(":")):
        base = Path(extra)
        dirs = [base] if (base / MANIFEST_FILE).is_file() else sorted(base.glob("*/"))
        for d in dirs:
            if (d / MANIFEST_FILE).is_file():
                yield PluginSource.COMMUNITY, d


def discover(root: Optional[Path] = None) -> list[RegisteredPlugin]:
    """Every plugin on disk, valid or not, with its trust problems. No database."""
    root = root or plugins_root()
    lock = _lock(root)
    out: list[RegisteredPlugin] = []
    seen: dict[str, Path] = {}
    for source, d in _candidates(root):
        checksum = tree_checksum(d)
        try:
            manifest: Optional[PluginManifest] = load_manifest(d)
            problems: list[str] = []
        except ContractError as exc:
            manifest, problems = None, list(exc.reasons)
        reg = RegisteredPlugin(manifest, source, d, checksum, problems,
                               enabled=source in (PluginSource.BUILTIN, PluginSource.VERIFIED))
        if manifest is not None:
            key = f"{manifest.id}@{manifest.version}"
            if key in seen:
                reg.problems.append(f"{key} is also at {seen[key]}; ids and versions are unique")
            seen[key] = d
            ns = manifest.id.split(".", 1)[0]
            if source == PluginSource.BUILTIN and ns != "official":
                reg.problems.append("a plugin under plugins/official must have an id starting official.")
            if source != PluginSource.BUILTIN and ns == "official":
                reg.problems.append("the official. namespace is reserved for plugins shipped with BOSS")
            if source == PluginSource.VERIFIED:
                pin = lock.get(key)
                if pin is None:
                    reg.problems.append(f"{key} is not pinned in plugins/{LOCK_FILE}")
                elif pin.get("checksum") != checksum:
                    reg.problems.append(f"{key} does not match its pinned checksum; its files changed")
        out.append(reg)
    return out


# ---------------------------------------------------------------------------------------------
# State: enable/disable and operator pins (install-wide)
# ---------------------------------------------------------------------------------------------

def _state_rows(connection: Any) -> dict[str, dict[str, Any]]:
    from sqlalchemy import text
    rows = connection.execute(text(
        "SELECT plugin_id, enabled, pinned_version, approved_checksum FROM infra_plugin_state")).mappings().all()
    return {r["plugin_id"]: dict(r) for r in rows}


def apply_state(plugins: list[RegisteredPlugin], state: dict[str, dict[str, Any]]) -> list[RegisteredPlugin]:
    """Overlay operator state. A community plugin is enabled only while its checksum is the one
    the operator approved; a changed tree is disabled again with a problem saying why."""
    for p in plugins:
        row = state.get(p.id)
        if row is None:
            continue
        p.pinned_version = row.get("pinned_version")
        p.approved_checksum = row.get("approved_checksum")
        want = bool(row.get("enabled"))
        if want and p.source == PluginSource.COMMUNITY and p.approved_checksum != p.checksum:
            p.enabled = False
            p.problems.append("its files changed after it was enabled; enable it again to approve them")
        else:
            p.enabled = want
    return plugins


class RegistryError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        self.code, self.status = code, status
        super().__init__(message)


class Registry:
    """The registry as BOSS sees it: discovery plus (optionally) the database's state."""

    def __init__(self, plugins: list[RegisteredPlugin]) -> None:
        self.plugins = plugins

    @classmethod
    def load(cls, connection: Any = None, root: Optional[Path] = None) -> "Registry":
        plugins = discover(root)
        if connection is not None:
            apply_state(plugins, _state_rows(connection))
        return cls(plugins)

    def get(self, plugin_id: str, version: Optional[str] = None) -> Optional[RegisteredPlugin]:
        matches = [p for p in self.plugins if p.id == plugin_id and (version is None or p.version == version)]
        return max(matches, key=lambda p: parse_version(p.version), default=None)

    def search(self, capability: Optional[str] = None, features: Iterable[str] = (),
               engine: Optional[str] = None, include_disabled: bool = False,
               support_level: Optional[SupportLevel] = None) -> list[RegisteredPlugin]:
        """By capability (exact, or a parent: `database` finds `database.relational`), features
        (all required), engine and support level. Certified first, then experimental, community."""
        want = set(features)
        out = []
        for p in self.plugins:
            if p.manifest is None or (not include_disabled and not p.usable):
                continue
            caps = p.manifest.spec.capabilities
            if capability and not any(c == capability or c.startswith(capability + ".") for c in caps):
                continue
            if want - set(p.manifest.spec.features):
                continue
            if engine and p.manifest.spec.engines and engine not in p.manifest.spec.engines:
                continue
            if support_level and p.support_level != support_level:
                continue
            out.append(p)
        rank = {SupportLevel.CERTIFIED: 0, SupportLevel.EXPERIMENTAL: 1, SupportLevel.COMMUNITY: 2}
        return sorted(out, key=lambda p: (rank[p.support_level], p.id, tuple(-x if isinstance(x, int) else 0
                                                                               for x in parse_version(p.version)[:3])))

    def resolve_version(self, plugin_id: str, pin: Optional[str] = None) -> RegisteredPlugin:
        """The highest usable version matching the request's pin AND the operator's pin."""
        cands = [p for p in self.plugins if p.id == plugin_id]
        if not cands:
            raise RegistryError("plugin_unknown", f"no plugin {plugin_id} is installed", 404)
        usable = [p for p in cands if p.usable]
        if not usable:
            why = "; ".join(sum((p.problems for p in cands), [])) or "it is disabled"
            raise RegistryError("plugin_unusable", f"{plugin_id} cannot be used: {why}", 409)
        operator_pin = next((p.pinned_version for p in cands if p.pinned_version), None)
        ok = [p for p in usable if version_matches(p.version, pin) and version_matches(p.version, operator_pin)]
        if not ok:
            raise RegistryError("version_unavailable",
                                f"{plugin_id} has no usable version matching {pin or operator_pin}", 409)
        return max(ok, key=lambda p: parse_version(p.version))


def set_state(connection: Any, plugin: RegisteredPlugin, *, enabled: bool, actor_id: Optional[str],
              pinned_version: Optional[str] = None) -> dict[str, Any]:
    """Enable/disable (install-wide). Enabling records the checksum the operator approved."""
    from sqlalchemy import text
    if enabled and not plugin.trusted:
        raise RegistryError("plugin_untrusted", f"{plugin.id} cannot be enabled: {'; '.join(plugin.problems)}", 409)
    if pinned_version is not None and pinned_version != "" and not re.fullmatch(SEMVER, pinned_version.lstrip("^~")):
        raise RegistryError("bad_pin", "a version pin is a semantic version, optionally with ^ or ~")
    connection.execute(text(
        "INSERT INTO infra_plugin_state (plugin_id, enabled, pinned_version, approved_checksum, updated_by, updated_at) "
        "VALUES (:id, :en, :pin, :ck, :by, now()) ON CONFLICT (plugin_id) DO UPDATE SET enabled = :en, "
        "pinned_version = COALESCE(:pin, infra_plugin_state.pinned_version), "
        "approved_checksum = CASE WHEN :en THEN :ck ELSE infra_plugin_state.approved_checksum END, "
        "updated_by = :by, updated_at = now()"),
        {"id": plugin.id, "en": enabled, "pin": pinned_version or None, "ck": plugin.checksum, "by": actor_id})
    plugin.enabled = enabled
    return plugin.view()


# ---------------------------------------------------------------------------------------------
# Loading a plugin's hooks
# ---------------------------------------------------------------------------------------------

def load_plugin(reg: RegisteredPlugin) -> InfraPlugin:
    """The in-process implementation of a builtin python plugin. Any other source runs out of
    process (runner.SubprocessPlugin); callers go through `runner.plugin_for`."""
    if reg.manifest is None:
        raise RegistryError("plugin_invalid", f"{reg.id} has no valid manifest")
    return _load_entrypoint(str(reg.directory), reg.manifest.spec.runtime.entrypoint or "", reg.checksum,
                            reg.manifest)


def _load_entrypoint(directory: str, entrypoint: str, checksum: str, manifest: PluginManifest) -> InfraPlugin:
    """A fresh instance per call (instances are never shared); the module load is cached on the
    tree checksum, so a changed plugin is imported again."""
    instance = _plugin_class(directory, entrypoint, checksum)()
    instance.manifest = manifest
    for hook in manifest.spec.hooks:
        if getattr(type(instance), hook.value) is getattr(InfraPlugin, hook.value):
            raise RegistryError("hook_missing", f"{manifest.id} declares {hook.value} but does not implement it")
    return instance


@lru_cache(maxsize=64)
def _plugin_class(directory: str, entrypoint: str, checksum: str) -> type:
    module_name, _, cls_name = entrypoint.partition(":")
    path = Path(directory, *module_name.split("."))
    candidates = [path.with_suffix(".py"), path / "__init__.py"]
    file = next((c for c in candidates if c.is_file()), None)
    if file is None:
        raise RegistryError("entrypoint_missing", f"{entrypoint} is not a file under {directory}")
    unique = "boss_plugin_" + hashlib.sha256(f"{directory}:{checksum}".encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(f"{unique}_{module_name.replace('.', '_')}", file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cls = getattr(module, cls_name, None)
    if not (isinstance(cls, type) and issubclass(cls, InfraPlugin)):
        raise RegistryError("entrypoint_invalid", f"{entrypoint} is not an InfraPlugin subclass")
    return cls


def support_matrix(registry: Optional[Registry] = None) -> list[dict[str, Any]]:
    """One row per plugin version, for docs/community/support-matrix.md and the console."""
    registry = registry or Registry.load()
    rows = []
    for p in sorted(registry.plugins, key=lambda p: (p.source.value != "builtin", p.id, p.version)):
        if p.manifest is None:
            continue
        rows.append({
            "id": p.id, "version": p.version, "name": p.manifest.metadata.name,
            "source": p.source.value, "supportLevel": p.support_level.value,
            "capabilities": list(p.manifest.spec.capabilities),
            "engines": list(p.manifest.spec.engines), "engineVersions": list(p.manifest.spec.engineVersions),
            "hooks": [h.value for h in p.manifest.spec.hooks if h != Hook.VALIDATE],
        })
    return rows
