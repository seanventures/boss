"""NO AGENT MOVES A SELF-FIX (BOSSAPP-571 spec §11.4): an agent or a client can list self-fixes,
read one with its lineage and card, and read the autonomy metric — under `improvement:read`.
The one tool that starts one, `request_self_fix`, is a press a person makes (operator_only: the
model and MCP get a button, never the run). No tool approves, installs, removes or sends one."""
from __future__ import annotations

from boss_engine.tools import build_default_registry

READS = ("list_self_fixes", "get_self_fix", "autonomy_report")
#: Starts one from an open recommendation, only when a person presses it.
PRESSED = ("request_self_fix",)


def _registry():
    return build_default_registry()


def test_the_three_reads_exist_and_nothing_writes():
    reg = _registry()
    names = set(reg.names())
    for name in READS:
        assert name in names, name
        spec = reg.get(name)
        assert getattr(spec, "permission", None) == "improvement:read"
    movers = [n for n in names if "self_fix" in n and n not in READS + PRESSED]
    assert movers == [], f"no tool moves a self-fix: {movers}"
    for name in PRESSED:
        spec = reg.get(name)
        assert spec.operator_only and not spec.self_executable, name
        assert spec.permission == "improvement:manage", name


def test_the_model_asking_to_start_one_gets_a_button_never_a_run():
    """The door, not the model: without a person's press, invoke refuses before the handler (it
    would fail on a missing context if it ran)."""
    from boss_engine.tools import build_discuss_read_registry

    reg = build_discuss_read_registry(edition="community")
    out = reg.invoke("request_self_fix", None, project_id="p_x", actor="agent",
                     args={"recommendation_id": "brec_x"})
    assert out["status"] == "confirmation_required", out
    assert out["proposed_args"] == {"recommendation_id": "brec_x"}


def test_they_are_reads():
    reg = _registry()
    reads = {s.name for s in reg.specs(kind="read")}
    assert set(READS) <= reads
