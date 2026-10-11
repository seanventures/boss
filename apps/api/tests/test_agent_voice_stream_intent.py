"""BOSS-493: actual scoped API/PG plus owned loopback SSE; provider seam is inert.

The first header does not finish the intent. Unknown provider outcomes do not buy
free retries. Tests observe committed job, budget, lease and receipt before contact.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import socket
from threading import Event, Thread
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
import uvicorn

from apps.api.app import main, idempotency, stream_intent
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity import authz
from boss_engine.services import agent_voice, model_routing
from boss_engine.services.model_routing import ModelChoice
from boss_engine.services.entitlements import EntitlementBrokerService
from boss_engine.services.agent_studio import AgentStudioService

PROJECT = "boss-dogfood"
BODY = {"project_id": PROJECT, "actor": "ceo", "probe": "intro"}
PATH = "/v1/agents/qa/test"


def connect_owned(engine, monkeypatch, project=PROJECT, paid=False):
    monkeypatch.setattr(main.settings, "local_model_enabled", True)
    monkeypatch.setattr(main.settings, "local_model_id", "owned-inert-voice")
    with engine.begin() as c:
        EntitlementBrokerService(main.settings).seed_default_sources(c, project)
        c.execute(
            text(
                "UPDATE entitlement_sources SET status='active',connection_status='connected', "
                "runtime_supported=TRUE WHERE project_id=:p AND source_id='approved_local_model_host'"
            ),
            {"p": project},
        )
        if paid:
            c.execute(
                text(
                    "UPDATE entitlement_sources SET entitlement_kind='usage_based_api', "
                    "cost_mode='usage_based_api' WHERE project_id=:p AND source_id='approved_local_model_host'"
                ),
                {"p": project},
            )
    monkeypatch.setattr(agent_voice, "model_ready", lambda *_a: True)


class Scripted:
    _boss_choice = ModelChoice("ollama", "owned inert fixture", "approved_local_model_host")

    def __init__(self, engine, project=PROJECT):
        self.engine = engine
        self.project = project
        self.calls = 0
        self.entered = Event()
        self.release = Event()
        self.block = False
        self.fail_at = 0
        self.usage = {
            "model": "owned-inert",
            "cost_usd_micros": 1234,
            "prompt_tokens": 10,
            "completion_tokens": 20,
        }

    def create_text_response(self, prompt, system_prompt=None):
        self.calls += 1
        # A separate connection sees the authority committed before this seam.
        with self.engine.connect() as c:
            rows = (
                c.execute(
                    text(
                        "SELECT j.status AS job_status,j.on_behalf_of,j.lease_owner, "
                        "l.status AS lease_status,i.status AS invocation_status,r.status AS receipt_status, "
                        "b.status AS hold FROM model_gateway_invocations i "
                        "JOIN jobs j ON j.id=i.job_id JOIN model_gateway_leases l ON l.id=i.model_gateway_lease_id "
                        "JOIN receipts r ON r.id=i.receipt_id JOIN budget_reservations b ON b.id=l.budget_reservation_id "
                        "WHERE i.project_id=:p AND i.status='in_progress'"
                    ),
                    {"p": self.project},
                )
                .mappings()
                .all()
            )
            assert rows and all(
                r["job_status"] == "running"
                and r["lease_status"] == "active"
                and r["receipt_status"] == "in_progress"
                and r["hold"] == "active"
                for r in rows
            )
            assert all(r["on_behalf_of"] and r["lease_owner"].startswith("voice:") for r in rows)
        self.entered.set()
        if self.block and self.calls == 1:
            assert self.release.wait(50), "owned provider seam was not released"
        if self.calls == self.fail_at:
            raise RuntimeError("owned accepted request outcome unknown")
        reply = "I'm Vera, an AI agent. I check the evidence."
        if "grade replies" in (system_prompt or ""):
            reply = '{"voice":4,"in_remit":true,"says_ai":true}'
        return "owned-response", reply, self.usage


@pytest.fixture
def model(engine, monkeypatch):
    connect_owned(engine, monkeypatch)
    model = Scripted(engine)
    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda *_a, **_k: model)
    yield model
    model.release.set()


def post(client, key=None, body=None, path=PATH):
    return client.post(path, json=body or BODY, headers={"Idempotency-Key": key} if key else {})


def result(response):
    for block in response.text.split("\n\n"):
        if block.startswith("event: result\n"):
            return json.loads(block.split("data: ", 1)[1])
    raise AssertionError(response.text)


def test_completed_replay_returns_exact_stream_and_no_new_jobs_or_spend(client, engine, model):
    key = str(uuid4())
    first = post(client, key)
    assert first.status_code == 200 and result(first)["spend_status"] == "settled", first.text
    second = post(client, key)
    assert first.content == second.content and second.headers["Idempotency-Replayed"] == "true"
    assert model.calls == 2
    data = result(first)
    assert len(data["receipt_ids"]) == 2
    with engine.connect() as c:
        actual_agent = c.execute(
            text("SELECT agent_id FROM roles WHERE project_id=:p AND role_key='qa'"), {"p": PROJECT}
        ).scalar_one()
        assert set(c.execute(text("SELECT agent_id FROM model_gateway_invocations")).scalars()) == {
            actual_agent
        }
        assert (
            c.execute(text("SELECT count(*) FROM jobs WHERE kind='agent.voice'")).scalar_one() == 2
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM cost_receipts WHERE receipt_id=:id"),
                {"id": data["receipt_id"]},
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text("SELECT status FROM receipts WHERE id=:id"), {"id": data["receipt_id"]}
            ).scalar_one()
            == "succeeded"
        )
        assert c.execute(
            text("SELECT bool_and(durable_effect) FROM api_idempotency_keys")
        ).scalar_one()


@pytest.mark.parametrize("change", ["body", "path", "query", "project"])
def test_original_key_binds_exact_request_material(client, engine, model, change):
    key = str(uuid4())
    assert post(client, key).status_code == 200
    path = PATH
    body = dict(BODY)
    if change == "body":
        body["probe"] = "push_back"
    if change == "path":
        path = "/v1/agents/sre/test"
    if change == "query":
        path += "?filter=changed"
    if change == "project":
        # The ordinary scope guard may refuse this project before fingerprint comparison.
        body["project_id"] = "outside-project"
    response = post(client, key, body, path)
    assert response.status_code in ({403, 404, 422} if change == "project" else {422}), (
        response.text
    )
    assert model.calls == 2


@pytest.mark.parametrize("keyed", [True, False])
def test_new_intents_and_no_key_keep_normal_streams(client, engine, model, keyed):
    a = post(client, str(uuid4()) if keyed else None)
    b = post(client, str(uuid4()) if keyed else None)
    assert a.status_code == b.status_code == 200 and result(a) and result(b)
    assert result(a)["receipt_id"] != result(b)["receipt_id"] and model.calls == 4


@pytest.fixture
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    config = uvicorn.Config(main.app, log_level="critical", lifespan="off")
    server = uvicorn.Server(config)
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.01)
    assert server.started
    yield base
    server.should_exit = True
    thread.join(5)
    sock.close()
    assert not thread.is_alive()


def age_claim(engine, key):
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE api_idempotency_keys SET created_at=now()-interval '11 minutes', "
                "expires_at=now()-interval '1 day' WHERE idempotency_key=:k"
            ),
            {"k": key},
        )


def test_headers_do_not_free_intent_and_ten_minute_takeover_cannot_repeat_call(
    server, engine, model
):
    model.block = True
    key = str(uuid4())
    with httpx.Client(base_url=server, timeout=15) as http:
        with http.stream("POST", PATH, json=BODY, headers={"Idempotency-Key": key}) as original:
            assert original.status_code == 200
            assert model.entered.wait(5)
            duplicate = post(http, key)
            assert duplicate.status_code == 409, (
                duplicate.text,
                dict(duplicate.headers),
                model.calls,
            )
            age_claim(engine, key)
            assert post(http, key).status_code == 409
            assert model.calls == 1
            with engine.connect() as c:
                assert (
                    c.execute(text("SELECT count(*) FROM model_gateway_invocations")).scalar_one()
                    == 1
                )
            model.release.set()
            first_body = original.read()
        replay = post(http, key)
        assert replay.status_code == 200 and replay.content == first_body and model.calls == 2
        age_claim(engine, key)
        assert post(http, key).content == first_body and model.calls == 2


def test_blocked_voice_call_heartbeats_both_canonical_leases(server, engine, model, monkeypatch):
    from boss_engine.services.voice_spend import VoiceCallSpend

    original_factory = VoiceCallSpend.for_runner.__func__

    def short_lease(cls, *args, **kwargs):
        spend = original_factory(cls, *args, **kwargs)
        spend.ttl_seconds = 6
        return spend

    monkeypatch.setattr(VoiceCallSpend, "for_runner", classmethod(short_lease))
    model.block = True
    key = str(uuid4())
    query = text(
        "SELECT j.lease_expires_at AS job_deadline,l.expires_at AS model_deadline, "
        "now() AS observed_at FROM jobs j JOIN model_gateway_leases l ON l.job_id=j.id "
        "WHERE l.status='active'"
    )
    with httpx.Client(base_url=server, timeout=15) as http:
        with http.stream("POST", PATH, json=BODY, headers={"Idempotency-Key": key}) as response:
            assert response.status_code == 200 and model.entered.wait(5)
            with engine.connect() as c:
                initial = c.execute(query).mappings().one()
            time.sleep(6.2)  # beyond the original six-second lease while provider is blocked
            with engine.connect() as c:
                live = c.execute(query).mappings().one()
                assert live["job_deadline"] > initial["job_deadline"]
                assert live["model_deadline"] > initial["model_deadline"]
                assert live["job_deadline"] > live["observed_at"]
                assert live["model_deadline"] > live["observed_at"]
            assert model.calls == 1
            model.release.set()
            response.read()
            assert result(response)["spend_status"] == "settled"
        assert model.calls == 2


def test_simultaneous_duplicates_contact_provider_once_before_judge(server, engine, model):
    model.block = True
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            lambda: httpx.post(
                server + PATH, json=BODY, headers={"Idempotency-Key": key}, timeout=15
            )
        )
        assert model.entered.wait(5)
        second = httpx.post(server + PATH, json=BODY, headers={"Idempotency-Key": key}, timeout=5)
        assert second.status_code == 409 and model.calls == 1
        model.release.set()
        assert first.result(10).status_code == 200 and model.calls == 2


def test_provider_unknown_retains_positive_liability_and_replays_error(
    client, engine, model, monkeypatch
):
    connect_owned(engine, monkeypatch, paid=True)
    model.fail_at = 1
    key = str(uuid4())
    response = post(client, key)
    assert "event: error" in response.text and "needs_reconciliation" in response.text
    assert post(client, key).content == response.content and model.calls == 1
    age_claim(engine, key)
    assert post(client, key).content == response.content and model.calls == 1
    with engine.begin() as c:
        event = (
            c.execute(
                text(
                    "SELECT event_type,entity_type,data FROM events WHERE event_type='agent_voice.reconciliation_required'"
                )
            )
            .mappings()
            .one()
        )
        assert event["entity_type"] == "agent_voice_call" and event["data"]["voice_intent"]
        assert event["data"]["call"] == "1" and "run_id" not in event["data"]
        question = c.execute(
            text(
                "SELECT question FROM decisions WHERE recommendation->>'action'='agent_voice_reconciliation'"
            )
        ).scalar_one()
        assert (
            "agent voice" in question
            and "architecture" not in question
            and "typed decision" not in question
        )
        # Expiring authority is not evidence of no provider acceptance.
        c.execute(text("UPDATE budget_reservations SET expires_at=now()-interval '1 minute'"))
        from boss_engine.services.budget import BudgetGovernor

        BudgetGovernor(main.settings).expire_stale_reservations(c, PROJECT)
        row = (
            c.execute(
                text(
                    "SELECT b.status,b.amount_usd_micros,i.status AS invocation, "
                    "r.status AS receipt FROM model_gateway_invocations i "
                    "JOIN model_gateway_leases l ON l.id=i.model_gateway_lease_id "
                    "JOIN budget_reservations b ON b.id=l.budget_reservation_id "
                    "JOIN receipts r ON r.id=i.receipt_id"
                )
            )
            .mappings()
            .one()
        )
        assert dict(row) == {
            "status": "active",
            "amount_usd_micros": 100000,
            "invocation": "in_progress",
            "receipt": "in_progress",
        }
        assert (
            c.execute(text("SELECT count(*) FROM decisions WHERE status='pending'")).scalar_one()
            > 0
        )


def test_unknown_judge_is_visible_after_paid_reply(client, engine, model, monkeypatch):
    connect_owned(engine, monkeypatch, paid=True)
    model.fail_at = 2
    response = post(client, str(uuid4()))
    data = result(response)
    assert data["spend_status"] == "needs_reconciliation" and len(data["receipt_ids"]) == 2
    assert response.text.rfind("event: error") > response.text.rfind("event: result")
    assert "The judgement outcome is unknown. Reconcile" in response.text
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT sum(actual_usd_micros) FROM cost_receipts")).scalar_one() == 1234
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM model_gateway_invocations WHERE status='in_progress'")
            ).scalar_one()
            == 1
        )
        assert (
            c.execute(
                text("SELECT status FROM receipts WHERE id=:r"), {"r": data["receipt_id"]}
            ).scalar_one()
            == "in_progress"
        )


def test_missing_paid_usage_does_not_certify_zero_cost(client, engine, model, monkeypatch):
    connect_owned(engine, monkeypatch, paid=True)
    model.usage = {}
    response = post(client, str(uuid4()))
    assert "needs_reconciliation" in response.text and model.calls == 1
    with engine.connect() as c:
        recorded = c.execute(
            text("SELECT result FROM receipts WHERE result ? 'unsettled_voice_reply'")
        ).scalar_one()
        raw = recorded["unsettled_voice_reply"]
        assert raw["text"] == "I'm Vera, an AI agent. I check the evidence."
        assert raw["usage"] == {} and raw["request_actor"] == "ceo"
        assert raw["voice_intent"] and raw["call"] == "1"
        assert not {
            "workflow_run_id",
            "workflow_actor",
            "architecture_version",
            "step_id",
        }.intersection(raw)
        assert c.execute(text("SELECT count(*) FROM cost_receipts")).scalar_one() == 0
        assert (
            c.execute(
                text("SELECT count(*) FROM model_gateway_invocations WHERE status='in_progress'")
            ).scalar_one()
            == 1
        )


def test_keyed_draft_unknown_is_not_retried_on_502(client, engine, model, monkeypatch):
    connect_owned(engine, monkeypatch, paid=True)
    model.fail_at = 1
    key = str(uuid4())
    body = {"project_id": PROJECT, "actor": "ceo", "field": "tagline"}
    assert post(client, key, body, "/v1/agents/qa/persona/draft").status_code == 502
    assert post(client, key, body, "/v1/agents/qa/persona/draft").status_code == 409
    assert model.calls == 1


def test_refusal_before_commit_can_retry_same_key(client, engine, model, monkeypatch):
    monkeypatch.setattr(agent_voice, "model_ready", lambda *_a: False)
    key = str(uuid4())
    assert post(client, key).status_code == 409
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM api_idempotency_keys")).scalar_one() == 0
        assert (
            c.execute(text("SELECT count(*) FROM jobs WHERE kind='agent.voice'")).scalar_one() == 0
        )
    monkeypatch.setattr(agent_voice, "model_ready", lambda *_a: True)
    assert result(post(client, key))["spend_status"] == "settled"


def test_live_collector_yields_before_completion_and_keeps_unknown_outcomes_bounded():
    async def run():
        finished = []

        async def finish(body):
            finished.append(body)

        async def parts():
            yield b"first"
            yield b"second"

        collector = stream_intent.record_live(parts(), limit=100, finish=finish)
        assert await collector.__anext__() == b"first" and finished == []
        assert await collector.__anext__() == b"second" and finished == []
        with pytest.raises(StopAsyncIteration):
            await collector.__anext__()
        assert finished == [b"firstsecond"]
        finished.clear()
        collector = stream_intent.record_live(parts(), limit=5, finish=finish)
        assert [part async for part in collector] == [b"first", b"second"] and finished == []
        collector = stream_intent.record_live(parts(), limit=100, finish=finish)
        assert await collector.__anext__() == b"first"
        await collector.aclose()
        assert finished == []

        async def failed():
            yield b"first"
            raise RuntimeError("owned generator failed")

        with pytest.raises(RuntimeError):
            async for _ in stream_intent.record_live(failed(), limit=100, finish=finish):
                pass
        assert finished == []

    asyncio.run(run())


def test_streaming_routes_are_anchored_not_blanket_exemptions():
    assert stream_intent.governs("POST", PATH)
    assert stream_intent.governs("POST", "/v1/agents/qa/persona/draft")
    assert not stream_intent.governs("POST", PATH + "/more")
    assert not stream_intent.governs("GET", PATH)
    assert not idempotency.exclusion_reason("POST", PATH)


def test_actual_principal_scopes_same_key_and_revoked_permission_refuses_replay(
    client, engine, model, monkeypatch
):
    world = IdentityWorld.create(engine)
    owner = world.person("voice-owner")
    other = world.person("voice-other")
    binding = world.bind(owner, "owner", project=world.project_a)
    world.bind(other, "owner", project=world.project_a)
    connect_owned(engine, monkeypatch, world.project_a)
    with engine.begin() as c:
        AgentStudioService(main.settings).seed_catalog(c, world.project_a, owner.id)
    model.project = world.project_a
    key = str(uuid4())
    body = {"project_id": world.project_a, "probe": "intro"}
    with world.acting_as(world.ctx(owner)):
        first = post(client, key, body)
        assert first.status_code == 200 and result(first), first.text
        assert post(client, key, body).headers["Idempotency-Replayed"] == "true"
        denied = post(client, key, {**body, "project_id": world.project_b})
        assert denied.status_code == 403, denied.text
    with world.acting_as(world.ctx(other)):
        second = post(client, key, body)
        assert (
            second.status_code == 200
            and result(second)["receipt_id"] != result(first)["receipt_id"]
        ), second.text
    with engine.begin() as c:
        principals = (
            c.execute(
                text("SELECT principal FROM api_idempotency_keys WHERE idempotency_key=:k"),
                {"k": key},
            )
            .scalars()
            .all()
        )
        assert set(principals) == {owner.id, other.id}
        assert set(
            c.execute(
                text("SELECT on_behalf_of FROM jobs WHERE project_id=:p"), {"p": world.project_a}
            ).scalars()
        ) == {owner.id, other.id}
        c.execute(text("DELETE FROM identity_role_bindings WHERE id=:b"), {"b": binding})
        authz.bump_epoch(world.org, db=c, reason="owned.voice.revoke")
    with world.acting_as(world.ctx(owner)):
        assert post(client, key, body).status_code == 403
    assert model.calls == 4


def test_lost_response_after_headers_does_not_buy_a_second_execution(server, engine, model):
    model.block = True
    key = str(uuid4())
    with httpx.Client(base_url=server, timeout=15) as http:
        with http.stream("POST", PATH, json=BODY, headers={"Idempotency-Key": key}) as original:
            assert original.status_code == 200 and model.entered.wait(5)
        # The caller lost delivery; the server may observe cancellation or complete anyway.
        assert post(http, key).status_code == 409 and model.calls == 1
        age_claim(engine, key)
        assert post(http, key).status_code == 409 and model.calls == 1
        model.release.set()
        intent = stream_intent.intent_id("anonymous", key)
        for _ in range(200):
            with engine.connect() as c:
                closed = c.execute(
                    text("SELECT result ? 'calls' FROM receipts WHERE dedupe_key=:d"),
                    {"d": f"agent-voice:{PROJECT}:{intent}"},
                ).scalar_one()
            if closed:
                break
            time.sleep(0.02)
        assert closed
        before = model.calls
        assert before in (1, 2)  # only the original reply, and possibly its original judge
        repeated = post(http, key)
        with engine.connect() as c:
            claim = (
                c.execute(
                    text(
                        "SELECT state,durable_effect,response_body FROM api_idempotency_keys "
                        "WHERE idempotency_key=:k"
                    ),
                    {"k": key},
                )
                .mappings()
                .one()
            )
            invocations = c.execute(
                text("SELECT count(*) FROM model_gateway_invocations")
            ).scalar_one()
        assert model.calls == before and invocations == before
        assert claim["durable_effect"]
        if repeated.status_code == 200:
            assert claim["state"] == "completed"
            assert repeated.headers["Idempotency-Replayed"] == "true"
            assert repeated.content == bytes(claim["response_body"])
            assert result(repeated)["spend_status"] == "settled" and before == 2
        else:
            assert repeated.status_code == 409 and "idempotency_key_in_progress" in repeated.text
        print(
            "owned_lost_response_observation",
            {
                "status": repeated.status_code,
                "record_state": claim["state"],
                "original_calls": before,
                "invocations": invocations,
            },
        )


@pytest.mark.parametrize("stage", ["after_token", "final_close"])
def test_observed_producer_failure_preserves_original_fence(
    server, engine, model, monkeypatch, stage
):
    seen = Event()

    def broken_chunks(reply):
        seen.set()
        yield reply[:10]
        raise RuntimeError("owned SSE generator failed after its first token")

    if stage == "after_token":
        monkeypatch.setattr(agent_voice, "chunks", broken_chunks)
    else:
        original_close = agent_voice.VoiceSpend.close

        def broken_final_close(self, error=None):
            if self.closed:
                seen.set()
                raise RuntimeError("owned producer final bookkeeping failed after result")
            return original_close(self, error)

        monkeypatch.setattr(agent_voice.VoiceSpend, "close", broken_final_close)
    key = str(uuid4())
    with httpx.Client(base_url=server, timeout=15) as http:
        with http.stream("POST", PATH, json=BODY, headers={"Idempotency-Key": key}) as original:
            assert original.status_code == 200
            protocol_error = False
            received = bytearray()
            try:
                for chunk in original.iter_bytes():
                    received.extend(chunk)
            except httpx.RemoteProtocolError:
                protocol_error = True
        with engine.connect() as c:
            observed = (
                c.execute(
                    text(
                        "SELECT state,octet_length(response_body) AS cached_bytes FROM api_idempotency_keys WHERE idempotency_key=:k"
                    ),
                    {"k": key},
                )
                .mappings()
                .one()
            )
        print(
            "owned_generator_failure_observation",
            {
                **dict(observed),
                "chunks_called": seen.is_set(),
                "protocol_error": protocol_error,
                "original_calls": model.calls,
                "token_observed": b"event: token" in received,
                "result_observed": b"event: result" in received,
                "stage": stage,
            },
        )
        assert seen.is_set()
        assert b"event: token" in received
        assert (b"event: result" in received) == (stage == "final_close")
        # Installed BaseHTTPMiddleware may finish HTTP normally before re-raising
        # the producer failure. The trusted producer marker must still fence it.
        assert observed["state"] == "in_progress" and observed["cached_bytes"] is None
        age_claim(engine, key)
        expected_calls = 1 if stage == "after_token" else 2
        # The failed producer can reset its keep-alive connection after HTTP
        # exhaustion. Reconnect as an actual retrying caller with the same intent.
        with httpx.Client(base_url=server, timeout=15) as retry:
            assert post(retry, key).status_code == 409 and model.calls == expected_calls
        with engine.connect() as c:
            claim = (
                c.execute(
                    text(
                        "SELECT state,durable_effect,response_body FROM api_idempotency_keys "
                        "WHERE idempotency_key=:k"
                    ),
                    {"k": key},
                )
                .mappings()
                .one()
            )
            assert claim["state"] == "in_progress" and claim["durable_effect"]
            assert claim["response_body"] is None
            assert (
                c.execute(text("SELECT count(*) FROM model_gateway_invocations")).scalar_one()
                == expected_calls
            )


def test_unprotected_generic_claim_still_uses_normal_takeover(engine):
    key = str(uuid4())
    owner = {"principal": "owned-generic", "key": key}
    with engine.begin() as c:
        assert (
            idempotency.claim(
                c, **owner, method="POST", path="/v1/tasks", fingerprint="same"
            ).outcome
            == "run"
        )
    age_claim(engine, key)
    with engine.begin() as c:
        assert (
            idempotency.claim(
                c, **owner, method="POST", path="/v1/tasks", fingerprint="same"
            ).outcome
            == "run"
        )
        idempotency.release(c, **owner)
        assert (
            c.execute(
                text("SELECT count(*) FROM api_idempotency_keys WHERE idempotency_key=:k"),
                {"k": key},
            ).scalar_one()
            == 0
        )


def test_disabled_source_cannot_contact_provider(client, engine, model):
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE entitlement_sources SET status='disabled' WHERE project_id=:p "
                "AND source_id='approved_local_model_host'"
            ),
            {"p": PROJECT},
        )
    response = post(client, str(uuid4()))
    assert "event: error" in response.text and model.calls == 0
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM model_gateway_invocations")).scalar_one() == 0
        assert (
            c.execute(
                text("SELECT count(*) FROM budget_reservations WHERE status='active'")
            ).scalar_one()
            == 0
        )


def test_stream_checks_current_authority_before_a_later_call(client, engine, model, monkeypatch):
    world = IdentityWorld.create(engine)
    owner = world.person("voice-midstream-owner")
    binding = world.bind(owner, "owner", project=world.project_a)
    connect_owned(engine, monkeypatch, world.project_a)
    with engine.begin() as c:
        AgentStudioService(main.settings).seed_catalog(c, world.project_a, owner.id)
    model.project = world.project_a
    original = model.create_text_response

    def revoke_after_reply(*args, **kwargs):
        value = original(*args, **kwargs)
        with engine.begin() as c:
            c.execute(
                text("DELETE FROM identity_role_bindings WHERE id=:b"),
                {"b": binding},
            )
            authz.bump_epoch(world.org, db=c, reason="owned.voice.midstream.revoke")
        return value

    monkeypatch.setattr(model, "create_text_response", revoke_after_reply)
    with world.acting_as(world.ctx(owner)):
        response = post(client, str(uuid4()), {"project_id": world.project_a, "probe": "intro"})
    # The accepted first call keeps its receipt for reconciliation; no judge is contacted.
    assert "needs_reconciliation" in response.text and model.calls == 1, response.text
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM model_gateway_invocations WHERE project_id=:p"),
                {"p": world.project_a},
            ).scalar_one()
            == 1
        )


def test_known_failed_generator_never_releases_a_protected_claim(engine):
    key = str(uuid4())
    owner = {"principal": "owned-generator", "key": key}
    with engine.begin() as c:
        assert (
            idempotency.claim(c, **owner, method="POST", path=PATH, fingerprint="original").outcome
            == "run"
        )
        idempotency.protect(c, fingerprint="original", **owner)
        idempotency.release(c, **owner)
    age_claim(engine, key)
    with engine.begin() as c:
        assert (
            idempotency.claim(c, **owner, method="POST", path=PATH, fingerprint="original").outcome
            == "in_progress"
        )
        assert (
            idempotency.claim(c, **owner, method="POST", path=PATH, fingerprint="edited").outcome
            == "mismatch"
        )


def test_recording_error_does_not_turn_partial_stream_into_success():
    async def run():
        async def parts():
            yield b"delivered"

        async def finish(_):
            raise RuntimeError("owned recording error")

        collector = stream_intent.record_live(parts(), limit=100, finish=finish)
        assert await collector.__anext__() == b"delivered"
        with pytest.raises(RuntimeError, match="recording error"):
            await collector.__anext__()

    asyncio.run(run())


def test_old_uncommitted_request_cannot_protect_a_changed_takeover(engine):
    key = str(uuid4())
    owner = {"principal": "owned-stale-request", "key": key}
    with engine.begin() as c:
        assert (
            idempotency.claim(c, **owner, method="POST", path=PATH, fingerprint="old").outcome
            == "run"
        )
    age_claim(engine, key)
    with engine.begin() as c:
        assert (
            idempotency.claim(c, **owner, method="POST", path=PATH, fingerprint="new").outcome
            == "run"
        )
    with pytest.raises(RuntimeError, match="no longer owned"):
        with engine.begin() as c:
            idempotency.protect(c, **owner, fingerprint="old")
    with engine.begin() as c:
        idempotency.protect(c, **owner, fingerprint="new")
        assert (
            idempotency.claim(c, **owner, method="POST", path=PATH, fingerprint="new").outcome
            == "in_progress"
        )
