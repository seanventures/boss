"""THE FLEET SCHEDULER AND RECONCILER v1 (P8a, BOSSAPP-1063; boss-agent-hosts-lld.md §3.14.1).

Turns each live `runtime: microvm` deployment into replica rows on enrolled hosts, and each host's
replica set into a DESIRED STATE with a generation counter (services/fleet.desired):

  * PRODUCTION: hard anti-affinity — at most one replica of a deployment per host. With fewer
    eligible hosts than `replicas`, the deployment is `degraded`; replicas are NEVER silently
    co-located (NFR-1).
  * dev / staging: soft anti-affinity — spread first, then the least-loaded host.
  * eligible host: same org, `ready`, beaten within UNREACHABLE_SECONDS, not revoked, in the
    deployment's pool (`default` when it names none), carrying its labels, of its backend when
    it names one.
  * a host silent for more than UNREACHABLE_SECONDS turns `unreachable`; its replicas are stopped
    there and placed elsewhere (the run they held resumes from its checkpoint when its lease
    expires — fencing makes the move safe, §2.14.2).
  * `vm_mode: per_run` places nothing while BOSS_MICROVM_CLAIMS_FROZEN (operator decision
    2026-11-14: built, off by default); `replica` is the activated mode.

LEADER-SAFE: one reconcile at a time across every API process, through a transaction-scoped
`pg_try_advisory_xact_lock` — a second caller returns `{"skipped": "busy"}` at once.
Every change to a host's set bumps that host's `desired_generation`, which wakes its watch.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import time
import uuid
from typing import Any, Optional

from sqlalchemy import text

from .fleet import UNREACHABLE_SECONDS, host_event

LOCK_KEY = int.from_bytes(hashlib.sha256(b"boss.fleet.scheduler").digest()[:8], "big",
                          signed=True)
DEFAULT_SLOTS = 4
_LAST_TRY = {"at": 0.0}


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def frozen(settings: Any = None) -> bool:
    from .sandbox_provider import microvm_claims_are_frozen

    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    return microvm_claims_are_frozen(settings)


def _eligible(host: dict, dep: dict, load: Optional[dict] = None) -> bool:
    """§3.14.1 "filter by pool, labels, KVM live, image presence and cordon state", plus the
    host's own capacity."""
    placement = _json(dep.get("placement"), {})
    if host["org_id"] != dep["org_id"] or host["status"] != "ready" or host["revoked_at"]:
        return False  # cordoned, draining, unreachable, retired or another org's
    # A host serves exactly its pool: a deployment that names none runs in `default` (§3.14.1
    # "filter by pool") — never spilling onto a pool somebody set aside.
    if (placement.get("pool") or "default") != host["pool"]:
        return False
    if placement.get("backend") and placement["backend"] != host["backend"]:
        return False
    # KVM live: a Firecracker host that cannot run a VM takes none (the process backend is the
    # parity lane and needs no KVM).
    if host["backend"] == "firecracker" and not host.get("kvm_live"):
        return False
    # Image presence: when the host reports the images it holds, the deployment's must be one.
    facts = _json(host.get("facts"), {})
    images = facts.get("images") if isinstance(facts, dict) else None
    if dep.get("image_digest") and isinstance(images, list) and dep["image_digest"] not in images:
        return False
    # Capacity: never beyond the VMs the host says it can run.
    capacity = _json(host.get("capacity"), {})
    max_vms = capacity.get("max_vms") if isinstance(capacity, dict) else None
    if isinstance(max_vms, (int, float)) and (load or {}).get(host["id"], 0) >= int(max_vms):
        return False
    labels = _json(host.get("labels"), {})
    return all(labels.get(k) == v for k, v in (placement.get("labels") or {}).items())


#: Re-placement after a loss (an unreachable or cordoned host, a quarantined replica) moves at
#: most this share of a deployment per minute (§3.14.1), and at least one replica.
REPLACE_SHARE_PER_MINUTE = 0.10


def _vm_ip(connection: Any, host: dict) -> Optional[str]:
    if not host.get("vm_prefix"):
        return None
    used = {str(r[0]) for r in connection.execute(text(
        "SELECT host(vm_ip) FROM agent_replicas WHERE host_id = :h AND vm_ip IS NOT NULL "
        "AND (desired_state = 'running' OR observed_state NOT IN ('stopped', 'gone'))"),
        {"h": host["id"]}).all()}
    for address in ipaddress.ip_network(str(host["vm_prefix"])).hosts():
        if str(address) not in used:
            return str(address)
    return None


def reconcile(engine: Any, settings: Any = None, *, org_id: Optional[str] = None) -> dict:
    """One pass (module doc). Safe to call from any process at any time."""
    is_frozen = frozen(settings)
    with engine.begin() as c:
        if not c.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": LOCK_KEY}).scalar():
            return {"skipped": "busy"}
        report = _reconcile(c, is_frozen=is_frozen, org_id=org_id)
    # Committed: the hosts whose desired state moved are told now (a watch waiting in this
    # process; one in another process sees it at its next generation sweep).
    from .run_plane import wake_desired

    wake_desired([b["host_id"] for b in report["bumped"]] + list(report.get("directors") or []))
    return report


def maybe_reconcile(engine: Any, settings: Any = None, *, every: float = 2.0) -> Optional[dict]:
    """The heartbeat's nudge: at most one attempt per `every` seconds in this process."""
    now = time.monotonic()
    if now - _LAST_TRY["at"] < every:
        return None
    _LAST_TRY["at"] = now
    return reconcile(engine, settings)


def _reconcile(c: Any, *, is_frozen: bool, org_id: Optional[str]) -> dict:
    report: dict = {"placed": [], "stopped": [], "degraded": [], "unreachable": [],
                    "bumped": [], "vips": [], "directors": []}
    changed: dict = {}  # host id -> the replica rows this pass changed there
    orgs_touched: set = set()  # orgs whose directors must see a new generation (P8b-10)
    from . import fleet_directors

    # 0. Directors that stopped beating (their leases lapse on their own).
    for did in fleet_directors.mark_unreachable(c, org_id):
        report["unreachable"].append(did)

    def touch(host_id: str, replica_id: str) -> None:
        changed.setdefault(host_id, set()).add(replica_id)

    # 1. Hosts that stopped beating.
    for row in c.execute(text("""
            UPDATE agent_hosts SET status = 'unreachable', updated_at = now()
             WHERE status = 'ready' AND revoked_at IS NULL
               AND (last_heartbeat_at IS NULL OR last_heartbeat_at < now() - make_interval(secs => :s))
               AND (CAST(:o AS TEXT) IS NULL OR org_id = :o)
            RETURNING id, org_id"""), {"s": UNREACHABLE_SECONDS, "o": org_id}).all():
        report["unreachable"].append(row[0])
        host_event(c, org_id=row[1], host_id=row[0], kind="unreachable", actor="scheduler")
    hosts = [dict(r) for r in c.execute(text(
        "SELECT id, org_id, pool, labels, backend, status, revoked_at, vm_prefix, kvm_live, "
        "facts, capacity FROM agent_hosts "
        "WHERE (CAST(:o AS TEXT) IS NULL OR org_id = :o)"), {"o": org_id}).mappings().all()]
    by_id = {h["id"]: h for h in hosts}
    deps = [dict(r) for r in c.execute(text("""
        SELECT d.id, d.project_id, d.agent_key, d.environment, d.version, d.status, d.runtime,
               d.vm_mode, d.replicas, d.placement, d.image_digest, d.resources, w.org_id,
               d.placement_state, d.vip, d.routing
          FROM agent_deployments d JOIN projects p ON p.id = d.project_id
          JOIN workspaces w ON w.id = p.workspace_id
         WHERE (d.runtime = 'microvm' OR d.vip IS NOT NULL
                OR d.id IN (SELECT deployment_id FROM agent_replicas
                            WHERE desired_state = 'running'))
           AND (CAST(:o AS TEXT) IS NULL OR w.org_id = :o)"""), {"o": org_id}).mappings().all()]
    load: dict = {}
    for r in c.execute(text(
            "SELECT host_id, count(*) FROM agent_replicas WHERE desired_state = 'running' "
            "GROUP BY host_id")).all():
        load[r[0]] = int(r[1])

    def stop(replica: dict, why: str) -> None:
        c.execute(text("UPDATE agent_replicas SET desired_state = 'stopped', updated_at = now() "
                       "WHERE id = :r"), {"r": replica["id"]})
        orgs_touched.add(replica.get("org_id") or "")
        load[replica["host_id"]] = max(0, load.get(replica["host_id"], 1) - 1)
        touch(replica["host_id"], replica["id"])
        report["stopped"].append({"replica_id": replica["id"], "why": why})

    for dep in deps:
        replicas = [dict(r) for r in c.execute(text(
            "SELECT id, host_id, agent_version, org_id FROM agent_replicas WHERE deployment_id = :d "
            "AND desired_state = 'running' ORDER BY created_at"), {"d": dep["id"]}
        ).mappings().all()]
        live = dep["status"] == "live" and dep["runtime"] == "microvm"
        mode = dep["vm_mode"] or "replica"
        if not live or (mode == "per_run" and is_frozen):
            # D8: a retired deployment, or one that left `microvm`, gives its VIP back.
            if not live and dep.get("vip") is not None:
                if fleet_directors.release_vip(c, deployment_id=dep["id"]):
                    report["vips"].append({"deployment_id": dep["id"], "released": str(dep["vip"])})
                    orgs_touched.add(dep["org_id"])
            for r in replicas:
                stop(r, "not placed: " + ("per_run is off while microVM claims are frozen"
                                          if live else "deployment is not a live microvm one"))
            state = "frozen" if live else None
            c.execute(text("UPDATE agent_deployments SET placement_state = :s WHERE id = :d"),
                      {"s": state, "d": dep["id"]})
            continue
        if mode == "per_run":
            # Warm pools for per_run land with snapshots in P8b; nothing to place yet.
            c.execute(text("UPDATE agent_deployments SET placement_state = 'pool' WHERE id = :d"),
                      {"d": dep["id"]})
            continue
        prod = dep["environment"] == "prod"
        wanted = max(0, int(dep["replicas"] or 0))
        # D8: one VIP per production agent, allocated on its first placement (P8b-10). Without
        # a pool the deployment serves internal callers only.
        if prod and dep.get("vip") is None and dep.get("routing", "ipvs") == "ipvs":
            vip = fleet_directors.allocate_vip(c, org_id=dep["org_id"], deployment_id=dep["id"])
            if vip:
                report["vips"].append({"deployment_id": dep["id"], "vip": vip})
                orgs_touched.add(dep["org_id"])
        # 2. Replicas on hosts that can no longer hold them.
        kept = []
        for r in replicas:
            host = by_id.get(r["host_id"])
            if host is None or not _eligible(host, {**dep, "image_digest": None}):
                stop(r, f"host {r['host_id']} is {host['status'] if host else 'gone'}")
            else:
                kept.append(r)
        # 3. The pinned version moved (promotion/rollback): replace in place.
        for r in kept:
            if r["agent_version"] != dep["version"]:
                c.execute(text(
                    "UPDATE agent_replicas SET agent_version = :v, image_digest = :img, "
                    "updated_at = now() WHERE id = :r"),
                    {"v": dep["version"], "img": dep["image_digest"], "r": r["id"]})
                touch(r["host_id"], r["id"])
        # 4. Scale in: the most loaded hosts give theirs up first.
        while len(kept) > wanted:
            victim = max(kept, key=lambda r: (load.get(r["host_id"], 0), r["id"]))
            kept.remove(victim)
            stop(victim, "scale in")
        # 5. Scale out. A deployment that was placed before (ok / degraded) is RE-placing what
        # it lost: at most REPLACE_SHARE_PER_MINUTE of it per minute. A first placement or an
        # operator's change (set_runtime marks it `pending`) is not limited.
        holding = {r["host_id"] for r in kept}
        budget = None
        if dep.get("placement_state") in ("ok", "degraded"):
            recent = int(c.execute(text(
                "SELECT count(*) FROM host_events WHERE kind = 'replica_replaced' "
                "AND detail->>'deployment_id' = :d AND created_at > now() - interval '1 minute'"),
                {"d": dep["id"]}).scalar() or 0)
            budget = max(1, math.ceil(REPLACE_SHARE_PER_MINUTE * max(wanted, 1))) - recent
        while len(kept) < wanted:
            if budget is not None and budget <= 0:
                report.setdefault("rate_limited", []).append(dep["id"])
                break
            candidates = [h for h in hosts if _eligible(h, dep, load)]
            spread = [h for h in candidates if h["id"] not in holding]
            pool = spread if (prod or spread) else candidates
            if not pool:
                break
            host = min(pool, key=lambda h: (load.get(h["id"], 0), h["id"]))
            rid = f"rep_{uuid.uuid4().hex[:20]}"
            c.execute(text("""
                INSERT INTO agent_replicas (id, org_id, project_id, deployment_id, host_id, vm_id,
                       vm_mode, vm_ip, image_digest, agent_version, slots)
                VALUES (:i, :o, :p, :d, :h, :vm, 'replica', CAST(:ip AS INET), :img, :v, :slots)"""),
                {"i": rid, "o": dep["org_id"], "p": dep["project_id"], "d": dep["id"],
                 "h": host["id"], "vm": f"vm_{uuid.uuid4().hex[:16]}", "ip": _vm_ip(c, host),
                 "img": dep["image_digest"], "v": dep["version"],
                 "slots": int((_json(dep["resources"], {}) or {}).get("slots") or DEFAULT_SLOTS)})
            kept.append({"id": rid, "host_id": host["id"], "agent_version": dep["version"],
                         "org_id": dep["org_id"]})
            holding.add(host["id"])
            orgs_touched.add(dep["org_id"])
            load[host["id"]] = load.get(host["id"], 0) + 1
            touch(host["id"], rid)
            report["placed"].append({"replica_id": rid, "host_id": host["id"],
                                     "deployment_id": dep["id"]})
            if budget is not None:
                budget -= 1
                host_event(c, org_id=dep["org_id"], host_id=host["id"], kind="replica_replaced",
                           detail={"deployment_id": dep["id"], "replica_id": rid},
                           actor="scheduler")
        state = "ok" if len(kept) >= wanted else ("degraded" if kept else "pending")
        if state != "ok":
            report["degraded"].append({"deployment_id": dep["id"], "placed": len(kept),
                                       "wanted": wanted})
        c.execute(text("UPDATE agent_deployments SET placement_state = :s WHERE id = :d"),
                  {"s": state, "d": dep["id"]})
    # 6. Every host whose set changed moves to a new generation (wakes its watch).
    for host_id in sorted(changed):
        gen = c.execute(text(
            "UPDATE agent_hosts SET desired_generation = desired_generation + 1, "
            "updated_at = now() WHERE id = :h RETURNING desired_generation"),
            {"h": host_id}).scalar()
        c.execute(text("UPDATE agent_replicas SET generation = :g WHERE id = ANY(:r)"),
                  {"g": gen, "r": sorted(changed[host_id])})
        report["bumped"].append({"host_id": host_id, "generation": int(gen or 0)})
    # 7. The directors of every org whose serving sets or VIPs may have moved.
    for org in sorted(o for o in orgs_touched if o):
        report["directors"].extend(fleet_directors.bump(c, org))
    return report


__all__ = ["LOCK_KEY", "frozen", "maybe_reconcile", "reconcile"]
