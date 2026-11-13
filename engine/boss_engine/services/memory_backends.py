"""External memory backends behind remember / recall / forget (BOSSAPP-892).

WHY. Some teams have standardised on an external agent-memory store (mem0, Zep...). BOSS's own
memory (agent_memory) and the knowledge graph stay CANONICAL: what an agent is told every turn is
built from them, never from an outside store. A backend is a MIRROR going out and an extra,
non-binding source coming in.

THE INTERFACE IS NARROW ON PURPOSE (:class:`MemoryBackend`): ``write`` one memory, ``delete`` one,
``search`` by text. Anything richer (graphs, sessions, users) is the outside product's business.

NOTHING LEAVES WITHOUT A PERSON. Every memory that becomes active in a project with a backend is
queued as a ``memory_exports`` row in ``pending_review`` holding a SNAPSHOT of the exact text that
would leave. A person approves (or rejects) each one in BOSS; :func:`sync` sends only approved
rows, and only their snapshot — an edit after approval is a new review. Deleting is not writing
out: a memory forgotten in BOSS queues an approved ``delete`` of its copy.

WHAT NEVER LEAVES: lessons still awaiting review (they bind nothing yet), and ``actor``-scoped
memories (one person's own; they are not the project's to export).

COMING BACK IN: with ``read_enabled``, :func:`recall_external` searches the backend and returns
hits marked ``source`` = the backend. They are shown to the agent as outside context, never
packed into the turn as a RULE, and never written into agent_memory.

ONE REFERENCE BACKEND: ``mem0`` (its documented REST API: POST /v1/memories/, POST
/v1/memories/search/, DELETE /v1/memories/{id}/, ``Authorization: Token``). It is exercised here
against a recorded transport; it has not been run against a live mem0 account.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Protocol
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .core import emit_event, make_id

KINDS = ("mem0",)
EXPORTED_SCOPES = ("org", "project", "role")
#: A transport is (method, url, headers, json body) -> (status, parsed json or None).
Transport = Callable[[str, str, dict[str, str], Optional[dict[str, Any]]],
                     tuple[int, Any]]


class MemoryBackendError(ValueError):
    """Something the operator can fix, phrased for them."""

    status = 422


class MemoryBackend(Protocol):
    name: str

    def write(self, *, body: str, user_id: str, metadata: dict[str, Any]) -> Optional[str]:
        """Store one memory; the backend's id for it (None when it does not say)."""

    def delete(self, external_id: str) -> None:
        """Remove one memory by the backend's id."""

    def search(self, *, query: str, user_id: str, limit: int) -> list[dict[str, Any]]:
        """[{id, body, score}] most relevant first."""


def urllib_transport(timeout: float = 8.0) -> Transport:
    def send(method: str, url: str, headers: dict[str, str],
             body: Optional[dict[str, Any]]) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, None

    return send


class Mem0Backend:
    """mem0's REST API. `base_url` is https://api.mem0.ai or a self-hosted server."""

    name = "mem0"

    def __init__(self, base_url: str, api_key: str, transport: Transport):
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Token {api_key}"} if api_key else {}
        self.transport = transport

    def _call(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        status, payload = self.transport(method, self.base + path, self.headers, body)
        if status >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise MemoryBackendError(f"mem0 answered {status}" + (f": {detail}" if detail else ""))
        return payload

    def write(self, *, body: str, user_id: str, metadata: dict[str, Any]) -> Optional[str]:
        payload = self._call("POST", "/v1/memories/", {
            "messages": [{"role": "user", "content": body}], "user_id": user_id,
            "metadata": metadata, "infer": False})
        results = payload.get("results") if isinstance(payload, dict) else payload
        for item in results or []:
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
        return None

    def delete(self, external_id: str) -> None:
        self._call("DELETE", f"/v1/memories/{external_id}/")

    def search(self, *, query: str, user_id: str, limit: int) -> list[dict[str, Any]]:
        payload = self._call("POST", "/v1/memories/search/",
                             {"query": query, "user_id": user_id, "limit": limit})
        results = payload.get("results") if isinstance(payload, dict) else payload
        hits = []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            body = str(item.get("memory") or item.get("text") or "").strip()
            if body:
                hits.append({"id": item.get("id"), "body": body[:2000],
                             "score": item.get("score")})
        return hits[:limit]


# ── configuration ───────────────────────────────────────────────────────────────────────────


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _backend_row(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def _check_credential(name: str, base_url: str) -> None:
    from . import env_refs

    if not name:
        return
    if not env_refs.env_name_allowed(name):
        raise MemoryBackendError(env_refs.refusal(name))
    try:
        env_refs.assert_may_send(name, base_url)
    except Exception as error:  # noqa: BLE001 — CredentialMisdirected says where, never the value
        raise MemoryBackendError(
            f"{name} may not be sent to {urlsplit(base_url).hostname}: list that host for it in "
            "BOSS_CREDENTIAL_HOSTS on the host first.") from error


def configure(connection: Connection, *, project_id: str, actor: str, name: str, kind: str,
              base_url: str, credential_env: str = "", read_enabled: bool = False,
              enabled: bool = True) -> dict[str, Any]:
    """Add or change a project's backend. The credential is NAMED (an env variable on the host,
    allowlisted by env_refs), never stored."""
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise MemoryBackendError("a backend needs a name")
    if kind not in KINDS:
        raise MemoryBackendError(f"kind must be one of: {', '.join(KINDS)}")
    parts = urlsplit(str(base_url or "").strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise MemoryBackendError("base_url must be an http(s) address")
    credential_env = str(credential_env or "").strip()
    _check_credential(credential_env, base_url)
    row = connection.execute(
        text("""
            INSERT INTO memory_backends (id, project_id, name, kind, base_url, credential_env,
                                         read_enabled, status, created_by)
            VALUES (:id, :p, :n, :k, :u, :c, :r, :s, :a)
            ON CONFLICT (project_id, name) DO UPDATE SET
              kind = EXCLUDED.kind, base_url = EXCLUDED.base_url,
              credential_env = EXCLUDED.credential_env, read_enabled = EXCLUDED.read_enabled,
              status = EXCLUDED.status, updated_at = now()
            RETURNING *
        """),
        {"id": make_id("mbk"), "p": project_id, "n": name, "k": kind,
         "u": str(base_url).strip().rstrip("/"), "c": credential_env, "r": bool(read_enabled),
         "s": "active" if enabled else "disabled", "a": actor},
    ).mappings().one()
    emit_event(connection, project_id, "memory_backend.configured", actor, "memory_backend",
               row["id"], {"name": name, "kind": kind, "read_enabled": bool(read_enabled),
                           "status": row["status"]})
    return _backend_row(row)


def list_backends(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("""
            SELECT b.*,
                   (SELECT count(*) FROM memory_exports e WHERE e.backend_id = b.id
                     AND e.status = 'pending_review') AS awaiting_review,
                   (SELECT count(*) FROM memory_exports e WHERE e.backend_id = b.id
                     AND e.status = 'written' AND e.op = 'write') AS written
              FROM memory_backends b WHERE b.project_id = :p ORDER BY b.name
        """),
        {"p": project_id},
    ).mappings().all()
    return [_backend_row(r) for r in rows]


def get_backend(connection: Connection, project_id: str, backend_id: str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM memory_backends WHERE id = :i AND project_id = :p"),
        {"i": backend_id, "p": project_id},
    ).mappings().first()
    if row is None:
        err = MemoryBackendError(f"no memory backend {backend_id!r} in this project")
        err.status = 404
        raise err
    return _backend_row(row)


def build(backend: dict[str, Any], transport: Optional[Transport] = None) -> MemoryBackend:
    name = backend.get("credential_env") or ""
    _check_credential(name, backend["base_url"])
    key = os.environ.get(name, "") if name else ""
    if name and not key:
        raise MemoryBackendError(f"{name} is not set on this host")
    if backend["kind"] == "mem0":
        return Mem0Backend(backend["base_url"], key, transport or urllib_transport())
    raise MemoryBackendError(f"no adapter for {backend['kind']!r}")


def _user_id(project_id: str, scope: str, scope_ref: str) -> str:
    return f"boss:{project_id}" if scope in ("org", "project") else f"boss:{project_id}:{scope}:{scope_ref}"


# ── the queue going out ─────────────────────────────────────────────────────────────────────


def on_memory_changed(connection: Connection, project_id: str, memory_id: str) -> int:
    """Called by agent_memory after a write that may change what may leave. Queues a review for
    an active, exportable memory; an approved delete for a retired/rejected one already sent.
    Returns how many rows were queued (0, and one cheap read, for a project with no backend)."""
    backends = connection.execute(
        text("SELECT id FROM memory_backends WHERE project_id = :p AND status = 'active'"),
        {"p": project_id},
    ).scalars().all()
    if not backends:
        return 0
    memory = connection.execute(
        text("SELECT id, kind, scope, scope_ref, body, status FROM agent_memory "
             "WHERE id = :i AND project_id = :p"), {"i": memory_id, "p": project_id},
    ).mappings().first()
    if memory is None:
        return 0
    queued = 0
    for backend_id in backends:
        if memory["status"] == "active" and memory["scope"] in EXPORTED_SCOPES:
            snapshot = {"body": memory["body"], "kind": memory["kind"], "scope": memory["scope"],
                        "scope_ref": memory["scope_ref"]}
            same = connection.execute(
                text("SELECT 1 FROM memory_exports WHERE backend_id = :b AND memory_id = :m "
                     "AND op = 'write' AND status IN ('pending_review', 'approved', 'written') "
                     "AND body = :body AND NOT EXISTS (SELECT 1 FROM memory_exports d "
                     "  WHERE d.backend_id = :b AND d.memory_id = :m AND d.op = 'delete' "
                     "  AND d.created_at > memory_exports.created_at)"),
                {"b": backend_id, "m": memory_id, "body": memory["body"]}).first()
            if same is None:
                connection.execute(
                    text("UPDATE memory_exports SET status = 'skipped', updated_at = now(), "
                         "error = 'superseded by a newer snapshot' WHERE backend_id = :b "
                         "AND memory_id = :m AND op = 'write' "
                         "AND status IN ('pending_review', 'approved')"),
                    {"b": backend_id, "m": memory_id})
                connection.execute(
                    text("INSERT INTO memory_exports (id, project_id, backend_id, memory_id, op, "
                         "body, snapshot, status) VALUES (:i, :p, :b, :m, 'write', :body, "
                         "CAST(:s AS JSONB), 'pending_review')"),
                    {"i": make_id("mex"), "p": project_id, "b": backend_id, "m": memory_id,
                     "body": memory["body"], "s": json.dumps(snapshot)})
                queued += 1
        elif memory["status"] in ("retired", "rejected"):
            # Never sent: nothing to take back; drop what was waiting.
            connection.execute(
                text("UPDATE memory_exports SET status = 'skipped', updated_at = now(), "
                     "error = 'the memory was retired before it was sent' "
                     "WHERE backend_id = :b AND memory_id = :m AND op = 'write' "
                     "AND status IN ('pending_review', 'approved')"),
                {"b": backend_id, "m": memory_id})
            sent = connection.execute(
                text("SELECT external_id FROM memory_exports WHERE backend_id = :b "
                     "AND memory_id = :m AND op = 'write' AND status = 'written' "
                     "AND external_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1"),
                {"b": backend_id, "m": memory_id}).scalar()
            if sent:
                connection.execute(
                    text("INSERT INTO memory_exports (id, project_id, backend_id, memory_id, op, "
                         "body, external_id, status, reviewed_by, reviewed_at) VALUES (:i, :p, "
                         ":b, :m, 'delete', '', :x, 'approved', 'system', now()) "
                         "ON CONFLICT DO NOTHING"),
                    {"i": make_id("mex"), "p": project_id, "b": backend_id, "m": memory_id,
                     "x": sent})
                queued += 1
    return queued


def pending(connection: Connection, project_id: str,
            backend_id: Optional[str] = None) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT e.*, b.name AS backend_name FROM memory_exports e "
             "JOIN memory_backends b ON b.id = e.backend_id WHERE e.project_id = :p "
             "AND e.status = 'pending_review' AND (CAST(:b AS TEXT) IS NULL OR e.backend_id = :b) "
             "ORDER BY e.created_at"),
        {"p": project_id, "b": backend_id},
    ).mappings().all()
    return [{**dict(r), "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"]),
             "reviewed_at": _iso(r["reviewed_at"])} for r in rows]


def review(connection: Connection, *, project_id: str, actor: str, export_ids: list[str],
           decision: str) -> dict[str, Any]:
    """A person lets these exact snapshots leave (approve) or keeps them in (reject)."""
    if decision not in ("approve", "reject"):
        raise MemoryBackendError("decision must be approve or reject")
    status = "approved" if decision == "approve" else "rejected"
    changed = connection.execute(
        text("UPDATE memory_exports SET status = :s, reviewed_by = :a, reviewed_at = now(), "
             "updated_at = now() WHERE project_id = :p AND id = ANY(:ids) "
             "AND status = 'pending_review' RETURNING id"),
        {"s": status, "a": actor, "p": project_id, "ids": list(export_ids)},
    ).scalars().all()
    if changed:
        emit_event(connection, project_id, f"memory_export.{status}", actor, "project",
                   project_id, {"count": len(changed)})
    return {"status": status, "ids": list(changed)}


def sync(engine: Engine, *, project_id: str, backend_id: str,
         transport: Optional[Transport] = None, limit: int = 50) -> dict[str, Any]:
    """Send approved rows (writes and deletes). Each row is its own transaction; a network call
    is never made while a row lock is held."""
    with engine.begin() as c:
        backend = get_backend(c, project_id, backend_id)
        rows = c.execute(
            text("SELECT e.id, e.op, e.body, e.snapshot, e.external_id, e.memory_id, "
                 "m.status AS memory_status FROM memory_exports e "
                 "LEFT JOIN agent_memory m ON m.id = e.memory_id "
                 "WHERE e.backend_id = :b AND e.status = 'approved' "
                 "ORDER BY e.created_at LIMIT :n"),
            {"b": backend_id, "n": int(limit)},
        ).mappings().all()
    if backend["status"] != "active":
        return {"sent": 0, "failed": 0, "skipped": 0, "note": "the backend is disabled"}
    client = build(backend, transport)
    report = {"sent": 0, "failed": 0, "skipped": 0}
    for row in rows:
        snapshot = row["snapshot"] if isinstance(row["snapshot"], dict) else json.loads(
            row["snapshot"] or "{}")
        external_id, error, status = row["external_id"], None, "written"
        try:
            if row["op"] == "write":
                if row["memory_status"] != "active":
                    status, error = "skipped", "the memory was retired after it was approved"
                else:
                    external_id = client.write(
                        body=row["body"],
                        user_id=_user_id(project_id, snapshot.get("scope", "project"),
                                         snapshot.get("scope_ref", "")),
                        metadata={"source": "boss", "boss_memory_id": row["memory_id"],
                                  "kind": snapshot.get("kind"), "scope": snapshot.get("scope")})
            else:
                client.delete(str(row["external_id"]))
        except Exception as exc:  # noqa: BLE001 — a failed send is recorded and retried later
            status, error = "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
        with engine.begin() as c:
            c.execute(
                text("UPDATE memory_exports SET status = CASE WHEN :s = 'failed' THEN "
                     "'approved' ELSE :s END, external_id = :x, error = :e, "
                     "attempts = attempts + 1, updated_at = now() WHERE id = :i"),
                {"s": status, "x": external_id, "e": error, "i": row["id"]})
        report[{"written": "sent", "failed": "failed", "skipped": "skipped"}[status]] += 1
    with engine.begin() as c:
        emit_event(c, project_id, "memory_backend.synced", "system", "memory_backend",
                   backend_id, report)
    return report


def recall_external(connection: Connection, *, project_id: str, query: str,
                    transport: Optional[Transport] = None,
                    limit: int = 5) -> list[dict[str, Any]]:
    """Hits from read-enabled backends for `query`, marked with their source. Never raises: an
    outside store being down must not take recall down with it."""
    if not (query or "").strip():
        return []
    rows = connection.execute(
        text("SELECT * FROM memory_backends WHERE project_id = :p AND status = 'active' "
             "AND read_enabled"), {"p": project_id},
    ).mappings().all()
    hits: list[dict[str, Any]] = []
    for row in rows:
        try:
            client = build(_backend_row(row), transport)
            for hit in client.search(query=query, user_id=_user_id(project_id, "project", ""),
                                     limit=limit):
                hits.append({**hit, "source": row["name"], "kind": row["kind"]})
        except Exception:  # noqa: BLE001
            continue
    return hits
