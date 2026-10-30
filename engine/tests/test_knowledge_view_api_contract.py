"""Covers the Graph area's read-route contract (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §3; BOSSAPP-768).

The HTTP routes live in ``apps/api/app/routers/knowledge_view.py`` and are exercised end to end
over the wire in ``apps/api/tests/test_knowledge_view_api.py``. That suite cannot run under the
code-task gate: its conftest refuses the build-services database (boss.verify.yaml says so on
purpose), so apps/api is not a verify suite. This engine test is where the routes' logic is
pinned under a suite the gate DOES run — it imports the one module the routes delegate to,
``boss_engine.services.knowledge.view``, and asserts the pure building blocks the wire shape and
each route's parameter validation are built from, so a one-line change to any of them is caught.
"""
from __future__ import annotations

import pytest

from boss_engine.services.knowledge import view


def test_parse_layers_accepts_only_the_three_chips():
    assert view.parse_layers(None) == view.DEFAULT_LAYERS
    assert view.parse_layers("") == view.DEFAULT_LAYERS
    assert view.parse_layers("   ") == view.DEFAULT_LAYERS
    assert view.parse_layers("code,rules") == frozenset({"code", "rules"})
    assert view.parse_layers(["learning"]) == frozenset({"learning"})
    # An unknown chip is refused in words (the route maps this ValueError to 422).
    with pytest.raises(ValueError) as unknown:
        view.parse_layers("code,secrets")
    assert "unknown layer secrets" in str(unknown.value)
    # `domain` is a real internal product layer but a reserved, non-selectable chip: it is
    # refused like any other unknown name, never silently accepted.
    with pytest.raises(ValueError):
        view.parse_layers("domain")


def test_kinds_of_unions_layers_and_drops_what_is_withheld():
    code = view.kinds_of(["code"])
    assert code and set(code) == set(view.CODE_KINDS)
    both = set(view.kinds_of(["code", "rules"]))
    assert both == set(view.CODE_KINDS) | set(view.RULE_KINDS)
    assert "rule" not in view.kinds_of(["rules"], withheld={"rule"})


def test_edge_label_is_singular_at_one_and_silent_for_a_lone_raw_edge():
    assert view.edge_label("calls", 1) == "1 call"
    assert view.edge_label("calls", 2) == "2 calls"
    assert view.edge_label("calls", 16749) == "16,749 calls"
    # A single raw (non-rollup) edge has nothing to say.
    assert view.edge_label("calls", 1, rollup=False) is None


def test_rule_status_prefers_attrs_then_lifecycle():
    assert view.rule_status({"attrs": {"status": "approved"}}) == "approved"
    assert view.rule_status({"valid_to": "2026-01-01"}) == "rejected"
    assert view.rule_status({"trust_tier": "approved"}) == "approved"
    assert view.rule_status({}) == "proposed"


def test_rollup_sub_never_says_zero_classes():
    assert view.rollup_sub(3, 0, 0, "package") == "3 classes"
    assert view.rollup_sub(1, 0, 0, "package") == "1 class"
    assert view.rollup_sub(0, 24, 0, "package") == "24 functions"
    assert view.rollup_sub(0, 0, 2, "area") == "2 packages"
    assert view.rollup_sub(0, 0, 2, "package") == "2 modules"
    assert view.rollup_sub(0, 0, 0, "area") == "empty"


def test_node_payload_renders_the_wire_node():
    method = view.node_payload({
        "id": "sym1", "kind": "symbol", "name": "bank.loan.Disbursement.disburse",
        "node_key": "k1", "attrs": {"symbol_kind": "method"}})
    assert method["label"] == "disburse()" and method["layer"] == "code"
    f = view.node_payload({"id": "f1", "kind": "file", "name": "loan/src/Disbursement.java",
                           "node_key": "k2", "path": "loan/src/Disbursement.java"})
    assert f["label"] == "Disbursement.java" and f["sub"] == "loan/src/Disbursement.java"


def test_decision_kinds_are_withheld_without_decision_read():
    assert view.KIND_PERMISSIONS["decision"] == "decision:read"
    assert view.KIND_PERMISSIONS["precedent"] == "decision:read"
    assert view.product_layer("rule") == "rules"
    assert view.product_layer("decision") == "learning"
