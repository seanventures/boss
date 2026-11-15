"""THE FLEET: agent hosts, their enrolment, heartbeat, desired state and guest-call audit (P8a,
BOSSAPP-1063; boss-agent-hosts-lld.md §3.1, §3.2). The control-plane side of the host contract
boss-hostd (Go, BOSSAPP-1062) speaks.

    POST /v1/hosts/enrol                  one-time join token + CSR → host id, 24 h certificate,
                                          optional SVID intermediate, the CA, `vm_prefix`
    POST /v1/hosts/{id}/renew             a fresh certificate (refused once the host is revoked)
    POST /v1/hosts/{id}/heartbeat         every 2 s: attestation facts, capacity, pressure,
                                          per-replica observed state, observed_generation
    GET  /v1/hosts/{id}/desired?since=g   LEVEL-BASED: the host's whole desired state when its
                                          generation is past `g` (long-poll up to `wait` s), else
                                          `{"changed": false}`; a reconnect with since=0 resyncs
    POST /v1/hosts/{id}/observed          per-replica observed state (also accepted in heartbeat)
    POST /v1/hosts/{id}/guest-calls       a batch of guest_calls audit rows (idempotent per call_id)

Postgres is the only source of truth (OR-3): a host's bbolt cache is rebuilt from `desired`.
Hosts never decide ownership; the scheduler (services/fleet_scheduler.py) does.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import fleet_ca
from .core import make_id

log = logging.getLogger(__name__)

JOIN_TOKEN_PREFIX = "boss_hjt_"
JOIN_TOKEN_HOURS = 24
#: A host that has not beaten for this long is `unreachable` (§3.14.1 re-placement threshold).
UNREACHABLE_SECONDS = 10
#: The heartbeat cadence boss-hostd is told (FR-4).
HEARTBEAT_SECONDS = 2
#: Each host's VM address block: a /23 holds 510 VMs (NFR-8).
VM_PREFIX_LEN = 23
WATCH_MAX_SECONDS = 30.0
GUEST_CALL_BATCH_MAX = 1000
ROUTES = ("model", "git", "api", "mcp", "run_plane", "connect")
#: Handle prefixes that must never reach the audit table (only a fingerprint may, §3.19).
_HANDLE = re.compile(r"boss_(mdl|egr|mcpgw|run|sak|pat|hjt)_[A-Za-z0-9_\-]{6,}")

#: The attestation facts a heartbeat may set, by column (§3.1, §3.17.4).
ATTESTATION = ("kvm_live", "firecracker_live", "jailer_on", "seccomp_default", "smt_off",
               "ksm_off", "swap_off", "strict_secrets", "net_policy_ok")


class FleetError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.status, self.code = status, code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def host_event(connection: Any, *, org_id: str, host_id: Optional[str], kind: str,
               detail: Optional[dict] = None, actor: str = "operator",
               vm_id: Optional[str] = None) -> None:
    connection.execute(text(
        "INSERT INTO host_events (org_id, host_id, vm_id, kind, detail, actor) "
        "VALUES (:o, :h, :v, :k, CAST(:d AS JSONB), :a)"),
        {"o": org_id, "h": host_id, "v": vm_id, "k": kind, "d": json.dumps(detail or {}),
         "a": actor})


# ── Join tokens (console) ───────────────────────────────────────────────────────────────────

def issue_join_token(connection: Any, *, org_id: str, actor: str, pool: str = "default",
                     labels: Optional[dict] = None, hours: int = JOIN_TOKEN_HOURS,
                     role: str = "host") -> dict:
    """A one-time join token, for one role: `host` (boss-hostd) or `director` (boss-lbd,
    P8b-10). The token itself is in this answer once and never again."""
    if not 1 <= int(hours) <= 168:
        raise FleetError("a join token lives between 1 and 168 hours.")
    if role not in ("host", "director"):
        raise FleetError("a join token is for a host or a director.")
    token = JOIN_TOKEN_PREFIX + secrets.token_urlsafe(32)
    tid = make_id("hjt")
    expires = _now() + timedelta(hours=int(hours))
    connection.execute(text("""
        INSERT INTO agent_host_join_tokens (id, org_id, token_hash, pool, labels, expires_at,
                                            created_by, role)
        VALUES (:i, :o, :h, :p, CAST(:l AS JSONB), :e, :a, :r)"""),
        {"i": tid, "o": org_id, "h": _hash(token), "p": pool or "default",
         "l": json.dumps(labels or {}), "e": expires, "a": actor, "r": role})
    host_event(connection, org_id=org_id, host_id=None, kind="join_token_issued",
               detail={"join_token_id": tid, "pool": pool, "role": role}, actor=actor)
    return {"id": tid, "token": token, "pool": pool or "default", "labels": labels or {},
            "expires_at": expires.isoformat(), "role": role}


def revoke_join_token(connection: Any, *, org_id: str, token_id: str, actor: str) -> bool:
    done = connection.execute(text(
        "UPDATE agent_host_join_tokens SET revoked_at = now() WHERE id = :i AND org_id = :o "
        "AND used_at IS NULL AND revoked_at IS NULL RETURNING id"),
        {"i": token_id, "o": org_id}).first()
    if done:
        host_event(connection, org_id=org_id, host_id=None, kind="join_token_revoked",
                   detail={"join_token_id": token_id}, actor=actor)
    return bool(done)


# ── Enrolment ───────────────────────────────────────────────────────────────────────────────

def _supernet() -> ipaddress.IPv4Network:
    return ipaddress.ip_network(os.environ.get("BOSS_FLEET_VM_SUPERNET") or "10.192.0.0/10")


def _allocate_prefix(connection: Any, org_id: str) -> str:
    """The lowest free /23 in the supernet (per org; addresses are only routed within it),
    under a per-org advisory lock so two concurrent enrolments never take the same block (the
    unique index idx_agent_hosts_vm_prefix holds it in the database too)."""
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                       {"k": f"boss.fleet.vm_prefix:{org_id}"})
    used = {str(r[0]) for r in connection.execute(text(
        "SELECT vm_prefix FROM agent_hosts WHERE org_id = :o AND vm_prefix IS NOT NULL "
        "AND revoked_at IS NULL"), {"o": org_id}).all()}
    for block in _supernet().subnets(new_prefix=VM_PREFIX_LEN):
        if str(block) not in used:
            return str(block)
    raise FleetError("the VM address plan is full.", status=409, code="address_plan_full")


#: The capability a host advertises when it holds per-run DPoP keys (P8b S13; HOST_API.md §4.8).
CAP_HOST_TOKENS = "host_tokens"
HOST_TOKENS_REQUIRED_ENV = "BOSS_HOST_TOKENS_REQUIRED"
_HOST_TOKENS_CACHE: dict = {}
_HOST_TOKENS_TTL = 30.0


def _advertises_host_tokens(capabilities: Any) -> bool:
    return isinstance(capabilities, (list, tuple)) and CAP_HOST_TOKENS in capabilities


def host_tokens_required(connection: Any, host_id: Optional[str]) -> bool:
    """Whether forwards from this host MUST carry the run's delegated token (P8b S13 decision):
    yes for a host that advertised the `host_tokens` capability (sticky), no for a host that
    predates S13. BOSS_HOST_TOKENS_REQUIRED=1 forces it on for every host (tests, a strict
    install); it never turns the requirement off. Cached 30 s per host — the flag only ever goes
    from false to true, so a stale answer is at most 30 s of the old leniency. A database error
    FAILS CLOSED (required) and is not cached."""
    import time as _time

    if (os.environ.get(HOST_TOKENS_REQUIRED_ENV) or "").strip().lower() in ("1", "true", "yes"):
        return True
    if not host_id:
        return False
    now = _time.monotonic()
    hit = _HOST_TOKENS_CACHE.get(host_id)
    if hit is not None and hit[1] > now and hit[0]:
        return True
    try:
        with connection.begin_nested():
            value = bool(connection.execute(text(
                "SELECT host_tokens FROM agent_hosts WHERE id = :h"), {"h": host_id}).scalar())
    except Exception:  # noqa: BLE001 — FAIL CLOSED: an unreadable answer is "required"
        log.warning("host_tokens unreadable for %s: treating the token as required", host_id)
        return True                      # never cached: the next call asks again
    _HOST_TOKENS_CACHE[host_id] = (value, now + _HOST_TOKENS_TTL)
    return value


def _record_capabilities(connection: Any, host_id: str, capabilities: Any) -> None:
    if _advertises_host_tokens(capabilities):
        connection.execute(text(
            "UPDATE agent_hosts SET host_tokens = true WHERE id = :i AND NOT host_tokens"),
            {"i": host_id})
        _HOST_TOKENS_CACHE.pop(host_id, None)


def enrol(connection: Any, *, join_token: str, csr_pem: str, svid_csr_pem: Optional[str] = None,
          hostname: str = "", backend: str = "firecracker", facts: Optional[dict] = None,
          capacity: Optional[dict] = None, labels: Optional[dict] = None, role: str = "host",
          address: Optional[str] = None, capabilities: Optional[list] = None) -> dict:
    """Spend a join token: a host row, its 24 h certificate (and SVID intermediate) and its VM
    address block. A token that is unknown, spent, revoked or expired is refused identically.
    A token issued for `role: director` enrols a boss-lbd director instead (P8b-10)."""
    if backend not in ("firecracker", "process"):
        raise FleetError("backend must be firecracker or process.")
    row = connection.execute(text("""
        SELECT id, org_id, pool, labels, expires_at, used_at, revoked_at, role
          FROM agent_host_join_tokens WHERE token_hash = :h FOR UPDATE"""),
        {"h": _hash(str(join_token or ""))}).mappings().first()
    if (row is None or row["used_at"] is not None or row["revoked_at"] is not None
            or row["expires_at"] <= _now()):
        raise FleetError("that join token is not valid.", status=401, code="join_token_invalid")
    if (role or "host") != (row["role"] or "host"):
        raise FleetError(f"this join token enrols a {row['role']}, not a {role}.", status=400,
                         code="join_token_role")
    if row["role"] == "director":
        from . import fleet_directors

        try:
            return fleet_directors.enrol(connection, token=dict(row), csr_pem=csr_pem,
                                         hostname=hostname, address=address, labels=labels)
        except fleet_directors.DirectorError as exc:
            raise FleetError(str(exc), status=exc.status, code=exc.code) from exc
    org_id = str(row["org_id"])
    host_id = make_id("hst")
    prefix = _allocate_prefix(connection, org_id)
    merged = {**_json(row["labels"], {}), **(labels or {})}
    connection.execute(text("""
        INSERT INTO agent_hosts (id, org_id, pool, hostname, labels, backend, vm_prefix,
                                 capacity, facts, status, last_heartbeat_at, address)
        VALUES (:i, :o, :p, :hn, CAST(:l AS JSONB), :b, CAST(:vp AS CIDR),
                CAST(:c AS JSONB), CAST(:f AS JSONB), 'ready', now(), CAST(:addr AS INET))"""),
        {"i": host_id, "o": org_id, "p": row["pool"], "hn": hostname[:200],
         "l": json.dumps(merged), "b": backend, "vp": prefix,
         "c": json.dumps(capacity or {}), "f": json.dumps(facts or {}),
         "addr": _ipv4_or_none(address)})
    try:
        issued = fleet_ca.issue_host_cert(connection, org_id=org_id, host_id=host_id,
                                          csr_pem=csr_pem)
        svid = (fleet_ca.issue_svid_intermediate(connection, org_id=org_id, host_id=host_id,
                                                 csr_pem=svid_csr_pem)
                if svid_csr_pem else None)
    except fleet_ca.FleetAuthError as exc:
        raise FleetError(f"the CSR was refused: {exc.code}", status=400, code=exc.code) from exc
    _apply_facts(connection, host_id, facts or {})
    _record_capabilities(connection, host_id, capabilities)
    connection.execute(text(
        "UPDATE agent_hosts SET cert_fingerprint = :f, cert_not_after = :na WHERE id = :i"),
        {"f": issued.fingerprint, "na": issued.not_after, "i": host_id})
    connection.execute(text(
        "UPDATE agent_host_join_tokens SET used_at = now(), used_by_host = :h WHERE id = :i"),
        {"h": host_id, "i": row["id"]})
    host_event(connection, org_id=org_id, host_id=host_id, kind="enrolled",
               detail={"backend": backend, "pool": row["pool"], "vm_prefix": prefix,
                       "join_token_id": row["id"]}, actor="host")
    # The directors route this host's block through its address (P8b-10).
    from . import fleet_directors

    fleet_directors.bump(connection, org_id)
    return {"host_id": host_id, "org_id": org_id, "certificate_pem": issued.cert_pem,
            "certificate_not_after": issued.not_after.isoformat(),
            "renew_after": fleet_ca.renew_after(issued).isoformat(),
            "svid_intermediate_pem": svid.cert_pem if svid else None,
            "ca_pem": fleet_ca.ca_pem(connection), "spiffe_id": fleet_ca.host_spiffe_id(host_id),
            "trust_domain": fleet_ca.trust_domain(), "vm_prefix": prefix,
            "heartbeat_seconds": HEARTBEAT_SECONDS, "endpoints": endpoints(host_id)}


def endpoints(host_id: str) -> dict:
    """The control-plane paths boss-hostd uses (never the guest)."""
    return {"heartbeat": f"/v1/hosts/{host_id}/heartbeat",
            "desired": f"/v1/hosts/{host_id}/desired",
            "observed": f"/v1/hosts/{host_id}/observed",
            "guest_calls": f"/v1/hosts/{host_id}/guest-calls",
            "renew": f"/v1/hosts/{host_id}/renew", "run_plane": "/v1/run-plane",
            # The central streaming model gateway boss-hostd forwards the guest's model calls
            # to (P8b S10; HOST_API.md §4.6). Absent → the host answers 503 gateway_unconfigured.
            "model_gateway": _model_gateway_url(),
            # The MCP/tool gateway lives on the control plane (P8b S12; HOST_API.md §4.7): the
            # guest's /mcp/<server> is forwarded here, relative to the control-plane URL.
            "mcp_gateway": _mcp_gateway_route(),
            # boss-egress (P8b S11; HOST_API.md §4.9): the guest's /git/<host>/… and /api/<host>/…
            # go here with the host's proof. Absent → the host answers 503 gateway_unconfigured.
            "egress": _egress_url()}


def _egress_url() -> str:
    """The URL hosts reach boss-egress at: BOSS_EGRESS_HOST_URL (the address hosts see, when it
    differs from the API's), else BOSS_EGRESS_URL. "" when egress is off."""
    return (os.environ.get("BOSS_EGRESS_HOST_URL") or os.environ.get("BOSS_EGRESS_URL")
            or "").strip().rstrip("/")


def _mcp_gateway_route() -> str:
    from .mcp_gateway import HOST_ROUTE

    return HOST_ROUTE


def _model_gateway_url() -> str:
    from .streaming_model_gateway import gateway_url

    return gateway_url()


def renew(connection: Any, host: fleet_ca.HostIdentity, *, csr_pem: str,
          svid_csr_pem: Optional[str] = None) -> dict:
    """A fresh certificate for a live host. A revoked host never gets here (authentication
    refuses it), and is refused again here under the row lock: revocation refuses renewal."""
    row = connection.execute(text(
        "SELECT status, revoked_at FROM agent_hosts WHERE id = :i FOR UPDATE"),
        {"i": host.host_id}).mappings().first()
    if row is None or row["revoked_at"] is not None or row["status"] == "retired":
        raise FleetError("this host is revoked.", status=403, code="host_revoked")
    try:
        issued = fleet_ca.issue_host_cert(connection, org_id=host.org_id, host_id=host.host_id,
                                          csr_pem=csr_pem)
        svid = (fleet_ca.issue_svid_intermediate(connection, org_id=host.org_id,
                                                 host_id=host.host_id, csr_pem=svid_csr_pem)
                if svid_csr_pem else None)
    except fleet_ca.FleetAuthError as exc:
        raise FleetError(f"the CSR was refused: {exc.code}", status=400, code=exc.code) from exc
    connection.execute(text(
        "UPDATE agent_hosts SET cert_fingerprint = :f, cert_not_after = :na, updated_at = now() "
        "WHERE id = :i"), {"f": issued.fingerprint, "na": issued.not_after, "i": host.host_id})
    host_event(connection, org_id=host.org_id, host_id=host.host_id, kind="renewed",
               detail={"serial": issued.serial}, actor="host")
    return {"host_id": host.host_id, "certificate_pem": issued.cert_pem,
            "certificate_not_after": issued.not_after.isoformat(),
            "renew_after": fleet_ca.renew_after(issued).isoformat(),
            "svid_intermediate_pem": svid.cert_pem if svid else None,
            "ca_pem": fleet_ca.ca_pem(connection)}


def revoke(connection: Any, *, org_id: str, host_id: str, actor: str, reason: str = "") -> dict:
    """Revoke a host: every certificate it holds, every call it makes from now on, its next
    renewal. Its replicas are stopped and placed elsewhere by the next reconcile."""
    row = connection.execute(text(
        "UPDATE agent_hosts SET revoked_at = now(), revoked_reason = :r, status = 'retired', "
        "isolation_ok = false, "
        "desired_generation = desired_generation + 1, updated_at = now() "
        "WHERE id = :i AND org_id = :o AND revoked_at IS NULL RETURNING id"),
        {"r": reason[:500], "i": host_id, "o": org_id}).first()
    if row is None:
        raise FleetError("no such live host in this organisation.", status=404, code="not_found")
    connection.execute(text(
        "UPDATE agent_certs SET revoked_at = now() WHERE host_id = :i AND revoked_at IS NULL"),
        {"i": host_id})
    connection.execute(text(
        "UPDATE agent_replicas SET desired_state = 'stopped', updated_at = now() "
        "WHERE host_id = :i AND desired_state = 'running'"), {"i": host_id})
    host_event(connection, org_id=org_id, host_id=host_id, kind="revoked",
               detail={"reason": reason[:500]}, actor=actor)
    # The directors drop the host's route and its real servers now, not at the next reconcile.
    from . import fleet_directors

    woken = fleet_directors.bump(connection, org_id)
    return {"host_id": host_id, "revoked": True, "directors_woken": woken}


def set_cordon(connection: Any, *, org_id: str, host_id: str, actor: str,
               cordoned: bool) -> dict:
    row = connection.execute(text(
        "UPDATE agent_hosts SET status = :s, updated_at = now() WHERE id = :i AND org_id = :o "
        "AND revoked_at IS NULL AND status IN ('ready', 'cordoned', 'unreachable') "
        "RETURNING id"), {"s": "cordoned" if cordoned else "ready", "i": host_id,
                          "o": org_id}).first()
    if row is None:
        raise FleetError("no such live host in this organisation.", status=404, code="not_found")
    host_event(connection, org_id=org_id, host_id=host_id,
               kind="cordoned" if cordoned else "uncordoned", actor=actor)
    return get_host(connection, org_id, host_id) or {}


# ── Heartbeat and observed state ────────────────────────────────────────────────────────────

def _apply_facts(connection: Any, host_id: str, facts: dict) -> None:
    sets = {k: bool(facts[k]) for k in ATTESTATION if k in facts}
    if not sets:
        return
    clause = ", ".join(f"{k} = :{k}" for k in sets)
    connection.execute(text(f"UPDATE agent_hosts SET {clause} WHERE id = :i"),
                       {**sets, "i": host_id})


def refresh_isolation(connection: Any, host_id: str) -> bool:
    """Store the host's AP-D11 attestation verdict (agent_hosts.isolation_ok, P8b-11) from the
    facts it just reported. True when it flipped: the directors must re-filter the members of
    deployments that require strong isolation (fleet_directors.desired)."""
    from .strong_isolation import HOST_FACTS, host_isolation_ok

    row = connection.execute(text(
        "SELECT backend, status, revoked_at, facts, isolation_ok, " + ", ".join(HOST_FACTS)
        + " FROM agent_hosts WHERE id = :i"), {"i": host_id}).mappings().first()
    if row is None:
        return False
    host = dict(row)
    host["facts"] = _json(host.get("facts"), {})
    ok = host_isolation_ok(host)
    if ok == bool(row["isolation_ok"]):
        return False
    connection.execute(text("UPDATE agent_hosts SET isolation_ok = :ok WHERE id = :i"),
                       {"ok": ok, "i": host_id})
    return True


def _ipv4_or_none(value: Any) -> Optional[str]:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(str(value))
    except ValueError:
        return None
    return str(ip) if ip.version == 4 else None


#: Directors to wake after this transaction commits (collected per connection by _observe and
#: friends, taken by the service's return value, woken by the router).
_WAKE_KEY = "boss.fleet.wake_directors"


def _note_wake(connection: Any, ids: Iterable[str]) -> None:
    info = connection.info.setdefault(_WAKE_KEY, set())
    info.update(ids)


def _take_wake(connection: Any) -> list:
    return sorted(connection.info.pop(_WAKE_KEY, set()))


def _observe(connection: Any, host_id: str, replicas: Iterable[dict]) -> int:
    seen = 0
    flipped = False
    for item in replicas or ():
        if not isinstance(item, dict) or not item.get("replica_id"):
            continue
        vm_ip = item.get("vm_ip")
        if vm_ip:
            try:
                ipaddress.ip_address(str(vm_ip))
            except ValueError:
                vm_ip = None
        updated = connection.execute(text("""
            UPDATE agent_replicas r SET observed_state = :s, vm_ip = COALESCE(CAST(:ip AS INET), vm_ip),
                   busy = :busy, restarts = GREATEST(restarts, :restarts), last_seen_at = now(),
                   updated_at = now()
              FROM (SELECT id, observed_state AS prev, vm_ip AS prev_ip FROM agent_replicas
                     WHERE id = :r) p
             WHERE r.id = p.id AND r.host_id = :h
            RETURNING r.id, p.prev, (host(p.prev_ip) IS DISTINCT FROM host(r.vm_ip)) AS ip_changed"""),
            {"s": str(item.get("state") or "unknown")[:40], "ip": vm_ip,
             "busy": max(0, int(item.get("busy") or 0)),
             "restarts": max(0, int(item.get("restarts") or 0)),
             "r": str(item["replica_id"]), "h": host_id}).first()
        seen += 1 if updated else 0
        if updated:
            now_serving = str(item.get("state") or "") == "serving"
            # A real server joins or leaves the directors' feed (P8b-10).
            if (updated[1] == "serving") != now_serving or (now_serving and updated[2]):
                flipped = True
    if flipped:
        from . import fleet_directors

        org = connection.execute(text("SELECT org_id FROM agent_hosts WHERE id = :h"),
                                 {"h": host_id}).scalar()
        if org:
            _note_wake(connection, fleet_directors.bump(connection, str(org)))
    return seen


def heartbeat(connection: Any, host: fleet_ca.HostIdentity, body: dict) -> dict:
    """One heartbeat: facts, capacity, pressure, replicas' observed state, the generation the
    host converged to. Answers with the generation it should be at and whether to renew."""
    facts = body.get("facts") if isinstance(body.get("facts"), dict) else {}
    row = connection.execute(text("""
        UPDATE agent_hosts SET last_heartbeat_at = now(), updated_at = now(),
               status = CASE WHEN status = 'unreachable' THEN 'ready' ELSE status END,
               capacity = COALESCE(CAST(:cap AS JSONB), capacity),
               allocatable = COALESCE(CAST(:alloc AS JSONB), allocatable),
               pressure = COALESCE(CAST(:pr AS JSONB), pressure),
               facts = facts || CAST(:f AS JSONB),
               bgp_state = COALESCE(:bgp, bgp_state),
               address = COALESCE(CAST(:addr AS INET), address),
               observed_generation = GREATEST(observed_generation, :og)
         WHERE id = :i AND revoked_at IS NULL
        RETURNING desired_generation, cert_not_after, status, org_id,
                  (CAST(:addr AS INET) IS NOT NULL AND address IS DISTINCT FROM
                   (SELECT address FROM agent_hosts WHERE id = :i)) AS address_changed"""),
        {"cap": json.dumps(body["capacity"]) if isinstance(body.get("capacity"), dict) else None,
         "alloc": json.dumps(body["allocatable"]) if isinstance(body.get("allocatable"),
                                                                dict) else None,
         "pr": json.dumps(body["pressure"]) if isinstance(body.get("pressure"), dict) else None,
         "f": json.dumps(facts), "bgp": body.get("bgp_state"),
         "addr": _ipv4_or_none(body.get("address")),
         "og": int(body.get("observed_generation") or 0), "i": host.host_id}).mappings().first()
    if row is None:
        raise FleetError("this host is revoked.", status=403, code="host_revoked")
    if row["address_changed"]:
        from . import fleet_directors

        _note_wake(connection, fleet_directors.bump(connection, str(row["org_id"])))
    _apply_facts(connection, host.host_id, facts)
    if refresh_isolation(connection, host.host_id):
        from . import fleet_directors

        _note_wake(connection, fleet_directors.bump(connection, str(row["org_id"])))
    _record_capabilities(connection, host.host_id, body.get("capabilities"))
    seen = _observe(connection, host.host_id, body.get("replicas") or ())
    if isinstance(body.get("replicas"), list):
        # A heartbeat lists EVERY VM the host runs: a replica wanted stopped that it no longer
        # lists is gone, and leaves the desired state (fleet.desired lists it until then).
        reported = [str(r.get("replica_id")) for r in body["replicas"]
                    if isinstance(r, dict) and r.get("replica_id")]
        connection.execute(text(
            "UPDATE agent_replicas SET observed_state = 'gone', updated_at = now() "
            "WHERE host_id = :h AND desired_state = 'stopped' AND observed_state <> 'gone' "
            "AND NOT (id = ANY(:seen))"), {"h": host.host_id, "seen": reported})
    renew_due = bool(row["cert_not_after"] and row["cert_not_after"] - _now() <= timedelta(
        hours=fleet_ca.HOST_CERT_HOURS * (1 - fleet_ca.RENEW_FRACTION)))
    return {"host_id": host.host_id, "desired_generation": int(row["desired_generation"]),
            "status": row["status"], "replicas_seen": seen, "renew": renew_due,
            "heartbeat_seconds": HEARTBEAT_SECONDS, "_wake": _take_wake(connection)}


def _not_mine(connection: Any, host_id: str, replicas: Iterable[dict]) -> None:
    """A host speaks only for its own replicas (P8a-T8): one it does not hold is refused 403."""
    ids = sorted({str(r.get("replica_id")) for r in replicas or ()
                  if isinstance(r, dict) and r.get("replica_id")})
    if not ids:
        return
    mine = {r[0] for r in connection.execute(text(
        "SELECT id FROM agent_replicas WHERE id = ANY(:ids) AND host_id = :h"),
        {"ids": ids, "h": host_id}).all()}
    foreign = [i for i in ids if i not in mine]
    if foreign:
        raise FleetError(f"replica {foreign[0]} is not placed on this host.", status=403,
                         code="replica_not_on_host")


def _store_gen(connection: Any, host_id: str, state_gen: Any) -> None:
    """The full map (PUT …/replicas) sets the generation outright."""
    if state_gen is not None:
        connection.execute(text(
            "UPDATE agent_hosts SET replica_state_gen = :g WHERE id = :h"),
            {"g": int(state_gen), "h": host_id})


def _advance_gen(connection: Any, host_id: str, state_gen: Any) -> bool:
    """An /observed push moves the generation by exactly one. Anything else (a push lost in
    between, a host that restarted its count) is NOT stored: the host's next poll then
    mismatches and answers {resync: true}, and the full map repairs whatever was missed."""
    if state_gen is None:
        return True
    moved = connection.execute(text(
        "UPDATE agent_hosts SET replica_state_gen = :g WHERE id = :h "
        "AND replica_state_gen = :g - 1 RETURNING 1"),
        {"g": int(state_gen), "h": host_id}).first()
    return bool(moved)


def put_replicas(connection: Any, host: fleet_ca.HostIdentity, *, state_gen: int,
                 replicas: list) -> dict:
    """PUT /v1/hosts/{id}/replicas: the host's FULL replica map (after a {resync: true}). Every
    listed replica's state is stored; a replica of this host it no longer lists is `gone`; the
    generation is stored, so the next poll claims (§3.13.0 REVISION)."""
    _not_mine(connection, host.host_id, replicas)
    seen = _observe(connection, host.host_id, replicas)
    listed = [str(r.get("replica_id")) for r in replicas
              if isinstance(r, dict) and r.get("replica_id")]
    connection.execute(text(
        "UPDATE agent_replicas SET observed_state = 'gone', updated_at = now() "
        "WHERE host_id = :h AND observed_state <> 'gone' AND NOT (id = ANY(:seen))"),
        {"h": host.host_id, "seen": listed})
    _store_gen(connection, host.host_id, state_gen)
    return {"host_id": host.host_id, "replicas_seen": seen, "state_gen": int(state_gen),
            "_wake": _take_wake(connection)}


def observed(connection: Any, host: fleet_ca.HostIdentity, body: dict) -> dict:
    """Replica state changes, pushed by the host on every change (watchdog transitions too),
    with the host's bumped state_gen (§3.13.0 REVISION)."""
    _not_mine(connection, host.host_id, body.get("replicas") or ())
    in_step = _advance_gen(connection, host.host_id, body.get("state_gen"))
    seen = _observe(connection, host.host_id, body.get("replicas") or ())
    if body.get("observed_generation") is not None:
        connection.execute(text(
            "UPDATE agent_hosts SET observed_generation = GREATEST(observed_generation, :g) "
            "WHERE id = :i"), {"g": int(body["observed_generation"]), "i": host.host_id})
    return {"host_id": host.host_id, "replicas_seen": seen,
            **({} if in_step else {"resync": True}), "_wake": _take_wake(connection)}


#: The kinds boss-hostd reports (HOST_API.md §2.7), recorded as host_events rows.
EVENT_KINDS = ("vm_state", "restart", "crashloop", "handle_replay", "refused", "adopted",
               "orphan_tap_removed")
EVENT_ACTORS = ("watchdog", "gateway", "reconciler")


def host_events_ingest(connection: Any, host: fleet_ca.HostIdentity, events: list) -> dict:
    """POST /v1/hosts/{id}/events: the watchdog's and gateway's detail (crashloops, handle
    replays) as host_events rows, so the trace and the Fleet doctor see them (§3.1)."""
    if not isinstance(events, list):
        raise FleetError("`events` is a list.")
    if len(events) > GUEST_CALL_BATCH_MAX:
        raise FleetError(f"at most {GUEST_CALL_BATCH_MAX} events per batch.", status=413)
    accepted, rejected = 0, []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("kind") not in EVENT_KINDS:
            rejected.append({"index": index, "reason": "kind_unknown"})
            continue
        if any(isinstance(v, str) and _HANDLE.search(v) for v in
               [*event.values(), *((event.get("detail") or {}).values()
                                   if isinstance(event.get("detail"), dict) else [])]):
            rejected.append({"index": index, "reason": "raw_handle_refused"})
            continue
        detail = {str(k)[:60]: str(v)[:300] for k, v in (event.get("detail") or {}).items()} \
            if isinstance(event.get("detail"), dict) else {}
        detail.update({k: str(event[k])[:60] for k in ("from", "to", "at") if event.get(k)})
        actor = event.get("actor") if event.get("actor") in EVENT_ACTORS else "watchdog"
        host_event(connection, org_id=host.org_id, host_id=host.host_id,
                   kind=str(event["kind"]), detail=detail, actor=actor,
                   vm_id=_clean(event.get("vm_id"), 120))
        accepted += 1
    return {"accepted": accepted, "rejected": rejected}


# ── Desired state ───────────────────────────────────────────────────────────────────────────

def desired(connection: Any, host_id: str) -> dict:
    """The host's whole desired state (level-based): every VM it should run, and every VM it
    still runs that it should stop. Never a credential: the guest's handles arrive per run over
    vsock (P8b), the replica's run-plane identity is the host's."""
    host = connection.execute(text(
        "SELECT id, org_id, status, desired_generation, vm_prefix, revoked_at FROM agent_hosts "
        "WHERE id = :i"), {"i": host_id}).mappings().first()
    if host is None:
        raise FleetError("no such host.", status=404, code="not_found")
    rows = connection.execute(text("""
        SELECT r.id, r.vm_id, r.vm_mode, r.vm_ip, r.image_digest, r.agent_version, r.slots,
               r.desired_state, r.observed_state, r.generation, r.rs_weight, r.deployment_id,
               d.project_id, d.agent_key, d.environment, d.resources, d.vip, d.vip_ports,
               d.hostname, d.routing, d.strict_secrets
          FROM agent_replicas r JOIN agent_deployments d ON d.id = r.deployment_id
         WHERE r.host_id = :h AND (r.desired_state = 'running'
               OR r.observed_state NOT IN ('stopped', 'gone'))
         ORDER BY r.created_at, r.id"""), {"h": host_id}).mappings().all()
    vms = [{"replica_id": r["id"], "vm_id": r["vm_id"], "vm_mode": r["vm_mode"],
            "state": r["desired_state"], "deployment_id": r["deployment_id"],
            "project_id": r["project_id"], "agent_key": r["agent_key"],
            "environment": r["environment"],
            # A string on the wire (docs/infra/HOST_API.md §2.5: Go's DesiredVM.AgentVersion).
            "agent_version": str(r["agent_version"]) if r["agent_version"] is not None else None,
            "image_digest": r["image_digest"], "resources": _json(r["resources"], {}),
            "slots": int(r["slots"]), "weight": int(r["rs_weight"]),
            "vm_ip": str(r["vm_ip"]) if r["vm_ip"] is not None else None,
            "vip": str(r["vip"]) if r["vip"] is not None else None,
            "vip_ports": list(r["vip_ports"] or []), "hostname": r["hostname"],
            "routing": r["routing"], "strict_secrets": bool(r["strict_secrets"]),
            "spiffe_id": fleet_ca.vm_spiffe_id(host_id, r["vm_id"]),
            "generation": int(r["generation"])} for r in rows]
    serving = host["revoked_at"] is None and host["status"] not in ("retired",)
    from . import fleet_directors

    directors, push = fleet_directors.for_host_desired(connection, str(host["org_id"]))
    return {"host_id": host_id, "generation": int(host["desired_generation"]),
            "status": host["status"], "vm_prefix": str(host["vm_prefix"] or ""),
            "vms": vms if serving else [{**v, "state": "stopped"} for v in vms],
            "pools": [], "endpoints": endpoints(host_id), "fences": [], "revocations": [],
            # P8b-10: the directors' addresses (GRE sources the tap policy accepts) and their
            # mTLS withdraw-push listeners.
            "directors": directors, "director_push": push}


async def watch(engine: Any, host_id: str, *, since: int, wait: float = 0.0) -> dict:
    """GET …/desired?since=g&wait=s: the whole desired state as soon as the host's generation is
    past `since` (at once when it already is: a reconnect with since=0 is a full resync), or
    `{"changed": false, "generation": g}` after `wait` seconds (long-poll, at most 30).

    The wait is an asyncio wait on THIS process's waiters (run_plane.WAITERS), never a thread
    and never a per-waiter query: a reconcile, revoke or cordon wakes it after its commit, and
    one generation sweep per process (a single query for every waiting host) catches a change
    another process made."""
    from starlette.concurrency import run_in_threadpool

    from .run_plane import WAITERS

    def read() -> tuple:
        with engine.begin() as c:
            gen = c.execute(text("SELECT desired_generation FROM agent_hosts WHERE id = :i"),
                            {"i": host_id}).scalar()
            if gen is None:
                raise FleetError("no such host.", status=404, code="not_found")
            return int(gen), (desired(c, host_id) if int(gen) > int(since or 0) else None)

    timeout = max(0.0, min(float(wait or 0), WATCH_MAX_SECONDS))
    waiter = WAITERS.register_desired(engine, host_id, int(since or 0)) if timeout else None
    try:
        gen, state = await run_in_threadpool(read)
        if state is not None:
            return {"changed": True, **state}
        if waiter is not None and await waiter.wait(timeout):
            gen, state = await run_in_threadpool(read)
            if state is not None:
                return {"changed": True, **state}
        return {"changed": False, "host_id": host_id, "generation": gen}
    finally:
        if waiter is not None:
            WAITERS.unregister(waiter)


# ── Guest-call audit ────────────────────────────────────────────────────────────────────────

def _clean(value: Any, limit: int = 300) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:limit]


def ingest_guest_calls(connection: Any, host: fleet_ca.HostIdentity, calls: list) -> dict:
    """One batch of guest_calls rows (FR-13). A row that carries anything handle-shaped is
    refused whole: the audit holds a handle's fingerprint, never the handle (§3.19)."""
    if not isinstance(calls, list):
        raise FleetError("`calls` is a list.")
    if len(calls) > GUEST_CALL_BATCH_MAX:
        raise FleetError(f"at most {GUEST_CALL_BATCH_MAX} calls per batch.", status=413)
    accepted = duplicates = 0
    rejected: list = []
    for index, call in enumerate(calls):
        reason = None
        if not isinstance(call, dict) or not call.get("call_id"):
            reason = "call_id_missing"
        elif call.get("route") not in ROUTES:
            reason = "route_unknown"
        elif any(isinstance(v, str) and _HANDLE.search(v) for v in call.values()):
            reason = "raw_handle_refused"
        elif call.get("verdict", "allow") not in ("allow", "deny"):
            reason = "verdict_unknown"
        if reason:
            rejected.append({"index": index, "reason": reason})
            continue
        try:
            at = datetime.fromisoformat(str(call.get("at")).replace("Z", "+00:00")) \
                if call.get("at") else _now()
        except ValueError:
            rejected.append({"index": index, "reason": "at_unreadable"})
            continue
        row = connection.execute(text("""
            INSERT INTO guest_calls (org_id, host_id, call_id, at, vm_id, run_id, epoch,
                deployment_id, route, dest_host, method, path_template, status, bytes_in,
                bytes_out, duration_ms, handle_fp, verdict, rule_id, rejected_by, trace_id)
            VALUES (:o, :h, :c, :at, :vm, :run, :ep, :dep, :route, :dest, :m, :path, :st, :bi,
                    :bo, :ms, :fp, :v, :rule, :rej, :trace)
            ON CONFLICT (host_id, call_id) DO NOTHING RETURNING id"""),
            {"o": host.org_id, "h": host.host_id, "c": _clean(call["call_id"], 120), "at": at,
             "vm": _clean(call.get("vm_id"), 120), "run": _clean(call.get("run_id"), 120),
             "ep": int(call["epoch"]) if call.get("epoch") is not None else None,
             "dep": _clean(call.get("deployment_id"), 120), "route": call["route"],
             "dest": _clean(call.get("dest_host"), 255), "m": _clean(call.get("method"), 12),
             "path": _clean(call.get("path_template"), 500),
             "st": int(call["status"]) if call.get("status") is not None else None,
             "bi": max(0, int(call.get("bytes_in") or 0)),
             "bo": max(0, int(call.get("bytes_out") or 0)),
             "ms": max(0, int(call.get("duration_ms") or 0)),
             "fp": _clean(call.get("handle_fp"), 128), "v": call.get("verdict") or "allow",
             "rule": _clean(call.get("rule_id"), 120), "rej": _clean(call.get("rejected_by"), 120),
             "trace": _clean(call.get("trace_id"), 64)}).first()
        if row:
            accepted += 1
        else:
            duplicates += 1
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


# ── Reads (console) ─────────────────────────────────────────────────────────────────────────

_HOST_COLUMNS = ("id, org_id, pool, hostname, labels, backend, status, vm_prefix, kvm_live, "
                 "firecracker_live, jailer_on, seccomp_default, smt_off, ksm_off, swap_off, "
                 "strict_secrets, net_policy_ok, capacity, allocatable, pressure, "
                 "last_heartbeat_at, desired_generation, observed_generation, cert_not_after, "
                 "enrolled_at, revoked_at, host(address) AS address")


def _host_out(row: Any) -> dict:
    out = dict(row)
    for k in ("labels", "capacity", "allocatable", "pressure"):
        out[k] = _json(out.get(k), {})
    for k in ("last_heartbeat_at", "cert_not_after", "enrolled_at", "revoked_at"):
        out[k] = out[k].isoformat() if out.get(k) else None
    out["vm_prefix"] = str(out["vm_prefix"]) if out.get("vm_prefix") is not None else None
    out["address"] = str(out["address"]) if out.get("address") else None
    return out


def list_hosts(connection: Any, org_id: str) -> list:
    rows = connection.execute(text(
        f"SELECT {_HOST_COLUMNS} FROM agent_hosts WHERE org_id = :o ORDER BY enrolled_at"),
        {"o": org_id}).mappings().all()
    return [_host_out(r) for r in rows]


def get_host(connection: Any, org_id: str, host_id: str) -> Optional[dict]:
    row = connection.execute(text(
        f"SELECT {_HOST_COLUMNS} FROM agent_hosts WHERE org_id = :o AND id = :i"),
        {"o": org_id, "i": host_id}).mappings().first()
    if row is None:
        return None
    out = _host_out(row)
    out["replicas"] = [dict(r) for r in connection.execute(text(
        "SELECT id, deployment_id, vm_id, vm_mode, desired_state, observed_state, busy, slots, "
        "agent_version FROM agent_replicas WHERE host_id = :i ORDER BY created_at"),
        {"i": host_id}).mappings().all()]
    return out


__all__ = ["ATTESTATION", "FleetError", "HEARTBEAT_SECONDS", "JOIN_TOKEN_PREFIX", "ROUTES",
           "UNREACHABLE_SECONDS", "desired", "endpoints", "enrol", "get_host", "heartbeat",
           "host_event", "host_events_ingest", "ingest_guest_calls", "issue_join_token", "list_hosts", "observed",
           "put_replicas", "renew", "revoke", "revoke_join_token", "set_cordon", "watch"]
