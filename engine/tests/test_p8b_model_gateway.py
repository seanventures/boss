"""P8b S10 (BOSSAPP-1071): the central streaming model gateway, with no database.

  * P8b-T12 (the gateway half): a fake upstream emitting 1,000 chunks is relayed in order, chunk
    by chunk, with back-pressure (the upstream is read only as fast as the client takes it) and
    flat memory;
  * P8b-T9: a guest-supplied Authorization / x-api-key / cookie / X-Boss-* header never survives;
    the real key goes on in the provider's own form; a placeholder (`boss_mdl_`) never reaches
    the upstream under any header; a handle sent to a non-model path or with a model the run
    does not route is refused before a byte goes upstream;
  * fencing: the host's epoch, the handle's epoch and the run's current epoch must agree, or
    `409 fenced`; a handle from another VM is a replay: revoked, reported, refused;
  * budget: exhaustion refuses `402 spend_refused`, nothing is sent;
  * SigV4: the gateway's signer agrees with botocore's on a Bedrock request;
  * strict CONNECT: model hosts leave boss-egress's allowlist in strict mode;
  * the loopback proxy of the local lane shares the gateway's adapters and rewrite.

The database cases (handles bound in Postgres, the fence read from jobs, the audit rows, the work
item carrying the guest's handles) live in test_p8b_model_gateway_db.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from boss_engine.services import streaming_model_gateway as smg

ROOT = Path(__file__).resolve().parents[2]
HOST_HEADERS = {"authorization": "BossHost fake-proof", smg.VM_HEADER: "vm_a",
                smg.RUN_HEADER: "run_1", smg.EPOCH_HEADER: "3", smg.DEPLOYMENT_HEADER: "dep_1"}
REAL_KEY = "sk-ant-api03-not-a-real-key-" + "k" * 40
HANDLE = smg.TOKEN_PREFIX + "a" * 64


def _handle(**over: Any) -> smg.Handle:
    base = dict(id="mdl_1", token_hash=smg.hash_handle(HANDLE), org_id="org_1", project_id="p_1",
                run_id="run_1", fence_epoch=3, vm_id="vm_a", deployment_id="dep_1",
                provider="anthropic", upstream=None, credential_ref="env:ANTHROPIC_API_KEY",
                models=(), expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    base.update(over)
    return smg.Handle(**base)


class FakeAudit:
    def __init__(self) -> None:
        self.rows: list = []

    def put(self, row: Any) -> None:
        self.rows.append(dict(row))

    def pending(self) -> int:
        return 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class FakeKeys:
    def __init__(self, value: str = REAL_KEY) -> None:
        self.value, self.asked = value, []

    def resolve(self, ref: str, project_id: str) -> str:
        self.asked.append((ref, project_id))
        if self.value is None:
            raise smg.KeyUnavailable("env:ANTHROPIC_API_KEY is not set")
        return self.value


class FakeUpstream:
    """Records what reaches the upstream; streams `chunks` back, counting how many were pulled."""

    def __init__(self, chunks: list, *, status: int = 200,
                 content_type: str = "text/event-stream") -> None:
        self.chunks, self.status, self.content_type = chunks, status, content_type
        self.calls: list = []
        self.pulled = 0
        self.closed = False

    @contextlib.asynccontextmanager
    async def __call__(self, method: str, url: str, headers: dict, body: bytes):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})

        async def gen():
            for chunk in self.chunks:
                self.pulled += 1
                yield chunk

        try:
            yield self.status, {"content-type": self.content_type, "x-request-id": "up-1"}, gen()
        finally:
            self.closed = True


def _gateway(upstream: Any, *, handle: smg.Handle = None, keys: Any = None, budget: Any = False,
             direct: bool = False, audit: FakeAudit = None) -> smg.ModelGateway:
    gw = smg.ModelGateway(engine=None, settings=None, upstream=upstream, keys=keys or FakeKeys(),
                          budget=budget, audit=audit or FakeAudit(), direct=direct,
                          verify_host=lambda token, method, path: ("hst_1", "org_1"),
                          ledger=False)
    gw._resolve_in_tx = lambda caller, raw: (handle or _handle())  # type: ignore[method-assign]
    return gw


def _app(gw: smg.ModelGateway):
    import sys

    sys.path.insert(0, str(ROOT / "apps" / "model-gateway"))
    from boss_model_gateway.app import build_app

    return build_app(engine=object(), settings=object(), gateway=gw)


# ── P8b-T9: the rewrite ──────────────────────────────────────────────────────────────────────

def test_guest_credentials_never_survive_and_the_real_key_goes_on():
    up = FakeUpstream([b'data: {"type":"message_start","message":{"model":"claude-x","usage":{"input_tokens":5}}}\n\n',
                       b'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'])
    audit = FakeAudit()
    gw = _gateway(up, audit=audit)
    with TestClient(_app(gw)) as client:
        resp = client.post("/anthropic/v1/messages?beta=true", json={"model": "claude-x"},
                           headers={**HOST_HEADERS, smg.HANDLE_HEADER: HANDLE,
                                    "x-api-key": "guest-supplied", "cookie": "sid=1",
                                    "x-forwarded-for": "10.0.0.9", "x-boss-run-id": "forged",
                                    "boss-run-assertion": "", "anthropic-version": "2023-06-01",
                                    "accept-encoding": "gzip"})
    assert resp.status_code == 200
    assert b"message_delta" in resp.content
    sent = up.calls[0]
    assert sent["url"] == "https://api.anthropic.com/v1/messages?beta=true"
    h = {k.lower(): v for k, v in sent["headers"].items()}
    assert h["x-api-key"] == REAL_KEY, "the real key, in the provider's own header"
    assert h["anthropic-version"] == "2023-06-01", "provider headers pass"
    assert h["accept-encoding"] == "identity"
    for name in ("authorization", "cookie", "x-forwarded-for", "x-boss-run-id", "x-boss-handle",
                 "x-boss-vm", "x-boss-run", "x-boss-fence-epoch", "boss-run-assertion"):
        assert name not in h, name
    assert "guest-supplied" not in json.dumps(sent["headers"])
    assert HANDLE not in json.dumps(sent["headers"]), "the placeholder never goes upstream"
    assert up.closed
    row = audit.rows[-1]
    assert row["verdict"] == "allow" and row["route"] if "route" in row else True
    assert row["run_id"] == "run_1" and row["epoch"] == 3 and row["vm_id"] == "vm_a"
    assert row["handle_fp"] == smg.fingerprint(HANDLE) and HANDLE not in json.dumps(row, default=str)
    assert row["path_template"] == "/anthropic/v1/messages" and row["bytes_out"] > 0


def test_a_placeholder_read_as_the_key_never_reaches_the_upstream():
    up = FakeUpstream([b"x"])
    gw = _gateway(up, keys=FakeKeys(value=smg.TOKEN_PREFIX + "b" * 64))
    with TestClient(_app(gw)) as client:
        resp = client.post("/anthropic/v1/messages", json={"model": "m"},
                           headers={**HOST_HEADERS, smg.HANDLE_HEADER: HANDLE})
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "key_unavailable"
    assert up.calls == []
    with pytest.raises(smg.PlaceholderRefused):
        smg.outbound_headers(smg.ANTHROPIC, {}, smg.TOKEN_PREFIX + "c" * 64)


def test_only_the_providers_inference_paths_and_the_runs_models_are_allowed():
    up = FakeUpstream([b"x"])
    gw = _gateway(up, handle=_handle(models=("claude-ok",)))
    with TestClient(_app(gw)) as client:
        h = {**HOST_HEADERS, smg.HANDLE_HEADER: HANDLE}
        assert client.post("/anthropic/v1/files", json={}, headers=h).status_code == 403
        assert client.get("/anthropic/v1/organizations/keys", headers=h).status_code == 403
        assert client.post("/anthropic/v1/messagesX", json={}, headers=h).status_code == 403
        assert client.post("/openai/v1/chat/completions", json={}, headers=h).json()["error"][
            "code"] == "foreign_provider"
        assert client.post("/nope/v1/messages", json={}, headers=h).status_code == 404
        wrong = client.post("/anthropic/v1/messages", json={"model": "claude-other"}, headers=h)
        assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "model_not_allowed"
        assert up.calls == [], "nothing went upstream"
        ok = client.post("/anthropic/v1/messages", json={"model": "claude-ok"}, headers=h)
        assert ok.status_code == 200 and len(up.calls) == 1


def test_a_host_proof_is_required_unless_the_direct_lane_is_on():
    up = FakeUpstream([b"x"])
    gw = _gateway(up)
    with TestClient(_app(gw)) as client:
        resp = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": HANDLE})
        assert resp.status_code == 401 and resp.json()["error"]["code"] == "host_proof_required"
        assert client.post("/anthropic/v1/messages", json={}, headers={
            **HOST_HEADERS}).json()["error"]["code"] == "no_handle"
    direct = _gateway(up, direct=True, handle=_handle(vm_id=None))
    with TestClient(_app(direct)) as client:
        assert client.post("/anthropic/v1/messages", json={"model": "m"},
                           headers={"x-api-key": HANDLE}).status_code == 200
    assert up.calls and smg.presented_handle({"Authorization": "Bearer " + HANDLE}) == HANDLE


def test_the_assertion_must_agree_with_the_hosts_headers():
    import base64

    def assertion(**claims: Any) -> str:
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        return f"eyJhbGciOiJFUzI1NiJ9.{body}.c2ln"

    gw = _gateway(FakeUpstream([b"x"]))
    agree = assertion(run="run_1", vm="vm_a", epoch=3, aud="model")
    disagree = assertion(run="run_2", vm="vm_a", epoch=3, aud="model")
    with TestClient(_app(gw)) as client:
        h = {**HOST_HEADERS, smg.HANDLE_HEADER: HANDLE}
        assert client.post("/anthropic/v1/messages", json={}, headers={
            **h, smg.ASSERTION_HEADER: agree}).status_code == 200
        bad = client.post("/anthropic/v1/messages", json={}, headers={
            **h, smg.ASSERTION_HEADER: disagree})
        assert bad.status_code == 409 and bad.json()["error"]["code"] == "assertion_mismatch"


# ── fencing and replay (resolve(), with the registry faked) ─────────────────────────────────

class _FakeEngine:
    @contextlib.contextmanager
    def begin(self):
        yield "connection"


def _resolver(monkeypatch, handle: smg.Handle, *, now_epoch: int):
    events: list = []
    revoked: list = []
    monkeypatch.setattr(smg, "load_handle", lambda connection, raw: handle)
    monkeypatch.setattr(smg, "current_epoch", lambda connection, run_id: now_epoch)
    monkeypatch.setattr(smg, "revoke_handle", lambda c, h, reason: revoked.append((h, reason)))
    from boss_engine.services import fleet

    monkeypatch.setattr(fleet, "host_event", lambda c, **kw: events.append(kw))
    gw = smg.ModelGateway(engine=_FakeEngine(), settings=None, upstream=FakeUpstream([]),
                          keys=FakeKeys(), budget=False, audit=FakeAudit(),
                          verify_host=lambda *a, **k: ("hst_1", "org_1"), ledger=False)
    return gw, events, revoked


def test_a_fenced_epoch_is_refused_within_one_request(monkeypatch):
    gw, events, revoked = _resolver(monkeypatch, _handle(fence_epoch=3), now_epoch=3)
    good = smg.Caller(host_id="hst_1", org_id="org_1", vm_id="vm_a", run_id="run_1", epoch=3)
    assert gw.resolve("connection", good, HANDLE).run_id == "run_1"
    # the host's word is stale
    with pytest.raises(smg.Refused) as stale:
        gw.resolve("connection", smg.Caller(host_id="hst_1", org_id="org_1", vm_id="vm_a",
                                            run_id="run_1", epoch=2), HANDLE)
    assert (stale.value.status, stale.value.reason) == (409, "fenced")
    # the run moved on in BOSS: the very next request is refused
    monkeypatch.setattr(smg, "current_epoch", lambda connection, run_id: 4)
    with pytest.raises(smg.Refused) as moved:
        gw.resolve("connection", good, HANDLE)
    assert (moved.value.status, moved.value.reason) == (409, "fenced")
    assert revoked == [] and events == [], "a fence is not a replay"


def test_a_handle_from_another_vm_is_a_replay_revoked_and_reported(monkeypatch):
    gw, events, revoked = _resolver(monkeypatch, _handle(vm_id="vm_a"), now_epoch=3)
    with pytest.raises(smg.Refused) as replay:
        gw.resolve("connection", smg.Caller(host_id="hst_2", org_id="org_1", vm_id="vm_b",
                                            run_id="run_1", epoch=3), HANDLE)
    assert (replay.value.status, replay.value.reason) == (401, "handle_replay")
    assert revoked == [(smg.hash_handle(HANDLE), "handle_replay:another_vm")]
    assert events[0]["kind"] == "handle_replay" and events[0]["host_id"] == "hst_2"
    assert events[0]["vm_id"] == "vm_b" and events[0]["detail"]["bound_vm"] == "vm_a"
    assert HANDLE not in json.dumps(events, default=str)
    # revoked and expired handles are refused by name, never a replay
    monkeypatch.setattr(smg, "load_handle", lambda c, raw: _handle(
        revoked_at=dt.datetime.now(dt.timezone.utc)))
    with pytest.raises(smg.Refused) as dead:
        gw.resolve("connection", smg.Caller(host_id="hst_1", org_id="org_1"), HANDLE)
    assert dead.value.reason == "handle_revoked"
    with pytest.raises(smg.Refused) as none:
        gw.resolve("connection", smg.Caller(host_id="hst_1", org_id="org_1"), "not-a-handle")
    assert none.value.reason == "no_handle"


# ── budget ───────────────────────────────────────────────────────────────────────────────────

def test_budget_exhaustion_refuses_before_anything_is_sent_and_a_good_call_settles():
    from boss_engine.services.model_routing import ModelSpendRefused

    class Budget:
        def __init__(self, refuse: bool) -> None:
            self.refuse, self.settled, self.failed = refuse, [], []

        def reserve(self, project_id, run_id, model):
            if self.refuse:
                raise ModelSpendRefused("budget cap reached for p_1")
            return ("gate", ("res_1", "rcpt_1"))

        def settle(self, held, usage):
            self.settled.append((held, dict(usage)))

        def fail(self, held, error):
            self.failed.append((held, str(error)))

    up = FakeUpstream([b"x"])
    refusing = Budget(refuse=True)
    gw = _gateway(up, budget=refusing)
    with TestClient(_app(gw)) as client:
        resp = client.post("/anthropic/v1/messages", json={"model": "m"},
                           headers={**HOST_HEADERS, smg.HANDLE_HEADER: HANDLE})
    assert resp.status_code == 402 and resp.json()["error"]["code"] == "spend_refused"
    assert up.calls == []
    up = FakeUpstream([b'{"model":"claude-x","usage":{"input_tokens":11,"output_tokens":4}}'],
                      content_type="application/json")
    paying = Budget(refuse=False)
    gw = _gateway(up, budget=paying)
    with TestClient(_app(gw)) as client:
        resp = client.post("/anthropic/v1/messages", json={"model": "claude-x"},
                           headers={**HOST_HEADERS, smg.HANDLE_HEADER: HANDLE})
    assert resp.status_code == 200
    (held, usage), = paying.settled
    assert held == ("gate", ("res_1", "rcpt_1")), "settled with what reserve() returned"
    assert usage["prompt_tokens"] == 11 and usage["completion_tokens"] == 4
    assert usage["model"] == "claude-x", "accounted from the provider's own answer"
    # an upstream error releases the reservation instead
    up = FakeUpstream([b'{"type":"error"}'], status=529, content_type="application/json")
    failing = Budget(refuse=False)
    with TestClient(_app(_gateway(up, budget=failing))) as client:
        assert client.post("/anthropic/v1/messages", json={"model": "m"},
                           headers={**HOST_HEADERS, smg.HANDLE_HEADER: HANDLE}).status_code == 529
    assert failing.failed and failing.settled == []


def test_a_missing_key_is_a_503_and_releases_the_reservation():
    class Budget:
        failed: list = []

        def reserve(self, *a):
            return ("gate", ("r", "c"))

        def settle(self, *a):
            raise AssertionError("never settled")

        def fail(self, held, error):
            self.failed.append(held)

    up = FakeUpstream([b"x"])
    gw = _gateway(up, keys=FakeKeys(value=None), budget=Budget())
    with TestClient(_app(gw)) as client:
        resp = client.post("/anthropic/v1/messages", json={}, headers={
            **HOST_HEADERS, smg.HANDLE_HEADER: HANDLE})
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "key_unavailable"
    assert Budget.failed == [("gate", ("r", "c"))] and up.calls == []


# ── P8b-T12: streaming ───────────────────────────────────────────────────────────────────────

async def _drive(app: Any, *, chunks_before_block: int, release: asyncio.Event,
                 pulled: list) -> list:
    """Call the ASGI app by hand: the `send` side stalls after `chunks_before_block` body chunks
    until `release` is set, so back-pressure is observable; returns the body chunks received."""
    body = json.dumps({"model": "m"}).encode()
    scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/anthropic/v1/messages", "raw_path": b"/anthropic/v1/messages",
             "query_string": b"", "root_path": "", "server": ("test", 80),
             "client": ("127.0.0.1", 1), "headers": [
                 (k.encode(), v.encode()) for k, v in {**HOST_HEADERS, smg.HANDLE_HEADER: HANDLE,
                                                       "content-type": "application/json",
                                                       "content-length": str(len(body))}.items()]}
    sent_body = False
    forever = asyncio.Event()

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await forever.wait()
        return {"type": "http.disconnect"}

    received: list = []

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            received.append(message["body"])
            pulled.append(len(received))
            if len(received) == chunks_before_block:
                await release.wait()

    await app(scope, receive, send)
    return received


def test_a_thousand_chunks_are_relayed_in_order_with_back_pressure_and_flat_memory():
    chunks = [b"data: %06d\n\n" % i for i in range(1000)]
    up = FakeUpstream(chunks)
    gw = _gateway(up)
    app = _app(gw)

    async def scenario():
        release = asyncio.Event()
        pulled: list = []
        task = asyncio.create_task(_drive(app, chunks_before_block=10, release=release,
                                          pulled=pulled))
        for _ in range(200):          # let the relay run until it blocks on the slow client
            await asyncio.sleep(0.005)
            if len(pulled) >= 10:
                break
        await asyncio.sleep(0.05)
        stalled_at = up.pulled
        assert 10 <= stalled_at <= 12, (
            f"the upstream was read {stalled_at} chunks ahead of a client that took 10: "
            "chunks are being buffered")
        release.set()
        received = await asyncio.wait_for(task, 10)
        return received

    received = asyncio.run(scenario())
    assert received == chunks, "every chunk, in order, none merged or dropped"
    assert up.pulled == 1000 and up.closed

    # memory: 1,000 chunks of 64 KiB (64 MiB) through the relay leave no buffer behind
    big = [bytes([65 + (i % 26)]) * 65536 for i in range(1000)]
    up2 = FakeUpstream(big, content_type="application/octet-stream")
    app2 = _app(_gateway(up2))

    async def discard():
        release = asyncio.Event()
        release.set()
        pulled: list = []
        received = await _drive(app2, chunks_before_block=10**9, release=release, pulled=pulled)
        return len(received)

    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    count = asyncio.run(discard())
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 1000
    assert peak - before < 8 * 1024 * 1024, f"peak grew by {(peak - before) // 1024} KiB"


# ── SigV4 ────────────────────────────────────────────────────────────────────────────────────

def test_sigv4_agrees_with_botocore_on_a_bedrock_request():
    botocore = pytest.importorskip("botocore")
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    url = ("https://bedrock-runtime.us-east-1.amazonaws.com/model/"
           "anthropic.claude-3-5-sonnet-20241022-v2%3A0/converse")
    body = json.dumps({"messages": [{"role": "user", "content": [{"text": "hi"}]}]}).encode()
    access, secret = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    for token in (None, "FQoGZXIvYXdzEBYaDExampleSessionToken"):
        request = AWSRequest(method="POST", url=url, data=body,
                             headers={"content-type": "application/json"})
        SigV4Auth(Credentials(access, secret, token=token), "bedrock", "us-east-1").add_auth(request)
        theirs = dict(request.headers)
        mine = smg.sigv4_headers("POST", url, {"content-type": "application/json"}, body,
                                 access_key=access, secret_key=secret, session_token=token,
                                 region="us-east-1", service="bedrock",
                                 amz_date=theirs["X-Amz-Date"])
        assert mine["authorization"] == theirs["Authorization"], botocore.__version__
        if token:
            assert mine["x-amz-security-token"] == token
    # the adapter signs when the credential is an access-key pair, and the pair never leaks
    out = smg.outbound_headers(smg.BEDROCK, {"authorization": "Bearer guest", "content-type":
                                             "application/json"}, f"{access}:{secret}",
                               method="POST", url=url, body=body)
    assert out["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/")
    assert secret not in json.dumps(out) and "guest" not in json.dumps(out)
    assert smg.BEDROCK.allows("POST", "/model/anthropic.claude-3%3A0/converse-stream")
    assert not smg.BEDROCK.allows("POST", "/model/x/list") and not smg.BEDROCK.allows("GET", "/")
    assert smg.BEDROCK.model_of("/model/anthropic.claude-3%3A0/converse", None) == \
        "anthropic.claude-3:0"
    assert smg.upstream_for(smg.BEDROCK, "eu-west-1") == \
        "https://bedrock-runtime.eu-west-1.amazonaws.com"
    # a Bedrock API key is a bearer, not a signature
    assert smg.outbound_headers(smg.BEDROCK, {}, "bedrock-api-key-value", url=url)[
        "authorization"] == "Bearer bedrock-api-key-value"


def test_azure_reads_the_deployment_from_the_path_and_keys_with_api_key():
    path = "/openai/deployments/gpt-4o-prod/chat/completions"
    assert smg.AZURE_OPENAI.allows("POST", path) and not smg.AZURE_OPENAI.allows("GET", path)
    assert smg.AZURE_OPENAI.model_of(path, b"{}") == "gpt-4o-prod"
    assert smg.outbound_headers(smg.AZURE_OPENAI, {"api-key": "guest"}, "real")["api-key"] == "real"
    assert smg.upstream_for(smg.AZURE_OPENAI, "https://acme.openai.azure.com/") == \
        "https://acme.openai.azure.com"


# ── the two lanes share one implementation; strict CONNECT; the host's endpoint ─────────────

def test_the_loopback_proxy_shares_the_gateways_adapters_and_rewrite():
    from boss_engine.services import model_proxy

    assert model_proxy.ADAPTERS is smg.ADAPTERS and model_proxy.ANTHROPIC is smg.ANTHROPIC
    assert model_proxy.outbound_headers is smg.outbound_headers
    assert model_proxy._UsageReader is smg.UsageReader
    proxy = model_proxy.ModelProxy(upstream=lambda *a: None)
    issued = proxy.issue("anthropic", REAL_KEY, ttl=60)
    grant = proxy.grant_for(issued.token)
    out = proxy.forward_headers(grant, {"x-api-key": issued.token, "cookie": "a=b",
                                        "x-boss-run-id": "forged"})
    assert out["x-api-key"] == REAL_KEY and "cookie" not in out and "x-boss-run-id" not in out
    assert not model_proxy.GatewayClient.enabled(), "the thin-client lane is off by default"


def test_strict_mode_removes_model_hosts_from_the_connect_allowlist(monkeypatch):
    from apps.egress.boss_egress import connect

    monkeypatch.delenv("BOSS_EGRESS_CONNECT_ALLOW", raising=False)
    monkeypatch.delenv(connect.STRICT_ENV, raising=False)
    assert connect.host_allowed("api.anthropic.com") and connect.host_allowed("claude.ai")
    monkeypatch.setenv(connect.STRICT_ENV, "1")
    for host in ("api.anthropic.com", "claude.ai", "chatgpt.com", "api.openai.com",
                 "generativelanguage.googleapis.com", "openrouter.ai"):
        assert not connect.host_allowed(host), host
    assert connect.host_allowed("registry.npmjs.org") and connect.host_allowed("pypi.org")
    assert not connect.host_allowed("github.com")


def test_the_host_learns_the_gateways_address_from_its_desired_state(monkeypatch):
    from boss_engine.services import fleet

    monkeypatch.delenv(smg.URL_ENV, raising=False)
    assert fleet.endpoints("hst_1")["model_gateway"] == ""
    monkeypatch.setenv(smg.URL_ENV, "http://model-gateway:8092/")
    assert fleet.endpoints("hst_1")["model_gateway"] == "http://model-gateway:8092"
    assert fleet.endpoints("hst_1")["run_plane"] == "/v1/run-plane"


def test_the_usage_reader_accounts_from_the_stream_not_the_agent():
    reader = smg.UsageReader("text/event-stream")
    reader.feed(b'data: {"type":"message_start","message":{"model":"claude-x",'
                b'"usage":{"input_tokens":12,"cache_read_input_tokens":3}}}\n\ndata: {"type"')
    reader.feed(b':"message_delta","usage":{"output_tokens":40}}\n\ndata: {"type":"message_delta",'
                b'"usage":{"output_tokens":55}}\n')
    found = reader.finish()
    assert found == {"model": "claude-x", "input_tokens": 12, "cache_read_input_tokens": 3,
                     "output_tokens": 55}
    assert smg.as_ledger_usage(found, model="m")["total_tokens"] == 67
    bedrock = smg.UsageReader("application/json")
    bedrock.feed(b'{"output":{},"usage":{"inputTokens":9,"outputTokens":2}}')
    assert smg.as_ledger_usage(bedrock.finish(), model="b") ["prompt_tokens"] == 9
    assert smg.path_template("/bedrock/model/arn%3Aaws/converse") == "/bedrock/model/*"


def test_handles_name_a_reference_never_a_value():
    class Conn:
        def execute(self, *a, **k):
            raise AssertionError("not reached")

    with pytest.raises(ValueError):
        smg.issue_handle(Conn(), org_id="o", project_id="p", run_id="r", epoch=0, vm_id=None,
                         provider="anthropic", credential_ref="sk-ant-a-real-looking-key-value")
    with pytest.raises(ValueError):
        smg.issue_handle(Conn(), org_id="o", project_id="p", run_id="r", epoch=0, vm_id=None,
                         provider="nope", credential_ref="env:X")
    assert sorted(smg.ADAPTERS) == ["anthropic", "azure_openai", "bedrock", "openai", "openrouter"]


def test_python_and_go_agree_on_the_contracts_credential_rules():
    """The v1.1 validators share their field list and reason codes (P8b S9)."""
    import re

    from boss_engine.services import sandbox_contract as sc

    go = (ROOT / "services" / "sandbox-service" / "internal" / "contract" / "contract.go").read_text()
    start = go.index("var RawCredentialFields = []string{")
    names = set(re.findall(r'"([a-z_]+)"', go[start:go.index("\n}\n", start)]))
    assert names == set(sc.RAW_CREDENTIAL_FIELDS)
    start = go.index("var HandlePrefixes = map[string]string{")
    pairs = dict(re.findall(r'"([a-z]+)": "([a-z_]+)"', go[start:go.index("\n}\n", start)]))
    assert pairs == sc.HANDLE_PREFIXES
    for reason in (sc.RAW_CREDENTIAL_FIELD, sc.SECRET_SHAPED_VALUE, sc.CONTRACT_DEPRECATED,
                   sc.CONTRACT_LEGACY_REFUSED):
        assert f'"{reason}"' in go, reason
    assert 'const Version = "1.1"' in go and 'const LegacyVersion = "1.0"' in go


# ── review round 2: aborted streams, Bedrock event streams, allowlists, wiring ───────────────

def test_an_abandoned_stream_is_settled_with_what_was_read_not_released():
    """A client that disconnects mid-answer was still answered by the provider: the usage read
    so far is settled; only an upstream error, or a stream that never produced a byte, releases
    the reservation."""
    class Budget:
        def __init__(self) -> None:
            self.settled, self.failed = [], []

        def reserve(self, project_id, run_id, model):
            return ("gate", ("r", "c"))

        def settle(self, held, usage):
            self.settled.append(dict(usage))

        def fail(self, held, error):
            self.failed.append(str(error))

    chunks = [b'data: {"type":"message_start","message":{"usage":{"input_tokens":30}}}\n\n'] + [
        b'data: {"type":"content_block_delta","delta":{"text":"x"}}\n\n' for _ in range(20)] + [
        b'data: {"type":"message_delta","usage":{"output_tokens":99}}\n\n']
    up = FakeUpstream(chunks)
    budget = Budget()
    audit = FakeAudit()
    app = _app(_gateway(up, budget=budget, audit=audit))

    async def abandon():
        body = json.dumps({"model": "m"}).encode()
        scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/anthropic/v1/messages", "raw_path": b"/anthropic/v1/messages",
                 "query_string": b"", "root_path": "", "server": ("test", 80),
                 "client": ("127.0.0.1", 1), "headers": [
                     (k.encode(), v.encode()) for k, v in {
                         **HOST_HEADERS, smg.HANDLE_HEADER: HANDLE,
                         "content-type": "application/json",
                         "content-length": str(len(body))}.items()]}
        sent = {"body": False}

        async def receive():
            if not sent["body"]:
                sent["body"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        got = []

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                got.append(message["body"])
                if len(got) == 5:
                    raise ConnectionResetError("client went away")

        with pytest.raises(Exception):
            await app(scope, receive, send)
        return got

    got = asyncio.run(abandon())
    assert len(got) == 5 and up.closed
    assert budget.failed == [], "an abandoned stream is not a free call"
    (usage,) = budget.settled
    assert usage["prompt_tokens"] == 30, "what the provider had already said is charged"
    assert usage["completion_tokens"] == 0, "the final delta never arrived: nothing invented"
    row = audit.rows[-1]
    assert row["verdict"] == "allow" and row["rejected_by"] == "client_disconnected"
    assert 0 < row["bytes_out"] < sum(len(c) for c in chunks)


def _eventstream_frame(event_type: str, payload: bytes) -> bytes:
    import struct
    import zlib

    name = b":event-type"
    header = bytes([len(name)]) + name + bytes([7]) + struct.pack(">H", len(event_type)) + \
        event_type.encode()
    total = 12 + len(header) + len(payload) + 4
    prelude = struct.pack(">II", total, len(header))
    prelude += struct.pack(">I", zlib.crc32(prelude))
    body = prelude + header + payload
    return body + struct.pack(">I", zlib.crc32(body))


def test_bedrock_event_streams_are_accounted_from_their_frames():
    import base64

    reader = smg.UsageReader("application/vnd.amazon.eventstream")
    frames = (_eventstream_frame("messageStart", b'{"role":"assistant"}')
              + _eventstream_frame("contentBlockDelta", b'{"delta":{"text":"hi"}}')
              + _eventstream_frame("metadata", b'{"usage":{"inputTokens":41,"outputTokens":7}}'))
    # fed in awkward pieces: a frame boundary never lines up with a chunk boundary
    for i in range(0, len(frames), 7):
        reader.feed(frames[i:i + 7])
    assert reader.finish() == {"input_tokens": 41, "output_tokens": 7}
    assert reader.buf == b"", "no frame is held past the chunk in flight"
    # invoke-with-response-stream wraps the model's own events in base64 `bytes`
    reader = smg.UsageReader("application/vnd.amazon.eventstream")
    inner = json.dumps({"type": "message_stop", "amazon-bedrock-invocationMetrics": {
        "inputTokenCount": 12, "outputTokenCount": 5}}).encode()
    start = json.dumps({"type": "message_start", "message": {
        "model": "claude-on-bedrock", "usage": {"input_tokens": 12}}}).encode()
    reader.feed(_eventstream_frame("chunk", json.dumps(
        {"bytes": base64.b64encode(start).decode()}).encode()))
    reader.feed(_eventstream_frame("chunk", json.dumps(
        {"bytes": base64.b64encode(inner).decode()}).encode()))
    found = reader.finish()
    assert found["input_tokens"] == 12 and found["output_tokens"] == 5
    assert found["model"] == "claude-on-bedrock"
    # garbage is not a frame: reading stops, nothing is invented, nothing is held
    reader = smg.UsageReader("application/vnd.amazon.eventstream")
    reader.feed(b"\x00\x00\x00\x05" + b"garbage " * 4)
    assert reader.finish() == {} and reader.buf == b""


def test_a_handle_is_allow_listed_to_the_runs_routed_models(monkeypatch):
    from boss_engine.services import model_routing

    class Conn:
        def execute(self, *a, **k):
            raise RuntimeError("no database in this unit test")

    resolutions = {"build": "claude-sonnet-4-5", "review": "claude-opus-4-1", "chat": None,
                   "decisions": "claude-sonnet-4-5", "hard": "auto", "routine": ""}
    monkeypatch.setattr(model_routing, "resolve_job",
                        lambda c, s, p, job: SimpleNamespace(model=resolutions.get(job)))
    assert smg.routed_models(Conn(), None, "p_1") == ("claude-sonnet-4-5", "claude-opus-4-1")
    minted: dict = {}

    def fake_issue(connection, **kw):
        minted.update(kw)
        return smg.TOKEN_PREFIX + "m" * 64

    monkeypatch.setattr(smg, "issue_handle", fake_issue)
    monkeypatch.setattr(smg, "credential_ref_for", lambda c, s, p, prov: "env:ANTHROPIC_API_KEY")
    monkeypatch.setattr(smg, "provider_for_project", lambda c, s, p: "anthropic")
    out = smg.guest_handles(Conn(), None, org_id="o", project_id="p_1", run_id="r", epoch=4,
                            vm_id=None)
    assert out["mdl_provider"] == "anthropic"
    assert minted["models"] == ("claude-sonnet-4-5", "claude-opus-4-1") and minted["epoch"] == 4
    # a caller that names the task's model pins exactly that
    smg.guest_handles(Conn(), None, org_id="o", project_id="p_1", run_id="r", epoch=4,
                      vm_id=None, models=("gpt-5",))
    assert minted["models"] == ("gpt-5",)
    # and the gateway refuses a model off the list before a byte goes upstream
    up = FakeUpstream([b"x"])
    gw = _gateway(up, handle=_handle(models=("claude-sonnet-4-5", "claude-opus-4-1")))
    with TestClient(_app(gw)) as client:
        h = {**HOST_HEADERS, smg.HANDLE_HEADER: HANDLE}
        off = client.post("/anthropic/v1/messages", json={"model": "claude-haiku-4"}, headers=h)
        assert off.status_code == 403 and off.json()["error"]["code"] == "model_not_allowed"
        assert up.calls == []
        assert client.post("/anthropic/v1/messages", json={"model": "claude-opus-4-1"},
                           headers=h).status_code == 200


def test_the_handles_endpoint_reads_the_runs_epoch_and_takes_no_upstream(monkeypatch):
    from boss_engine.identity import tokens

    minted: dict = {}

    class Result:
        def scalar(self):
            return "org_1"

    class Conn:
        def execute(self, *a, **k):
            return Result()

    class Engine:
        @contextlib.contextmanager
        def begin(self):
            yield Conn()

    def fake_issue(connection, **kw):
        minted.update(kw)
        return smg.TOKEN_PREFIX + "n" * 64

    monkeypatch.setattr(smg, "issue_handle", fake_issue)
    monkeypatch.setattr(smg, "current_epoch", lambda c, run_id: 7)
    monkeypatch.setattr(tokens, "egress_service_key_ok", lambda presented: presented == "svc")
    gw = _gateway(FakeUpstream([b"x"]))
    gw.engine = Engine()
    import sys

    sys.path.insert(0, str(ROOT / "apps" / "model-gateway"))
    from boss_model_gateway.app import build_app

    with TestClient(build_app(engine=Engine(), settings=object(), gateway=gw)) as client:
        body = {"provider": "anthropic", "project_id": "p_1", "run_id": "crun_9",
                "credential_ref": "env:ANTHROPIC_API_KEY", "upstream": "https://evil.example",
                "epoch": 0}
        assert client.post("/_boss/handles", json=body).status_code == 401
        resp = client.post("/_boss/handles", json=body, headers={"X-Boss-Egress-Key": "svc"})
    assert resp.status_code == 200 and resp.json()["handle"].startswith(smg.TOKEN_PREFIX)
    assert minted["epoch"] == 7, "the run's current epoch, read by the gateway"
    assert "upstream" not in minted or minted["upstream"] is None


def test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode():
    import re

    compose = (ROOT / "scripts" / "install" / "community-compose.yml").read_text()

    def env_of(service: str) -> str:
        start = compose.index(f"\n  {service}:\n")
        nxt = re.search(r"\n  [a-z-]+:\n", compose[start + 1:])
        return compose[start:start + 1 + (nxt.start() if nxt else len(compose))]

    for service in ("api", "worker", "model-gateway"):
        assert "BOSS_MODEL_GATEWAY_URL: ${BOSS_MODEL_GATEWAY_URL:-http://model-gateway:8092}" in \
            env_of(service), service
    assert "BOSS_EGRESS_STRICT_SECRETS: ${BOSS_EGRESS_STRICT_SECRETS:-}" in env_of("egress")
    gateway = env_of("model-gateway")
    assert 'command: ["python", "-m", "boss_model_gateway"]' in gateway
    assert "max-size:" in gateway and "/_boss/healthz" in gateway
    assert "ports:" not in gateway, "only the compose network reaches the gateway"


def test_the_pooled_client_is_one_per_process():
    async def scenario():
        a = smg._client()
        b = smg._client()
        assert a is b
        await smg.close_client()
        assert smg._CLIENT is None

    asyncio.run(scenario())
