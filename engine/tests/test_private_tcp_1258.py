"""A build reaches an approved private TCP service through a customer connector (BOSSAPP-1258).

The dial is a real socket to a listener this test binds on 127.0.0.1 and a high port. It never
dials a public host. Link-local addresses and cloud metadata addresses are refused before any
socket, even when a row claims they were approved.
"""

from __future__ import annotations

import os
import socket
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import private_tcp
from boss_engine.services.private_tcp import (
    CustomerConnector,
    PrivateTcpFailed,
    PrivateTcpRefused,
)


ROOT = Path(__file__).resolve().parents[2]


class _Listener:
    """One echo: read a line, answer ``pong``. Counts accepted connections."""

    def __init__(self) -> None:
        self.connections = 0
        self._stop = False
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        assert self.port > 1024
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop:
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections += 1
            try:
                conn.settimeout(1)
                buf = b""
                while b"\n" not in buf and len(buf) < 2048:
                    chunk = conn.recv(2048)
                    if not chunk:
                        break
                    buf += chunk
                conn.sendall(b"pong\n")
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1)


@pytest.fixture
def listener():
    ear = _Listener()
    try:
        yield ear
    finally:
        ear.close()


def _forbid_dial(monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise AssertionError("dialed")

    monkeypatch.setattr(private_tcp, "_connect", explode)
    monkeypatch.setattr(private_tcp.socket, "getaddrinfo", explode)


def _connector() -> CustomerConnector:
    return CustomerConnector(project_id=f"proj_{uuid.uuid4().hex[:8]}", name="billing")


def test_a_build_reaches_an_approved_service_on_loopback(listener):
    connector = _connector()
    connector.approve("127.0.0.1", listener.port)
    result = connector.reach(
        build_id="build_1",
        host="127.0.0.1",
        port=listener.port,
        payload=b"ping\n",
        dedupe_key="build_1:ping",
    )
    assert result["reply"] == "pong\n"
    assert result["dialed"] is True
    assert result["repeated"] is False
    assert result["address"] == "127.0.0.1"
    assert result["status"] == "reached"
    assert listener.connections == 1


def test_an_unapproved_host_is_not_dialed(listener, monkeypatch):
    _forbid_dial(monkeypatch)
    connector = _connector()
    with pytest.raises(PrivateTcpRefused) as exc:
        connector.reach(
            build_id="build_1",
            host="127.0.0.1",
            port=listener.port,
            payload=b"ping\n",
            dedupe_key="build_1:no",
        )
    assert exc.value.reason == "unapproved"
    assert "127.0.0.1" in str(exc.value)
    assert listener.connections == 0


def test_link_local_is_refused_before_a_socket(monkeypatch):
    _forbid_dial(monkeypatch)
    connector = _connector()
    with pytest.raises(PrivateTcpRefused) as exc:
        connector.approve("169.254.1.1", 9)
    assert exc.value.reason == "link_local"
    with pytest.raises(PrivateTcpRefused) as exc:
        connector.approve("fe80::1", 9)
    assert exc.value.reason == "link_local"
    with pytest.raises(PrivateTcpRefused) as exc:
        connector.reach(
            build_id="build_1",
            host="169.254.1.1",
            port=9,
            payload=b"ping\n",
            dedupe_key="build_1:link",
        )
    assert exc.value.reason == "link_local"
    assert connector.approved_services() == []


def test_cloud_metadata_is_refused_before_a_lookup(monkeypatch):
    _forbid_dial(monkeypatch)
    connector = _connector()
    for host in (
        "169.254.169.254",
        "metadata.google.internal",
        "metadata",
        "fd00:ec2::254",
        "100.100.100.200",
        "::ffff:169.254.169.254",
    ):
        with pytest.raises(PrivateTcpRefused) as exc:
            connector.reach(
                build_id="build_1",
                host=host,
                port=80,
                payload=b"ping\n",
                dedupe_key=f"build_1:{host}",
            )
        assert exc.value.reason == "metadata", host


def test_a_public_address_cannot_be_approved_or_dialed(monkeypatch):
    """192.0.2.1 is TEST-NET-1 (RFC 5737). The refusal must happen before any socket."""
    _forbid_dial(monkeypatch)
    connector = _connector()
    with pytest.raises(PrivateTcpRefused) as exc:
        connector.approve("192.0.2.1", 80)
    assert exc.value.reason == "public"
    with pytest.raises(PrivateTcpRefused) as exc:
        connector.reach(
            build_id="build_1",
            host="192.0.2.1",
            port=80,
            payload=b"ping\n",
            dedupe_key="build_1:public",
        )
    assert exc.value.reason == "public"


def test_an_unapproved_name_is_refused_before_dns(monkeypatch):
    _forbid_dial(monkeypatch)
    connector = _connector()
    with pytest.raises(PrivateTcpRefused) as exc:
        connector.reach(
            build_id="build_1",
            host="db.internal",
            port=5432,
            payload=b"ping\n",
            dedupe_key="build_1:name",
        )
    assert exc.value.reason == "unapproved"


def test_an_approved_name_is_dialed_only_at_the_address_it_resolved_to(listener, monkeypatch):
    calls = {"names": 0}

    def once(host, port, *args, **kwargs):
        if host == "127.0.0.1":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        assert host == "db.internal"
        calls["names"] += 1
        if calls["names"] > 1:
            raise AssertionError("resolved the name twice")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(private_tcp.socket, "getaddrinfo", once)
    connector = _connector()
    connector.approve("db.internal", listener.port)
    result = connector.reach(
        build_id="build_1",
        host="db.internal",
        port=listener.port,
        payload=b"ping\n",
        dedupe_key="build_1:name",
    )
    assert result["reply"] == "pong\n"
    assert result["address"] == "127.0.0.1"
    assert listener.connections == 1
    assert calls["names"] == 1


def test_an_approved_name_that_resolves_to_metadata_is_not_dialed(monkeypatch):
    def to_metadata(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(private_tcp.socket, "getaddrinfo", to_metadata)
    monkeypatch.setattr(
        private_tcp, "_connect", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("dialed"))
    )
    connector = _connector()
    connector.approve("db.internal", 80)
    with pytest.raises(PrivateTcpRefused) as exc:
        connector.reach(
            build_id="build_1",
            host="db.internal",
            port=80,
            payload=b"ping\n",
            dedupe_key="build_1:rebind",
        )
    assert exc.value.reason == "metadata"


def test_the_same_request_does_not_dial_twice(listener):
    connector = _connector()
    connector.approve("127.0.0.1", listener.port)
    first = connector.reach(
        build_id="build_1",
        host="127.0.0.1",
        port=listener.port,
        payload=b"ping\n",
        dedupe_key="build_1:once",
    )
    second = connector.reach(
        build_id="build_1",
        host="127.0.0.1",
        port=listener.port,
        payload=b"ping\n",
        dedupe_key="build_1:once",
    )
    assert first["reply"] == "pong\n"
    assert second["reply"] == "pong\n"
    assert second["repeated"] is True
    assert second["dialed"] is False
    assert listener.connections == 1


def test_a_service_that_does_not_answer_is_a_visible_error(monkeypatch):
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    calls = {"n": 0}
    real = private_tcp._connect

    def counting(address, port_, timeout):
        calls["n"] += 1
        return real(address, port_, timeout)

    monkeypatch.setattr(private_tcp, "_connect", counting)
    connector = _connector()
    connector.approve("127.0.0.1", port)
    with pytest.raises(PrivateTcpFailed) as exc:
        connector.reach(
            build_id="build_1",
            host="127.0.0.1",
            port=port,
            payload=b"ping\n",
            dedupe_key="build_1:down",
        )
    assert "127.0.0.1" in str(exc.value)
    assert str(port) in str(exc.value)
    assert exc.value.reason == "unreachable"
    record = connector.dial_record("build_1:down")
    assert record["status"] == "failed"
    assert record["error"]
    with pytest.raises(PrivateTcpFailed) as again:
        connector.reach(
            build_id="build_1",
            host="127.0.0.1",
            port=port,
            payload=b"ping\n",
            dedupe_key="build_1:down",
        )
    assert str(again.value) == str(exc.value)
    assert calls["n"] == 1


def _schema_sql() -> str:
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    marker = "-- BOSSAPP-1258:"
    assert marker in sql
    return sql[sql.index(marker):]


def _statements(sql: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            statement = "\n".join(buf).strip()
            if statement:
                parts.append(statement)
            buf = []
    return parts


@pytest.fixture(scope="module")
def postgres():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        pytest.skip("DATABASE_URL is not set")
    import psycopg

    raw = url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(raw, autocommit=True, connect_timeout=3) as conn:
            for statement in _statements(_schema_sql()):
                conn.execute(statement)
    except Exception as exc:
        pytest.skip(f"Postgres is not available: {exc}")
    engine = create_engine(url, future=True)
    yield engine
    engine.dispose()


def test_schema_declares_the_connector_tables():
    block = _schema_sql()
    assert "CREATE TABLE IF NOT EXISTS private_tcp_connectors" in block
    assert "CREATE TABLE IF NOT EXISTS private_tcp_approvals" in block
    assert "CREATE TABLE IF NOT EXISTS private_tcp_dials" in block
    assert "dedupe_key" in block


def test_a_saved_approval_is_reserved_before_the_dial_and_not_repeated(listener, postgres, monkeypatch):
    project_id = f"proj_{uuid.uuid4().hex[:10]}"
    created = private_tcp.open_connector(project_id=project_id, name="billing")
    private_tcp.approve_saved(
        project_id=project_id, connector_id=created["id"], host="127.0.0.1", port=listener.port
    )
    real = private_tcp._connect

    def reserved_first(address, port, timeout):
        row = private_tcp.get_dial(project_id, "build_db:ping")
        assert row is not None
        assert row["status"] == "reserved"
        assert row["address"] == "127.0.0.1"
        return real(address, port, timeout)

    monkeypatch.setattr(private_tcp, "_connect", reserved_first)
    first = private_tcp.dial_through(
        project_id=project_id,
        connector_id=created["id"],
        build_id="build_db",
        host="127.0.0.1",
        port=listener.port,
        payload=b"ping\n",
        dedupe_key="build_db:ping",
    )
    second = private_tcp.dial_through(
        project_id=project_id,
        connector_id=created["id"],
        build_id="build_db",
        host="127.0.0.1",
        port=listener.port,
        payload=b"ping\n",
        dedupe_key="build_db:ping",
    )
    assert first["reply"] == "pong\n"
    assert first["dialed"] is True
    assert second["repeated"] is True
    assert second["dialed"] is False
    assert listener.connections == 1
    stored = private_tcp.list_dials(project_id, created["id"])
    assert len([row for row in stored if row["dedupe_key"] == "build_db:ping"]) == 1
    assert stored[0]["status"] == "reached"


def test_a_forged_metadata_approval_is_still_refused(postgres, monkeypatch):
    _forbid_dial(monkeypatch)
    project_id = f"proj_{uuid.uuid4().hex[:10]}"
    created = private_tcp.open_connector(project_id=project_id, name="billing")
    with postgres.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO private_tcp_approvals (id, connector_id, host, port)
                VALUES (:id, :connector_id, '169.254.169.254', 80)
                """
            ),
            {"id": f"ptap_{uuid.uuid4().hex[:8]}", "connector_id": created["id"]},
        )
    with pytest.raises(PrivateTcpRefused) as exc:
        private_tcp.dial_through(
            project_id=project_id,
            connector_id=created["id"],
            build_id="build_db",
            host="169.254.169.254",
            port=80,
            payload=b"ping\n",
            dedupe_key="build_db:meta",
        )
    assert exc.value.reason == "metadata"


def test_another_projects_connector_is_not_dialed(listener, postgres, monkeypatch):
    _forbid_dial(monkeypatch)
    project_id = f"proj_{uuid.uuid4().hex[:10]}"
    created = private_tcp.open_connector(project_id=project_id, name="billing")
    private_tcp.approve_saved(
        project_id=project_id, connector_id=created["id"], host="127.0.0.1", port=listener.port
    )
    with pytest.raises(PrivateTcpRefused) as exc:
        private_tcp.dial_through(
            project_id=f"proj_{uuid.uuid4().hex[:10]}",
            connector_id=created["id"],
            build_id="build_other",
            host="127.0.0.1",
            port=listener.port,
            payload=b"ping\n",
            dedupe_key="build_other:ping",
        )
    assert exc.value.reason == "unapproved"
    assert listener.connections == 0


def test_http_dials_only_the_approved_service(listener, postgres):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers.gap_1258 import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    project_id = f"proj_{uuid.uuid4().hex[:10]}"
    created = client.post(
        "/v1/private-tcp/connectors", json={"project_id": project_id, "name": "billing"}
    )
    assert created.status_code == 200
    connector_id = created.json()["id"]
    approved = client.post(
        f"/v1/private-tcp/connectors/{connector_id}/approvals",
        json={"project_id": project_id, "host": "127.0.0.1", "port": listener.port},
    )
    assert approved.status_code == 200
    body = {
        "project_id": project_id,
        "connector_id": connector_id,
        "build_id": "build_http",
        "host": "127.0.0.1",
        "port": listener.port,
        "payload": "ping\n",
        "dedupe_key": "http:ping",
    }
    first = client.post("/v1/private-tcp/dial", json=body)
    assert first.status_code == 200
    assert first.json()["reply"] == "pong\n"
    assert first.json()["dialed"] is True
    second = client.post("/v1/private-tcp/dial", json=body)
    assert second.status_code == 200
    assert second.json()["repeated"] is True
    assert listener.connections == 1
    refused = client.post(
        "/v1/private-tcp/dial",
        json={**body, "host": "169.254.1.1", "dedupe_key": "http:link", "port": 9},
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["reason"] == "link_local"
    missing = client.post(
        "/v1/private-tcp/dial",
        json={**body, "host": "127.0.0.1", "port": 9, "dedupe_key": "http:no"},
    )
    assert missing.status_code == 403
    assert missing.json()["detail"]["reason"] == "unapproved"
