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

from typing import Any

from .design_prompts import is_ui_bearing

PYTHON_TEST_CMD = "python3 -m unittest discover -v"

# One shell command so it fits the pipeline's single-command model; `&&` short-circuits on the first
# failure, so the repair evidence points at the earliest broken stage (compile before lint before e2e).
_WEB_STEPS = ["npm run build", "npm run typecheck", "npm run lint", "npm run test:e2e"]
WEB_TEST_CMD = " && ".join(_WEB_STEPS)

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
