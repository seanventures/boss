"""Agents › Organization says only what BOSS recorded (org-view spec §6.1, §7.1).

One read, `GET /v1/agents/org-board`, behind Teams and Flow. Pinned here:
  (a) no percent, ETA, capacity or score anywhere in the payload;
  (b) three handoffs Forge→Rook, one failed and one waiting, are 3 / 1 / 1 on the agent edge
      and on the team edge they fold into;
  (c) work a person handed an agent is a `from: "person"` edge;
  (d) Done / Failed / Open per team equal a direct count of the acts;
  (e) a stage nested in its own step is busy time once — the outer span, not the sum;
  (f) an act open 20 hours is in Waiting, with its real status;
  (g) an unchanged board is a 304; one act ending changes the ETag;
  (h) a window other than 1, 7 or 30 days is refused.
And a real run: a gate holding an agent's work is that agent's team's wait, with its card.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from apps.api.tests import org_board_support as ob
from apps.api.tests import workflow_management_support as wf

BANNED = {"percent", "percentage", "eta", "capacity", "score", "estimate", "estimated",
          "utilisation", "utilization", "progress"}


def _keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


def _pair(engine):
    eng, qa = ob.team("eng"), ob.team("qa")
    forge = ob.role(engine, ob.uid("forge"), eng)
    rook = ob.role(engine, ob.uid("rook"), qa)
    return eng, qa, forge, rook


def test_handoff_outcomes_are_counted_on_the_agent_and_the_team_edge(client, engine):
    eng, qa, forge, rook = _pair(engine)
    for status, ended in (("complete", timedelta(minutes=30)), ("error", timedelta(minutes=20)),
                          ("awaiting_input", None)):
        top = ob.root(engine)
        # Production shape: the run act is the person's; its first step is handed by BOSS.
        step = ob.act(engine, forge, parent=top, root=top, handed_by="boss", how="named")
        ob.act(engine, rook, parent=step, root=top, status=status, ended=ended,
               started=timedelta(minutes=50), how="model_pick")
    got = ob.board(client)
    assert got.status_code == 200, got.text
    body = got.json()
    # (a)
    banned = [k for k in _keys(body) if k.lower() in BANNED]
    assert not banned, banned
    # (b)
    edge = next(e for e in body["agent_edges"] if (e["from"], e["to"]) == (forge, rook))
    assert (edge["handoffs"], edge["failed"], edge["waiting"]) == (3, 1, 1)
    assert edge["label"] == "Picked by BOSS"
    team_edge = next(e for e in body["team_edges"] if (e["from"], e["to"]) == (eng, qa))
    assert (team_edge["handoffs"], team_edge["failed"], team_edge["waiting"]) == (3, 1, 1)
    # Flow's `Stuck now`: the failure, with its run.
    assert [f["role_key"] for f in body["failures"]] == [rook] and body["failures"][0]["team"] == qa
    # (c) each run's first step hangs from the run its person started: a person→agent edge
    person = next(e for e in body["agent_edges"] if (e["from"], e["to"]) == ("person", forge))
    assert person["handoffs"] == 3
    assert any(e["from"] == "person" and e["to"] == eng for e in body["team_edges"])
    # The Edge panel's rows are the edge's own evidence.
    rows = client.get("/v1/agents/org-board/handoffs", params={
        "project_id": ob.PROJECT, "days": 7, "source": forge, "target": rook}).json()["items"]
    assert len(rows) == 3 and {r["status"] for r in rows} == {"complete", "error",
                                                              "awaiting_input"}
    assert all(r["from"] == forge and r["to"] == rook for r in rows)


def test_work_counts_equal_a_direct_count_of_the_acts(client, engine):
    eng, qa, forge, rook = _pair(engine)
    top = ob.root(engine)
    ob.act(engine, forge, parent=top, root=top, status="complete")
    ob.act(engine, forge, parent=top, root=top, status="complete", kind="stage")
    ob.act(engine, forge, parent=top, root=top, status="error")
    ob.act(engine, forge, parent=top, root=top, status="active", ended=None)
    # Ended before the window: not counted as done in 24h.
    ob.act(engine, forge, parent=top, root=top, status="complete",
           started=timedelta(days=3), ended=timedelta(days=2))
    for days, done in ((1, 2), (7, 3)):
        body = ob.board(client, days).json()
        row = ob.team_row(body, eng)
        with engine.connect() as c:
            direct = c.execute(text(
                "SELECT count(*) FILTER (WHERE status='complete'), "
                "count(*) FILTER (WHERE status='error'), count(*) FILTER (WHERE ended_at IS NULL) "
                "FROM agent_acts WHERE project_id=:p AND role_key=:r AND kind <> 'run' "
                "AND (ended_at IS NULL OR ended_at > now() - make_interval(days => :d))"),
                {"p": ob.PROJECT, "r": forge, "d": days}).one()
        assert (row["work"]["done"], row["work"]["failed"], row["work"]["open"]) == tuple(direct)
        assert row["work"]["done"] == done


def test_nested_work_is_busy_time_once(client, engine):
    eng, _, forge, _ = _pair(engine)
    top = ob.root(engine)
    step = ob.act(engine, forge, parent=top, root=top, started=timedelta(hours=3),
                  ended=timedelta(hours=1))
    ob.act(engine, forge, parent=step, root=top, kind="stage", started=timedelta(hours=2, minutes=30),
           ended=timedelta(hours=2))
    ob.act(engine, forge, parent=step, root=top, kind="stage", started=timedelta(hours=2),
           ended=timedelta(hours=1, minutes=30))
    body = ob.board(client).json()
    busy = ob.agent_row(body, forge)["busy_seconds"]
    assert abs(busy - 2 * 3600) <= 2, busy  # the outer span, not 2h + 30m + 30m
    assert ob.team_row(body, eng)["members_load"][0] == {"role_key": forge, "busy_seconds": busy}


def test_an_old_open_item_waits_with_its_real_status(client, engine):
    eng, _, forge, _ = _pair(engine)
    top = ob.root(engine, started=timedelta(hours=21))
    old = ob.act(engine, forge, parent=top, root=top, status="active", ended=None,
                 started=timedelta(hours=20), label="Build · Old one", run_id="wfr_old")
    ob.act(engine, forge, parent=old, root=top, kind="stage", status="active", ended=None,
           started=timedelta(hours=19))  # inside its own step: not a second wait
    body = ob.board(client, 1).json()
    row = ob.team_row(body, eng)
    assert row["waiting_total"] == 1, row["waiting"]
    first = row["waiting"][0]
    assert first["act_id"] == old and first["status"] == "active"
    assert first["label"] == "Build · Old one" and first["run_id"] == "wfr_old"
    assert first["attention_id"] is None
    # One count of what waits: Open is the Waiting list's length, nested work counted once.
    assert row["work"]["open"] == row["waiting_total"] == 1


def test_an_unchanged_board_is_a_304_and_an_ending_act_changes_it(client, engine):
    _, _, forge, _ = _pair(engine)
    top = ob.root(engine)
    open_one = ob.act(engine, forge, parent=top, root=top, status="active", ended=None)
    got = ob.board(client)
    etag = got.headers["etag"]
    assert ob.board(client, **{"If-None-Match": etag}).status_code == 304
    with engine.begin() as c:
        c.execute(text("UPDATE agent_acts SET status='complete', ended_at=now() WHERE act_id=:a"),
                  {"a": open_one})
    again = ob.board(client, **{"If-None-Match": etag})
    assert again.status_code == 200 and again.headers["etag"] != etag


def test_a_window_it_does_not_offer_is_refused(client):
    for days in (0, 2, 14, 90):
        got = client.get("/v1/agents/org-board", params={"project_id": ob.PROJECT, "days": days})
        assert got.status_code == 422, (days, got.text)


def test_a_gate_holding_an_agents_work_is_its_teams_wait(client, engine, monkeypatch):
    wf.install_model(monkeypatch)
    herald = wf.new_agent(client, wf.key("herald"), holds=tuple(wf.skills(client, 1)))
    spec = {
        "entry_step": "draft",
        "input_schema": {"type": "object", "required": ["instructions", "title"],
                         "properties": {"instructions": {"type": "string"},
                                        "title": {"type": "string"}}},
        "steps": [wf.agent_step("draft", captain=herald, nxt=("publish",)),
                  wf.gate_step("publish", question="Publish it?")],
    }
    made = wf.published(client, spec)
    run_id = wf.started(client, made["definition"]["workflow_key"],
                        {"instructions": "Draft it.", "title": "Weekly"})
    for _ in range(4):
        wf.tick(client)
    view = wf.run_view(client, run_id)
    assert wf.statuses(view)["publish"] == "waiting", wf.statuses(view)
    decision_id = wf.steps_of(view)["publish"]["decision_id"]
    body = ob.board(client).json()
    yours = ob.team_row(body, "yours")
    gate = [w for w in yours["waiting"] if w["attention_id"] == decision_id]
    assert gate and gate[0]["role_key"] == herald and gate[0]["run_id"] == run_id, yours
    assert gate[0]["status"] == "awaiting_input"
    # D13: the card's needs-you badge and Open count include the gate, as Waiting does.
    assert yours["needs_you"] >= 1 and yours["waiting_you"] >= 1
    assert yours["work"]["open"] == yours["waiting_total"]
    assert ob.agent_row(body, herald)["work"]["done"] >= 1
