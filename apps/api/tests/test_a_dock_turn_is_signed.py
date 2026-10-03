"""A question to an agent in the dock is signed work (Agent World §5.1 hook 6, §12.4 test 9).

One act per turn, asked by the person, parented to what the dock was opened about, with its id
and the definition's version on the stored messages. The reply's [act:…] citations come back
only when they are this project's real acts. BOSS-in-general is not an agent and signs nothing.
The model's side of the turn is stood in for at the router, the seam every turn goes through.
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _stub_router(monkeypatch, reply: str) -> None:
    from apps.api.app import main as api_main

    def route_turn(*_args, **_kwargs):
        return SimpleNamespace(reply=reply, tool_calls=[], actions=[], proposed_actions=[],
                               pending_confirmation=None, next_steps=[])

    monkeypatch.setattr(api_main.orchestration_agent.router, "route_turn", route_turn)


def _about(engine) -> str:
    """Something the dock can be opened about: an act of qa's, signed by the ledger itself."""
    from boss_engine.services import agent_acts

    with engine.begin() as c:
        c.execute(text("UPDATE roles SET fingerprint = 'fp_qa_v4', identity_version = 4 "
                       "WHERE project_id = :p AND role_key = 'qa'"), {"p": PROJECT})
        act_id = agent_acts.open_act(
            c, project_id=PROJECT, kind="step", role_key="qa", how="named",
            handed_by_kind="boss", accountable="ceo", label="Run the tests · Ship it")
        agent_acts.close_act(c, act_id, status="complete")
    return act_id


def _chat(client, **body):
    said = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": "executive_console", **body})
    assert said.status_code == 200, said.text
    return said.json()


def test_a_question_to_an_agent_is_one_act_under_what_it_was_about(client, engine, monkeypatch):
    about = _about(engine)
    _stub_router(monkeypatch, f"I ran this [act:{about}]; not [act:act_01NOTAREALACT0000000000].")

    reply = _chat(client, role="qa", message="What did you check?",
                  context={"act_id": about})
    assert reply["act_id"]
    assert reply["cited_acts"] == [about]

    with engine.begin() as c:
        act = dict(c.execute(text("SELECT * FROM agent_acts WHERE act_id = :a"),
                             {"a": reply["act_id"]}).mappings().one())
        stored = c.execute(text("SELECT role, act_id, fingerprint FROM agent_messages "
                                "WHERE thread_id = :t ORDER BY created_at"),
                           {"t": reply["thread_id"]}).mappings().all()
    assert (act["kind"], act["role_key"], act["how"], act["status"]) == (
        "chat", "qa", "asked_by_person", "complete")
    assert (act["handed_by_kind"], act["handed_by_id"], act["accountable"]) == (
        "person", "executive_console", "executive_console")
    assert act["parent_act_id"] == about
    assert act["thread_id"] == reply["thread_id"]
    assert (act["fingerprint"], act["identity_version"]) == ("fp_qa_v4", 4)
    # One turn, both its messages (they share one transaction's timestamp, so no order).
    assert sorted((m["role"], m["act_id"], m["fingerprint"]) for m in stored) == [
        ("assistant", reply["act_id"], "fp_qa_v4"), ("user", reply["act_id"], "fp_qa_v4")]

    one = client.get(f"/v1/acts/{reply['act_id']}", params={"project_id": PROJECT}).json()
    assert one["edge_label"] == "Asked by you"
    assert one["from"] == {"kind": "person", "id": "executive_console"}
    assert one["turns_excerpt"][0]["text"].startswith("I ran this")


def test_a_title_reaches_the_same_agent_and_boss_in_general_signs_nothing(
        client, engine, monkeypatch):
    _stub_router(monkeypatch, "Here is what I know.")
    by_title = _chat(client, role="QA Lead", message="Anything risky?")
    assert by_title["act_id"]
    with engine.begin() as c:
        role = c.execute(text("SELECT role_key FROM agent_acts WHERE act_id = :a"),
                         {"a": by_title["act_id"]}).scalar()
    assert role == "qa"

    general = _chat(client, message="What can you do?")
    assert general["act_id"] is None and general["cited_acts"] == []


def test_the_dock_cannot_hang_a_turn_under_another_projects_work(client, engine, monkeypatch):
    from boss_engine.services import agent_acts

    with engine.begin() as c:
        elsewhere = agent_acts.open_act(
            c, project_id="some-other-project", kind="step", role_key="qa", how="named",
            handed_by_kind="boss", accountable="someone", label="Not yours")
    _stub_router(monkeypatch, f"See [act:{elsewhere}].")
    reply = _chat(client, role="qa", message="And this?", context={"act_id": elsewhere})
    with engine.begin() as c:
        parent = c.execute(text("SELECT parent_act_id FROM agent_acts WHERE act_id = :a"),
                           {"a": reply["act_id"]}).scalar()
    assert parent is None
    assert reply["cited_acts"] == []


def test_chat_turns_expire_after_ninety_days_and_other_work_never_does(client, engine,
                                                                       monkeypatch):
    from boss_engine.services import agent_acts

    _stub_router(monkeypatch, "Noted.")
    fresh = _chat(client, role="qa", message="Today?")["act_id"]
    old = _chat(client, role="qa", message="Long ago?")["act_id"]
    with engine.begin() as c:
        step = agent_acts.open_act(
            c, project_id=PROJECT, kind="step", role_key="qa", how="named",
            handed_by_kind="boss", accountable="ceo", label="Old work")
        agent_acts.close_act(c, step, status="complete")
        c.execute(text("UPDATE agent_acts SET started_at = now() - interval '120 days', "
                       "ended_at = now() - interval '120 days' WHERE act_id IN (:o, :s)"),
                  {"o": old, "s": step})
        agent_acts.reconcile(c, PROJECT)
        left = set(c.execute(text("SELECT act_id FROM agent_acts WHERE act_id IN (:f, :o, :s)"),
                             {"f": fresh, "o": old, "s": step}).scalars())
    assert left == {fresh, step}
