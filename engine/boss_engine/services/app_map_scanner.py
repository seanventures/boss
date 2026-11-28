"""Deterministic repository scanner for BOSS App Map (BOSSAPP-556 & BOSSAPP-557).

Strict detection precedence (never defaults to repository root on ambiguity):
  0. `.boss/apps.yaml` in the repository — ALWAYS wins when present.
  1. Native workspace & build graphs:
       - npm / pnpm / yarn workspaces (`pnpm-workspace.yaml`, `package.json#workspaces`)
       - Turborepo (`turbo.json`)
       - Nx (`nx.json`, `**/project.json`)
       - Maven multi-module (`pom.xml` `<modules>`)
       - Gradle multi-project (`settings.gradle` / `settings.gradle.kts` `include(...)`)
  2. Deployment / runtime / developer descriptors:
       - `boss.deploy.yaml`, `Dockerfile`, `docker-compose.yml` (`build.context`),
         `Procfile`, `.github/workflows/*.yml` (`working-directory`),
         `.devcontainer/devcontainer.json` (`workspaceFolder`), `AGENTS.md`
  3. Language & project manifest markers (excluding decoys like `docs/`, `examples/`, `fixtures/`,
     `templates/`, `scripts/`, and tooling-only root `package.json` files).
  4. Optional `model_classifier` hook LAST, only if deterministic layers return empty or
     unresolved ambiguity and caller injects a classifier.

When multiple runnable apps are discovered and none is pinned, returns `status="ambiguous"` with
the complete candidate list so the operator confirms or edits via the console card.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional

import yaml

from .app_map import (
    AppMapEntry,
    AppMapError,
    compute_manifest_fingerprint,
    normalize_root_path,
)

BOSS_APPS_YAML = ".boss/apps.yaml"

#: Folders that are never runnable apps (decoys, docs, fixtures, build artifacts).
DECOY_DIRS = frozenset({
    "node_modules", ".git", ".venv", "venv", "env", "__pycache__",
    "dist", "build", "out", "target", ".next", ".nuxt", ".vite", ".turbo",
    ".gradle", ".cache", "coverage", "vendor", "third_party",
    "docs", "doc", "examples", "example", "samples", "sample",
    "templates", "template", "fixtures", "test-fixtures", "testdata",
    "__fixtures__", "__mocks__", "scripts", "script", "tools",
    ".github", ".circleci", ".vscode", ".idea", ".boss", ".devcontainer",
    "migrations", "assets", "public", "static", "helm", "k8s", "terraform", "infra",
})

#: Root-level tooling packages that do not make a repo root a runnable app on their own.
_TOOLING_ONLY_DEPS = frozenset({
    "prettier", "eslint", "husky", "lint-staged", "commitlint", "@commitlint/cli",
    "@commitlint/config-conventional", "turbo", "nx", "@nx/workspace", "@nx/devkit",
    "lerna", "changesets", "@changesets/cli", "concurrently", "npm-run-all",
    "typescript", "rimraf", "cross-env", "dotenv-cli", "lefthook", "biome", "@biomejs/biome",
})

#: Manifest files recognized by the marker layer.
_MARKER_MANIFESTS = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "Pipfile",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Cargo.toml",
    "Gemfile",
    "composer.json",
)


@dataclass(frozen=True)
class ScanResult:
    """Deterministic output of `scan_repository`."""

    status: str  # proposed | ambiguous | empty
    apps: tuple[AppMapEntry, ...] = ()
    selected: Optional[AppMapEntry] = None
    workspace_root: str = "."
    source: str = "empty"
    shared_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    refusal: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "workspace_root": self.workspace_root,
            "selected": self.selected.as_dict() if self.selected else None,
            "apps": [a.as_dict() for a in self.apps],
            "candidates": [a.as_dict() for a in self.apps],
            "shared_paths": list(self.shared_paths),
            "warnings": list(self.warnings),
            "refusal": self.refusal,
        }


# ── Helpers ────────────────────────────────────────────────────────────────────────────────────

def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_read_text(path))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(_read_text(path))
    except yaml.YAMLError:
        return None


def _slug_key(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return slug or "app"


def _is_decoy_rel(rel: str) -> bool:
    if rel in ("", "."):
        return False
    parts = PurePosixPath(rel).parts
    return any(p in DECOY_DIRS or p.startswith(".") for p in parts)


def _package_manager_for(base: Path, app_dir: Path) -> str:
    for d in (app_dir, base):
        if (d / "pnpm-lock.yaml").is_file() or (d / "pnpm-workspace.yaml").is_file():
            return "pnpm"
        if (d / "yarn.lock").is_file():
            return "yarn"
        if (d / "bun.lockb").is_file() or (d / "bun.lock").is_file():
            return "bun"
    return "npm"


def _is_tooling_only_package(pkg: Mapping[str, Any], folder: Path) -> bool:
    """True when a package.json looks like a repo-root workspace/tooling wrapper, not an app."""
    if not pkg:
        return False
    if pkg.get("workspaces"):
        return True
    deps = set((pkg.get("dependencies") or {}).keys()) if isinstance(pkg.get("dependencies"), dict) else set()
    dev_deps = set((pkg.get("devDependencies") or {}).keys()) if isinstance(pkg.get("devDependencies"), dict) else set()
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    # If it has runtime deps or a start/serve/dev script or main entrypoint with source file, it's an app
    if deps:
        return False
    for k in ("start", "serve", "preview"):
        if str(scripts.get(k) or "").strip():
            return False
    if pkg.get("main") and (folder / str(pkg["main"])).is_file():
        return False
    # Check if there are actual source files directly in src/ or index.js/server.js
    for f in ("index.js", "server.js", "app.js", "index.ts", "server.ts", "src/index.ts", "src/index.js", "src/main.ts", "src/App.tsx"):
        if (folder / f).is_file():
            return False
    if not dev_deps or dev_deps.issubset(_TOOLING_ONLY_DEPS):
        return True
    return False


def _is_node_runnable_app(pkg: Mapping[str, Any], folder: Path, *, rel: str = "") -> bool:
    """Distinguish runnable apps (web, api, cli) from shared internal library packages."""
    parts = PurePosixPath(rel).parts if rel and rel != "." else ()
    if parts and parts[0] in ("packages", "libs", "lib", "shared", "internal"):
        # Only treat packages/* or libs/* as an app if it explicitly declares a server/web framework or start script
        deps = _pkg_deps(pkg)
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        web_or_srv = {"next", "express", "fastify", "koa", "hono", "@nestjs/core", "nuxt", "@sveltejs/kit"}
        if not (deps & web_or_srv) and not str(scripts.get("start") or "").strip():
            return False
    return True


def _pkg_deps(pkg: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        val = pkg.get(key)
        if isinstance(val, dict):
            out.update(str(k) for k in val)
    return out


def _detect_stack_and_commands(
    base: Path,
    root_path: str,
    *,
    workspace_root: str = ".",
) -> tuple[dict[str, Any], dict[str, Optional[str]], dict[str, Any]]:
    """Inspect manifests inside `root_path` and return (stack, commands, run)."""
    app_dir = base if root_path == "." else base / root_path
    stack: dict[str, Any] = {}
    commands: dict[str, Optional[str]] = {}
    run: dict[str, Any] = {}

    if not app_dir.is_dir():
        return stack, commands, run

    # Check boss.preview.command / boss.preview.health / Procfile first for run spec
    if (app_dir / "boss.preview.command").is_file():
        cmd = _read_text(app_dir / "boss.preview.command").strip()
        if cmd:
            run["command"] = cmd
    if (app_dir / "boss.preview.health").is_file():
        health = _read_text(app_dir / "boss.preview.health").strip()
        if health:
            run["health"] = health
    if (app_dir / "Procfile").is_file() and not run.get("command"):
        for line in _read_text(app_dir / "Procfile").splitlines():
            m = re.match(r"^\s*web\s*:\s*(.+)$", line)
            if m:
                run["command"] = m.group(1).strip()
                break

    # Node / JS / TS
    pkg_file = app_dir / "package.json"
    has_pkg = pkg_file.is_file()
    has_py = any((app_dir / f).is_file() for f in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"))
    has_go = (app_dir / "go.mod").is_file()
    has_maven = (app_dir / "pom.xml").is_file()
    has_gradle = (app_dir / "build.gradle").is_file() or (app_dir / "build.gradle.kts").is_file()
    has_rust = (app_dir / "Cargo.toml").is_file()
    has_ruby = (app_dir / "Gemfile").is_file()
    has_php = (app_dir / "composer.json").is_file()

    # Check if this directory is a 2-service full-stack app (e.g. backend/ + frontend/)
    sub_services = _detect_child_services(app_dir)
    if sub_services and not (has_pkg or has_py or has_go or has_maven or has_gradle):
        langs = list(dict.fromkeys(s["language"] for s in sub_services))
        stack = {
            "language": "multi" if len(langs) > 1 else langs[0],
            "languages": langs,
            "role": "fullstack",
            "services": sub_services,
        }
        return stack, commands, run

    if has_pkg:
        pkg = _read_json(pkg_file)
        deps = _pkg_deps(pkg)
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        pm = _package_manager_for(base, app_dir)
        fw = None
        role = "app"
        for marker, fw_name, fw_role in (
            ("next", "next", "frontend"),
            ("@nestjs/core", "nestjs", "backend"),
            ("express", "express", "backend"),
            ("fastify", "fastify", "backend"),
            ("hono", "hono", "backend"),
            ("koa", "koa", "backend"),
            ("react", "react", "frontend"),
            ("vite", "vite", "frontend"),
            ("vue", "vue", "frontend"),
            ("svelte", "svelte", "frontend"),
            ("@angular/core", "angular", "frontend"),
        ):
            if marker in deps:
                fw = fw_name
                role = fw_role
                break
        stack = {
            "language": "node",
            "framework": fw,
            "role": role,
            "package_manager": pm,
        }
        install_cmd = {
            "pnpm": "pnpm install --frozen-lockfile" if (base / "pnpm-lock.yaml").is_file() else "pnpm install",
            "yarn": "yarn install",
            "bun": "bun install",
        }.get(pm, "npm install --include=dev --no-audit --no-fund --loglevel=error")
        commands["install"] = install_cmd
        run_prefix = f"{pm} run" if pm != "npm" else "npm run"
        for k in ("build", "typecheck", "lint"):
            if str(scripts.get(k) or "").strip():
                commands[k] = f"{run_prefix} {k}"
        test_body = str(scripts.get("test") or "").strip()
        if test_body and "no test specified" not in test_body.lower():
            commands["test"] = f"{pm} test"
        if str(scripts.get("test:e2e") or "").strip():
            commands["e2e"] = f"{run_prefix} test:e2e"
        if not run.get("command"):
            if str(scripts.get("start") or "").strip():
                run["command"] = f"{pm} start" if pm == "npm" else f"{pm} run start"
            elif str(scripts.get("dev") or "").strip():
                run["command"] = f"{run_prefix} dev"
            elif pkg.get("main") and (app_dir / str(pkg["main"])).is_file():
                run["command"] = f"node {pkg['main']}"
        return stack, commands, run

    if has_py:
        py_text = _read_text(app_dir / "pyproject.toml") + "\n" + _read_text(app_dir / "requirements.txt")
        low = py_text.lower()
        fw = next((k for k in ("fastapi", "django", "flask") if k in low), None)
        stack = {
            "language": "python",
            "framework": fw,
            "role": "backend",
            "package_manager": "pip",
        }
        if (app_dir / "requirements.txt").is_file():
            commands["install"] = "python3 -m pip install --user --disable-pip-version-check --no-input -q -r requirements.txt"
        elif "[project]" in _read_text(app_dir / "pyproject.toml") or (app_dir / "setup.py").is_file():
            commands["install"] = "python3 -m pip install --user --disable-pip-version-check --no-input -q ."
        uses_pytest = (
            "pytest" in low
            or (app_dir / "pytest.ini").is_file()
            or (app_dir / "conftest.py").is_file()
            or (app_dir / "tests" / "conftest.py").is_file()
        )
        commands["test"] = "python3 -m pytest -q" if uses_pytest else "python3 -m unittest discover -v"
        if not run.get("command"):
            if (app_dir / "manage.py").is_file():
                run["command"] = 'python3 manage.py runserver "0.0.0.0:$PORT" --noreload'
            elif fw == "fastapi":
                run["command"] = 'python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"'
            elif fw == "flask":
                run["command"] = 'python3 -m flask --app app run --host 0.0.0.0 --port "$PORT"'
        return stack, commands, run

    if has_go:
        gomod = _read_text(app_dir / "go.mod")
        fw = next(
            (name for marker, name in (
                ("github.com/gin-gonic/gin", "gin"),
                ("github.com/labstack/echo", "echo"),
                ("github.com/go-chi/chi", "chi"),
                ("github.com/gofiber/fiber", "fiber"),
            ) if marker in gomod),
            "net-http",
        )
        cmd_entries: list[str] = []
        cmd_dir = app_dir / "cmd"
        if cmd_dir.is_dir():
            for child in sorted(cmd_dir.iterdir()):
                if child.is_dir() and any(child.glob("*.go")):
                    cmd_entries.append(f"cmd/{child.name}")
        stack = {
            "language": "go",
            "framework": fw,
            "role": "backend",
            "package_manager": "go modules",
            **({"entrypoints": cmd_entries} if cmd_entries else {}),
        }
        commands["lint"] = "go vet ./..."
        commands["test"] = "go test ./..."
        if cmd_entries:
            commands["build"] = "go build ./..."
            run.setdefault("entrypoints", cmd_entries)
            run.setdefault("command", f"go run ./{cmd_entries[0]}")
        elif any(app_dir.glob("*.go")):
            commands["build"] = "go build ."
            run.setdefault("command", "go run .")
        return stack, commands, run

    if has_maven:
        pom_text = _read_text(app_dir / "pom.xml")
        fw = "spring-boot" if "spring-boot" in pom_text else ("dropwizard" if "dropwizard" in pom_text else None)
        stack = {"language": "java", "framework": fw, "role": "backend", "package_manager": "maven"}
        commands["build"] = "mvn -B -ntp verify"
        commands["test"] = "mvn -B -ntp test"
        return stack, commands, run

    if has_gradle:
        gfile = app_dir / ("build.gradle.kts" if (app_dir / "build.gradle.kts").is_file() else "build.gradle")
        gtext = _read_text(gfile)
        fw = next((k for k in ("spring-boot", "ktor", "quarkus", "micronaut") if k in gtext.lower()), None)
        wrapper = "./gradlew" if (app_dir / "gradlew").is_file() or (base / "gradlew").is_file() else "gradle"
        stack = {"language": "java", "framework": fw, "role": "backend", "package_manager": "gradle"}
        commands["build"] = f"{wrapper} build"
        commands["test"] = f"{wrapper} test"
        return stack, commands, run

    if has_rust:
        stack = {"language": "rust", "framework": None, "role": "backend", "package_manager": "cargo", "supported": False}
        commands["build"] = "cargo build"
        commands["test"] = "cargo test"
        return stack, commands, run

    if has_ruby:
        stack = {"language": "ruby", "framework": None, "role": "backend", "package_manager": "bundler", "supported": False}
        return stack, commands, run

    if has_php:
        stack = {"language": "php", "framework": None, "role": "backend", "package_manager": "composer", "supported": False}
        return stack, commands, run

    return stack, commands, run


def _detect_child_services(folder: Path) -> list[dict[str, Any]]:
    """Detect immediate child services inside a full-stack app folder (e.g. backend/ + frontend/)."""
    services: list[dict[str, Any]] = []
    try:
        children = sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return []
    for child in children:
        if child.name in DECOY_DIRS or child.name.startswith("."):
            continue
        if (child / "package.json").is_file():
            pkg = _read_json(child / "package.json")
            deps = _pkg_deps(pkg)
            role = "frontend" if deps & {"react", "vite", "vue", "svelte", "next", "@angular/core"} else "backend"
            services.append({"dir": child.name, "language": "node", "role": role})
        elif any((child / m).is_file() for m in ("pyproject.toml", "requirements.txt", "setup.py")):
            services.append({"dir": child.name, "language": "python", "role": "backend"})
        elif (child / "go.mod").is_file():
            services.append({"dir": child.name, "language": "go", "role": "backend"})
        elif any((child / m).is_file() for m in ("pom.xml", "build.gradle", "build.gradle.kts")):
            services.append({"dir": child.name, "language": "java", "role": "backend"})
    return services


def _build_entry(
    base: Path,
    root_path: str,
    *,
    workspace_root: str = ".",
    app_key: Optional[str] = None,
    app_name: Optional[str] = None,
    source: str,
    confidence: str,
    evidence: Iterable[str],
    shared_paths: Iterable[str] = (),
    extra_stack: Optional[Mapping[str, Any]] = None,
    extra_commands: Optional[Mapping[str, Any]] = None,
    extra_run: Optional[Mapping[str, Any]] = None,
) -> AppMapEntry:
    norm_root = normalize_root_path(root_path)
    norm_ws = normalize_root_path(workspace_root)
    stack, commands, run_spec = _detect_stack_and_commands(base, norm_root, workspace_root=norm_ws)
    if extra_stack:
        stack.update(dict(extra_stack))
    if extra_commands:
        for k, v in extra_commands.items():
            if v is not None:
                commands[k] = v
    if extra_run:
        run_spec.update(dict(extra_run))

    default_name = base.name if norm_root == "." else PurePosixPath(norm_root).name
    if norm_root != "." and (base / norm_root / "package.json").is_file():
        pkg_name = str(_read_json(base / norm_root / "package.json").get("name") or "").strip()
        if pkg_name:
            default_name = pkg_name.split("/")[-1]
    elif norm_root == "." and (base / "package.json").is_file():
        pkg_name = str(_read_json(base / "package.json").get("name") or "").strip()
        if pkg_name:
            default_name = pkg_name.split("/")[-1]

    key = _slug_key(app_key or default_name)
    name = (app_name or default_name or key).strip()
    fp, mans = compute_manifest_fingerprint(base, norm_root, norm_ws)
    return AppMapEntry(
        app_key=key,
        app_name=name,
        name=name,
        root_path=norm_root,
        workspace_root=norm_ws,
        stack=stack,
        commands=commands,
        run=run_spec,
        manifests=tuple(mans),
        shared_paths=tuple(shared_paths),
        provenance={
            "source": source,
            "confidence": confidence,
            "evidence": list(evidence),
        },
        fingerprint=fp,
        status="proposed",
    )


# ── Layer 0: `.boss/apps.yaml` ─────────────────────────────────────────────────────────────────

def load_boss_apps_yaml(repo_root: Path | str) -> list[AppMapEntry]:
    """Parse `.boss/apps.yaml` if present. Always wins over heuristic detection."""
    base = Path(repo_root)
    cfg_path = base / BOSS_APPS_YAML
    if not cfg_path.is_file():
        return []
    raw = _read_yaml(cfg_path)
    if not isinstance(raw, dict):
        raise AppMapError("invalid_boss_apps_yaml", f"{BOSS_APPS_YAML} must be a YAML mapping.")
    apps_raw = raw.get("apps")
    if not isinstance(apps_raw, list) or not apps_raw:
        raise AppMapError("invalid_boss_apps_yaml", f"{BOSS_APPS_YAML} must define a non-empty 'apps' list.")
    entries: list[AppMapEntry] = []
    for idx, item in enumerate(apps_raw):
        if not isinstance(item, dict):
            raise AppMapError("invalid_boss_apps_yaml", f"{BOSS_APPS_YAML} apps[{idx}] must be a mapping.")
        root_path = normalize_root_path(item.get("root_path", "."))
        ws_root = normalize_root_path(item.get("workspace_root", "."))
        key = _slug_key(str(item.get("key") or item.get("app_key") or item.get("name") or (
            PurePosixPath(root_path).name if root_path != "." else "app"
        )))
        name = str(item.get("name") or item.get("app_name") or key).strip()
        detected_stack, detected_cmds, detected_run = _detect_stack_and_commands(
            base, root_path, workspace_root=ws_root
        )
        stack = {**detected_stack, **(item.get("stack") if isinstance(item.get("stack"), dict) else {})}
        commands = {**detected_cmds, **(item.get("commands") if isinstance(item.get("commands"), dict) else {})}
        run_spec = {**detected_run, **(item.get("run") if isinstance(item.get("run"), dict) else {})}
        shared = item.get("shared_paths") or ()
        fp, mans = compute_manifest_fingerprint(base, root_path, ws_root)
        entries.append(AppMapEntry(
            app_key=key,
            app_name=name,
            name=name,
            root_path=root_path,
            workspace_root=ws_root,
            stack=stack,
            commands=commands,
            run=run_spec,
            manifests=tuple(mans),
            shared_paths=tuple(shared),
            provenance={
                "source": "boss_apps_yaml",
                "confidence": "authoritative",
                "evidence": [BOSS_APPS_YAML],
            },
            fingerprint=fp,
            status="confirmed",
        ))
    return entries


# ── Layer 1: Native workspace & build graphs ───────────────────────────────────────────────────

def _expand_workspace_globs(base: Path, patterns: Iterable[str]) -> list[str]:
    found: list[str] = []
    excluded: set[str] = set()
    pos_patterns: list[str] = []
    for raw in patterns:
        pat = str(raw or "").strip().strip("/")
        if not pat:
            continue
        if pat.startswith("!"):
            excluded.add(pat[1:].strip("/"))
        else:
            pos_patterns.append(pat)
    for pat in pos_patterns:
        for match in sorted(base.glob(pat)):
            if not match.is_dir() or match.is_symlink():
                continue
            rel = match.relative_to(base).as_posix()
            if _is_decoy_rel(rel) or rel in excluded:
                continue
            if rel not in found:
                found.append(rel)
    return found


def _scan_nx(base: Path) -> Optional[tuple[list[AppMapEntry], list[str], str]]:
    if not (base / "nx.json").is_file():
        return None
    apps: list[AppMapEntry] = []
    shared: list[str] = []
    evidence = ["nx.json"]
    for proj_file in sorted(base.rglob("project.json")):
        rel_dir = proj_file.parent.relative_to(base).as_posix()
        if rel_dir == "." or _is_decoy_rel(rel_dir):
            continue
        data = _read_json(proj_file)
        ptype = str(data.get("projectType") or "").lower()
        name = str(data.get("name") or PurePosixPath(rel_dir).name)
        targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}
        is_lib = ptype == "library" or (not ptype and rel_dir.startswith(("libs/", "packages/")))
        if is_lib:
            shared.append(rel_dir)
            continue
        extra_cmds: dict[str, Any] = {}
        if "build" in targets:
            extra_cmds["build"] = f"npx nx build {name}"
        if "test" in targets:
            extra_cmds["test"] = f"npx nx test {name}"
        if "lint" in targets:
            extra_cmds["lint"] = f"npx nx lint {name}"
        apps.append(_build_entry(
            base,
            rel_dir,
            workspace_root=".",
            app_key=name,
            app_name=name,
            source="workspace_graph",
            confidence="high",
            evidence=[*evidence, f"{rel_dir}/project.json"],
            extra_stack={"workspace_tool": "nx"},
            extra_commands=extra_cmds,
        ))
    if not apps:
        return None
    if shared:
        apps = [replace(a, shared_paths=tuple(shared)) for a in apps]
    return apps, shared, "workspace_graph:nx"


def _scan_js_workspaces(base: Path) -> Optional[tuple[list[AppMapEntry], list[str], str]]:
    patterns: list[str] = []
    evidence: list[str] = []
    ws_tool = "npm-workspaces"

    pnpm_ws = base / "pnpm-workspace.yaml"
    if pnpm_ws.is_file():
        ws_tool = "pnpm-workspaces"
        evidence.append("pnpm-workspace.yaml")
        data = _read_yaml(pnpm_ws)
        if isinstance(data, dict) and isinstance(data.get("packages"), list):
            patterns.extend(str(p) for p in data["packages"])

    root_pkg = _read_json(base / "package.json") if (base / "package.json").is_file() else {}
    ws_field = root_pkg.get("workspaces")
    if isinstance(ws_field, list):
        patterns.extend(str(p) for p in ws_field)
        evidence.append("package.json#workspaces")
    elif isinstance(ws_field, dict) and isinstance(ws_field.get("packages"), list):
        patterns.extend(str(p) for p in ws_field["packages"])
        evidence.append("package.json#workspaces")

    has_turbo = (base / "turbo.json").is_file()
    if has_turbo:
        ws_tool = "turborepo"
        evidence.append("turbo.json")

    if not patterns:
        return None

    dirs = _expand_workspace_globs(base, patterns)
    apps: list[AppMapEntry] = []
    shared: list[str] = []
    for rel in dirs:
        pkg_path = base / rel / "package.json"
        if not pkg_path.is_file():
            continue
        pkg = _read_json(pkg_path)
        if _is_node_runnable_app(pkg, base / rel, rel=rel):
            apps.append(_build_entry(
                base,
                rel,
                workspace_root=".",
                source="workspace_graph",
                confidence="high",
                evidence=[*evidence, f"{rel}/package.json"],
                extra_stack={"workspace_tool": ws_tool},
            ))
        else:
            shared.append(rel)

    if not apps:
        return None
    if shared:
        apps = [
            AppMapEntry(
                app_key=a.app_key,
                app_name=a.app_name,
                name=a.name,
                root_path=a.root_path,
                workspace_root=a.workspace_root,
                stack=a.stack,
                commands=a.commands,
                run=a.run,
                manifests=a.manifests,
                shared_paths=tuple(shared),
                provenance=a.provenance,
                fingerprint=a.fingerprint,
                status=a.status,
            )
            for a in apps
        ]
    return apps, shared, f"workspace_graph:{ws_tool}"


def _scan_maven_multimodule(base: Path) -> Optional[tuple[list[AppMapEntry], list[str], str]]:
    pom = base / "pom.xml"
    if not pom.is_file():
        return None
    text = _read_text(pom)
    modules = [m.strip().lstrip("./").strip("/") for m in re.findall(r"<module>\s*([^<]+?)\s*</module>", text)]
    modules = [m for m in modules if m and not _is_decoy_rel(m) and (base / m / "pom.xml").is_file()]
    if not modules:
        return None
    # Distinguish runnable app modules (e.g. spring-boot-maven-plugin / mainClass) vs library modules
    app_mods: list[str] = []
    lib_mods: list[str] = []
    for mod in modules:
        mpom = _read_text(base / mod / "pom.xml")
        if any(tok in mpom for tok in ("spring-boot-maven-plugin", "spring-boot-starter-web", "quarkus", "micronaut", "exec-maven-plugin", "<mainClass>")):
            app_mods.append(mod)
        else:
            lib_mods.append(mod)

    entry = _build_entry(
        base,
        ".",
        workspace_root=".",
        source="workspace_graph",
        confidence="high",
        evidence=["pom.xml#<modules>", *[f"{m}/pom.xml" for m in modules]],
        shared_paths=tuple(lib_mods),
        extra_stack={
            "workspace_tool": "maven-multi-module",
            "modules": modules,
            "runnable_modules": app_mods,
            "library_modules": lib_mods,
        },
    )
    return [entry], lib_mods, "workspace_graph:maven"


def _scan_gradle_multiproject(base: Path) -> Optional[tuple[list[AppMapEntry], list[str], str]]:
    sfile = next((base / n for n in ("settings.gradle.kts", "settings.gradle") if (base / n).is_file()), None)
    if sfile is None:
        return None
    text = _read_text(sfile)
    # Match include(":api", ":web") or include 'api', 'web'
    raw_includes = re.findall(r"""include\s*\(?([^\n\)]+)\)?""", text)
    subprojects: list[str] = []
    for block in raw_includes:
        for tok in re.findall(r"""["']:?([A-Za-z0-9_.:-]+)["']""", block):
            rel = tok.lstrip(":").replace(":", "/")
            if rel and not _is_decoy_rel(rel) and (base / rel).is_dir() and rel not in subprojects:
                subprojects.append(rel)
    if not subprojects:
        return None

    app_subs: list[str] = []
    lib_subs: list[str] = []
    for sub in subprojects:
        bfile = next((base / sub / n for n in ("build.gradle.kts", "build.gradle") if (base / sub / n).is_file()), None)
        btext = _read_text(bfile) if bfile else ""
        if any(tok in btext for tok in ("application", "org.springframework.boot", "io.ktor.plugin", "mainClass")):
            app_subs.append(sub)
        else:
            lib_subs.append(sub)

    entry = _build_entry(
        base,
        ".",
        workspace_root=".",
        source="workspace_graph",
        confidence="high",
        evidence=[sfile.name, *subprojects],
        shared_paths=tuple(lib_subs),
        extra_stack={
            "workspace_tool": "gradle-multi-project",
            "modules": subprojects,
            "runnable_modules": app_subs,
            "library_modules": lib_subs,
        },
    )
    return [entry], lib_subs, "workspace_graph:gradle"


# ── Layer 2: Deployment & runtime descriptors ──────────────────────────────────────────────────

def _scan_descriptors(base: Path) -> Optional[tuple[list[AppMapEntry], list[str], str]]:
    """Scan Dockerfile, docker-compose, boss.deploy.yaml, Procfile, CI workflows, devcontainer, AGENTS.md."""
    candidates: dict[str, list[str]] = {}

    def _add(rel: str, ev: str) -> None:
        norm = normalize_root_path(rel)
        if norm != "." and _is_decoy_rel(norm):
            return
        d = base if norm == "." else base / norm
        if not d.is_dir():
            return
        # Must actually contain a manifest or source files
        has_any_manifest = any((d / m).is_file() for m in _MARKER_MANIFESTS) or bool(_detect_child_services(d))
        if not has_any_manifest:
            return
        candidates.setdefault(norm, [])
        if ev not in candidates[norm]:
            candidates[norm].append(ev)

    # 1. docker-compose.yml build.context entries
    for cname in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        cpath = base / cname
        if not cpath.is_file():
            continue
        data = _read_yaml(cpath)
        svcs = data.get("services") if isinstance(data, dict) and isinstance(data.get("services"), dict) else {}
        for svc_name, svc in svcs.items():
            if not isinstance(svc, dict):
                continue
            build = svc.get("build")
            ctx = build if isinstance(build, str) else (build.get("context") if isinstance(build, dict) else None)
            if ctx:
                try:
                    _add(str(ctx), f"{cname}#services.{svc_name}.build")
                except AppMapError:
                    pass

    # 2. boss.deploy.yaml (depth <= 2)
    for depth_pat in ("boss.deploy.yaml", "*/boss.deploy.yaml", "*/*/boss.deploy.yaml"):
        for f in sorted(base.glob(depth_pat)):
            rel = f.parent.relative_to(base).as_posix()
            _add(rel, f.relative_to(base).as_posix())

    # 3. GitHub Actions working-directory
    wf_dir = base / ".github" / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.glob("*.y*ml")):
            wtext = _read_text(wf)
            for m in re.finditer(r"working-directory\s*:\s*['\"]?([A-Za-z0-9_./-]+)['\"]?", wtext):
                raw_dir = m.group(1).strip()
                if "$" in raw_dir:
                    continue
                try:
                    _add(raw_dir, f".github/workflows/{wf.name}#working-directory")
                except AppMapError:
                    pass

    # 4. .devcontainer/devcontainer.json workspaceFolder
    dc = base / ".devcontainer" / "devcontainer.json"
    if dc.is_file():
        dtext = _read_text(dc)
        m = re.search(r'"workspaceFolder"\s*:\s*"/workspaces/[^/]+/([^"]+)"', dtext)
        if m:
            try:
                _add(m.group(1), ".devcontainer/devcontainer.json#workspaceFolder")
            except AppMapError:
                pass

    # 5. AGENTS.md explicit app path references (only if they match a real manifest dir)
    agents_md = base / "AGENTS.md"
    if agents_md.is_file():
        atext = _read_text(agents_md)
        for m in re.finditer(r"`([A-Za-z0-9][\w.-]*(?:/[\w.-]+)*)/`", atext):
            try:
                _add(m.group(1), "AGENTS.md")
            except AppMapError:
                pass

    # 6. Nested Dockerfile / Procfile (only nested; root Dockerfile is checked if no nested descriptors)
    for depth_pat in ("*/Dockerfile", "*/*/Dockerfile", "*/Procfile", "*/*/Procfile"):
        for f in sorted(base.glob(depth_pat)):
            rel = f.parent.relative_to(base).as_posix()
            _add(rel, f.relative_to(base).as_posix())

    # Filter out "." if concrete subfolder apps were found via descriptors
    sub_cands = {k: v for k, v in candidates.items() if k != "."}
    chosen = sub_cands if sub_cands else candidates
    if not chosen:
        return None

    apps = [
        _build_entry(
            base,
            rel,
            workspace_root=".",
            source="descriptor",
            confidence="medium",
            evidence=evs,
        )
        for rel, evs in sorted(chosen.items())
    ]
    return apps, [], "descriptor"


# ── Layer 3: Language & manifest markers ───────────────────────────────────────────────────────

def _has_any_marker(folder: Path) -> Optional[str]:
    for m in _MARKER_MANIFESTS:
        if (folder / m).is_file():
            return m
    return None


def _scan_markers(base: Path) -> tuple[list[AppMapEntry], list[str], str]:
    """Scan language manifests at root and non-decoy subfolders (depth <= 2)."""
    subfolder_hits: list[tuple[str, str]] = []
    group_dirs = ("apps", "services", "packages")

    try:
        top_dirs = sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        top_dirs = []

    for d in top_dirs:
        if d.name in DECOY_DIRS or d.name.startswith("."):
            continue
        m = _has_any_marker(d)
        if m:
            subfolder_hits.append((d.name, f"{d.name}/{m}"))
        elif d.name in group_dirs:
            try:
                nested = sorted(p for p in d.iterdir() if p.is_dir() and not p.is_symlink())
            except OSError:
                nested = []
            for sub in nested:
                if sub.name in DECOY_DIRS or sub.name.startswith("."):
                    continue
                sm = _has_any_marker(sub)
                if sm:
                    rel = f"{d.name}/{sub.name}"
                    subfolder_hits.append((rel, f"{rel}/{sm}"))

    root_marker = _has_any_marker(base)
    root_is_tooling = False
    if root_marker == "package.json":
        root_is_tooling = _is_tooling_only_package(_read_json(base / "package.json"), base)

    # Case A: Single real subfolder app beside decoys / tooling-only root package.json
    if len(subfolder_hits) == 1 and (root_marker is None or root_is_tooling):
        rel, ev = subfolder_hits[0]
        return [
            _build_entry(base, rel, workspace_root=".", source="marker", confidence="medium", evidence=[ev])
        ], [], "marker"

    # Case B: Two-service full-stack app at root (e.g. backend/ + frontend/ or api/ + web/, no stray root app)
    if len(subfolder_hits) == 2 and (root_marker is None or root_is_tooling):
        child_svcs = _detect_child_services(base)
        roles = {s["role"] for s in child_svcs}
        if len(child_svcs) == 2 and "backend" in roles and "frontend" in roles:
            entry = _build_entry(
                base,
                ".",
                workspace_root=".",
                source="marker",
                confidence="high",
                evidence=[ev for _, ev in subfolder_hits],
            )
            return [entry], [], "marker"

    # Case C: Multi-app repo (2+ independent subfolder apps, possibly with stray root manifests like boss-sandbox)
    if len(subfolder_hits) >= 2:
        apps = [
            _build_entry(base, rel, workspace_root=".", source="marker", confidence="medium", evidence=[ev])
            for rel, ev in subfolder_hits
        ]
        return apps, [], "marker"

    # Case D: Root app (single-app repo)
    if root_marker is not None:
        return [
            _build_entry(base, ".", workspace_root=".", source="marker", confidence="medium", evidence=[root_marker])
        ], [], "marker"

    return [], [], "empty"


# ── Main Scanner Entrypoint ────────────────────────────────────────────────────────────────────

def scan_repository(
    repo_root: Path | str,
    *,
    model_classifier: Optional[Callable[[Path, list[AppMapEntry]], list[AppMapEntry]]] = None,
) -> ScanResult:
    """Scan `repo_root` deterministically in strict precedence order:
    .boss/apps.yaml -> native workspace graphs -> descriptors -> markers -> optional model hook.
    """
    base = Path(repo_root)
    if not base.is_dir():
        return ScanResult(
            status="empty",
            source="empty",
            refusal=f"Repository directory {str(base)!r} does not exist.",
        )

    # Layer 0: `.boss/apps.yaml` ALWAYS wins
    yaml_apps = load_boss_apps_yaml(base)
    if yaml_apps:
        if len(yaml_apps) == 1:
            return ScanResult(
                status="proposed",
                apps=tuple(yaml_apps),
                selected=yaml_apps[0],
                workspace_root=yaml_apps[0].workspace_root,
                source="boss_apps_yaml",
                shared_paths=yaml_apps[0].shared_paths,
            )
        return ScanResult(
            status="ambiguous",
            apps=tuple(yaml_apps),
            selected=None,
            workspace_root=".",
            source="boss_apps_yaml",
            refusal=(
                f".boss/apps.yaml defines {len(yaml_apps)} apps "
                f"({', '.join(a.root_path for a in yaml_apps[:6])}). Pick which app to work on."
            ),
        )

    # Layer 1: Native workspace & build graphs (Nx, npm/pnpm/Turbo workspaces, Maven, Gradle)
    for scanner in (_scan_nx, _scan_js_workspaces, _scan_maven_multimodule, _scan_gradle_multiproject):
        found = scanner(base)
        if found is not None:
            apps, shared, src = found
            return _finalize_scan(base, apps, shared, src, model_classifier=model_classifier)

    # Layer 2: Deployment & runtime descriptors (docker-compose, boss.deploy.yaml, Dockerfile, Procfile, CI, devcontainer, AGENTS.md)
    desc = _scan_descriptors(base)
    if desc is not None:
        apps, shared, src = desc
        return _finalize_scan(base, apps, shared, src, model_classifier=model_classifier)

    # Layer 3: Language manifests & markers
    apps, shared, src = _scan_markers(base)
    return _finalize_scan(base, apps, shared, src, model_classifier=model_classifier)


def _finalize_scan(
    base: Path,
    apps: list[AppMapEntry],
    shared: list[str],
    source: str,
    *,
    model_classifier: Optional[Callable[[Path, list[AppMapEntry]], list[AppMapEntry]]] = None,
) -> ScanResult:
    # Layer 4: Model classifier LAST, only when deterministic layers are empty or ambiguous
    if (not apps or len(apps) > 1) and model_classifier is not None:
        try:
            classified = list(model_classifier(base, list(apps)) or [])
        except Exception:  # noqa: BLE001 — model hook failure never crashes deterministic scan
            classified = []
        if classified:
            apps = classified
            source = "model"

    if len(apps) == 1:
        return ScanResult(
            status="proposed",
            apps=(apps[0],),
            selected=apps[0],
            workspace_root=apps[0].workspace_root,
            source=source,
            shared_paths=tuple(shared) or apps[0].shared_paths,
        )

    if len(apps) > 1:
        listed = ", ".join(f"{a.root_path}/" for a in apps[:8])
        return ScanResult(
            status="ambiguous",
            apps=tuple(apps),
            selected=None,
            workspace_root=".",
            source=source,
            shared_paths=tuple(shared),
            refusal=(
                f"Multiple apps detected ({listed}). Confirm which app to map in the console "
                "(or declare them in `.boss/apps.yaml`); BOSS will not fall back to the repository root."
            ),
        )

    return ScanResult(
        status="empty",
        apps=(),
        selected=None,
        workspace_root=".",
        source=source,
        refusal=(
            "No runnable app manifest was detected in this repository. Configure the App Map in "
            "the console or add `.boss/apps.yaml`; BOSS will not silently default to the root."
        ),
    )
