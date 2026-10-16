"""THE APP'S DIRECTORY FOR A BUILD — the one answer every gate planner asks.

A shared repository (paramg/boss-sandbox) holds many apps, and its ROOT holds one of them: a
pom.xml from an old Java demo. HelpDesk lives in `helpdesk-ai/` (one npm project). Four times a
HelpDesk build was refused "This project needs Java" because a planner looked at the repository
root instead of the app:

  1. find_stack_root fell back to the root when the changes sat in one app folder;
  2. it fell back again on an EMPTY change list (a repair seeded from a saved workspace,
     crun_03e44800);
  3. the gate plan's Java branch (verification_target.resolve_gate_plan) ran
     `toolchains.detect_java(<repo root>)` whatever find_stack_root said, and the first plan of a
     build is made with no change list at all (crun_7768e91ecf2d420c8b72: a resumed repair whose
     own diff was two files; its template gate then ran where that plan pointed);
  4. HelpDesk's retry 1 (crun_3933d79d) rewrote the root's package.json and package-lock.json
     beside its 27 files in helpdesk-ai/. Every later attempt was seeded with those two files, and
     "the lineage changed the root's own manifest, so the root is the app" outvoted the 27: the
     stack plan took the shared root (its pom.xml + a dozen npm apps) and retry 4
     (crun_cd48617a) was blocked "This app needs Java" in two minutes, no test run.

This module is the class fix: the app's directory comes from the run's IDENTITY — the named app
(a pinned benchmark's app_root, the folder the brief names), the folder BOSS recorded for the app
(recorded_app_dir), then the files this run changed (the change list, the seeded workspace), then
the files the app's whole lineage and record wrote (identity_paths). In a shared repository
(is_multi_app) a root manifest never outvotes the app's own files, and with no evidence at all
BOSS REFUSES in words (REFUSAL) instead of testing the whole repository as the app. The root is
the app only when it genuinely is: a single-app repository, or work that runs at the root.

Every root-level stack detection goes through here: `java_build` is the only way a gate planner
asks "is this app a Java build", and engine/tests/test_app_root.py fails if a planner calls
`toolchains.detect_java` on anything else.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

#: A file that makes its directory an app (a buildable project of some stack).
APP_MANIFESTS = ("package.json", "pom.xml", "build.gradle", "build.gradle.kts",
                 "settings.gradle", "settings.gradle.kts", "requirements.txt", "pyproject.toml", "setup.py", "go.mod", "Cargo.toml")

#: Directories that are never an app of their own, wherever they sit.
_NOT_APP_DIRS = frozenset({"node_modules", ".git", ".venv", "venv", "env", "__pycache__", "dist",
                           "build", "target", ".github", "docs"})

#: A lockfile at the root says as little about WHICH app this is as the root manifest does.
_LOCKFILES = frozenset({"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
                        "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum"})
#: A root-level file of these kinds is code that runs AT the root (serve.py, index.js).
_CODE_SUFFIXES = frozenset({".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt",
                            ".go", ".rs", ".rb", ".php"})
#: Where a workspaces root keeps its packages, one level further down.
_GROUP_DIRS = ("apps", "services", "packages")

#: THE HARNESS VERDICT for a shared repository and no evidence of the app's folder.
REFUSAL = ("BOSS cannot tell which folder this app lives in: the repository root holds several "
           "apps ({apps}), and neither this build's changes, its earlier attempts, nor the app's "
           "own record name one of them. BOSS will not test the whole repository as if it were "
           "this app. Name the app's folder in the brief (\"in the `my-app/` folder\"), then "
           "retry the build. This is BOSS's to resolve, not the code's.")


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


# ---- is the root a home for several apps -------------------------------------------------------

def subfolder_apps(root: Path | str) -> list[str]:
    """The apps BELOW the root: each top-level folder (and each one under apps/, services/,
    packages/) that holds an app manifest and is not a module of the root's own build."""
    base = Path(root)
    try:
        children = sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return []
    out: list[str] = []
    for d in children:
        if d.name in _NOT_APP_DIRS or d.name.startswith("."):
            continue
        dirs = [d]
        if d.name in _GROUP_DIRS and not _has_manifest(d):
            try:
                dirs = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
            except OSError:
                dirs = []
        for sub in dirs:
            if _has_manifest(sub) and not (sub == d and _is_module_of_root(base, d.name)):
                out.append(sub.relative_to(base).as_posix())
    return out


def _workspaces_root(base: Path) -> bool:
    """The root declares npm / pnpm workspaces: it is a home for several packages."""
    if (base / "pnpm-workspace.yaml").is_file():
        return True
    try:
        pkg = json.loads((base / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(pkg, dict) and bool(pkg.get("workspaces"))


def is_multi_app(root: Path | str) -> bool:
    """Does the repository root hold SEVERAL apps (a shared repository like boss-sandbox), so it
    is never "the app" by default? A workspaces root with packages, or three or more app folders.
    Two app folders are a full-stack app's backend/ + frontend/, not a shared repository."""
    base = Path(root)
    apps = subfolder_apps(base)
    return (bool(apps) and _workspaces_root(base)) or len(apps) >= 3


# ---- what a list of files says -----------------------------------------------------------------

def _from_paths(base: Path, paths: Optional[Iterable[str]], multi: bool = False
                ) -> tuple[Optional[Path], str]:
    """What a list of files says the app is: (directory, strength), or (None, "") when it says
    nothing. Strength "app": the files sit in app folder(s); "root": work that runs at the root
    (a root-level script, a folder that is no app of its own); "manifest": only the root's
    manifest or lockfile changed, the weakest evidence there is.

    A ROOT MANIFEST NEVER OUTVOTES THE APP'S OWN FILES IN A SHARED REPOSITORY (`multi`): crun_
    3933d79d's root package.json beside 27 helpdesk-ai/ files made the root "the app" for every
    attempt seeded after it. In a repository that is one app, a root manifest changed beside a
    nested package (a server and its client/) still makes the root the app."""
    rels = _rels(paths)
    if not rels:
        return None, ""
    root_manifest = False
    root_code = False
    apps: set[PurePosixPath] = set()
    for r in rels:
        if len(r.parts) == 1:
            if r.name in APP_MANIFESTS or r.name in _LOCKFILES:
                root_manifest = True
            elif PurePosixPath(r.name).suffix in _CODE_SUFFIXES:
                root_code = True
            continue  # a README, a root deploy file: says nothing about the app
        app = _app_of(base, r)
        if app is not None:
            apps.add(app)
        elif not (r.parts[0] in _NOT_APP_DIRS or r.parts[0].startswith(".")):
            root_code = True  # sudoku_core/board.py, tests/test_board.py: the root's own app
    if apps and (multi or not root_manifest):
        if len(apps) == 1:
            return base.joinpath(*next(iter(apps)).parts), "app"
        # Several apps: the directory that holds them all (a full-stack app's backend/ +
        # frontend/, often the root itself).
        common = list(next(iter(apps)).parts)
        for a in apps:
            n = 0
            while n < min(len(common), len(a.parts)) and common[n] == a.parts[n]:
                n += 1
            common = common[:n]
        return (base.joinpath(*common), "app") if common else (base, "root")
    if apps or root_code:
        return base, "root"
    if root_manifest:
        return base, "manifest"
    return None, ""


# ---- the resolver ------------------------------------------------------------------------------

@dataclass(frozen=True)
class AppDir:
    """Where the app lives in a checkout, and how BOSS knows. `path` is None when BOSS cannot
    tell (`refusal` says why, in words): never the shared root by default."""
    path: Optional[Path]
    source: str  # named | recorded | changed | lineage | single_app | refused
    refusal: Optional[str] = None


def clean_rel(value: Any) -> Optional[str]:
    """A repo-relative app folder, or None for the root, an empty value or one leaving the repo."""
    rel = str(value or "").strip().strip("/")
    if not rel or rel == ".":
        return None
    p = PurePosixPath(rel)
    if ".." in p.parts or p.is_absolute():
        return None
    return rel


def resolve(root: Path | str, *, app_id: Optional[str] = None, recorded: Optional[str] = None,
            changed: Optional[Iterable[str]] = None,
            lineage: Optional[Iterable[str]] = None) -> AppDir:
    """THE directory of the app this build builds, inside the checkout `root`, and its source.

    In order: `app_id`, the named app (verification_target.app_scope: the pinned benchmark app,
    else the folder the brief names); `recorded`, the folder BOSS recorded for this app
    (recorded_app_dir); the files this run changed (a seeded workspace's included); the files
    the app's whole lineage and record wrote (identity_paths). A change list that touched only
    the root's manifest defers to a lineage that names the app's folder.

    With no evidence at all: the root when the repository is one app, and REFUSED (path None,
    REFUSAL) when the root holds several apps — never the shared root by default."""
    base = Path(root)
    aid = clean_rel(app_id)
    if aid:
        return AppDir(base / aid, "named")
    rec = clean_rel(recorded)
    if rec:
        return AppDir(base / rec, "recorded")
    multi = is_multi_app(base)
    found_c, how_c = _from_paths(base, changed, multi)
    if found_c is not None and how_c != "manifest":
        return AppDir(found_c, "changed")
    found_l, how_l = _from_paths(base, lineage, multi)
    if found_l is not None and how_l == "app":
        return AppDir(found_l, "lineage")
    if found_c is not None:
        return AppDir(found_c, "changed")
    if found_l is not None:
        return AppDir(found_l, "lineage")
    if multi:
        shown = ", ".join(f"{a}/" for a in subfolder_apps(base)[:6])
        return AppDir(None, "refused", REFUSAL.format(apps=shown))
    return AppDir(base, "single_app")


def app_dir(root: Path | str, *, app_id: Optional[str] = None, recorded: Optional[str] = None,
            changed: Optional[Iterable[str]] = None,
            lineage: Optional[Iterable[str]] = None) -> Optional[Path]:
    """THE directory of the app this build builds (resolve), or None when BOSS cannot tell
    which folder of a shared repository it is. A named app's directory is the app's even before
    the build has created it (a first build's plan is made on the base branch)."""
    return resolve(root, app_id=app_id, recorded=recorded, changed=changed, lineage=lineage).path


def root_is_the_app(root: Path | str, **identity) -> bool:
    return app_dir(root, **identity) == Path(root)


def java_build(root: Path | str, **identity) -> tuple[Optional[Path], Optional[str]]:
    """(the app's directory, "maven" | "gradle" | None): is THIS APP a Java build. A pom.xml at
    the root of a shared repository never makes an app in a subfolder Java, and an app BOSS
    cannot place is no Java build: (None, None)."""
    from . import toolchains

    where = app_dir(root, **identity)
    if where is None:
        return None, None
    return where, toolchains.detect_java(where)


def npm_dir_in(root: Path | str, where: Path, *, changed: Optional[Iterable[str]] = None,
               lineage: Optional[Iterable[str]] = None) -> Path:
    """The app's npm package: the deepest package.json on this build's paths INSIDE the app
    directory `where` (verification_target.resolve_verify_cwd), else `where`. Never a
    package.json outside the app, such as a shared root's workspaces file."""
    from .verification_target import resolve_verify_cwd

    base = Path(root)
    if where == base:
        return resolve_verify_cwd(base, changed=[p.as_posix() for p in _rels(changed)] or None,
                                  lineage=[p.as_posix() for p in _rels(lineage)] or None)
    try:
        rel = where.relative_to(base).as_posix()
    except ValueError:
        return where
    inside = [p.as_posix() for p in (_rels(changed) + _rels(lineage))
              if p.as_posix().startswith(rel + "/")]
    found = resolve_verify_cwd(base, app_id=rel, changed=inside or None)
    return found if found == where or where in found.parents else where


def gate_dir(root: Path | str, *, app_id: Optional[str] = None, recorded: Optional[str] = None,
             changed: Optional[Iterable[str]] = None,
             lineage: Optional[Iterable[str]] = None) -> Optional[Path]:
    """Where a gate runs when no verify recorded a directory (an applied template's tests, the
    web package check): the app's npm package when it has one (npm_dir_in), else the app's
    directory. None when BOSS cannot tell which folder the app is (resolve)."""
    base = Path(root)
    where = app_dir(base, app_id=app_id, recorded=recorded, changed=changed, lineage=lineage)
    if where is None:
        return None
    return npm_dir_in(base, where, changed=changed, lineage=lineage)


# ---- the app's own record (BOSS's database) ----------------------------------------------------

def _run_repo(run: dict) -> tuple[str, str]:
    spec = run.get("spec") or {}
    return str(spec.get("owner") or "").strip(), str(spec.get("repo") or "").strip()


def recorded_app_dir(connection: Any, run: dict) -> Optional[str]:
    """THE APP'S FOLDER FROM BOSS'S OWN RECORDS, not from what one attempt happened to change:
    the app's code component path (app_components, Factory › Apps › Components) in this run's
    repository, else the folder a run of the same app (finalize_ctx.app_id, or the same lineage)
    in the same repository resolved and recorded (remember_app_dir). None when nothing records
    one, or the record says the root."""
    from sqlalchemy import text

    fctx = run.get("finalize_ctx") or {}
    project_id = run.get("project_id")
    owner, repo = _run_repo(run)
    if not project_id or not repo:
        return None
    try:
        from .agent_assignment import app_key
        from .naming import project_name

        key = app_key(project_name(str(run.get("title") or ""))[0])
        if key:
            row = connection.execute(text(
                "SELECT c.path FROM app_components c JOIN github_repository_bindings b "
                "ON b.id = c.binding_id WHERE c.project_id = :p AND c.app_key = :k "
                "AND c.kind = 'code' AND lower(b.full_name) = lower(:r) "
                "ORDER BY c.position, c.name LIMIT 1"),
                {"p": project_id, "k": key, "r": f"{owner}/{repo}"}).mappings().first()
            if row and clean_rel(row["path"]):
                return clean_rel(row["path"])
    except Exception:  # noqa: BLE001 — no components table / binding: the runs' record decides
        pass
    root_run = str(fctx.get("root_run_id") or run.get("id") or "")
    app_id = str(fctx.get("app_id") or "")
    row = connection.execute(text(
        "SELECT finalize_ctx->>'app_dir' AS d FROM code_task_runs "
        "WHERE project_id = :p AND spec->>'repo' = :r AND COALESCE(spec->>'owner','') = :o "
        "AND COALESCE(finalize_ctx->>'app_dir','') <> '' "
        "AND ((:a <> '' AND finalize_ctx->>'app_id' = :a) "
        "     OR COALESCE(finalize_ctx->>'root_run_id', id) = :root) "
        "ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "r": repo, "o": owner, "a": app_id, "root": root_run}).mappings().first()
    return clean_rel(row["d"]) if row else None


def remember_app_dir(connection: Any, run_id: str, rel: Optional[str]) -> bool:
    """Record the app's folder on the run (finalize_ctx.app_dir) once it is known, so every later
    attempt of the app reads it (recorded_app_dir) instead of re-deriving it from its own diff.
    Only a subfolder is recorded: the root needs no record."""
    from sqlalchemy import text

    rel = clean_rel(rel)
    if not rel or not run_id:
        return False
    connection.execute(text(
        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
        "|| jsonb_build_object('app_dir', CAST(:d AS TEXT)) WHERE id = :id"),
        {"id": run_id, "d": rel})
    return True


def _paths_of(row: dict) -> list[str]:
    out: list[str] = []
    st = row.get("state") or {}
    fctx = row.get("finalize_ctx") or {}
    saved = fctx.get("saved_workspace") if isinstance(fctx.get("saved_workspace"), dict) else {}
    for files in (st.get("impl_files"), st.get("test_files")):
        if isinstance(files, list):
            out += [p for p in files if isinstance(p, str)]
    for files in (saved.get("files"), fctx.get("pending_diff")):
        if isinstance(files, list):
            out += [str(f.get("path")) for f in files if isinstance(f, dict) and f.get("path")]
    return out


def identity_paths(engine: Any, run: dict) -> list[str]:
    """EVERY FILE THE APP HAS WRITTEN in this repository, newest first: its lineage's
    implementation files (preview_service.lineage_impl_files: every attempt and step retry under
    one root run, a deferred follow-up, the branch it built on), and every run of the same app
    (finalize_ctx.app_id) — their implementation and test files, saved workspaces and held
    diffs. One attempt's own diff is never the app's identity."""
    from sqlalchemy import text

    from ..db import begin
    from .preview_service import lineage_impl_files

    out: list[str] = []

    def add(paths: Iterable[str]) -> None:
        for p in paths:
            if p and p not in out:
                out.append(p)

    add(lineage_impl_files(engine, run) or [])
    fctx = run.get("finalize_ctx") or {}
    owner, repo = _run_repo(run)
    root_run = str(fctx.get("root_run_id") or run.get("id") or "")
    app_id = str(fctx.get("app_id") or "")
    if not run.get("project_id") or not repo:
        return out
    with begin(engine) as c:
        rows = c.execute(text(
            "SELECT state, finalize_ctx FROM code_task_runs "
            "WHERE project_id = :p AND spec->>'repo' = :r AND COALESCE(spec->>'owner','') = :o "
            "AND ((:a <> '' AND finalize_ctx->>'app_id' = :a) "
            "     OR COALESCE(finalize_ctx->>'root_run_id', id) = :root) "
            "ORDER BY created_at DESC LIMIT 200"),
            {"p": run["project_id"], "r": repo, "o": owner, "a": app_id,
             "root": root_run}).mappings().all()
    for row in rows:
        add(_paths_of(dict(row)))
    return out
