"""AP-P5: signed agent cards, the A2A JSON-RPC client and the Agent Protocol client (no DB)."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from boss_engine.identity import jwks
from boss_engine.protocols import a2a_types as t
from boss_engine.protocols.a2a_client import A2AClient, A2AError, sse_payloads
from boss_engine.protocols.agent_card import CardSignatureError, canonical, sign_card, verify_card
from boss_engine.protocols.agent_protocol_client import AgentProtocolClient

CARD = {"name": "Triage", "description": "Triages issues.", "version": "3",
        "supportedInterfaces": [{"url": "https://boss.example/a2a/agt_1",
                                 "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": True, "pushNotifications": True},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "triage", "name": "Triage", "description": "ü", "tags": ["ops"]}]}


def _key():
    private = Ed25519PrivateKey.generate()
    jwk = jwks.public_jwk(private.public_key())
    kid = jwks.thumbprint(jwk)
    return private, kid, {"keys": [{**jwk, "kid": kid, "alg": "EdDSA", "use": "sig"}]}


def test_a_signed_card_verifies_against_the_jwks():
    private, kid, keyset = _key()
    signed = sign_card(CARD, private, kid, jku="https://boss.example/v1/agents/agt_1/jwks")
    header = verify_card(signed, keyset)
    assert header["kid"] == kid and header["alg"] == "EdDSA" and header["typ"] == "JOSE"
    assert canonical(signed) == canonical(CARD), "signatures are not part of what is signed"
    assert b"\xc3\xbc" in canonical(CARD), "UTF-8, not ASCII escapes (JCS)"
    # Key order does not matter: canonical JSON.
    reordered = dict(reversed(list(signed.items())))
    assert verify_card(reordered, keyset)["kid"] == kid


def test_a_tampered_card_an_unknown_kid_and_alg_none_are_refused():
    private, kid, keyset = _key()
    signed = sign_card(CARD, private, kid)
    with pytest.raises(CardSignatureError) as bad:
        verify_card({**signed, "description": "Deletes repositories."}, keyset)
    assert bad.value.code == "bad_signature"
    _, _, other = _key()
    with pytest.raises(CardSignatureError) as unknown:
        verify_card(signed, other)
    assert unknown.value.code == "unknown_kid"
    none = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": kid}).encode()
                                    ).rstrip(b"=").decode()
    forged = {**CARD, "signatures": [{"protected": none, "signature": ""}]}
    with pytest.raises(CardSignatureError) as alg:
        verify_card(forged, keyset)
    assert alg.value.code == "bad_alg"
    with pytest.raises(CardSignatureError) as unsigned:
        verify_card(CARD, keyset)
    assert unsigned.value.code == "unsigned"


def test_sse_payloads_join_data_lines_and_skip_comments():
    lines = [": keepalive", 'data: {"a":', 'data: 1}', "", "event: message",
             'data: {"b": 2}', "", "data: not json", ""]
    assert list(sse_payloads(lines)) == [{"a": 1}, {"b": 2}]


def test_the_a2a_client_speaks_v1_json_rpc_and_streams():
    seen = []
    task = {"id": "t1", "contextId": "c1", "status": {"state": "TASK_STATE_WORKING"}}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body["method"], dict(request.headers)))
        if body["method"] == "SendStreamingMessage":
            events = [{"task": task},
                      {"statusUpdate": {"taskId": "t1", "contextId": "c1",
                                        "status": {"state": "TASK_STATE_COMPLETED"}}}]
            sse = "".join(f"data: {json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': e})}\n\n"
                          for e in events)
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        if body["method"] == "GetTask":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32001, "message": "no task"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": {"task": task}})

    client = A2AClient("https://remote.example/a2a", transport=httpx.MockTransport(handler),
                       headers={"Authorization": "Bearer x"})
    tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    got = client.send_message(t.SendMessageRequest(message=t.user_message("hi")), traceparent=tp)
    assert isinstance(got, t.Task) and got.state is t.TaskState.WORKING
    method, headers = seen[0]
    assert method == "SendMessage" and headers["a2a-version"] == "1.0"
    assert headers["traceparent"] == tp
    events = list(client.stream_message(t.SendMessageRequest(message=t.user_message("hi"))))
    assert isinstance(events[0], t.Task) and isinstance(events[1], t.TaskStatusUpdateEvent)
    assert events[1].status.state is t.TaskState.COMPLETED
    with pytest.raises(A2AError) as missing:
        client.get_task("nope")
    assert missing.value.code == t.TASK_NOT_FOUND


def test_a_v03_server_is_asked_again_under_its_old_method_name():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["method"])
        if body["method"] == "GetTask":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32601, "message": "nope"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "kind": "task", "id": "t9", "contextId": "c", "status": {"state": "completed"}}})

    client = A2AClient("https://remote.example/a2a", transport=httpx.MockTransport(handler))
    assert client.get_task("t9").state is t.TaskState.COMPLETED
    assert seen == ["GetTask", "tasks/get"]


def test_an_unsafe_destination_is_refused_before_any_request():
    with pytest.raises(A2AError):
        A2AClient("http://127.0.0.1:9/a2a").get_task("t")


def test_an_agent_protocol_run_is_an_a2a_task():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/threads":
            return httpx.Response(200, json={"thread_id": "th1"})
        if path == "/threads/th1/runs" and request.method == "POST":
            return httpx.Response(200, json={"run_id": "r1", "status": "pending"})
        if path == "/threads/th1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "success",
                                             "metadata": {"cost_usd": 0.0042}})
        if path == "/threads/th1/runs/r1/join":
            return httpx.Response(200, json={"messages": [{"type": "human", "content": "q"},
                                                          {"type": "ai", "content": "answer"}]})
        return httpx.Response(404)

    client = AgentProtocolClient("https://lg.example", agent_id="agent", flavor="langgraph",
                                 transport=httpx.MockTransport(handler))
    thread = client.create_thread()
    run = client.start_run(thread, {"messages": []})
    task = client.task(thread, run["run_id"])
    assert task.state is t.TaskState.COMPLETED and task.context_id == "th1"
    assert task.artifacts[0].parts[0].text == "answer"
    assert task.to_dict()["status"]["state"] == "TASK_STATE_COMPLETED"
