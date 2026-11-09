"""Real PG/API authority and live HTTP; the provider alone is an inert scripted seam."""

from __future__ import annotations

import json
import socket
import time
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import text

from apps.api.app import main
from apps.api.app.services.agent_studio import AgentStudioService
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.test_agent_voice_stream_intent import connect_owned
from boss_engine.identity import authz
from boss_engine.services import agent_converse
from boss_engine.services.model_routing import ModelChoice


class Scripted:
    _boss_choice = ModelChoice("ollama", "owned inert conversation", "approved_local_model_host")

    def __init__(self, engine, project, principal):
        self.engine, self.project, self.principal = engine, project, principal
        self.calls = 0
        self.block = False
        self.fail = False
        self.entered, self.release = Event(), Event()
        self.usage = {
            "model": "owned-inert",
            "cost_usd_micros": 1234,
            "prompt_tokens": 10,
            "completion_tokens": 20,
        }
        self.observations = []
        self.reply = "Owned live token."

    def stream_text_response(self, prompt, system_prompt=None):
        self.calls += 1
        with self.engine.connect() as c:
            row = (
                c.execute(
                    text(
                        "SELECT j.status AS job,j.on_behalf_of,j.lease_owner,l.status AS lease,"
                        "i.status AS invocation,r.status AS receipt,b.status AS hold,b.amount_usd_micros AS amount "
                        "FROM model_gateway_invocations i JOIN jobs j ON j.id=i.job_id "
                        "JOIN model_gateway_leases l ON l.id=i.model_gateway_lease_id "
                        "JOIN receipts r ON r.id=i.receipt_id "
                        "JOIN budget_reservations b ON b.id=l.budget_reservation_id "
                        "WHERE i.project_id=:p AND i.status='in_progress' ORDER BY i.started_at DESC LIMIT 1"
                    ),
                    {"p": self.project},
                )
                .mappings()
                .one()
            )
            self.observations.append(dict(row))
            assert dict(row) == {
                "job": "running",
                "on_behalf_of": self.principal,
                "lease_owner": row["lease_owner"],
                "lease": "active",
                "invocation": "in_progress",
                "receipt": "in_progress",
                "hold": "active",
                "amount": row["amount"],
            }
            assert row["amount"] > 0
            assert row["lease_owner"].startswith("conversation:")
        self.entered.set()
        yield self.reply
        if self.block:
            assert self.release.wait(15), "test did not release its owned provider barrier"
        if self.fail:
            raise RuntimeError("owned accepted request outcome unknown")
        yield self.usage


@pytest.fixture
def owned(client, engine, monkeypatch):
    world = IdentityWorld.create(engine)
    owner = world.person("conversation-owner")
    binding = world.bind(owner, "owner", project=world.project_a)
    connect_owned(engine, monkeypatch, world.project_a, paid=True)
    with engine.begin() as c:
        AgentStudioService(main.settings).seed_catalog(c, world.project_a, owner.id)
        c.execute(
            text("UPDATE roles SET lifecycle='active' WHERE project_id=:p"), {"p": world.project_a}
        )
    model = Scripted(engine, world.project_a, owner.id)
    monkeypatch.setattr(agent_converse, "make_adapter", lambda *_a, **_k: model)
    monkeypatch.setattr(agent_converse, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(agent_converse, "REVEAL_SECONDS", 0)
    with world.acting_as(world.ctx(owner)):
        made = client.post(
            "/v1/conversations",
            json={
                "project_id": world.project_a,
                "kind": "talk",
                "seats": ["qa"],
                "actor": "not-the-authority",
            },
        )
        assert made.status_code == 201, made.text
        cid = made.json()["conversation_id"]
        value = SimpleNamespace(
            world=world,
            owner=owner,
            binding=binding,
            model=model,
            project=world.project_a,
            cid=cid,
            path=f"/v1/conversations/{cid}/turns",
        )
        try:
            yield value
        finally:
            model.release.set()


def post(client, owned, key=None, **changes):
    return client.post(
        owned.path,
        json={
            "project_id": owned.project,
            "message": "A bounded owned question.",
            "actor": "not-the-authority",
            **changes,
        },
        headers={"Idempotency-Key": key} if key else {},
    )


def events(response):
    assert response.status_code == 200, response.text
    return [
        json.loads(block.split("data: ", 1)[1]) | {"type": block.splitlines()[0][7:]}
        for block in response.text.split("\n\n")
        if block.startswith("event: ")
    ]


def test_completed_stream_replays_exactly_and_has_one_canonical_call(client, engine, owned):
    key = str(uuid4())
    first = post(client, owned, key)
    with engine.connect() as c:
        diagnostics = (
            c.execute(
                text("SELECT cost_summary FROM decisions WHERE project_id=:p"), {"p": owned.project}
            )
            .scalars()
            .all()
        )
    assert not diagnostics, diagnostics
    assert next(e for e in events(first) if e["type"] == "turn_end")["status"] == "complete", (
        first.text,
        owned.model.observations,
    )
    second = post(client, owned, key)
    assert second.content == first.content and second.headers["Idempotency-Replayed"] == "true"
    assert owned.model.calls == 1 and len(owned.model.observations) == 1
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM jobs WHERE project_id=:p AND kind='agent.conversation'"),
                {"p": owned.project},
            ).scalar_one()
            == 1
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"), {"p": owned.project}
            ).scalar_one()
            == 1
        )
        assert c.execute(
            text("SELECT bool_and(durable_effect) FROM api_idempotency_keys WHERE principal=:p"),
            {"p": owned.owner.id},
        ).scalar_one()
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM agent_conversation_turns WHERE conversation_id=:c AND speaker_kind='person'"
                ),
                {"c": owned.cid},
            ).scalar_one()
            == 1
        )


def test_changed_request_refuses_and_revoked_permission_cannot_replay(client, engine, owned):
    key = str(uuid4())
    assert post(client, owned, key).status_code == 200
    assert post(client, owned, key, message="Different intent").status_code == 422
    with engine.begin() as c:
        c.execute(text("DELETE FROM identity_role_bindings WHERE id=:b"), {"b": owned.binding})
        authz.bump_epoch(owned.world.org, db=c, reason="owned.conversation.revoke")
    assert post(client, owned, key).status_code == 403
    assert owned.model.calls == 1


def test_accepted_unknown_stream_retains_hold_receipt_and_durable_replay(client, engine, owned):
    owned.model.fail = True
    key = str(uuid4())
    first = post(client, owned, key)
    assert any(e["type"] == "error" and e["error"].get("retry") is False for e in events(first))
    with engine.begin() as c:
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE project_id=:p"),
                {"p": owned.project},
            ).scalar_one()
            == "active"
        )
        assert (
            c.execute(
                text("SELECT status FROM model_gateway_invocations WHERE project_id=:p"),
                {"p": owned.project},
            ).scalar_one()
            == "in_progress"
        )
        c.execute(
            text(
                "UPDATE api_idempotency_keys SET created_at=now()-interval '11 minutes',expires_at=now()-interval '1 day' WHERE idempotency_key=:k"
            ),
            {"k": key},
        )
    second = post(client, owned, key)
    assert second.content == first.content and owned.model.calls == 1


@pytest.fixture
def server(owned):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_level="critical"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        owned.model.release.set()
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


def test_actual_http_tokens_arrive_before_completion_and_parallel_retry_is_held(
    engine, owned, server
):
    owned.model.block = True
    key = str(uuid4())
    with httpx.Client(base_url=server, timeout=10) as http:
        with http.stream(
            "POST",
            owned.path,
            json={"project_id": owned.project, "message": "Live barrier"},
            headers={"Idempotency-Key": key},
        ) as first:
            assert first.status_code == 200
            seen = []
            lines = first.iter_lines()
            for line in lines:
                seen.append(line)
                if "Owned live token." in line:
                    break
            assert owned.model.entered.is_set() and not owned.model.release.is_set(), seen
            reloaded = http.get(
                f"/v1/conversations/{owned.cid}", params={"project_id": owned.project}
            )
            assert reloaded.status_code == 200, reloaded.text
            active = next(t for t in reloaded.json()["turns"] if t["speaker_kind"] == "agent")
            assert (
                active["status"] == "streaming" and active["payload"]["spend_status"] == "admitted"
            )
            assert active["cost_usd"] is None
            duplicate = http.post(
                owned.path,
                json={"project_id": owned.project, "message": "Live barrier"},
                headers={"Idempotency-Key": key},
            )
            assert duplicate.status_code == 409 and owned.model.calls == 1
            owned.model.release.set()
            list(lines)


@pytest.mark.parametrize("expired", ["worker", "model"])
def test_precontact_expired_authority_refuses_and_releases_known_unused(
    client, engine, owned, monkeypatch, expired
):
    from boss_engine.services.conversation_spend import ConversationCallSpend

    original = ConversationCallSpend._ensure_lease

    def expire(spend, authority):
        with engine.begin() as c:
            table, field = (
                ("jobs", "lease_expires_at")
                if expired == "worker"
                else ("model_gateway_leases", "expires_at")
            )
            c.execute(
                text(f"UPDATE {table} SET {field}=now()-interval '1 second' WHERE project_id=:p"),
                {"p": owned.project},
            )
        original(spend, authority)

    monkeypatch.setattr(ConversationCallSpend, "_ensure_lease", expire)
    response = post(client, owned, str(uuid4()))
    assert any(e["type"] == "error" for e in events(response))
    assert owned.model.calls == 0
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE project_id=:p"),
                {"p": owned.project},
            ).scalar_one()
            == "released"
        )
        assert (
            c.execute(
                text(
                    "SELECT status FROM receipts WHERE project_id=:p AND effect_kind='model.invoke'"
                ),
                {"p": owned.project},
            ).scalar_one()
            == "failed"
        )


@pytest.mark.parametrize("changed", ["worker", "model_token"])
def test_precontact_changed_owner_cannot_free_another_authority(
    client, engine, owned, monkeypatch, changed
):
    from boss_engine.services.conversation_spend import ConversationCallSpend

    original = ConversationCallSpend._ensure_lease

    def reclaim(spend, authority):
        with engine.begin() as c:
            c.execute(
                text(
                    "UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE project_id=:p"
                ),
                {"p": owned.project},
            )
            if changed == "worker":
                c.execute(
                    text("UPDATE jobs SET lease_owner='another-owned-worker' WHERE project_id=:p"),
                    {"p": owned.project},
                )
            else:
                c.execute(
                    text(
                        "UPDATE model_gateway_leases SET lease_token_hash='changed' WHERE project_id=:p"
                    ),
                    {"p": owned.project},
                )
        original(spend, authority)

    monkeypatch.setattr(ConversationCallSpend, "_ensure_lease", reclaim)
    response = post(client, owned, str(uuid4()))
    assert any(e["type"] == "error" and e["error"]["retry"] is False for e in events(response))
    assert owned.model.calls == 0
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE project_id=:p"),
                {"p": owned.project},
            ).scalar_one()
            == "active"
        )


@pytest.mark.parametrize(
    "usage", [{}, {"cost_usd_micros": True}, {"cost_usd_micros": "0"}, {"cost_usd_micros": -1}]
)
def test_unknown_paid_cost_remains_positive_across_expiry_and_periods(client, engine, owned, usage):
    from boss_engine.services.budget import BudgetGovernor, period_spend

    owned.model.usage = usage
    response = post(client, owned, str(uuid4()))
    assert any(e["type"] == "error" and e["error"]["retry"] is False for e in events(response))
    assert owned.model.calls == 1
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE budget_reservations SET created_at=now()-interval '40 days', expires_at=now()-interval '39 days' WHERE project_id=:p"
            ),
            {"p": owned.project},
        )
        BudgetGovernor(main.settings).expire_stale_reservations(c, owned.project)
        for period in ("day", "month"):
            assert (
                period_spend(c, owned.project, period)["reserved_usd_micros"]
                == agent_converse.REPLY_RESERVE_MICROS
            )
        assert (
            c.execute(
                text("SELECT count(*) FROM cost_receipts WHERE project_id=:p"), {"p": owned.project}
            ).scalar_one()
            == 0
        )


def test_output_limit_holds_before_accumulating_overflow(client, engine, owned, monkeypatch):
    from boss_engine.services.conversation_spend import ConversationCallSpend

    monkeypatch.setattr(ConversationCallSpend, "max_reply_bytes", 8)
    response = post(client, owned, str(uuid4()))
    assert not any(e["type"] == "token" for e in events(response))
    assert any(e["type"] == "error" and e["error"]["retry"] is False for e in events(response))
    with engine.connect() as c:
        result = c.execute(
            text("SELECT result FROM receipts WHERE project_id=:p AND effect_kind='model.invoke'"),
            {"p": owned.project},
        ).scalar_one()
        assert result["unsettled_conversation_reply"]["text"] == ""
        assert (
            c.execute(
                text(
                    "SELECT amount_usd_micros FROM budget_reservations WHERE project_id=:p AND status='active'"
                ),
                {"p": owned.project},
            ).scalar_one()
            > 0
        )


def test_close_failure_after_settlement_keeps_http_claim_and_booked_cost(
    client, engine, owned, monkeypatch
):
    original = owned.model.stream_text_response

    class BrokenClose:
        def __init__(self):
            self.iterator = original("owned fixture")

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.iterator)

        def close(self):
            raise RuntimeError("owned producer close failure")

    monkeypatch.setattr(owned.model, "stream_text_response", lambda *_a, **_k: BrokenClose())
    key = str(uuid4())
    first = post(client, owned, key)
    assert any(e["type"] == "turn_end" and e["spend_status"] == "settled" for e in events(first))
    second = post(client, owned, key)
    assert second.status_code == 409 and owned.model.calls == 1
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT actual_usd_micros FROM cost_receipts WHERE project_id=:p"),
                {"p": owned.project},
            ).scalar_one()
            == 1234
        )
        assert (
            c.execute(
                text("SELECT state FROM api_idempotency_keys WHERE principal=:p"),
                {"p": owned.owner.id},
            ).scalar_one()
            == "in_progress"
        )


def test_no_identified_source_never_contacts_and_records_visible_failure(client, engine, owned):
    owned.model._boss_choice = None
    response = post(client, owned, str(uuid4()))
    assert any(e["type"] == "error" for e in events(response))
    assert owned.model.calls == 0
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM jobs WHERE project_id=:p"), {"p": owned.project}
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text(
                    "SELECT status FROM agent_conversation_turns WHERE conversation_id=:c AND speaker_kind='agent'"
                ),
                {"c": owned.cid},
            ).scalar_one()
            == "error"
        )


def test_foreign_owner_and_unsigned_request_cannot_replay(client, engine, owned):
    key = str(uuid4())
    first = post(client, owned, key)
    assert first.status_code == 200
    other = owned.world.person("other conversation owner")
    owned.world.bind(other, "owner", project=owned.project)
    with owned.world.acting_as(owned.world.ctx(other)):
        assert post(client, owned, key).status_code == 404
    with owned.world.acting_as(None):
        assert post(client, owned, key).status_code in (401, 403)
    assert owned.model.calls == 1


@pytest.mark.parametrize("part", ["summary", "propose"])
def test_helper_returning_summary_and_proposal_have_durable_exact_replay(
    client, engine, owned, part
):
    made = client.post(
        "/v1/conversations",
        json={
            "project_id": owned.project,
            "kind": "room" if part == "summary" else "plan",
            "seats": ["qa", "pm"] if part == "summary" else ["qa"],
        },
    )
    assert made.status_code == 201, made.text
    cid = made.json()["conversation_id"]
    # An already persisted operator request is the structured action's input.
    with engine.begin() as c:
        agent_converse._insert_turn(
            c,
            cid,
            speaker_kind="person",
            actor=owned.owner.id,
            content="A bounded owned plan",
            status="complete",
        )
    if part == "summary":
        # Summary requires a persisted completed agent contribution, not only
        # the operator request. This is inert transcript input, not a model call.
        with engine.begin() as c:
            agent_converse._insert_turn(
                c,
                cid,
                speaker_kind="agent",
                role_key="qa",
                actor=owned.owner.id,
                content="Owned prior contribution",
                status="complete",
            )
        owned.model.reply = json.dumps(
            {"text": "Owned summary", "decisions": [], "next_actions": []}
        )
    else:
        # Publish checks real holdings even though this test only proposes and
        # never starts work. Give the owned seat a genuine bounded capability.
        skill = client.post(
            "/v1/skill-definitions",
            json={
                "project_id": owned.project,
                "skill_key": "owned_plan_text",
                "label": "Owned plan text",
                "behaviour_kind": "builtin",
                "config": {"executor_id": "model.generate_text"},
                "trusted": True,
            },
        )
        assert skill.status_code == 201, skill.text
        grant = client.post(
            "/v1/role-skills",
            json={
                "project_id": owned.project,
                "role_key": "qa",
                "skill_id": "owned_plan_text",
            },
        )
        assert grant.status_code == 201, grant.text
        created = client.post(
            "/v1/delivery-workflows/create",
            json={"project_id": owned.project, "name": "Owned proposal"},
        )
        assert created.status_code == 200, created.text
        definition = created.json()["definition"]["id"]
        saved = client.post(
            "/v1/delivery-workflows/save",
            json={
                "project_id": owned.project,
                "definition_id": definition,
                "spec": {
                    "entry_step": "work",
                    "steps": [
                        {
                            "id": "work",
                            "title": "Owned work",
                            "kind": "capability",
                            "captain": "qa",
                            "next": [],
                            "binding": {
                                "agent_work": True,
                                "inputs": {"instructions": "Owned fixture"},
                            },
                        }
                    ],
                },
            },
        )
        assert saved.status_code == 200, saved.text
        with engine.connect() as c:
            catalog = agent_converse._catalogue(c, main.settings, owned.project)
        chosen = next(w for w in catalog if w["name"] == "Owned proposal")
        # The start card is the pipeline's decision, in its contract: the workflow is the
        # chosen action, the card's draft its arguments.
        owned.model.reply = json.dumps(
            {
                "action": chosen["workflow_key"],
                "confidence": 0.9,
                "reason": "Owner requested it",
                "arguments": {
                    "title": "Owned plan",
                    "directive": "Inspect the owned fixture",
                    "inputs": {},
                    "acceptance_criteria": ["Owned output exists"],
                    "why": "Owner requested it",
                },
            }
        )
    path = f"/v1/conversations/{cid}/{part}"
    key = str(uuid4())
    first = client.post(path, json={"project_id": owned.project}, headers={"Idempotency-Key": key})
    assert next(e for e in events(first) if e["type"] == "turn_end")["status"] == "complete", (
        first.text
    )
    second = client.post(path, json={"project_id": owned.project}, headers={"Idempotency-Key": key})
    assert second.content == first.content and second.headers["Idempotency-Replayed"] == "true"
    assert owned.model.calls == 1
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM workflow_runs WHERE project_id=:p"), {"p": owned.project}
            ).scalar_one()
            == 0
        )


def test_revocation_after_first_live_token_holds_and_stops_later_room_seats(client, engine, owned):
    from boss_engine.identity import lineage
    from boss_engine.services.conversation_spend import ConversationAuthority

    made = client.post(
        "/v1/conversations",
        json={"project_id": owned.project, "kind": "room", "seats": ["qa", "pm"]},
    )
    cid = made.json()["conversation_id"]
    allowed = True

    def check():
        if not allowed:
            raise RuntimeError("Owned permission revoked")

    authority = ConversationAuthority(
        owned.owner.id, lineage.Acting(owned.owner.id, owned.world.org), check
    )
    stream = agent_converse.take_turn(
        engine,
        project_id=owned.project,
        conversation_id=cid,
        actor=owned.owner.id,
        message="Owned live room",
        settings=main.settings,
        authority=authority,
    )
    seen = []
    for event in stream:
        seen.append(event)
        if event["type"] == "token":
            allowed = False
            break
    seen.extend(stream)
    assert len([e for e in seen if e["type"] == "turn_start"]) == 1
    assert owned.model.calls == 1
    assert any(e["type"] == "error" and e["error"]["retry"] is False for e in seen)
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE project_id=:p"),
                {"p": owned.project},
            ).scalar_one()
            == "active"
        )


def test_heartbeat_continues_while_live_provider_is_blocked(client, engine, owned, monkeypatch):
    from boss_engine.services.conversation_spend import ConversationCallSpend

    owned.model.block = True
    renewed = Event()
    original_runner = ConversationCallSpend.for_runner.__func__
    original_heartbeat = ConversationCallSpend._heartbeat

    def runner(cls, *args, **kwargs):
        spend = original_runner(cls, *args, **kwargs)
        spend.ttl_seconds = 5
        return spend

    def heartbeat(spend, c, *args):
        result = original_heartbeat(spend, c, *args)
        if owned.model.entered.is_set():
            renewed.set()
            owned.model.release.set()
        return result

    monkeypatch.setattr(ConversationCallSpend, "for_runner", classmethod(runner))
    monkeypatch.setattr(ConversationCallSpend, "_heartbeat", heartbeat)
    response = post(client, owned, str(uuid4()))
    assert renewed.is_set()
    assert next(e for e in events(response) if e["type"] == "turn_end")["status"] == "complete", (
        response.text
    )
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM jobs WHERE project_id=:p"), {"p": owned.project}
            ).scalar_one()
            == "succeeded"
        )


def test_deadline_after_contact_holds_without_retry(client, engine, owned, monkeypatch):
    from boss_engine.services import conversation_spend

    clock = [100.0]
    monkeypatch.setattr(conversation_spend, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(main.settings, "chat_turn_seconds", 5)
    original = owned.model.stream_text_response

    def timed(*args, **kwargs):
        for piece in original(*args, **kwargs):
            yield piece
            clock[0] = 106.0

    monkeypatch.setattr(owned.model, "stream_text_response", timed)
    response = post(client, owned, str(uuid4()))
    assert any(e["type"] == "error" and e["error"]["retry"] is False for e in events(response))
    assert owned.model.calls == 1
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE project_id=:p"),
                {"p": owned.project},
            ).scalar_one()
            == "active"
        )


def test_dead_legacy_stream_without_invocation_keeps_its_positive_hold(engine, owned):
    from boss_engine.services.budget import BudgetGovernor

    with engine.begin() as c:
        reserved = BudgetGovernor(main.settings).reserve(
            c, owned.project, "agent_conversation", 5000, "Owned legacy unknown turn", 1
        )
        assert reserved["status"] == "reserved"
        turn = agent_converse._insert_turn(
            c,
            owned.cid,
            speaker_kind="agent",
            role_key="qa",
            status="streaming",
            payload={"reservation_id": reserved["reservation_id"]},
        )
        agent_converse._sweep(c, main.settings, owned.project, owned.cid)
        agent_converse._sweep(c, main.settings, owned.project, owned.cid)
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE id=:r"),
                {"r": reserved["reservation_id"]},
            ).scalar_one()
            == "active"
        )
        saved = c.execute(
            text("SELECT payload FROM agent_conversation_turns WHERE turn_id=:t"),
            {"t": turn["turn_id"]},
        ).scalar_one()
        assert saved["spend_status"] == "needs_reconciliation" and not saved["retry"]
        assert (
            c.execute(
                text("SELECT count(*) FROM decisions WHERE project_id=:p"), {"p": owned.project}
            ).scalar_one()
            == 1
        )
    assert owned.model.calls == 0


def test_router_failure_never_reaches_fallback_on_original_receipt(
    client, engine, owned, monkeypatch
):
    from boss_engine.services.model_router.failover import FailoverChatAdapter

    owned.model.fail = True
    reroutes = []

    def reroute(*args):
        reroutes.append(1)
        return owned.model, None

    wrapper = FailoverChatAdapter(
        owned.model,
        SimpleNamespace(engine="ollama", model="owned", id="owned", mode="smart"),
        reroute,
        stage="converse",
        record=False,
    )
    wrapper._boss_choice = owned.model._boss_choice
    monkeypatch.setattr(agent_converse, "make_adapter", lambda *_a, **_k: wrapper)
    response = post(client, owned, str(uuid4()))
    assert any(e["type"] == "error" for e in events(response))
    assert owned.model.calls == 1 and not reroutes


def test_intent_protection_and_floor_rollback_together_before_contact(
    client, engine, owned, monkeypatch
):
    from apps.api.app import idempotency

    original = idempotency.protect

    def reject_after_protect(c, **kwargs):
        original(c, **kwargs)
        raise agent_converse.ConverseError(409, "owned_rollback", "Owned admission rolled back.")

    monkeypatch.setattr(idempotency, "protect", reject_after_protect)
    key = str(uuid4())
    refused = post(client, owned, key)
    assert refused.status_code == 409 and owned.model.calls == 0
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT streaming_claim FROM agent_conversations WHERE conversation_id=:c"),
                {"c": owned.cid},
            ).scalar_one()
            is None
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM agent_conversation_turns WHERE conversation_id=:c"),
                {"c": owned.cid},
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM jobs WHERE project_id=:p"), {"p": owned.project}
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM api_idempotency_keys WHERE principal=:p"),
                {"p": owned.owner.id},
            ).scalar_one()
            == 0
        )
    monkeypatch.setattr(idempotency, "protect", original)
    accepted = post(client, owned, key)
    assert next(e for e in events(accepted) if e["type"] == "turn_end")["status"] == "complete"
    assert owned.model.calls == 1


def _intent(response):
    found = [event for event in events(response) if event["type"] == "intent"]
    assert found, [event["type"] for event in events(response)]
    return found[0]


def test_failed_turn_and_its_replay_carry_the_original_message_and_action(client, engine, owned):
    """The break: a failed turns stream, and the replay of that same request, omit the
    caller's message, the turns action, and the Idempotency-Key that named the request."""
    message = "BOSSAPP-496 keeps this ledger sentence."
    key = "496-turn-intent"
    owned.model.fail = True
    first = post(client, owned, key, message=message)
    assert first.status_code == 200, first.text
    assert _intent(first) == {
        "type": "intent",
        "action": "turns",
        "message": message,
        "idempotency_key": key,
    }
    second = post(client, owned, key, message=message)
    assert second.status_code == 200, second.text
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.content == first.content
    assert _intent(second)["message"] == message and _intent(second)["action"] == "turns"
    assert owned.model.calls == 1
    with engine.connect() as c:
        stored = c.execute(
            text(
                "SELECT state, path, response_body FROM api_idempotency_keys "
                "WHERE idempotency_key=:k"
            ),
            {"k": key},
        ).one()
        assert stored.state == "completed"
        assert stored.path.endswith("/turns")
        assert message.encode() in bytes(stored.response_body or b"")
        assert (
            c.execute(
                text(
                    "SELECT content FROM agent_conversation_turns "
                    "WHERE conversation_id=:c AND speaker_kind='person'"
                ),
                {"c": owned.cid},
            ).scalar_one()
            == message
        )


def test_failed_summary_and_propose_and_their_replays_carry_the_requested_action(
    client, engine, owned
):
    """Summary and propose have no message field. A failed stream and its replay still name
    the action the caller requested, and the key that named that request."""
    made = client.post(
        "/v1/conversations",
        json={"project_id": owned.project, "kind": "room", "seats": ["qa", "pm"]},
    )
    assert made.status_code == 201, made.text
    cid = made.json()["conversation_id"]
    with engine.begin() as c:
        agent_converse._insert_turn(
            c, cid, speaker_kind="person", actor=owned.owner.id,
            content="A bounded owned plan", status="complete",
        )
        agent_converse._insert_turn(
            c, cid, speaker_kind="agent", role_key="qa", actor=owned.owner.id,
            content="Owned prior contribution", status="complete",
        )
    owned.model.fail = True
    summary_key = "496-summary-intent"
    summary_path = f"/v1/conversations/{cid}/summary"
    body = {"project_id": owned.project}
    first = client.post(summary_path, json=body, headers={"Idempotency-Key": summary_key})
    assert first.status_code == 200, first.text
    assert _intent(first) == {
        "type": "intent",
        "action": "summary",
        "message": "",
        "idempotency_key": summary_key,
    }
    second = client.post(summary_path, json=body, headers={"Idempotency-Key": summary_key})
    assert second.content == first.content and second.headers["Idempotency-Replayed"] == "true"
    plan = client.post(
        "/v1/conversations",
        json={"project_id": owned.project, "kind": "plan", "seats": ["qa"]},
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["conversation_id"]
    with engine.begin() as c:
        agent_converse._insert_turn(
            c, plan_id, speaker_kind="person", actor=owned.owner.id,
            content="A bounded owned plan", status="complete",
        )
    propose_key = "496-propose-intent"
    propose_path = f"/v1/conversations/{plan_id}/propose"
    proposed = client.post(propose_path, json=body, headers={"Idempotency-Key": propose_key})
    assert proposed.status_code == 200, proposed.text
    assert _intent(proposed) == {
        "type": "intent",
        "action": "propose",
        "message": "",
        "idempotency_key": propose_key,
    }
    again = client.post(propose_path, json=body, headers={"Idempotency-Key": propose_key})
    assert again.content == proposed.content and again.headers["Idempotency-Replayed"] == "true"
    assert owned.model.calls == 1
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT idempotency_key, state, path FROM api_idempotency_keys "
                "WHERE idempotency_key IN ('496-summary-intent', '496-propose-intent') "
                "ORDER BY idempotency_key"
            )
        ).mappings().all()
    assert [(row["idempotency_key"], row["state"], row["path"].rsplit("/", 1)[-1]) for row in rows] == [
        ("496-propose-intent", "completed", "propose"),
        ("496-summary-intent", "completed", "summary"),
    ]


def test_modern_unsettled_cost_is_null_and_historical_or_settled_cost_is_preserved():
    for status in ("not_admitted", "admitted", "needs_reconciliation"):
        assert (
            agent_converse._turn_dict({"payload": {"spend_status": status}, "cost_usd": 0})[
                "cost_usd"
            ]
            is None
        )
    for payload in ({}, {"spend_status": "settled"}):
        for amount in (0, 0.003):
            assert (
                agent_converse._turn_dict({"payload": payload, "cost_usd": amount})["cost_usd"]
                == amount
            )
