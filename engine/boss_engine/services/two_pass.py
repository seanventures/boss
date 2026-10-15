"""EVERY BUILD IS TWO PASSES, in every workflow, and THE APP NEVER WAITS FOR PASS 2.

Operator, 2026-10-13: a workflow first builds the app cleanly with a lean test set covering only
the critical paths (P0: the app starts, sign-in/authz, the core happy path; P1: the main path of
each requested feature). A second pass ("More tests", P2: edge cases, error paths, API
contracts, browser tests) then extends the same branch and PR. Connectors are pre-tested.

Then, the same day: "for the user to see the app running, does it need to wait for P2?" It did.
Pass 2 sat in FRONT of everything after pass 1, so a workflow with no preview showed nothing until
P2 ended, and every deploy read the More tests run, so even staging waited. The rule now:

NO DEPLOY WAITS FOR PASS 2, IN ANY ENVIRONMENT.

    build ─▶ preview ─▶ [readiness] ─▶ pass_2 ═╦═▶ more_tests ─────────────────╗
                                                ╚═▶ … deploy (pass 1) … ───────╩═▶ pass_2_joined ─▶ deploy again (pass 2)

- Right after pass 1 the app is visible: its preview. A workflow with no preview gets one
  (`advisory`: if the app cannot be previewed the step says so and the run goes on), unless its
  deploy goes to an environment whose policy needs nobody, which is where the app is seen first.
- Pass 1's own finishing work (the preview, PLRR's readiness repairs) stays in front, because it
  writes the same branch. Then a `parallel` step runs More tests ALONGSIDE the rest of the
  workflow: the deploy ships pass 1's run under the environment's normal policy (staging by
  policy; an environment that asks still asks a person, for the pass-1 deploy).
- A `join` waits for both, then "Deploy again" (a `deploy` step with `pass_2_of`) ships the
  More tests run as a NEW governed deploy under the same policy — production asks again, and its
  card says what pass 2 added or fixed. If pass 2 failed or changed nothing, or the first deploy
  never happened, it does not deploy and says why (workflow_capability_runner).
- Anything after the join (a later build, readiness that writes the branch) starts from the
  More tests run, so it never races pass 2 on the same branch.

Applied to a workflow definition when it is resolved for a run (`workflows._published_spec`) and
to the catalogue when it is constructed (`workflow_catalogue`). A run keeps the shape it started
with (`workflows._run_spec`), including runs started under the rc10.2 inline shape.

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
#: The rc10.2 inline shape's anchors (with_more_tests_inline).
_PREVIEW_CAPABILITIES = frozenset({"preview"})
_SHIPS = frozenset({"deploy", "redeploy", "plrr", "retry_build", BUILD_CAPABILITY})
#: The opt-out / lighter values (`more_tests` on the spec or on a build step).
LIGHT = "light"


def _capability(step: dict[str, Any]) -> str:
    if step.get("kind") != "capability":
        return ""
    binding = step.get("binding")
    return str(binding.get("capability") or "") if isinstance(binding, dict) else ""


def _targets(step: dict[str, Any]) -> list[str]:
    """Every step this one can lead to, its error path included: the runtime's own edges
    (workflows._targets), read from there so the two never drift."""
    from . import workflows

    return workflows._targets(step)  # noqa: SLF001 — the one definition of a step's edges


def _success_targets(step: dict[str, Any]) -> list[str]:
    """The steps this one leads to when it does NOT fail (workflows._success_targets). Pass 2
    follows a build that succeeded, so the straight walks below follow these."""
    from . import workflows

    return workflows._success_targets(step)  # noqa: SLF001


def _has_error_path(step: dict[str, Any]) -> bool:
    return _targets(step) != _success_targets(step)


def _straight_on(by_id: dict[str, dict[str, Any]], start: dict[str, Any]) -> list[dict[str, Any]]:
    """The steps that follow `start` one after another, until the path branches or ends."""
    chain: list[dict[str, Any]] = []
    seen = {str(start.get("id"))}
    current = start
    while True:
        targets = _success_targets(current)
        if len(targets) != 1 or targets[0] in seen or targets[0] not in by_id:
            return chain
        current = by_id[targets[0]]
        seen.add(str(current.get("id")))
        chain.append(current)


def _reachable(by_id: dict[str, dict[str, Any]], start_id: str, *,
               stop_at_builds: bool = False, on_success: bool = False) -> set[str]:
    """Every step reachable from `start_id`, error paths included; with `stop_at_builds`, not
    past another build; with `on_success`, only along the edges a step takes when it succeeds."""
    edges = _success_targets if on_success else _targets
    seen: set[str] = set()
    stack = [start_id]
    while stack:
        sid = stack.pop()
        for target in edges(by_id.get(sid) or {}):
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
        # Pass 2 follows the build when it succeeds: a More tests only its error path reaches
        # is not it.
        if not any(_capability(by_id[t]) == MORE_TESTS_CAPABILITY
                   for t in _reachable(by_id, str(step["id"]), stop_at_builds=True,
                                       on_success=True)):
            missing.append(str(step["id"]))
    return missing


#: Pass 1's own finishing work: it writes or shows the same branch, so pass 2 comes after it.
_PASS_ONE_TAIL = frozenset({"preview", "plrr"})
#: Steps that write the branch: never alongside pass 2, which is writing it too.
_WRITES_BRANCH = frozenset({BUILD_CAPABILITY, "retry_build", "plrr", "security_fix",
                            MORE_TESTS_CAPABILITY})
_SHIPS_NOW = frozenset({"deploy", "redeploy"})
_CONTROL = frozenset({"parallel", "join", "condition", "bounded_loop", "for_each",
                      "subworkflow"})


def _free(taken: set[str], *candidates: str) -> str:
    for candidate in candidates:
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    base, n = candidates[-1], 2
    while f"{base}_{n}" in taken:
        n += 1
    taken.add(f"{base}_{n}")
    return f"{base}_{n}"


def _named(taken: set[str], build_id: str, word: str) -> str:
    """`preview`, `more_tests`…; `inc2_preview` beside a build called `inc2_build`, so a
    group of steps keeps one prefix; `<build>_<word>` when the plain name is taken."""
    first = f"{build_id[:-len('_build')]}_{word}" if build_id.endswith("_build") else word
    return _free(taken, first, f"{build_id}_{word}")


def _titled(build: dict[str, Any], words: str) -> dict[str, Any]:
    """"Increment 2 · More tests" beside "Increment 2 · Build": a build whose title names its
    group lends that name to the steps the rule adds, so they read as part of it."""
    title = str(build.get("title") or "")
    return {"title": f"{title.split(' · ')[0]} · {words}"} if " · " in title else {}


def _staffed(step: dict[str, Any], captains: Optional[dict[str, str]]) -> dict[str, Any]:
    role = (captains or {}).get(_capability(step))
    if role:
        step["captain"] = role
        step["default_captain"] = role
    return step


def _built(build_id: str) -> dict[str, str]:
    return {"from_step": build_id, "field": "code_task_run_id"}


def with_more_tests(spec: Any, *, captains: Optional[dict[str, str]] = None) -> Any:
    """`spec` with pass 2 alongside every build owed one (see the module docstring).

    Returns a new spec; the argument is not changed. A spec that is not a workflow (no steps
    list), or whose builds all have their pass 2, comes back equal to what went in. `captains`
    names soft default agents on the inserted steps by capability (the catalogue's; a run leaves
    them to Auto).
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
        taken = set(by_id)
        build = by_id[build_id]

        def insert_after(anchor: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
            nxt = anchor.get("next", [])
            new.setdefault("next", [nxt] if isinstance(nxt, str) else list(nxt or []))
            anchor["next"] = [new["id"]]
            steps.insert(steps.index(anchor) + 1, new)
            by_id[new["id"]] = new
            return new

        # 1. PASS 1, THEN SOMETHING TO LOOK AT. The straight run of steps after the build, up to
        # whatever ships it or writes the branch again.
        tail: list[dict[str, Any]] = []
        for step in _straight_on(by_id, build):
            if _capability(step) in _SHIPS_NOW or _capability(step) == BUILD_CAPABILITY \
                    or step.get("kind") in _CONTROL:
                break
            tail.append(step)
        if not any(_capability(s) == "preview" for s in tail):
            insert_after(build, _staffed({
                "id": _named(taken, build_id, "preview"),
                "kind": "capability", **_titled(build, "Preview"),
                "binding": {"capability": "preview",
                            "inputs": {"run_id": _built(build_id), "advisory": True}},
            }, captains))
            tail = [by_id[build["next"][0]]] + tail
        anchor = build
        for step in tail:
            if _capability(step) in _PASS_ONE_TAIL:
                anchor = step

        inputs: dict[str, Any] = {"run_id": _built(build_id)}
        if _depth(out, build) == LIGHT:
            inputs["depth"] = LIGHT
        more = _staffed({
            "id": _named(taken, build_id, MORE_TESTS_CAPABILITY),
            "kind": "capability", **_titled(build, "More tests"),
            "binding": {"capability": MORE_TESTS_CAPABILITY, "inputs": inputs},
        }, captains)

        # 2. WHAT RUNS ALONGSIDE: the straight steps after pass 1 that neither write the branch
        # nor branch themselves.
        # A step other paths also lead to (a report several branches end at, or a step some
        # step's error path goes to) is where the workflow meets again, never part of one
        # build's pass-2 branch.
        entered: dict[str, int] = {}
        for each in steps:
            for target in _targets(each):
                entered[target] = entered.get(target, 0) + 1
        alongside: list[dict[str, Any]] = []
        after: list[str] = []
        current = anchor
        while True:
            targets = _success_targets(current)
            if len(targets) != 1 or targets[0] not in by_id:
                break
            following = by_id[targets[0]]
            # A step with an error path may leave along it and never reach the join, so it
            # stays out of the branch the join waits for (it runs after pass 2 instead, where
            # its error path works as drawn).
            if _capability(following) in _WRITES_BRANCH or following.get("kind") in _CONTROL \
                    or len(_targets(following)) > 1 or _has_error_path(following) \
                    or entered.get(str(following["id"]), 0) > 1:
                after = [str(following["id"])]
                break
            alongside.append(following)
            current = following
        if not alongside:
            # Nothing to run beside it: pass 2 goes straight on, before what writes the branch.
            insert_after(anchor, more)
            continue

        joined_id = _named(taken, build_id, "pass_2_joined")
        fork = {
            "id": _named(taken, build_id, "pass_2"), "kind": "parallel",
            **_titled(build, "Pass 2 alongside"),
            "branches": [more["id"], str(alongside[0]["id"])], "join": joined_id, "next": [],
        }
        anchor["next"] = [fork["id"]]
        steps.insert(steps.index(anchor) + 1, fork)
        more["next"] = [joined_id]
        steps.insert(steps.index(fork) + 1, more)
        last = alongside[-1]
        last["next"] = [joined_id]
        join = {"id": joined_id, "kind": "join", **_titled(build, "Pass 2 done"),
                "wait_for": [more["id"], str(last["id"])], "success_policy": "all", "next": []}
        steps.insert(steps.index(last) + 1, join)
        by_id.update({fork["id"]: fork, more["id"]: more, joined_id: join})

        # 3. DEPLOY AGAIN, FROM PASS 2: one per deploy that shipped pass 1, under its policy.
        tip = join
        for shipped in [s for s in alongside if _capability(s) == "deploy"]:
            wired = dict((shipped.get("binding") or {}).get("inputs") or {})
            again: dict[str, Any] = {
                "id": _free(taken, f"{shipped['id']}_again"),
                "kind": "capability",
                "title": (f"{str(shipped['title']).split(' · ')[0]} · Deploy again"
                          if " · " in str(shipped.get("title") or "")
                          else "Deploy again (More tests)"),
                "binding": {"capability": "deploy", "inputs": {
                    **wired, "run_id": {"from_step": more["id"], "field": "code_task_run_id"},
                    "pass_2_of": str(shipped["id"])}},
                "next": [],
            }
            for key in ("approval", "captain", "default_captain"):
                if key in shipped:
                    again[key] = copy.deepcopy(shipped[key])
            tip["next"] = [again["id"]]
            steps.insert(steps.index(tip) + 1, again)
            by_id[again["id"]] = again
            tip = again
        tip["next"] = list(after)
        # Whatever comes after the join builds on the newest branch: pass 2's.
        for sid in _reachable(by_id, joined_id):
            binding = by_id[sid].get("binding")
            wired = binding.get("inputs") if isinstance(binding, dict) else None
            if isinstance(wired, dict) and not wired.get("pass_2_of"):
                for name, value in list(wired.items()):
                    if value == _built(build_id):
                        wired[name] = {"from_step": more["id"], "field": "code_task_run_id"}
    return out


def with_more_tests_inline(spec: Any, *, captain: Optional[str] = None) -> Any:
    """THE rc10.2 SHAPE, kept only so a run started under it keeps it (workflows._run_spec):
    More tests inline after the build (or its preview), and later readers of the build's run
    rebound to it, so the deploy waited for pass 2. New runs get `with_more_tests`.

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
