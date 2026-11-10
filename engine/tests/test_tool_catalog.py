"""A tool's schema and description are hashed, and a hash change quarantines it.

The digest is canonical JSON, so key order cannot launder a changed schema. A
first sight is not a change. A tool that disappears is not quarantined here.
A changed tool is marked ask on a copy of the observed list, and that mark is
what a reachable probe turns into a decision. The console is a later slice.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json

from boss_engine.services.mcp_registry import _tool_summary, record_reachability
from boss_engine.services.tool_catalog import (
    apply_quarantine,
    changed_tool_decisions,
    compare_catalog,
    description_sha256,
    schema_sha256,
)


def _canonical(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_schema_sha256_is_the_canonical_json_digest():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    assert schema_sha256(schema) == _canonical(schema)


def test_schema_key_order_does_not_change_the_hash():
    forward = {"type": "object", "properties": {"b": {"type": "number"}, "a": {"type": "string"}}}
    reverse = {"properties": {"a": {"type": "string"}, "b": {"type": "number"}}, "type": "object"}
    assert schema_sha256(forward) == schema_sha256(reverse) == _canonical(forward)


def test_a_different_schema_changes_the_hash():
    original = schema_sha256({"type": "object"})
    changed = schema_sha256({"type": "object", "required": ["path"]})
    assert changed != original


def test_a_missing_schema_hashes_json_null():
    assert schema_sha256(None) == _canonical(None)


def test_description_sha256_hashes_the_characters():
    text = "reads a file"
    assert description_sha256(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert description_sha256("café") == hashlib.sha256("café".encode("utf-8")).hexdigest()
    assert description_sha256(None) == hashlib.sha256(b"").hexdigest()


def test_tool_summary_carries_both_hashes_and_does_not_mutate():
    tool = {
        "name": "write_file",
        "description": "writes",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
    original = {
        "name": "write_file",
        "description": "writes",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
    summary = _tool_summary([tool, {"description": "nameless"}, "nope"])
    assert tool == original
    assert summary == [{
        "name": "write_file",
        "description": "writes",
        "inputSchema": original["inputSchema"],
        "schema_sha256": schema_sha256(original["inputSchema"]),
        "description_sha256": description_sha256("writes"),
    }]


def test_a_long_description_is_hashed_after_the_stored_cut():
    text = "x" * 600
    summary = _tool_summary([{"name": "read", "description": text, "inputSchema": None}])
    assert summary[0]["description"] == "x" * 500
    assert summary[0]["description_sha256"] == description_sha256("x" * 500)
    assert summary[0]["schema_sha256"] == schema_sha256(None)


def test_compare_catalog_quarantines_only_a_changed_same_named_tool():
    previous = _tool_summary([
        {"name": "write_file", "description": "writes", "inputSchema": {"type": "object"}},
        {"name": "read_file", "description": "reads", "inputSchema": {"type": "object"}},
        {"name": "gone", "description": "old", "inputSchema": None},
    ])
    observed = _tool_summary([
        {"name": "read_file", "description": "reads", "inputSchema": {"type": "object"}},
        {
            "name": "write_file",
            "description": "writes a file",
            "inputSchema": {"type": "object", "required": ["path"]},
        },
        {"name": "new_tool", "description": "fresh", "inputSchema": {"type": "object"}},
    ])
    before = [dict(item) for item in previous]
    found = compare_catalog(previous, observed)
    assert previous == before
    assert found == {
        "quarantined": [{
            "name": "write_file",
            "quarantine": "ask",
            "schema_changed": True,
            "description_changed": True,
            "previous_schema_sha256": previous[0]["schema_sha256"],
            "schema_sha256": observed[1]["schema_sha256"],
            "previous_description_sha256": previous[0]["description_sha256"],
            "description_sha256": observed[1]["description_sha256"],
        }]
    }


def test_an_unchanged_catalog_quarantines_nothing():
    tools = _tool_summary([
        {"name": "read_file", "description": "reads", "inputSchema": {"type": "object"}},
    ])
    assert compare_catalog(tools, [dict(tools[0])]) == {"quarantined": []}


def test_a_first_sight_with_no_previous_catalog_quarantines_nothing():
    observed = _tool_summary([
        {"name": "read_file", "description": "reads", "inputSchema": None},
    ])
    assert compare_catalog(None, observed) == {"quarantined": []}


def test_apply_quarantine_asks_only_the_named_tools_and_does_not_mutate():
    observed = [
        {"name": "write_file", "description": "writes", "inputSchema": {"type": "object"}},
        {"name": "read_file", "description": "reads", "inputSchema": {"type": "object"}},
    ]
    quarantined = [{
        "name": "write_file",
        "quarantine": "ask",
        "schema_changed": True,
        "description_changed": False,
    }]
    observed_before = copy.deepcopy(observed)
    quarantined_before = copy.deepcopy(quarantined)
    marked = apply_quarantine(observed, quarantined)
    assert observed == observed_before
    assert quarantined == quarantined_before
    assert marked is not observed
    assert marked[0] is not observed[0]
    assert marked[1] is not observed[1]
    assert marked == [
        {
            "name": "write_file",
            "description": "writes",
            "inputSchema": {"type": "object"},
            "quarantine": "ask",
        },
        {
            "name": "read_file",
            "description": "reads",
            "inputSchema": {"type": "object"},
        },
    ]


def test_changed_tool_decisions_names_the_tool_and_keeps_both_hashes():
    quarantined = [{
        "name": "write_file",
        "quarantine": "ask",
        "schema_changed": True,
        "description_changed": False,
        "previous_schema_sha256": "old-schema",
        "schema_sha256": "new-schema",
        "previous_description_sha256": "old-description",
        "description_sha256": "new-description",
    }]
    assert changed_tool_decisions("files", quarantined) == [{
        "question": "Tool changed: files / write_file",
        "recommendation": {
            "action": "review_tool_change",
            "server": "files",
            "tool": "write_file",
            "quarantine": "ask",
            "schema_changed": True,
            "description_changed": False,
            "previous_schema_sha256": "old-schema",
            "schema_sha256": "new-schema",
            "previous_description_sha256": "old-description",
            "description_sha256": "new-description",
        },
    }]


def test_record_reachability_compares_applies_and_decides_on_a_tool_change():
    source = inspect.getsource(record_reachability)
    assert "compare_catalog(" in source
    assert "apply_quarantine(" in source
    assert "changed_tool_decisions(" in source
