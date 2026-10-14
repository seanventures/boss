"""THE APP'S DIRECTORY FOR A BUILD — the one answer every gate planner asks.

A shared repository (paramg/boss-sandbox) holds many apps, and its ROOT holds one of them: a
pom.xml from an old Java demo. HelpDesk lives in `helpdesk-ai/` (one npm project). Three times a
HelpDesk build was refused "This project needs Java" because a planner looked at the repository
root instead of the app:

  1. find_stack_root fell back to the root when the changes sat in one app folder;
  2. it fell back again on an EMPTY change list (a repair seeded from a saved workspace,
     crun_03e44800);
  3. the gate plan's Java branch (verification_target.resolve_gate_plan) ran
     `toolchains.detect_java(<repo root>)` whatever find_stack_root said, and the first plan of a
     build is made with no change list at all (crun_7768e91ecf2d420c8b72: a resumed repair whose
     own diff was two files; its template gate then ran where that plan pointed).

Each fix patched one planner. This module is the class fix: the app's directory comes from the
run's IDENTITY — the named app (a pinned benchmark's app_root, the folder the brief names), then
the files this lineage wrote (the change list, the seeded workspace, the lineage's files) — and is
the repository root only when the app genuinely lives there: the lineage changed the root's own
manifest, its work spans several apps under the root, or there is no evidence of a subfolder app
at all (a single-app repository).

Every root-level stack detection goes through here: `java_build` is the only way a gate planner
asks "is this app a Java build", and engine/tests/test_app_root.py fails if a planner calls
`toolchains.detect_java` on anything else.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

#: A file that makes its directory an app (a buildable project of some stack).
APP_MANIFESTS = ("package.json", "pom.xml", "build.gradle", "build.gradle.kts",
                 "settings.gradle", "settings.gradle.kts", "requirements.txt", "pyproject.toml", "setup.py", "go.mod", "Cargo.toml")

#: Directories that are never an app of their own, wherever they sit.
_NOT_APP_DIRS = frozenset({"node_modules", ".git", ".venv", "venv", "env", "__pycache__", "dist",
                           "build", "target", ".github", "docs"})


def _rels(paths: Optional[Iterable[str]]) -> list[PurePosixPath]:
    out = []
    for p in paths or []:
        s = str(p or "").strip()
        while s.startswith("./"):
            s = s[2:]
        if not s or s.startswith("/"):
            continue
        rel = PurePosixPath(s)
        if ".." in rel.parts:
            continue
        out.append(rel)
    return out


def _has_manifest(d: Path) -> bool:
    return any((d / name).is_file() for name in APP_MANIFESTS)


def _is_module_of_root(base: Path, top: str) -> bool:
    """`top` is a module of a build at the root (a Maven <module> or a Gradle include): the root
    build is then the app, not the module."""
    try:
        pom = base / "pom.xml"
        if pom.is_file() and re.search(rf"<module>\s*(?:\./)?{re.escape(top)}(?:/[^<]*)?\s*</module>",
                                       pom.read_text(errors="ignore")):
            return True
        for name in ("settings.gradle", "settings.gradle.kts"):
            f = base / name
            if f.is_file() and re.search(rf"""["']:?{re.escape(top)}["']""",
                                         f.read_text(errors="ignore")):
                return True
    except OSError:
        return False
    return False


def _app_of(base: Path, rel: PurePosixPath) -> Optional[PurePosixPath]:
    """The app directory (below the root) a repo-relative path belongs to: its SHALLOWEST
    ancestor below the root that holds an app manifest. None for a path in no subfolder app
    (a root file, .github/, docs/)."""
    parts = rel.parts
    for depth in range(1, len(parts)):  # directories only, shallowest first
        if parts[depth - 1] in _NOT_APP_DIRS or parts[depth - 1].startswith("."):
            return None
        d = base.joinpath(*parts[:depth])
        if d.is_dir() and _has_manifest(d):
            if depth == 1 and _is_module_of_root(base, parts[0]):
                return None
            return PurePosixPath(*parts[:depth])
    return None


def _from_paths(base: Path, paths: Optional[Iterable[str]]) -> Optional[Path]:
    """What a list of files says the app is, or None when it says nothing."""
    rels = _rels(paths)
    if not rels:
        return None
    if any(len(r.parts) == 1 and r.name in APP_MANIFESTS for r in rels):
        return base  # the lineage changed the root's OWN manifest: the root is the app
    apps = {a for a in (_app_of(base, r) for r in rels) if a is not None}
    if not apps:
        return None
    if len(apps) == 1:
        return base.joinpath(*next(iter(apps)).parts)
    # Several apps: the directory that holds them all (a full-stack app's backend/ + frontend/,
    # often the root itself).
    common = list(next(iter(apps)).parts)
    for a in apps:
        n = 0
        while n < min(len(common), len(a.parts)) and common[n] == a.parts[n]:
            n += 1
        common = common[:n]
    return base.joinpath(*common) if common else base


def app_dir(root: Path | str, *, app_id: Optional[str] = None, changed: Optional[Iterable[str]] = None,
            lineage: Optional[Iterable[str]] = None) -> Path:
    """THE directory of the app this build builds, inside the checkout `root`.

    `app_id` is the named app (verification_target.app_scope: the pinned benchmark app, else the
    folder the brief names), `changed` the files this run changed (including a seeded
    workspace's), `lineage` the files the app's lineage wrote before. The root only when the app
    genuinely lives there (see the module docstring). A named app's directory is the app's even
    before the build has created it (a first build's plan is made on the base branch)."""
    base = Path(root)
    aid = str(app_id or "").strip().strip("/")
    if aid and ".." not in PurePosixPath(aid).parts and not PurePosixPath(aid).is_absolute():
        return base / aid
    for evidence in (changed, lineage):
        found = _from_paths(base, evidence)
        if found is not None:
            return found
    return base


def root_is_the_app(root: Path | str, **identity) -> bool:
    return app_dir(root, **identity) == Path(root)


def java_build(root: Path | str, **identity) -> tuple[Path, Optional[str]]:
    """(the app's directory, "maven" | "gradle" | None): is THIS APP a Java build. A pom.xml at
    the root of a shared repository never makes an app in a subfolder Java."""
    from . import toolchains

    where = app_dir(root, **identity)
    return where, toolchains.detect_java(where)


def gate_dir(root: Path | str, *, app_id: Optional[str] = None,
             changed: Optional[Iterable[str]] = None,
             lineage: Optional[Iterable[str]] = None) -> Path:
    """Where a gate runs when no verify recorded a directory (an applied template's tests, the
    web package check): the app's npm package when it has one (the deepest package.json on the
    app's paths, verification_target.resolve_verify_cwd), else the app's directory."""
    from .verification_target import resolve_verify_cwd

    base = Path(root)
    npm_dir = resolve_verify_cwd(base, app_id=app_id, changed=list(changed or []) or None,
                                 lineage=list(lineage or []) or None)
    if npm_dir != base:
        return npm_dir
    return app_dir(base, app_id=app_id, changed=changed, lineage=lineage)
