"""Every write that only QUARANTINES something a person reviews later runs in the turn, with
no confirm button on top of that review (brec_eddfaf96e14349e49a86 fixed the KG proposals;
brec_9157d2686df34517addc found record_learning, the same class, still confirm-gated).

One list, pinned: a write lands here only if its effect is a quarantined record (an
`inferred` fact or learning, a proposed rule, a filed gap) that changes nothing until a person
acts. The chat registry rebuild keeps the flag."""
from __future__ import annotations

from boss_engine.tools import build_default_registry
from boss_engine.tools.builtin import build_discuss_read_registry

QUARANTINED_WRITES = ("file_platform_gap", "kg_propose_fact", "kg_propose_rule",
                      "record_learning",
                      # an agent's friction report (BOSSAPP-889): a record a person reads
                      "report_friction")


def test_every_quarantined_write_is_a_queued_safe_write():
    base = build_default_registry()
    for name in QUARANTINED_WRITES:
        spec = base.get(name)
        assert spec is not None, name
        assert spec.kind == "write" and spec.reversibility == "safe", name
        assert spec.queues_for_review is True, (
            f"{name} only quarantines a record a person reviews; register it with "
            "queues_for_review=True so chat does not hold it behind a confirm button")


def test_the_chat_registry_keeps_the_flag():
    discuss = build_discuss_read_registry()
    for name in QUARANTINED_WRITES:
        spec = discuss.get(name)
        if spec is not None:
            assert spec.queues_for_review is True, name
