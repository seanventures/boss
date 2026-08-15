"""Factory quality loop — after tests go green, measure gaps and improve once.

This is not a second product scorecard. It feeds the existing v1.1 benchmark
scorecard with workspace evidence and, when that evidence is thin, produces
one bounded improve-pass prompt. Tests stay frozen. Decisions stay human.

Hard rules:
  * Missing evidence stays not_scored. Never invent UI/UX scores.
  * At most one quality pass after a green authoritative test run.
  * Quality pass must not edit frozen test files.
  * If the quality pass breaks tests, the caller should revert to the last
    green commit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .benchmark_apps import (
    SCORECARD_VERSION,
    benchmark_scorecard,
    completion_report,
)

REQUIRED_UI_STATES = ("loading", "empty", "error", "ok")
UX_RUBRIC_KEYS = ("task_success", "clarity", "trust", "delight", "accessibility")
MAX_QUALITY_PASSES = 1
QUALITY_SCORE_FLOOR = 70

_TEST_HINT = re.compile(
    r"(test|spec|e2e|playwright|cypress|vitest)",
    re.I,
)
_A11Y_HINT = re.compile(r"\b(aria-|role=|alt=|axe|a11y|sr-only|visually-hidden)\b", re.I)
_STATE_HINTS = {
    "loading": re.compile(r"\b(loading|spinner|skeleton)\b", re.I),
    "empty": re.compile(r"\b(empty[- ]state|no [a-z]+ yet|nothing here)\b", re.I),
    "error": re.compile(r"\b(error[- ]state|went wrong|try again|aria-invalid)\b", re.I),
    "ok": re.compile(r"\b(success|saved|created|dashboard)\b", re.I),
}


def collect_workspace_evidence(files: dict[str, str]) -> dict[str, Any]:
    """Deterministic evidence from path→content. Pure. Never spends."""
    paths = list(files)
    contents = "\n".join(files.values())
    test_paths = [p for p in paths if _is_real_test_path(p)]
    e2e_paths = [p for p in test_paths if re.search(r"e2e|playwright", p, re.I)]
    api_test_paths = [p for p in test_paths if re.search(r"api|request|route", p, re.I)]
    pkg = _package_scripts(files)
    states = sorted(
        name for name, pattern in _STATE_HINTS.items() if pattern.search(contents)
    )
    a11y_hits = len(_A11Y_HINT.findall(contents))
    ui_evidence = {
        "a11y_markers": a11y_hits,
        "states_covered": states,
        "has_a11y_script": any("a11y" in s or "axe" in s for s in pkg),
    }
    ux_evidence = {
        "jtbd": _jtbd_from_tests(files, test_paths),
        "rubric": {},
        "has_e2e": bool(e2e_paths) or any("test:e2e" in s for s in pkg),
    }
    capability_evidence = {
        "scenario_mapping": [
            {"id": path, "tested": True, "passed": None}
            for path in test_paths[:24]
        ]
    }
    return {
        "ui_evidence": ui_evidence,
        "ux_evidence": ux_evidence,
        "capability_evidence": capability_evidence,
        "interface_evidence": {"states_covered": states + (["keyboard"] if a11y_hits else [])},
        "script_contract": _script_contract(files),
        "test_paths": test_paths,
        "e2e_paths": e2e_paths,
        "api_test_paths": api_test_paths,
        "scripts": pkg,
        "file_count": len(paths),
    }


def collect_workspace_evidence_from_root(
    root: Path, *, limit: int = 200, app_id: str | None = None
) -> dict[str, Any]:
    """Read a working tree. Skips caches / node_modules / .git / binaries.

    Official sandbox checkouts are a monorepo. When ``app_id`` matches a
    subdirectory (bugbox/, hookwatch/, approveit/), scan that tree plus
    top-level tests that mention the app — never the sibling canaries.
    """
    files: dict[str, str] = {}
    skip = {
        ".git",
        "node_modules",
        ".next",
        "dist",
        "coverage",
        "__pycache__",
        ".pytest_cache",
        "test-results",
        "playwright-report",
        ".turbo",
        ".cache",
    }
    if not root.is_dir():
        return collect_workspace_evidence({})
    scoped = root / app_id if app_id and (root / app_id).is_dir() else None
    for path in sorted(root.rglob("*")):
        if any(part in skip for part in path.parts):
            continue
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        if scoped is not None:
            try:
                path.relative_to(scoped)
                in_app = True
            except ValueError:
                in_app = False
            if not in_app:
                if not _is_real_test_path(rel):
                    continue
                if app_id and app_id.lower() not in rel.lower():
                    continue
        if len(files) >= limit:
            break
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > 80_000:
            text = text[:80_000]
        files[rel] = text
    return collect_workspace_evidence(files)


def bind_test_outcomes(evidence: dict[str, Any], *, tests_passed: bool | None) -> dict[str, Any]:
    """Attach observed verify outcome to JTBD rows. Never invents rubric scores."""
    if tests_passed is not True:
        return evidence
    ux = dict(evidence.get("ux_evidence") or {})
    rows = []
    changed = False
    for item in ux.get("jtbd") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        if row.get("passed") is None:
            row["passed"] = True
            changed = True
        rows.append(row)
    if not changed:
        return evidence
    ux["jtbd"] = rows
    out = dict(evidence)
    out["ux_evidence"] = ux
    mapping = []
    for item in (out.get("capability_evidence") or {}).get("scenario_mapping") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        if row.get("passed") is None and row.get("tested"):
            row["passed"] = True
        mapping.append(row)
    if mapping:
        cap = dict(out.get("capability_evidence") or {})
        cap["scenario_mapping"] = mapping
        out["capability_evidence"] = cap
    return out


def attach_evidence_to_run(run: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Copy collected evidence onto run state so the v1.1 scorecard can see it."""
    state = dict(run.get("state") or {})
    for key in ("ui_evidence", "ux_evidence", "capability_evidence", "interface_evidence"):
        blob = evidence.get(key)
        if isinstance(blob, dict) and blob:
            state[key] = blob
    out = dict(run)
    out["state"] = state
    return out


def score_run_with_workspace(run: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Scorecard v1.1 over a run plus workspace evidence. Pure."""
    enriched = attach_evidence_to_run(run, evidence)
    return completion_report(enriched).get("scorecard") or benchmark_scorecard(
        enriched,
        benchmark=(enriched.get("finalize_ctx") or {}).get("qa_benchmark") or {},
        verify_status=(
            "pass"
            if (enriched.get("state") or {}).get("tests_passed")
            else "not_recorded"
        ),
    )


def measured_quality_gaps(scorecard: dict[str, Any], *, floor: int = QUALITY_SCORE_FLOOR) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for item in scorecard.get("dimensions") or []:
        if not isinstance(item, dict):
            continue
        dim = item.get("id")
        if dim not in {"functionality", "capability", "interface", "ui", "ux", "quality"}:
            continue
        if item.get("status") == "not_scored":
            gaps.append({
                "dimension": dim,
                "status": "not_scored",
                "score": None,
                "reason": item.get("reason") or "missing evidence",
            })
        elif item.get("status") == "scored" and isinstance(item.get("score"), (int, float)):
            if int(item["score"]) < floor:
                gaps.append({
                    "dimension": dim,
                    "status": "scored",
                    "score": int(item["score"]),
                    "reason": f"below floor {floor}",
                })
    return gaps


def should_run_quality_pass(
    evidence: dict[str, Any],
    *,
    tests_passed: bool,
    quality_passes_used: int = 0,
) -> bool:
    """One extra pass only after green tests, when UI/UX/capability evidence is thin."""
    if not tests_passed:
        return False
    if quality_passes_used >= MAX_QUALITY_PASSES:
        return False
    ui = evidence.get("ui_evidence") or {}
    ux = evidence.get("ux_evidence") or {}
    states = set(ui.get("states_covered") or [])
    thin_ui = len(states) < 3
    thin_ux = not ux.get("has_e2e") and not (ux.get("jtbd") or [])
    thin_a11y = int(ui.get("a11y_markers") or 0) < 2 and not ui.get("has_a11y_script")
    return bool(thin_ui or thin_ux or thin_a11y)


def quality_improve_prompt(evidence: dict[str, Any], *, title: str, gaps: Iterable[dict[str, Any]] | None = None) -> str:
    """Bounded improve-pass. Do not touch frozen tests. Do not invent scores."""
    gap_ids = [str(item.get("dimension")) for item in (gaps or []) if item.get("dimension")]
    ui = evidence.get("ui_evidence") or {}
    states = ui.get("states_covered") or []
    missing_states = [name for name in REQUIRED_UI_STATES if name not in states]
    return (
        f"QUALITY IMPROVE PASS for {title!r}. Tests already pass. Do not restart the app.\n"
        "The scorecard is v1.1. UI craft and UX/task-success are scored only from evidence.\n"
        "Do NOT edit, delete, or weaken any existing test file. Add implementation only.\n"
        "Do NOT invent ui_evidence or ux_evidence numbers.\n"
        f"Observed UI states: {states or 'none'}. Missing states: {missing_states or 'none'}.\n"
        f"a11y markers seen: {ui.get('a11y_markers', 0)}. e2e present: "
        f"{bool((evidence.get('ux_evidence') or {}).get('has_e2e'))}.\n"
        f"Gaps: {', '.join(gap_ids) or 'thin UI/UX evidence'}.\n"
        f"Script contract stubs: {(evidence.get('script_contract') or {}).get('stubs') or 'none'}. "
        f"Missing runners: {(evidence.get('script_contract') or {}).get('missing_runners') or 'none'}.\n"
        "Make the smallest change that:\n"
        "1) Adds honest loading, empty, and error UI where those flows exist.\n"
        "2) Uses real labels / aria attributes on primary controls.\n"
        "3) Keeps contrast readable and hierarchy obvious.\n"
        "4) Leaves `npm run verify` (or the frozen test command) green.\n"
        "End with a one-line summary of files changed. Do not commit."
    )


def quality_pass_record(*, attempt: int, tests_passed: bool, reverted: bool, gaps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "quality_pass",
        "attempt": attempt,
        "tests_passed": bool(tests_passed),
        "reverted": bool(reverted),
        "gaps": [item.get("dimension") for item in gaps],
        "scorecard_version": SCORECARD_VERSION,
    }


def _package_scripts(files: dict[str, str]) -> list[str]:
    raw = files.get("package.json") or files.get("apps/web/package.json") or ""
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return []
    return [str(name) for name in scripts]


_STUB_SCRIPT = re.compile(
    r"""(?:^|[;&|])\s*(?:echo\s+|process\.exit\(0\))|skipping""",
    re.I,
)
_REQUIRED_RUNNERS = ("vite", "@playwright/test")


def _script_contract(files: dict[str, str]) -> dict[str, Any]:
    """Deterministic package.json contract. Never invents a pass."""
    raw = files.get("package.json") or files.get("apps/web/package.json") or ""
    empty = {"stubs": [], "missing_runners": [], "has_verify": False}
    if not raw:
        return empty
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return empty
    if not isinstance(payload, dict):
        return empty
    scripts = payload.get("scripts") if isinstance(payload.get("scripts"), dict) else {}
    deps = {}
    if isinstance(payload.get("dependencies"), dict):
        deps.update(payload["dependencies"])
    if isinstance(payload.get("devDependencies"), dict):
        deps.update(payload["devDependencies"])
    stubs = [
        name
        for name, cmd in scripts.items()
        if _STUB_SCRIPT.search(str(cmd or ""))
    ]
    missing = [name for name in _REQUIRED_RUNNERS if name not in deps]
    return {
        "stubs": stubs,
        "missing_runners": missing,
        "has_verify": bool(str(scripts.get("verify") or "").strip()),
    }


def _is_real_test_path(path: str) -> bool:
    """True for executable test files. Configs, caches, and testdata stay out."""
    text = path.replace("\\", "/")
    parts = {part.lower() for part in text.split("/")}
    if parts & {
        ".pytest_cache",
        "test-results",
        "playwright-report",
        "node_modules",
        ".git",
        "coverage",
        "testdata",
    }:
        return False
    if re.search(r"(?:playwright|vitest|jest|cypress)\.config\.", text, re.I):
        return False
    if text.endswith(".json"):
        return False
    return bool(
        _TEST_HINT.search(text)
        and text.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"))
    )


def _jtbd_from_tests(files: dict[str, str], test_paths: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in test_paths[:16]:
        text = files.get(path) or ""
        names = re.findall(r"(?:it|test|describe)\(\s*['\"]([^'\"]+)['\"]", text)
        for name in names[:8]:
            cleaned = name.strip()
            if not cleaned or cleaned in {";", "{", "}"}:
                continue
            out.append({"id": cleaned[:80], "source": path, "passed": None})
    return out
