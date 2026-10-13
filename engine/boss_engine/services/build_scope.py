"""What a build owes BEYOND its brief: ops endpoints and browser e2e, only when asked for.

A "Notes Lite" speed check (crun_3a39cd19) asked for an Express API, a static page and Vitest
tests. BOSS's own prompts and gates added Prometheus /metrics, a version endpoint, a Playwright
spec and the vite/@playwright runners to it: the observability standard fired because the
prose said "API", and every npm build was treated as a full web app. More code, more tests,
more ways to fail. The build ran four rounds and 35 minutes on a tiny app.

The rule, decided ONCE per run and read by every prompt and gate:

- ``ops`` (the observability standard: /metrics, health, version) when the brief or criteria
  ask for it, the run belongs to a workflow that deploys or monitors, or the app declares a
  ``boss.deploy.yaml``. BOSS scrapes what it deploys, so a deployed app still gets it.
- ``e2e`` (Playwright and its runners) when the brief or criteria ask for browser/e2e tests.
- ``bundler`` (vite as a required runner) when the brief names a bundled front end.

Outside a build (no scope set) every caller keeps its previous behaviour.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any, Iterable, Optional

_SCOPE: ContextVar[Optional[dict[str, bool]]] = ContextVar("boss_build_scope", default=None)

_OPS_WORDS = re.compile(
    r"\b(metrics?|observability|prometheus|opentelemetry|monitor(ing|ed)?|healthz|readyz|"
    r"health ?checks?|deploy(ed|ment|s)?|production|sre)\b", re.I)
_E2E_WORDS = re.compile(
    r"\b(playwright|e2e|end[- ]to[- ]end|browser tests?|test:e2e|ui tests?)\b", re.I)
_BUNDLER_WORDS = re.compile(r"\b(vite|react|vue|svelte|next(\.js)?|tailwind|spa)\b", re.I)
#: Workflow capabilities after which BOSS runs and watches the app.
_OPS_CAPABILITIES = frozenset({"deploy", "monitor", "smoke_test", "preview_deploy"})


def decide(title: str, instructions: str, criteria: Iterable[str] = (), *,
           workflow_deploys: bool = False, has_manifest: bool = False) -> dict[str, bool]:
    text = "\n".join([title or "", instructions or "", *[str(c) for c in criteria or ()]])
    return {
        "ops": bool(workflow_deploys or has_manifest or _OPS_WORDS.search(text)),
        "e2e": bool(_E2E_WORDS.search(text)),
        "bundler": bool(_BUNDLER_WORDS.search(text)),
    }


def workflow_deploys(connection: Any, workflow_run_id: Optional[str]) -> bool:
    """Does the workflow this build belongs to deploy or monitor what it builds? Never raises."""
    if not workflow_run_id:
        return False
    try:
        from sqlalchemy import text

        rows = connection.execute(text(
            "SELECT COALESCE(s.binding_snapshot->>'capability', s.step_id) AS cap "
            "FROM workflow_step_runs s WHERE s.workflow_run_id = :r"),
            {"r": workflow_run_id}).scalars().all()
        return any(str(c or "") in _OPS_CAPABILITIES for c in rows)
    except Exception:  # noqa: BLE001 — unknown means "ask the brief", not a failed build
        return False


def set_scope(scope: Optional[dict[str, bool]]):
    return _SCOPE.set(scope)


def current() -> Optional[dict[str, bool]]:
    return _SCOPE.get()


def wants_ops(default: bool) -> bool:
    scope = _SCOPE.get()
    return default if scope is None else bool(scope.get("ops"))


def wants_e2e(default: bool = True) -> bool:
    scope = _SCOPE.get()
    return default if scope is None else bool(scope.get("e2e"))


def web_runners(default: tuple[str, ...] = ("vite", "@playwright/test")) -> tuple[str, ...]:
    """The runners a web app's package.json must declare: Playwright only when e2e was asked."""
    if _SCOPE.get() is None:
        return default
    scope = _SCOPE.get() or {}
    keep = {"@playwright/test": wants_e2e(), "vite": bool(scope.get("bundler"))}
    return tuple(r for r in default if keep.get(r, True))
