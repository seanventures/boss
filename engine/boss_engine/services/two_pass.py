"""EVERY BUILD IS TWO PASSES, in every workflow: the rule, once, for the whole engine.

Operator, 2026-10-13: a workflow first builds the app cleanly with a lean test set covering only
the critical paths (P0: the app starts, sign-in/authz, the core happy path; P1: the main path of
each requested feature). Its PR and preview come first. A second pass ("More tests", P2: edge
cases, error paths, API contracts, browser tests) then extends the same branch and PR, and never
blocks pass 1. Connectors are pre-tested and never re-tested.

That rule was wired by hand into three catalogue workflows, so every other workflow that builds
code — the rest of the catalogue, and every workflow an operator or an agent drafts — shipped a
pass-1 suite and never got pass 2. So it is not written into workflows any more: it is applied
to a workflow definition when it is resolved for a run (`workflows._published_spec`), and to the
catalogue when it is constructed (`workflow_catalogue`), by `with_more_tests` below.

THE RULE. Every `build` capability step is followed by a `more_tests` step:

- after the build's preview when one follows it (straight on, before anything ships), so the
  PR and the preview come first; otherwise straight after the build;
- every later step that read the build's `code_task_run_id` (deploy, readiness…) reads the
  More tests step's instead, so what ships is the last pass's branch. A failed pass 2 reports
  the build's own run id there (workflow_capability_runner._complete_advisory_failure), so this
  never ships nothing;
- idempotent: a build already followed by a `more_tests` step is left as it is.

OPTING OUT, OR LIGHTER. `"more_tests": false` on the workflow spec (or on one build step) keeps
the build single-pass; `"more_tests": "light"` asks for a lighter pass 2 (regression tests
around the change and its error paths, no browser suite, pass 1's budget rather than twice it).
"""
from __future__ import annotations

import copy
from typing import Any, Optional

#: The capability that builds code. `retry_build` re-runs a build that already has its pass 2
#: after it; `security_fix` closes reviewed findings inside the security review's own loop.
BUILD_CAPABILITY = "build"
MORE_TESTS_CAPABILITY = "more_tests"
#: Where a preview of the build sits, so pass 2 starts once the operator can look at the app.
_PREVIEW_CAPABILITIES = frozenset({"preview"})
#: Steps that ship or change what was built: pass 2 goes before them, never after.
_SHIPS = frozenset({"deploy", "redeploy", "plrr", "retry_build", BUILD_CAPABILITY})
#: The opt-out / lighter values (`more_tests` on the spec or on a build step).
LIGHT = "light"


def _capability(step: dict[str, Any]) -> str:
    if step.get("kind") != "capability":
        return ""
    binding = step.get("binding")
    return str(binding.get("capability") or "") if isinstance(binding, dict) else ""


def _targets(step: dict[str, Any]) -> list[str]:
    """Every step this one can lead to (the same edges as workflows._targets)."""
    out: list[str] = []
    nxt = step.get("next", [])
    out.extend([nxt] if isinstance(nxt, str) else [str(t) for t in nxt or []])
    if step.get("kind") == "condition":
        out.extend([str(step.get("on_true") or ""), str(step.get("on_false") or "")])
    elif step.get("kind") == "parallel":
        out.extend(str(b) for b in step.get("branches") or [])
        if step.get("join"):
            out.append(str(step["join"]))
    elif step.get("kind") == "bounded_loop" and step.get("body"):
        out.append(str(step["body"]))
    return [t for t in out if t]


def _straight_on(by_id: dict[str, dict[str, Any]], start: dict[str, Any]) -> list[dict[str, Any]]:
    """The steps that follow `start` one after another, until the path branches or ends."""
    chain: list[dict[str, Any]] = []
    seen = {str(start.get("id"))}
    current = start
    while True:
        targets = _targets(current)
        if len(targets) != 1 or targets[0] in seen or targets[0] not in by_id:
            return chain
        current = by_id[targets[0]]
        seen.add(str(current.get("id")))
        chain.append(current)


def _reachable(by_id: dict[str, dict[str, Any]], start_id: str, *,
               stop_at_builds: bool = False) -> set[str]:
    """Every step reachable from `start_id`; with `stop_at_builds`, not past another build."""
    seen: set[str] = set()
    stack = [start_id]
    while stack:
        sid = stack.pop()
        for target in _targets(by_id.get(sid) or {}):
            if target not in seen and target in by_id:
                seen.add(target)
                if not (stop_at_builds and _capability(by_id[target]) == BUILD_CAPABILITY):
                    stack.append(target)
    return seen


def _depth(spec: dict[str, Any], build: dict[str, Any]) -> Optional[str]:
    """None = no pass 2; "full" or "light" otherwise. A build step's own setting wins."""
    for value in (build.get("more_tests"), spec.get("more_tests")):
        if value is False or value == "false":
            return None
        if value == LIGHT:
            return LIGHT
        if value is not None:
            return "full"
    return "full"


def _free_id(taken: set[str], build_id: str) -> str:
    base = MORE_TESTS_CAPABILITY if MORE_TESTS_CAPABILITY not in taken \
        else f"{build_id}_{MORE_TESTS_CAPABILITY}"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def builds_without_more_tests(spec: dict[str, Any]) -> list[str]:
    """The build steps in `spec` that are owed a pass 2 and have none after them."""
    steps = [s for s in (spec.get("steps") or []) if isinstance(s, dict) and s.get("id")]
    by_id = {str(s["id"]): s for s in steps}
    missing = []
    for step in steps:
        if _capability(step) != BUILD_CAPABILITY or _depth(spec, step) is None:
            continue
        if not any(_capability(by_id[t]) == MORE_TESTS_CAPABILITY
                   for t in _reachable(by_id, str(step["id"]), stop_at_builds=True)):
            missing.append(str(step["id"]))
    return missing


def with_more_tests(spec: Any, *, captain: Optional[str] = None) -> Any:
    """`spec` with a More tests step after every build owed one (see the module docstring).

    Returns a new spec; the argument is not changed. A spec that is not a workflow (no steps
    list), or whose builds all have their pass 2, comes back equal to what went in. `captain`
    names a soft default agent on the inserted steps (the catalogue's; a run leaves it to Auto).
    """
    if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list):
        return spec
    owed = builds_without_more_tests(spec)
    if not owed:
        return spec
    out = copy.deepcopy(spec)
    steps: list[dict[str, Any]] = out["steps"]
    for build_id in owed:
        by_id = {str(s["id"]): s for s in steps if isinstance(s, dict) and s.get("id")}
        build = by_id[build_id]
        anchor = build
        for step in _straight_on(by_id, build):
            cap = _capability(step)
            if cap in _SHIPS:
                break
            if cap in _PREVIEW_CAPABILITIES:
                anchor = step
                break
        new_id = _free_id(set(by_id), build_id)
        inputs: dict[str, Any] = {"run_id": {"from_step": build_id, "field": "code_task_run_id"}}
        if _depth(out, build) == LIGHT:
            inputs["depth"] = LIGHT
        nxt = anchor.get("next", [])
        new_step: dict[str, Any] = {
            "id": new_id,
            "kind": "capability",
            "binding": {"capability": MORE_TESTS_CAPABILITY, "inputs": inputs},
            "next": [nxt] if isinstance(nxt, str) else list(nxt or []),
        }
        if captain:
            new_step["captain"] = captain
            new_step["default_captain"] = captain
        anchor["next"] = [new_id]
        steps.insert(steps.index(anchor) + 1, new_step)
        by_id[new_id] = new_step
        # WHAT SHIPS IS THE LAST PASS'S BRANCH: later readers of the build's run read pass 2's.
        built = {"from_step": build_id, "field": "code_task_run_id"}
        for sid in _reachable(by_id, new_id):
            binding = by_id[sid].get("binding")
            wired = binding.get("inputs") if isinstance(binding, dict) else None
            if isinstance(wired, dict):
                for name, value in list(wired.items()):
                    if value == built:
                        wired[name] = {"from_step": new_id, "field": "code_task_run_id"}
    return out


def added_step_ids(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    """The step ids `with_more_tests` added."""
    def ids(s: Any) -> set[str]:
        return {str(x.get("id")) for x in (s or {}).get("steps") or [] if isinstance(x, dict)}
    return ids(after) - ids(before)
