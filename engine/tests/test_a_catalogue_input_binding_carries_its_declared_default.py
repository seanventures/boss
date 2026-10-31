"""A catalogue step's input binding carries the default its field declares (2026-10-31).

`max_hops` is declared optional with "default": 2 (_FIELD_TEXT), but Context Enrichment bound it
as a bare `from_input`, so a run started without it dead-lettered: "this workflow was started
without 'max_hops', which this step needs" (banking_regulatory_compliance, wfr_438b8ffb). The
runtime honours a binding's `default`; every binding must carry the one its field declares."""
from __future__ import annotations

from boss_engine.services import workflow_catalogue as wc


def _bindings():
    for entry in wc.CATALOGUE:
        for step in entry.get("steps") or []:
            for name, value in ((step.get("binding") or {}).get("inputs") or {}).items():
                if isinstance(value, dict) and "from_input" in value:
                    yield entry["key"], step.get("id"), name, value


def _optional(step: dict) -> frozenset:
    from boss_engine.services.workflow_capabilities import CAPABILITIES

    cap = CAPABILITIES.get(str((step.get("binding") or {}).get("capability") or ""))
    return frozenset(getattr(cap, "optional_inputs", None) or ())


def test_a_required_binding_carries_its_fields_declared_default():
    """A binding the step does NOT treat as optional fails the run when its input is absent,
    unless it carries a default. A field that declares one must pass it on."""
    steps = {(e["key"], s.get("id")): s for e in wc.CATALOGUE for s in (e.get("steps") or [])}
    missing = []
    for key, step_id, name, value in _bindings():
        declared = (wc._FIELD_TEXT.get(value["from_input"]) or {})
        if name in _optional(steps[(key, step_id)]):
            continue
        if "default" in declared and value.get("default") != declared["default"]:
            missing.append(f"{key}.{step_id}.{name}")
    assert not missing, f"bindings without their field's declared default: {missing}"


def test_context_enrichment_defaults_to_two_hops():
    hops = [v for k, s, n, v in _bindings() if s == "context_enrichment" and n == "max_hops"]
    assert hops and all(v.get("default") == 2 for v in hops)
