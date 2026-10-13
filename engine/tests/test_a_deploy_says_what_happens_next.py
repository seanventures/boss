"""A planned deploy says what happens next, as it will (rc10 tester).

The first-deploy reply said "it runs once the deployment decision is approved" for a staging
deploy the environment's rule had approved: no decision existed and it was already starting.
"""
from __future__ import annotations

from boss_engine.services.stack_deploy import planned_note

DEPLOY = {"repository": "paramg/boss-sandbox", "commit": "8a009a6ca24930c765d1"}


def test_a_deploy_with_a_decision_waits_for_it():
    assert "once the deployment decision is approved" in planned_note(
        {**DEPLOY, "decision_id": "dec_1"}, "HelpDesk")


def test_a_deploy_the_rule_approved_says_it_is_starting():
    note = planned_note({**DEPLOY, "decision_id": None}, "HelpDesk")
    assert "decision" not in note and "starting now" in note
