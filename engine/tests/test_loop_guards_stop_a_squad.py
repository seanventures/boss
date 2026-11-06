"""Loop guards stop a squad in code, not in a prompt (BOSSAPP-908; design §6, §9).

* The project stop ends the squad at its next turn: the lead's lease is gone.
* A person who takes the item over ends it at once; the squad never claims it back.
* A whole round where every member passes ends the run (no progress).
* The turn cap and the per-item budget end it; so does the project's spent envelope.
* A squad that keeps proposing hits the proposal cap: the lead can no longer propose.
* A lead whose lease lapsed takes the item back at the next round — unless it is stopped.
"""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services.ambient import board, policy, squad_turns

from ._ambient_support import eng, project  # noqa: F401
from ._squad_support import (CRITIC, ENGINEER, LEAD, Launchers, ScriptedSquadModel, move,
                             settings, squad_item)

CHATTY = {who: [move("finding", summary=f"{who} note {n}") for n in range(12)]
          for who in (LEAD, ENGINEER, CRITIC, "agent:qa")}


def _item(eng, project, **kw):
    with eng.begin() as c:
        return squad_item(c, project, **kw)["item"]["id"]


def test_the_stop_ends_the_squad_at_its_next_turn(eng, project):
    item_id = _item(eng, project)

    class StopsMidRound(ScriptedSquadModel):
        def create_text_response(self, prompt, system_prompt=None, model=None):
            if "SEAT: agent:software_engineer" in str(prompt):
                with eng.begin() as other:
                    policy.stop(other, project_id=project, actor="u_ops", reason="pager noise")
            return super().create_text_response(prompt, system_prompt, model)

    run = squad_turns.collaborate(eng, settings(), item_id, rounds=3,
                                  model=StopsMidRound(CHATTY), launchers=Launchers())
    assert run["stopped"]["guard"] in ("stopped", "lease_lost")
    assert [t["member"] for t in run["turns"]] == [LEAD, ENGINEER]


def test_a_person_taking_over_ends_the_squad_and_it_never_claims_back(eng, project):
    item_id = _item(eng, project)
    with eng.begin() as c:
        board.take_over(c, item_id, actor="u_ops", reason="I'll patch it myself")
    run = squad_turns.collaborate(eng, settings(), item_id, rounds=2,
                                  model=ScriptedSquadModel(CHATTY), launchers=Launchers())
    with eng.begin() as c:
        item = board.get(c, item_id)
    assert run["stopped"]["guard"] == "taken_over" and run["turns"] == []
    assert item["lease_owner"] == "person:u_ops"


def test_a_round_where_everyone_passes_ends_the_run(eng, project):
    item_id = _item(eng, project)
    run = squad_turns.collaborate(eng, settings(), item_id, rounds=5,
                                  model=ScriptedSquadModel({}), launchers=Launchers())
    assert run["rounds"] == 1 and run["stopped"]["guard"] == "no_progress"
    assert {t["move"] for t in run["turns"]} == {"pass"}


def test_the_turn_cap_ends_a_squad_that_will_not_stop_talking(eng, project, monkeypatch):
    monkeypatch.setattr(squad_turns, "MAX_TURNS_PER_ITEM", 6)
    item_id = _item(eng, project)
    run = squad_turns.collaborate(eng, settings(), item_id, rounds=5,
                                  model=ScriptedSquadModel(CHATTY), launchers=Launchers())
    assert run["stopped"]["guard"] == "turn_cap" and len(run["turns"]) == 6


def test_the_items_budget_and_the_projects_envelope_end_it(eng, project):
    item_id = _item(eng, project)
    with eng.begin() as c:
        board.add_cost(c, item_id, squad_turns.SQUAD_BUDGET_USD_MICROS)
    run = squad_turns.collaborate(eng, settings(), item_id, model=ScriptedSquadModel(CHATTY),
                                  launchers=Launchers())
    assert run["stopped"]["guard"] == "budget"

    other = _item(eng, project, fingerprint="fp:other")
    with eng.begin() as c:
        policy.update_settings(c, project, actor="u_ops", daily_cap_usd_micros=0)
    run = squad_turns.collaborate(eng, settings(), other, model=ScriptedSquadModel(CHATTY),
                                  launchers=Launchers())
    assert run["stopped"]["guard"] == "envelope"


def test_the_proposal_cap_stops_a_lead_that_keeps_proposing(eng, project, monkeypatch):
    monkeypatch.setattr(squad_turns, "MAX_PROPOSALS_PER_ITEM", 1)
    item_id = _item(eng, project)
    model = ScriptedSquadModel({
        LEAD: [move("proposal", action="open_draft_pr", summary="Bump lodash"),
               move("proposal", action="open_draft_pr", summary="Bump lodash, take two")],
        CRITIC: [move("critique", position="object", summary="Escape /emails too")]})
    run = squad_turns.collaborate(eng, settings(), item_id, rounds=2, model=model,
                                  launchers=Launchers())
    lead_turns = [t for t in run["turns"] if t["member"] == LEAD]
    assert lead_turns[0]["move"] == "proposal"
    # Round two: proposing is no longer a choice, so the second proposal is refused.
    assert lead_turns[1]["move"] == "pass"
    lead_prompts = [p for who, p in model.prompts if who == LEAD]
    assert "- proposal:" not in lead_prompts[-1]


def test_a_lapsed_lead_takes_the_item_back_but_not_while_stopped(eng, project):
    item_id = _item(eng, project)
    with eng.begin() as c:
        c.execute(text("UPDATE work_items SET lease_expires_at = now() - interval '1 minute' "
                       "WHERE id = :w"), {"w": item_id})
        board.expire_leases(c, project_id=project)
    run = squad_turns.collaborate(eng, settings(), item_id, rounds=1,
                                  model=ScriptedSquadModel(CHATTY), launchers=Launchers())
    with eng.begin() as c:
        assert board.get(c, item_id)["lease_owner"] == LEAD
        c.execute(text("UPDATE work_items SET lease_expires_at = now() - interval '1 minute' "
                       "WHERE id = :w"), {"w": item_id})
        board.expire_leases(c, project_id=project)
        policy.stop(c, project_id=project, actor="u_ops", reason="freeze")
    stopped = squad_turns.collaborate(eng, settings(), item_id, rounds=1,
                                      model=ScriptedSquadModel(CHATTY), launchers=Launchers())
    assert len(run["turns"]) == 4
    assert stopped["stopped"]["guard"] == "stopped" and stopped["turns"] == []
