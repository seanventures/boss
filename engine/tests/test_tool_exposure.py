"""K5 (BOSSAPP-961): every registry tool declares where it is relevant, and derives strictly.

Two contracts, both across EVERY tool of build_default_registry (HARNESS_KERNEL_DESIGN.md §5):

STRICT DERIVATION (§5.3, §9.5 — this test was written before the flag existed, because the
risky part is the derivation, not the wiring): `ToolSpec.strict_schema()` makes every property
required, originally-optional properties nullable (type gains "null", an enum gains None) and
`additionalProperties: false` at every level, dropping `default` and `format`. The risk is a
call that is valid today becoming invalid: for every tool, calls built from the tool's own
schema (required-only, and every-property) must stay valid under the strict form once their
omitted optionals are written as null — which is exactly what a strict-mode model emits, and
what handlers already read as "missing" (they treat None as absent).

EXPOSURE LINT (§5.1): every registered tool carries an `Exposure` at its registration — a new
tool with none fails here with the words to fix it — the chat core stays ≤20, and the count of
tools per surface is pinned, so a tool cannot slip onto a surface without this file changing.
"""
from __future__ import annotations

import copy
import json

import pytest

from boss_engine.kernel.schema_check import first_violation
from boss_engine.tools.builtin import build_default_registry
from boss_engine.tools.registry import SURFACES, strict_parameters


@pytest.fixture(scope="module")
def registry():
    return build_default_registry()


# ── Building calls from a tool's own schema ───────────────────────────────────────────────────

def example_value(prop):
    if not isinstance(prop, dict) or not prop:
        return "x"
    if isinstance(prop.get("enum"), list) and prop["enum"]:
        return prop["enum"][0]
    declared = prop.get("type")
    if isinstance(declared, list):
        declared = next((t for t in declared if t != "null"), "string")
    if declared == "object" or isinstance(prop.get("properties"), dict):
        return example_call(prop, every=False)
    if declared == "array":
        items = prop.get("items")
        return [example_value(items)] if isinstance(items, dict) else []
    if declared == "integer":
        return int(prop.get("minimum", 1))
    if declared == "number":
        return float(prop.get("minimum", 1))
    if declared == "boolean":
        return True
    return "x"


def example_call(schema, *, every):
    """A call the schema describes: its required properties, or every property."""
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {str(r) for r in (schema.get("required") or [])}
    return {name: example_value(prop) for name, prop in props.items()
            if every or name in required}


def strict_complete(value, schema):
    """The call as a strict-mode model writes it: every omitted optional present as null,
    recursively — the completion step handlers already understand (missing == None)."""
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        out = {name: (strict_complete(value[name], prop if isinstance(prop, dict) else {})
                      if name in value else None)
               for name, prop in schema["properties"].items()}
        out.update({k: v for k, v in value.items() if k not in out})
        return out
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [strict_complete(item, schema["items"]) for item in value]
    return value


def each_object_level(schema, path=""):
    if not isinstance(schema, dict):
        return
    if isinstance(schema.get("properties"), dict):
        yield path or "/", schema
        for name, prop in schema["properties"].items():
            yield from each_object_level(prop, f"{path}/{name}")
    if isinstance(schema.get("items"), dict):
        yield from each_object_level(schema["items"], f"{path}/[]")


# ── Strict derivation, across every tool ──────────────────────────────────────────────────────

class TestStrictDerivation:
    def test_every_level_is_strict_and_every_optional_is_nullable(self, registry):
        for spec in registry.specs():
            strict = spec.strict_schema()
            for where, level in each_object_level(strict):
                assert level.get("additionalProperties") is False, (spec.name, where)
                assert level.get("required") == sorted(level["properties"]), (spec.name, where)
            # Every property the ORIGINAL schema left optional must accept null.
            original = spec.parameters or {}
            required = {str(r) for r in (original.get("required") or [])}
            for name, prop in (strict.get("properties") or {}).items():
                if name in required:
                    continue
                why = first_violation(None, prop)
                assert why is None, (f"{spec.name}.{name} was optional and must be nullable "
                                     f"in the strict form: {why}")

    def test_no_call_valid_today_becomes_invalid(self, registry):
        """The §9.5 risk, measured: required-only and every-property calls from each tool's own
        schema stay valid under the strict form once omitted optionals are written as null."""
        for spec in registry.specs():
            original = spec.parameters or {}
            strict = spec.strict_schema()
            for every in (False, True):
                call = example_call(original, every=every)
                sanity = first_violation(call, original)
                assert sanity is None, (spec.name, "the example breaks its OWN schema", sanity)
                completed = strict_complete(call, original)
                why = first_violation(completed, strict)
                assert why is None, (f"{spec.name}: a call valid today is invalid under the "
                                     f"strict schema: {why}")

    def test_default_and_format_are_dropped_and_nothing_is_mutated(self, registry):
        def each_schema(schema):
            yield schema
            for prop in (schema.get("properties") or {}).values():
                if isinstance(prop, dict):
                    yield from each_schema(prop)
            if isinstance(schema.get("items"), dict):
                yield from each_schema(schema["items"])

        for spec in registry.specs():
            before = json.dumps(spec.parameters, sort_keys=True, default=str)
            strict = spec.strict_schema()
            assert json.dumps(spec.parameters, sort_keys=True, default=str) == before, spec.name
            for level in each_schema(strict):
                # The KEYWORDS the strict subsets reject — a property NAMED "format" stays.
                assert "default" not in level, spec.name
                assert "format" not in level or isinstance(level["format"], dict), spec.name

    def test_the_derivation_on_a_worked_example(self):
        schema = {"type": "object",
                  "properties": {
                      "run": {"type": "string"},
                      "level": {"type": "string", "enum": ["info", "error"],
                                "default": "info"},
                      "window": {"type": "object",
                                 "properties": {"hours": {"type": "integer"},
                                                "tz": {"type": "string"}},
                                 "required": ["hours"]},
                      "ids": {"type": "array", "items": {"type": "string"}}},
                  "required": ["run"]}
        strict = strict_parameters(copy.deepcopy(schema))
        assert strict["required"] == ["ids", "level", "run", "window"]
        assert strict["additionalProperties"] is False
        assert strict["properties"]["level"]["enum"] == ["info", "error", None]
        assert "default" not in strict["properties"]["level"]
        assert strict["properties"]["window"]["type"] == ["object", "null"]
        window = strict["properties"]["window"]
        assert window["additionalProperties"] is False
        assert window["required"] == ["hours", "tz"]
        assert window["properties"]["tz"]["type"] == ["string", "null"]
        assert window["properties"]["hours"]["type"] == "integer"  # required stays non-null
        assert strict["properties"]["ids"]["type"] == ["array", "null"]
        # The all-null-optionals call a strict model writes is valid.
        call = {"run": "r1", "level": None, "window": None, "ids": None}
        assert first_violation(call, strict) is None
        # ...and the original minimal call, completed, too.
        assert first_violation(strict_complete({"run": "r1"}, schema), strict) is None


# ── Exposure lint, across every tool ──────────────────────────────────────────────────────────

#: Tools per surface, pinned. A NEW TOOL FAILS HERE ON PURPOSE: declare its `exposure=` at the
#: registration (which surfaces see it; conservative default Exposure() = chat + mcp, deferred),
#: then update these counts in the same change — that is the conscious step.
EXPECTED_PER_SURFACE = {
    "chat": 181,   # +4: list/create/link_work_items and ask_human (BOSSAPP-830/832)
    "mcp": 181,
    "agent_step": 1,   # find_tools; agent steps join when K4 moves them onto the kernel
    "decision": 1,     # find_tools; a decision's read tools stay the caller's (ReadPhase)
    "build": 1,        # find_tools; build stages join at K12
}
#: The schemas every chat call carries when BOSS_SCOPED_TOOLS is on (≤20 by design §5.1).
EXPECTED_CHAT_CORE = 20


class TestExposureLint:
    def test_every_tool_declares_its_exposure(self, registry):
        missing = [s.name for s in registry.specs() if s.exposure is None]
        assert not missing, (
            "these tools declare no exposure — pass exposure=Exposure(tags=(...), ...) at the "
            f"registration (tools/registry.Exposure), never in a separate list: {missing}")

    def test_the_counts_per_surface_are_pinned(self, registry):
        counts = {surface: 0 for surface in SURFACES}
        for spec in registry.specs():
            for surface in spec.exposure.surfaces:
                counts[surface] += 1
        assert counts == EXPECTED_PER_SURFACE, (
            "a tool changed which surfaces see it — if that is intended, update "
            "EXPECTED_PER_SURFACE in the same change; the diff of the two dicts says which "
            "surface moved")

    def test_the_chat_core_is_small_and_pinned(self, registry):
        core = sorted(s.name for s in registry.specs()
                      if "chat" in s.exposure.core_for)
        assert len(core) <= 20, (
            f"the chat core is the schema block EVERY chat call pays for; it is capped at 20 "
            f"and holds {len(core)}: {core}")
        assert len(core) == EXPECTED_CHAT_CORE, (
            "the chat core changed — if that is intended, update EXPECTED_CHAT_CORE; "
            f"it now holds {core}")
        assert "find_tools" in core, "the search tool itself is always offered"

    def test_every_tag_is_short_and_lowercase(self, registry):
        for spec in registry.specs():
            for tag in spec.exposure.tags:
                assert tag == tag.lower() and 2 <= len(tag) <= 16, (spec.name, tag)

    def test_the_discuss_registry_keeps_the_exposure(self):
        from boss_engine.tools.builtin import build_discuss_read_registry

        reg = build_discuss_read_registry()
        missing = [s.name for s in reg.specs() if s.exposure is None]
        assert not missing, f"build_discuss_read_registry dropped exposure for: {missing}"
