"""Every decision BOSS raises is classified, so its card offers only the buttons that work.

The decision action contract (boss_engine/services/decision_actions.py) first recognised three
refusal names and called everything else `general`. Review of the console stream found that
the refusals BOSS raises most never matched: a Factory build that ran out of model capacity
writes `restore_model_capacity_or_raise_budget` / `model_capacity_exhausted`, the budget
governor's tier-2 freeze writes `raise_cap_or_wait_for_headroom`, and the autoscaler writes
`increase_budget_or_defer_work`. They were ranked first in the Command Center as gates and
offered Approve, "Carries out what this card proposes", on a card nothing acts on. The test
that was meant to catch it checked one hand-picked sample per kind.

So this test does not sample. It reads every call in engine/ and apps/ that creates a decision,
works out the `action` each one writes (through wrappers, conditionals and f-strings), and fails
on any action the contract has not classified. It also reads DecisionService's own follow-on
handlers, so "approving carries this out" and "request changes is read" are facts taken from
the code rather than a list somebody keeps in step by hand.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import pytest

from boss_engine.services import decision_actions as da

ROOT = Path(__file__).resolve().parents[2]
#: Where decisions are raised. A Community export ships without some of these; what is there
#: is scanned.
SCAN_ROOTS = ("engine/boss_engine", "apps/api/app", "apps/worker")
#: Calls that create a decision. The two sinks are excluded by their own definitions below.
PRODUCER_CALLS = {"create_blocking_decision", "create_blocking", "open_step_decision"}
#: The sinks themselves: they pass `recommendation` straight through. open_step_decision is the
#: one way a step's decision is opened (ONE GATE, ONE RUN); it adds the run identity only.
SINKS = {
    ("engine/boss_engine/services/core.py", "create_blocking_decision"),
    ("engine/boss_engine/defaults/events.py", "create_blocking"),
    ("engine/boss_engine/services/run_decisions.py", "open_step_decision"),
}

#: A decision that writes no `action` at all. Each is listed with why it is safe: a new one
#: fails the test until somebody decides what its card offers.
NO_ACTION_BY_DESIGN = {
    # An SRE runbook approval; the runbook executor reads approved decisions of this kind.
    # General kind, Approve says it records the approval and starts nothing by itself.
    ("engine/boss_engine/services/sre_runbooks.py", "_hold_decision"),
    # A meeting's own decision items: the panel writes the question, and the answer is
    # recorded for the minutes. General kind, no follow-on.
    ("engine/boss_engine/services/canonical.py", "_create_meeting_decision"),
    # A live app secret awaiting a person (templates.secrets): Approve records the approval,
    # and a deploy reads it (_live_approved) before passing the live value. No follow-on.
    ("engine/boss_engine/templates/secrets.py", "_live_decision"),
    # An autonomous agent's Needs-you card (ambient/board.needs_you): the asker's own action
    # rides in the recommendation (a squad's proposal, ambient_route, incident_squad.mitigate)
    # and decision_actions.is_ambient_card classifies it by work_item_id + asked_by: the answer
    # is carried out by ambient/answers.on_answered (test_an_ambient_card_is_carried_out).
    ("engine/boss_engine/services/ambient/board.py", "needs_you"),
    # The same card asking about several items at once (the Chief of Staff's one card per
    # project per tick): carried by answers.on_answered → chief_of_staff.carry_card.
    ("engine/boss_engine/services/ambient/board.py", "needs_you_batch"),
    # The project's ONE ambient card (ambient/asks.raise_card): every always-on agent's ask —
    # the Chief of Staff's, a squad's, the SRE squad's — joins it; carried by
    # answers.on_answered → asks.carry_card per entry + chief_of_staff.carry_card.
    ("engine/boss_engine/services/ambient/asks.py", "raise_card"),
}

#: Functions that write a decision row themselves instead of through create_blocking_decision.
#: The scan finds every one by its SQL; this names them so a new one cannot appear unnoticed.
DIRECT_INSERTS = {
    ("engine/boss_engine/services/core.py", "_create_blocking_decision_postgres"),
    ("engine/boss_engine/services/self_improvement.py", "_create_decision"),
    ("engine/boss_engine/services/infrastructure.py", "_create_provision_review_decision"),
    ("engine/boss_engine/services/canonical.py", "_create_meeting_decision"),
    ("engine/boss_engine/services/role_governance.py", "_create_autonomy_change_decision"),
    ("engine/boss_engine/templates/secrets.py", "_live_decision"),
    # BOSS fixes itself: the install card (self_fix.install) and the incident a failed rollback
    # raises for a person (review_rollback_or_retry).
    ("engine/boss_engine/services/self_fix_install.py", "request_approval"),
    ("engine/boss_engine/services/self_fix_install.py", "_incident"),
}
#: The Postgres body of create_blocking_decision: its callers are the producers scanned above.
SINK_INSERTS = {("engine/boss_engine/services/core.py", "_create_blocking_decision_postgres")}


@dataclass
class Module:
    rel: str
    tree: ast.Module
    parents: dict = field(default_factory=dict)

    def enclosing_function(self, node: ast.AST) -> Optional[ast.FunctionDef]:
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None


def _modules() -> list[Module]:
    found = []
    for base in SCAN_ROOTS:
        root = ROOT / base
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.relative_to(ROOT).parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            module = Module(str(path.relative_to(ROOT)), tree)
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    module.parents[child] = parent
            found.append(module)
    return found


MODULES = _modules()


def _called_name(call: ast.Call) -> Optional[str]:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _params(func: ast.FunctionDef) -> list[str]:
    return [a.arg for a in func.args.posonlyargs + func.args.args]


def _kwonly(func: ast.FunctionDef) -> list[str]:
    return [a.arg for a in func.args.kwonlyargs]


@dataclass(frozen=True)
class Found:
    """One action a producer writes, or a reason it could not be read."""
    where: str
    action: Optional[str] = None   # a literal action
    prefix: Optional[str] = None   # an f-string action's literal start
    gate: bool = False             # no action, but a workflow_run_id: a workflow gate
    none: bool = False             # no action key at all
    unresolved: Optional[str] = None


def _argument(call: ast.Call, func: ast.FunctionDef, param: str) -> Optional[ast.expr]:
    """The expression a call passes for `param` of `func`, or None."""
    for kw in call.keywords:
        if kw.arg == param:
            return kw.value
    names = _params(func)
    if param not in names:
        return None
    index = names.index(param)
    # self.method(...) does not pass self positionally; Base.method(self, ...) does.
    explicit_self = bool(call.args) and isinstance(call.args[0], ast.Name) \
        and call.args[0].id in {"self", "cls"}
    if names and names[0] in {"self", "cls"} and isinstance(call.func, ast.Attribute) \
            and not explicit_self:
        index -= 1
    return call.args[index] if 0 <= index < len(call.args) else None


def _enclosing_class(module: Module, node: ast.AST) -> Optional[ast.ClassDef]:
    while node in module.parents:
        node = module.parents[node]
        if isinstance(node, ast.ClassDef):
            return node
    return None


def _subclass_modules(module: Module, func: ast.FunctionDef) -> list[Module]:
    """Modules holding a subclass (by name, transitively) of the class `func` is a method of:
    an inherited method is called as self.method(...) from the subclass's own module."""
    owner = _enclosing_class(module, func)
    if owner is None:
        return []
    names, found = {owner.name}, []
    grew = True
    while grew:
        grew = False
        for other in MODULES:
            for node in ast.walk(other.tree):
                if isinstance(node, ast.ClassDef) and node.name not in names and any(
                        isinstance(b, ast.Name) and b.id in names for b in node.bases):
                    names.add(node.name)
                    grew = True
                    if other not in found and other is not module:
                        found.append(other)
    return found


def _overrides(module: Module, func: ast.FunctionDef) -> bool:
    """Whether a method overrides one of its base classes' (it then also inherits their
    callers, which are scanned through the base)."""
    owner = _enclosing_class(module, func)
    bases = {b.id for b in (owner.bases if owner else []) if isinstance(b, ast.Name)}
    return any(isinstance(node, ast.ClassDef) and node.name in bases and any(
                   isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func.name
                   for n in node.body)
               for other in MODULES for node in ast.walk(other.tree))


def _callers(module: Module, func: ast.FunctionDef) -> Iterable[tuple[Module, ast.Call]]:
    """Calls of a wrapper. A method is looked for in its own module and its subclasses'; a
    function everywhere. A call inside the wrapper itself (an override calling its base) is
    not a caller of it."""
    method = bool(_params(func)) and _params(func)[0] in {"self", "cls"}
    for other in ([module, *_subclass_modules(module, func)] if method else MODULES):
        for node in ast.walk(other.tree):
            if isinstance(node, ast.Call) and _called_name(node) == func.name \
                    and not (other is module and module.enclosing_function(node) is func):
                yield other, node


def _where(module: Module, node: ast.AST) -> str:
    return f"{module.rel}:{getattr(node, 'lineno', '?')}"


def _resolve_action(module: Module, expr: ast.expr, func: Optional[ast.FunctionDef],
                    depth: int = 0) -> list[Found]:
    where = _where(module, expr)
    if depth > 4:
        return [Found(where, unresolved="wrapper chain too deep")]
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return [Found(where, action=expr.value)]
    if isinstance(expr, ast.IfExp):
        return (_resolve_action(module, expr.body, func, depth)
                + _resolve_action(module, expr.orelse, func, depth))
    # A subclass renaming its base's action: reason.replace("typed_decision", "architecture").
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) \
            and expr.func.attr == "replace" and len(expr.args) == 2 and all(
                isinstance(a, ast.Constant) and isinstance(a.value, str) for a in expr.args):
        old, new = expr.args[0].value, expr.args[1].value
        return [Found(f.where, action=f.action.replace(old, new) if f.action else None,
                      prefix=f.prefix.replace(old, new) if f.prefix else None, gate=f.gate,
                      none=f.none, unresolved=f.unresolved)
                for f in _resolve_action(module, expr.func.value, func, depth)]
    if isinstance(expr, ast.JoinedStr):
        head = expr.values[0] if expr.values else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str) and head.value:
            return [Found(where, prefix=head.value)]
        return [Found(where, unresolved=f"f-string action {ast.unparse(expr)}")]
    if isinstance(expr, ast.Name) and func is not None:
        if expr.id in _params(func) or expr.id in _kwonly(func):
            return _through_wrapper(module, func, expr.id, depth, _resolve_action)
        assigned = _assignments(func, expr.id, before=expr.lineno)
        if assigned:
            return [f for value in assigned
                    for f in _resolve_action(module, value, func, depth)]
        # A module constant (`DECISION_ACTION = "infra_operation"`), read where it is defined.
        constant = _module_constant(module, expr.id)
        if constant is not None:
            return [Found(where, action=constant)]
    return [Found(where, unresolved=f"action is {ast.unparse(expr)}")]


def _module_constant(module: Module, name: str) -> Optional[str]:
    """The string a top-level `NAME = "..."` (or `NAME: str = "..."`) binds, or None."""
    for node in module.tree.body:
        target, value = None, None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id == name \
                and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _may_carry_action(expr: ast.expr) -> bool:
    """Whether a `**spread` could put an "action" key in the dict. Only a literal dict (or a
    conditional choosing between literal dicts) without one is known not to."""
    if isinstance(expr, ast.Dict):
        return any(k is None and _may_carry_action(v) or
                   isinstance(k, ast.Constant) and k.value == "action"
                   for k, v in zip(expr.keys, expr.values))
    if isinstance(expr, ast.IfExp):
        return _may_carry_action(expr.body) or _may_carry_action(expr.orelse)
    return True


def _resolve_recommendation(module: Module, expr: Optional[ast.expr],
                            func: Optional[ast.FunctionDef], depth: int = 0) -> list[Found]:
    if expr is None:
        return [Found(_where(module, func or module.tree), unresolved="no recommendation")]
    where = _where(module, expr)
    if depth > 4:
        return [Found(where, unresolved="wrapper chain too deep")]
    if isinstance(expr, ast.Dict):
        keys = {k.value: v for k, v in zip(expr.keys, expr.values)
                if isinstance(k, ast.Constant)}
        if "action" in keys:
            # A `**spread` AFTER the literal action can replace it at run time, and the scan
            # would vouch for an action the decision may not carry. Review round 2 found the
            # workflow decision step written that way: a spec's recommendation.action replaced
            # resume_workflow.
            position = next(i for i, k in enumerate(expr.keys)
                            if isinstance(k, ast.Constant) and k.value == "action")
            for k, v in list(zip(expr.keys, expr.values))[position + 1:]:
                if k is None and _may_carry_action(v):
                    return [Found(where, unresolved=(
                        f"a spread after 'action' can replace it: **{ast.unparse(v)}"))]
            return _resolve_action(module, keys["action"], func, depth)
        if "workflow_run_id" in keys:
            return [Found(where, gate=True)]
        return [Found(where, none=True)]
    if isinstance(expr, ast.Name) and func is not None:
        if expr.id in _params(func) or expr.id in _kwonly(func):
            return _through_wrapper(module, func, expr.id, depth, _resolve_recommendation)
        assigned = _assignments(func, expr.id, before=expr.lineno)
        if assigned:
            return [f for value in assigned
                    for f in _resolve_recommendation(module, value, func, depth)]
    return [Found(where, unresolved=f"recommendation is {ast.unparse(expr)}")]


def _assignments(func: ast.FunctionDef, name: str, *, before: int) -> list[ast.expr]:
    """What `name` can hold where it is used: assignments above the use, leaving out one made
    in a branch that returns before reaching it (github_triage_agent reads an existing
    decision's recommendation that way and returns it, without raising a new one)."""
    exits: set[ast.AST] = set()
    for branch in ast.walk(func):
        if isinstance(branch, ast.If) and any(isinstance(s, ast.Return) for s in branch.body):
            inside = branch.body[0].lineno <= before <= (branch.body[-1].end_lineno or 0)
            if not inside:
                exits.update(n for s in branch.body for n in ast.walk(s))
    values = []
    for node in ast.walk(func):
        if getattr(node, "lineno", before) >= before or node in exits:
            continue
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name and node.value is not None:
            values.append(node.value)
    return values


def _through_wrapper(module, func, param, depth, resolve) -> list[Found]:
    found: list[Found] = []
    for caller_module, call in _callers(module, func):
        arg = _argument(call, func, param)
        if arg is None:
            continue
        found += resolve(caller_module, arg, caller_module.enclosing_function(call), depth + 1)
    if not found and _overrides(module, func):
        return found   # its callers are its base's, scanned through the base
    if not found:
        found.append(Found(_where(module, func),
                           unresolved=f"wrapper {func.name}({param}) has no callers"))
    return found


def _producer_findings() -> list[Found]:
    found: list[Found] = []
    for module in MODULES:
        for node in ast.walk(module.tree):
            if not (isinstance(node, ast.Call) and _called_name(node) in PRODUCER_CALLS):
                continue
            func = module.enclosing_function(node)
            if func is not None and (module.rel, func.name) in SINKS:
                continue
            rec = node.args[4] if len(node.args) > 4 else next(
                (kw.value for kw in node.keywords if kw.arg == "recommendation"), None)
            for item in _resolve_recommendation(module, rec, func):
                if item.none and func is not None \
                        and (module.rel, func.name) in NO_ACTION_BY_DESIGN:
                    continue
                if item.none and _called_name(node) == "open_step_decision":
                    # It writes the run and step itself: a workflow gate.
                    item = Found(item.where, gate=True)
                found.append(item)
    return found


def _direct_insert_functions() -> set[tuple[str, str]]:
    sites = set()
    for module in MODULES:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and "INSERT INTO decisions" in node.value:
                func = module.enclosing_function(node)
                sites.add((module.rel, func.name if func else "<module>"))
    return sites


def _direct_insert_findings() -> list[Found]:
    """The action literals a direct-INSERT producer writes, or its callers pass in."""
    found: list[Found] = []
    for module in MODULES:
        for func in ast.walk(module.tree):
            if not isinstance(func, ast.FunctionDef) \
                    or (module.rel, func.name) not in DIRECT_INSERTS \
                    or (module.rel, func.name) in NO_ACTION_BY_DESIGN \
                    or (module.rel, func.name) in SINK_INSERTS:
                continue
            if "recommendation" in _params(func) + _kwonly(func):
                found += _through_wrapper(module, func, "recommendation", 0,
                                          _resolve_recommendation)
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values):
                        if isinstance(k, ast.Constant) and k.value == "action" \
                                and not (isinstance(v, ast.Constant)
                                         and v.value in {"approve", "reject", "revise"}):
                            found += _resolve_action(module, v, func)
    return found


ALL_FINDINGS = _producer_findings() + _direct_insert_findings()


def _classified(item: Found) -> bool:
    if item.gate:
        return True
    if item.action is not None:
        return item.action in da.KNOWN_ACTIONS
    if item.prefix is not None:
        return item.prefix.startswith(da.RECORDED_ON_APPROVAL_PREFIXES)
    return False


def test_the_scan_sees_the_producers_the_review_named():
    """If the scan quietly found nothing it would pass for the wrong reason."""
    actions = {f.action for f in ALL_FINDINGS if f.action}
    for named in ("restore_model_capacity_or_raise_budget", "raise_cap_or_wait_for_headroom",
                  "increase_budget_or_defer_work", "review_pull_request", "resume_workflow",
                  "budget_denied", "model_capacity_denied", "role.autonomy_change",
                  "self_improvement.begin_canary", "review_infra_provisioning"):
        assert named in actions, f"the producer scan no longer sees {named}"
    assert len(ALL_FINDINGS) > 60


def test_a_spread_after_the_action_is_not_vouched_for():
    """Review round 2: the workflow decision step wrote {"action": "resume_workflow",
    **template["recommendation"], ...}, so a spec's own action replaced the gate's, and the
    scan, reading only the literal key, vouched for resume_workflow. A spread after the action
    is now unresolved unless it is a literal dict that cannot carry one."""
    def resolve(source: str) -> Found:
        tree = ast.parse(source)
        expr = tree.body[0].value
        [found] = _resolve_recommendation(Module("snippet.py", tree), expr, None)
        return found

    assert resolve('{"action": "resume_workflow", **spec}').unresolved
    assert resolve('{"action": "resume_workflow", **({"x": 1} if y else {"action": 2})}'
                   ).unresolved
    assert resolve('{"action": "resume_workflow", **({"x": 1} if y else {})}'
                   ).action == "resume_workflow"
    assert resolve('{**spec, "action": "resume_workflow"}').action == "resume_workflow"


def test_every_action_boss_writes_on_a_decision_is_classified():
    """A producer whose action is not classified falls through to `general` and gets buttons
    that may do nothing — the model-capacity refusal on boss-1 was exactly that."""
    problems = sorted(
        f"{f.where}: {f.unresolved or ('no action and no workflow_run_id' if f.none else f.action or f.prefix)}"
        for f in ALL_FINDINGS if f.unresolved or f.none or not _classified(f)
    )
    assert not problems, (
        "These places raise a decision whose action decision_actions does not classify. Add "
        "the action to REFUSALS, CARRIED_OUT_ON_APPROVAL, WORKFLOW_GATE_ACTIONS or "
        "RECORDED_ON_APPROVAL (or teach this scan the wrapper):\n  " + "\n  ".join(problems))


def test_every_function_that_writes_a_decision_row_directly_is_known():
    assert _direct_insert_functions() == DIRECT_INSERTS, (
        "A function writes INSERT INTO decisions itself. Add it to DIRECT_INSERTS so the "
        "action it writes is checked too.")


def test_no_classified_action_is_stale():
    """A name nobody writes any more is a classification nobody can check."""
    written = {f.action for f in ALL_FINDINGS if f.action}
    # Reason codes are matched as well as actions; they are written as the reason argument.
    stale = sorted(
        a for a in da.KNOWN_ACTIONS - written
        if a not in da.REFUSALS or not _reason_is_written(a))
    assert not stale, f"classified but never written by any producer: {stale}"


def _reason_is_written(reason: str) -> bool:
    for module in MODULES:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Constant) and node.value == reason:
                return True
    return False


def _compared_actions(method: str, variable: str) -> set[str]:
    """String literals a DecisionService method compares its recommendation action with."""
    path = ROOT / "engine/boss_engine/services/decisions.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == method)
    seen: set[str] = set()
    for node in ast.walk(func):
        if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == variable):
            continue
        for comparator in node.comparators:
            for leaf in ast.walk(comparator):
                if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                    seen.add(leaf.value)
    return seen


def test_approve_says_it_carries_out_exactly_what_the_follow_on_carries_out():
    """`Carries out what this card proposes` is true only where _handle_approved_follow_on
    acts. The set is read from that function, not kept in step by hand."""
    assert _compared_actions("_handle_approved_follow_on", "action") \
        == set(da.CARRIED_OUT_ON_APPROVAL)


def test_request_changes_is_offered_exactly_where_something_reads_it():
    """A pull request sent back with "Request changes" could never be merged from BOSS:
    nothing read revision_requested for it and the status refused approval."""
    assert _compared_actions("_handle_non_approved_follow_on", "recommendation_action") \
        == set(da.REVISE_CONSUMED)
    for action in sorted(da.KNOWN_ACTIONS):
        card = {"status": "pending", "recommendation": {"action": action}}
        offers_revise = "revise" in {a["action"] for a in da.allowed_actions(card)}
        assert offers_revise == (action in da.REVISE_CONSUMED), action


@pytest.mark.parametrize("action", sorted(da.KNOWN_ACTIONS))
def test_every_classified_action_gets_buttons_that_tell_the_truth(action):
    card = {"status": "pending", "recommendation": {"action": action}}
    kind = da.kind_of(card)
    offered = da.allowed_actions(card)
    assert offered, action
    if action in da.REFUSALS:
        assert kind == "refusal"
        assert [a["action"] for a in offered] == ["reject"]
        assert da.guidance(card)["what_to_do"]
        return
    approve = next(a for a in offered if a["action"] == "approve")
    if action in da.CARRIED_OUT_ON_APPROVAL or kind in {"workflow_gate", "deploy_gate"}:
        assert "starts nothing" not in approve["effect"]
    else:
        assert kind == "general"
        assert "Carries out" not in approve["effect"]
        assert "starts nothing" in approve["effect"]


def test_the_classification_sets_do_not_overlap():
    groups = [da.REFUSAL_REASONS, da.CARRIED_OUT_ON_APPROVAL, da.WORKFLOW_GATE_ACTIONS,
              da.RECORDED_ON_APPROVAL]
    for i, a in enumerate(groups):
        for b in groups[i + 1:]:
            assert not (a & b), a & b


def test_an_ambient_card_is_carried_out():
    card = {"recommendation": {"action": "start_workflow", "work_item_id": "work_1",
                               "asked_by": "agent:security_reviewer", "proposal_id": "wim_1"}}
    assert da.carries_out_on_approval(card) and da.consumes_revision(card)
    assert not da.carries_out_on_approval({"recommendation": {"action": "start_workflow"}})
