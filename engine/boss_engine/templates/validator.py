"""A template's whole tree, validated without running any of it (F2 §2.5 step 2, §2.9 S2.5).

The same checks for a built-in (`templates/apps/<id>/`, read from disk) and an import (read from
git objects by `importer.py`, never checked out). A tree is a list of entries; an entry that is
not a plain file (a symlink, a submodule) is recorded as such and refused, never followed.

    tree = tree_from_dir(Path("templates/apps/hello"))
    report = validate_tree(tree, source="builtin")
    report.ok, report.reasons, report.manifest, report.digest

`reasons` lists every problem at once, one line each, so the review page shows them together.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal, Optional

from .manifest import ManifestError, TemplateManifest, validate_manifest
from .secret_scan import manifest_patterns, scan_files

MANIFEST_FILE = "boss-template.yaml"
BRIEF_FILE = "brief.md"
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 2000
MAX_FILE_BYTES = 2 * 1024 * 1024
#: The only binary files a template may carry: images and fonts (§2.5).
BINARY_ALLOWED = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
                            ".woff", ".woff2", ".ttf", ".otf", ".eot"})
#: Names a template may not ask for (as a secret or a setting): BOSS's own configuration and
#: credentials, and variables that redirect a process's network or loader. A template asking for
#: one is asking for something it should never get.
RESERVED_ENV_PREFIXES = ("BOSS_", "AWS_", "AZURE_", "GOOGLE_APPLICATION_", "GCP_", "DOCKER_",
                         "GIT_", "SSH_", "NPM_CONFIG_", "PIP_", "LD_", "DYLD_", "PYTHON", "NODE_")
RESERVED_ENV_NAMES = frozenset({
    "DATABASE_URL", "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "OPENAI_BASE_URL", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "PATH", "HOME", "SHELL",
    "USER", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "JAVA_TOOL_OPTIONS", "MAVEN_OPTS", "CI",
})


def reserved_env(name: str) -> bool:
    return name in RESERVED_ENV_NAMES or any(name.startswith(p) for p in RESERVED_ENV_PREFIXES)


LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "LICENCE.md", "COPYING")

EntryKind = Literal["file", "symlink", "submodule", "other"]


@dataclass
class TreeEntry:
    path: str
    kind: EntryKind
    size: int = 0
    #: None when not read (too big, past the total, or not a file).
    data: Optional[bytes] = field(default=None, repr=False)


@dataclass
class TemplateTree:
    entries: dict[str, TreeEntry] = field(default_factory=dict)
    #: problems found while reading (a path escaping the root, more files than allowed).
    problems: list[str] = field(default_factory=list)

    def files(self) -> dict[str, bytes]:
        return {p: e.data for p, e in self.entries.items() if e.kind == "file" and e.data is not None}

    def add(self, entry: TreeEntry) -> None:
        self.entries[entry.path] = entry


@dataclass
class ValidationReport:
    ok: bool
    reasons: list[str]
    manifest: Optional[TemplateManifest]
    digest: Optional[str]
    file_count: int
    total_bytes: int
    tree: list[dict]

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reasons": list(self.reasons), "digest": self.digest,
                "file_count": self.file_count, "total_bytes": self.total_bytes,
                "tree": self.tree}


def safe_relpath(path: str) -> Optional[str]:
    """`path` as a normalised relative POSIX path inside the tree, or None if it would escape
    (absolute, `..`, a drive, a backslash, an empty component)."""
    if not path or "\x00" in path or "\\" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return None
    parts = PurePosixPath(path).parts
    if not parts or any(p in ("..", "") for p in parts):
        return None
    return "/".join(p for p in parts if p != ".")


def tree_from_dir(root: Path, *, max_files: int = MAX_FILES) -> TemplateTree:
    """Read a template directory (a built-in, or a fixture). Symlinks are recorded, never
    followed; a top-level `.git` directory is skipped (a working copy's own metadata)."""
    tree = TemplateTree()
    root = Path(root)
    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir == Path("."):
            dirnames[:] = [d for d in dirnames if d != ".git"]
        # Python's bytecode cache is the interpreter's, written by a local test run, never part of
        # the template (git never holds it: an import reads git objects, not this directory).
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        names = sorted(dirnames + filenames)
        for name in names:
            full = Path(dirpath) / name
            rel = (rel_dir / name).as_posix()
            st = full.lstat()
            if stat.S_ISLNK(st.st_mode):
                tree.add(TreeEntry(rel, "symlink"))
                if name in dirnames:
                    dirnames.remove(name)
                continue
            if stat.S_ISDIR(st.st_mode):
                continue
            count += 1
            if count > max_files:
                tree.problems.append(f"more than {max_files} files")
                return tree
            if not stat.S_ISREG(st.st_mode):
                tree.add(TreeEntry(rel, "other"))
                continue
            data = None
            if st.st_size <= MAX_FILE_BYTES and total + st.st_size <= MAX_TOTAL_BYTES:
                data = full.read_bytes()
            total += st.st_size
            tree.add(TreeEntry(rel, "file", st.st_size, data))
    return tree


def digest_of(files: dict[str, bytes]) -> str:
    """sha256 over the sorted (path, sha256(content)) list: the pin an approval records."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode("utf-8") + b"\x00" + hashlib.sha256(files[path]).hexdigest().encode() + b"\n")
    return "sha256:" + h.hexdigest()


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _host_is_boss(host: str, boss_hosts: Iterable[str]) -> bool:
    for b in boss_hosts:
        b = (b or "").strip().lower().rstrip(".")
        if b and (host == b or host.endswith("." + b)):
            return True
    return False


def validate_tree(tree: TemplateTree, *, source: Literal["builtin", "git"] = "git",
                  builtin_ids: Iterable[str] = (), boss_hosts: Iterable[str] = (),
                  may_label_insecure: bool = False, ack_insecure: bool = False) -> ValidationReport:
    """Every check of §2.5 step 2 that needs the tree. Nothing in the tree is executed."""
    reasons: list[str] = list(tree.problems)
    total = sum(e.size for e in tree.entries.values() if e.kind == "file")
    files = tree.files()
    listing = [{"path": p, "kind": e.kind, "size": e.size} for p, e in sorted(tree.entries.items())]

    # ---- shape: limits, links, escapes, git metadata ------------------------------------------
    if len([e for e in tree.entries.values() if e.kind == "file"]) > MAX_FILES:
        reasons.append(f"more than {MAX_FILES} files")
    if total > MAX_TOTAL_BYTES:
        reasons.append(f"larger than {MAX_TOTAL_BYTES // (1024 * 1024)} MB in total")
    for path, e in sorted(tree.entries.items()):
        if safe_relpath(path) != path:
            reasons.append(f"{path!r}: path escapes the template root")
            continue
        parts = [p.lower() for p in PurePosixPath(path).parts]
        if ".git" in parts:
            reasons.append(f"{path}: git metadata (.git) is not template content (no hooks, no config)")
        if path == ".gitmodules":
            reasons.append(".gitmodules: submodules are not allowed")
        if e.kind == "symlink":
            reasons.append(f"{path}: symlinks are not allowed")
        elif e.kind == "submodule":
            reasons.append(f"{path}: submodules are not allowed")
        elif e.kind == "other":
            reasons.append(f"{path}: not a regular file")
        elif e.size > MAX_FILE_BYTES:
            reasons.append(f"{path}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        elif e.data is not None and _is_binary(e.data) \
                and PurePosixPath(path).suffix.lower() not in BINARY_ALLOWED:
            reasons.append(f"{path}: a binary file (only images and fonts may be binary)")

    # ---- the manifest -------------------------------------------------------------------------
    manifest: Optional[TemplateManifest] = None
    raw = files.get(MANIFEST_FILE)
    if raw is None:
        reasons.append(f"{MANIFEST_FILE}: missing")
    else:
        try:
            manifest = validate_manifest(raw.decode("utf-8", errors="replace"))
        except ManifestError as exc:
            reasons.extend(f"{MANIFEST_FILE}: {r}" for r in exc.reasons)

    if manifest is not None:
        if source == "git" and manifest.id in set(builtin_ids):
            reasons.append(f"id: {manifest.id!r} is a built-in template's id")
        for stack in manifest.stacks:
            base = f"stacks/{stack}/"
            if not any(p.startswith(base) for p in files):
                reasons.append(f"stacks.{stack}: {base} is missing or empty")
            if not any(p.startswith(base + "tests/") for p in files):
                reasons.append(f"tests.{stack}: {base}tests/ is missing or empty")
        for label, name in (("brief", BRIEF_FILE), ("guide", manifest.guide),
                            ("security_note", manifest.security_note)):
            if name not in files:
                reasons.append(f"{label}: {name} is missing")
        for kind, items in (("secrets", manifest.secrets), ("env", manifest.env)):
            for item in items:
                if reserved_env(item.name):
                    reasons.append(f"{kind}: {item.name} is reserved (BOSS's own configuration or "
                                   "a variable that redirects the network or the loader)")
        for host in manifest.egress:
            if _host_is_boss(host, boss_hosts):
                reasons.append(f"egress: {host!r} is one of this BOSS install's own hosts")
        if manifest.insecure_by_design and source == "git":
            if not may_label_insecure:
                reasons.append("labels: insecure_by_design needs an importer holding template:approve")
            elif not ack_insecure:
                reasons.append("labels: insecure_by_design needs the importer's acknowledgement "
                               "(acknowledge_insecure: true)")
    if not any(name in files for name in LICENSE_FILES):
        reasons.append("license: no LICENSE file at the template root")

    # ---- the secret scan ----------------------------------------------------------------------
    declared = manifest_patterns(manifest) if manifest is not None else []
    for finding in scan_files(files, declared=declared):
        reasons.append(finding.reason())

    ok = not reasons
    return ValidationReport(ok=ok, reasons=reasons, manifest=manifest,
                            digest=digest_of(files) if files else None,
                            file_count=len(files), total_bytes=total, tree=listing)
