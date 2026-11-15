"""AP-D11: `strong_isolation(deployment)`, derived from attested facts only (P8b-11, BOSSAPP-1078;
boss-agent-hosts-lld.md §3.17.4, D5, D15, D17, D21).

AGENT_PLATFORM's D11 keeps write-capable agents off A2A "until strong isolation exists". Until P8
that was a constant False. It is now TRUE ONLY WHEN ALL of these hold, read from the database at
the moment of the request — there is no flag, setting or override:

  1. runtime == 'microvm', the deployment is live, and BOSS_MICROVM_CLAIMS_FROZEN is off (the
     kill switch, §3.17.4);
  2. vm_mode meets D5: a replica or per_run VM; and a deployment open to PUBLIC (unauthenticated)
     callers hands each run to a `per_run` VM (expose.public ⇒ per_run);
  3. there is at least one serving replica, and EVERY serving replica's host attests from a
     heartbeat under 10 s old: the Firecracker backend with firecracker_live (and kvm_live),
     jailer_on, seccomp_default, strict_secrets (D21), net_policy_ok (inbound-only tap / no NIC),
     and no D15 fallback tap (`fallback_tap`, which does not qualify);
  4. the image digest is pinned, signed and active (the deployment's and every serving
     replica's);
  5. every serving replica holds a valid serving certificate (agent_ca: unexpired, unrevoked);
  6. no serving replica is tainted (a raw secret was never delivered to it, D17).

`runtime: local` is unchanged: always False. When an attestation lapses the predicate is False on
the very next request, so A2A for a write-capable agent closes (a2a_server.exposure_refusal runs on
every request), and the directors stop serving the replicas whose host lapsed
(fleet_directors.desired filters on agent_hosts.isolation_ok + heartbeat age; the heartbeat and
the scheduler's sweep bump the directors when it flips).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from sqlalchemy import text

#: A heartbeat older than this is no attestation at all.
MAX_HEARTBEAT_AGE_SECONDS = 10
#: The host facts that must all be true (agent_hosts columns, measured by boss-hostd).
HOST_FACTS = ("kvm_live", "firecracker_live", "jailer_on", "seccomp_default", "strict_secrets",
              "net_policy_ok")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.ok

    def explain(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "strong isolation holds"


def host_attests(host: Mapping[str, Any]) -> list:
    """Why this host's last heartbeat does not attest strong isolation (empty = it does).
    `host` carries the agent_hosts columns plus `facts` (the JSON the heartbeat sent)."""
    why: list = []
    if host.get("backend") != "firecracker":
        why.append(f"backend {host.get('backend')} is not a microVM")
    for name in HOST_FACTS:
        if not bool(host.get(name)):
            why.append(f"{name} is false")
    facts = host.get("facts") if isinstance(host.get("facts"), Mapping) else {}
    if facts.get("fallback_tap"):
        why.append("the D15 fallback tap is on")
    if host.get("revoked_at") is not None:
        why.append("host is revoked")
    elif host.get("status") not in (None, "ready", "cordoned", "draining"):
        why.append(f"host is {host.get('status')}")
    return why


def evaluate(deployment: Mapping[str, Any], replicas: list, hosts: Mapping[str, Mapping],
             *, frozen: bool, images: Mapping[str, Mapping], certified: set) -> Verdict:
    """THE PREDICATE over plain facts (no database): the truth table the tests pin.
    `replicas` are the deployment's SERVING replicas ({id, host_id, vm_mode, image_digest,
    tainted}); `hosts` by id with `heartbeat_age` in seconds; `images` by digest ({status,
    signature}); `certified` the replica ids holding a valid serving certificate."""
    why: list = []
    if deployment.get("runtime") != "microvm":
        return Verdict(False, ("runtime is local",))
    if frozen:
        why.append("microVM claims are frozen (BOSS_MICROVM_CLAIMS_FROZEN)")
    if deployment.get("status") != "live":
        why.append(f"deployment is {deployment.get('status')}")
    mode = deployment.get("vm_mode")
    expose = deployment.get("expose") if isinstance(deployment.get("expose"), Mapping) else {}
    if mode not in ("replica", "per_run"):
        why.append("vm_mode is unset")
    elif expose.get("public") and mode != "per_run":
        why.append("public callers need per_run VMs (D5)")
    if not replicas:
        why.append("no serving replica")

    def image_ok(digest: Optional[str]) -> bool:
        row = images.get(digest or "")
        return bool(digest and row and row.get("status") == "active" and row.get("signature"))

    if not image_ok(deployment.get("image_digest")):
        why.append("the image is not pinned, signed and active")
    for r in replicas:
        rid = r.get("id")
        host = hosts.get(r.get("host_id")) or {}
        age = host.get("heartbeat_age")
        if age is None or age >= MAX_HEARTBEAT_AGE_SECONDS:
            why.append(f"{rid}: host heartbeat is older than {MAX_HEARTBEAT_AGE_SECONDS} s")
        why.extend(f"{rid}: {w}" for w in host_attests(host))
        if r.get("image_digest") and not image_ok(r.get("image_digest")):
            why.append(f"{rid}: image is not signed and active")
        if rid not in certified:
            why.append(f"{rid}: no valid serving certificate")
        if r.get("tainted"):
            why.append(f"{rid}: tainted (a raw secret was delivered, D17)")
    return Verdict(not why, tuple(why))


def _deployment(connection: Any, deployment: Any) -> Optional[dict]:
    if isinstance(deployment, Mapping) and deployment.get("id") and "runtime" in deployment \
            and "image_digest" in deployment:
        return dict(deployment)
    dep_id = deployment.get("id") if isinstance(deployment, Mapping) else deployment
    row = connection.execute(text(
        "SELECT id, runtime, status, vm_mode, expose, image_digest FROM agent_deployments "
        "WHERE id = :d"), {"d": str(dep_id or "")}).mappings().first()
    return dict(row) if row else None


def verdict(connection: Any, deployment: Any, *, settings: Any = None) -> Verdict:
    """The predicate for one deployment (an agent_deployments row or its id), from the database
    now. Never raises for missing data: missing facts are False."""
    from .fleet_scheduler import frozen

    dep = _deployment(connection, deployment)
    if dep is None:
        return Verdict(False, ("no such deployment",))
    if dep.get("runtime") != "microvm":
        return Verdict(False, ("runtime is local",))
    if isinstance(dep.get("expose"), str):
        import json

        try:
            dep["expose"] = json.loads(dep["expose"])
        except ValueError:
            dep["expose"] = {}
    replicas = [dict(r) for r in connection.execute(text(
        "SELECT id, host_id, vm_mode, image_digest, tainted FROM agent_replicas "
        "WHERE deployment_id = :d AND desired_state = 'running' AND observed_state = 'serving'"),
        {"d": dep["id"]}).mappings().all()]
    host_ids = sorted({r["host_id"] for r in replicas})
    hosts = {r["id"]: dict(r) for r in connection.execute(text(
        "SELECT id, backend, status, revoked_at, facts, " + ", ".join(HOST_FACTS) + ", "
        "EXTRACT(EPOCH FROM (now() - last_heartbeat_at)) AS heartbeat_age "
        "FROM agent_hosts WHERE id = ANY(:h)"), {"h": host_ids}).mappings().all()}
    for h in hosts.values():
        if h.get("heartbeat_age") is not None:
            h["heartbeat_age"] = float(h["heartbeat_age"])
    digests = sorted({d for d in [dep.get("image_digest")] + [r.get("image_digest")
                                                              for r in replicas] if d})
    images = {r["digest"]: dict(r) for r in connection.execute(text(
        "SELECT digest, status, signature FROM vm_images WHERE digest = ANY(:d)"),
        {"d": digests}).mappings().all()}
    from .agent_ca import replicas_with_valid_cert

    certified = replicas_with_valid_cert(connection, [r["id"] for r in replicas])
    return evaluate(dep, replicas, hosts, frozen=frozen(settings), images=images,
                    certified=certified)


def strong_isolation(connection: Any, deployment: Any, *, settings: Any = None) -> bool:
    """AP-D11 (module doc). True only when every fact holds now."""
    return verdict(connection, deployment, settings=settings).ok


def host_isolation_ok(host: Mapping[str, Any]) -> bool:
    """The per-host half of the predicate, stored on every heartbeat (agent_hosts.isolation_ok)."""
    return not host_attests(host)


__all__ = ["HOST_FACTS", "MAX_HEARTBEAT_AGE_SECONDS", "Verdict", "evaluate", "host_attests",
           "host_isolation_ok", "strong_isolation", "verdict"]
