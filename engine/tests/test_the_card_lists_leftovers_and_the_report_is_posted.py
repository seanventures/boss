"""BOSSAPP-1192: the tier-3 approval card lists what the executor left alone.
BOSSAPP-1194: a rule that copies a shipped default is not proposed again, and resolving
an incident posts its report to chat and to notifications.

No database and no live delivery. The notifier in these tests is a recording fake.
"""
from __future__ import annotations

import pytest

from boss_engine.services import infra_agent, storage_cleanup as sc
from boss_engine.services import storage_incidents as si
from boss_engine.services import storage_rules
from boss_engine.tools.registry import ToolRegistry
from boss_engine.tools.storage_tools import register_storage_tools

LEFT_ALONE = ["unlabelled items", "data volumes", "non-scratch databases"]
NEVER_AUTOMATIC = ["unlabelled", "data volumes", "non-scratch databases"]
DUPLICATE_ERROR = "that rule duplicates the shipped default; it is already in force"


class _Stops:
    """A connection that must not be used once the decision under test is decided."""

    def execute(self, *args, **kwargs):
        raise AssertionError("this path must not touch the database")

    def begin(self):
        raise AssertionError("this path must not open a connection")


class _PastGuard:
    """begin() is the first database use. Reaching it means the duplicate guard did not fire."""

    def __init__(self):
        self.opened = False

    def begin(self):
        self.opened = True
        return self

    def __enter__(self):
        raise RuntimeError("past the duplicate guard")

    def __exit__(self, *args):
        return False

    def execute(self, *args, **kwargs):
        self.opened = True
        raise RuntimeError("past the duplicate guard")


def _propose_tool():
    reg = ToolRegistry()
    register_storage_tools(reg)
    return reg.get("storage_propose_rule").handler


def test_the_approval_card_lists_what_it_left_alone(monkeypatch):
    captured: dict = {}

    def fake_create(connection, project_id, task_id, question, recommendation, cost, reason, **kwargs):
        captured["recommendation"] = recommendation
        captured["reason"] = reason
        return "dec_left"

    monkeypatch.setattr("boss_engine.services.core.create_blocking_decision", fake_create)
    incident = {"id": "si_left", "host_id": "h1", "host_label": "laptop", "project_id": "p1",
                "cause_key": "", "cause_text": "Disk is full.", "free_pct_now": 12,
                "target_pct": 25, "paused_runs": 2}
    step = {"tool": "storage.remove_backups", "args": {"keep_newest": 10}, "plan_hash": "ph",
            "gb": 4, "action_id": "act_1"}

    class _AfterCard:
        def execute(self, *args, **kwargs):
            raise RuntimeError("card already built")

    with pytest.raises(RuntimeError, match="card already built"):
        sc._ask(_AfterCard(), incident, [], step, ["boss-backup-old"])

    rec = captured["recommendation"]
    assert rec["left_alone"] == LEFT_ALONE
    assert rec["never_automatic"] == NEVER_AUTOMATIC
    assert "Left alone: unlabelled items, data volumes, and non-scratch databases." in captured["reason"]


def test_a_rule_that_copies_a_shipped_default_is_not_proposed():
    assert si.propose_rule(_Stops(), {"id": "si", "host_id": "h", "project_id": "p"},
                           kind="images", rule={"keep_versions": 2}, why="seen again") is None
    assert si.propose_rule(_Stops(), {"id": "si", "host_id": "h", "project_id": "p"},
                           kind="docker_trim", rule={"enabled": True}, why="seen again") is None
    assert si.propose_rule(_Stops(), {"id": "si", "host_id": "h", "project_id": "p"},
                           kind="logs", rule={"max_size_mb": 50, "max_file": 3}, why="seen again") is None
    # A subset of the shipped telemetry rule is the same rule already in force.
    assert si.propose_rule(_Stops(), {"id": "si", "host_id": "h", "project_id": "p"},
                           kind="telemetry", rule={"keep_days": 30}, why="seen again") is None


def test_a_tighter_rule_and_a_backup_rule_are_still_proposed():
    for kind, rule in (
        ("images", {"keep_versions": 1}),
        ("telemetry", {"keep_days": 14}),
        ("backups", dict(storage_rules.PROPOSED_BACKUP_RULE)),
    ):
        gate = _PastGuard()
        with pytest.raises(RuntimeError, match="past the duplicate guard"):
            si.propose_rule(gate, {"id": "si", "host_id": "h", "project_id": "p"},
                            kind=kind, rule=rule, why="tighter than the default")
        assert gate.opened


def test_the_tool_refuses_a_duplicate_instead_of_opening_a_card():
    handler = _propose_tool()
    refused = handler(_Stops(), project_id="p", actor="agent:infra", args={
        "incident_id": "si", "kind": "images", "rule": {"keep_versions": 2}, "why": "again"})
    assert refused == {"ok": False, "error": DUPLICATE_ERROR}

    gate = _PastGuard()
    with pytest.raises(RuntimeError, match="past the duplicate guard"):
        handler(gate, project_id="p", actor="agent:infra", args={
            "incident_id": "si", "kind": "images", "rule": {"keep_versions": 1}, "why": "tighter"})
    assert gate.opened


def test_learning_records_the_shipped_default_instead_of_proposing_it(monkeypatch):
    existing = {"id": "rule_images", "kind": "images", "source": "default", "enabled": True,
                "label": "keep 2 versions"}
    monkeypatch.setattr(si.storage_rules, "rule_for", lambda *args, **kwargs: existing)
    monkeypatch.setattr(si, "times_seen", lambda *args, **kwargs: 3)

    class _Pending:
        def execute(self, *args, **kwargs):
            raise AssertionError("a shipped default must not look for a decision card")

    learned = si.learn(_Pending(), {"id": "si", "host_id": "h", "project_id": "p",
                                     "cause_key": "images.no_retention"})
    assert learned["rule_proposed"] is None
    assert learned["rule_added"] == existing


def test_resolving_posts_the_report_to_chat_and_to_notifications():
    posted = {"chat": [], "notifications": []}

    class Fake:
        def chat(self, connection, incident, body: str) -> None:
            posted["chat"].append((incident["id"], body))

        def notify(self, connection, incident, body: str) -> None:
            posted["notifications"].append((incident["id"], body))

    report = {"summary": "Images filled the disk. BOSS paused 2 runs, freed 4.0 GB safest first.",
              "freed_gb": 4.0, "paused_runs": 2, "waited_seconds_max": 0, "cost_usd": 0.0,
              "duration_seconds": 12, "timeline": [], "cause": {"text": "old images piled up"},
              "removed": [], "learned": {}}
    incident = {"id": "si_9", "project_id": "proj", "host_id": "h", "host_label": "laptop"}
    infra_agent.on_resolved(object(), incident, report, notifier=Fake())
    assert len(posted["chat"]) == 1
    assert len(posted["notifications"]) == 1
    assert posted["chat"][0][0] == "si_9"
    assert posted["chat"][0][1] == posted["notifications"][0][1]
    assert "freed 4.0 GB" in posted["chat"][0][1]
    assert "Images filled the disk" in posted["chat"][0][1]


def test_a_report_with_no_project_is_not_posted():
    calls: list[str] = []

    class Fake:
        def chat(self, *args, **kwargs):
            calls.append("chat")

        def notify(self, *args, **kwargs):
            calls.append("notify")

    infra_agent.on_resolved(object(), {"id": "si", "host_id": "h"},
                            {"summary": "nothing to post"}, notifier=Fake())
    assert calls == []
