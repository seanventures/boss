"""Execution environments (BOSSAPP-824 phase 1): rows, resolution, leases, probes.

An execution environment row says WHERE a governed build's sandbox may run: the
control-plane box ('local'), a machine over SSH ('ssh'), the existing micro-VM
lane ('microvm'), or a phase-2 driver ('kubernetes', 'e2b', 'daytona').

This module owns:
  * CRUD over `execution_environments` (db/schema.sql), with a public view that
    never returns a secret value — `secret_refs` holds secret_refs.id references
    only, and secret-shaped config keys are scrubbed on the way out;
  * the resolution precedence the factory asks for: explicit spec override ->
    agent-bound row -> project default -> instance (org-wide) default;
  * `environment_leases`: one build's hold on an environment, released on
    teardown and expired by `reap_leases` (registered on resource_reaper.tick,
    the same maintenance pass that reaps previews and lost job leases);
  * `probe_environment`, what the console's Test button calls.

Nothing here changes the default build path: with no rows configured the
factory (sandbox_provider.make_governed_sandbox_provider) returns the
LocalSubprocessProvider exactly as before.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from .core import make_id

_log = logging.getLogger(__name__)

#: Every driver the schema's CHECK accepts. 'local' and 'microvm' route to the
#: providers that already exist; 'ssh' lands in this phase; the rest are
#: recognized-but-refused until their drivers land (phase 2).
DRIVER_KINDS = ("local", "ssh", "kubernetes", "microvm", "e2b", "daytona")
IMPLEMENTED_DRIVERS = ("local", "ssh", "microvm")

#: Statuses a resolution may hand to a build. 'registered' is included on
#: purpose: a row is usable before its first probe, exactly like a deployment
#: host is reachable before Verify — the probe is evidence, not a gate here.
USABLE_STATUSES = ("registered", "verified", "degraded")

#: A lease the holder never released is expired after this long by default;
#: config.lease_ttl_seconds overrides per environment.
DEFAULT_LEASE_TTL_SECONDS = 4 * 3600

#: Config keys that look like a credential never leave through the API — a
#: secret belongs in secret_refs (as a reference), never in config.
_SECRET_SHAPED = re.compile(
    r"(key|token|secret|password|credential|passphrase)", re.IGNORECASE
)


class EnvironmentNotFound(LookupError):
    pass


class EnvironmentUnusable(ValueError):
    """Phrased for the operator: why this environment cannot take a build."""


# ── small shared helpers ─────────────────────────────────────────────────────


def _jsonb(connection) -> str:
    """The bind expression for a jsonb column: CAST on Postgres, plain on the
    SQLite the unit tests run (the lineage_sandbox dialect-check precedent)."""
    try:
        name = connection.engine.dialect.name
    except AttributeError:  # a raw connection in tests
        name = "sqlite"
    return ":%s" if name == "sqlite" else "CAST(:%s AS JSONB)"


def _parse(value: Any) -> dict:
    """jsonb comes back as a dict on Postgres and a str on SQLite."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _row_dict(row: Any) -> dict:
    out = dict(row)
    out["config"] = _parse(out.get("config"))
    out["secret_refs"] = _parse(out.get("secret_refs"))
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── CRUD ─────────────────────────────────────────────────────────────────────


def create_environment(
    connection,
    *,
    name: str,
    driver: str,
    project_id: Optional[str] = None,
    config: Optional[dict] = None,
    secret_refs: Optional[dict] = None,
    status: str = "registered",
    actor: str = "operator",
) -> dict:
    driver = str(driver or "").strip().lower()
    if driver not in DRIVER_KINDS:
        raise EnvironmentUnusable(
            f"driver must be one of {', '.join(DRIVER_KINDS)} — got {driver!r}."
        )
    name = str(name or "").strip()
    if not name:
        raise EnvironmentUnusable("an environment needs a name.")
    env_id = make_id("env")
    cast = _jsonb(connection)
    connection.execute(
        text(
            "INSERT INTO execution_environments "
            "(id, project_id, name, driver, config, secret_refs, status, created_by) "
            f"VALUES (:i, :p, :n, :d, {cast % 'c'}, {cast % 's'}, :st, :by)"
        ),
        {
            "i": env_id, "p": project_id, "n": name, "d": driver,
            "c": json.dumps(config or {}), "s": json.dumps(secret_refs or {}),
            "st": status, "by": actor,
        },
    )
    return get_environment(connection, env_id)


def get_environment(connection, environment_id: str) -> dict:
    row = connection.execute(
        text("SELECT * FROM execution_environments WHERE id = :i"),
        {"i": environment_id},
    ).mappings().first()
    if row is None:
        raise EnvironmentNotFound(f"no execution environment {environment_id!r}.")
    return _row_dict(row)


def list_environments(
    connection, project_id: Optional[str] = None, *, include_org_wide: bool = True
) -> list[dict]:
    """A project's environments plus (by default) the org-wide rows it may use."""
    if project_id is None:
        rows = connection.execute(
            text("SELECT * FROM execution_environments WHERE project_id IS NULL "
                 "ORDER BY created_at DESC")
        ).mappings().all()
    elif include_org_wide:
        rows = connection.execute(
            text("SELECT * FROM execution_environments "
                 "WHERE project_id = :p OR project_id IS NULL "
                 "ORDER BY (project_id IS NULL), created_at DESC"),
            {"p": project_id},
        ).mappings().all()
    else:
        rows = connection.execute(
            text("SELECT * FROM execution_environments WHERE project_id = :p "
                 "ORDER BY created_at DESC"),
            {"p": project_id},
        ).mappings().all()
    return [_row_dict(r) for r in rows]


def update_environment(
    connection,
    environment_id: str,
    *,
    name: Optional[str] = None,
    config: Optional[dict] = None,
    secret_refs: Optional[dict] = None,
    status: Optional[str] = None,
) -> dict:
    get_environment(connection, environment_id)  # a missing id 404s in words first
    sets: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
    params: dict[str, Any] = {"i": environment_id}
    cast = _jsonb(connection)
    if name is not None and name.strip():
        sets.append("name = :n")
        params["n"] = name.strip()
    if config is not None:
        sets.append(f"config = {cast % 'c'}")
        params["c"] = json.dumps(config)
    if secret_refs is not None:
        sets.append(f"secret_refs = {cast % 's'}")
        params["s"] = json.dumps(secret_refs)
    if status is not None:
        sets.append("status = :st")
        params["st"] = status
    connection.execute(
        text(f"UPDATE execution_environments SET {', '.join(sets)} WHERE id = :i"),
        params,
    )
    return get_environment(connection, environment_id)


def delete_environment(connection, environment_id: str) -> bool:
    result = connection.execute(
        text("DELETE FROM execution_environments WHERE id = :i"),
        {"i": environment_id},
    )
    return bool(getattr(result, "rowcount", 0))


def public_view(row: dict) -> dict:
    """The row as the API returns it. secret_refs carries REFERENCE IDS (never
    values) and keeps only its keys + ids; a secret-shaped config key is
    scrubbed — a credential pasted into config was stored wrong, and echoing it
    back would make the mistake permanent."""
    config = {}
    for key, value in (row.get("config") or {}).items():
        config[key] = "[redacted — use secret_refs]" if _SECRET_SHAPED.search(key) else value
    refs = {}
    for key, value in (row.get("secret_refs") or {}).items():
        refs[key] = str(value)[:128] if isinstance(value, (str, int)) else "[ref]"
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "name": row.get("name"),
        "driver": row.get("driver"),
        "config": config,
        "secret_refs": refs,
        "status": row.get("status"),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


# ── resolution (step -> agent -> project -> instance) ────────────────────────


def resolve_environment(
    connection,
    *,
    project_id: Optional[str] = None,
    agent: Optional[str] = None,
    spec: Optional[dict] = None,
) -> Optional[dict]:
    """The environment this build runs in, by precedence — or None, which means
    "exactly the behaviour BOSS had before this table existed".

    1. The step's explicit override: spec.environment_id (a row), or
       spec.substrate naming a NEW driver kind (ssh/kubernetes/e2b/daytona —
       the legacy vocabulary local/microvm stays with the paths that own it).
    2. The agent's bound row: config.agents contains the agent's name.
    3. The project's default row: config.default is true.
    4. The instance default: an org-wide row (project_id IS NULL) with
       config.default true.

    A substrate override that names a driver with no configured row raises
    EnvironmentUnusable in words — never a silent fallback to local.
    """
    spec = spec or {}
    env_id = str(spec.get("environment_id") or "").strip()
    if env_id:
        row = get_environment(connection, env_id)  # missing id: words, not local
        _require_usable(row)
        return row

    substrate = str(spec.get("substrate") or "").strip().lower()
    legacy = {"local", "subprocess", "local_subprocess", "microvm", "firecracker", "gvisor"}
    if substrate in legacy:
        # An EXPLICIT legacy ask pins the legacy path: the factory then answers
        # exactly as it did before environments existed, whatever defaults are
        # configured — a per-run local/microvm override must stay honoured.
        return None
    if substrate:
        rows = [r for r in _candidate_rows(connection, project_id)
                if r["driver"] == substrate]
        if not rows:
            raise EnvironmentUnusable(
                f"the task asks for substrate {substrate!r}, but no usable "
                f"execution environment with that driver is configured for this "
                "project (or org-wide). Add one under Environments, or drop the "
                "override."
            )
        return rows[0]

    candidates = _candidate_rows(connection, project_id)
    agent_name = str(agent or spec.get("agent") or spec.get("agent_id") or "").strip()
    if agent_name:
        for row in candidates:
            agents = row["config"].get("agents")
            if isinstance(agents, list) and agent_name in [str(a) for a in agents]:
                return row
    for row in candidates:  # project rows come first (see _candidate_rows)
        if row["project_id"] is not None and _truthy(row["config"].get("default")):
            return row
    for row in candidates:
        if row["project_id"] is None and _truthy(row["config"].get("default")):
            return row
    return None


def _candidate_rows(connection, project_id: Optional[str]) -> list[dict]:
    rows = list_environments(connection, project_id, include_org_wide=True)
    usable = [r for r in rows if str(r.get("status") or "") in USABLE_STATUSES]
    # Project-scoped rows outrank org-wide ones at every step of the precedence.
    return sorted(usable, key=lambda r: r["project_id"] is None)


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


def _require_usable(row: dict) -> None:
    status = str(row.get("status") or "")
    if status not in USABLE_STATUSES:
        raise EnvironmentUnusable(
            f"execution environment {row.get('name')!r} is {status or 'unusable'} "
            "— pick another, or re-enable it under Environments."
        )


# ── leases ───────────────────────────────────────────────────────────────────


def lease_fingerprint(row: dict) -> str:
    """driver+image+cpu/mem when present — what a later pooling phase matches on."""
    config = row.get("config") or {}
    parts = [str(row.get("driver") or "")]
    for key in ("image", "cpu", "vcpu", "mem", "mem_mb"):
        value = config.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return ":".join(parts)


def acquire_lease(
    connection,
    *,
    environment_id: str,
    run_id: Optional[str] = None,
    fingerprint: str = "",
    ttl_seconds: Optional[int] = None,
) -> dict:
    lease_id = make_id("envl")
    ttl = int(ttl_seconds or DEFAULT_LEASE_TTL_SECONDS)
    # ISO strings, like lineage_sandbox: Postgres casts them to timestamptz and
    # SQLite (the unit tests) keeps them comparable after a Python parse.
    now = _now()
    connection.execute(
        text(
            "INSERT INTO environment_leases "
            "(id, environment_id, fingerprint, state, run_id, acquired_at, expires_at) "
            "VALUES (:i, :e, :f, 'held', :r, :now, :exp)"
        ),
        {
            "i": lease_id, "e": environment_id, "f": fingerprint or "",
            "r": run_id, "now": now.isoformat(),
            "exp": (now + timedelta(seconds=ttl)).isoformat(),
        },
    )
    return {
        "id": lease_id, "environment_id": environment_id, "state": "held",
        "run_id": run_id, "fingerprint": fingerprint or "",
        "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
    }


def release_lease(
    connection,
    *,
    lease_id: Optional[str] = None,
    run_id: Optional[str] = None,
    environment_id: Optional[str] = None,
) -> int:
    """Mark held leases released — by lease id, or every held lease of a run
    (optionally narrowed to one environment). Returns how many were released."""
    if not lease_id and not run_id:
        return 0
    where = ["state = 'held'"]
    params: dict[str, Any] = {}
    if lease_id:
        where.append("id = :i")
        params["i"] = lease_id
    if run_id:
        where.append("run_id = :r")
        params["r"] = run_id
    if environment_id:
        where.append("environment_id = :e")
        params["e"] = environment_id
    result = connection.execute(
        text(
            "UPDATE environment_leases SET state = 'released', "
            f"released_at = CURRENT_TIMESTAMP WHERE {' AND '.join(where)}"
        ),
        params,
    )
    return int(getattr(result, "rowcount", 0) or 0)


def reap_leases(engine: Any, settings: Any = None) -> dict:
    """Expire held leases past their expires_at. Registered on
    resource_reaper.tick — the maintenance pass previews and job leases already
    ride — so a build that died without teardown never holds an environment
    forever. Returns counts; tolerates an install whose schema predates the
    table (the reaper runs on every install)."""
    try:
        with engine.begin() as connection:
            rows = connection.execute(
                text("SELECT id, expires_at FROM environment_leases "
                     "WHERE state = 'held' AND expires_at IS NOT NULL")
            ).mappings().all()
            # Parse in Python, like lineage_sandbox.evict_idle: Postgres hands
            # back datetimes and SQLite hands back the ISO strings we stored.
            now = _now()
            stale: list[str] = []
            for row in rows:
                value = row["expires_at"]
                if isinstance(value, datetime):
                    expires = value
                else:
                    try:
                        expires = datetime.fromisoformat(str(value))
                    except ValueError:
                        continue
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires < now:
                    stale.append(str(row["id"]))
            expired = 0
            for lease_id in stale:
                result = connection.execute(
                    text("UPDATE environment_leases SET state = 'expired', "
                         "released_at = :now WHERE id = :i AND state = 'held'"),
                    {"i": lease_id, "now": now.isoformat()},
                )
                expired += int(getattr(result, "rowcount", 0) or 0)
    except Exception as exc:  # noqa: BLE001 — pre-824 schema: nothing to reap
        return {"expired": 0, "skipped": f"{type(exc).__name__}"[:80]}
    if expired:
        _log.info("environment_leases: expired %s stale lease(s)", expired)
    return {"expired": expired}


# ── probe (the console's Test button) ────────────────────────────────────────


def probe_environment(connection, settings: Any, row: dict) -> dict:
    """Health/capability check for one environment, per driver. Never returns a
    secret; never claims more than it measured."""
    driver = str(row.get("driver") or "").lower()
    if driver == "local":
        import shutil

        return {
            "ok": True,
            "driver": "local",
            "git": bool(shutil.which("git")),
            "note": "builds run as subprocesses on the control-plane box.",
        }
    if driver == "ssh":
        from .ssh_environment import probe_over_ssh

        return probe_over_ssh(connection, settings, row)
    if driver == "microvm":
        from .sandbox_provider import microvm_claims_are_frozen, sandbox_host_base

        base = sandbox_host_base(settings)
        return {
            "ok": bool(base),
            "driver": "microvm",
            "host_configured": bool(base),
            "claims_frozen": microvm_claims_are_frozen(settings),
            "note": ("sandbox host configured; execution claims follow "
                     "BOSS_MICROVM_CLAIMS_FROZEN." if base
                     else "no sandbox host configured (BOSS_SANDBOX_AGENT_URL is empty)."),
        }
    return {
        "ok": False,
        "driver": driver,
        "error": f"the {driver!r} driver is recognized but not implemented in "
                 "this phase (phase 2: kubernetes / e2b / daytona).",
    }


def record_probe(connection, environment_id: str, result: dict) -> None:
    """Verified on a passing probe, degraded on a failing one — same meaning the
    deployment fleet gives those words."""
    connection.execute(
        text("UPDATE execution_environments SET status = :s, "
             "updated_at = CURRENT_TIMESTAMP WHERE id = :i AND status != 'retired'"),
        {"s": "verified" if result.get("ok") else "degraded", "i": environment_id},
    )
