"""Agents › Activity: a river of agent work, inner work and tool calls folded under it
(agents-home spec §4.9, §6.4).

`GET /v1/agents/activity` is the project's acts, newest first, paged by cursor; unfiltered it
lists the work that stands on its own and folds the rest under it (`children.count`, read with
`parent=`). `turns.tool_calls` says how many tool calls fold under an item — 0 means no fold
control, never a promise — and `GET /v1/acts/{id}/tools` lists them: name, ok, latency. Never
their content: a tool call's arguments and result are the customer's code and data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

PROJECT = "boss-dogfood"
NOW = datetime.now(timezone.utc)
SECRET = "SECRET-PAYLOAD-NEVER-SHOWN"


def _seed(engine):
    from boss_engine.services import agent_acts

    with engine.begin() as c:
        def op(**kw):
            base = dict(project_id=PROJECT, handed_by_kind="boss", accountable="ceo",
                        how="named", status="active")
            base.update(kw)
            return agent_acts.open_act(c, **base)

        root = op(kind="run", role_key=None, how="started", handed_by_kind="person",
                  handed_by_id="ceo", label="Ship it #182", started_at=NOW - timedelta(minutes=50))
        plan = op(kind="step", role_key="pm", parent_act_id=root, label="PRD · Ship it #182",
                  started_at=NOW - timedelta(minutes=40), step_id="prd")
        build = op(kind="step", role_key="software_engineer", parent_act_id=plan,
                   how="model_pick", considered=["software_engineer", "backend_engineer"],
                   label="Build · Ship it #182", started_at=NOW - timedelta(minutes=30),
                   step_id="build")
        stages = [op(kind="stage", role_key="software_engineer", parent_act_id=build,
                     how="next_stage", label=f"Implement · Ship it #{n}", step_id="implement",
                     started_at=NOW - timedelta(minutes=29 - n)) for n in range(3)]
        chat = op(kind="chat", role_key="qa", label="Talk · flaky test",
                  handed_by_kind="person", handed_by_id="ceo", how="asked_by_person",
                  started_at=NOW - timedelta(minutes=5))
        for n, (direction, tool, ok, ms) in enumerate([
                ("tool_call", "read_file", True, 0), ("tool_result", "read_file", True, 120),
                ("tool_call", "run_tests", True, 0), ("tool_result", "run_tests", False, 340)]):
            c.execute(text(
                "INSERT INTO agent_turns (id, project_id, direction, tool_name, content, ok, "
                "latency_ms, act_id, turn_index, created_at) VALUES (:i, :p, :d, :t, :c, :ok, "
                ":ms, :a, :n, :at)"),
                {"i": f"turn_actv_{stages[0]}_{n}", "p": PROJECT, "d": direction, "t": tool,
                 "c": SECRET, "ok": ok, "ms": ms, "a": stages[0], "n": n,
                 "at": NOW - timedelta(minutes=28, seconds=-n)})
    return {"root": root, "plan": plan, "build": build, "stages": stages, "chat": chat}


def _feed(client, **params):
    got = client.get("/v1/agents/activity", params={"project_id": PROJECT, **params})
    assert got.status_code == 200, got.text
    return got.json()


def test_the_river_is_newest_first_with_inner_work_folded(client, engine):
    made = _seed(engine)
    items = _feed(client)["items"]
    ids = [i["act_id"] for i in items]
    # The work that stands on its own; the stages fold under the build; the run's root is
    # the person starting it, not agent work.
    assert ids[:3] == [made["chat"], made["build"], made["plan"]]
    assert made["root"] not in ids and not set(made["stages"]) & set(ids)
    build = next(i for i in items if i["act_id"] == made["build"])
    assert build["children"] == {"count": 3}
    assert build["verb"] and build["from"]["kind"] in ("agent", "person", "boss")
    starts = [i["started_at"] for i in items]
    assert starts == sorted(starts, reverse=True)
    # The fold opens with parent=.
    inside = _feed(client, parent=made["build"])["items"]
    assert {i["act_id"] for i in inside} == set(made["stages"])
    assert all(i["verb"] == "Coding" for i in inside)


def test_the_river_pages_by_cursor(client, engine):
    _seed(engine)
    first = _feed(client, limit=1)
    assert len(first["items"]) == 1 and first["next"]
    second = _feed(client, limit=1, cursor=first["next"])
    assert second["items"][0]["act_id"] != first["items"][0]["act_id"]
    assert second["items"][0]["started_at"] <= first["items"][0]["started_at"]


def test_filters_select_what_they_say(client, engine):
    made = _seed(engine)
    handoffs = _feed(client, kinds="handoffs")["items"]
    assert [i["act_id"] for i in handoffs if i["act_id"] in made.values()] == [made["build"]]
    assert all(i["how"] in ("captain", "asked_by_agent", "model_pick") or i["kind"] == "handoff"
               for i in handoffs)
    builds = _feed(client, kinds="builds")["items"]
    assert {i["act_id"] for i in builds} >= set(made["stages"])
    assert all(i["kind"] == "stage" for i in builds)
    talks = _feed(client, kinds="talks")["items"]
    assert [i["act_id"] for i in talks] == [made["chat"]]
    mine = _feed(client, role="pm")["items"]
    assert [i["act_id"] for i in mine] == [made["plan"]]
    assert client.get("/v1/agents/activity",
                      params={"project_id": PROJECT, "kinds": "gossip"}).status_code == 422


def test_tool_calls_fold_under_their_act_and_never_carry_content(client, engine):
    made = _seed(engine)
    inside = {i["act_id"]: i for i in _feed(client, parent=made["build"])["items"]}
    assert inside[made["stages"][0]]["turns"] == {"tool_calls": 2}
    # An act with no turns shows no fold control.
    assert inside[made["stages"][1]]["turns"] == {"tool_calls": 0}
    got = client.get(f"/v1/acts/{made['stages'][0]}/tools", params={"project_id": PROJECT})
    assert got.status_code == 200, got.text
    body = got.json()
    assert SECRET not in got.text
    assert [(t["tool_name"], t["ok"], t["latency_ms"]) for t in body["items"]] == [
        ("read_file", True, 120), ("run_tests", False, 340)]
    assert all(set(t) == {"tool_name", "ok", "latency_ms", "at"} for t in body["items"])
    assert body["more"] is False
    other = client.get(f"/v1/acts/{made['stages'][0]}/tools", params={"project_id": "elsewhere"})
    assert other.status_code in (403, 404)
