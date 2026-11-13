"""Installed extensions, the trust gate, install / enable / disable / uninstall.

INSTALL (an app-store consent, two steps):

    preview(path)    reads the bundle (a directory, or a .zip / .tar.gz) and answers its manifest,
                     the capabilities it asks for in plain words, and the checksum BOSS computed.
                     Nothing is written.
    install(path, checksum, accept)
                     the operator's checksum must equal the one BOSS computes AND the one the
                     manifest declares, and `accept` must name exactly the capabilities the
                     manifest asks for. The bundle is copied into BOSS's own extensions directory
                     (so editing the source afterwards changes nothing) and the copy's whole-tree
                     checksum is recorded as the APPROVED checksum.

THE TRUST GATE (`verdict`), evaluated on every load — every broker call, every UI frame, every
webhook — is the boss.plugin/v1 rule (platform/registry.py: a changed tree is disabled until an
operator approves it again): the installed copy must still hash to the approved checksum, its
manifest must still validate and still name the installed id and version, and the bundle must
still match the checksum its manifest declares. Any problem refuses the call and is shown.

ENABLE is per project. Enabling an extension that registers tools registers ONE MCP server row in
that project (mcp_registry.register_mcp_server, the public seam) pointing at BOSS's own extension
MCP endpoint (/v1/ext/{id}/projects/{project}/mcp), signed with a per-install bearer token. From
there the extension's tools are MCP tools like any other: offered by the agent loop, gated by
tool_policy, and called only through call_tool_with_receipt (receipt + tool_call_audit row + the
injection screen). A new version of an installed extension starts disabled everywhere: its
capabilities may have changed, so it is consented to again.
"""
from __future__ import annotations

import json
import secrets
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text

from ..platform.registry import tree_checksum
from .contract import (
    MANIFEST_FILE, ExtensionContractError, ExtensionManifest, bundle_checksum, load_manifest,
)

#: An archive bigger than this is refused before it is unpacked.
MAX_BUNDLE_BYTES = 20 * 1024 * 1024


class ExtensionRefused(ValueError):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        self.code, self.status = code, status
        super().__init__(message)


@dataclass
class InstalledExtension:
    extension_id: str
    version: str
    name: str
    directory: Path
    approved_checksum: str
    bundle_checksum: str
    nonce: str
    manifest_json: dict[str, Any]
    installed_by: Optional[str] = None
    installed_at: Any = None
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    _manifest: Optional[ExtensionManifest] = None
    _problems: Optional[list[str]] = None

    # ---- the trust gate -------------------------------------------------------------------
    def verdict(self) -> dict[str, Any]:
        if self._problems is None:
            problems: list[str] = []
            manifest: Optional[ExtensionManifest] = None
            if not self.directory.is_dir():
                problems.append(f"its files are gone from {self.directory}")
            else:
                actual = tree_checksum(self.directory)
                if actual != self.approved_checksum:
                    problems.append("its files changed after it was installed; install it again "
                                    "to approve them")
                try:
                    manifest = load_manifest(self.directory)
                except ExtensionContractError as exc:
                    problems.extend(exc.reasons)
                if manifest is not None:
                    if (manifest.id, manifest.version) != (self.extension_id, self.version):
                        problems.append(f"its manifest now says {manifest.id}@{manifest.version}, "
                                        f"not {self.extension_id}@{self.version}")
                    if bundle_checksum(self.directory) != manifest.spec.checksum:
                        problems.append("its files do not match the checksum its manifest declares")
            self._manifest, self._problems = manifest, problems
        return {"trusted": not self._problems, "problems": list(self._problems),
                "approvedChecksum": self.approved_checksum, "bundleChecksum": self.bundle_checksum}

    @property
    def trusted(self) -> bool:
        return bool(self.verdict()["trusted"])

    @property
    def manifest(self) -> ExtensionManifest:
        """The manifest, only once the gate passed. Every capability path reads it here."""
        v = self.verdict()
        if not v["trusted"] or self._manifest is None:
            raise ExtensionRefused("extension_untrusted",
                                   f"{self.extension_id} cannot run: {'; '.join(v['problems'])}", 409)
        return self._manifest

    def enabled_in(self, project_id: Optional[str]) -> bool:
        return bool(project_id) and bool((self.projects.get(str(project_id)) or {}).get("enabled"))

    @property
    def enabled_anywhere(self) -> bool:
        return any(p.get("enabled") for p in self.projects.values())

    def view(self, project_id: Optional[str] = None) -> dict[str, Any]:
        m = self.manifest_json or {}
        meta, spec = m.get("metadata") or {}, m.get("spec") or {}
        caps = list(spec.get("capabilities") or [])
        from .contract import CAPABILITY_WORDS

        out = {
            "id": self.extension_id, "version": self.version, "name": self.name,
            "description": meta.get("description", ""), "publisher": meta.get("publisher", ""),
            "capabilities": [{"capability": c, "words": CAPABILITY_WORDS.get(c, c)} for c in caps],
            "tools": [{"name": t.get("name"), "description": t.get("description"),
                       "riskTier": t.get("riskTier", 1)} for t in spec.get("tools") or []],
            "events": (spec.get("events") or {}).get("kinds", []),
            "jobs": [{"name": j.get("name"), "schedule": j.get("schedule")} for j in spec.get("jobs") or []],
            "webhooks": [{"name": w.get("name"), "description": w.get("description", ""),
                          "url": f"/v1/ext/{self.extension_id}/webhook/{w.get('name')}"}
                         for w in spec.get("webhooks") or []],
            "slots": [{"type": s.get("type"), "name": s.get("name"), "title": s.get("title")}
                      for s in ((spec.get("ui") or {}).get("slots") or [])],
            "trust": self.verdict(),
            "installedBy": self.installed_by,
            "installedAt": self.installed_at.isoformat() if hasattr(self.installed_at, "isoformat")
            else self.installed_at,
            "enabledProjects": sorted(p for p, row in self.projects.items() if row.get("enabled")),
        }
        if project_id is not None:
            out["enabled"] = self.enabled_in(project_id)
        return out


# ------------------------------------------------------------------------------------------ #
# Where installs live
# ------------------------------------------------------------------------------------------ #

def extensions_root(settings: Any = None) -> Path:
    configured = str(getattr(settings, "extensions_dir", "") or "").strip()
    if configured:
        return Path(configured)
    artifact_root = getattr(settings, "artifact_root", None)
    if artifact_root is not None:
        return Path(artifact_root) / "extensions"
    return Path(tempfile.gettempdir()) / "boss-extensions"


def _unpack(source: Path, into: Path) -> Path:
    """A directory as it is; an archive unpacked (refusing absolute paths, `..` and links).
    Returns the directory that holds boss-extension.yaml."""
    if source.is_dir():
        return source
    if not source.is_file():
        raise ExtensionRefused("bundle_missing", f"nothing at {source}", 404)
    if source.stat().st_size > MAX_BUNDLE_BYTES:
        raise ExtensionRefused("bundle_too_large", f"a bundle is at most {MAX_BUNDLE_BYTES} bytes")
    into.mkdir(parents=True, exist_ok=True)

    def safe(name: str) -> None:
        p = Path(name)
        if p.is_absolute() or ".." in p.parts:
            raise ExtensionRefused("bundle_unsafe", f"the archive names a path outside itself: {name}")

    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as zf:
            for info in zf.infolist():
                safe(info.filename)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ExtensionRefused("bundle_unsafe", f"the archive holds a link: {info.filename}")
            zf.extractall(into)
    elif tarfile.is_tarfile(source):
        with tarfile.open(source) as tf:
            for member in tf.getmembers():
                safe(member.name)
                if not (member.isfile() or member.isdir()):
                    raise ExtensionRefused("bundle_unsafe", f"the archive holds a link or device: {member.name}")
            tf.extractall(into)  # noqa: S202 — every member checked above (3.11 has no filter=)
    else:
        raise ExtensionRefused("bundle_unknown", "a bundle is a directory, a .zip or a .tar.gz")
    hits = sorted(into.rglob(MANIFEST_FILE), key=lambda p: len(p.parts))
    if not hits:
        raise ExtensionRefused("manifest_missing", f"the bundle has no {MANIFEST_FILE}")
    return hits[0].parent


def _read_bundle(source: str, scratch: Path) -> tuple[Path, ExtensionManifest, str]:
    directory = _unpack(Path(source).expanduser(), scratch)
    try:
        manifest = load_manifest(directory)
    except ExtensionContractError as exc:
        raise ExtensionRefused("manifest_invalid", str(exc)) from None
    return directory, manifest, bundle_checksum(directory)


def preview(source: str) -> dict[str, Any]:
    """What the install screen shows before anyone accepts. Writes nothing."""
    with tempfile.TemporaryDirectory(prefix="boss-ext-preview-") as tmp:
        directory, manifest, computed = _read_bundle(source, Path(tmp))
        problems = [] if computed == manifest.spec.checksum else [
            "the bundle's files do not match the checksum its manifest declares"]
        return {"id": manifest.id, "version": manifest.version, "name": manifest.metadata.name,
                "description": manifest.metadata.description,
                "publisher": manifest.metadata.publisher,
                "capabilities": manifest.consent(), "computedChecksum": computed,
                "declaredChecksum": manifest.spec.checksum,
                "trust": {"trusted": not problems, "problems": problems}}


def install(connection: Any, settings: Any, source: str, *, checksum: str,
            accept: list[str], actor: Optional[str]) -> InstalledExtension:
    with tempfile.TemporaryDirectory(prefix="boss-ext-install-") as tmp:
        directory, manifest, computed = _read_bundle(source, Path(tmp))
        if computed != manifest.spec.checksum:
            raise ExtensionRefused("checksum_mismatch",
                                   "the bundle's files do not match the checksum its manifest "
                                   f"declares ({manifest.spec.checksum}); BOSS computed {computed}", 409)
        if str(checksum or "").strip() != computed:
            raise ExtensionRefused("checksum_mismatch",
                                   "the checksum you gave is not the bundle's; check you have the "
                                   f"bundle the publisher signed off (BOSS computed {computed})", 409)
        if sorted(set(accept or [])) != sorted(manifest.spec.capabilities):
            raise ExtensionRefused("consent_required",
                                   "accept exactly the capabilities the extension asks for: "
                                   + ", ".join(manifest.spec.capabilities), 409)
        target = extensions_root(settings) / manifest.id / manifest.version
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(directory, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    approved = tree_checksum(target)
    previous = connection.execute(text(
        "SELECT version FROM extension_installs WHERE extension_id = :e"), {"e": manifest.id}).first()
    nonce = secrets.token_hex(16)
    connection.execute(text(
        "INSERT INTO extension_installs (extension_id, version, name, directory, approved_checksum, "
        "bundle_checksum, manifest, nonce, installed_by, installed_at, updated_at) "
        "VALUES (:e, :v, :n, :d, :ac, :bc, CAST(:m AS JSONB), :nonce, :by, now(), now()) "
        "ON CONFLICT (extension_id) DO UPDATE SET version = EXCLUDED.version, name = EXCLUDED.name, "
        "directory = EXCLUDED.directory, approved_checksum = EXCLUDED.approved_checksum, "
        "bundle_checksum = EXCLUDED.bundle_checksum, manifest = EXCLUDED.manifest, "
        "nonce = EXCLUDED.nonce, installed_by = EXCLUDED.installed_by, installed_at = now(), "
        "updated_at = now()"),
        {"e": manifest.id, "v": manifest.version, "n": manifest.metadata.name, "d": str(target),
         "ac": approved, "bc": computed, "m": json.dumps(manifest.model_dump(mode="json")),
         "nonce": nonce, "by": actor})
    if previous is not None:
        # A NEW VERSION IS CONSENTED TO AGAIN: off everywhere until a person enables it.
        for row in connection.execute(text(
                "SELECT project_id FROM extension_projects WHERE extension_id = :e AND enabled"),
                {"e": manifest.id}).all():
            _set_project(connection, settings, load(connection, manifest.id), row[0],
                         enabled=False, actor=actor or "system")
    found = load(connection, manifest.id)
    assert found is not None
    return found


# ------------------------------------------------------------------------------------------ #
# Reading installs
# ------------------------------------------------------------------------------------------ #

_COLUMNS = ("extension_id, version, name, directory, approved_checksum, bundle_checksum, nonce, "
            "manifest, installed_by, installed_at")


def _from_row(row: Any, projects: dict[str, dict[str, Any]]) -> InstalledExtension:
    manifest = row["manifest"]
    return InstalledExtension(
        extension_id=row["extension_id"], version=row["version"], name=row["name"],
        directory=Path(row["directory"]), approved_checksum=row["approved_checksum"],
        bundle_checksum=row["bundle_checksum"], nonce=row["nonce"],
        manifest_json=json.loads(manifest) if isinstance(manifest, str) else (manifest or {}),
        installed_by=row["installed_by"], installed_at=row["installed_at"], projects=projects)


def _projects(connection: Any, extension_id: Optional[str] = None) -> dict[str, dict[str, dict[str, Any]]]:
    rows = connection.execute(text(
        "SELECT extension_id, project_id, enabled, mcp_server_id, updated_by, updated_at "
        "FROM extension_projects" + (" WHERE extension_id = :e" if extension_id else "")),
        {"e": extension_id} if extension_id else {}).mappings().all()
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["extension_id"], {})[r["project_id"]] = dict(r)
    return out


def load(connection: Any, extension_id: str) -> Optional[InstalledExtension]:
    row = connection.execute(text(f"SELECT {_COLUMNS} FROM extension_installs WHERE extension_id = :e"),
                             {"e": extension_id}).mappings().first()
    if row is None:
        return None
    return _from_row(row, _projects(connection, extension_id).get(extension_id, {}))


def list_installed(connection: Any) -> list[InstalledExtension]:
    rows = connection.execute(text(f"SELECT {_COLUMNS} FROM extension_installs ORDER BY extension_id")
                              ).mappings().all()
    projects = _projects(connection)
    return [_from_row(r, projects.get(r["extension_id"], {})) for r in rows]


# ------------------------------------------------------------------------------------------ #
# Enable / disable per project; uninstall
# ------------------------------------------------------------------------------------------ #

def mcp_token(ext: InstalledExtension, project_id: str) -> str:
    """The bearer the extension's MCP server row carries, derived (never stored in BOSS's own
    tables) from the server key, the install's nonce and the project, so a reinstall rotates it."""
    from ..identity.authn_support import keyed_hash

    return keyed_hash("boss.ext.mcp", f"{ext.extension_id}:{project_id}:{ext.nonce}")


def api_base_url(settings: Any) -> str:
    import os

    return (str(getattr(settings, "extensions_api_url", "") or "").strip()
            or os.environ.get("BOSS_CONTROL_PLANE_URL", "").strip()
            or "http://127.0.0.1:8000").rstrip("/")


def mcp_url(settings: Any, extension_id: str, project_id: str) -> str:
    return f"{api_base_url(settings)}/v1/ext/{extension_id}/projects/{project_id}/mcp"


def _set_project(connection: Any, settings: Any, ext: Optional[InstalledExtension], project_id: str,
                 *, enabled: bool, actor: str) -> dict[str, Any]:
    from ..services import mcp_registry
    from ..services.core import emit_event

    if ext is None:
        raise ExtensionRefused("extension_unknown", "no such extension is installed", 404)
    current = ext.projects.get(project_id) or {}
    server_id = current.get("mcp_server_id")
    if enabled:
        manifest = ext.manifest   # THE TRUST GATE: refuses an untrusted install here
        if manifest.has("tools.register"):
            auth = {"type": "bearer", "secret": mcp_token(ext, project_id)}
            url = mcp_url(settings, ext.extension_id, project_id)
            provenance = {"source": "extension", "extension_id": ext.extension_id,
                          "version": ext.version, "trusted": True, "reviewed_by": actor}
            # EACH TOOL'S DECLARED TIER IS ITS TIER from the first call: the registry's stored
            # tool summary keeps no annotations, so the backstop (call_tool_with_receipt) reads
            # the tier from tool_risk_overrides. riskTier 2+ is therefore Ask at the backstop too.
            tiers = {t.name: int(t.riskTier) for t in manifest.spec.tools}
            existing = mcp_registry.get_mcp_server(connection, project_id, server_id) if server_id else None
            if existing is None:
                server = mcp_registry.register_mcp_server(
                    connection, project_id, f"Extension: {ext.name}", url, transport="http",
                    risk_tier=1, description=f"Tools of the {ext.extension_id} extension "
                    f"(boss.extension/v1, {ext.version})",
                    publisher={"name": manifest.metadata.publisher or ext.extension_id},
                    provenance=provenance, actor=actor, enabled=True, auth=auth, settings=settings,
                    tool_risk_overrides=tiers)
                server_id = server["id"]
            else:
                changes: dict[str, Any] = {"url": url, "enabled": True, "auth": auth}
                if (existing.get("provenance") or {}).get("version") != ext.version:
                    # A NEW VERSION brings its own declared tiers; within a version the
                    # operator's own per-tool tier edits stand.
                    changes["tool_risk_overrides"] = tiers
                mcp_registry.update_mcp_server(connection, project_id, server_id, actor=actor,
                                               settings=settings, **changes)
                mcp_registry.review_mcp_server(connection, project_id, server_id, actor=actor)
                _stamp_version(connection, project_id, server_id, ext.version)
            # THE CONSENTED TOOL LIST IS THE FIRST PROBE: stored now, so the registry knows the
            # tools before any agent turn probes them, and a later version whose tool changed is
            # quarantined to Ask like any MCP tool's (BOSSAPP-818).
            from .tools import tool_list

            mcp_registry.record_reachability(connection, project_id, server_id, reachable=True,
                                             tools=tool_list(ext))
    elif server_id and mcp_registry.get_mcp_server(connection, project_id, server_id) is not None:
        # Off means off for its tools too: the server row stays (its policy choices are the
        # operator's) but nothing offers or calls it.
        mcp_registry.set_mcp_enabled(connection, project_id, server_id, False, actor)
    connection.execute(text(
        "INSERT INTO extension_projects (extension_id, project_id, enabled, mcp_server_id, updated_by, "
        "updated_at) VALUES (:e, :p, :en, :s, :by, now()) ON CONFLICT (extension_id, project_id) "
        "DO UPDATE SET enabled = EXCLUDED.enabled, mcp_server_id = EXCLUDED.mcp_server_id, "
        "updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"e": ext.extension_id, "p": project_id, "en": enabled, "s": server_id, "by": actor})
    emit_event(connection, project_id, "extension.enabled" if enabled else "extension.disabled",
               actor, "extension", ext.extension_id,
               {"version": ext.version, "checksum": ext.approved_checksum, "mcp_server_id": server_id})
    found = load(connection, ext.extension_id)
    assert found is not None
    return found.view(project_id)


def _stamp_version(connection: Any, project_id: str, server_id: str, version: str) -> None:
    connection.execute(text(
        "UPDATE mcp_servers SET provenance = jsonb_set(provenance, '{version}', to_jsonb(CAST(:v AS TEXT))) "
        "WHERE id = :id AND project_id = :p"), {"v": version, "id": server_id, "p": project_id})


def set_enabled(connection: Any, settings: Any, extension_id: str, project_id: str, *,
                enabled: bool, actor: str) -> dict[str, Any]:
    return _set_project(connection, settings, load(connection, extension_id), project_id,
                        enabled=enabled, actor=actor)


def uninstall(connection: Any, settings: Any, extension_id: str, *, actor: str,
              purge_state: bool = True) -> None:
    from ..services import mcp_registry

    ext = load(connection, extension_id)
    if ext is None:
        raise ExtensionRefused("extension_unknown", "no such extension is installed", 404)
    for project_id, row in ext.projects.items():
        if row.get("mcp_server_id"):
            try:
                with connection.begin_nested():
                    mcp_registry.delete_mcp_server(connection, project_id, row["mcp_server_id"],
                                                   actor, release_grants=True)
            except mcp_registry.McpServerError:
                # Something still points at it (a custom step): leave it, switched off.
                mcp_registry.set_mcp_enabled(connection, project_id, row["mcp_server_id"], False, actor)
    for table in ("extension_projects", "extension_event_cursors", "extension_job_leases",
                  "extension_installs") + (("extension_state",) if purge_state else ()):
        connection.execute(text(f"DELETE FROM {table} WHERE extension_id = :e"), {"e": extension_id})
    shutil.rmtree(ext.directory, ignore_errors=True)
