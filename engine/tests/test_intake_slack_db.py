"""Slack conversations and the reply outbox, on Postgres (P9a-3 BOSSAPP-1095, P9a-6 BOSSAPP-1098;
boss-eventing-lld.md §3.7, §5.2, §7.1-7.2, §9, §14 T3/T6/T7/T9).

The Slack side is a FAKE Slack Web API (`FakeSlack`, chat_adapters.TRANSPORT); the Events API side
is real signed requests through the intake front door. Agent runs execute on the local worker path
(agent_run_worker.execute) with a scripted model (bots.PARTS_HOOK), so the reply tools, the fence,
the settle hook and the sender are the production code.

  T3  every Slack delivery replayed 1-3 times with X-Slack-Retry-Num → runs = distinct event_ids
  T6  an agent answering in a `channel_all` channel: 100 echoes of its own replies → 0 runs;
      bot-to-bot ping-pong stops at hop 3
  T7  an unlinked person → a link prompt, no run
  T9  event → the first visible status in Slack, p95 ≤ 1.5 s (NOTIFY-woken processor and sender)
  a 10-turn thread conversation: follow-ups without a mention continue the SAME BOSS thread, each
      answer edited into its own "Looking into it…" message
  the outbox: 429 honoured through Retry-After without spending an attempt, dedupe per tool call,
      op_seq order, a stale epoch refused, the channel bucket, the bot token never leaving it
  which agent (§5.2): a leading name, a channel binding, the default DM agent, the list reply
  Socket Mode: one leased consumer; an envelope stored once and acked after commit
"""
from __future__ import annotations

import json
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.services import (
    chat_adapters,
    chat_reply,
    event_intake,
    intake_guards,
    notify,
    scoped_budgets,
)

from ._intake_support import (
    SLACK_BOT_ID,
    SLACK_BOT_TOKEN,
    SLACK_BOT_USER,
    FakeSlack,
    channel_message,
    deploy_local_agent,
    direct_message,
    event_row,
    link_chat_user,
    make_person,
    make_project,
    make_route,
    make_slack,
    mention,
    pg_engine,
    purge_slack,
    runs_of,
    slack_body,
    slack_request,
)


@pytest.fixture()
def world(monkeypatch):
    engine = pg_engine()
    pid = make_project(engine)
    event_intake.BUCKETS.reset()
    chat_reply.BUCKETS.reset()
    intake_guards.reset_for_tests()
    scoped_budgets.reset_for_tests()
    fake = FakeSlack()
    monkeypatch.setattr(chat_adapters, "TRANSPORT", fake)
    try:
        yield engine, pid, fake
    finally:
        purge_slack(engine, pid)


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _receive(engine, slack: dict, event: dict, *, event_id=None, retry: int = 0) -> dict:
    got = event_intake.receive(engine, "slack", slack["token"],
                               slack_request(slack_body(event, event_id=event_id), retry=retry))
    assert got["http_status"] == 200, got
    return got


def _process(engine) -> dict:
    return event_intake.process_due(engine, _settings())


def _deliver(engine, *, at: float = 0.0) -> dict:
    return notify.deliver_chat_due(engine, _settings(),
                                   now=datetime.now(timezone.utc) + timedelta(seconds=at))


def _flush(engine) -> None:
    """Send everything queued, as a person typing at human speed would let it go (the channel
    bucket refilled between turns)."""
    for _ in range(6):
        chat_reply.BUCKETS.reset()
        got = _deliver(engine, at=3)
        if not (got["sent"] or got["skipped"] or got["waiting"]):
            return


def _outbox(engine, pid: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT * FROM chat_outbox WHERE project_id = :p ORDER BY created_at, op_seq"),
            {"p": pid}).mappings().all()]


def _person(engine, slack: dict, user: str = "U0PRIYA") -> str:
    principal = make_person(engine, f"{user.lower()}-{uuid.uuid4().hex[:6]}@intake.test")
    link_chat_user(engine, slack["endpoint"]["id"], user, principal)
    return principal


def _route(engine, pid, dep, slack, **kw):
    return make_route(engine, pid, dep["id"], slack["connection"]["id"], **kw)


# ═══ the adapter at the front door ════════════════════════════════════════════════════════════

def test_url_verification_signature_and_stale_requests(world):
    engine, pid, _fake = world
    slack = make_slack(engine, pid)
    assert slack["connection"]["chat_endpoint_id"] == slack["endpoint"]["id"]
    assert "signing_secret" not in slack["connection"]["secret_fields"], \
        "the endpoint's secret is reused, never copied or minted (D1)"
    shake = event_intake.receive(engine, "slack", slack["token"], slack_request(
        {"type": "url_verification", "challenge": "c-123", "token": "x"}))
    assert shake["http_status"] == 200 and shake["body"] == {"challenge": "c-123"}
    forged = event_intake.receive(engine, "slack", slack["token"],
                                  slack_request(slack_body(mention()), secret="wrong"))
    assert forged["http_status"] == 401 and forged["body"]["reason"] == "bad_signature"
    stale = event_intake.receive(engine, "slack", slack["token"], slack_request(
        slack_body(mention()), timestamp=int(time.time()) - 600))
    assert stale["http_status"] == 401 and stale["body"]["reason"] == "stale_timestamp"
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM intake_events WHERE connection_id = :c"),
                         {"c": slack["connection"]["id"]}).scalar() == 0


def test_parse_types_conversations_and_self_filters(world):
    engine, pid, _fake = world
    slack = make_slack(engine, pid)
    root = "1731577200.000100"
    cases = [
        (mention("help", ts=root), "com.slack.message.app_mention", f"C0SUPPORT/{root}"),
        (channel_message("more detail", thread_ts=root), "com.slack.message.thread_reply",
         f"C0SUPPORT/{root}"),
        (channel_message("lunch?"), "com.slack.message.channel", None),
        (direct_message("hello", ts="1731577200.000200"), "com.slack.message.im",
         "D0PRIYA/2024-11-14"),
    ]
    ids = {}
    for ev, _type, _key in cases:
        ids[_type] = _receive(engine, slack, ev)["event_id"]
    # A channel message that @mentions us (Slack also sends the app_mention), an edit, our own
    # bot's message: never an event a route sees.
    dup = _receive(engine, slack, channel_message(f"<@{SLACK_BOT_USER}> help"))["event_id"]
    edit = _receive(engine, slack, {**channel_message("x"), "subtype": "message_changed"})[
        "event_id"]
    ours = _receive(engine, slack, channel_message("an answer", user=SLACK_BOT_USER,
                                                   bot_id=SLACK_BOT_ID))["event_id"]
    _process(engine)
    for ev, type_, key in cases:
        row = event_row(engine, ids[type_])
        assert row["type"] == type_, row
        if key:
            assert row["conversation_key"] == key
    assert event_row(engine, ids["com.slack.message.app_mention"])["envelope"]["data"]["text"] \
        == "help", "the @mention that addressed us is taken off the person's words"
    assert event_row(engine, dup)["status"] == "ignored"
    assert event_row(engine, edit)["status"] == "ignored"
    assert event_row(engine, ours)["status"] == "filtered"


# ═══ T3: redelivery is one run ════════════════════════════════════════════════════════════════

def test_t3_slack_redelivery_creates_exactly_one_run_per_event_id(world):
    engine, pid, _fake = world
    slack = make_slack(engine, pid)
    _person(engine, slack)
    dep = deploy_local_agent(engine, pid)
    _route(engine, pid, dep, slack, name="support", identity_mode="person")
    distinct, latencies = 12, []
    for n in range(distinct):
        ev = mention(f"question {n}", ts=f"17315772{n:02d}.000100")
        event_id = f"EvT3{n:04d}"
        for retry in range(1 + n % 3):
            started = time.perf_counter()
            got = _receive(engine, slack, ev, event_id=event_id, retry=retry)
            latencies.append(time.perf_counter() - started)
            assert got["outcome"] == ("received" if retry == 0 else "duplicate"), got
    while _process(engine)["claimed"]:
        pass
    runs = runs_of(engine, pid)
    assert len(runs) == distinct, runs
    with engine.begin() as c:
        seen = c.execute(text(
            "SELECT sum(duplicates), max(retry_count_seen) FROM intake_events "
            "WHERE connection_id = :c"), {"c": slack["connection"]["id"]}).one()
    assert int(seen[0]) == sum(n % 3 for n in range(distinct)) and int(seen[1]) == 2
    assert sorted(latencies)[int(len(latencies) * 0.95) - 1] < 0.5, \
        "the front door answers well inside Slack's 3 s"


# ═══ T6: loops ════════════════════════════════════════════════════════════════════════════════

def test_t6_agent_replies_in_a_channel_all_channel_never_wake_it(world):
    engine, pid, fake = world
    slack = make_slack(engine, pid)
    _person(engine, slack)
    dep = deploy_local_agent(engine, pid)
    _route(engine, pid, dep, slack, name="desk", identity_mode="person",
           config={"addressing": "channel_all"})
    # One person's message: one run, and its status message goes out carrying our marker.
    _receive(engine, slack, channel_message("the payments job failed"))
    _process(engine)
    assert len(runs_of(engine, pid)) == 1
    _deliver(engine)
    status = fake.named("chat.postMessage")[-1]
    assert status["metadata"]["event_type"] == "boss_run"
    assert status["metadata"]["event_payload"]["endpoint_id"] == slack["endpoint"]["id"]
    # 100 echoes of the agent's own replies (Slack sends our posts back as message events).
    for n in range(100):
        echo = channel_message(f"answer {n}", user=SLACK_BOT_USER, bot_id=SLACK_BOT_ID,
                               metadata=status["metadata"])
        if n % 2:
            echo.pop("user")          # some echoes carry only the bot id and the marker
            echo["subtype"] = "bot_message"
        _receive(engine, slack, echo)
    while _process(engine)["claimed"]:
        pass
    assert len(runs_of(engine, pid)) == 1, "0 self-triggered runs over 100 replies"


def test_t6_bot_ping_pong_stops_at_hop_three(world):
    engine, pid, _fake = world
    slack = make_slack(engine, pid)
    dep = deploy_local_agent(engine, pid)
    route = _route(engine, pid, dep, slack, name="desk", identity_mode="owner",
                   config={"addressing": "channel_all", "allow_bots": ["B0OTHER"]})
    stranger = channel_message("hi from a bot", user="U0OTHERBOT", bot_id="B0OTHER",
                               subtype="bot_message", username="otherbot")
    stranger.pop("user")
    unlisted = _receive(engine, slack, {**stranger, "bot_id": "B0UNLISTED"})["event_id"]
    ids = []
    for hops in (1, 2, 3, 4):
        ev = {**stranger, "ts": f"17315772{hops:02d}.000300",
              "metadata": {"event_type": "boss_run",
                           "event_payload": {"run_id": f"run_x{hops}", "hops": hops,
                                             "deployment_id": "dep_elsewhere",
                                             "endpoint_id": "che_another_install"}}}
        ids.append(_receive(engine, slack, ev)["event_id"])
    while _process(engine)["claimed"]:
        pass
    assert event_row(engine, unlisted)["status"] == "filtered", "a bot not in allow_bots"
    statuses = [event_row(engine, i)["status"] for i in ids]
    assert statuses[:2] == ["routed", "routed"] and statuses[2:] == ["filtered", "filtered"], \
        statuses
    assert len(runs_of(engine, pid)) == 2
    with engine.begin() as c:
        dropped = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND "
                                 "event_type = 'intake.loop_dropped'"), {"p": pid}).scalar()
        hops_sent = [int((r[0] or {}).get("hops") or 0) for r in c.execute(text(
            "SELECT payload FROM chat_outbox WHERE project_id = :p AND run_id IS NOT NULL"),
            {"p": pid}).all()]
    assert dropped == 2
    assert sorted(hops_sent) == [2, 3], "our replies carry the incoming hop count + 1"
    assert {r["trigger_id"] for r in runs_of(engine, pid)} == {route["id"]}


# ═══ T7: an unlinked person ═══════════════════════════════════════════════════════════════════

def test_t7_an_unlinked_person_gets_a_link_prompt_and_no_run(world):
    engine, pid, fake = world
    slack = make_slack(engine, pid)
    dep = deploy_local_agent(engine, pid)
    _route(engine, pid, dep, slack, name="support")          # identity: person (Slack default)
    first = _receive(engine, slack, mention("who am I?", user="U0STRANGER"))["event_id"]
    second = _receive(engine, slack, mention("hello?", user="U0STRANGER"))["event_id"]
    while _process(engine)["claimed"]:
        pass
    assert runs_of(engine, pid) == []
    assert event_row(engine, first)["status"] == "refused"
    assert event_row(engine, second)["status"] == "refused"
    prompts = [r for r in _outbox(engine, pid) if (r["payload"] or {}).get("op") == "dm"]
    assert len(prompts) == 1, "one link prompt an hour"
    _deliver(engine)
    dm = fake.named("chat.postMessage")[-1]
    assert "Link your chat account to BOSS" in dm["text"]


# ═══ which agent (§5.2) ═══════════════════════════════════════════════════════════════════════

def test_which_agent_name_binding_default_dm_and_the_list_reply(world):
    engine, pid, fake = world
    slack = make_slack(engine, pid, config={"default_dm_agent": "billing"})
    _person(engine, slack)
    support = deploy_local_agent(engine, pid, key="support")
    billing = deploy_local_agent(engine, pid, key="billing")
    r_support = _route(engine, pid, support, slack, name="support", priority=10,
                       identity_mode="person", config={"aliases": ["helpdesk"]})
    r_billing = _route(engine, pid, billing, slack, name="billing", priority=20,
                       identity_mode="person", config={"channels": ["C0BILLING"]})
    named = _receive(engine, slack, mention("helpdesk: is the VPN down?"))["event_id"]
    bound = _receive(engine, slack, mention("refund please", channel="C0BILLING"))["event_id"]
    dm = _receive(engine, slack, direct_message("my invoice is wrong"))["event_id"]
    vague = _receive(engine, slack, mention("hello?", channel="C0RANDOM"))["event_id"]
    while _process(engine)["claimed"]:
        pass
    by_trigger = {r["trigger_id"]: 0 for r in runs_of(engine, pid)}
    for run in runs_of(engine, pid):
        by_trigger[run["trigger_id"]] += 1
    assert by_trigger == {r_support["id"]: 1, r_billing["id"]: 2}, by_trigger

    def admitted(event_id):
        return [o["trigger_id"] for o in event_row(engine, event_id)["outcome"]
                if o.get("fire_outcome") == "admitted"]

    assert admitted(named) == [r_support["id"]]
    assert admitted(bound) == [r_billing["id"]] and admitted(dm) == [r_billing["id"]]
    assert event_row(engine, named)["envelope"]["data"]["text"] == "is the VPN down?"
    assert event_row(engine, vague)["status"] == "filtered"
    _deliver(engine)
    listed = [c for c in fake.named("chat.postEphemeral") if "Which agent" in c["text"]]
    assert len(listed) == 1 and "*billing*" in listed[0]["text"] \
        and "*support*" in listed[0]["text"]


# ═══ the 10-turn conversation ═════════════════════════════════════════════════════════════════

class _Parts:
    """The deployed agent's model: odd turns answer with chat_reply, even turns just say it (the
    settle hook posts it). Every model request is kept, so history can be checked."""

    def __init__(self):
        self.turn = 0
        self.requests: list = []

    def __call__(self, connection, settings, run, version_row):
        from boss_engine.kernel.scripted import Call, Reply, Say, ScriptedEngine
        from boss_engine.kernel.toolset import FunctionToolSet

        self.turn += 1
        n = self.turn
        parts = self

        def answer(messages, tools):
            parts.requests.append((n, [m.get("content") for m in messages
                                       if m.get("role") == "user"], tools))
            if n % 2:
                return Call("chat_reply", {"text": f"answer {n}"}, id=f"c{n}")
            return Say(f"answer {n}")

        engine = ScriptedEngine([Reply(answer), Say("done")])
        return {"engine": engine, "tools": FunctionToolSet([])}


def _execute_due_runs(engine, pid: str) -> list[dict]:
    from boss_engine.services import agent_run_worker
    from boss_engine.services.jobs import JobLeaseService

    done = []
    while True:
        with engine.begin() as c:
            job = c.execute(text(
                "SELECT id, kind, payload->>'run_id' AS run_id FROM jobs WHERE project_id = :p "
                "AND kind IN ('agent.run', 'agent.resume') AND status = 'queued' "
                "ORDER BY created_at LIMIT 1"), {"p": pid}).mappings().first()
            if job is None:
                return done
            leased = JobLeaseService(_settings()).lease_owned_job(
                c, project_id=pid, job_id=job["id"], worker_id="w-slack", lease_ttl_seconds=60)
        done.append(agent_run_worker.execute(
            engine, _settings(), kind=job["kind"], job_id=job["id"], run_id=job["run_id"],
            worker_id="w-slack", lease_token=leased["lease_token"]))


def test_a_ten_turn_thread_conversation_continues_one_boss_thread(world, monkeypatch):
    from boss_engine.services import bots

    engine, pid, fake = world
    parts = _Parts()
    monkeypatch.setattr(bots, "PARTS_HOOK", parts)
    slack = make_slack(engine, pid)
    _person(engine, slack)
    dep = deploy_local_agent(engine, pid)
    _route(engine, pid, dep, slack, name="support", identity_mode="person")
    root = "1731577300.000100"
    for turn in range(1, 11):
        ev = (mention("why did last night's payments job fail?", ts=root) if turn == 1 else
              channel_message(f"follow-up {turn}", ts=f"17315773{turn:02d}.000200",
                              thread_ts=root))
        _receive(engine, slack, ev)
        got = _process(engine)
        assert got["routed"] == 1, (turn, got)
        _flush(engine)                                 # "Looking into it…" goes first
        outcome = _execute_due_runs(engine, pid)
        assert [o.get("status") for o in outcome] == ["completed"], outcome
        _flush(engine)
    runs = runs_of(engine, pid)
    assert len(runs) == 10 and len({r["thread_id"] for r in runs}) == 1, \
        "every follow-up continued the SAME BOSS thread"
    posts = fake.named("chat.postMessage")
    updates = fake.named("chat.update")
    assert len(posts) == 10 and all(p["thread_ts"] == root for p in posts)
    assert all(p["text"] == chat_reply.DEFAULT_STATUS for p in posts)
    assert sorted(u["text"] for u in updates) == sorted(f"answer {n}" for n in range(1, 11))
    with engine.begin() as c:
        status_ts = {str((r[0] or {}).get("ts")) for r in c.execute(text(
            "SELECT receipt FROM chat_outbox WHERE project_id = :p AND kind = 'agent_status'"),
            {"p": pid}).all()}
    assert {u["ts"] for u in updates} == status_ts and len(status_ts) == 10, \
        "each answer edits its own status message in place"
    assert all(u["metadata"]["event_type"] == "boss_run" for u in updates)
    tenth = [r for r in parts.requests if r[0] == 10][0]
    seen = " ".join(str(x) for x in tenth[1])
    assert "payments job" in seen and "follow-up 9" in seen and "follow-up 10" in seen, \
        "the agent remembers what came before"
    assert "chat_reply" in (tenth[2] or [])


# ═══ the outbox ═══════════════════════════════════════════════════════════════════════════════

def _admitted_run(engine, pid, fake):
    slack = make_slack(engine, pid)
    _person(engine, slack)
    dep = deploy_local_agent(engine, pid)
    _route(engine, pid, dep, slack, name="support", identity_mode="person")
    _receive(engine, slack, mention("status?"))
    _process(engine)
    run = runs_of(engine, pid)[0]
    return slack, run


def test_outbox_honours_429_retry_after_without_spending_an_attempt(world):
    engine, pid, fake = world
    _slack, run = _admitted_run(engine, pid, fake)
    fake.fail_next("chat.postMessage", ("429", "7"))
    got = _deliver(engine)
    assert got["rate_limited"] == 1 and got["sent"] == 0
    row = [r for r in _outbox(engine, pid) if r["run_id"] == run["id"]][0]
    assert row["status"] == "pending" and row["attempts"] == 0 and row["rate_limited"] == 1
    wait = (row["next_attempt_at"] - datetime.now(timezone.utc)).total_seconds()
    assert 5 < wait <= 7.5, wait
    assert _deliver(engine)["sent"] == 0, "not before Retry-After"
    chat_reply.BUCKETS.reset()
    assert _deliver(engine, at=8)["sent"] == 1
    row = [r for r in _outbox(engine, pid) if r["run_id"] == run["id"]][0]
    assert row["status"] == "sent" and row["attempts"] == 1


def test_outbox_dedupes_orders_fences_and_never_holds_the_token(world):
    engine, pid, fake = world
    _slack, run = _admitted_run(engine, pid, fake)
    with engine.begin() as c:
        first = chat_reply.say(c, run["id"], "half way", final=False, kind="agent_reply",
                               dedupe=f"chat:{run['id']}:1:chat_reply:k1")
        again = chat_reply.say(c, run["id"], "half way", final=False, kind="agent_reply",
                               dedupe=f"chat:{run['id']}:1:chat_reply:k1")
        final = chat_reply.say(c, run["id"], "the answer", final=True, kind="agent_reply",
                               dedupe=f"chat:{run['id']}:2:chat_reply:k2")
    assert final["ok"] and not final["duplicate"]
    assert again["duplicate"] and again["outbox_ids"] == first["outbox_ids"]
    rows = [r for r in _outbox(engine, pid) if r["run_id"] == run["id"]]
    assert [r["op_seq"] for r in rows] == [1, 2, 3]
    assert [r["payload"]["op"] for r in rows] == ["post", "update", "update"]
    # The status post fails once: nothing after it in the run goes out of order.
    fake.fail_next("chat.postMessage", ("500",))
    got = _deliver(engine)
    assert got["failed"] == 1 and got["sent"] == 0 and fake.named("chat.update") == []
    for _ in range(4):
        chat_reply.BUCKETS.reset()
        _deliver(engine, at=120)
    updates = fake.named("chat.update")
    assert [u["text"] for u in updates] == ["the answer"], \
        "the superseded edit is skipped; the final one edits the status message"
    with engine.begin() as c:
        status_ts = c.execute(text(
            "SELECT receipt->>'ts' FROM chat_outbox WHERE run_id = :r AND op_seq = 1"),
            {"r": run["id"]}).scalar()
    assert updates[0]["ts"] == status_ts
    # A stale executor (an older epoch than the run's job now holds) is refused.
    with engine.begin() as c:
        job_id = c.execute(text("SELECT job_id FROM agent_runs WHERE id = :r"),
                           {"r": run["id"]}).scalar()
        c.execute(text("UPDATE jobs SET fence_epoch = 5 WHERE id = :j"), {"j": job_id})
    with pytest.raises(chat_reply.ReplyFenced):
        with engine.begin() as c:
            chat_reply.say(c, run["id"], "from a dead replica", final=True, kind="agent_reply",
                           dedupe=f"chat:{run['id']}:9:stale", epoch=4)
    with engine.begin() as c:
        ok = chat_reply.say(c, run["id"], "from the holder", final=True, kind="agent_reply",
                            dedupe=f"chat:{run['id']}:9:fresh", epoch=5)
    assert ok["ok"]
    # The bot token is only ever in the Authorization header of the call the sender made.
    with engine.begin() as c:
        dumped = json.dumps([dict(r) for r in c.execute(text(
            "SELECT payload, receipt, last_error FROM chat_outbox WHERE project_id = :p"),
            {"p": pid}).mappings().all()], default=str)
        runs = json.dumps([dict(r) for r in c.execute(text(
            "SELECT finalize_ctx, messages FROM agent_runs WHERE project_id = :p"),
            {"p": pid}).mappings().all()], default=str)
    assert SLACK_BOT_TOKEN not in dumped and SLACK_BOT_TOKEN not in runs
    assert all(c["auth"] == f"Bearer {SLACK_BOT_TOKEN}" for c in fake.calls)
    # The tool itself, called by a stale run-plane replica (the epoch its call was fenced at):
    # refused in words, nothing written.
    from boss_engine.kernel.types import ToolCall

    before = len(_outbox(engine, pid))
    token = chat_reply.FENCE_EPOCH.set(4)
    try:
        with engine.begin() as c:
            tools = chat_reply.ChatToolSet(c, _settings(), {"id": run["id"]})
            assert [t.name for t in tools.schemas(step=1, state=None)] == list(
                chat_reply.TOOL_NAMES)
            got = tools.invoke(ToolCall(id="k9", name="chat_reply",
                                        arguments={"text": "late"}),
                               state=type("S", (), {"step": 3})())
    finally:
        chat_reply.FENCE_EPOCH.reset(token)
    assert got.status == "refused" and got.content["error"] == "fenced"
    assert len(_outbox(engine, pid)) == before


def test_streaming_behind_the_flag_starts_appends_and_stops_one_stream(world, monkeypatch):
    engine, pid, fake = world
    monkeypatch.setenv("BOSS_SLACK_STREAMING", "1")
    slack = make_slack(engine, pid)
    _person(engine, slack)
    dep = deploy_local_agent(engine, pid)
    _route(engine, pid, dep, slack, name="support", identity_mode="person",
           config={"stream": True})
    _receive(engine, slack, mention("stream it", ts="1731577400.000100"))
    _process(engine)
    run = runs_of(engine, pid)[0]
    with engine.begin() as c:
        for n, (words, final) in enumerate([("Reading the logs", False),
                                            ("Reading the logs. The job", False),
                                            ("Reading the logs. The job timed out.", True)]):
            chat_reply.say(c, run["id"], words, final=final, kind="agent_reply",
                           dedupe=f"chat:{run['id']}:{n}")
    _flush(engine)
    assert fake.named("reactions.add")[0]["name"] == "eyes", "the first sign of life"
    start = fake.named("chat.startStream")
    assert len(start) == 1 and start[0]["thread_ts"] == "1731577400.000100"
    assert start[0]["recipient_user_id"] == "U0PRIYA"
    appended = "".join(a.get("markdown_text", "") for a in fake.named("chat.appendStream"))
    stopped = fake.named("chat.stopStream")
    assert len(stopped) == 1 and appended + stopped[0].get("markdown_text", "") == \
        ". The job timed out."
    assert stopped[0]["metadata"]["event_type"] == "boss_run"
    assert fake.named("chat.postMessage") == [], "no status post when streaming"


def test_the_channel_bucket_paces_a_burst(world):
    engine, pid, fake = world
    _slack, run = _admitted_run(engine, pid, fake)
    with engine.begin() as c:
        chat_reply.say(c, run["id"], "x" * (chat_reply.MAX_CHUNK * 5), final=True,
                       kind="agent_reply", dedupe=f"chat:{run['id']}:long")
    sent = waiting = 0
    for _ in range(6):                     # a run's rows go one after another, in op_seq order
        got = _deliver(engine)
        sent, waiting = sent + got["sent"], waiting + got["waiting"]
    # the burst (3) goes at once; the rest wait for tokens, no attempt spent
    assert sent == 3 and waiting >= 1, (sent, waiting)
    time.sleep(1.1)
    assert _deliver(engine, at=10)["sent"] == 1, "one token a second"
    _flush(engine)
    rows = [r for r in _outbox(engine, pid) if r["run_id"] == run["id"]]
    assert all(r["status"] == "sent" for r in rows) and all(r["attempts"] == 1 for r in rows)


# ═══ T9: the first visible status ═════════════════════════════════════════════════════════════

def test_t9_event_to_first_visible_status_p95_under_1_5_s(world):
    engine, pid, fake = world
    slack = make_slack(engine, pid)
    _person(engine, slack)
    dep = deploy_local_agent(engine, pid)
    _route(engine, pid, dep, slack, name="support", identity_mode="person",
           config={"double_text": "enqueue"})
    settings = _settings()
    event_intake.start(engine, settings)
    chat_reply.start(engine, settings)
    try:
        time.sleep(1.0)                               # the LISTENs are up
        latencies = []
        for n in range(20):
            words = f"probe {uuid.uuid4().hex[:8]}"
            channel = f"C0LAT{n:02d}"
            started = time.monotonic()
            _receive(engine, slack, mention(words, channel=channel))
            deadline = started + 5.0
            seen = None
            while time.monotonic() < deadline and seen is None:
                with fake.lock:
                    seen = next((c["at"] for c in fake.calls if c["name"] == "chat.postMessage"
                                 and c["body"].get("channel") == channel), None)
                if seen is None:
                    time.sleep(0.01)
            assert seen is not None, f"no status for event {n}"
            latencies.append(seen - started)
        p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
        assert p95 <= 1.5, (p95, statistics.median(latencies))
    finally:
        event_intake.stop()
        chat_reply.stop()
        time.sleep(0.3)


# ═══ Socket Mode ══════════════════════════════════════════════════════════════════════════════

class _Socket:
    def __init__(self, frames):
        self.frames, self.sent, self.closed = list(frames), [], False

    def recv(self, timeout=None):
        if not self.frames:
            return json.dumps({"type": "disconnect", "reason": "refresh_requested"})
        return self.frames.pop(0)

    def send(self, message):
        self.sent.append(json.loads(message))

    def close(self):
        self.closed = True


def test_socket_mode_one_leased_consumer_stores_once_and_acks_after_commit(world, monkeypatch):
    from boss_engine.connectors.intake.slack import socket_mode

    engine, pid, fake = world
    slack = make_slack(engine, pid, mode="socket", secrets={"app_token": "xapp-test-token"})
    cid = slack["connection"]["id"]
    assert socket_mode.take_lease(engine, cid, "holder-a")
    assert not socket_mode.take_lease(engine, cid, "holder-b"), "one consumer install-wide"
    assert socket_mode.take_lease(engine, cid, "holder-a"), "the holder renews"
    payload = slack_body(mention("over the socket"), event_id="EvSOCK0001")
    frames = [json.dumps({"type": "hello"}),
              json.dumps({"envelope_id": "env-1", "type": "events_api", "payload": payload}),
              json.dumps({"envelope_id": "env-2", "type": "events_api", "payload": payload,
                          "retry_attempt": 1, "retry_reason": "timeout"})]
    sock = _Socket(frames)
    monkeypatch.setattr(socket_mode, "CONNECT", lambda url: sock)
    consumer = socket_mode.Consumer(engine, _settings(), cid, holder="holder-a")
    assert consumer.run_once() == "reconnect"
    assert sock.sent == [{"envelope_id": "env-1"}, {"envelope_id": "env-2"}] and sock.closed
    opened = [c for c in fake.calls if c["name"] == "apps.connections.open"]
    assert opened and opened[0]["auth"] == "Bearer xapp-test-token"
    with engine.begin() as c:
        rows = c.execute(text("SELECT verify_scheme, duplicates, retry_count_seen FROM "
                              "intake_events WHERE connection_id = :c"), {"c": cid}).all()
    assert len(rows) == 1 and rows[0][0] == "slack_socket" and rows[0][1] == 1 \
        and rows[0][2] == 1
    socket_mode.release_lease(engine, cid, "holder-a")
    assert socket_mode.take_lease(engine, cid, "holder-b"), "released: another process takes it"


# ═══ review fixes ═════════════════════════════════════════════════════════════════════════════

def test_an_injected_reply_pings_nobody(world):
    engine, pid, fake = world
    _slack, run = _admitted_run(engine, pid, fake)
    injected = ("<!channel> urgent! <!here|here> <!everyone> ping <!subteam^S0ONCALL|@oncall> "
                "and <@U0CEO|ceo> and <@W0ADMIN> and plain @here; see <https://ok.example|docs>")
    with engine.begin() as c:
        chat_reply.say(c, run["id"], injected, final=True, kind="agent_reply",
                       dedupe=f"chat:{run['id']}:inject")
    _flush(engine)
    answer = fake.named("chat.update")[-1]["text"]
    for token in ("<!channel>", "<!here", "<!everyone>", "<!subteam", "<@U0CEO", "<@W0ADMIN"):
        assert token not in answer, (token, answer)
    assert "<!" not in answer and "<@" not in answer
    assert "@​channel" in answer and "@​oncall" in answer and "@​ceo" in answer
    assert "@​here" in answer and "<https://ok.example|docs>" in answer, "links stay links"
    assert chat_reply.inert("<! channel>") == "@\u200bchannel"
    assert chat_reply.inert("<!date^1|x>").startswith("<\u200b!date")


def test_socket_mode_routes_interactivity_to_the_chat_handler(world, monkeypatch):
    from boss_engine.connectors.intake.slack import socket_mode

    engine, pid, fake = world
    slack = make_slack(engine, pid, mode="socket", secrets={"app_token": "xapp-test-token"})
    with engine.begin() as c:
        row = dict(c.execute(text("SELECT * FROM intake_connections WHERE id = :c"),
                             {"c": slack["connection"]["id"]}).mappings().one())
    press = {"type": "block_actions", "user": {"id": "U0PRESSER", "username": "presser"},
             "container": {"type": "message", "message_ts": "1700000000.000100",
                           "channel_id": "C0SUPPORT"},
             "channel": {"id": "C0SUPPORT"}, "trigger_id": "t-1",
             "actions": [{"action_id": "boss_decide_approve", "value": "cha_forged.sig",
                          "type": "button"}]}
    ack = socket_mode.handle_envelope(engine, _settings(), row, {
        "envelope_id": "env-int-1", "type": "interactive", "payload": press})
    assert ack == {"envelope_id": "env-int-1"}
    answers = [r for r in _outbox(engine, pid) if (r["payload"] or {}).get("op") == "ephemeral"]
    assert answers, "the press reached the decision handler and was answered in words"
    with engine.begin() as c:
        stored = c.execute(text("SELECT count(*) FROM intake_events WHERE connection_id = :c"),
                           {"c": slack["connection"]["id"]}).scalar()
    assert stored == 0, "a press is not an intake event"


def test_every_channel_row_spends_the_bucket_and_a_429_blocks_the_channel(world):
    engine, pid, fake = world
    slack = make_slack(engine, pid)
    from boss_engine.services.notify import enqueue_chat

    with engine.begin() as c:
        for n in range(5):
            enqueue_chat(c, endpoint_id=slack["endpoint"]["id"], project_id=pid,
                         kind="message", payload={"op": "post", "channel": "C0CARDS",
                                                  "message": {"text": f"card {n}"}},
                         dedupe_key=f"card:{pid}:{n}")
    got = _deliver(engine)
    assert got["sent"] == 3 and got["waiting"] == 2, got
    chat_reply.BUCKETS.reset()
    fake.fail_next("chat.postMessage", ("429", "30"))
    got = _deliver(engine, at=5)
    assert got["rate_limited"] == 1 and got["sent"] == 0 and got["waiting"] == 1, \
        "the 429 blocks the channel for the row behind it too"
    # A row the channel keeps refusing stops waiting after MAX_RATE_LIMITED and backs off.
    with engine.begin() as c:
        c.execute(text("UPDATE chat_outbox SET rate_limited = :n WHERE project_id = :p "
                       "AND status = 'pending'"), {"n": chat_reply.MAX_RATE_LIMITED, "p": pid})
    chat_reply.BUCKETS.reset()
    fake.fail_next("chat.postMessage", ("429", "1"))
    got = _deliver(engine, at=60)
    assert got["failed"] == 1, got


def test_the_list_reply_names_only_usable_agents_or_says_how_to_get_access(world, monkeypatch):
    from boss_engine.services import intake_identity

    engine, pid, fake = world
    slack = make_slack(engine, pid)
    _person(engine, slack)
    for key in ("support", "billing"):
        _route(engine, pid, deploy_local_agent(engine, pid, key=key), slack, name=key,
               identity_mode="person")
    monkeypatch.setattr(intake_identity, "person_acting",
                        lambda c, p, *, project_id, agent_key: (
                            (object(), "ok") if agent_key == "support" else (None, "no_grant")))
    _receive(engine, slack, mention("anyone?", channel="C0ONE"))
    _process(engine)
    monkeypatch.setattr(intake_identity, "person_acting",
                        lambda c, p, *, project_id, agent_key: (None, "no_grant"))
    _receive(engine, slack, mention("anyone at all?", channel="C0TWO"))
    _process(engine)
    _flush(engine)
    texts = {c["channel"]: c["text"] for c in fake.named("chat.postEphemeral")}
    assert "*support*" in texts["C0ONE"] and "billing" not in texts["C0ONE"]
    assert texts["C0TWO"].startswith("No agent here can act for you") \
        and "support" not in texts["C0TWO"]
    assert runs_of(engine, pid) == []


# ═══ people links (E7, P9a-7) ═════════════════════════════════════════════════════════════════

def test_people_lists_linked_unlinked_and_guests_and_an_admin_revoke_sticks(world):
    engine, pid, _fake = world
    slack = make_slack(engine, pid)
    dep = deploy_local_agent(engine, pid)
    _route(engine, pid, dep, slack, name="support")
    _person(engine, slack, "U0PRIYA")
    _receive(engine, slack, mention("hello", user="U0PRIYA"))
    _receive(engine, slack, mention("who am I?", user="U0STRANGER"))
    guest = mention("from the vendor", user="U0VENDOR")
    guest["user_team"] = "T0OTHER"
    _receive(engine, slack, guest)
    while _process(engine)["claimed"]:
        pass
    # A refusal that is not a link prompt (here: a budget stop) must not read as "Link sent".
    _receive(engine, slack, mention("budget?", user="U0QUIET"))
    while _process(engine)["claimed"]:
        pass
    with engine.begin() as c:
        c.execute(text("DELETE FROM chat_outbox WHERE dedupe_key LIKE :k"),
                  {"k": f"link_dm:intake:{slack['endpoint']['id']}:U0QUIET:%"})
    cid = slack["connection"]["id"]
    with engine.begin() as c:
        got = event_intake.connection_people(c, project_id=pid, connection_id=cid, reveal=True)
        hidden = event_intake.connection_people(c, project_id=pid, connection_id=cid)
        page = event_intake.connection_people(c, project_id=pid, connection_id=cid, reveal=True,
                                              limit=2, offset=0)
    by = {p["external_user_id"]: p for p in got["people"]}
    assert by["U0PRIYA"]["state"] == "linked" and by["U0PRIYA"]["method"] == "code"
    assert by["U0PRIYA"]["person"]["principal_id"] and "@" in by["U0PRIYA"]["person"]["email"]
    assert by["U0STRANGER"]["state"] == "not_linked" and by["U0STRANGER"]["prompted_at"]
    assert by["U0QUIET"]["state"] == "not_linked" and by["U0QUIET"]["prompted_at"] is None
    assert by["U0VENDOR"]["state"] == "guest" and by["U0VENDOR"]["guest"] is True
    assert by["U0VENDOR"]["prompted_at"] is None
    assert got["counts"] == {"linked": 1, "not_linked": 2, "guests": 1, "revoked": 0}
    assert "link_code_hash" not in json.dumps(got, default=str)
    # Without user:read: the link and its method, never who it is.
    assert hidden["people_redacted"] is True and got["people_redacted"] is False
    assert all(p["person"] is None for p in hidden["people"])
    assert "intake.test" not in json.dumps(hidden, default=str)
    assert len(page["people"]) == 2 and page["next_offset"] == 2

    link_id = by["U0PRIYA"]["link_id"]
    with engine.begin() as c:
        event_intake.revoke_person_link(c, project_id=pid, connection_id=cid, link_id=link_id,
                                        actor="ceo")
    with engine.begin() as c:
        again = event_intake.connection_people(c, project_id=pid, connection_id=cid, reveal=True)
        with pytest.raises(event_intake.IntakeServiceError):
            event_intake.revoke_person_link(c, project_id=pid, connection_id=cid,
                                            link_id=link_id, actor="ceo")
    after = {p["external_user_id"]: p for p in again["people"]}
    assert after["U0PRIYA"]["state"] == "revoked" and after["U0PRIYA"]["revoked_at"]
    assert again["counts"]["revoked"] == 1 and again["counts"]["linked"] == 0
    # The next message from a revoked user is refused (a new link prompt), never re-linked.
    first = _receive(engine, slack, mention("still me", user="U0PRIYA"))["event_id"]
    while _process(engine)["claimed"]:
        pass
    assert event_row(engine, first)["status"] == "refused"


def test_preview_match_says_the_words_and_who_said_them(world):
    from boss_engine.services import intake_router

    engine, pid, _fake = world
    slack = make_slack(engine, pid)
    _receive(engine, slack, mention("why did the payments job fail?", user="U0PRIYA"))
    _receive(engine, slack, channel_message("lunch at 12:30?", user="U0DEV"))
    while _process(engine)["claimed"]:
        pass
    with engine.begin() as c:
        got = intake_router.preview_match(
            c, project_id=pid, connection_id=slack["connection"]["id"],
            match={"field": "type", "op": "eq", "value": "com.slack.message.app_mention"})
    assert got["ok"] and got["matched"] == 1 and got["considered"] == 2
    hit = next(e for e in got["events"] if e["matched"])
    assert "payments job" in hit["text"]
    assert all("actor" in e for e in got["events"])
