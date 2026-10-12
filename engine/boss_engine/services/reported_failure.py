"""A failure the operator PASTES is a claim about the past. Answer about what is true now.

THE INCIDENT (2026-10-12, HelpDesk on 192.0.2.202).
---------------------------------------------------
The operator pasted a deploy error into Ask BOSS: "Failed at Preflight checks: /opt/boss-apps is
not writable by root on 192.0.2.202; prepare the host once: sudo mkdir …". Harbor answered the
paste: Community will not let it run a host shell, so "run the one-time host prep yourself".
Every part of that was out of date. Two later deploys of the same target had got past preflight
at 13:45 and 13:46 — BOSS now makes that directory itself — and the real problem was the app the
deploys had shipped. The answer cost the operator a sudo session for a step that no longer
existed, and said nothing about the problem they actually had.

THE DESIGN, FOR THE WHOLE CLASS.
--------------------------------
A pasted failure with no record id in it (an id goes through turn_subject, which already reads
the record) is matched to the deployment targets it names — by host, app name or address — and
those targets' latest runs are read BEFORE the model answers, into a block that opens the
prompt: what each target's latest attempts did, which step each stopped at, and whether the
pasted error belongs to an earlier attempt that a later one got past. The rules under it say:
lead with what is true now; say when the paste is stale; offer the action BOSS can run.

`summarize_now` is pure (rows in, text out) so the judgement is tested without a database;
`current_state_note` is the thin read around it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from sqlalchemy import text

_log = logging.getLogger(__name__)

#: Words a pasted failure carries. Deliberately about FAILING, not about deploys: "is HelpDesk up?"
#: is a question, not a paste, and has its own path.
_FAILURE = re.compile(
    r"\b(failed at|failed|failure|error|not writable|refused|blocked|cannot|could not|"
    r"unreachable|timed out|exited)\b", re.IGNORECASE)
#: ...about the deploy pipeline or a host — the places a stale paste can be checked against.
_DEPLOY = re.compile(
    r"\b(deploy(?:ment|ing|ed)?|preflight|health check|plan and build|transfer|release|"
    r"roll(?:ed)? back|host)\b|/opt/boss-apps|\b\d{1,3}(?:\.\d{1,3}){3}\b", re.IGNORECASE)
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

#: The pasted text names a host-preparation failure: the deploy directory, sudo, ownership.
_HOST_PREP = re.compile(
    r"/opt/boss-apps|prepare the host once|not writable by|sudo mkdir|sudo chown",
    re.IGNORECASE)

#: Steps that finished cleanly. Anything else on a run's step list is where it stopped.
_OK_STEPS = frozenset({"ok", "succeeded", "success", "skipped", "done", "passed"})


def looks_like_a_pasted_failure(question: str) -> bool:
    """A failure, about the deploy pipeline or a host. Both, or it is some other kind of turn."""
    q = question or ""
    return bool(_FAILURE.search(q) and _DEPLOY.search(q))


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _host_of(target_ref: Any) -> str:
    """`root@192.0.2.202:22` → `192.0.2.202`."""
    ref = str(target_ref or "")
    ref = ref.split("@", 1)[-1]
    return ref.rsplit(":", 1)[0] if ref.count(":") == 1 else ref


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def stopped_at(run: dict) -> str:
    """The label of the step a run stopped at, or "" when every step it recorded finished."""
    for step in _json(run.get("steps")) or []:
        if isinstance(step, dict) and _norm(step.get("status")) not in _OK_STEPS:
            return str(step.get("label") or step.get("name") or "")
    return ""


def passed_step(run: dict, label: str) -> bool:
    """Did this run get through the step named `label`?"""
    want = _norm(label)
    for step in _json(run.get("steps")) or []:
        if isinstance(step, dict) and _norm(step.get("label") or step.get("name")) == want:
            return _norm(step.get("status")) in _OK_STEPS
    return False


def matching_targets(question: str, targets: list[dict]) -> list[dict]:
    """The targets the paste names: by host address, app name or domain. If it names none and
    the project has very few, all of them — a paste from the Deploys page rarely says which."""
    q = _norm(question)
    ips = set(_IPV4.findall(question or ""))
    named = []
    for t in targets:
        host = _host_of(t.get("target_ref"))
        app = _norm(t.get("app_name"))
        domains = [str(d) for d in (_json(t.get("domains")) or []) if d]
        if (host and host in ips) or (app and len(app) > 2 and app in q) or any(
                _norm(d) and _norm(d) in q for d in domains):
            named.append(t)
    if named:
        return named[:3]
    return targets if len(targets) <= 2 else []


def _when(value: Any) -> str:
    s = str(value or "")
    return s[:16].replace("T", " ") if s else "?"


def summarize_now(question: str, targets: list[dict],
                  runs_by_target: dict[str, list[dict]]) -> Optional[str]:
    """The block that opens the prompt, or None when there is nothing current to say.

    `runs_by_target` holds each target's runs NEWEST FIRST. Pure: no reads, no clock.
    """
    if not looks_like_a_pasted_failure(question):
        return None
    chosen = matching_targets(question, targets)
    if not chosen:
        return None
    pasted = _norm(question)
    lines = ["WHAT IS TRUE NOW — read from BOSS's deployment records at the start of this "
             "turn. The operator's message may quote an EARLIER attempt."]
    stale_notes: list[str] = []
    for t in chosen:
        runs = runs_by_target.get(str(t.get("id"))) or []
        name = f"{t.get('app_name')} · {t.get('environment')} on {_host_of(t.get('target_ref'))}"
        if not runs:
            lines.append(f"- {name}: no deployment has run yet.")
            continue
        lines.append(f"- {name}:")
        for r in runs[:4]:
            where = stopped_at(r)
            reason = re.sub(r"\s+", " ", str(r.get("failure_reason") or "")).strip()[:220]
            packet = _json(r.get("release_packet")) or {}
            build = packet.get("code_task_run_id") if isinstance(packet, dict) else None
            lines.append(
                f"    {r.get('id')} — {r.get('status')} (started {_when(r.get('created_at'))})"
                + (f", stopped at {where}" if where else "")
                + (f': "{reason}"' if reason else "")
                + (f" · build {build}" if build else ""))
        latest = runs[0]
        # IS THE PASTE AN EARLIER ATTEMPT? It matches an older run's failure, and the newest run
        # either does not carry that failure or got through the step that one stopped at.
        for older in runs[1:]:
            reason = _norm(older.get("failure_reason"))
            if not reason or reason[:60] not in pasted:
                continue
            older_step = stopped_at(older)
            if _norm(latest.get("failure_reason"))[:60] != reason[:60] or (
                    older_step and passed_step(latest, older_step)):
                stale_notes.append(
                    f"The error the operator pasted is from {older.get('id')} "
                    f"({_when(older.get('created_at'))}). The latest attempt, {latest.get('id')}, "
                    + (f"got past {older_step} and " if older_step and passed_step(latest, older_step)
                       else "")
                    + f"is {latest.get('status')}"
                    + (f", stopped at {stopped_at(latest)}" if stopped_at(latest) else "")
                    + ". Say this in your first sentence, then answer about the latest attempt.")
                break
    lines.extend(stale_notes)
    lines.append(
        "RULES FOR THIS ANSWER:\n"
        "1. Lead with what is true NOW (the newest attempt above), not with the pasted text.\n"
        "2. Before asking the operator to do anything by hand, check whether BOSS can do it — a "
        "retry, `redeploy` with the build id above, the run's own Retry — and offer that through "
        "propose_action.\n"
        "3. Write commands, paths, ids and setting names in backticks.")
    if _HOST_PREP.search(question or ""):
        lines.append(
            "HOST PREPARATION IS BOSS'S JOB. A deploy's preflight now creates the app directory "
            "(/opt/boss-apps) itself — as the deploy user, then with passwordless sudo. A "
            "preflight failure about that directory is answered by retrying the deploy, not by "
            "asking the operator to run sudo. Only when the LATEST attempt says BOSS could not "
            "create it (no write access to its parent and no passwordless sudo) is it the "
            "operator's one-line step.")
    return "\n".join(lines)


def current_state_note(connection, project_id: str, question: str) -> Optional[str]:
    """`summarize_now` over the project's deployment records. Never raises: a read that fails
    leaves the turn as it was rather than costing the operator their answer."""
    if not looks_like_a_pasted_failure(question):
        return None
    try:
        targets = [dict(r) for r in connection.execute(text(
            "SELECT id, app_name, environment, target_ref, domains FROM deployment_targets "
            "WHERE project_id = :p AND COALESCE(status, 'active') <> 'retired' "
            "ORDER BY app_name"), {"p": project_id}).mappings().all()]
        chosen = matching_targets(question, targets)
        runs: dict[str, list[dict]] = {}
        for t in chosen:
            runs[str(t["id"])] = [dict(r) for r in connection.execute(text(
                "SELECT id, status, created_at, failure_reason, steps, release_packet "
                "FROM deployment_runs WHERE project_id = :p AND deployment_target_id = :t "
                "ORDER BY created_at DESC LIMIT 4"),
                {"p": project_id, "t": t["id"]}).mappings().all()]
    except Exception:  # noqa: BLE001 — the answer goes on without it
        _log.warning("could not read the current deploy state for a pasted failure",
                     exc_info=True)
        return None
    return summarize_now(question, chosen, runs)


#: A question the operator already asked in this thread. Normalised hard: a re-paste of the same
#: error differs in whitespace, case and trailing punctuation.
def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9/._-]+", " ", str(value or "").lower()).strip()


def asked_before(messages: list) -> bool:
    """Is the newest user message one they already sent earlier in this thread?"""
    users = [m for m in (messages or []) if isinstance(m, dict) and m.get("role") == "user"
             and str(m.get("content") or "").strip()]
    if len(users) < 2:
        return False
    latest = _key(users[-1].get("content"))
    return bool(latest) and any(_key(m.get("content")) == latest for m in users[:-1])


ASKED_AGAIN = (
    "THE OPERATOR HAS ASKED THIS BEFORE in this conversation. Asking again means the earlier "
    "answer did not help, or something has changed since. Re-read the current state with your "
    "tools before answering; do not repeat your earlier answer. If the record still supports "
    "it, say what you re-checked and what is different about the next step you offer.")
