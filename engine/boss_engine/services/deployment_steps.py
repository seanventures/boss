"""Recording a deployment WHILE it happens.

The deploy used to write nothing until it returned. Everything the console could not do follows
from that one fact: no progress, no logs, no duration, and a crash that left no trace of how far
it got. Spinners drawn over that are animations, not information.

A `StepRecorder` writes each step to `deployment_runs.steps` as it starts and as it ends, in its
own transaction. Deliberately not buffered: the value of this table is highest exactly when the
process dies mid-step, and a buffer would lose precisely that.

Logs are bounded per step. Enough to show the docker error or the failing command, capped so a
runaway build cannot fill the database — and truncated at the FRONT, because the end of a log is
where the error is.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Optional

from ..ports import sql

# Per-step log cap. A docker or npm failure fits comfortably; a verbose build does not get to
# grow without bound.
LOG_TAIL_BYTES = 4000

# The steps a deployment goes through, in order. Declared so the console can render the whole
# sequence up front — greyed-out future steps tell an operator what is still to come, which a
# list that grows one row at a time cannot.
# EXACTLY the steps the pipeline emits, in the order it emits them. An aspirational list shows
# an operator phantom steps that never arrive and never complete — worse than showing fewer,
# because a step stuck at "pending" forever reads as a stall.
STEPS = ("resolve", "preflight", "build", "transfer", "observe")

STEP_LABELS = {
    "resolve": "Resolve source",
    "preflight": "Preflight checks",
    "build": "Plan and build",
    # Staging the previous release and starting the container happen inside apply(); splitting
    # them here would claim a granularity the connector does not report.
    "transfer": "Build, transfer and release",
    "observe": "Health check",
}


def _tail(text: str) -> str:
    """Keep the END of the output — that is where the error is."""
    if not text:
        return ""
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= LOG_TAIL_BYTES:
        return text
    return "…(truncated)…\n" + encoded[-LOG_TAIL_BYTES:].decode("utf-8", "replace")


class StepRecorder:
    """Appends step progress to a deployment run, one small write at a time."""

    def __init__(self, engine, deployment_run_id: str, *,
                 table: str = "deployment_runs", set_duration: bool = True):
        # PARAMETERISED RATHER THAN COPIED. Previews needed the same "what is done, what is
        # pending" ledger a deploy already has, and a second implementation of it would drift
        # from this one the first time either was touched. Only the destination differs.
        self._engine = engine
        self._run_id = deployment_run_id
        self._table = table
        self._set_duration = set_duration
        self._started = time.monotonic()
        self._steps: list[dict[str, Any]] = []

    # -- persistence ------------------------------------------------------------
    def _flush(self, *, duration_ms: Optional[int] = None) -> None:
        from ..db import begin

        try:
            payload = json.dumps(self._steps, default=str)
            with begin(self._engine) as c:
                # `app_previews` has neither `updated_at` nor `duration_ms`; deployment_runs has
                # both. The step ledger is the only thing in common, so that is all this writes
                # when the extra columns are not there.
                extra = ", updated_at = now()" if self._table == "deployment_runs" else ""
                if duration_ms is None or not self._set_duration:
                    c.execute(sql(f"UPDATE {self._table} SET steps = CAST(:s AS JSONB){extra} "
                                  "WHERE id = :i"),
                              {"s": payload, "i": self._run_id})
                else:
                    c.execute(sql(f"UPDATE {self._table} SET steps = CAST(:s AS JSONB), "
                                  f"duration_ms = :d{extra} WHERE id = :i"),
                              {"s": payload, "d": duration_ms, "i": self._run_id})
        except Exception:  # noqa: BLE001
            # Progress reporting must never break the thing it reports on. A deployment that
            # fails because its progress bar could not be written would be absurd.
            pass

    # -- api --------------------------------------------------------------------
    @contextmanager
    def step(self, name: str, *, label: Optional[str] = None):
        """Run a step, recording start, end, duration and any log it collects.

        Yields a small handle whose `.log(text)` accumulates output and `.skip(reason)` marks the
        step as deliberately not done — a skipped step is a fact worth showing, not an absence.
        """
        entry: dict[str, Any] = {
            "step": name, "label": label or STEP_LABELS.get(name, name),
            "status": "running", "started_at": _now_iso(), "ended_at": None,
            "duration_ms": None, "log": "",
        }
        self._steps.append(entry)
        self._flush()
        began = time.monotonic()

        class Handle:
            def log(_self, text: str) -> None:
                if text:
                    entry["log"] = _tail((entry["log"] or "") + str(text))

            def skip(_self, reason: str) -> None:
                entry["status"] = "skipped"
                entry["log"] = reason

            def failed(_self, reason: str) -> None:
                """Mark this step failed without raising.

                A step can complete its work and still produce a bad VERDICT — the health check
                runs perfectly and reports the app unreachable. Recording that as `ok` gave a
                stepper of all-green above an overall failure, which tells the reader the
                console is broken rather than the deployment."""
                entry["status"] = "failed"
                entry["error"] = reason[:400]

        handle = Handle()
        try:
            yield handle
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"[:400]
            entry["ended_at"] = _now_iso()
            entry["duration_ms"] = int((time.monotonic() - began) * 1000)
            self._flush()
            raise
        else:
            if entry["status"] == "running":
                entry["status"] = "ok"
            entry["ended_at"] = _now_iso()
            entry["duration_ms"] = int((time.monotonic() - began) * 1000)
            self._flush()

    def fail(self, name: str, message: str) -> None:
        """Record a step that failed without an exception — a refusal, a bad verdict."""
        self._steps.append({
            "step": name, "label": STEP_LABELS.get(name, name), "status": "failed",
            "started_at": _now_iso(), "ended_at": _now_iso(), "duration_ms": 0,
            "log": "", "error": message[:400],
        })
        self._flush()

    def finish(self) -> int:
        total = int((time.monotonic() - self._started) * 1000)
        self._flush(duration_ms=total)
        return total

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def planned_steps() -> list[dict[str, str]]:
    """The full sequence, for a console that wants to show what is still to come."""
    return [{"step": s, "label": STEP_LABELS[s], "status": "pending"} for s in STEPS]
