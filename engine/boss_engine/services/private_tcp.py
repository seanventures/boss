"""A build reaches an approved private TCP service through a customer connector.

Dial only a host and port a person approved on that connector. Link-local addresses and cloud
metadata addresses are refused before any socket, even when an approval row names them. A name
is resolved once and the socket opens on that pinned address, never on a second lookup.

This is not a priced model call. The dial row is the one effect: it is inserted as ``reserved``
and committed before the socket opens, and a repeat of the same dedupe key reads that row
instead of opening another connection. A connect failure stays on the row and is raised again.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

from ..db import begin
from .core import make_id

_MAX_BYTES = 4096

_METADATA_NAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
})

_METADATA_IPS = frozenset(
    ipaddress.ip_address(item)
    for item in (
        "169.254.169.254",
        "169.254.170.2",
        "fd00:ec2::254",
        "100.100.100.200",
    )
)

#: Private destinations a connector may dial once they are approved. Loopback is included so a
#: test (and a service on this machine) can be reached. Link-local is not in this list.
_PRIVATE = tuple(
    ipaddress.ip_network(item)
    for item in (
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
)

_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class PrivateTcpRefused(Exception):
    """The dial did not happen. ``reason`` is unapproved, link_local, metadata, or public."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class PrivateTcpFailed(Exception):
    """The dial was allowed and the service did not answer. The error is the message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason = "unreachable"


@dataclass(frozen=True)
class _Plan:
    host: str
    port: int
    address: str


def canonical_host(host: str) -> str:
    """Lower-case host, brackets and a zone id removed, an IPv4-mapped address unwrapped."""
    raw = str(host or "").strip().lower().rstrip(".")
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    raw = raw.split("%", 1)[0].strip()
    if not raw or any(ch in raw for ch in " \t\r\n/@\\"):
        raise PrivateTcpRefused("unapproved", "That host is not one a connector can dial.")
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        if _NAME.match(raw) is None:
            raise PrivateTcpRefused("unapproved", f"{raw} is not a host a connector can dial.")
        return raw
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def _as_ip(host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def classify(host: str) -> Optional[str]:
    """``metadata``, ``link_local``, or ``public`` when the host must not be dialed.

    A name that is not a metadata name returns None: the caller resolves it and classifies
    every answer. Metadata is decided before link-local, because 169.254.169.254 is both.
    """
    canon = canonical_host(host)
    if canon in _METADATA_NAMES:
        return "metadata"
    ip = _as_ip(canon)
    if ip is None:
        return None
    if ip in _METADATA_IPS:
        return "metadata"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast or ip.is_unspecified:
        return "public"
    if any(ip in net for net in _PRIVATE):
        return None
    return "public"


def _refusal_message(reason: str, host: str) -> str:
    if reason == "metadata":
        return (
            f"{host} is a cloud metadata address. "
            "A private TCP connector never dials one."
        )
    if reason == "link_local":
        return (
            f"{host} is a link-local address. "
            "A private TCP connector never dials one."
        )
    if reason == "public":
        return (
            f"{host} is not a private address. "
            "A private TCP connector dials only an approved private host."
        )
    return f"{host} is not an approved service on this connector."


def _port(port: object) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise PrivateTcpRefused("unapproved", "The port must be an integer from 1 to 65535.")
    return port


def _dedupe(key: object) -> str:
    text_key = str(key or "").strip()
    if not text_key or len(text_key) > 200:
        raise PrivateTcpRefused("unapproved", "A dial needs a dedupe key.")
    return text_key


def _payload(payload: object) -> bytes:
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raise PrivateTcpRefused("unapproved", "The payload must be text or bytes.")
    if len(raw) > _MAX_BYTES:
        raise PrivateTcpRefused("unapproved", "The payload is larger than 4096 bytes.")
    return raw


def plan_dial(host: str, port: int, approved: set[tuple[str, int]]) -> _Plan:
    """The pinned address to dial, or a refusal. Does not open a socket."""
    port_i = _port(port)
    canon = canonical_host(host)
    reason = classify(canon)
    if reason:
        raise PrivateTcpRefused(reason, _refusal_message(reason, canon))
    if (canon, port_i) not in approved:
        raise PrivateTcpRefused(
            "unapproved",
            f"{canon}:{port_i} is not an approved service on this connector.",
        )
    ip = _as_ip(canon)
    if ip is not None:
        return _Plan(canon, port_i, str(ip))
    try:
        infos = socket.getaddrinfo(canon, port_i, type=socket.SOCK_STREAM)
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise PrivateTcpFailed(f"Could not resolve {canon}. {detail}.") from exc
    addresses: list[str] = []
    for info in infos:
        addr = canonical_host(str(info[4][0]).split("%", 1)[0])
        if addr not in addresses:
            addresses.append(addr)
    if not addresses:
        raise PrivateTcpFailed(f"Could not resolve {canon}. It has no address.")
    bad: list[str] = []
    good: list[str] = []
    for addr in addresses:
        why = classify(addr)
        if why:
            bad.append(why)
        else:
            good.append(addr)
    if bad:
        for kind in ("metadata", "link_local", "public"):
            if kind in bad:
                raise PrivateTcpRefused(kind, _refusal_message(kind, canon))
    return _Plan(canon, port_i, good[0])


def _connect(address: str, port: int, timeout: float) -> socket.socket:
    """Open a TCP socket to a pinned IP. A name is refused here, so this cannot look up DNS."""
    ip = _as_ip(address)
    if ip is None:
        raise PrivateTcpRefused("unapproved", f"{address} is not a pinned address.")
    reason = classify(str(ip))
    if reason:
        raise PrivateTcpRefused(reason, _refusal_message(reason, str(ip)))
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((str(ip), port))
    except OSError as exc:
        sock.close()
        detail = exc.strerror or str(exc)
        raise PrivateTcpFailed(f"Could not reach {ip}:{port}. {detail}.") from exc
    return sock


def _exchange(address: str, port: int, payload: bytes, timeout: float) -> str:
    sock = _connect(address, port, timeout)
    try:
        if payload:
            sock.sendall(payload)
        buf = bytearray()
        while len(buf) < _MAX_BYTES:
            try:
                chunk = sock.recv(_MAX_BYTES - len(buf))
            except OSError as exc:
                detail = exc.strerror or str(exc)
                raise PrivateTcpFailed(f"Could not reach {address}:{port}. {detail}.") from exc
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in buf:
                break
        return bytes(buf).decode("utf-8", errors="replace")
    finally:
        sock.close()


def _result(connector_id: str, project_id: str, row: dict, *, dialed: bool, repeated: bool) -> dict:
    return {
        "connector_id": connector_id,
        "project_id": project_id,
        "build_id": row["build_id"],
        "host": row["host"],
        "port": int(row["port"]),
        "address": row["address"],
        "dedupe_key": row["dedupe_key"],
        "reply": row.get("reply") or "",
        "status": row["status"] if row["status"] == "reached" else row["status"],
        "dialed": dialed,
        "repeated": repeated,
    }


def _replay(row: dict) -> dict:
    status = row["status"]
    if status == "reached":
        return row
    if status == "failed":
        raise PrivateTcpFailed(row.get("error") or "The dial failed.")
    raise PrivateTcpFailed("This dial is already in progress.")


class CustomerConnector:
    """Approvals and one in-memory dial record per dedupe key."""

    def __init__(self, *, project_id: str, name: str, connector_id: Optional[str] = None) -> None:
        self.project_id = project_id
        self.name = name
        self.id = connector_id or make_id("ptcp")
        self._approvals: set[tuple[str, int]] = set()
        self._dials: dict[str, dict] = {}

    def approve(self, host: str, port: int) -> dict:
        port_i = _port(port)
        canon = canonical_host(host)
        reason = classify(canon)
        if reason:
            raise PrivateTcpRefused(reason, _refusal_message(reason, canon))
        key = (canon, port_i)
        created = key not in self._approvals
        self._approvals.add(key)
        return {"connector_id": self.id, "host": canon, "port": port_i, "created": created}

    def approved_services(self) -> list[dict]:
        return [{"host": host, "port": port} for host, port in sorted(self._approvals)]

    def dial_record(self, dedupe_key: str) -> Optional[dict]:
        row = self._dials.get(str(dedupe_key or "").strip())
        return dict(row) if row else None

    def reach(
        self,
        *,
        build_id: str,
        host: str,
        port: int,
        payload: object = b"",
        dedupe_key: str,
        timeout: float = 2.0,
    ) -> dict:
        build = str(build_id or "").strip()
        if not build:
            raise PrivateTcpRefused("unapproved", "A dial needs a build.")
        key = _dedupe(dedupe_key)
        prior = self._dials.get(key)
        if prior is not None:
            replay = _replay(prior)
            if replay["status"] != "reached":
                raise PrivateTcpFailed("This dial is already in progress.")
            return _result(self.id, self.project_id, replay, dialed=False, repeated=True)
        body = _payload(payload)
        plan = plan_dial(host, port, self._approvals)
        self._dials[key] = {
            "dedupe_key": key,
            "build_id": build,
            "host": plan.host,
            "port": plan.port,
            "address": plan.address,
            "status": "reserved",
            "reply": "",
            "error": "",
        }
        try:
            reply = _exchange(plan.address, plan.port, body, timeout)
        except PrivateTcpFailed as exc:
            self._dials[key]["status"] = "failed"
            self._dials[key]["error"] = str(exc)
            raise
        self._dials[key]["status"] = "reached"
        self._dials[key]["reply"] = reply
        return _result(self.id, self.project_id, self._dials[key], dialed=True, repeated=False)


def _require_text(value: object, what: str) -> str:
    text_value = str(value or "").strip()
    if not text_value or len(text_value) > 200:
        raise PrivateTcpRefused("unapproved", f"A dial needs a {what}.")
    return text_value


def _load(connection, project_id: str, connector_id: str) -> CustomerConnector:
    row = connection.execute(
        text(
            """
            SELECT id, project_id, name
            FROM private_tcp_connectors
            WHERE id = :id
            """
        ),
        {"id": connector_id},
    ).mappings().first()
    if row is None or row["project_id"] != project_id:
        raise PrivateTcpRefused("unapproved", "This connector is not in this project.")
    connector = CustomerConnector(
        project_id=row["project_id"], name=row["name"], connector_id=row["id"]
    )
    approvals = connection.execute(
        text(
            """
            SELECT host, port
            FROM private_tcp_approvals
            WHERE connector_id = :id
            """
        ),
        {"id": connector_id},
    ).mappings().all()
    for item in approvals:
        try:
            connector._approvals.add((canonical_host(item["host"]), int(item["port"])))
        except (PrivateTcpRefused, TypeError, ValueError):
            continue
    return connector


def _select_dial(connection, project_id: str, dedupe_key: str):
    return connection.execute(
        text(
            """
            SELECT id, project_id, connector_id, build_id, dedupe_key, host, port,
                   address, status, reply, error
            FROM private_tcp_dials
            WHERE project_id = :project_id AND dedupe_key = :dedupe_key
            """
        ),
        {"project_id": project_id, "dedupe_key": dedupe_key},
    ).mappings().first()


def _row_dict(row) -> dict:
    data = dict(row)
    data["port"] = int(data["port"])
    return data


def open_connector(*, project_id: str, name: str, connector_id: Optional[str] = None) -> dict:
    project = _require_text(project_id, "project")
    label = _require_text(name, "name")
    cid = connector_id or make_id("ptcp")
    with begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO private_tcp_connectors (id, project_id, name)
                VALUES (:id, :project_id, :name)
                """
            ),
            {"id": cid, "project_id": project, "name": label},
        )
    return {"id": cid, "project_id": project, "name": label}


def approve_saved(*, project_id: str, connector_id: str, host: str, port: int) -> dict:
    project = _require_text(project_id, "project")
    with begin() as connection:
        connector = _load(connection, project, connector_id)
        result = connector.approve(host, port)
        if result["created"]:
            connection.execute(
                text(
                    """
                    INSERT INTO private_tcp_approvals (id, connector_id, host, port)
                    VALUES (:id, :connector_id, :host, :port)
                    """
                ),
                {
                    "id": make_id("ptap"),
                    "connector_id": connector.id,
                    "host": result["host"],
                    "port": result["port"],
                },
            )
        return result


def get_dial(project_id: str, dedupe_key: str) -> Optional[dict]:
    with begin() as connection:
        row = _select_dial(connection, project_id, dedupe_key)
    return _row_dict(row) if row else None


def list_dials(project_id: str, connector_id: str) -> list[dict]:
    with begin() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, connector_id, build_id, dedupe_key, host, port,
                       address, status, reply, error
                FROM private_tcp_dials
                WHERE project_id = :project_id AND connector_id = :connector_id
                ORDER BY created_at ASC, id ASC
                """
            ),
            {"project_id": project_id, "connector_id": connector_id},
        ).mappings().all()
    return [_row_dict(row) for row in rows]


def _mark(project_id: str, dedupe_key: str, *, status: str, reply: str, error: str) -> None:
    with begin() as connection:
        updated = connection.execute(
            text(
                """
                UPDATE private_tcp_dials
                SET status = :status, reply = :reply, error = :error
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {
                "status": status,
                "reply": reply,
                "error": error,
                "project_id": project_id,
                "dedupe_key": dedupe_key,
            },
        )
    if updated.rowcount != 1:
        raise PrivateTcpFailed("The dial record was not updated.")


def dial_through(
    *,
    project_id: str,
    connector_id: str,
    build_id: str,
    host: str,
    port: int,
    payload: object = b"",
    dedupe_key: str,
    timeout: float = 2.0,
) -> dict:
    """Reserve the dedupe key, commit it, then dial. A repeat does not open a socket."""
    project = _require_text(project_id, "project")
    build = _require_text(build_id, "build")
    key = _dedupe(dedupe_key)
    body = _payload(payload)
    with begin() as connection:
        connector = _load(connection, project, connector_id)
        existing = _select_dial(connection, project, key)
        if existing is not None:
            replay = _replay(_row_dict(existing))
            if replay["status"] != "reached":
                raise PrivateTcpFailed("This dial is already in progress.")
            return _result(connector.id, project, replay, dialed=False, repeated=True)
        plan = plan_dial(host, port, connector._approvals)
        connection.execute(
            text(
                """
                INSERT INTO private_tcp_dials (
                    id, project_id, connector_id, build_id, dedupe_key,
                    host, port, address, status, reply, error
                ) VALUES (
                    :id, :project_id, :connector_id, :build_id, :dedupe_key,
                    :host, :port, :address, 'reserved', '', ''
                )
                """
            ),
            {
                "id": make_id("ptdl"),
                "project_id": project,
                "connector_id": connector.id,
                "build_id": build,
                "dedupe_key": key,
                "host": plan.host,
                "port": plan.port,
                "address": plan.address,
            },
        )
        pinned = plan
        connector_id_out = connector.id
    try:
        reply = _exchange(pinned.address, pinned.port, body, timeout)
    except PrivateTcpFailed as exc:
        _mark(project, key, status="failed", reply="", error=str(exc))
        raise
    except PrivateTcpRefused as exc:
        _mark(project, key, status="failed", reply="", error=str(exc))
        raise
    _mark(project, key, status="reached", reply=reply, error="")
    return _result(
        connector_id_out,
        project,
        {
            "build_id": build,
            "host": pinned.host,
            "port": pinned.port,
            "address": pinned.address,
            "dedupe_key": key,
            "reply": reply,
            "status": "reached",
        },
        dialed=True,
        repeated=False,
    )
