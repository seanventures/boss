"""THE IPVS DIRECTORS' CONTROL-PLANE SIDE (P8b-10, BOSSAPP-1077; boss-agent-hosts-lld.md §3.14.2,
§3.14.4; docs/infra/HOST_API.md §9).

A director (`boss-lbd`) enrols through the host enrol path with a join token issued for
`role: director`, heartbeats every 2 s, and long-polls ITS desired state: every VIP the pair
fronts with that VIP's SERVING real servers (`agent_replicas.observed_state = 'serving'`,
`vm_ip`, `rs_weight`), one static route per host (`vm_prefix` via the host's `address`), the
VIP pools, and the VRRP grouping both directors must agree on (computed here, once, so A and B
never disagree about who is MASTER of a group).

  * DIRECTOR LEASE (§3.14.4): per (org, VRRP group), 5 s TTL. A heartbeat asks for the lease of
    every group its keepalived is MASTER of; it gets it when free, expired or its own. A MASTER
    whose group's lease is another live director's goes FAULT (boss-lbd's track script), which
    ends a split brain in at most one heartbeat.
  * DESIRED GENERATION: every director of an org moves to a new generation when a replica
    enters or leaves `serving` (fleet._observe), the scheduler changes a host's set, a VIP is
    allocated, or a director/host enrols or changes address — the long-poll wakes.
  * VIP ALLOCATION (D8): the lowest free address of the org's `vip_pool` rows (routing ipvs)
    is given to a production microVM deployment the first time the scheduler places it.
    `vip_ports` defaults to [443]. The VIP returns to the pool when the deployment is retired.
"""
from __future__ import annotations

import ipaddress
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import fleet_ca
from .core import make_id

LEASE_SECONDS = 5
GROUP_SIZE = 50          # VIPs per VRRP instance (LLD §3.14.2 "about 50")
VRID_BASE = 1
MAX_GROUPS = 255 - VRID_BASE
DEFAULT_VIP_PORTS = [443]
PUSH_PORT = 7446
UNREACHABLE_SECONDS = 10


class DirectorError(ValueError):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.status, self.code = status, code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


# ── Grouping (the shared rule; boss-lbd has the same in lbd.Group) ──────────────────────────

def group_vips(vips: Iterable[str], director_ids: Iterable[str]) -> list:
    """VIPs sorted and chunked by GROUP_SIZE; group i is MASTER on sorted directors[i % n]."""
    sorted_vips = sorted({v for v in vips if v})
    ids = sorted({d for d in director_ids if d})
    out = []
    for i in range(0, min(len(sorted_vips), GROUP_SIZE * MAX_GROUPS), GROUP_SIZE):
        g = i // GROUP_SIZE
        out.append({"group": g, "vrid": VRID_BASE + g, "master": ids[g % len(ids)] if ids else "",
                    "vips": sorted_vips[i:i + GROUP_SIZE]})
    return out


def scheduler_for(members: list) -> str:
    slots = {int(m.get("slots") or 0) for m in members}
    return "wlc" if len(slots) > 1 else "rr"


# ── Enrolment, renewal, retirement ───────────────────────────────────────────────────────────

def enrol(connection: Any, *, token: dict, csr_pem: str, hostname: str, address: Optional[str],
          labels: Optional[dict]) -> dict:
    """Spend a director join token: a director_nodes row and a 24 h certificate (client AND
    server auth: it calls the control plane and serves host pushes)."""
    org_id = str(token["org_id"])
    addr = _ipv4(address)
    director_id = make_id("dir")
    merged = {**_json(token["labels"], {}), **(labels or {}), "role": "director"}
    connection.execute(text("""
        INSERT INTO director_nodes (id, org_id, hostname, address, labels, status, last_heartbeat_at)
        VALUES (:i, :o, :hn, CAST(:a AS INET), CAST(:l AS JSONB), 'ready', now())"""),
        {"i": director_id, "o": org_id, "hn": (hostname or "")[:200], "a": addr,
         "l": json.dumps(merged)})
    try:
        issued = fleet_ca.issue_director_cert(connection, org_id=org_id, director_id=director_id,
                                              csr_pem=csr_pem, address=addr)
    except fleet_ca.FleetAuthError as exc:
        raise DirectorError(f"the CSR was refused: {exc.code}", status=400, code=exc.code) from exc
    connection.execute(text(
        "UPDATE director_nodes SET cert_fingerprint = :f, cert_not_after = :na WHERE id = :i"),
        {"f": issued.fingerprint, "na": issued.not_after, "i": director_id})
    connection.execute(text(
        "UPDATE agent_host_join_tokens SET used_at = now(), used_by_host = :h WHERE id = :i"),
        {"h": director_id, "i": token["id"]})
    from .fleet import host_event

    host_event(connection, org_id=org_id, host_id=director_id, kind="director_enrolled",
               detail={"address": addr, "join_token_id": token["id"]}, actor="director")
    bump(connection, org_id)  # the peer learns of its new partner; hosts learn the new GRE source
    _bump_hosts(connection, org_id)
    return {"host_id": director_id, "director_id": director_id, "org_id": org_id,
            "certificate_pem": issued.cert_pem,
            "certificate_not_after": issued.not_after.isoformat(),
            "renew_after": fleet_ca.renew_after(issued).isoformat(),
            "svid_intermediate_pem": None, "ca_pem": fleet_ca.ca_pem(connection),
            "trust_domain": fleet_ca.trust_domain(), "heartbeat_seconds": 2, "role": "director",
            "endpoints": endpoints(director_id)}


def endpoints(director_id: str) -> dict:
    return {"heartbeat": f"/v1/directors/{director_id}/heartbeat",
            "desired": f"/v1/directors/{director_id}/desired",
            "renew": f"/v1/directors/{director_id}/renew"}


def renew(connection: Any, director: fleet_ca.HostIdentity, *, csr_pem: str) -> dict:
    row = connection.execute(text(
        "SELECT status, revoked_at, host(address) AS address FROM director_nodes WHERE id = :i "
        "FOR UPDATE"), {"i": director.host_id}).mappings().first()
    if row is None or row["revoked_at"] is not None or row["status"] == "retired":
        raise DirectorError("this director is revoked.", status=403, code="host_revoked")
    try:
        issued = fleet_ca.issue_director_cert(connection, org_id=director.org_id,
                                              director_id=director.host_id, csr_pem=csr_pem,
                                              address=row["address"])
    except fleet_ca.FleetAuthError as exc:
        raise DirectorError(f"the CSR was refused: {exc.code}", status=400, code=exc.code) from exc
    connection.execute(text(
        "UPDATE director_nodes SET cert_fingerprint = :f, cert_not_after = :na, updated_at = now() "
        "WHERE id = :i"), {"f": issued.fingerprint, "na": issued.not_after, "i": director.host_id})
    return {"host_id": director.host_id, "certificate_pem": issued.cert_pem,
            "certificate_not_after": issued.not_after.isoformat(),
            "renew_after": fleet_ca.renew_after(issued).isoformat(),
            "svid_intermediate_pem": None, "ca_pem": fleet_ca.ca_pem(connection)}


def retire(connection: Any, *, org_id: str, director_id: str, actor: str, reason: str = "") -> dict:
    """Retire a director: its certificate and every later call are refused; its leases lapse;
    the peer becomes MASTER of everything at the next desired state."""
    row = connection.execute(text(
        "UPDATE director_nodes SET revoked_at = now(), revoked_reason = :r, status = 'retired', "
        "updated_at = now() WHERE id = :i AND org_id = :o AND revoked_at IS NULL RETURNING id"),
        {"r": reason[:500], "i": director_id, "o": org_id}).first()
    if row is None:
        raise DirectorError("no such live director in this organisation.", status=404,
                            code="not_found")
    connection.execute(text(
        "UPDATE agent_certs SET revoked_at = now() WHERE host_id = :i AND revoked_at IS NULL"),
        {"i": director_id})
    connection.execute(text("DELETE FROM director_leases WHERE holder = :i"), {"i": director_id})
    from .fleet import host_event

    host_event(connection, org_id=org_id, host_id=director_id, kind="director_retired",
               detail={"reason": reason[:500]}, actor=actor)
    bump(connection, org_id)
    _bump_hosts(connection, org_id)
    return {"director_id": director_id, "retired": True}


def _ipv4(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(str(value))
    except ValueError as exc:
        raise DirectorError(f"address {value!r} is not an IP address.") from exc
    if ip.version != 4:
        raise DirectorError("directors need an IPv4 address (GRE source, VRRP unicast).")
    return str(ip)


# ── Heartbeat and lease ───────────────────────────────────────────────────────────────────────

def heartbeat(connection: Any, director: fleet_ca.HostIdentity, body: dict) -> dict:
    """One heartbeat: liveness, address, IPVS/keepalived facts, and the lease of every group
    this node is MASTER of. Answers every group's holder (5 s TTL)."""
    addr = _ipv4(body.get("address")) if body.get("address") else None
    row = connection.execute(text("""
        UPDATE director_nodes SET last_heartbeat_at = now(), updated_at = now(),
               status = CASE WHEN status = 'unreachable' THEN 'ready' ELSE status END,
               address = COALESCE(CAST(:a AS INET), address),
               keepalived_live = :kl,
               ipvs_state = CAST(:ipvs AS JSONB),
               groups_master = CAST(:gm AS JSONB),
               observed_generation = GREATEST(observed_generation, :og)
         WHERE id = :i AND revoked_at IS NULL
        RETURNING desired_generation, cert_not_after, status, org_id,
                  (host(address) IS DISTINCT FROM :a) AS address_changed"""),
        {"a": addr, "kl": bool(body.get("keepalived_live")),
         "ipvs": json.dumps(body.get("ipvs") if isinstance(body.get("ipvs"), dict) else {}),
         "gm": json.dumps([int(g) for g in (body.get("groups_master") or [])]),
         "og": int(body.get("observed_generation") or 0), "i": director.host_id}
    ).mappings().first()
    if row is None:
        raise DirectorError("this director is revoked.", status=403, code="host_revoked")
    org_id = str(row["org_id"])
    masters = [int(g) for g in (body.get("groups_master") or [])]
    for g in masters:
        connection.execute(text("""
            INSERT INTO director_leases (org_id, vrrp_group, holder, expires_at)
            VALUES (:o, :g, :me, now() + make_interval(secs => :ttl))
            ON CONFLICT (org_id, vrrp_group) DO UPDATE
               SET holder = :me, expires_at = now() + make_interval(secs => :ttl)
             WHERE director_leases.holder = :me OR director_leases.expires_at < now()"""),
            {"o": org_id, "g": g, "me": director.host_id, "ttl": LEASE_SECONDS})
    # No longer MASTER of a group: release its lease now, so the peer (held in FAULT while we
    # hold it) may take the group without waiting out the TTL.
    connection.execute(text(
        "DELETE FROM director_leases WHERE org_id = :o AND holder = :me "
        "AND NOT (vrrp_group = ANY(:m))"), {"o": org_id, "me": director.host_id, "m": masters})
    leases = {str(r[0]): str(r[1]) for r in connection.execute(text(
        "SELECT vrrp_group, holder FROM director_leases WHERE org_id = :o AND expires_at > now()"),
        {"o": org_id}).all()}
    renew_due = bool(row["cert_not_after"] and row["cert_not_after"] - _now() <= timedelta(
        hours=fleet_ca.HOST_CERT_HOURS * (1 - fleet_ca.RENEW_FRACTION)))
    return {"director_id": director.host_id, "desired_generation": int(row["desired_generation"]),
            "leases": leases, "renew": renew_due, "heartbeat_seconds": 2, "status": row["status"]}


def mark_unreachable(connection: Any, org_id: Optional[str] = None) -> list:
    """Directors that stopped beating: `unreachable` (their leases simply expire)."""
    rows = connection.execute(text("""
        UPDATE director_nodes SET status = 'unreachable', updated_at = now()
         WHERE status = 'ready' AND revoked_at IS NULL
           AND (last_heartbeat_at IS NULL
                OR last_heartbeat_at < now() - make_interval(secs => :s))
           AND (CAST(:o AS TEXT) IS NULL OR org_id = :o)
        RETURNING id"""), {"s": UNREACHABLE_SECONDS, "o": org_id}).all()
    return [r[0] for r in rows]


# ── Desired state ─────────────────────────────────────────────────────────────────────────────

def live_directors(connection: Any, org_id: str) -> list:
    """The org's directors that may carry traffic (not retired), id order = side order."""
    return [dict(r) for r in connection.execute(text("""
        SELECT id, host(address) AS address, status FROM director_nodes
         WHERE org_id = :o AND revoked_at IS NULL AND status <> 'retired' ORDER BY id"""),
        {"o": org_id}).mappings().all()]


def vip_pools(connection: Any, org_id: str) -> list:
    return [str(r[0]) for r in connection.execute(text(
        "SELECT cidr FROM vip_pool WHERE org_id = :o AND routing = 'ipvs' ORDER BY cidr"),
        {"o": org_id}).all()]


def desired(connection: Any, director_id: str) -> dict:
    """The director's whole desired state (level-based). Members are SERVING replicas only."""
    me = connection.execute(text(
        "SELECT id, org_id, status, desired_generation, revoked_at, host(address) AS address "
        "FROM director_nodes WHERE id = :i"), {"i": director_id}).mappings().first()
    if me is None:
        raise DirectorError("no such director.", status=404, code="not_found")
    org_id = str(me["org_id"])
    directors = live_directors(connection, org_id)
    sides = {d["id"]: chr(ord("A") + i) for i, d in enumerate(directors)}
    peers = [{"id": d["id"], "address": d["address"], "side": sides[d["id"]], "port": PUSH_PORT}
             for d in directors]
    self_peer = next((p for p in peers if p["id"] == director_id),
                     {"id": director_id, "address": me["address"], "side": "", "port": PUSH_PORT})
    others = [p for p in peers if p["id"] != director_id]
    rows = connection.execute(text("""
        SELECT d.id AS deployment_id, host(d.vip) AS vip, d.vip_ports,
               r.id AS replica_id, host(r.vm_ip) AS vm_ip, r.host_id, r.rs_weight, r.slots,
               r.observed_state
          FROM agent_deployments d
          JOIN projects p ON p.id = d.project_id
          JOIN workspaces w ON w.id = p.workspace_id
          LEFT JOIN agent_replicas r ON r.deployment_id = d.id AND r.desired_state = 'running'
               AND r.vm_mode = 'replica' AND r.vm_ip IS NOT NULL
         WHERE w.org_id = :o AND d.vip IS NOT NULL AND d.status = 'live'
           AND d.runtime = 'microvm' AND d.routing = 'ipvs'
         ORDER BY d.vip, r.id"""), {"o": org_id}).mappings().all()
    vips: dict = {}
    for r in rows:
        entry = vips.setdefault(r["vip"], {"vip": r["vip"], "ports": list(r["vip_ports"] or DEFAULT_VIP_PORTS),
                                           "deployment_id": r["deployment_id"], "members": []})
        if r["replica_id"] and r["observed_state"] == "serving":
            entry["members"].append({"replica_id": r["replica_id"], "vm_ip": r["vm_ip"],
                                     "host_id": r["host_id"], "weight": int(r["rs_weight"]),
                                     "slots": int(r["slots"])})
    # STABLE groups (review of PR #120): a VIP keeps the group it was given at allocation, so
    # adding a VIP never moves another VIP's MASTER. A VIP without one (set by hand) is given the
    # lowest group with room, once.
    group_of: dict = {}
    for r in connection.execute(text("""
            SELECT host(d.vip), d.vrrp_group, d.id FROM agent_deployments d
              JOIN projects p ON p.id = d.project_id JOIN workspaces w ON w.id = p.workspace_id
             WHERE w.org_id = :o AND d.vip IS NOT NULL ORDER BY d.vip"""), {"o": org_id}).all():
        if r[0] not in vips:
            continue
        group_of[r[0]] = int(r[1]) if r[1] is not None else assign_group(connection, org_id, r[2])
    ids = [d["id"] for d in directors]
    groups = []
    for g in sorted(set(group_of.values())):
        members = sorted(v for v, gg in group_of.items() if gg == g)
        groups.append({"group": g, "vrid": VRID_BASE + g, "master": ids[g % len(ids)] if ids else "",
                       "vips": members})
    out_vips = []
    for vip in sorted(vips):
        v = vips[vip]
        v["scheduler"] = scheduler_for(v["members"])
        v["group"] = group_of.get(vip, 0)
        out_vips.append(v)
    hosts = [dict(r) for r in connection.execute(text("""
        SELECT id AS host_id, host(address) AS address, text(vm_prefix) AS vm_prefix
          FROM agent_hosts WHERE org_id = :o AND revoked_at IS NULL AND address IS NOT NULL
           AND vm_prefix IS NOT NULL ORDER BY id"""), {"o": org_id}).mappings().all()]
    serving = me["revoked_at"] is None and me["status"] != "retired"
    return {"director_id": director_id, "generation": int(me["desired_generation"]),
            "status": me["status"], "self": self_peer, "peer": others[0] if others else None,
            "directors": peers, "vip_pools": vip_pools(connection, org_id),
            "vips": out_vips if serving else [], "groups": groups if serving else [],
            "hosts": hosts}


async def watch(engine: Any, director_id: str, *, since: int, wait: float = 0.0) -> dict:
    """GET …/desired?since=g&wait=s for a director: fleet.watch's shape, director_nodes'
    generation (run_plane.WAITERS sweeps director_nodes for `dir_` ids)."""
    from starlette.concurrency import run_in_threadpool

    from .fleet import WATCH_MAX_SECONDS
    from .run_plane import WAITERS

    def read() -> tuple:
        with engine.begin() as c:
            gen = c.execute(text("SELECT desired_generation FROM director_nodes WHERE id = :i"),
                            {"i": director_id}).scalar()
            if gen is None:
                raise DirectorError("no such director.", status=404, code="not_found")
            return int(gen), (desired(c, director_id) if int(gen) > int(since or 0) else None)

    timeout = max(0.0, min(float(wait or 0), WATCH_MAX_SECONDS))
    waiter = WAITERS.register_desired(engine, director_id, int(since or 0)) if timeout else None
    try:
        gen, state = await run_in_threadpool(read)
        if state is not None:
            return {"changed": True, **state}
        if waiter is not None and await waiter.wait(timeout):
            gen, state = await run_in_threadpool(read)
            if state is not None:
                return {"changed": True, **state}
        return {"changed": False, "director_id": director_id, "generation": gen}
    finally:
        if waiter is not None:
            WAITERS.unregister(waiter)


def bump(connection: Any, org_id: str) -> list:
    """Every live director of the org moves to a new generation; returns the ids to wake."""
    return [r[0] for r in connection.execute(text(
        "UPDATE director_nodes SET desired_generation = desired_generation + 1, updated_at = now() "
        "WHERE org_id = :o AND revoked_at IS NULL RETURNING id"), {"o": org_id}).all()]


def _bump_hosts(connection: Any, org_id: str) -> list:
    """The hosts' desired state carries the director list (GRE sources, push targets)."""
    return [r[0] for r in connection.execute(text(
        "UPDATE agent_hosts SET desired_generation = desired_generation + 1, updated_at = now() "
        "WHERE org_id = :o AND revoked_at IS NULL RETURNING id"), {"o": org_id}).all()]


def for_host_desired(connection: Any, org_id: str) -> tuple:
    """What a host's desired state says about directors: the GRE source addresses and the
    withdraw-push endpoints (HOST_API.md §2.5 `directors`, `director_push`)."""
    directors = [d for d in live_directors(connection, org_id) if d["address"]]
    return ([d["address"] for d in directors],
            [{"id": d["id"], "address": d["address"], "port": PUSH_PORT} for d in directors])


# ── Host pushes: the durable record ───────────────────────────────────────────────────────────

def vip_member(connection: Any, host: fleet_ca.HostIdentity, *, vip: str, replica_id: str,
               method: str, vm_ip: Optional[str], weight: Optional[int], reason: str = "",
               seq: Optional[int] = None) -> dict:
    """PUT/DELETE /v1/vips/{vip}/members/{replica}: the host's own replica only, whose
    deployment owns that VIP. PUT records the weight (0 = drained), DELETE weight 0; the serving
    state itself comes from /observed. Directors are woken either way."""
    row = connection.execute(text("""
        SELECT r.id, r.org_id, host(d.vip) AS vip, host(r.vm_ip) AS vm_ip
          FROM agent_replicas r JOIN agent_deployments d ON d.id = r.deployment_id
         WHERE r.id = :r AND r.host_id = :h"""), {"r": replica_id, "h": host.host_id}
    ).mappings().first()
    if row is None:
        raise DirectorError("that replica is not on this host.", status=403,
                            code="replica_not_on_host")
    if row["vip"] != vip:
        raise DirectorError("that replica's deployment does not own this VIP.", status=409,
                            code="vip_mismatch")
    if vm_ip and row["vm_ip"] and str(vm_ip) != str(row["vm_ip"]):
        raise DirectorError("vm_ip does not match the replica's.", status=409, code="vm_ip_mismatch")
    w = 0 if method == "DELETE" else max(0, int(weight or 0))
    # Latest decision wins (the host's sequence): an older push that arrives late changes nothing.
    applied = connection.execute(text(
        "UPDATE agent_replicas SET rs_weight = :w, rs_seq = GREATEST(rs_seq, :s), updated_at = now() "
        "WHERE id = :r AND (:s = 0 OR :s >= rs_seq) RETURNING id"),
        {"w": w, "r": replica_id, "s": int(seq or 0)}).first()
    if applied is None:
        return {"vip": vip, "replica_id": replica_id, "stale": True, "_wake": []}
    from .fleet import host_event

    host_event(connection, org_id=str(row["org_id"]), host_id=host.host_id,
               kind="vip_member_withdrawn" if w == 0 else "vip_member_registered",
               detail={"vip": vip, "replica_id": replica_id, "weight": w, "reason": reason[:120],
                       "method": method}, actor="host")
    return {"vip": vip, "replica_id": replica_id, "weight": w,
            "_wake": bump(connection, str(row["org_id"]))}


# ── VIP pools and allocation (D8) ─────────────────────────────────────────────────────────────

def add_vip_pool(connection: Any, *, org_id: str, cidr: str, routing: str = "ipvs",
                 actor: str = "operator") -> dict:
    try:
        net = ipaddress.ip_network(str(cidr), strict=True)
    except ValueError as exc:
        raise DirectorError(f"{cidr!r} is not a network.") from exc
    if routing not in ("ipvs", "bgp", "cloud_l4"):
        raise DirectorError("routing must be ipvs, bgp or cloud_l4.")
    pid = make_id("vpool")
    connection.execute(text("""
        INSERT INTO vip_pool (id, org_id, cidr, family, routing)
        VALUES (:i, :o, CAST(:c AS CIDR), :f, :r) ON CONFLICT (org_id, cidr) DO NOTHING"""),
        {"i": pid, "o": org_id, "c": str(net), "f": f"ipv{net.version}", "r": routing})
    from .fleet import host_event

    host_event(connection, org_id=org_id, host_id=None, kind="vip_pool_added",
               detail={"cidr": str(net), "routing": routing}, actor=actor)
    return {"cidr": str(net), "family": f"ipv{net.version}", "routing": routing}


def list_vip_pools(connection: Any, org_id: str) -> list:
    rows = connection.execute(text("""
        SELECT p.id, text(p.cidr) AS cidr, p.family, p.routing, p.created_at,
               (SELECT count(*) FROM agent_deployments d JOIN projects pr ON pr.id = d.project_id
                  JOIN workspaces w ON w.id = pr.workspace_id
                 WHERE w.org_id = p.org_id AND d.vip <<= p.cidr) AS allocated
          FROM vip_pool p WHERE p.org_id = :o ORDER BY p.cidr"""), {"o": org_id}).mappings().all()
    return [{**dict(r), "created_at": r["created_at"].isoformat(), "allocated": int(r["allocated"])}
            for r in rows]


def allocate_vip(connection: Any, *, org_id: str, deployment_id: str) -> Optional[str]:
    """The lowest free VIP of the org's ipvs pools for a deployment without one. None when the
    org has no pool (or it is full): the deployment then serves internal callers only."""
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                       {"k": f"boss.fleet.vip:{org_id}"})
    used = {str(r[0]) for r in connection.execute(text("""
        SELECT host(d.vip) FROM agent_deployments d JOIN projects p ON p.id = d.project_id
          JOIN workspaces w ON w.id = p.workspace_id WHERE w.org_id = :o AND d.vip IS NOT NULL"""),
        {"o": org_id}).all()}
    for cidr in vip_pools(connection, org_id):
        net = ipaddress.ip_network(cidr)
        if net.version != 4:
            continue  # the IPVS directors (GRE, VRRP) are IPv4 only
        candidates = net.hosts() if net.prefixlen < 31 else iter(net)
        for address in candidates:
            if str(address) in used:
                continue
            done = connection.execute(text("""
                UPDATE agent_deployments SET vip = CAST(:v AS INET),
                       vip_ports = COALESCE(vip_ports, CAST(:ports AS INTEGER[]))
                 WHERE id = :d AND vip IS NULL RETURNING id"""),
                {"v": str(address), "ports": DEFAULT_VIP_PORTS, "d": deployment_id}).first()
            if done is None:
                # It already has one (or it is gone): answer what it holds, never a phantom.
                held = connection.execute(text(
                    "SELECT host(vip) FROM agent_deployments WHERE id = :d"),
                    {"d": deployment_id}).scalar()
                return str(held) if held else None
            assign_group(connection, org_id, deployment_id)
            from .fleet import host_event

            host_event(connection, org_id=org_id, host_id=None, kind="vip_allocated",
                       detail={"deployment_id": deployment_id, "vip": str(address)},
                       actor="scheduler")
            return str(address)
    return None


def assign_group(connection: Any, org_id: str, deployment_id: str) -> int:
    """The lowest VRRP group with fewer than GROUP_SIZE VIPs (stable: never recomputed)."""
    counts = {int(r[0]): int(r[1]) for r in connection.execute(text("""
        SELECT d.vrrp_group, count(*) FROM agent_deployments d JOIN projects p ON p.id = d.project_id
          JOIN workspaces w ON w.id = p.workspace_id
         WHERE w.org_id = :o AND d.vip IS NOT NULL AND d.vrrp_group IS NOT NULL
         GROUP BY d.vrrp_group"""), {"o": org_id}).all()}
    group = next(g for g in range(MAX_GROUPS + 1) if counts.get(g, 0) < GROUP_SIZE)
    connection.execute(text("UPDATE agent_deployments SET vrrp_group = :g WHERE id = :d"),
                       {"g": group, "d": deployment_id})
    return group


def release_vip(connection: Any, *, deployment_id: str) -> Optional[str]:
    """The VIP goes back to the pool (the deployment retired or left `microvm`)."""
    row = connection.execute(text(
        "UPDATE agent_deployments SET vip = NULL, vrrp_group = NULL WHERE id = :d "
        "AND vip IS NOT NULL RETURNING host(vip)"), {"d": deployment_id}).first()
    if row:
        from .fleet import host_event

        org = connection.execute(text(
            "SELECT w.org_id FROM agent_deployments d JOIN projects p ON p.id = d.project_id "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE d.id = :d"),
            {"d": deployment_id}).scalar()
        if org:
            host_event(connection, org_id=str(org), host_id=None, kind="vip_released",
                       detail={"deployment_id": deployment_id, "vip": str(row[0])},
                       actor="scheduler")
    return str(row[0]) if row else None


# ── Console ───────────────────────────────────────────────────────────────────────────────────

def list_directors(connection: Any, org_id: str) -> list:
    rows = connection.execute(text("""
        SELECT id, hostname, host(address) AS address, labels, status, keepalived_live,
               ipvs_state, groups_master, desired_generation, observed_generation,
               last_heartbeat_at, cert_not_after, enrolled_at, revoked_at
          FROM director_nodes WHERE org_id = :o ORDER BY id"""), {"o": org_id}).mappings().all()
    leases = {str(r[0]): str(r[1]) for r in connection.execute(text(
        "SELECT vrrp_group, holder FROM director_leases WHERE org_id = :o AND expires_at > now()"),
        {"o": org_id}).all()}
    out = []
    for r in rows:
        d = dict(r)
        for k in ("labels", "ipvs_state"):
            d[k] = _json(d.get(k), {})
        d["groups_master"] = _json(d.get("groups_master"), [])
        for k in ("last_heartbeat_at", "cert_not_after", "enrolled_at", "revoked_at"):
            d[k] = d[k].isoformat() if d.get(k) else None
        d["leases_held"] = sorted(int(g) for g, h in leases.items() if h == d["id"])
        out.append(d)
    return out


__all__ = ["DEFAULT_VIP_PORTS", "DirectorError", "GROUP_SIZE", "LEASE_SECONDS", "PUSH_PORT",
           "add_vip_pool", "allocate_vip", "bump", "desired", "enrol", "for_host_desired",
           "group_vips", "heartbeat", "list_directors", "list_vip_pools", "live_directors",
           "mark_unreachable", "release_vip", "assign_group", "renew", "retire", "scheduler_for", "vip_member",
           "watch"]
