"""P8b-9 (BOSSAPP-1076): the replica's TLS listener on the process backend — P8b-T1 locally
(boss-agent-hosts-lld.md §3.13.2, §3.17.2, §3.17.3; explained.md Flow 2 ⑤-⑥).

A real TLS listener (runtime/listener.py) for a replica of a deployed agent, its certificate from
the agent CA for a key generated in the "VM", its run plane through a fake boss-hostd's guest
gateway (host proof, X-Boss-Replica from the "socket", run tokens kept in host memory), against
the real fleet API on a real port and Postgres:

  * `curl --cacert agent-ca https://<VIP>`: the Agent Protocol (threads, runs, SSE on the same
    connection, wait) and A2A (the signed card, SendMessage, SendStreamingMessage, GetTask);
  * a caller with no credential, a forged key, an unknown key, a key without agent:invoke, a
    client certificate from another CA, or a revoked one is refused; mTLS alone works; a token
    and a certificate naming different callers are refused;
  * EVERY request goes to admit (the PDP), never decided in the VM;
  * the serving certificate rotates without dropping an open connection (P8b-T4, locally);
  * the connection limit closes the excess at accept;
  * the same handlers serve in-process (InProcessBackend): the same Task for the same run.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import tempfile
import threading
import time
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, text

from boss_engine.kernel.scripted import Say, ScriptedEngine

from ._fleet_support import FakeHost, cleanup_project, fleet_app, join_token, use_test_master_key

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True, pool_size=20, max_overflow=20)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM agent_ca LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable: {error}")
    return engine


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def api(eng):
    """The fleet API on a real port (the runtime is another 'machine')."""
    import uvicorn
    from fastapi import FastAPI

    from apps.api.app.routers.a2a import build_a2a_router
    from apps.api.app.routers.fleet import build_fleet_router

    app = FastAPI()
    app.include_router(build_fleet_router(engine=eng))
    app.include_router(build_a2a_router(engine=eng, require_write_actor=lambda *a: "ceo"))
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


def _gateway(host: FakeHost, base_url: str, replica_id: str, admits: list):
    """boss-hostd's guest gateway for one VM: signs as the host, sets X-Boss-Replica from the
    socket, attaches run tokens from host memory and strips them from admit answers."""
    from boss_engine.runtime.client import HttpTransport, RunPlaneClient

    http = httpx.Client(base_url=base_url, timeout=60.0)

    class Gateway(HttpTransport):
        def request(self, method, path, *, json=None, params=None, headers=None):
            sent = {k: v for k, v in (headers or {}).items()
                    if k.lower() not in ("x-boss-replica", "x-boss-run-token", "authorization")}
            sent["X-Boss-Replica"] = replica_id
            parts = path.split("/")
            if len(parts) > 4 and parts[3] == "runs" and parts[4] in host.tokens:
                sent["X-Boss-Run-Token"] = host.tokens[parts[4]]
            if path.endswith("/run-plane/admit"):
                admits.append(dict(json or {}).get("op"))
            out = super().request(method, path, json=json, params=params, headers=sent)
            lease = out.get("lease") if isinstance(out, dict) else None
            if isinstance(lease, dict) and lease.get("run_token"):
                host.tokens[str(out["run_id"])] = lease.pop("run_token")
            return out

    return RunPlaneClient(Gateway(http, signer=host.signer()), replica_id)


class Agent:
    """A read-only agent deployed to prod on two process-backend hosts; host A's replica runs a
    listener on 127.0.0.1 (= the deployment's VIP for this test)."""

    def __init__(self, eng, api: str, monkeypatch, *, max_connections: int = 64):
        from fastapi.testclient import TestClient

        from apps.api.tests.identity_fixtures import IdentityWorld
        from boss_engine.agents import versions
        from boss_engine.identity.principal import PrincipalKind
        from boss_engine.runtime import runner as rt
        from boss_engine.runtime.listener import AgentListener, ServingIdentity, TrustCache
        from boss_engine.runtime.serving import RunPlaneBackend
        from boss_engine.services import (a2a_server, agent_ca, agent_deployments, agent_keys,
                                          fleet_scheduler)

        use_test_master_key(monkeypatch)
        agent_ca.reset_cache()
        monkeypatch.setattr(rt, "ENGINE_FACTORY",
                            lambda package, handle: ScriptedEngine([Say("pong")]))
        self.eng = eng
        world = IdentityWorld.create(eng)
        world.claimed()
        self.world, self.project, self.org = world, world.project_a, world.org
        self.key = f"helper_{uuid.uuid4().hex[:6]}"
        self.pool = f"t_{uuid.uuid4().hex[:10]}"
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO project_members (id, project_id, member_id, display_name, "
                "member_type, role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', "
                "'[\"ceo\"]'::jsonb, '[]'::jsonb) ON CONFLICT DO NOTHING"),
                {"i": f"pm_{self.project}_ceo", "p": self.project})
            versions.create_from_spec(c, self.project, {
                "apiVersion": "boss.agent/v1", "kind": "Agent",
                "metadata": {"key": self.key, "name": "Helper"},
                "spec": {"mission": "Answer questions.", "prompts": {"system": "v1"},
                         "limits": {"max_steps": 4}}}, actor="ceo", source="operator")
            agent_keys.create_key(c, self.project, self.key, actor="ceo")
            agent_deployments.deploy(c, self.project, self.key, "prod", actor="ceo", version=1)
            self.dep = agent_deployments.set_runtime(
                c, self.project, self.key, "prod", actor="ceo", runtime="microvm", replicas=2,
                placement={"pool": self.pool})
            a2a_server.set_exposure(c, self.project, self.key, "prod", enabled=True,
                                    actor="ceo")
            c.execute(text("UPDATE agent_deployments SET vip = '127.0.0.1' WHERE id = :d"),
                      {"d": self.dep["id"]})
            self.agent_id = c.execute(text("SELECT agent_id FROM roles WHERE project_id = :p "
                                           "AND role_key = :r"),
                                      {"p": self.project, "r": self.key}).scalar()
        tc = TestClient(fleet_app(eng))
        self.hosts = []
        for _ in range(2):
            host = FakeHost(tc)
            host.enrol(join_token(eng, self.org, pool=self.pool))
            self.hosts.append(host)
        fleet_scheduler.reconcile(eng)
        for host in self.hosts:
            host.serve_all()
            assert host.heartbeat().status_code == 200
        self.host = self.hosts[0]
        (self.replica_id,) = list(self.host.vms)
        # callers: an invoker and a reader service account, with REAL keys
        self.invoker = world.principal("Portal", kind=PrincipalKind.SERVICE_ACCOUNT)
        world.bind(self.invoker, world.custom_role(f"inv_{uuid.uuid4().hex[:6]}",
                                                   ("agent:invoke",)), project=self.project)
        self.reader = world.principal("Reader", kind=PrincipalKind.SERVICE_ACCOUNT)
        world.bind(self.reader, world.custom_role(f"rd_{uuid.uuid4().hex[:6]}",
                                                  ("agent:read",)), project=self.project)
        self.invoker_key = self._key(self.invoker, ["agent:invoke"])
        self.reader_key = self._key(self.reader, ["agent:read"])
        # the VM: its run plane through the host, its key, its certificate, its listener
        self.admits: list = []
        self.rp = _gateway(self.host, api, self.replica_id, self.admits)
        self.identity = ServingIdentity(self.rp)
        self.issued = self.identity.obtain()
        self.trust = TrustCache(self.rp)
        self.backend = RunPlaneBackend(self.rp, card=self.trust.card)
        self.listener = AgentListener(("127.0.0.1", 0), backend=self.backend, trust=self.trust,
                                      identity=self.identity, max_connections=max_connections)
        threading.Thread(target=self.listener.serve_forever, daemon=True).start()
        self.base = f"https://127.0.0.1:{self.listener.server_address[1]}"
        self.cacert = self._file(self.issued["chain_pem"])

    def _key(self, principal, scopes) -> str:
        from boss_engine.identity import authn

        raw = authn.mint_api_key("sak")
        with self.eng.begin() as c:
            c.execute(text(
                "INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, "
                "scopes, created_by) VALUES (:i, :p, 'sak', 'test', :h, :s, 'test')"),
                {"i": f"key_{uuid.uuid4().hex[:16]}", "p": principal.id,
                 "h": authn.hash_token(raw), "s": scopes})
        return raw

    def _file(self, content: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        return path

    def http(self, *, cert=None, **kw) -> httpx.Client:
        """`curl --cacert agent-ca`: trusts ONLY the agent CA bundle."""
        context = ssl.create_default_context(cafile=self.cacert)
        if cert:
            context.load_cert_chain(*cert)
        return httpx.Client(base_url=self.base, verify=context, timeout=30.0, **kw)

    def bearer(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}"}

    def caller_cert(self, principal) -> tuple:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        from boss_engine.services import agent_ca
        from boss_engine.services.fleet_ca import make_csr

        key = ec.generate_private_key(ec.SECP256R1())
        with self.eng.begin() as c:
            issued = agent_ca.issue_caller_cert(c, org_id=self.org, principal_id=principal.id,
                                                csr_pem=make_csr(key, "caller"), days=1)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        return (self._file(issued["certificate_pem"]), self._file(pem)), issued["serial"]

    def close(self) -> None:
        self.listener.shutdown()
        self.listener.server_close()
        cleanup_project(self.eng, self.project, [h.host_id for h in self.hosts if h.host_id])


@pytest.fixture()
def agent(eng, api, monkeypatch):
    a = Agent(eng, api, monkeypatch)
    yield a
    a.close()


def _sse(text_: str) -> list:
    events = []
    for block in text_.strip().split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads("".join(data)))
    return events


def test_p8b_t1_agent_protocol_over_tls_with_the_agent_ca(agent):
    with agent.http() as http:
        # TLS ends in the "VM" with the agent's own certificate, trusted via the agent CA only
        thread = http.post("/v1/agents/x/threads", json={}, headers=agent.bearer(
            agent.invoker_key))
        assert thread.status_code == 201, thread.text
        tid = thread.json()["thread_id"]
        streamed = http.post(f"/v1/threads/{tid}/runs/stream", json={"input": "ping"},
                             headers=agent.bearer(agent.invoker_key))
        assert streamed.status_code == 200, streamed.text
        assert streamed.headers["content-type"].startswith("text/event-stream")
        events = _sse(streamed.text)
        kinds = [e["type"] for e in events]
        assert kinds[0] == "metadata" and kinds[-1] == "end" and "updates" in kinds, kinds
        assert events[-1]["data"]["status"] == "success", events[-1]
        assert any(e["type"] == "messages" and e["data"]["content"] == "pong" for e in events)
        run_id = events[0]["data"]["run_id"]
        got = http.get(f"/v1/threads/{tid}/runs/{run_id}", headers=agent.bearer(
            agent.invoker_key)).json()
        assert got["status"] == "success" and got["output"] == "pong"
        waited = http.post("/v1/agents/x/runs/wait", json={"input": "again"},
                           headers=agent.bearer(agent.invoker_key)).json()
        assert waited["status"] == "success" and waited["output"] == "pong", waited
    assert run_id in agent.host.tokens, "leased to THIS replica: its host holds the run token"
    assert "read" in agent.admits and "invoke" in agent.admits and "thread" in agent.admits


def test_p8b_t1_a2a_over_tls_card_send_stream_get(agent):
    from boss_engine.protocols import agent_card

    with agent.http() as http:
        card = http.get("/.well-known/agent-card.json").json()
        with agent.eng.begin() as c:
            from boss_engine.services import agent_keys

            jwks = agent_keys.jwks_for_agent(c, agent.agent_id)
        assert agent_card.verify_card(card, jwks)["alg"] == "EdDSA"
        assert card["bossAgentCa"]["url"].endswith("/v1/pki/agent-ca")
        assert any(i["url"].startswith("https://") and i["url"].endswith("/a2a")
                   for i in card["supportedInterfaces"])
        sent = http.post("/a2a", headers={**agent.bearer(agent.invoker_key),
                                          "A2A-Version": "1.0"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
                             "message": {"role": "ROLE_USER", "messageId": "m1",
                                         "parts": [{"text": "ping"}]}}}).json()
        task = sent["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED", sent
        assert task["artifacts"][0]["parts"][0]["text"] == "pong"
        got = http.post("/a2a", headers=agent.bearer(agent.invoker_key), json={
            "jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": task["id"]}}).json()
        assert got["result"]["id"] == task["id"]
        streamed = http.post("/a2a", headers=agent.bearer(agent.invoker_key), json={
            "jsonrpc": "2.0", "id": 3, "method": "SendStreamingMessage", "params": {
                "message": {"role": "ROLE_USER", "messageId": "m2",
                            "parts": [{"text": "ping"}]}}})
        frames = _sse(streamed.text)
        assert frames and all(f["id"] == 3 for f in frames), streamed.text
        last = frames[-1]["result"]
        state = (last.get("statusUpdate") or last.get("task") or {}).get("status", {}).get("state")
        assert state == "TASK_STATE_COMPLETED", frames


def test_a_caller_is_refused_without_a_valid_credential_and_admit_runs_the_pdp(agent):
    from boss_engine.identity import authn

    forged = agent.invoker_key[:-1] + ("A" if agent.invoker_key[-1] != "A" else "B")
    with agent.http() as http:
        before = len(agent.admits)
        nothing = http.post("/v1/agents/x/threads", json={})
        assert nothing.status_code == 401
        bad = http.post("/v1/agents/x/threads", json={}, headers=agent.bearer(forged))
        assert bad.status_code == 401 and bad.json()["detail"]["error"] == "invalid_credentials"
        jwt = http.post("/v1/agents/x/threads", json={},
                        headers=agent.bearer("eyJhbGciOiJFZERTQSJ9.eyJ4IjoxfQ.c2ln"))
        assert jwt.status_code == 401
        assert len(agent.admits) == before, "refused locally: BOSS was never asked"
        unknown = http.post("/v1/agents/x/threads", json={},
                            headers=agent.bearer(authn.mint_api_key("sak")))
        assert unknown.status_code == 401
        assert unknown.json()["detail"]["error"] == "caller_unauthenticated"
        reader = http.post("/v1/agents/x/threads", json={}, headers=agent.bearer(
            agent.reader_key))
        assert reader.status_code == 403 and reader.json()["detail"]["error"] == \
            "caller_forbidden", reader.text
        # reads are authorised by BOSS too: a reader cannot read a run either
        assert http.post("/a2a", headers=agent.bearer(agent.reader_key), json={
            "jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "arun_x"}}
        ).json()["error"]["code"] != 0
    with agent.eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM agent_threads WHERE project_id = :p"),
                         {"p": agent.project}).scalar() == 0, "nothing refused wrote anything"


def test_mtls_callers_are_verified_locally_and_by_boss(agent):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    import datetime as dt

    from boss_engine.services import agent_ca

    files, serial = agent.caller_cert(agent.invoker)
    with agent.http(cert=files) as http:
        ok = http.post("/v1/agents/x/threads", json={})
        assert ok.status_code == 201, ok.text
        mixed = http.post("/v1/agents/x/threads", json={},
                          headers=agent.bearer(agent.reader_key))
        assert mixed.status_code == 401, "a token and a certificate naming different callers"
    # a certificate from a CA the listener does not trust: the handshake itself fails
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, agent.invoker.id)])
    now = dt.datetime.now(dt.timezone.utc)
    rogue = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
             .public_key(key.public_key()).serial_number(7).not_valid_before(now)
             .not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256()))
    rogue_files = (agent._file(rogue.public_bytes(serialization.Encoding.PEM).decode()),
                   agent._file(key.private_bytes(serialization.Encoding.PEM,
                                                 serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()).decode()))
    with agent.http(cert=rogue_files) as http:
        with pytest.raises((httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
            http.post("/v1/agents/x/threads", json={}).raise_for_status()
    # revoked: refused (BOSS at admit, and the listener once its trust cache refreshes)
    with agent.eng.begin() as c:
        agent_ca.revoke(c, serial=serial, reason="test")
    with agent.http(cert=files) as http:
        assert http.post("/v1/agents/x/threads", json={}).status_code == 401
        agent.trust.doc(refresh=True)
        refused = http.post("/v1/agents/x/threads", json={})
        assert refused.status_code == 401
        assert refused.json()["detail"]["error"] == "client_certificate_revoked"


def test_the_serving_cert_rotates_without_dropping_an_open_connection(agent):
    def serial_seen() -> int:
        context = ssl.create_default_context(cafile=agent.cacert)
        with socket.create_connection(("127.0.0.1", agent.listener.server_address[1])) as raw:
            with context.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                from cryptography import x509

                return x509.load_der_x509_certificate(tls.getpeercert(True)).serial_number

    first = serial_seen()
    with agent.http() as http:
        assert http.post("/v1/agents/x/threads", json={}, headers=agent.bearer(
            agent.invoker_key)).status_code == 201
        agent.identity.obtain()          # renewal: a NEW key, a new certificate
        again = http.post("/v1/agents/x/threads", json={}, headers=agent.bearer(
            agent.invoker_key))          # the same keep-alive connection
        assert again.status_code == 201
    second = serial_seen()
    assert first != second and format(second, "x") == agent.identity.serial


def test_the_connection_limit_closes_the_excess_at_accept(eng, api, monkeypatch):
    small = Agent(eng, api, monkeypatch, max_connections=2)
    try:
        port = small.listener.server_address[1]
        held = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
        time.sleep(0.3)
        context = ssl.create_default_context(cafile=small.cacert)
        with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
                with context.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                    tls.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                    if not tls.recv(1):
                        raise ConnectionError("closed")
        assert small.listener.refused_connections >= 1
        for s in held:
            s.close()
    finally:
        small.close()


def test_the_same_handlers_serve_in_process(agent):
    """The shared library over the control plane's own database gives the same Task."""
    from boss_engine.protocols.agent_serving import (Caller, InProcessBackend, ServingRequest,
                                                     dispatch, task_from_view)
    from boss_engine.services import a2a_server

    with agent.http() as http:
        sent = http.post("/a2a", headers=agent.bearer(agent.invoker_key), json={
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
                "message": {"role": "ROLE_USER", "messageId": "m1",
                            "parts": [{"text": "ping"}]}}}).json()
    task_id = sent["result"]["task"]["id"]
    with agent.eng.begin() as c:
        resolved = a2a_server.resolve(c, agent.agent_id)
    backend = InProcessBackend(agent.eng, resolved, authorize=lambda caller: None)
    out = dispatch(backend, ServingRequest("POST", "/a2a", body=json.dumps({
        "jsonrpc": "2.0", "id": 9, "method": "GetTask", "params": {"id": task_id}}).encode()),
        Caller(authorization="Bearer x"))
    assert out.status == 200 and out.body["result"]["id"] == task_id
    in_vm = sent["result"]["task"]
    assert out.body["result"]["status"]["state"] == in_vm["status"]["state"]
    assert out.body["result"]["artifacts"] == in_vm["artifacts"]
    with agent.eng.begin() as c:
        shared = task_from_view(a2a_server.view_for_run(c, resolved, task_id)).to_dict()
        api = a2a_server.task_for_run(c, agent.project, task_id).to_dict()
    for task in (shared, api):
        task["status"]["message"].pop("messageId")  # fresh per rendering
    assert shared == api


# ── review fixes: Content-Length, JWT claims, the kid refresh limit, the caller bundle ───────

def test_a_negative_or_bogus_content_length_is_400_before_anything_is_read(agent):
    port = agent.listener.server_address[1]
    context = ssl.create_default_context(cafile=agent.cacert)
    for value in ("-1", "abc", "+5"):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                tls.sendall(f"POST /threads HTTP/1.1\r\nHost: x\r\nContent-Length: {value}\r\n"
                            f"Authorization: Bearer {agent.invoker_key}\r\n\r\n".encode())
                head = tls.recv(200).decode()
        assert head.startswith("HTTP/1.1 400"), head


class _FakeTrustClient:
    def __init__(self, doc):
        self.doc, self.calls = doc, 0

    def trust(self):
        self.calls += 1
        return self.doc


def _jwt(key, kid: str, **claims) -> str:
    from boss_engine.identity import jwks

    return jwks.sign({"alg": "EdDSA", "typ": "boss-run+jwt", "kid": kid}, claims, key)


def test_local_jwt_checks_cover_iss_aud_nbf_and_unknown_kids_are_rate_limited():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from boss_engine.identity import jwks
    from boss_engine.runtime.listener import CallerRefused, TrustCache

    key = Ed25519PrivateKey.generate()
    doc = {"jwks": {"keys": [jwks.public_jwk(key.public_key(), kid="k1")]},
           "issuer": "https://boss.example", "audiences": ["boss:api"]}
    client = _FakeTrustClient(doc)
    trust = TrustCache(client)
    now = int(time.time())
    good = dict(iss="https://boss.example", aud="boss:api", exp=now + 60, nbf=now - 5)
    trust.verify_bearer(_jwt(key, "k1", **good))
    for bad in ({"iss": "https://evil"}, {"aud": "boss:mcp"}, {"nbf": now + 600},
                {"exp": now - 600}, {"aud": ["boss:egress"]}):
        with pytest.raises(CallerRefused):
            trust.verify_bearer(_jwt(key, "k1", **{**good, **bad}))
    before = client.calls
    for _ in range(20):
        with pytest.raises(CallerRefused):
            trust.verify_bearer(_jwt(key, f"forged-{uuid.uuid4().hex[:6]}", **good))
    assert client.calls - before <= 1, "an unknown kid re-fetches at most once per window"


def test_the_caller_bundle_is_retried_and_loaded_into_the_live_context():
    from boss_engine.runtime.listener import AgentListener, ServingIdentity

    class Trust:
        def __init__(self):
            self.n = 0

        def doc(self, refresh=False):
            self.n += 1
            if self.n == 1:
                raise ConnectionError("run plane not reachable yet")
            return {"caller_bundle_pem": BUNDLE}

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import datetime as dt

    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "caller CA")])
    now = dt.datetime.now(dt.timezone.utc)
    BUNDLE = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(k.public_key()).serial_number(3).not_valid_before(now)
              .not_valid_after(now + dt.timedelta(days=1))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .sign(k, hashes.SHA256())).public_bytes(serialization.Encoding.PEM).decode()
    listener = AgentListener(("127.0.0.1", 0), backend=None, trust=Trust(),
                             identity=ServingIdentity(None))
    try:
        assert listener.caller_bundle == "" and listener.ssl_context.verify_mode == ssl.CERT_NONE
        assert listener.reload_caller_bundle() is True
        assert listener.ssl_context.verify_mode == ssl.CERT_OPTIONAL
        assert listener.reload_caller_bundle() is False, "unchanged: nothing reloaded"
    finally:
        listener.server_close()
