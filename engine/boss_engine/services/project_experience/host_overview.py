"""INFRA › SERVER (screen 14): one shared server at a glance — what it is, how full it is, which
projects run on it (each linking to that Project › Deployments), what it runs for them (host
plugins) and what changed on it lately.

Shared stays in Infra; a project's own view lives on its page. This read joins the two: the
server's record and capacity, and the residents of EVERY project the viewer may read (the route
passes them), not only the project the Infra page is open on.

Capacity is what the server said, never a constant: the deploy preflight (stack_deploy.
preflight_host) and `read_capacity` both run `CAPACITY_PROBE` in the host's one read-only ssh
session — `nproc`, /proc/loadavg, /proc/meminfo, `df -Pk /`, /etc/os-release and the images of
the running containers — and keep it on the host's record (last_preflight.facts.capacity) with
the time it was read.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import initials, iso, project_href

#: One shell line; prints BOSS_CAP / BOSS_IMG lines and nothing else. Read-only.
CAPACITY_PROBE = (
    "echo \"BOSS_CAP cpus $(nproc 2>/dev/null)\"; "
    "echo \"BOSS_CAP load $(cut -d' ' -f1 /proc/loadavg 2>/dev/null)\"; "
    "awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print \"BOSS_CAP mem \" t \" \" a}' "
    "/proc/meminfo 2>/dev/null; "
    "df -Pk / 2>/dev/null | awk 'NR==2{print \"BOSS_CAP disk \" $2 \" \" $3}'; "
    "(. /etc/os-release 2>/dev/null && echo \"BOSS_CAP os $PRETTY_NAME\"); "
    "docker ps --format '{{.Image}}' 2>/dev/null | head -40 | sed 's/^/BOSS_IMG /'; true")


def capacity_from(out: str, *, at: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """What CAPACITY_PROBE printed → {cpus, cpu_pct, mem_kb, mem_pct, disk_kb, disk_pct, os,
    images, at}; None when it printed nothing."""
    cap: dict[str, Any] = {}
    images: list[str] = []
    for line in (out or "").splitlines():
        parts = line.strip().split(" ")
        if parts[:1] == ["BOSS_IMG"] and len(parts) > 1:
            images.append(" ".join(parts[1:])[:120])
            continue
        if parts[:1] != ["BOSS_CAP"] or len(parts) < 3:
            continue
        key, rest = parts[1], parts[2:]
        try:
            if key == "cpus":
                cap["cpus"] = int(rest[0])
            elif key == "load":
                cap["load"] = float(rest[0])
            elif key == "mem" and len(rest) >= 2:
                total, avail = int(rest[0]), int(rest[1])
                cap["mem_kb"] = total
                cap["mem_pct"] = round(100 * (total - avail) / total) if total else None
            elif key == "disk" and len(rest) >= 2:
                total, used = int(rest[0]), int(rest[1])
                cap["disk_kb"] = total
                cap["disk_pct"] = round(100 * used / total) if total else None
            elif key == "os":
                cap["os"] = " ".join(rest).strip().strip('"')[:60]
        except (ValueError, ZeroDivisionError):
            continue
    if not cap:
        return None
    if cap.get("cpus") and cap.get("load") is not None:
        cap["cpu_pct"] = min(100, round(100 * cap["load"] / cap["cpus"]))
    cap["images"] = images
    cap["at"] = (at or datetime.now(timezone.utc)).isoformat()
    return cap


def store_capacity(connection: Connection, host_id: str, cap: Mapping[str, Any]) -> None:
    connection.execute(text(
        "UPDATE deployment_hosts SET last_preflight = jsonb_set(COALESCE(last_preflight, "
        "'{}'::jsonb), '{facts}', COALESCE(last_preflight->'facts', '{}'::jsonb) || "
        "jsonb_build_object('capacity', CAST(:c AS JSONB)), true) WHERE id = :h"),
        {"h": host_id, "c": json.dumps(dict(cap))})


def read_capacity(connection: Connection, settings: Any, project_id: str, host_id: str, *,
                  runner: Optional[Callable[[Mapping[str, Any], str], tuple[int, str, str]]] = None
                  ) -> dict[str, Any]:
    """Ask the server now (read-only) and keep the answer on its record."""
    from .host_folder import FolderError, _host, _ssh_runner, run_read

    host = _host(connection, project_id, host_id)
    code, out, err = run_read(host, CAPACITY_PROBE, runner or _ssh_runner(settings))
    cap = capacity_from(out)
    if cap is None:
        raise FolderError(f"{host['name']} did not answer ({(err or '').strip()[:160] or f'exit {code}'}).",
                          502)
    store_capacity(connection, host["id"], cap)
    return cap


def _gb(kb: Any) -> Optional[str]:
    if not kb:
        return None
    gb = int(kb) / (1024 * 1024)
    return f"{round(gb)} GB" if gb >= 10 else f"{gb:.1f} GB"


def _port_of(url: Any) -> Optional[int]:
    found = re.search(r":(\d{2,5})(?:/|$)", str(url or ""))
    return int(found.group(1)) if found else None


_PLUGIN_IMAGES = (("caddy", "Caddy TLS", "lock"), ("traefik", "Traefik TLS", "lock"),
                  ("nginx", "nginx", "globe"), ("node-exporter", "node-exporter", "chart"),
                  ("node_exporter", "node-exporter", "chart"), ("prometheus", "Prometheus", "chart"),
                  ("grafana", "Grafana", "chart"), ("cadvisor", "cAdvisor", "chart"),
                  ("postgres", "Postgres", "database"), ("redis", "Redis", "database"))


def plugins(facts: Mapping[str, Any]) -> list[dict[str, str]]:
    """What the server runs for its apps, from its facts: Docker (major version), Compose, and
    the infrastructure images its running containers use."""
    out: list[dict[str, str]] = []
    versions = facts.get("versions") or {}
    docker = re.search(r"(\d+)\.", str(versions.get("docker") or ""))
    if docker or facts.get("docker"):
        out.append({"key": "docker", "label": f"Docker {docker.group(1)}" if docker else "Docker",
                    "icon": "cube"})
    compose = re.search(r"(\d+\.\d+)", str(versions.get("compose") or ""))
    if compose:
        out.append({"key": "compose", "label": f"Compose {compose.group(1)}", "icon": "stack"})
    seen = {p["label"] for p in out}
    for image in ((facts.get("capacity") or {}).get("images") or []):
        name = str(image).lower()
        for needle, label, icon in _PLUGIN_IMAGES:
            if needle in name and label not in seen:
                seen.add(label)
                out.append({"key": needle, "label": label, "icon": icon})
    return out


def usage(connection: Connection, host_ids: list[str], visible_project_ids: list[str]
          ) -> dict[str, dict[str, Any]]:
    """WHAT A SERVER HOLDS, ONE COUNT (14-05): per host, the active targets of every project the
    viewer may read (not archived) — {apps, ports: set of ports their URLs hold, projects} — the
    numbers the server drawer's tiles show and the Infra rack's "apps · ports" meta repeats."""
    out: dict[str, dict[str, Any]] = {h: {"apps": 0, "ports": set(), "projects": set()}
                                      for h in host_ids}
    if not host_ids:
        return out
    for t in connection.execute(text(
            "SELECT t.host_id, t.project_id, t.domains, t.public_url FROM deployment_targets t "
            "JOIN projects p ON p.id = t.project_id WHERE t.host_id = ANY(:h) AND "
            "t.status = 'active' AND t.project_id = ANY(:ids) AND p.archived_at IS NULL"),
            {"h": host_ids, "ids": visible_project_ids}).mappings():
        slot = out.setdefault(str(t["host_id"]), {"apps": 0, "ports": set(), "projects": set()})
        domains = t["domains"] if isinstance(t["domains"], list) else json.loads(t["domains"] or "[]")
        port = next((p for p in (_port_of(d) for d in [t["public_url"], *domains]) if p), None)
        slot["apps"] += 1
        slot["projects"].add(str(t["project_id"]))
        if port:
            slot["ports"].add(port)
    return out


#: A resident's health in the console's words (screen 14: Healthy / Degraded).
_HEALTH_WORD = {"up": "Healthy", "healthy": "Healthy", "degraded": "Degraded", "down": "Down",
                "not_deployed": "Not deployed", "unknown": "No signal"}


def overview(connection: Connection, project_id: str, host_id: str,
             visible_project_ids: list[str], *, now: Optional[datetime] = None) -> dict[str, Any]:
    from ..infra_view import health_states
    from ..people_names import who
    from .host_folder import FolderError, _host

    try:
        host = _host(connection, project_id, host_id) if host_id != "this" else None
    except FolderError:
        host = None
    if host is None:
        row = connection.execute(text(
            "SELECT * FROM deployment_hosts WHERE id = :h"), {"h": host_id}).mappings().first()
        if row is None:
            raise LookupError("No such server.")
        from ..deployment_fleet import host_usable_by

        if not host_usable_by(connection, host_id, project_id):
            raise LookupError("No such server.")
        host = dict(row)
    pre = host.get("last_preflight") if isinstance(host.get("last_preflight"), dict) else \
        json.loads(host.get("last_preflight") or "{}")
    facts = pre.get("facts") or {}
    cap = facts.get("capacity") or {}

    targets = [dict(r) for r in connection.execute(text(
        "SELECT t.id, t.project_id, t.app_name, t.environment, t.domains, t.public_url, "
        "p.name AS project_name FROM deployment_targets t JOIN projects p ON p.id = t.project_id "
        "WHERE t.host_id = :h AND t.status = 'active' AND t.project_id = ANY(:ids) "
        "AND p.archived_at IS NULL ORDER BY p.name, t.environment"),
        {"h": host_id, "ids": visible_project_ids}).mappings()]
    health = health_states(connection, [t["id"] for t in targets], now=now)
    acting = {str(r[0]) for r in connection.execute(text(
        "SELECT DISTINCT target_id FROM stack_deployments WHERE target_id = ANY(:ids) "
        "AND status IN ('running', 'planned')"), {"ids": [t["id"] for t in targets]}).all()}
    ports: set[int] = set()
    projects: list[dict[str, Any]] = []
    for t in targets:
        domains = t["domains"] if isinstance(t["domains"], list) else json.loads(t["domains"] or "[]")
        port = next((p for p in (_port_of(d) for d in [t["public_url"], *domains]) if p), None)
        if port:
            ports.add(port)
        h = health.get(t["id"]) or {}
        projects.append({
            "id": t["project_id"], "name": t["project_name"], "initials": initials(t["project_name"]),
            "target_id": t["id"], "app": t["app_name"], "env": t["environment"], "port": port,
            "health": {"word": _HEALTH_WORD.get(str(h.get("status") or ""), h.get("word") or "No signal"),
                       "tone": h.get("tone") or "grey", "status": h.get("status")},
            "agent": {"name": "Beacon", "role_key": "sre", "live": t["id"] in acting},
            "href": project_href(str(t["project_id"]), tab="deployments"),
        })
    shared_by = len({p["id"] for p in projects})

    # RECENT CHANGES SAY WHO SHIPPED WHAT (14-10), as Project › Deployments does: the pull
    # request whose merge was deployed (deployments._pr_by_commit) and the person who approved
    # or asked for the deploy (people_names.who) — Beacon only when no person did.
    from .deployments import _pr_by_commit

    changes = []
    prs_of: dict[str, dict[str, dict[str, Any]]] = {}
    for r in connection.execute(text(
            "SELECT s.id, s.status, s.created_by, s.created_at, s.finished_at, t.environment, "
            "s.workflow_run_id, s.commit_sha, d.approved_by, "
            "p.name AS project_name, p.id AS project_id FROM stack_deployments s "
            "JOIN deployment_targets t ON t.id = s.target_id JOIN projects p ON p.id = s.project_id "
            "LEFT JOIN decisions d ON d.id = s.decision_id "
            "WHERE s.host_id = :h AND s.project_id = ANY(:ids) "
            "AND s.status IN ('healthy', 'failed', 'running') "
            "ORDER BY COALESCE(s.finished_at, s.created_at) DESC LIMIT 4"),
            {"h": host_id, "ids": visible_project_ids}).mappings():
        pid = str(r["project_id"])
        if pid not in prs_of:
            prs_of[pid] = _pr_by_commit(connection, pid)
        pr = prs_of[pid].get(str(r["commit_sha"] or ""))
        short = str(r["project_name"]).split()[0]
        env = str(r["environment"] or "").lower()
        what = f"PR #{pr['number']}" if pr else ((r["commit_sha"] or "")[:7] or "deploy")
        actor = str(r["approved_by"] or r["created_by"] or "")
        person = actor and actor not in ("boss", "auto_approval", "monitor") and \
            not actor.startswith(("agt_", "policy:"))
        changes.append({
            "id": r["id"], "label": f"{short} {env} · {what}".strip(),
            "title": (f"{r['project_name']} {env}: PR #{pr['number']} {pr['title']}" if pr
                      else f"{r['project_name']} {env} deploy"),
            "state": {"healthy": "done", "failed": "failed"}.get(r["status"], "now"),
            "at": iso(r["finished_at"] or r["created_at"]),
            "by": who(actor, connection) if person else "Beacon",
            "href": project_href(pid, tab="deployments"),
        })

    reachable = pre.get("reachable")
    problems = pre.get("problems") or []
    kind = "Docker host" if facts.get("docker") else "Server"
    sub = " · ".join(x for x in (kind, cap.get("os"),
                                 f"shared by {shared_by} project{'s' if shared_by != 1 else ''}"
                                 if shared_by else None) if x)
    port_range = (f"{host['port_min']}–{host['port_max']}"
                  if host.get("port_min") is not None and host.get("port_max") is not None else None)
    return {
        "id": host["id"], "name": host["name"], "address": host.get("address"), "sub": sub,
        "reachable": {"state": "reachable" if reachable else "unreachable" if reachable is False
                      else "unknown",
                      "reason": (problems[0].get("headline") if problems and isinstance(
                          problems[0], dict) else None),
                      "at": iso(host.get("last_preflight_at"))},
        "capacity": {
            "cpu": {"pct": cap.get("cpu_pct"), "label": f"{cap['cpus']} vCPU" if cap.get("cpus")
                    else None},
            "memory": {"pct": cap.get("mem_pct"), "label": _gb(cap.get("mem_kb"))},
            "disk": {"pct": cap.get("disk_pct"), "label": _gb(cap.get("disk_kb"))},
            "ports": {"used": len(usage(connection, [host["id"]], visible_project_ids)
                                  .get(host["id"], {}).get("ports") or ports), "range": port_range},
            "at": cap.get("at"),
        },
        "projects": projects, "shared_by": shared_by, "plugins": plugins(facts),
        "changes": changes,
    }


__all__ = ["CAPACITY_PROBE", "capacity_from", "overview", "plugins", "read_capacity",
           "store_capacity", "usage"]
