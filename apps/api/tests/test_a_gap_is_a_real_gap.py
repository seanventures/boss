"""A team's Gaps row lists only recorded gaps (org-view spec §4.2, §7.1): an ability the agent's
spec recommends that it does not hold, or one a workflow step uses whose named agent lacks it.
Giving the ability clears it; a Kit that cannot be read says so and the rest of the board
stands."""
from __future__ import annotations

from boss_engine.db import begin
from boss_engine.services import agents_org_board, kit

from apps.api.app.config import get_settings
from apps.api.tests import org_board_support as ob

#: An ability with nothing to connect first, so giving it is one press.
ABILITY = "web_research"


def test_a_recommended_ability_not_held_is_a_gap_until_given(client, engine):
    eng = ob.team("eng")
    forge = ob.role(engine, ob.uid("forge"), eng,
                    permissions={"recommended_abilities": [ABILITY]})
    body = ob.board(client).json()
    gaps = ob.team_row(body, eng)["gaps"]
    assert gaps["kit_ok"] is True
    assert [(a["ability"], a["roles"]) for a in gaps["abilities"]] == [(ABILITY, [forge])]
    label = kit.ability_def(get_settings(), ABILITY)["label"]
    assert gaps["abilities"][0]["label"] == label
    assert ob.agent_row(body, forge)["gaps"]["abilities"] == [
        {"ability": ABILITY, "label": label}]

    with begin(engine) as c:
        holders = kit.holders(c, get_settings(), ob.PROJECT,
                              [kit.ability_def(get_settings(), ABILITY)])[ABILITY]
        kit.set_holders(c, get_settings(), ob.PROJECT, ABILITY, [*holders, forge],
                        actor="ceo")
    after = ob.board(client).json()
    assert ob.team_row(after, eng)["gaps"]["abilities"] == []


def test_a_step_whose_agent_lacks_its_ability_is_a_step_gap(client, engine):
    eng = ob.team("eng")
    forge = ob.role(engine, ob.uid("forge"), eng)
    key = ob.workflow(engine, [ob.step("check", captain=forge, skills=["ci.status"])])
    body = ob.board(client).json()
    gaps = ob.team_row(body, eng)["gaps"]["step_gaps"]
    assert [(g["role_key"], g["ability"], g["workflow_key"], g["step_id"]) for g in gaps] == [
        (forge, "tests_ci", key, "check")], gaps
    assert ob.agent_row(body, forge)["gaps"]["step_gaps"] == gaps


def test_a_kit_that_cannot_be_read_says_so_and_the_rest_stands(client, engine, monkeypatch):
    eng = ob.team("eng")
    forge = ob.role(engine, ob.uid("forge"), eng)
    top = ob.root(engine)
    ob.act(engine, forge, parent=top, root=top)

    def broken(*_a, **_k):
        raise RuntimeError("kit file unreadable")

    monkeypatch.setattr(agents_org_board, "kit_gaps", broken)
    body = ob.board(client).json()
    gaps = ob.team_row(body, eng)["gaps"]
    assert gaps["kit_ok"] is False and "kit file unreadable" in gaps["kit_reason"]
    assert ob.team_row(body, eng)["work"]["done"] == 1
