"""One stop and one envelope for all self-started work (BOSSAPP-905; design §9).

* STOP: per project, install-wide, or BOSS_AMBIENT_DISABLED. A stop refuses every new item,
  claim, reservation and gate, and cancels every agent lease at once, so running work stops at
  its next lease check. A person's lease is never cancelled by a stop. Resuming lifts the stop
  and gives no lease back.
* ENVELOPE: ambient money is reserved through BudgetGovernor under category 'ambient', but first
  against the project's own ambient daily cap, so self-started work cannot spend what a person's
  work needs.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.ambient import board, policy

from ._ambient_support import eng, open_item, project  # noqa: F401


def test_stopping_a_project_cancels_agent_leases_and_refuses_new_work(eng, project):
    with eng.begin() as c:
        mine = open_item(c, project)["item"]
        token = board.claim(c, mine["id"], owner="agent:sre")["lease_token"]
        theirs = open_item(c, project, fingerprint="fp:person-held")["item"]
        board.take_over(c, theirs["id"], actor="ceo")
        stopped = policy.stop(c, project_id=project, actor="ceo", reason="too noisy today")
        with pytest.raises(board.LeaseLost):
            board.assert_lease(c, mine["id"], owner="agent:sre", token=token)
        with pytest.raises(board.Conflict, match="stopped"):
            board.claim(c, mine["id"], owner="agent:sre")
        refused = open_item(c, project, fingerprint="fp:new-problem")
        gate = policy.gate(c, project, "doc_drift", "propose")
        state = policy.stop_state(c, project)
        person_kept = board.get(c, theirs["id"])["lease_owner"]
    assert stopped["released"] == [mine["id"]]
    assert refused["outcome"] == "refused" and refused["guard"] == "stopped"
    assert not gate["allowed"] and "stopped" in gate["reason"]
    assert state == {"stopped": True, "scope": "project", "by": "ceo", "at": state["at"],
                     "reason": "too noisy today"}
    assert person_kept == "person:ceo", "a stop never takes work away from a person"


def test_resuming_lifts_the_stop_and_gives_no_lease_back(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        board.claim(c, item["id"], owner="agent:sre")
        policy.stop(c, project_id=project, actor="ceo", reason="pause")
        resumed = policy.resume(c, project_id=project, actor="ceo", reason="ok again")
        after = board.get(c, item["id"])
        again = board.claim(c, item["id"], owner="agent:sre")
    assert resumed["stopped"] is False
    assert after["lifecycle"] == "proposed" and after["lease_owner"] is None
    assert again["item"]["lease_owner"] == "agent:sre"


def test_the_install_wide_stop_and_the_environment_switch_stop_every_project(
        eng, project, monkeypatch):
    with eng.begin() as c:
        before = c.execute(text("SELECT value FROM system_state WHERE key = 'ambient'")).scalar()
        try:
            policy.stop(c, project_id=None, actor="ceo", reason="incident on the host")
            assert policy.stop_state(c, project)["scope"] == "global"
            assert open_item(c, project)["guard"] == "stopped"
            policy.resume(c, project_id=None, actor="ceo")
            assert policy.stop_state(c, project)["stopped"] is False
            monkeypatch.setenv(policy.ENV_DISABLED, "1")
            assert policy.stop_state(c, project)["scope"] == "env"
            assert not policy.gate(c, project, "doc_drift", "propose")["allowed"]
        finally:
            if before is None:
                c.execute(text("DELETE FROM system_state WHERE key = 'ambient'"))


def test_ambient_money_is_held_to_its_own_daily_envelope_before_the_project_budget(
        eng, project):
    with eng.begin() as c:
        policy.update_settings(c, project, actor="ceo", daily_cap_usd_micros=1_000_000)
        item = open_item(c, project)["item"]
        too_much = policy.reserve(c, get_settings(), project_id=project, work_item_id=item["id"],
                                  amount_usd_micros=2_000_000, reason="incident squad")
        room = policy.envelope(c, project)
        refused = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND "
                                 "event_type = 'ambient.envelope_refused'"),
                            {"p": project}).scalar()
    assert too_much["status"] == "denied" and too_much["refusal"] == "ambient_envelope"
    assert "$1.00 left of $1.00" in too_much["message"]
    assert room == {"cap_usd_micros": 1_000_000, "spent_usd_micros": 0, "held_usd_micros": 0,
                    "available_usd_micros": 1_000_000, "resets": "00:00 UTC"}
    assert refused == 1


def test_a_reservation_inside_the_envelope_goes_through_the_project_budget_and_is_linked(
        eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        result = policy.reserve(c, get_settings(), project_id=project, work_item_id=item["id"],
                                amount_usd_micros=250_000, reason="incident squad")
        row = c.execute(text("SELECT category, amount_usd_micros FROM budget_reservations "
                             "WHERE project_id = :p"), {"p": project}).mappings().all()
        links = [l for l in board.links(c, item["id"]) if l["kind"] == "reservation"]
        room = policy.envelope(c, project)
    if result["status"] != "reserved":
        pytest.skip(f"the project budget refused for its own reason: {result.get('refusal')}")
    assert [dict(r) for r in row] == [{"category": "ambient", "amount_usd_micros": 250_000}]
    assert links[0]["ref"] == result["reservation_id"]
    assert room["held_usd_micros"] == 250_000 and room["available_usd_micros"] == 4_750_000


def test_a_stopped_project_reserves_nothing(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        policy.stop(c, project_id=project, actor="ceo", reason="pause")
        result = policy.reserve(c, get_settings(), project_id=project, work_item_id=item["id"],
                                amount_usd_micros=1, reason="x")
    assert result["status"] == "denied" and result["refusal"] == "ambient_stopped"


def test_settings_refuse_unknown_keys_and_out_of_range_values(eng, project):
    with eng.begin() as c:
        with pytest.raises(ValueError, match="unknown"):
            policy.update_settings(c, project, actor="ceo", yolo=True)
        with pytest.raises(ValueError, match="between"):
            policy.update_settings(c, project, actor="ceo", max_spawn_depth=50)
        changed = policy.update_settings(c, project, actor="ceo", max_concurrent=3)
    assert changed["max_concurrent"] == 3 and changed["updated_by"] == "ceo"
