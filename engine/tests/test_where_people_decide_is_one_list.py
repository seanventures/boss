"""Where people decide: the compliance list is ONE list in code, and a card only tightens it.

The boundary used to be a regex in autonomy.py, a hand-kept set of always-a-person actions in
the ambient catalogue, and nothing the console could read. decide_list.py is now the one source:
the backstop's words, the ambient actions and the cards all come from it, and these tests fail
when any of them drifts from it — or when a card could make an entry looser than the backstop.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from boss_engine.services import autonomy, decide_list as dl
from boss_engine.services.ambient import task_classes as tc

#: The backstop's words as autonomy.py held them before the list existed. Every one must still
#: put a gate on the list: moving the source must not have dropped a word.
_BEFORE = re.compile(
    r"\b(deploy|deploys|deployment|ship|release|publish|merge|merges|merging"
    r"|delete|deletes|destroy|drop|purge|wipe|teardown|terminate"
    r"|send|email|notify|post|tweet|announce"
    r"|pay|payment|charge|purchase|buy|refund|transfer"
    r"|rotate|revoke|grant|permission|credential|secret|key"
    r"|production|prod\b|customer|external)\b", re.I)


def _words(pattern: re.Pattern) -> set[str]:
    inner = pattern.pattern[len(r"\b("):-len(r")\b")]
    return set(inner.split("|"))


def test_the_list_is_seven_entries_and_a_fixed_eighth():
    assert [b.key for b in dl.LIST] == ["prod_deploy", "merge", "spend", "secrets", "outside",
                                         "delete", "access", "change_list"]
    assert dl.LIST[-1].fixed and not any(b.fixed for b in dl.LIST_KINDS)
    for b in dl.LIST:
        assert b.title and b.why and b.icon and b.agents_may


def test_full_autos_backstop_is_the_lists_words_and_lost_none():
    assert autonomy.IRREVERSIBLE is dl.IRREVERSIBLE
    assert _words(dl.IRREVERSIBLE) == _words(_BEFORE)
    for word in _words(_BEFORE):
        sample = word.replace(r"\b", "")
        assert dl.IRREVERSIBLE.search(f"please {sample} it"), word


def test_the_always_on_agents_boundary_is_the_lists_actions():
    assert tc.ALWAYS_A_PERSON == dl.AMBIENT_ACTIONS
    for key in dl.AMBIENT_ACTIONS:
        assert key in tc.ACTIONS and tc.ACTIONS[key].always_a_person
    for key, action in tc.ACTIONS.items():
        assert action.always_a_person == dl.on_the_list(key), key


@pytest.mark.parametrize("entry", dl.LIST_KINDS, ids=lambda b: b.key)
def test_full_auto_never_approves_any_entry_on_the_list(entry):
    policy = autonomy.AutonomyPolicy(mode=autonomy.FULL_AUTO, threshold_usd=10_000)
    for word in entry.words:
        gate = {"status": "pending", "question": f"May I {word.replace(chr(92) + 'b', '')} now?",
                "risk_tier": 1, "cost_summary": {"usd": 1}}
        assert not autonomy.evaluate(gate, policy).approve, (entry.key, word)


@pytest.mark.parametrize("question,rec,kind", [
    ("Deploy HelpDesk v1.4 to production?", {}, "prod_deploy"),
    ("Deploy HelpDesk to staging", {}, None),
    ("Approve the plan?", {"action": "review_pull_request"}, "merge"),
    ("Raise the budget", {"action": "budget_denied"}, "spend"),
    ("Rotate the Stripe secret", {}, "secrets"),
    ("Email the customer about the outage", {}, "outside"),
    ("Purge the old cache volume", {}, "delete"),
    ("Grant the builder authority", {"action": "grant_builder_authority"}, "access"),
    ("Adopt the plan?", {}, None),
])
def test_a_decision_is_classified_onto_the_list_by_its_action_then_its_words(question, rec, kind):
    assert dl.classify({"question": question, "recommendation": rec}) == kind


@pytest.mark.parametrize("kind,rule,why", [
    ("prod_deploy", {"always_person": False}, "always waits for a person"),
    ("merge", {"full_auto": True}, "Full auto never"),
    ("spend", {"cap_usd": 900}, "never raised above"),
    ("spend", {"cap_usd": 0}, "above $0"),
    ("secrets", {"step_up": ""}, "passkey"),
    ("delete", {"step_up": ""}, "typed name"),
    ("access", {"decider": {"person": "agt_helm", "kind": "agent"}}, "must be a person"),
    ("outside", {"decider": {"person": "policy:autonomy"}}, "must be a person"),
    ("change_list", {}, "fixed"),
    ("not_a_kind", {}, "not on the list"),
])
def test_a_card_can_never_make_an_entry_looser_than_the_backstop(kind, rule, why):
    with pytest.raises(dl.RuleLooser) as refused:
        dl.validate(kind, rule)
    assert why in str(refused.value)


def test_a_card_can_make_an_entry_stricter():
    got = dl.validate("spend", {"cap_usd": 200, "decider": {"person": "usr_sarah", "name": "Sarah"},
                                "backup": {"person": "usr_ana", "name": "Ana"},
                                "quiet": [{"from_day": "fri", "from_time": "15:00",
                                           "to_day": "mon", "to_time": "08:00",
                                           "tz": "Europe/London"}],
                                "approvers": 2})
    assert got["cap_usd"] == 200 and got["approvers"] == 2 and got["always_person"]
    assert got["decider"]["person"] == "usr_sarah" and got["quiet"][0]["from_day"] == "fri"
    # A typed-name entry may be raised to a passkey, never lowered.
    assert dl.validate("delete", {"step_up": "passkey"})["step_up"] == "passkey"


def test_a_project_only_adds_to_the_installs_rule():
    install = dl.validate("spend", {"cap_usd": 200})
    with pytest.raises(dl.RuleLooser):
        dl.validate("spend", {"cap_usd": 300}, ceiling=install)
    assert dl.validate("spend", {"cap_usd": 150}, ceiling=install)["cap_usd"] == 150
    two = dl.validate("prod_deploy", {"approvers": 2})
    with pytest.raises(dl.RuleLooser):
        dl.validate("prod_deploy", {"approvers": 1}, ceiling=two)


def test_a_quiet_window_wraps_the_weekend_in_its_own_time_zone():
    w = dl.validate("prod_deploy", {"quiet": [{"from_day": "fri", "from_time": "15:00",
                                               "to_day": "mon", "to_time": "08:00",
                                               "tz": "Europe/London"}]})["quiet"][0]
    saturday = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    wednesday = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    friday_before = datetime(2026, 11, 6, 14, 59, tzinfo=timezone.utc)
    assert dl.in_window(w, saturday) == datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
    assert dl.in_window(w, wednesday) is None
    assert dl.in_window(w, friday_before) is None
    with pytest.raises(dl.RuleLooser):
        dl.validate("prod_deploy", {"quiet": [{"from_day": "fri", "from_time": "25:00",
                                               "to_day": "mon", "to_time": "08:00"}]})
