"""An omitted optional sample brief resolves to the default stored in its spec."""
from __future__ import annotations

from boss_engine.services.workflow_capability_runner import _resolve_inputs


def test_stored_default_is_used_only_when_the_run_has_no_brief() -> None:
    bindings = {"prompt": {"from_input": "brief", "default": "Stored sample brief"}}
    assert _resolve_inputs(bindings, {"input": {}}) == {"prompt": "Stored sample brief"}
    assert _resolve_inputs(bindings, {"input": {"brief": ""}}) == {
        "prompt": "Stored sample brief",
    }
    assert _resolve_inputs(bindings, {"input": {"brief": "My brief"}}) == {
        "prompt": "My brief",
    }
