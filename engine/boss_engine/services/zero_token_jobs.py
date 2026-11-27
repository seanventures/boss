"""Zero-token recurring script/command jobs and change-only monitors (BOSSAPP-939; Kiro gap 6).

Decision (2026-11-24): Build zero-token recurring jobs and change-only runs, on by default;
agents may create them for the project they work on.

* ``script`` / ``command`` jobs run allowlisted commands with ZERO model calls
  (``token_cost_usd_micros = 0``), recording a governed Receipt and audit event.
* ``change_monitor`` jobs poll a watched revision (git branch SHA, PR head SHA, file/URL
  digest) at zero token cost and wake an agent ONLY when the revision changes.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id
from .receipts import ReceiptService

VALID_KINDS = frozenset({"script", "command", "change_monitor"})
VALID_CADENCES = frozenset({"interval", "hourly", "daily", "weekly"})

#: Safe baseline executables allowed for zero-token command jobs when no project override is set.
DEFAULT_ALLOWED_EXECUTABLES = frozenset(
    {"python3", "python", "pytest", "git", "node", "npm", "npx", "bash", "sh", "echo", "curl"}
)
#: Characters that indicate shell chaining/injection when a command is given as a string.
_FORBIDDEN_SHELL_TOKENS = ("&&", "||", ";", "|", "`", "$(", ">", "<")


class ZeroTokenJobError(ValueError):
    """Validation or policy refusal for a zero-token job or change-only monitor."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def _serialize(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for k in ("command_spec", "watch_target"):
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                pass
    for k in ("last_checked_at", "last_triggered_at", "created_at", "updated_at"):
        if k in out:
            out[k] = _iso(out[k])
    return out


def validate_command_spec(
    command_spec: Mapping[str, Any],
    *,
    allowed_executables: Optional[set[str]] = None,
) -> list[str]:
    """Validate and normalize an allowlisted argv command (no shell injection)."""
    raw_argv = command_spec.get("argv")
    if isinstance(raw_argv, list) and raw_argv:
        argv = [str(part) for part in raw_argv if str(part)]
    else:
        cmd_str = str(command_spec.get("command") or "").strip()
        if not cmd_str:
            raise ZeroTokenJobError("command_spec requires non-empty 'argv' or 'command'.")
        for bad in _FORBIDDEN_SHELL_TOKENS:
            if bad in cmd_str:
                raise ZeroTokenJobError(
                    f"command contains disallowed shell operator {bad!r}; pass explicit argv."
                )
        argv = shlex.split(cmd_str)
    if not argv:
        raise ZeroTokenJobError("command argv must not be empty.")
    exe = argv[0].rsplit("/", 1)[-1]
    allow = allowed_executables if allowed_executables is not None else DEFAULT_ALLOWED_EXECUTABLES
    if exe not in allow:
        raise ZeroTokenJobError(
            f"executable {exe!r} is not in the zero-token job allowlist ({', '.join(sorted(allow))})."
        )
    return argv


def create_job(
    connection: Connection,
    project_id: str,
    *,
    name: str,
    kind: str,
    cadence: str = "interval",
    interval_minutes: int = 15,
    command_spec: Optional[Mapping[str, Any]] = None,
    watch_target: Optional[Mapping[str, Any]] = None,
    enabled: bool = True,
    created_by: str = "boss",
) -> dict[str, Any]:
    """Create a zero-token recurring job or change-only monitor (on by default; agents or
    people may create them for the project they work on)."""
    norm_kind = str(kind or "").strip().lower()
    if norm_kind not in VALID_KINDS:
        raise ZeroTokenJobError(f"kind must be one of {sorted(VALID_KINDS)}")
    norm_cadence = str(cadence or "interval").strip().lower()
    if norm_cadence not in VALID_CADENCES:
        raise ZeroTokenJobError(f"cadence must be one of {sorted(VALID_CADENCES)}")
    interval = max(1, min(int(interval_minutes), 24 * 60 * 7))
    cmd = dict(command_spec or {})
    watch = dict(watch_target or {})
    if norm_kind in ("script", "command"):
        validate_command_spec(cmd)
    else:
        if not (watch.get("target") or watch.get("ref") or watch.get("repo")):
            raise ZeroTokenJobError(
                "change_monitor requires watch_target with at least 'target', 'ref', or 'repo'."
            )

    job_id = make_id("ztj")
    connection.execute(
        text(
            "INSERT INTO zero_token_jobs "
            "(id, project_id, name, kind, enabled, cadence, interval_minutes, "
            "command_spec, watch_target, created_by) "
            "VALUES (:id, :p, :n, :k, :e, :c, :im, CAST(:cs AS JSONB), CAST(:wt AS JSONB), :cb)"
        ),
        {
            "id": job_id,
            "p": project_id,
            "n": str(name).strip()[:200],
            "k": norm_kind,
            "e": bool(enabled),
            "c": norm_cadence,
            "im": interval,
            "cs": json.dumps(cmd),
            "wt": json.dumps(watch),
            "cb": str(created_by or "boss")[:120],
        },
    )
    emit_event(
        connection,
        project_id,
        "zero_token.job_created",
        created_by,
        "zero_token_job",
        job_id,
        {"name": name, "kind": norm_kind, "enabled": bool(enabled), "cadence": norm_cadence},
    )
    return get_job(connection, project_id, job_id)


def get_job(connection: Connection, project_id: str, job_id: str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM zero_token_jobs WHERE id = :id AND project_id = :p"),
        {"id": job_id, "p": project_id},
    ).mappings().first()
    if row is None:
        raise LookupError(f"no zero-token job {job_id} in project {project_id}")
    return _serialize(row)


def list_jobs(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT * FROM zero_token_jobs WHERE project_id = :p ORDER BY created_at DESC"),
        {"p": project_id},
    ).mappings().all()
    return [_serialize(r) for r in rows]


def list_runs(
    connection: Connection, project_id: str, job_id: str, *, limit: int = 30
) -> list[dict[str, Any]]:
    get_job(connection, project_id, job_id)
    rows = connection.execute(
        text(
            "SELECT * FROM zero_token_job_runs WHERE project_id = :p AND job_id = :j "
            "ORDER BY created_at DESC LIMIT :n"
        ),
        {"p": project_id, "j": job_id, "n": max(1, min(int(limit), 200))},
    ).mappings().all()
    return [_serialize(r) for r in rows]


def _default_command_runner(argv: list[str], cwd: Optional[str] = None, timeout: int = 30) -> tuple[int, str]:
    proc = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        check=False,
    )
    combined = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, combined.strip()[:2000]


def _record_receipt(
    connection: Connection,
    project_id: str,
    *,
    dedupe_key: str,
    effect_kind: str,
    job_id: str,
    payload: dict[str, Any],
) -> Optional[str]:
    tenancy = connection.execute(
        text(
            "SELECT w.org_id, p.workspace_id FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": project_id},
    ).mappings().first()
    if tenancy is None:
        return None
    receipt, _ = ReceiptService().record_completed(
        connection,
        str(tenancy["org_id"]),
        str(tenancy["workspace_id"]),
        project_id,
        dedupe_key,
        effect_kind,
        f"zero_token_job:{job_id}",
        payload,
        provider_ref=f"ztj:{job_id}",
    )
    return str(receipt["id"])


def run_job(
    connection: Connection,
    project_id: str,
    job_id: str,
    *,
    actor: str = "boss",
    command_runner: Optional[Callable[[list[str]], tuple[int, str]]] = None,
    revision_fetcher: Optional[Callable[[Mapping[str, Any]], str]] = None,
    agent_waker: Optional[Callable[[Mapping[str, Any], str, Optional[str]], str]] = None,
) -> dict[str, Any]:
    """Execute a zero-token command job or evaluate a change-only monitor.

    * Script/command jobs NEVER call a model (``token_cost_usd_micros == 0``).
    * Change-only monitors check the revision at 0 tokens and call ``agent_waker`` ONLY when
      the revision changed since ``last_revision``.
    """
    job = get_job(connection, project_id, job_id)
    if not job["enabled"]:
        raise ZeroTokenJobError(f"zero-token job {job_id} is disabled.")
    started = time.perf_counter()
    run_id = make_id("ztjr")
    ts = _now()

    if job["kind"] in ("script", "command"):
        argv = validate_command_spec(job["command_spec"] or {})
        runner = command_runner or (lambda args: _default_command_runner(args))
        rc, output = runner(argv)
        duration_ms = int((time.perf_counter() - started) * 1000)
        status = "ok" if rc == 0 else "failed"
        connection.execute(
            text(
                "INSERT INTO zero_token_job_runs "
                "(id, job_id, project_id, status, changed, output_summary, "
                "token_cost_usd_micros, duration_ms, created_at) "
                "VALUES (:id, :j, :p, :st, false, :out, 0, :dur, :ts)"
            ),
            {
                "id": run_id,
                "j": job_id,
                "p": project_id,
                "st": status,
                "out": output[:2000],
                "dur": duration_ms,
                "ts": ts,
            },
        )
        connection.execute(
            text(
                "UPDATE zero_token_jobs SET last_checked_at = :ts, last_triggered_at = :ts, "
                "last_status = :st, last_output = :out, updated_at = now() WHERE id = :j"
            ),
            {"ts": ts, "st": status, "out": output[:2000], "j": job_id},
        )
        receipt_id = _record_receipt(
            connection,
            project_id,
            dedupe_key=f"zero_token_job:{job_id}:{run_id}",
            effect_kind="zero_token.command_run",
            job_id=job_id,
            payload={"run_id": run_id, "exit_code": rc, "token_cost_usd_micros": 0},
        )
        emit_event(
            connection,
            project_id,
            "zero_token.job_executed",
            actor,
            "zero_token_job",
            job_id,
            {"run_id": run_id, "status": status, "exit_code": rc, "token_cost_usd_micros": 0},
        )
        return {
            "run_id": run_id,
            "job_id": job_id,
            "kind": job["kind"],
            "status": status,
            "exit_code": rc,
            "output": output,
            "model_calls": 0,
            "token_cost_usd_micros": 0,
            "receipt_id": receipt_id,
            "duration_ms": duration_ms,
        }

    # Change-only monitor
    watch = job["watch_target"] or {}
    if revision_fetcher is not None:
        current_rev = str(revision_fetcher(watch) or "").strip()
    else:
        # Deterministic fallback digest over watch target metadata
        current_rev = hashlib.sha256(json.dumps(watch, sort_keys=True).encode()).hexdigest()[:16]
    prev_rev = job.get("last_revision")
    changed = current_rev != prev_rev
    duration_ms = int((time.perf_counter() - started) * 1000)

    agent_ref: Optional[str] = None
    if not changed:
        status = "skipped_unchanged"
        output = f"Revision unchanged ({current_rev}); agent not woken."
        connection.execute(
            text(
                "INSERT INTO zero_token_job_runs "
                "(id, job_id, project_id, status, revision_before, revision_after, changed, "
                "output_summary, token_cost_usd_micros, duration_ms, created_at) "
                "VALUES (:id, :j, :p, :st, :rb, :ra, false, :out, 0, :dur, :ts)"
            ),
            {
                "id": run_id,
                "j": job_id,
                "p": project_id,
                "st": status,
                "rb": prev_rev,
                "ra": current_rev,
                "out": output,
                "dur": duration_ms,
                "ts": ts,
            },
        )
        connection.execute(
            text(
                "UPDATE zero_token_jobs SET last_checked_at = :ts, last_status = :st, "
                "last_output = :out, updated_at = now() WHERE id = :j"
            ),
            {"ts": ts, "st": status, "out": output, "j": job_id},
        )
        return {
            "run_id": run_id,
            "job_id": job_id,
            "kind": "change_monitor",
            "status": status,
            "changed": False,
            "revision_before": prev_rev,
            "revision_after": current_rev,
            "woke_agent": False,
            "agent_run_ref": None,
            "model_calls": 0,
            "token_cost_usd_micros": 0,
            "duration_ms": duration_ms,
        }

    # Revision changed: wake the configured agent / record ambient signal
    status = "triggered_agent"
    if agent_waker is not None:
        agent_ref = str(agent_waker(job, current_rev, prev_rev))
    else:
        from .ambient import signals as ambient_signals

        sig = ambient_signals.record_signal(
            connection,
            project_id=project_id,
            kind="monitor.revision_changed",
            source="zero_token_monitor",
            source_ref=f"{job_id}:{current_rev}",
            title=f"Change monitor '{job['name']}' saw revision {current_rev}",
            severity="medium",
            payload={
                "job_id": job_id,
                "revision_before": prev_rev,
                "revision_after": current_rev,
                "watch_target": watch,
            },
        )
        agent_ref = f"signal:{sig['id']}"
    output = f"Revision changed {prev_rev or '<initial>'} -> {current_rev}; woke {agent_ref}."
    connection.execute(
        text(
            "INSERT INTO zero_token_job_runs "
            "(id, job_id, project_id, status, revision_before, revision_after, changed, "
            "output_summary, agent_run_ref, token_cost_usd_micros, duration_ms, created_at) "
            "VALUES (:id, :j, :p, :st, :rb, :ra, true, :out, :ar, 0, :dur, :ts)"
        ),
        {
            "id": run_id,
            "j": job_id,
            "p": project_id,
            "st": status,
            "rb": prev_rev,
            "ra": current_rev,
            "out": output,
            "ar": agent_ref,
            "dur": duration_ms,
            "ts": ts,
        },
    )
    connection.execute(
        text(
            "UPDATE zero_token_jobs SET last_revision = :rev, last_checked_at = :ts, "
            "last_triggered_at = :ts, last_status = :st, last_output = :out, "
            "updated_at = now() WHERE id = :j"
        ),
        {"rev": current_rev, "ts": ts, "st": status, "out": output, "j": job_id},
    )
    receipt_id = _record_receipt(
        connection,
        project_id,
        dedupe_key=f"change_monitor:{job_id}:{current_rev}",
        effect_kind="zero_token.monitor_wakeup",
        job_id=job_id,
        payload={
            "run_id": run_id,
            "revision_before": prev_rev,
            "revision_after": current_rev,
            "agent_run_ref": agent_ref,
        },
    )
    emit_event(
        connection,
        project_id,
        "zero_token.monitor_triggered",
        actor,
        "zero_token_job",
        job_id,
        {
            "run_id": run_id,
            "revision_before": prev_rev,
            "revision_after": current_rev,
            "agent_run_ref": agent_ref,
        },
    )
    return {
        "run_id": run_id,
        "job_id": job_id,
        "kind": "change_monitor",
        "status": status,
        "changed": True,
        "revision_before": prev_rev,
        "revision_after": current_rev,
        "woke_agent": True,
        "agent_run_ref": agent_ref,
        "receipt_id": receipt_id,
        "token_cost_usd_micros": 0,
        "duration_ms": duration_ms,
    }


def due_jobs(connection: Connection, project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Return enabled zero-token jobs whose interval has elapsed."""
    rows = connection.execute(
        text(
            "SELECT * FROM zero_token_jobs WHERE enabled = true "
            "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p) "
            "ORDER BY created_at"
        ),
        {"p": project_id},
    ).mappings().all()
    cutoff_now = _now()
    due: list[dict[str, Any]] = []
    for r in rows:
        last = r["last_checked_at"]
        if last is None:
            due.append(_serialize(r))
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if cutoff_now >= last + timedelta(minutes=int(r["interval_minutes"] or 15)):
            due.append(_serialize(r))
    return due


__all__ = [
    "DEFAULT_ALLOWED_EXECUTABLES",
    "VALID_CADENCES",
    "VALID_KINDS",
    "ZeroTokenJobError",
    "validate_command_spec",
    "create_job",
    "get_job",
    "list_jobs",
    "list_runs",
    "run_job",
    "due_jobs",
]
