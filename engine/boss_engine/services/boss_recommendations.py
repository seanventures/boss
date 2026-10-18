"""What BOSS should change in its own code, written down by the agents that found out.

WHY. A step's recovery agent (step_recovery) that concludes a failure is BOSS's OWN — its network
wiring, the planner reading the wrong directory, a capability it lacks, a tool its image does not
have — cannot fix it: only a code change to BOSS can. It parks the step (`wait_for_boss`) and
writes the finding HERE so a developer implements it: the date, the run/step/build ids, the
symptom, the evidence (log lines, redacted), the root-cause hypothesis, the proposed change
(files/functions when known) and a status.

WHERE IT LIVES.
  * The record: the `boss_recommendations` table, ONE ROW PER DISTINCT CAUSE (project, cause
    signature); a cause seen again adds its run to `refs` and counts an occurrence.
  * The file: `<artifact root>/boss-improvements/RECOMMENDATIONS.md` (BOSS_ARTIFACTS_DIR,
    beside every other artifact the install keeps), rewritten from the table on every record so
    it is never a second truth. The API serves the same markdown
    (GET /v1/boss-recommendations.md) for the console to show and download.
  * The event `boss.recommendation_recorded` on the run, so the run page says it.

Nothing secret enters: every text passes the agent redaction (`redact_for_agent`) first.

NO HOST ADDRESS ENTERS EITHER (self_fix_privacy). A recommendation seeds a self-fix, and a
self-fix's evidence is what a person may send to the BOSS team: every text is also host-masked
(IPs, host names, URL hosts, this install's own machines) when it is written, and again when it
is read, so a row written before the rule (the two real rows held the deploy host's public
address) is never shown or served with one.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id, now_utc

log = logging.getLogger(__name__)

EVENT = "boss.recommendation_recorded"
#: open → fixing (a self-fix is working on it) → implemented (verified on this box) → done (in a
#: release). A cause seen again after `implemented` or `done` reopens it.
STATUSES = ("open", "fixing", "implemented", "done", "wont_fix")
FILE_DIR = "boss-improvements"
FILE_NAME = "RECOMMENDATIONS.md"
MAX_REFS = 25
MAX_EVIDENCE = 12


def _clean(value: Any, limit: int = 2000, known: tuple[str, ...] = ()) -> str:
    from .agent_baseline import redact_for_agent
    from .self_fix_privacy import mask_hosts

    cleaned = redact_for_agent(str(value or ""))
    cleaned = cleaned if isinstance(cleaned, str) else ""
    return mask_hosts(cleaned, known)[:limit]


def _known(connection: Connection) -> tuple[str, ...]:
    from .self_fix_privacy import known_hosts

    return tuple(known_hosts(connection))


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def record(connection: Connection, settings: Any, *, project_id: str, signature: str,
           title: str, symptom: str = "", evidence: Optional[list[Any]] = None,
           root_cause: str = "", proposed_change: str = "",
           files: Optional[list[str]] = None, ref: Optional[Mapping[str, Any]] = None,
           actor: str = "boss", run_id: Optional[str] = None,
           boss_version: str = "") -> dict[str, Any]:
    """Record (or count again) BOSS's own cause `signature`. Returns {id, created, occurrences}.
    Never raises: a recommendation not written must not fail the recovery that found it."""
    point = connection.begin_nested()
    try:
        out = _record(connection, project_id=project_id, signature=signature, title=title,
                      symptom=symptom, evidence=evidence, root_cause=root_cause,
                      proposed_change=proposed_change, files=files, ref=ref, actor=actor,
                      run_id=run_id, boss_version=boss_version)
        point.commit()
    except Exception as error:  # noqa: BLE001
        point.rollback()
        log.warning("boss recommendation %s not recorded: %s", signature, error)
        return {"id": None, "created": False, "error": str(error)[:300]}
    write_file(connection, settings, project_id)
    return out


def _record(connection: Connection, *, project_id: str, signature: str, title: str,
            symptom: str, evidence: Optional[list[Any]], root_cause: str,
            proposed_change: str, files: Optional[list[str]], ref: Optional[Mapping[str, Any]],
            actor: str, run_id: Optional[str], boss_version: str) -> dict[str, Any]:
    entry = {**{k: v for k, v in dict(ref or {}).items() if v}, "at": now_utc().isoformat()}
    known = _known(connection)
    row = connection.execute(
        text("SELECT id, refs, occurrences FROM boss_recommendations WHERE project_id = :p "
             "AND signature = :s FOR UPDATE"), {"p": project_id, "s": signature}
    ).mappings().first()
    if row:
        refs = [r for r in _json(row["refs"], []) if isinstance(r, dict)]
        seen = any(all(r.get(k) == entry.get(k) for k in ("run_id", "step_id", "build_id",
                                                          "deployment_id")) for r in refs)
        refs = (refs + ([] if seen else [entry]))[-MAX_REFS:]
        occurrences = int(row["occurrences"] or 1) + (0 if seen else 1)
        connection.execute(
            text("UPDATE boss_recommendations SET refs = CAST(:r AS JSONB), occurrences = :n, "
                 "status = CASE WHEN status IN ('done', 'implemented') THEN 'open' "
                 "ELSE status END, "
                 "updated_at = now() WHERE id = :i"),
            {"r": json.dumps(refs), "n": occurrences, "i": row["id"]})
        return {"id": row["id"], "created": False, "occurrences": occurrences}
    rec_id = make_id("brec")
    connection.execute(
        text("INSERT INTO boss_recommendations (id, project_id, signature, title, symptom, "
             "evidence, root_cause, proposed_change, files, refs, boss_version, created_by) "
             "VALUES (:i, :p, :s, :t, :sy, CAST(:e AS JSONB), :rc, :pc, CAST(:f AS JSONB), "
             "CAST(:r AS JSONB), :v, :by)"),
        {"i": rec_id, "p": project_id, "s": signature[:300], "t": _clean(title, 300, known),
         "sy": _clean(symptom, 2000, known),
         "e": json.dumps([_clean(e, 600, known) for e in (evidence or [])[:MAX_EVIDENCE]]),
         "rc": _clean(root_cause, 2000, known), "pc": _clean(proposed_change, 4000, known),
         "f": json.dumps([_clean(f, 300, known) for f in (files or [])[:20]]),
         "r": json.dumps([entry]), "v": boss_version[:80], "by": actor[:120]})
    emit_event(connection, project_id, EVENT, actor, "workflow_run" if run_id else "project",
               run_id or project_id,
               {"recommendation_id": rec_id, "signature": signature[:300],
                "title": _clean(title, 300, known),
                **{k: v for k, v in entry.items() if k != "at"},
                "sentence": f"Recorded for BOSS's developers: {_clean(title, 200, known)}"})
    return {"id": rec_id, "created": True, "occurrences": 1}


def list_recommendations(connection: Connection, project_id: str, *,
                         status: Optional[str] = None) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT id, signature, status, title, symptom, evidence, root_cause, "
             "proposed_change, files, refs, occurrences, boss_version, created_by, created_at, "
             "updated_at, self_fix_id, class_signature, attempts FROM boss_recommendations "
             "WHERE project_id = :p "
             "AND (CAST(:s AS TEXT) IS NULL OR status = :s) ORDER BY created_at"),
        {"p": project_id, "s": status}).mappings().all()
    from .self_fix_privacy import mask_value

    known = _known(connection) if rows else ()
    out = []
    for r in rows:
        item = dict(r)
        for key in ("evidence", "files", "refs", "attempts"):
            item[key] = _json(item.get(key), [])
        # Masked on read too: a row written before the rule must never be served with an
        # address (the prose fields only; ids, the signature and the refs are BOSS's own).
        for key in ("title", "symptom", "root_cause", "proposed_change", "evidence", "files"):
            item[key] = mask_value(item.get(key), known)
        for key in ("created_at", "updated_at"):
            value = item.get(key)
            item[key] = value.isoformat() if hasattr(value, "isoformat") else value
        out.append(item)
    return out


def set_status(connection: Connection, project_id: str, rec_id: str, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    done = connection.execute(
        text("UPDATE boss_recommendations SET status = :s, updated_at = now() "
             "WHERE id = :i AND project_id = :p"), {"s": status, "i": rec_id, "p": project_id})
    return bool(done.rowcount)


def link_self_fix(connection: Connection, project_id: str, rec_id: str, self_fix_id: str,
                  class_signature: str) -> None:
    """A self-fix started on this cause: the recommendation says so (status `fixing`)."""
    connection.execute(
        text("UPDATE boss_recommendations SET status = 'fixing', self_fix_id = :f, "
             "class_signature = :c, updated_at = now() WHERE id = :i AND project_id = :p"),
        {"f": self_fix_id, "c": class_signature[:300], "i": rec_id, "p": project_id})


def record_attempt(connection: Connection, project_id: str, rec_id: str,
                   attempt: Mapping[str, Any]) -> None:
    """A self-fix for this cause ended without holding (refused, rejected, rolled back): the
    recommendation is open again, with what was learned, for the person or the next attempt."""
    from .self_fix_privacy import mask_value

    connection.execute(
        text("UPDATE boss_recommendations SET status = 'open', "
             "attempts = (COALESCE(attempts, '[]'::jsonb) || CAST(:a AS JSONB)), "
             "updated_at = now() WHERE id = :i AND project_id = :p"),
        {"a": json.dumps([mask_value(dict(attempt), _known(connection))], default=str),
         "i": rec_id, "p": project_id})


def markdown(items: list[Mapping[str, Any]]) -> str:
    """RECOMMENDATIONS.md: one section per distinct cause, newest last."""
    lines = ["# BOSS improvements recommended by its agents", "",
             "Each entry is a failure a step's recovery agent concluded was BOSS's own (wiring, "
             "planner, a missing capability, the image's tooling) — something only a change to "
             "BOSS's code can fix. One entry per distinct cause; `refs` lists every run that hit "
             "it. Steps parked for it resume by themselves after BOSS is updated.", ""]
    if not items:
        lines.append("_Nothing recorded yet._")
    for item in items:
        when = str(item.get("created_at") or "")[:10]
        lines += [f"## {item.get('title')}", "",
                  f"- **Status:** {item.get('status')}  ",
                  f"- **First seen:** {when} (BOSS {item.get('boss_version') or 'unknown'}); "
                  f"seen {item.get('occurrences') or 1} time(s)  ",
                  f"- **Signature:** `{item.get('signature')}`  ",
                  f"- **Id:** {item.get('id')}", ""]
        if item.get("symptom"):
            lines += ["**Symptom.** " + str(item["symptom"]), ""]
        evidence = [e for e in item.get("evidence") or [] if e]
        if evidence:
            lines += ["**Evidence.**", "", "```"] + [str(e) for e in evidence] + ["```", ""]
        if item.get("root_cause"):
            lines += ["**Root cause (hypothesis).** " + str(item["root_cause"]), ""]
        if item.get("proposed_change"):
            lines += ["**Proposed change.** " + str(item["proposed_change"]), ""]
        files = [f for f in item.get("files") or [] if f]
        if files:
            lines += ["**Where.** " + ", ".join(f"`{f}`" for f in files), ""]
        if item.get("self_fix_id"):
            lines += [f"**Self-fix.** {item['self_fix_id']}", ""]
        tried = [a for a in item.get("attempts") or [] if isinstance(a, dict)]
        if tried:
            lines.append("**Tried by BOSS itself, did not hold.**")
            for a in tried:
                lines.append(f"- {a.get('self_fix_id')}: {a.get('outcome')} — {a.get('why') or ''}")
            lines.append("")
        refs = [r for r in item.get("refs") or [] if isinstance(r, dict)]
        if refs:
            lines.append("**Runs.**")
            for r in refs:
                parts = [f"{k} {r[k]}" for k in ("run_id", "step_id", "build_id",
                                                  "deployment_id") if r.get(k)]
                lines.append(f"- {str(r.get('at') or '')[:16]} — " + ", ".join(parts))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def file_path(settings: Any) -> Optional[Path]:
    root = getattr(settings, "artifact_root", None) if settings is not None else None
    if root is None:
        return None
    return Path(root) / FILE_DIR / FILE_NAME


def write_file(connection: Connection, settings: Any, project_id: str) -> Optional[str]:
    """Rewrite RECOMMENDATIONS.md from the table. Best effort: the table is the record."""
    path = file_path(settings)
    if path is None:
        return None
    try:
        body = markdown(list_recommendations(connection, project_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return str(path)
    except Exception as error:  # noqa: BLE001
        log.warning("RECOMMENDATIONS.md not written: %s", error)
        return None


__all__ = ["EVENT", "FILE_NAME", "STATUSES", "file_path", "link_self_fix",
           "list_recommendations", "markdown", "record", "record_attempt", "set_status",
           "write_file"]
