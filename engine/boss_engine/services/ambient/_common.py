"""Small shared pieces of the ambient package: ids, time, party names, evidence refs, text hygiene.

Nothing here decides anything. It exists so the five modules that write ambient rows agree on
three shapes a reader depends on: how a PARTY is named (`agent:sre`, `person:u_1`,
`policy:staging-rollback`, `squad:wi_9`, `boss`), how an EVIDENCE REF is written
(`{"kind": "signal", "ref": "sig_..."}`) and checked, and how text that came from outside (an
alert's summary, an issue title) is made safe to store and show.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str) -> str:
    """prefix + '_' + a ULID: time-ordered, so ids sort the way things happened (as act ids do)."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return f"{prefix}_" + "".join(reversed(chars))


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def as_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def serialize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif hasattr(value, "as_integer_ratio") and not isinstance(value, (int, float)):
            out[key] = float(value)     # Decimal
        else:
            out[key] = value
    return out


# ── parties ──────────────────────────────────────────────────────────────────────────────────
#
# WHO, AS ONE STRING. A decision is decided_by `agent:<role>` (one agent), `policy:<rule id>` (a
# standing rule a person set), `squad:<work item id>` (a consensus) or `person:<id>`; a message's
# sender adds `boss` (BOSS's own machinery: a sweep, the gate, a guard). The diagram draws one
# node per party, so the string is also the node's identity.

PARTY_KINDS = ("agent", "person", "policy", "squad")
_PARTY = re.compile(r"^(agent|person|policy|squad):([A-Za-z0-9_.@:\-]{1,120})$")


def party(value: Any, *, allow_boss: bool = True) -> str:
    raw = str(value or "").strip()
    if allow_boss and raw == "boss":
        return raw
    if not _PARTY.match(raw):
        raise ValueError(f"a party is agent:<role>, person:<id>, policy:<id> or squad:<id>"
                         f"{' or boss' if allow_boss else ''}, not {raw!r}")
    return raw


def party_kind(value: str) -> str:
    return "boss" if value == "boss" else value.split(":", 1)[0]


def party_id(value: str) -> str:
    return "boss" if value == "boss" else value.split(":", 1)[1]


def person(actor: str) -> str:
    """A person's party string from an actor id ('ceo' -> 'person:ceo'); idempotent."""
    raw = str(actor or "").strip()
    if not raw:
        raise ValueError("a person's action needs who did it")
    return raw if raw.startswith("person:") else f"person:{raw}"


# ── text from outside is data ────────────────────────────────────────────────────────────────
#
# PROMPT INJECTION FROM ISSUES AND ALERTS (design §9). A signal's title is written by whoever
# raised the alert or the issue. It is stored and shown as DATA: format characters a word can
# hide behind (zero-width, bidi controls) are removed — the same normalisation the persona lint
# uses, so what is checked is what a model later reads — and it is bounded.

def clean_text(value: Any, limit: int = 500) -> str:
    from ..agent_persona import _normalise

    return _normalise(str(value or "")).strip()[:limit]


def short_label(value: Any, words: int = 4) -> str:
    """At most `words` words: a node's label on the diagram (design §11.0 rule 2)."""
    parts = clean_text(value, 120).split()
    return " ".join(parts[:words])


# ── evidence refs ────────────────────────────────────────────────────────────────────────────
#
# EVIDENCE BOSS HAS CHECKED EXISTS (design §11.2, the investigation.py / self_fix citation rule).
# A decision may cite only what is really there: each ref kind maps to the table that holds it,
# and a ref that is not found refuses the write. Kinds with no BOSS table (a commit sha, a URL,
# a log query) are checked for shape only and say so (`checked: "shape"`).

#: kind -> (table, id column, project column or None)
EVIDENCE_TABLES: dict[str, tuple[str, str, Optional[str]]] = {
    "signal": ("signals", "id", "project_id"),
    "work_item": ("work_items", "id", "project_id"),
    "message": ("work_item_messages", "id", "project_id"),
    "act": ("agent_acts", "act_id", "project_id"),
    "decision": ("decisions", "id", "project_id"),
    "workflow_run": ("workflow_runs", "id", "project_id"),
    "step_run": ("workflow_step_runs", "id", "project_id"),
    "code_task": ("code_task_runs", "id", "project_id"),
    "alert": ("app_alerts", "id", "project_id"),
    "incident": ("incidents", "id", "project_id"),
    "kg_node": ("kg_nodes", "id", "project_id"),
    "meeting": ("meeting_runs", "id", "project_id"),
    "finding": ("system_findings", "id", "project_id"),
    "escalation": ("shepherd_escalations", "id", "project_id"),
    "deadletter": ("deadletters", "id", "project_id"),
    "receipt": ("receipts", "id", "project_id"),
    # BOSSAPP-903/909: the stores the extra adapters and Discovery read.
    "ci_wait": ("ci_waits", "id", "project_id"),
    "security_finding": ("security_findings", "id", None),
    "cost_anomaly": ("cost_anomalies", "id", "project_id"),
    "dossier": ("project_dossiers", "id", "project_id"),
}
SHAPE_ONLY: dict[str, re.Pattern[str]] = {
    "commit": re.compile(r"^[0-9a-f]{7,40}$"),
    "url": re.compile(r"^https?://\S{3,2000}$"),
    "pull_request": re.compile(r"^(https?://\S+|[\w.\-]+/[\w.\-]+#\d+)$"),
    "query": re.compile(r"^.{1,2000}$", re.S),
    "log": re.compile(r"^.{1,500}$", re.S),
    # A path inside a repository (Discovery's hotspots, TODO clusters): relative, no '..'.
    "file": re.compile(r"^(?!/)(?!.*(^|/)\.\.(/|$))[^\0\n]{1,400}$"),
}
EVIDENCE_KINDS = frozenset(EVIDENCE_TABLES) | frozenset(SHAPE_ONLY)


class EvidenceError(ValueError):
    """A cited ref is not a known kind, or does not exist."""


def normalize_evidence(items: Iterable[Any]) -> list[dict[str, Any]]:
    """[{kind, ref, note?}], from dicts or 'kind:ref' strings; refuses unknown kinds."""
    out: list[dict[str, Any]] = []
    for item in items or ():
        if isinstance(item, str):
            kind, _, ref = item.partition(":")
            item = {"kind": kind, "ref": ref}
        if not isinstance(item, Mapping):
            raise EvidenceError(f"evidence is {{kind, ref}}, not {item!r}")
        kind = str(item.get("kind") or "").strip()
        ref = str(item.get("ref") or "").strip()
        if kind not in EVIDENCE_KINDS:
            raise EvidenceError(f"unknown evidence kind {kind!r} (one of {sorted(EVIDENCE_KINDS)})")
        if not ref:
            raise EvidenceError(f"evidence of kind {kind} has no ref")
        entry: dict[str, Any] = {"kind": kind, "ref": ref[:2000]}
        note = clean_text(item.get("note"), 300)
        if note:
            entry["note"] = note
        out.append(entry)
    return out


def verify_evidence(connection: Connection, project_id: Optional[str],
                    items: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalise and CHECK every ref; returns them stamped `checked: "exists" | "shape"`.

    A ref that names a BOSS row must exist (in this project, when the table has one); anything
    else raises EvidenceError naming the first missing ref, so a ledger row never cites a ghost.
    """
    checked: list[dict[str, Any]] = []
    for entry in normalize_evidence(items):
        kind, ref = entry["kind"], entry["ref"]
        if kind in SHAPE_ONLY:
            if not SHAPE_ONLY[kind].match(ref):
                raise EvidenceError(f"{kind} ref {ref[:80]!r} is not a {kind}")
            checked.append({**entry, "checked": "shape"})
            continue
        table, column, project_column = EVIDENCE_TABLES[kind]
        exists = connection.execute(text(f"SELECT to_regclass('{table}') IS NOT NULL")).scalar()
        found = False
        if exists:
            sql = f"SELECT 1 FROM {table} WHERE {column} = :ref"
            params: dict[str, Any] = {"ref": ref}
            if project_column and project_id:
                sql += f" AND ({project_column} = :p OR {project_column} IS NULL)"
                params["p"] = project_id
            found = connection.execute(text(sql + " LIMIT 1"), params).first() is not None
        if not found:
            raise EvidenceError(f"evidence {kind}:{ref} does not exist in this project")
        checked.append({**entry, "checked": "exists"})
    return checked


def emit(connection: Connection, project_id: Optional[str], event_type: str, actor: str,
         entity_type: str, entity_id: str, data: dict[str, Any]) -> Optional[str]:
    """core.emit_event for a project row; an install-wide row (no project) has no event stream."""
    if not project_id:
        return None
    from ..core import emit_event

    return emit_event(connection, project_id, event_type, actor, entity_type, entity_id, data)
