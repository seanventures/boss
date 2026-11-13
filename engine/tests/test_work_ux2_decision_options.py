"""BOSSAPP-879: decision options with typed, signed effects.

* an option set is validated (≤ 8 options, typed effects, targets in this project) and each
  option is signed over its effects and the target snapshot;
* choosing runs exactly that option's effects, recording one decision_effect_runs row each;
* a target that changed since the question was asked is skipped (skipped_stale), not guessed at;
* a tampered option (its effects changed after signing), a missing signature or an unknown
  option refuses the approval, and nothing is recorded;
* the card offers one Approve per option (carrying its id), "None of these" and Delegate.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from boss_engine.services import decision_actions, decision_options, work_items
from boss_engine.services.decisions import InvalidDecisionTransitionError

from ._ambient_support import eng, project  # noqa: F401 — fixtures used by name


def _svc():
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService

    return DecisionService(get_settings())


def _ceo(c, pid):
    c.execute(text(
        "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
        "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
        "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
        {"i": f"pm_{pid}_ceo", "p": pid})


def _status(c, item_id):
    return c.execute(text("SELECT status, assignee FROM work_items WHERE id = :i"),
                     {"i": item_id}).mappings().one()


def _offer(c, pid, epic, story):
    return decision_options.offer(
        c, project_id=pid, question="The checkout epic is stuck. What now?",
        asker="agent:planner", work_item_id=epic["id"],
        options=[
            {"id": "cancel", "label": "Cancel the epic",
             "effects": [{"type": "cancel_tree", "target": epic["id"]}]},
            {"id": "reassign", "label": "Give it to QA",
             "effects": [{"type": "assign", "target": story["id"], "assignee": "agent:qa"},
                         {"type": "comment", "target": story["id"], "body": "QA takes it"},
                         {"type": "create", "title": "Write the QA plan",
                          "parent": epic["id"]}]},
        ])


def test_options_are_validated(eng, project):
    with eng.begin() as c:
        item = work_items.create_item(c, project_id=project, title="x")
        for bad in ([], [{"label": "a", "effects": [{"type": "explode"}]}],
                    [{"label": "a", "effects": [{"type": "assign", "target": "wi_nope",
                                                  "assignee": "agent:qa"}]}],
                    [{"label": f"o{i}", "effects": []} for i in range(9)],
                    [{"id": "a", "label": "a"}, {"id": "a", "label": "b"}],
                    [{"label": "a", "effects": [{"type": "update_status", "target": item["id"],
                                                  "status": "in_review"}]}]):
            with pytest.raises((decision_options.OptionError, ValueError, LookupError)):
                with c.begin_nested():
                    decision_options.offer(c, project_id=project, question="q", options=bad,
                                           asker="agent:x")


def test_choosing_runs_exactly_that_option_and_records_each_effect(eng, project):
    svc = _svc()
    with eng.begin() as c:
        _ceo(c, project)
        epic = work_items.create_item(c, project_id=project, title="Checkout epic")
        story = work_items.create_item(c, project_id=project, title="Card form",
                                       parent_id=epic["id"])
        offered = _offer(c, project, epic, story)
        did = offered["decision_id"]
        card = svc.get_row(c, did)
        assert all(o["sig"].startswith("hs1:") for o in card["recommendation"]["effect_options"])
        offers = decision_actions.allowed_actions(card)
        assert [(a["action"], a.get("choose")) for a in offers] == [
            ("approve", "cancel"), ("approve", "reassign"), ("reject", None),
            ("delegate", None)]
        assert "Assign" in offers[1]["effect"]
        # Approve with no choice, or a choice the card did not carry: refused, nothing recorded.
        for choose in (None, "burn_it"):
            with pytest.raises(InvalidDecisionTransitionError):
                with c.begin_nested():
                    svc.transition(c, did, action="approve", actor="ceo", note=None,
                                   choose=choose)
        assert svc.get_row(c, did)["status"] == "pending"
        svc.transition(c, did, action="approve", actor="ceo", note=None, choose="reassign")
        assert _status(c, story["id"])["assignee"] == "agent:qa"
        assert _status(c, epic["id"])["status"] != "cancelled"     # the other option did not run
        runs = decision_options.effect_runs(c, did)
        assert [(r["effect_type"], r["status"]) for r in runs] == [
            ("assign", "done"), ("comment", "done"), ("create", "done")]
        created = runs[2]["detail"]["created"]
        assert c.execute(text("SELECT parent_id FROM work_items WHERE id = :i"),
                         {"i": created}).scalar() == epic["id"]
        assert svc.get_row(c, did)["recommendation"]["chosen_option"] == "reassign"
        assert decision_actions.allowed_actions(svc.get_row(c, did)) == []


def test_a_target_that_changed_is_skipped_not_guessed_at(eng, project):
    svc = _svc()
    with eng.begin() as c:
        _ceo(c, project)
        epic = work_items.create_item(c, project_id=project, title="Epic")
        story = work_items.create_item(c, project_id=project, title="Story", parent_id=epic["id"])
        did = _offer(c, project, epic, story)["decision_id"]
        # Somebody moved the story on while the question waited.
        c.execute(text("UPDATE work_items SET assignee = 'person:bob', "
                       "updated_at = now() + interval '1 second' WHERE id = :i"),
                  {"i": story["id"]})
        svc.transition(c, did, action="approve", actor="ceo", note=None, choose="reassign")
        assert _status(c, story["id"])["assignee"] == "person:bob"
        runs = {r["effect_type"]: r["status"] for r in decision_options.effect_runs(c, did)}
        # The story's two effects are skipped; the create under the unchanged epic runs.
        assert runs == {"assign": "skipped_stale", "comment": "skipped_stale", "create": "done"}


def test_cancel_tree_cancels_the_item_and_its_open_children(eng, project):
    svc = _svc()
    with eng.begin() as c:
        _ceo(c, project)
        epic = work_items.create_item(c, project_id=project, title="Epic")
        story = work_items.create_item(c, project_id=project, title="Story", parent_id=epic["id"])
        did = _offer(c, project, epic, story)["decision_id"]
        svc.transition(c, did, action="approve", actor="ceo", note=None, choose="cancel")
        assert _status(c, epic["id"])["status"] == "cancelled"
        assert _status(c, story["id"])["status"] == "cancelled"


def test_a_tampered_or_unsigned_option_never_runs(eng, project):
    svc = _svc()
    with eng.begin() as c:
        _ceo(c, project)
        epic = work_items.create_item(c, project_id=project, title="Epic")
        story = work_items.create_item(c, project_id=project, title="Story", parent_id=epic["id"])
        did = _offer(c, project, epic, story)["decision_id"]
        rec = svc.get_row(c, did)["recommendation"]
        # The effects were swapped after the person was shown them (same signature).
        rec["effect_options"][1]["effects"] = [
            {"type": "cancel_tree", "target": epic["id"]}]
        c.execute(text("UPDATE decisions SET recommendation = CAST(:r AS JSONB) WHERE id = :d"),
                  {"r": json.dumps(rec), "d": did})
        with pytest.raises(InvalidDecisionTransitionError, match="signature"):
            with c.begin_nested():
                svc.transition(c, did, action="approve", actor="ceo", note=None,
                               choose="reassign")
        # A stripped signature fails closed too.
        rec["effect_options"][0].pop("sig")
        c.execute(text("UPDATE decisions SET recommendation = CAST(:r AS JSONB) WHERE id = :d"),
                  {"r": json.dumps(rec), "d": did})
        with pytest.raises(InvalidDecisionTransitionError):
            with c.begin_nested():
                svc.transition(c, did, action="approve", actor="ceo", note=None, choose="cancel")
        assert svc.get_row(c, did)["status"] == "pending"
        assert _status(c, epic["id"])["status"] != "cancelled"
        assert decision_options.effect_runs(c, did) == []


def test_choose_on_a_decision_without_options_is_refused(eng, project):
    from boss_engine.services import typed_forms

    svc = _svc()
    with eng.begin() as c:
        _ceo(c, project)
        asked = typed_forms.ask_human(c, project_id=project, question="Name?", asker="agent:x",
                                      form={"fields": [{"key": "n", "type": "text",
                                                        "label": "Name"}]})
        with pytest.raises(InvalidDecisionTransitionError, match="no options"):
            with c.begin_nested():
                svc.transition(c, asked["decision_id"], action="approve", actor="ceo",
                               note=None, answer={"n": "x"}, choose="o1")
