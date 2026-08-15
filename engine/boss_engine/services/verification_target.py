"""APP-3 (#93) — web-appropriate verification target.

The governed loop's authoritative check was hardcoded to ``python3 -m unittest discover -v`` (spec 09
§2.4). A React/Next/Vite app's *actual* health signals — does it compile, typecheck, lint, render —
were never run, so Amplify (the repair loop) had no web-native signal to optimize against: it could
make a page "more correct" by unittest while it stayed broken as a web app.

This resolves the verification command set from the **project type**:

  - **web / TS** → ``npm run build`` (compiles) → ``tsc --noEmit`` (typechecks) → ``eslint`` (lints) →
    ``playwright test`` (a minimal e2e that loads the primary route and asserts real content). These are
    exactly the scripts APP-1's scaffold defines, so the target is knowable in advance.
  - **python** → the existing ``unittest`` default, unchanged.

Additive and profile-selected: explicit ``test_commands`` on the task always win; Python tasks are
untouched. Because the repair loop re-issues with whatever the authoritative check printed, switching
the command set automatically gives Amplify **build/typecheck/e2e failure evidence** for web tasks.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .design_prompts import is_ui_bearing

VERIFY_SCRIPT_STEPS = (
    "typecheck",
    "build",
    "lint",
    "test:api",
    "test:e2e",
    "healthcheck",
)

PYTHON_TEST_CMD = "python3 -m unittest discover -v"

# One shell command so it fits the pipeline's single-command model; `&&` short-circuits on the first
# failure, so the repair evidence points at the earliest broken stage (compile before lint before e2e).
# The install guard makes the target self-sufficient on a FRESH clone (authoritative re-runs can
# happen in a clean tree where node_modules doesn't exist yet — without it, `npm run build` exits
# 127 and the failure reads as the task's fault when it's the environment's).
_WEB_INSTALL = "[ -d node_modules ] || npm install --no-audit --no-fund --loglevel=error"
_WEB_STEPS = ["npm run build", "npm run typecheck", "npm run lint", "npm run test:e2e"]
WEB_TEST_CMD = " && ".join([_WEB_INSTALL, *_WEB_STEPS])

_WEB_KINDS = {"web", "spa", "frontend", "site", "landing", "nextjs", "vite"}


def detect_kind(spec: dict[str, Any], title: str = "") -> str:
    """Classify the project as ``web`` or ``python``. An explicit ``project_kind`` wins; otherwise a
    UI-bearing task (APP-2's detector) implies web. Everything else stays Python."""
    pk = str(spec.get("project_kind", "")).lower()
    if pk in _WEB_KINDS:
        return "web"
    if pk in ("python", "py", "backend", "lib"):
        return "python"
    return "web" if is_ui_bearing(spec, title) else "python"


def default_test_commands(spec: dict[str, Any], title: str = "") -> list[str]:
    """The verification target when the task didn't specify one — web command set for web projects,
    the unittest default otherwise. (Callers use this as the fallback for ``spec['test_commands']``.)"""
    return [WEB_TEST_CMD] if detect_kind(spec, title) == "web" else [PYTHON_TEST_CMD]


def verification_profile(spec: dict[str, Any], title: str = "") -> dict[str, Any]:
    """Human-readable profile for the console / run evidence: the kind and the resolved steps."""
    kind = detect_kind(spec, title)
    return {"kind": kind,
            "steps": list(_WEB_STEPS) if kind == "web" else [PYTHON_TEST_CMD],
            "command": (WEB_TEST_CMD if kind == "web" else PYTHON_TEST_CMD)}


def resolve_verify_cwd(
    root: Path | str,
    *,
    app_id: str | None = None,
    changed: list[str] | None = None,
) -> Path:
    """Directory where the authoritative test command must run.

    Official canaries live as subdirectories in a shared sandbox monorepo.
    Running ``npm run verify`` at the clone root either misses the script or
    executes a sibling app. Prefer the named app, then a changed-file
    package.json ancestor, then the repo root.
    """
    base = Path(root)
    aid = str(app_id or "").strip().strip("/")
    if aid and (base / aid / "package.json").is_file():
        return base / aid
    for rel in changed or []:
        parts = Path(str(rel)).parts
        for depth in range(len(parts), 0, -1):
            candidate = base.joinpath(*parts[:depth])
            if candidate.is_dir() and (candidate / "package.json").is_file():
                return candidate
    return base


def ensure_verify_script(app_dir: Path | str) -> bool:
    """Add a ``verify`` script that chains existing required scripts. Never overwrites."""
    pkg_path = Path(app_dir) / "package.json"
    if not pkg_path.is_file():
        return False
    try:
        payload = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}
        payload["scripts"] = scripts
    if str(scripts.get("verify") or "").strip():
        return False
    chain = [name for name in VERIFY_SCRIPT_STEPS if str(scripts.get(name) or "").strip()]
    if not chain:
        return False
    scripts["verify"] = " && ".join(f"npm run {name}" for name in chain)
    pkg_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return True


def official_app_id(spec: dict[str, Any] | None, finalize_ctx: dict[str, Any] | None) -> str | None:
    """Named official/QA app directory, if this run is a pinned benchmark."""
    bench = (finalize_ctx or {}).get("qa_benchmark") if isinstance((finalize_ctx or {}).get("qa_benchmark"), dict) else {}
    for source in (spec or {}, bench):
        value = str(source.get("app_root") or "").strip().strip("/")
        if value:
            return value
    value = str(bench.get("id") or "").strip().strip("/")
    return value or None
