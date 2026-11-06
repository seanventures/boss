"""A squad is seated from the project's own agents, by a template for the item's task class
(BOSSAPP-908; design §13.1).

* The template picks the seats: a CVE gets a security lead, an engineer, QA and an adversarial
  critic; failing CI gets an engineer lead; anything else the general squad.
* Only agents the project HAS, that are on duty and have room, are seated; a seat nobody can
  fill is recorded as unfilled with the reason, never invented. A lead and a different critic
  are the minimum.
* The roster is a `squad` message on the board, the lead claims the item, and the squad's
  memory opens. Assembling twice returns the same squad.
* The lead can hand the lead (and the lease) to another member; the roster follows.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services.ambient import board, collab, squad_memory, squads

from ._ambient_support import eng, open_item, project  # noqa: F401
from ._squad_support import CRITIC, ENGINEER, LEAD, QA, add_roles, squad_item


def test_a_cve_gets_the_cve_squad_with_a_lead_specialists_and_a_critic_who_is_not_the_lead(
        eng, project):
    with eng.begin() as c:
        got = squad_item(c, project)
        item = board.get(c, got["item"]["id"])
        kinds = [m["kind"] for m in collab.messages(c, item["id"])]
        memory = squad_memory.read(c, item["id"])
    squad = got["squad"]
    assert squad["template"] == "dependency_cve" and squad["title"] == "CVE squad"
    assert squad["lead"] == LEAD
    assert [(m["party"], m["seat"]) for m in squad["members"]] == [
        (LEAD, "lead"), (ENGINEER, "specialist"), (QA, "specialist"), (CRITIC, "critic")]
    assert squad["critics"] == [CRITIC] and CRITIC != squad["lead"]
    assert item["lease_owner"] == LEAD and item["lifecycle"] == "claimed"
    assert got["lease_token"] and kinds == ["claim", "squad"]
    assert set(item["squad"]) >= {LEAD, ENGINEER, QA, CRITIC}
    assert memory["open"] and memory["goal"] == "lodash CVE-2021-23337 in web"


def test_the_template_follows_the_task_class(eng, project):
    with eng.begin() as c:
        add_roles(c, project, ("software_engineer", "qa", "sre", "adversarial_reviewer",
                               "chief_architect"))
        ci = open_item(c, project, task_class="red_main", fingerprint="fp:ci")["item"]
        doc = open_item(c, project, task_class="doc_drift", fingerprint="fp:doc")["item"]
        ci_squad = squads.assemble(c, ci["id"])["squad"]
        doc_squad = squads.assemble(c, doc["id"])["squad"]
    assert ci_squad["template"] == "failing_ci" and ci_squad["lead"] == "agent:software_engineer"
    assert {m["party"] for m in ci_squad["members"]} >= {"agent:qa", "agent:sre"}
    # doc_drift has no template of its own: the general squad, led by the class's owner role
    # when the project has it, else the best agent it does have.
    assert doc_squad["template"] == "general"
    assert doc_squad["critics"] and doc_squad["lead"] not in doc_squad["critics"]


def test_paused_agents_and_agents_with_no_room_are_not_seated_and_the_gap_is_said(eng, project):
    with eng.begin() as c:
        add_roles(c, project, ("security_reviewer", "software_engineer", "qa",
                               "adversarial_reviewer", "backend_engineer"),
                  lifecycle={"qa": "paused"})
        # The engineer already sits on three live items: no room for a fourth.
        for n in range(squads.MAX_LIVE_ITEMS_PER_AGENT):
            other = open_item(c, project, task_class="flaky_test", fingerprint=f"fp:busy{n}")
            c.execute(text("UPDATE work_items SET squad = '[\"agent:software_engineer\"]' "
                           "WHERE id = :w"), {"w": other["item"]["id"]})
        item = open_item(c, project, task_class="security_patch", fingerprint="fp:cve")["item"]
        squad = squads.assemble(c, item["id"])["squad"]
    seated = {m["party"] for m in squad["members"]}
    assert "agent:qa" not in seated and "agent:software_engineer" not in seated
    assert "agent:backend_engineer" in seated      # the template's second choice
    assert squad["unfilled"] and squad["unfilled"][0]["seat"] == "specialist"
    assert "qa: paused" in squad["unfilled"][0]["reason"]


def test_a_project_that_cannot_field_a_lead_and_a_different_critic_gets_no_squad(eng, project):
    with eng.begin() as c:
        add_roles(c, project, ("security_reviewer",))
        item = open_item(c, project, task_class="security_patch", fingerprint="fp:cve")["item"]
        with pytest.raises(squads.SquadError, match="a lead and a critic"):
            squads.assemble(c, item["id"])
        after = board.get(c, item["id"])
    assert after["lifecycle"] == "proposed" and after["lease_owner"] is None


def test_assembling_twice_returns_the_same_squad(eng, project):
    with eng.begin() as c:
        first = squad_item(c, project)
        again = squads.assemble(c, first["item"]["id"])
        squads_posted = len(collab.messages(c, first["item"]["id"], kinds=("squad",)))
    assert again["outcome"] == "existing" and again["lease_token"] is None
    assert again["squad"]["lead"] == first["squad"]["lead"] and squads_posted == 1


def test_the_lead_hands_the_lead_and_the_lease_to_another_member(eng, project):
    with eng.begin() as c:
        got = squad_item(c, project)
        item_id = got["item"]["id"]
        moved = squads.hand_lead(c, item_id, to=ENGINEER, token=got["lease_token"],
                                 summary="the fix is in code the engineer owns")
        squad = squads.roster(c, item_id)
        item = board.get(c, item_id)
        with pytest.raises(board.LeaseLost):
            board.assert_lease(c, item_id, owner=LEAD, token=got["lease_token"])
        with pytest.raises(ValueError, match="not on this item's squad"):
            squads.hand_lead(c, item_id, to="agent:pm", token=moved["lease_token"], summary="x")
    assert squad["lead"] == ENGINEER and item["lease_owner"] == ENGINEER
    seats = {m["party"]: m["seat"] for m in squad["members"]}
    assert seats[ENGINEER] == "lead" and seats[LEAD] == "specialist"
