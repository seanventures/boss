"""AN ATTEMPT WHOSE REPAIR CONTINUED IS NOT A FAILURE (e2e recipe-box walk, 2026-11-18).

A build that fails its checks hands its work to a repair attempt (`review_repair_next_run`) and
ends `blocked`. Its page went on saying "It failed: Blocked: this attempt failed its checks… YOU:
Read the failure and press Retry", with Steer and Stop, long after the repair it handed on to had
merged. An attempt like that is "Continued in attempt N", with where the chain ended; the page
links the attempt and offers no Retry, no Steer and no Stop on it.

`continued_in` follows the chain once; the build page, its wait line, the Shepherd's triage and
the build's own state all read it, so none decides it alone.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from sqlalchemy import text

#: The statuses of an attempt that handed its work on (a live one keeps its own state).
HANDED_ON_STATUSES = frozenset({"blocked", "repairing", "failed"})
_CHAIN_CAP = 12


def _ctx(run: Mapping[str, Any]) -> dict[str, Any]:
    raw = run.get("finalize_ctx")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return raw if isinstance(raw, dict) else {}


def next_attempt_id(run: Mapping[str, Any]) -> str:
    """The attempt this one's repair continued in, or ""."""
    return str(_ctx(run).get("review_repair_next_run") or "").strip()


def handed_on(run: Mapping[str, Any]) -> bool:
    """This attempt ended and its repair continued in another (pure: no lookup)."""
    status = str(run.get("status") or "")
    phase = str(run.get("phase") or "")
    if not next_attempt_id(run):
        return False
    return status in ("blocked", "failed") or (status == "repairing" and phase == "done")


def _outcome(last: Mapping[str, Any]) -> str:
    status = str(last.get("status") or "")
    pr = last.get("pull_request_number")
    ref = f"change #{pr}" if pr else "its change"
    merged = bool(str((_ctx(last).get("merge") or {}).get("sha") or "").strip()) \
        if isinstance(_ctx(last).get("merge"), dict) else False
    if status == "completed":
        return f"merged as {ref}" if merged and pr else (f"opened {ref}" if pr else "finished")
    if status in ("running", "repairing", "queued", "waiting_for_github", "waiting_on_person"):
        return "still working"
    if status == "cancelled":
        return "stopped"
    return "stopped without passing"


def continued_in(connection: Any, run: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Where this attempt's repair went: {run_id, attempt, last_run_id, last_status, outcome,
    words}. None when the attempt did not hand on (or the next attempt does not exist)."""
    if not handed_on(run):
        return None
    first = next_attempt_id(run)
    project = run.get("project_id")
    seen = {str(run.get("id") or "")}
    chain: list[dict[str, Any]] = []
    nxt = first
    while nxt and nxt not in seen and len(chain) < _CHAIN_CAP:
        seen.add(nxt)
        try:
            with connection.begin_nested():
                row = connection.execute(text(
                    "SELECT id, status, phase, pull_request_number, finalize_ctx "
                    "FROM code_task_runs WHERE id = :i AND (CAST(:p AS text) IS NULL "
                    "OR project_id = :p)"), {"i": nxt, "p": project}).mappings().first()
        except Exception:  # noqa: BLE001 — the page still renders the attempt as it is
            row = None
        if row is None:
            break
        chain.append(dict(row))
        nxt = next_attempt_id(row) if handed_on(row) else ""
    if not chain:
        return None
    head, last = chain[0], chain[-1]
    attempt = int(_ctx(head).get("attempt") or 0) or None
    name = f"attempt {attempt}" if attempt else "the next attempt"
    outcome = _outcome(last)
    return {"run_id": str(head["id"]), "attempt": attempt, "last_run_id": str(last["id"]),
            "last_status": str(last.get("status") or ""), "outcome": outcome,
            "words": f"Continued in {name} · {outcome}"}
