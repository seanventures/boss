"""Every model call is spent by one agent or by nobody, and a subscription call is never money
(org-view spec §6.1, §7.1; operator decision 2026-10-04: the Directory's $ is metered only).

Attribution, in order: the act the call ran under → the role the call recorded → its stage's
owner → not tied to an agent. The teams' metered money plus the untied amount is exactly the
total, and the Directory (Workforce) says the same dollars per agent as Organization.
"""
from __future__ import annotations

from boss_engine.db import begin
from boss_engine.services.console_v2_workforce import workforce

from apps.api.tests import org_board_support as ob


def test_spend_is_attributed_once_and_a_plan_call_is_never_money(client, engine):
    eng, qa = ob.team("eng"), ob.team("qa")
    forge = ob.role(engine, ob.uid("forge"), eng)
    rook = ob.role(engine, ob.uid("rook"), qa)
    top = ob.root(engine)
    step = ob.act(engine, forge, parent=top, root=top)
    ob.call(engine, act_id=step, micros=1_500_000)                  # act-linked → forge
    ob.call(engine, act_id=step, micros=9_000_000, included=True)   # on a plan → a call only
    ob.call(engine, role_key=rook, micros=250_000, model="claude-haiku-4-5")  # role only → rook
    ob.call(engine, act_id=step, role_key=rook, micros=100_000)     # the act wins over the role
    ob.call(engine, stage="no_such_stage_x", micros=840_000)        # nobody's
    body = ob.board(client, 30).json()
    totals = body["totals"]
    assert totals["spend_ok"] is True

    f, r = ob.agent_row(body, forge), ob.agent_row(body, rook)
    assert f["spend"] == {"metered_usd": 1.6, "plan_calls": 1}
    assert r["spend"] == {"metered_usd": 0.25, "plan_calls": 0}
    assert ob.team_row(body, eng)["spend"] == {"metered_usd": 1.6, "plan_calls": 1}
    assert {m["model"]: m["calls"] for m in ob.team_row(body, qa)["models"]} == {
        "claude-haiku-4-5": 1}
    assert totals["untied_calls"] >= 1 and totals["untied_usd"] >= 0.84

    # Every metered dollar is on exactly one team or untied.
    teams_micros = sum(round(t["spend"]["metered_usd"] * 1_000_000) for t in body["teams"])
    assert teams_micros + round(totals["untied_usd"] * 1_000_000) == round(
        totals["metered_usd"] * 1_000_000)

    # The Directory reads the same attribution: same metered dollars per agent.
    with begin(engine) as c:
        wf = workforce(c, ob.PROJECT, hours=24)
    by_id = {a["id"]: a for a in wf["roster"]}
    assert by_id[forge]["spend_usd"] == 1.6
    assert by_id[rook]["spend_usd"] == 0.25
    assert wf["spend_attributed"] is True

    # And the Passport: metered money only, subscription use as its own count.
    passport = client.get(f"/v1/agents/{forge}/passport", params={"project_id": ob.PROJECT})
    assert passport.status_code == 200, passport.text
    vitals = passport.json()["vitals"]
    assert vitals["cost_7d"] == 1.6 and vitals["plan_calls_7d"] == 1
