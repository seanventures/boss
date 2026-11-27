"""Shared opaque-box fixtures for the BOSS Tier 1–4 Module E2E Suite.

Runs against real Postgres (`boss_test` on 127.0.0.1:55700, confined to a private
timestamped schema via `confine_to_private_schema`), real FastAPI (`BOSS_REQUIRE_API_AUTH=true`,
`BOSS_AUTHZ_MODE=enforce`), real CLI subprocesses, real Next.js console server, and local
HTTP/SSH fakes. Zero monkeypatching of LAST_MILE functions (`test_gate_tests_never_mock_the_last_mile.py`).
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT, ROOT / "engine", ROOT / "apps" / "worker"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ── Enforce production-like auth & DB confinement BEFORE importing apps.api.app.main ─────────
OWNER_API_KEY = "m6_e2e_owner_" + secrets.token_hex(16)
WORKER_API_KEY = "m6_e2e_worker_" + secrets.token_hex(16)
ORIGIN = "https://boss.example.test"

os.environ["BOSS_EDITION"] = "community"
os.environ["BOSS_REQUIRE_API_AUTH"] = "true"
os.environ["BOSS_AUTHZ_MODE"] = "enforce"
os.environ["BOSS_AUTHZ_ROUTER_MODES"] = ""
os.environ["BOSS_AUTONOMY_IMMEDIATE"] = "0"
os.environ["BOSS_INSTALL_TIER"] = "dev"
os.environ["BOSS_TELEMETRY"] = "off"
os.environ["BOSS_COMMUNITY_SECRET_STORE"] = "database"
os.environ["BOSS_IDENTITY_SERVER_KEY"] = (
    os.environ.get("BOSS_IDENTITY_SERVER_KEY")
    or "dGVzdC1vbmx5LWlkZW50aXR5LXNlcnZlci1rZXktLS0tLS0tLS0tLS0tLS0="
)
os.environ["BOSS_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["BOSS_API_KEY"] = OWNER_API_KEY
os.environ["BOSS_WORKER_API_KEY"] = WORKER_API_KEY
os.environ["BOSS_API_KEYS"] = f"{OWNER_API_KEY},{WORKER_API_KEY}"
os.environ["BOSS_API_KEY_PRINCIPALS"] = f"{OWNER_API_KEY}:ceo,{WORKER_API_KEY}:system"
os.environ["BOSS_WORKER_PRINCIPAL"] = "system"
os.environ["BOSS_PUBLIC_URL"] = ORIGIN
os.environ.setdefault("BOSS_SECRET_REF_ENV_ALLOW", "BOSS_TEST_*,BOSS_E2E_*")
os.environ.setdefault("PGOPTIONS", "-c statement_timeout=60000")


def _resolve_base_db_url() -> str:
    raw = (
        os.environ.get("BOSS_TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if not raw:
        pg = (os.environ.get("BOSS_TEST_PG_URL") or "").strip()
        if pg:
            parts = urllib.parse.urlsplit(pg)
            path = parts.path if parts.path and parts.path != "/" else "/boss_test"
            raw = urllib.parse.urlunsplit(parts._replace(path=path))
    if not raw:
        pw = os.environ.get("BOSS_DB_PASSWORD", "boss_local_test_pw")
        raw = f"postgresql+psycopg://boss:{urllib.parse.quote(pw, safe='')}@127.0.0.1:55700/boss_test"
    if raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
    return raw


from apps.api.tests.db_guard import assert_safe_to_wipe  # noqa: E402
from apps.api.tests.schema_confinement import confine_to_private_schema  # noqa: E402
from boss_engine.dev.test_databases import guard_test_url, session_start  # noqa: E402

_BASE_DATABASE_URL = _resolve_base_db_url()
os.environ["DATABASE_URL"] = _BASE_DATABASE_URL
os.environ["BOSS_TEST_DATABASE_URL"] = _BASE_DATABASE_URL
assert_safe_to_wipe(_BASE_DATABASE_URL)
guard_test_url(variable="DATABASE_URL")
_TEST_SCHEMA = confine_to_private_schema(_BASE_DATABASE_URL)

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from apps.api.app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from apps.api.app import main as api_main  # noqa: E402
from apps.api.app.bootstrap import seed  # noqa: E402
from apps.api.app.main import app  # noqa: E402
from apps.api.tests.identity_fixtures import IdentityWorld  # noqa: E402
from boss_engine.db import ensure_schema  # noqa: E402
from boss_engine.bootstrap import seed_project_defaults  # noqa: E402
from boss_engine.identity import api_keys, authn, claim, sessions, totp, vault  # noqa: E402
from boss_engine.identity.authn_support import utcnow  # noqa: E402
from boss_engine.identity.principal import Principal, PrincipalKind  # noqa: E402

os.environ["BOSS_EDITION"] = "community"
api_main.settings.boss_edition = "community"
api_main.settings.authz_mode = "enforce"
api_main.settings.authz_router_modes = ""
api_main.configure_rate_limiter_for_testing(5000)


def pytest_sessionstart(session):
    if _TEST_SCHEMA:
        for line in session_start(_BASE_DATABASE_URL):
            print(line)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def db_engine():
    if _TEST_SCHEMA is None:
        raise RuntimeError("Could not create private schema on test Postgres")
    eng = create_engine(os.environ["DATABASE_URL"], future=True)
    ensure_schema(eng)
    get_settings.cache_clear()
    with eng.begin() as db:
        vault.boot(db=db)
    seed()
    api_main.configure_rate_limiter_for_testing(5000)
    api_main.reset_rate_limiter_hits()
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def client(db_engine) -> TestClient:
    return TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)


@pytest.fixture(scope="session")
def owner_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_API_KEY}"}


@pytest.fixture(scope="session")
def worker_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {WORKER_API_KEY}"}


@dataclass
class MintedUser:
    principal: Principal
    role: str
    api_key: str
    headers: dict[str, str]
    auth: Optional[Any] = None


@dataclass
class IsolatedTenant:
    world: IdentityWorld
    project_id: str
    other_project_id: str
    owner: MintedUser
    operator: MintedUser
    viewer: MintedUser
    auditor: MintedUser
    service_account: MintedUser
    _engine: Any
    budget_worker: Optional[MintedUser] = None
    session_client: Any = None
    csrf_headers: dict[str, str] = None  # type: ignore[assignment]
    session_auth: Any = None

    def mint_pat(self, principal: Principal, *, scopes: Optional[tuple[str, ...]] = None,
                 project_ids: Optional[tuple[str, ...]] = None,
                 revoked: bool = False) -> str:
        raw_key = authn.mint_api_key("pat" if principal.kind == PrincipalKind.USER else "sak")
        token_hash = authn.hash_token(raw_key)
        cred_id = f"key_{uuid.uuid4().hex[:12]}"
        kind = "pat" if principal.kind == PrincipalKind.USER else "sak"
        with self._engine.begin() as db:
            if scopes is None:
                scope_list = sorted(api_keys.held_permissions(db, principal)) or ["project:read"]
            else:
                scope_list = list(scopes)
            db.execute(
                text(
                    "INSERT INTO identity_api_credentials "
                    "(id, principal_id, kind, name, token_hash, scopes, project_ids, created_by, revoked_at) "
                    "VALUES (:id, :p, :kind, :name, :h, :s, :pj, 'm6-test', "
                    "CASE WHEN :rev THEN now() ELSE NULL END)"
                ),
                {
                    "id": cred_id,
                    "p": principal.id,
                    "kind": kind,
                    "name": f"m6-{cred_id}",
                    "h": token_hash,
                    "s": scope_list,
                    "pj": list(project_ids) if project_ids is not None else None,
                    "rev": revoked,
                },
            )
        return raw_key

    def seed_project_defaults(self, project_id: Optional[str] = None) -> None:
        pid = project_id or self.project_id
        settings = get_settings()
        from apps.api.app.services.agent_studio import AgentStudioService
        from apps.api.app.services.entitlements import EntitlementBrokerService
        from apps.api.app.services.members import MemberDirectoryService
        from apps.api.app.services.skill_registry import seed_system_skills
        from apps.api.app.services.specpack import SpecPackService
        from apps.api.app.services.toolchain_registry import ToolchainRegistryService
        from apps.api.app.services.work_orchestration import WorkOrchestrationService

        ent = EntitlementBrokerService(settings)
        with self._engine.begin() as conn:
            seed_project_defaults(
                conn, settings, pid,
                entitlements=ent,
                members=MemberDirectoryService(settings),
                specpack=SpecPackService(settings),
            )
            seed_system_skills(conn, pid, ent.list_skills(), "m6-seed")
            ToolchainRegistryService(settings).seed_sources(conn, pid, "m6-seed")
            AgentStudioService(settings).seed_catalog(conn, pid, "m6-seed")
            WorkOrchestrationService(settings).seed_defaults(conn, pid, "m6-seed")
            # Ensure budget policy exists for isolated project
            conn.execute(
                text(
                    "INSERT INTO budget_policies "
                    "(id, scope_type, scope_id, daily_cap_usd_micros, monthly_cap_usd_micros, "
                    " burst_cap_usd_micros_per_min, max_concurrent_jobs, max_workers_total, "
                    " allowed_model_tiers, soft_cap_pct, auto_lite_mode_pct, "
                    " freeze_provisioning_pct, freeze_tier2_pct) "
                    "VALUES (:id, 'project', :p, 50000000, 500000000, 100000000, 3, 2, "
                    " '[\"gpt-5.2-codex\"]'::jsonb, 0.70, 0.85, 0.90, 0.95) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"id": f"bpol_{uuid.uuid4().hex[:10]}", "p": pid},
            )


@pytest.fixture
def tenant_factory(db_engine) -> Callable[..., IsolatedTenant]:
    """Create isolated org/workspace/projects + real identity_api_credentials (zero auth mocking)."""
    def _create(*, seed_defaults: bool = True) -> IsolatedTenant:
        world = IdentityWorld.create(db_engine)
        owner_p = world.person("m6-owner")
        world.bind(owner_p, "owner")
        op_p = world.person("m6-operator")
        world.bind(op_p, "operator", project=world.project_a)
        view_p = world.person("m6-viewer")
        world.bind(view_p, "viewer", project=world.project_a)
        aud_p = world.person("m6-auditor")
        world.bind(aud_p, "auditor")
        sa_p = world.principal("m6-svc", kind=PrincipalKind.SERVICE_ACCOUNT)
        # Service account has only AGENT_INVOKE via custom role
        sa_role = world.custom_role("m6-invoker", ("agent:invoke", "agent:read", "project:read"))
        world.bind(sa_p, sa_role, project=world.project_a)
        bw_p = world.principal("m6-budget-wrk", kind=PrincipalKind.SERVICE_ACCOUNT)
        world.bind(bw_p, "arl_system_worker")

        tenant = IsolatedTenant(
            world=world,
            project_id=world.project_a,
            other_project_id=world.project_b,
            owner=MintedUser(owner_p, "owner", "", {}),
            operator=MintedUser(op_p, "operator", "", {}),
            viewer=MintedUser(view_p, "viewer", "", {}),
            auditor=MintedUser(aud_p, "auditor", "", {}),
            service_account=MintedUser(sa_p, "service_account", "", {}),
            _engine=db_engine,
            budget_worker=MintedUser(bw_p, "system_worker", "", {}),
        )
        for u in (
            tenant.owner,
            tenant.operator,
            tenant.viewer,
            tenant.auditor,
            tenant.service_account,
            tenant.budget_worker,
        ):
            key = tenant.mint_pat(u.principal)
            u.api_key = key
            u.headers = {"Authorization": f"Bearer {key}"}
            with db_engine.begin() as conn:
                u.auth = authn.authenticate(
                    authn.RequestCredentials(
                        method="GET",
                        path="/v1/auth/session",
                        headers={"authorization": f"Bearer {key}"},
                        cookies={},
                    ),
                    db=conn,
                )
        with db_engine.begin() as conn:
            issued = sessions.issue(
                conn,
                principal_id=owner_p.id,
                org_id=owner_p.org_id,
                methods=["pwd", "totp"],
                mfa_at=utcnow(),
                ip="127.0.0.1",
                user_agent="m6-e2e",
                device_id_hash=None,
            )
            sess_auth = authn.authenticate(
                authn.RequestCredentials(
                    method="GET",
                    path="/v1/identity/me",
                    headers={},
                    cookies={authn.SESSION_COOKIE: issued.token},
                ),
                db=conn,
            )
        sess_client = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
        sess_client.cookies.set(authn.SESSION_COOKIE, issued.token)
        tenant.session_client = sess_client
        tenant.csrf_headers = {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "X-Boss-CSRF": sessions.csrf_token(issued.session_id),
        }
        tenant.session_auth = sess_auth
        if seed_defaults:
            tenant.seed_project_defaults(world.project_a)
        return tenant

    return _create


@dataclass
class ClaimedSession:
    client: TestClient
    csrf_headers: dict[str, str]
    totp_secret: str
    principal_id: str
    email: str
    _last_step: int = 0

    def step_up(self) -> None:
        cur = totp.current_step()
        candidate = max(cur - 1, self._last_step + 1)
        if candidate > cur + 1:
            time.sleep(max(0.5, 30.0 - (time.time() % 30.0) + 0.5))
            cur = totp.current_step()
            candidate = max(cur - 1, self._last_step + 1)
        code = totp.code_at(self.totp_secret, candidate)
        self._last_step = candidate
        res = self.client.post(
            "/v1/auth/step-up",
            headers=self.csrf_headers,
            json={"method": "totp", "code": code},
        )
        assert res.status_code == 200, f"step-up failed: {res.status_code} {res.text}"
        me = self.client.get("/v1/identity/me")
        assert me.status_code == 200, f"/v1/identity/me failed after step-up: {me.status_code} {me.text}"
        self.csrf_headers["X-Boss-CSRF"] = me.json()["csrf_token"]

    def age_mfa(self, db_engine: Any, minutes: int = 15) -> None:
        token = self.client.cookies.get(authn.SESSION_COOKIE) or ""
        h = authn.hash_token(token)
        with db_engine.begin() as db:
            db.execute(
                text("UPDATE identity_sessions SET mfa_at = now() - make_interval(mins => :m) WHERE token_hash = :h"),
                {"m": int(minutes), "h": h},
            )


@pytest.fixture(scope="session")
def claimed_owner_session(db_engine) -> ClaimedSession:
    """Real browser-like Owner session claimed via setup token + TOTP enrolment."""
    origin_headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    email = f"m6-owner-{uuid.uuid4().hex[:8]}@example.test"
    password = "M6-Owner-Pass-" + secrets.token_urlsafe(12)
    with db_engine.begin() as db:
        token = claim.issue_setup_token(db, force=True)
    assert token, "failed to issue setup token"
    sess_client = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
    r = sess_client.post(
        "/v1/auth/setup/claim",
        headers=origin_headers,
        json={"name": "M6 Owner", "email": email, "password": password, "setup_token": token},
    )
    assert r.status_code == 200, f"claim failed: {r.status_code} {r.text}"
    me = sess_client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    me_body = me.json()
    csrf_headers = {**origin_headers, "X-Boss-CSRF": me_body["csrf_token"]}
    begin_mfa = sess_client.post("/v1/identity/me/mfa/totp", headers=csrf_headers)
    assert begin_mfa.status_code == 200, begin_mfa.text
    secret = begin_mfa.json()["secret"]
    step0 = totp.current_step()
    confirm = sess_client.post(
        "/v1/identity/me/mfa/totp/confirm",
        headers=csrf_headers,
        json={"code": totp.code_at(secret, step0 - 1)},
    )
    assert confirm.status_code == 200, confirm.text
    me_after = sess_client.get("/v1/identity/me")
    assert me_after.status_code == 200, me_after.text
    csrf_headers["X-Boss-CSRF"] = me_after.json()["csrf_token"]
    return ClaimedSession(
        client=sess_client,
        csrf_headers=csrf_headers,
        totp_secret=secret,
        principal_id=me_body["principal_id"],
        email=email,
        _last_step=step0 - 1,
    )


@contextmanager
def start_local_http_server(
    handler_fn: Callable[[str, str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]],
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Spin up a real loopback HTTP server recording every request."""
    recorded: list[dict[str, Any]] = []
    lock = threading.Lock()

    class _Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            with lock:
                recorded.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": hdrs,
                    "body": body,
                })
            status, resp_headers, resp_body = handler_fn(self.command, self.path, hdrs, body)
            self.send_response(status)
            for k, v in resp_headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            if resp_body:
                self.wfile.write(resp_body)

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", recorded
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def http_fake_factory():
    return start_local_http_server


@pytest.fixture
def ssh_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real executable `ssh` binary on PATH that logs invocations (no function monkeypatching)."""
    bin_dir = tmp_path / "stub_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_file = tmp_path / "ssh_calls.jsonl"
    ssh_script = bin_dir / "ssh"
    ssh_script.write_text(
        f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
Path({str(log_file)!r}).open("a").write(json.dumps({{"argv": sys.argv[1:]}}) + "\\n")
remote_cmd = sys.argv[-1] if len(sys.argv) > 1 else ""
print(f"STUB_SSH_OK: {{remote_cmd}}")
""",
        encoding="utf-8",
    )
    ssh_script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    def read_calls() -> list[dict[str, Any]]:
        if not log_file.exists():
            return []
        return [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]

    return read_calls


@pytest.fixture(scope="session")
def live_api_url(db_engine) -> Iterator[str]:
    """Real uvicorn FastAPI server bound to an ephemeral loopback port sharing the confined DB."""
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.25)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("Live uvicorn API server failed to become healthy")
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def live_web_console(live_api_url: str) -> Iterator[dict[str, Any]]:
    """Real Next.js console server (`next dev` on loopback) wired to `live_api_url`."""
    web_dir = ROOT / "apps" / "web"
    next_bin = web_dir / "node_modules" / ".bin" / "next"
    if not next_bin.exists():
        raise RuntimeError(f"Next.js binary missing at {next_bin}; run pnpm install first")
    port = free_port()
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    env.update({
        "BOSS_API_URL": live_api_url,
        "NEXT_PUBLIC_BOSS_API_URL": live_api_url,
        "NEXT_PUBLIC_BOSS_PROJECT_ID": "boss-dogfood",
        "NEXT_TELEMETRY_DISABLED": "1",
    })
    proc = subprocess.Popen(
        [str(next_bin), "dev", "-p", str(port), "-H", "127.0.0.1"],
        cwd=str(web_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    web_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"Next.js dev server exited early ({proc.returncode}): {out[-1000:]}")
        try:
            r = httpx.get(f"{web_url}/login", timeout=5.0)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError("Next.js dev server did not respond 200 on /login within 90s")
    try:
        yield {"web_url": web_url, "api_url": live_api_url}
    finally:
        import signal as _signal

        try:
            os.killpg(proc.pid, _signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(proc.pid, _signal.SIGKILL)
            except Exception:
                pass
