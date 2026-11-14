"""Side-effect classes and UNKNOWN_OUTCOME reconciliation for the jobs plane (BOSSAPP-986).

THE PROBLEM THIS SOLVES
-----------------------
A lease, a timeout and a retry are the right machinery for work that is safe to run twice.
They are exactly the wrong machinery for work that is not: when a worker died between asking
GitHub to merge and recording the answer, the retry asked again — and the plane had no way to
even SAY "the merge may have happened" (BOSSAPP-975 parked-run authority, 982
approve-then-silent-revert are the recorded cousins). Three rules close that:

  1. Every externally effectful job kind DECLARES its side-effect class here, and its effect
     is deduped by a stable operation id — the receipt dedupe_key the effect seam already
     writes (services/receipts.py). Dispatch of the effect dedupes on that id, not on hope.
  2. An IRREVERSIBLE operation whose outcome is ambiguous (the call may or may not have
     reached the provider) is never blind-retried. The job parks with
     ``last_error.code = 'unknown_outcome'`` and its in-flight receipts move to the
     first-class status ``unknown_outcome``.
  3. A RECONCILER asks the provider what actually happened — the provider can always answer
     "did PR #N merge?" — and only then releases the job to retry (the effect seam's settled
     check makes that retry a bookkeeping pass, not a second merge) or lets it fail for real.

WHAT BELONGS IN THE REGISTRY
----------------------------
Only kinds whose execution leaves marks OUTSIDE BOSS's own database. A job that writes
artifacts, decisions and events is transactional with its own bookkeeping and needs none of
this: its rollback IS its compensation. The classes:

  pure           no external effect; retry is free (plans, agent briefs, meeting steps)
  idempotent     the provider dedupes or the call converges (push the same sha; a webhook
                 delivery carrying its delivery_id)
  compensatable  the effect can be undone by a later call (close a PR, destroy a droplet,
                 roll back a deployment)
  irreversible   no undo exists (a merge, a deleted droplet, a delivered message)
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, now_utc

logger = logging.getLogger(__name__)

PURE = "pure"
IDEMPOTENT = "idempotent"
COMPENSATABLE = "compensatable"
IRREVERSIBLE = "irreversible"

SIDE_EFFECT_CLASSES = frozenset({PURE, IDEMPOTENT, COMPENSATABLE, IRREVERSIBLE})

#: The FIRST honest set: the externally effectful kinds the worker actually runs today
#: (runner.capabilities), classified by what a duplicate would do — not a grand registry.
#: A kind not listed here is undeclared: the plane treats it as it always has.
JOB_KIND_CLASSES: dict[str, str] = {
    # GitHub
    "github.pull_request.merge": IRREVERSIBLE,   # a merged PR cannot be un-merged
    "change_set.merge": IRREVERSIBLE,            # a multi-repo merge set, same finality
    "software_factory.propose_pr": COMPENSATABLE,  # a duplicate PR can be closed
    "github.pull_request": COMPENSATABLE,
    "github.deployment.create": COMPENSATABLE,   # a deployment can be rolled back
    "docs.update": COMPENSATABLE,                # lands as a PR
    "repo.push_pending": IDEMPOTENT,             # pushing the same branch/sha converges
    # Infrastructure
    "infra.droplet.create": COMPENSATABLE,       # the janitor can destroy a duplicate
    "infra.droplet.delete": IRREVERSIBLE,        # a deleted server is gone
    "deployment.stack_apply": COMPENSATABLE,
    "deployment.host_apply": COMPENSATABLE,
    # Messages out of BOSS
    "comms.outbox.deliver": IRREVERSIBLE,        # a sent message cannot be unsent
    "webhook.outbound.deliver": IDEMPOTENT,      # the delivery carries its delivery_id
    # Internal-only work, declared pure so the declaration is explicit, not an omission
    "plan.generate": PURE,
    "plan.command": PURE,
    "meeting.step": PURE,
    # An agent run resumes from its last checkpoint; each TOOL it calls declares its own class
    # and an irreversible one that may have happened parks (kernel/idempotency.py).
    "agent.run": IDEMPOTENT,
    "openai.plan": PURE,
}


def side_effect_class(kind: str) -> Optional[str]:
    """The declared class for a job kind, or None for an undeclared kind."""
    return JOB_KIND_CLASSES.get(str(kind or ""))


# --------------------------------------------------------------------------- #
# Ambiguity: did the effect maybe happen?
# --------------------------------------------------------------------------- #
#: The request may have REACHED the provider before the connection died: a timeout after
#: sending, a reset mid-response, a gateway that lost its upstream, a server that crashed
#: while working. Matched case-insensitively against the error's type and message.
_AMBIGUOUS_MARKS = (
    "timed out", "timeout", "readtimeout", "read timed",
    "connection reset", "connection aborted", "connectionreset",
    "broken pipe", "remoteprotocolerror", "remote protocol",
    "server disconnected", "incomplete read", "eof occurred",
    "bad gateway", "502", "504",
    "internal server error", "500 ",
    "control plane crashed",
)
#: The request never got through, so the effect cannot have happened. Checked FIRST:
#: "connection refused" must not match an ambiguous mark by accident.
_UNAMBIGUOUS_MARKS = (
    "connection refused", "connecterror", "could not connect",
    "name or service not known", "temporary failure in name resolution",
    "service unavailable", "503",
    "401", "403", "404", "422", "unauthorized", "forbidden", "not found",
)


def ambiguous_outcome(error: dict[str, Any]) -> bool:
    """True when this failure leaves the external outcome unknown.

    Deliberately conservative in both directions: a refused connection or a 4xx is a knowable
    "did not happen"; only marks of a call that may have been in flight count as ambiguous.
    """
    if not isinstance(error, dict):
        return False
    if str(error.get("code") or "") == "unknown_outcome":
        return True
    blob = f"{error.get('type') or ''} {error.get('message') or ''}".lower()
    if not blob.strip():
        return False
    if any(mark in blob for mark in _UNAMBIGUOUS_MARKS):
        return False
    return any(mark in blob for mark in _AMBIGUOUS_MARKS)


def must_park_unknown(kind: str, error: dict[str, Any]) -> bool:
    """The never-blind-retry rule: irreversible effect + ambiguous failure = park, reconcile."""
    return side_effect_class(kind) == IRREVERSIBLE and ambiguous_outcome(error)


# --------------------------------------------------------------------------- #
# Parking and releasing
# --------------------------------------------------------------------------- #
#: How long a parked job stays out of every claim query. The reconciler normally resolves it
#: in minutes; this is the ceiling if the reconciler itself is never running.
HOLD = timedelta(days=7)
#: Reconciliation backoff: first ask almost immediately, then double, capped at an hour.
RECONCILE_FIRST_SECONDS = 15
RECONCILE_MAX_SECONDS = 3600
#: After this many unanswered asks the job is retired to a person instead of waiting forever.
RECONCILE_MAX_ATTEMPTS = 48

UNKNOWN_OUTCOME = "unknown_outcome"


def park_job_unknown_outcome(
    connection: Connection,
    job_row: dict[str, Any],
    error: dict[str, Any],
    *,
    actor: str,
    cause: str,
) -> dict[str, Any]:
    """Park a job whose irreversible effect has an ambiguous outcome.

    The job keeps its attempt count (parking is not an attempt), loses its lease, and leaves
    every claim query for HOLD — only the reconciler releases it earlier, once the provider
    has answered. Its in-flight receipts move to the first-class ``unknown_outcome`` status so
    the Receipts screen says what is actually known: nothing, yet.
    """
    job_id = job_row["id"]
    project_id = job_row["project_id"]
    current_time = now_utc()
    kind = job_row["kind"]
    held_error = {
        "code": UNKNOWN_OUTCOME,
        "kind": kind,
        "message": (
            f"The {kind} call ended without an answer ({cause}). The effect may have "
            "happened, so BOSS is checking with the provider before any retry — an "
            "irreversible operation is never retried blind."
        ),
        "cause": cause[:300],
        "original_error": {k: str(error.get(k) or "")[:500] for k in ("type", "message", "code")
                           if error.get(k)},
        "parked_at": current_time.isoformat(),
        "reconcile_attempts": 0,
        "reconcile_after": (current_time
                            + timedelta(seconds=RECONCILE_FIRST_SECONDS)).isoformat(),
    }
    connection.execute(
        text(
            """
            UPDATE jobs
            SET status = 'failed_retryable',
                lease_owner = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                last_error = CAST(:last_error AS JSONB),
                run_after = :run_after,
                updated_at = :updated_at
            WHERE id = :job_id
            """
        ),
        {
            "job_id": job_id,
            "last_error": json.dumps(held_error),
            "run_after": current_time + HOLD,
            "updated_at": current_time,
        },
    )
    parked_receipts = connection.execute(
        text(
            """
            UPDATE receipts
            SET status = :unknown,
                error = COALESCE(error, '{}'::jsonb) || CAST(:note AS JSONB),
                updated_at = :updated_at
            WHERE created_by_job_id = :job_id AND status = 'in_progress'
            RETURNING id
            """
        ),
        {
            "job_id": job_id,
            "unknown": UNKNOWN_OUTCOME,
            "note": json.dumps({"unknown_outcome": cause[:300]}),
            "updated_at": current_time,
        },
    ).scalars().all()
    emit_event(
        connection,
        project_id,
        "job.unknown_outcome",
        actor,
        "job",
        job_id,
        {"kind": kind, "cause": cause[:300], "receipt_ids": list(parked_receipts),
         "attempt": int(job_row.get("attempt") or 0)},
    )
    return {"job_id": job_id, "status": "failed_retryable", "held": UNKNOWN_OUTCOME,
            "run_after": (current_time + HOLD).isoformat(), "deadletter_id": None}


def _release_job(connection: Connection, job_row: dict[str, Any], *, found: str,
                 detail: dict[str, Any], actor: str) -> None:
    """Let a parked job run again, now that the outcome is KNOWN.

    Releasing is safe in both directions because the effect seam dedupes: a merge that DID
    land is recognised by the settled check (software_factory._pull_request_settled) and
    completed as bookkeeping; one that did not is a plain retry of something that never ran.
    """
    current_time = now_utc()
    connection.execute(
        text(
            """
            UPDATE jobs
            SET run_after = :now,
                last_error = COALESCE(last_error, '{}'::jsonb)
                    || jsonb_build_object('code', 'unknown_outcome_resolved',
                                          'found', CAST(:found AS TEXT),
                                          'resolved_at', CAST(:resolved_at AS TEXT)),
                updated_at = :now
            WHERE id = :job_id AND status = 'failed_retryable'
              AND last_error->>'code' = 'unknown_outcome'
            """
        ),
        {"job_id": job_row["id"], "now": current_time, "found": found,
         "resolved_at": current_time.isoformat()},
    )
    emit_event(connection, job_row["project_id"], "job.unknown_outcome_resolved", actor,
               "job", job_row["id"], {"kind": job_row["kind"], "found": found, **detail})


def _postpone(connection: Connection, job_row: dict[str, Any], last_error: dict[str, Any],
              *, note: str) -> None:
    """The provider could not answer; ask again later, with backoff, bounded."""
    attempts = int(last_error.get("reconcile_attempts") or 0) + 1
    delay = min(RECONCILE_MAX_SECONDS, RECONCILE_FIRST_SECONDS * (2 ** min(attempts, 8)))
    current_time = now_utc()
    last_error = {
        **last_error,
        "reconcile_attempts": attempts,
        "reconcile_after": (current_time + timedelta(seconds=delay)).isoformat(),
        "reconcile_note": note[:300],
    }
    connection.execute(
        text("UPDATE jobs SET last_error = CAST(:e AS JSONB), updated_at = :now "
             "WHERE id = :job_id"),
        {"job_id": job_row["id"], "e": json.dumps(last_error), "now": current_time},
    )


def _retire(connection: Connection, settings: Any, job_row: dict[str, Any],
            error: dict[str, Any], *, actor: str) -> None:
    """End a parked job through the plane's one terminal path (deadletter, run told why)."""
    from .jobs import JobLeaseService

    row = connection.execute(
        text("""
            SELECT id, project_id, task_id, work_item_id, tracker_binding_id, allocation_id,
                   kind, payload, status, attempt, max_attempts, data_boundary,
                   workload_class, resource_units
            FROM jobs WHERE id = :id FOR UPDATE
            """),
        {"id": job_row["id"]},
    ).mappings().first()
    if not row or row["status"] != "failed_retryable":
        return
    JobLeaseService(settings)._deadletter(  # noqa: SLF001 — the plane's one terminal path
        connection, row, error, actor=actor)


def _retire_unanswerable(connection: Connection, settings: Any, job_row: dict[str, Any],
                         last_error: dict[str, Any], *, actor: str) -> None:
    """Every ask went unanswered: a person must check. In words."""
    _retire(connection, settings, job_row,
            {"type": "UnknownOutcome", "code": UNKNOWN_OUTCOME,
             "message": (f"The {job_row['kind']}'s outcome is unknown: its call ended without "
                         f"an answer and the provider could not be reached in "
                         f"{last_error.get('reconcile_attempts')} checks. A person must look "
                         "before anything retries it."),
             "original_error": last_error.get("original_error")},
            actor=actor)


# --------------------------------------------------------------------------- #
# Reconcilers: ask the provider what actually happened
# --------------------------------------------------------------------------- #
# A reconciler takes (connection, settings, job_row, payload) and answers one question —
# did the effect happen? — as ("happened" | "did_not_happen" | "unknown", detail dict).
Reconciler = Callable[[Connection, Any, dict[str, Any], dict[str, Any]],
                      tuple[str, dict[str, Any]]]


def _reconcile_pull_request_merge(connection: Connection, settings: Any,
                                  job_row: dict[str, Any],
                                  payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Did the merge land? The repository host can always answer for a PR it hosts."""
    from .ci_gate import parse_pr_url
    from .software_factory import SoftwareFactoryService

    url = str(payload.get("pull_request_url") or "")
    number = payload.get("pull_request_number")
    owner_repo = (parse_pr_url(url) or (None, None))[0] or ""
    if "/" not in owner_repo or number is None:
        return "unknown", {"note": "the job payload does not name a pull request"}
    owner, repo = owner_repo.split("/", 1)
    factory = SoftwareFactoryService(settings, owner=owner, repo=repo)
    try:
        factory.bind_project_github(connection, job_row["project_id"], owner=owner, repo=repo)
    except Exception:  # noqa: BLE001 — the env-default adapter may still answer
        logger.debug("merge reconciler could not bind the project's GitHub", exc_info=True)
    settled = factory._pull_request_settled(int(number))  # noqa: SLF001 — the one settled read
    if settled is None:
        # None means open OR the host cannot say; only an explicit open PR is a knowable no.
        read = getattr(factory.github, "pull_request_state", None)
        if callable(read):
            try:
                pr = read(int(number)) or {}
            except Exception as exc:  # noqa: BLE001
                return "unknown", {"note": f"the host did not answer: {exc}"[:300]}
            if str(pr.get("state") or "") == "open":
                return "did_not_happen", {"pull_request_state": "open"}
        return "unknown", {"note": "the host could not say whether this merge landed"}
    if settled.get("state") == "merged":
        return "happened", {"merged_sha": settled.get("sha")}
    return "did_not_happen", {"pull_request_state": settled.get("state")}


RECONCILERS: dict[str, Reconciler] = {
    "github.pull_request.merge": _reconcile_pull_request_merge,
}


def _resolve_happened(connection: Connection, job_row: dict[str, Any],
                      detail: dict[str, Any], *, actor: str) -> None:
    """The effect DID land: say so on the receipts, then release the job so its retry can
    finish the bookkeeping (the effect seam recognises a settled effect and never re-fires)."""
    connection.execute(
        text(
            """
            UPDATE receipts
            SET status = 'succeeded',
                result = COALESCE(result, '{}'::jsonb) || CAST(:found AS JSONB),
                updated_at = :now
            WHERE created_by_job_id = :job_id AND status = :unknown
            """
        ),
        {"job_id": job_row["id"], "unknown": UNKNOWN_OUTCOME, "now": now_utc(),
         "found": json.dumps({"reconciled": True, **detail})},
    )
    _release_job(connection, job_row, found="happened", detail=detail, actor=actor)


def _resolve_did_not_happen(connection: Connection, settings: Any, job_row: dict[str, Any],
                            detail: dict[str, Any], *, actor: str) -> None:
    """The effect did NOT land: the receipts say so and the retry is an ordinary first try —
    unless the job's attempts are already spent, in which case it ends honestly, in words
    (every release-then-ambiguous-timeout cycle costs an attempt, so this bounds the loop)."""
    connection.execute(
        text(
            """
            UPDATE receipts
            SET status = 'failed',
                error = COALESCE(error, '{}'::jsonb) || CAST(:found AS JSONB),
                updated_at = :now
            WHERE created_by_job_id = :job_id AND status = :unknown
            """
        ),
        {"job_id": job_row["id"], "unknown": UNKNOWN_OUTCOME, "now": now_utc(),
         "found": json.dumps({"reconciled": True, "message":
                              "the provider confirmed this effect never happened", **detail})},
    )
    attempts = connection.execute(
        text("SELECT attempt, max_attempts FROM jobs WHERE id = :id"),
        {"id": job_row["id"]},
    ).one()
    if int(attempts[0] or 0) >= int(attempts[1] or 1):
        _retire(connection, settings, job_row,
                {"type": "UnknownOutcome", "code": UNKNOWN_OUTCOME,
                 "message": (f"The provider confirmed the {job_row['kind']} never happened, "
                             f"and every attempt ({attempts[0]} of {attempts[1]}) is spent. "
                             "BOSS stopped retrying it; a person must decide what happens "
                             "next.")},
                actor=actor)
        return
    _release_job(connection, job_row, found="did_not_happen", detail=detail, actor=actor)


def reconcile_unknown_outcomes(engine: Any, settings: Any, *, actor: str = "reconciler",
                               limit: int = 10,
                               reconcilers: Optional[dict[str, Reconciler]] = None
                               ) -> dict[str, int]:
    """One pass over every parked job whose next ask is due. Runs from the reaper tick.

    Each job is settled in its own transaction so one provider hiccup never holds the rest.
    """
    out = {"resolved_happened": 0, "resolved_did_not_happen": 0, "still_unknown": 0,
           "retired": 0}
    with engine.begin() as connection:
        due = connection.execute(
            text(
                """
                SELECT id FROM jobs
                WHERE status = 'failed_retryable'
                  AND last_error->>'code' = 'unknown_outcome'
                  AND COALESCE((last_error->>'reconcile_after')::timestamptz, now()) <= now()
                ORDER BY updated_at ASC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).scalars().all()
    for job_id in due:
        try:
            with engine.begin() as connection:
                row = connection.execute(
                    text("SELECT id, project_id, kind, payload, last_error FROM jobs "
                         "WHERE id = :id AND status = 'failed_retryable' "
                         "AND last_error->>'code' = 'unknown_outcome' "
                         "FOR UPDATE SKIP LOCKED"),
                    {"id": job_id},
                ).mappings().first()
                if not row:
                    continue
                payload = row["payload"] or {}
                if isinstance(payload, str):
                    payload = json.loads(payload)
                last_error = row["last_error"] or {}
                if isinstance(last_error, str):
                    last_error = json.loads(last_error)
                reconciler = (reconcilers or RECONCILERS).get(row["kind"])
                if reconciler is None:
                    found, detail = "unknown", {"note": f"no reconciler knows {row['kind']}"}
                else:
                    found, detail = reconciler(connection, settings, dict(row), payload)
                if found == "happened":
                    _resolve_happened(connection, dict(row), detail, actor=actor)
                    out["resolved_happened"] += 1
                elif found == "did_not_happen":
                    _resolve_did_not_happen(connection, settings, dict(row), detail,
                                            actor=actor)
                    out["resolved_did_not_happen"] += 1
                elif int(last_error.get("reconcile_attempts") or 0) + 1 >= RECONCILE_MAX_ATTEMPTS:
                    _retire_unanswerable(connection, settings, dict(row), last_error,
                                         actor=actor)
                    out["retired"] += 1
                else:
                    _postpone(connection, dict(row), last_error,
                              note=str(detail.get("note") or ""))
                    out["still_unknown"] += 1
        except Exception:  # noqa: BLE001 — one job's provider error never stops the sweep
            logger.warning("unknown-outcome reconciliation failed for %s", job_id,
                           exc_info=True)
    return out
