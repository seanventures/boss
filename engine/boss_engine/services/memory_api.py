"""THE MEMORY API (AP-P6, BOSSAPP-1057; AGENT_PLATFORM.md §4.6): one namespaced interface over the
stores BOSS already has.

    memory_api.read(connection, namespaces, project_id=..., query=..., budget_chars=...)
    memory_api.write(connection, namespace, body, project_id=..., actor=..., kind=...)

Before this, each store reached a different path: the knowledge graph's learnings reached builds
and agent steps but never chat; `agent_memory` reached chat alone, at 2,400 characters with no
relevance; procedural priors reached only the native coder and were built by a manual script.
Now every surface reads and writes memory the same way, and `remember` / `recall` are this API.

NAMESPACES (LangGraph store convention, written "kind:ref" in a spec's `memory.read/write`):

    ("thread", tid)          agent_session_ledger — where one long conversation had got to
    ("agent", key)           agent_memory, scope `role` = the agent — what it learned, across threads
    ("project",)             agent_memory, scope project/org, plus (read) the knowledge graph's
                             quoted pack: rules a person approved, learnings agents inferred
    ("user", uid)            agent_memory, scope `actor` — one person's preferences
    ("org",)                 agent_memory, scope `org`
    ("org", oid, "priors")   procedural priors (read-only: built from executed runs, by the job)

SEMANTICS ARE KEPT, NOT RE-INVENTED:

  * TRUST TIERS. A lesson is a rule: it arrives `proposed` (agent_memory.remember) and is never
    injected until a person reviews it (`review`). The KG pack keeps its own tiers: approved
    rules are binding, inferred learnings are quoted "check before relying" (learning_context).
  * DECAY per kg/learning.py: an entry's weight is `usefulness(uses, 0)` halved every 180 days
    unused (`decay`), and below the KG's drop line (learning_context.DROP_BELOW) or past its
    archive rule it is no longer injected — so a reviewed rule nobody has relied on for months
    stops spending every prompt's budget, exactly as an unused graph learning does.
  * BUDGET. Everything read ends in a prompt, so `read` returns rendered text inside a hard
    character budget, most binding first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

LOG = logging.getLogger(__name__)

#: The kinds of namespace.
THREAD, AGENT, PROJECT, USER, ORG = "thread", "agent", "project", "user", "org"
PRIORS = "priors"
KINDS = (THREAD, AGENT, PROJECT, USER, ORG)

#: agent_memory scope for each namespace kind that lives there.
_SCOPE = {AGENT: "role", PROJECT: "project", USER: "actor", ORG: "org"}
#: The kind a write takes when the caller names none: what an agent learns about its own work
#: is a lesson (reviewed before it binds); what it learns about a project is context; what it
#: learns about a person is a preference.
_DEFAULT_KIND = {AGENT: "lesson", PROJECT: "project", USER: "preference", ORG: "project"}

#: The callable job that folds executed runs into procedural priors (P3 schedules it as a bot).
PRIORS_JOB = "memory.priors_build"

DEFAULT_BUDGET_CHARS = 2_400


class MemoryAPIError(ValueError):
    """A request the API refuses, in words the caller (or the model) can act on."""


@dataclass(frozen=True)
class Namespace:
    kind: str
    ref: str = ""
    sub: str = ""

    def __str__(self) -> str:
        return ":".join(p for p in (self.kind, self.ref, self.sub) if p)


def parse(namespace: Any, *, defaults: Optional[dict] = None) -> Namespace:
    """("agent", "qa") / "agent:qa" / "project" / "project:kg" / "org:org_x:priors" →
    Namespace. `defaults` fills a missing ref ({"agent": role, "thread": tid, "user": uid}),
    and "agent:self" resolves to the run's own agent."""
    if isinstance(namespace, Namespace):
        parts = [namespace.kind, namespace.ref, namespace.sub]
    elif isinstance(namespace, (tuple, list)):
        parts = [str(p) for p in namespace]
    else:
        parts = [p for p in str(namespace or "").strip().split(":")]
    parts = [p.strip() for p in parts if p is not None]
    kind = (parts[0] if parts else "").lower()
    if kind not in KINDS:
        raise MemoryAPIError(f"unknown memory namespace {namespace!r}; the kinds are "
                             f"{', '.join(KINDS)}")
    ref = parts[1] if len(parts) > 1 else ""
    sub = parts[2] if len(parts) > 2 else ""
    if kind == ORG and ref == PRIORS:
        ref, sub = "", PRIORS
    if kind == PROJECT and ref == "kg":
        ref, sub = "", "kg"
    if ref in ("", "self") and kind in (AGENT, THREAD, USER):
        ref = str((defaults or {}).get(kind) or "")
    return Namespace(kind, ref, sub)


# ── Decay (kg/learning.py's rules, applied to agent_memory) ───────────────────────────────────

def weight_of(entry: dict, *, now: Optional[datetime] = None) -> dict:
    """{weight, days_unused, dropped}: the KG learning rule for one agent_memory entry. Unused
    since the last of (a real use, a person's review, its creation)."""
    from .knowledge.learning import (apply_learning, days_between)
    from .knowledge.learning_context import DROP_BELOW

    now = now or datetime.now(timezone.utc)
    last = max((t for t in (entry.get("last_used_at"), entry.get("reviewed_at"),
                            entry.get("created_at")) if t is not None),
               key=lambda t: t if t.tzinfo else t.replace(tzinfo=timezone.utc), default=None)
    stat = apply_learning({"success_count": int(entry.get("use_count") or 0),
                           "failure_count": 0, "last_used_at": last,
                           "trust_tier": "approved"}, now=now)
    return {"weight": stat["weight"], "days_unused": days_between(now, last),
            "dropped": stat["weight"] < DROP_BELOW or stat["archived"]}


# ── Reading ───────────────────────────────────────────────────────────────────────────────────

def _entries(connection: Any, namespaces: Sequence[Namespace], *, project_id: str,
             query: str, now: Optional[datetime], include_proposed: bool = False) -> list:
    """The agent_memory entries the namespaces name, active (and not decayed) only."""
    from .agent_memory import recall

    wanted = [n for n in namespaces if n.kind in _SCOPE and n.sub != PRIORS and n.sub != "kg"]
    if not wanted:
        return []
    actor = next((n.ref for n in wanted if n.kind == USER and n.ref), None)
    role = next((n.ref for n in wanted if n.kind == AGENT and n.ref), None)
    rows = recall(connection, project_id=project_id, actor=actor, role=role,
                  include_proposed=include_proposed, query=query or None, limit=200)
    scopes = set()
    for n in wanted:
        scopes.add(_SCOPE[n.kind])
        if n.kind == PROJECT:
            scopes.add("org")  # a project reads the org's rules too, as recall always did
    out = []
    for row in rows:
        if row["scope"] not in scopes:
            continue
        decay = weight_of(row, now=now)
        if decay["dropped"] and row.get("status") == "active":
            continue
        out.append({**row, **decay})
    return out


def read(connection: Any, namespaces: Iterable[Any], *, project_id: str,
         actor: Optional[str] = None, role: Optional[str] = None,
         thread_id: Optional[str] = None, query: str = "",
         budget_chars: int = DEFAULT_BUDGET_CHARS, run_id: str = "", step: str = "agent",
         now: Optional[datetime] = None) -> dict:
    """The memory the namespaces hold, rendered for a prompt within `budget_chars`:
    {"text", "items": [{id, kind, scope, body, weight}], "kg": quoted pack text}.

    Order of the text: what this project/agent/person already knows (rules first), where this
    session had got to (thread), then the knowledge graph's quoted pack — query-dependent, so
    LAST, after everything that is the same turn to turn."""
    defaults = {AGENT: role or "", THREAD: thread_id or "", USER: actor or ""}
    parsed = [parse(n, defaults=defaults) for n in namespaces]
    entries = _entries(connection, parsed, project_id=project_id, query=query, now=now)
    text = _render(connection, entries, budget_chars=budget_chars)
    for ns in parsed:
        if ns.kind == THREAD and ns.ref:
            from .agent_memory import pack_ledger

            text += pack_ledger(connection, thread_id=ns.ref)
    kg = ""
    if any(n.kind == PROJECT for n in parsed) and str(query or "").strip():
        kg = kg_pack(connection, project_id=project_id, query=query, run_id=run_id, step=step)
        if kg:
            from .knowledge.runtime import append_quoted_pack

            # Quoted, never an instruction (runtime.append_quoted_pack's frame), and last.
            text = text + "\n\n" + append_quoted_pack("", kg)
    return {"text": text, "items": [
        {k: e.get(k) for k in ("id", "kind", "scope", "scope_ref", "body", "status", "weight")}
        for e in entries], "kg": kg}


def kg_pack(connection: Any, *, project_id: str, query: str, run_id: str = "",
            step: str = "agent") -> str:
    """The knowledge graph's quoted pack for this query (rules in scope, learned before), under
    the project's knowledge switch — the pack agent steps already got, now on every surface."""
    try:
        from .knowledge.runtime import quoted_pack_for_turn

        return quoted_pack_for_turn(connection, project_id=project_id, query=query,
                                    run_id=run_id, step=step)
    except Exception:  # noqa: BLE001 — memory is an aid, never a dependency
        LOG.warning("knowledge pack skipped", exc_info=True)
        return ""


def _render(connection: Any, entries: list, *, budget_chars: int) -> str:
    """agent_memory's block (the same words pack_for_turn always used), most binding first,
    whole entries only; injection is counted as injection, never as use (BOSSAPP-960)."""
    if not entries:
        return ""
    from sqlalchemy import text as sql

    lines: list = []
    used_ids: list = []
    spent = 0
    for entry in entries:
        if entry.get("status") != "active":
            continue
        prefix = {"lesson": "RULE", "project": "CONTEXT", "preference": "PREFERENCE"}.get(
            entry["kind"], "CONTEXT")
        line = f"- [{prefix}] {entry['body']}"
        if spent + len(line) > budget_chars:
            break
        lines.append(line)
        used_ids.append(entry["id"])
        spent += len(line) + 1
    if not lines:
        return ""
    connection.execute(sql("UPDATE agent_memory SET injected_count = injected_count + 1, "
                           " last_injected_at = now() WHERE id = ANY(:ids)"), {"ids": used_ids})
    return ("\n\n=== WHAT YOU ALREADY KNOW ABOUT THIS PROJECT ===\n"
            "Recorded in earlier sessions. A RULE is binding — follow it without being asked "
            "again. CONTEXT and PREFERENCE inform how you answer.\n"
            "Do not recite this list back to the operator; act on it.\n" + "\n".join(lines))


def recall(connection: Any, *, project_id: str, actor: Optional[str] = None,
           role: Optional[str] = None, kinds: Optional[tuple] = None,
           include_proposed: bool = False, query: Optional[str] = None,
           now: Optional[datetime] = None) -> list:
    """The `recall` tool's rows: every namespace this caller reads (project, its agent, its
    person), each with its decay weight; a decayed rule is still LISTED (marked `dropped`) so
    "what do you know" stays honest, but `read` no longer injects it."""
    from .agent_memory import recall as _recall

    rows = _recall(connection, project_id=project_id, actor=actor, role=role,
                   kinds=kinds, include_proposed=include_proposed, query=query)
    return [{**row, **weight_of(row, now=now)} for row in rows]


# ── Writing ───────────────────────────────────────────────────────────────────────────────────

def write(connection: Any, namespace: Any, body: str, *, project_id: str, actor: str,
          kind: Optional[str] = None, role: Optional[str] = None,
          thread_id: Optional[str] = None, source_thread_id: Optional[str] = None) -> dict:
    """Record one memory in `namespace`. Trust tiers are the stores' own: a lesson comes back
    `proposed` and binds nothing until reviewed; a thread write is the session ledger's next
    step (its goal, the first time)."""
    ns = parse(namespace, defaults={AGENT: role or "", THREAD: thread_id or "",
                                    USER: actor or ""})
    body = str(body or "").strip()
    if ns.kind == ORG and ns.sub == PRIORS:
        raise MemoryAPIError("priors are built from executed runs (the priors job), never "
                             "written by hand")
    if ns.kind == PROJECT and ns.sub == "kg":
        raise MemoryAPIError("knowledge-graph learnings carry evidence: record them with "
                             "record_learning, which keeps their trust tier")
    if ns.kind == THREAD:
        if not ns.ref:
            raise MemoryAPIError("thread memory needs the thread")
        from .agent_memory import read_ledger, write_ledger

        existing = read_ledger(connection, thread_id=ns.ref)
        row = write_ledger(connection, project_id=project_id, thread_id=ns.ref, actor=actor,
                           goal=None if existing else body, next_step=body if existing else None)
        return {"namespace": str(ns), "status": "active", "ledger": _plain(row)}
    from .agent_memory import MemoryError_, remember

    scope = _SCOPE[ns.kind]
    if scope in ("role", "actor") and not ns.ref:
        raise MemoryAPIError(f"{ns.kind} memory needs whose it is ({ns.kind}:<ref>)")
    try:
        row = remember(connection, project_id=project_id, actor=actor,
                       kind=str(kind or _DEFAULT_KIND[ns.kind]), body=body, scope=scope,
                       scope_ref=ns.ref or None, source_thread_id=source_thread_id)
    except MemoryError_ as error:
        raise MemoryAPIError(str(error)) from error
    return {"namespace": str(ns), **_plain(row)}


def review(connection: Any, *, project_id: str, memory_id: str, actor: str,
           decision: str) -> dict:
    """A person lets a proposed lesson bind (`active`) or refuses it (`rejected`)."""
    from .agent_memory import review as _review

    return _plain(_review(connection, project_id=project_id, memory_id=memory_id, actor=actor,
                          decision=decision))


def _plain(row: Any) -> dict:
    out = dict(row or {})
    for key, value in list(out.items()):
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


# ── Priors, as a callable job ─────────────────────────────────────────────────────────────────

def build_priors_job(engine: Any, *, org_id: str, since: Optional[datetime] = None) -> dict:
    """THE PRIORS BUILD AS A JOB (§4.6 "priors build as a scheduled bot"): fold the executed
    runs' agent_turns into kg_action_priors for `org_id`. Idempotent (a high-water mark per
    org), so a scheduler may call it as often as it likes; P3's bot is its clock. The manual
    script (scripts/build_action_priors.py) is now a thin caller of this."""
    from .knowledge.procedural_priors import build_priors

    started = datetime.now(timezone.utc)
    result = build_priors(engine, org_id=org_id, since=since)
    return {"job": PRIORS_JOB, "org_id": org_id, "started_at": started.isoformat(),
            "result": result}


def read_priors(connection: Any, *, org_id: str, fingerprint_key: str) -> list:
    """("org", oid, "priors"): the ranked actions for one situation fingerprint."""
    from .knowledge.procedural_priors import ranked_actions

    return list(ranked_actions(connection, org_id=org_id, fingerprint_key=fingerprint_key))


# ── A run's /memories ─────────────────────────────────────────────────────────────────────────

class RunMemory:
    """The memory API as a kernel run's `/memories` (kernel/runfs.MemoryPort). Each call opens
    its own transaction through `connect` (an engine's `begin`, or the run's own connection),
    and inside a delegated run asks the run token for memory:read / memory:manage first."""

    def __init__(self, connect: Callable[[], Any], *, project_id: str, actor: str,
                 role: Optional[str] = None, thread_id: Optional[str] = None,
                 reads: Sequence[str] = (), writes: Sequence[str] = (), db: Any = None):
        self.connect = connect
        self.project_id = project_id
        self.actor = actor
        self.role = role
        self.thread_id = thread_id
        self.db = db
        defaults = [PROJECT] + ([f"{AGENT}:{role}"] if role else []) + (
            [f"{THREAD}:{thread_id}"] if thread_id else [])
        self.reads = [str(parse(n, defaults=self._defaults())) for n in (reads or defaults)]
        self.writes = [str(parse(n, defaults=self._defaults())) for n in (
            writes or ([f"{AGENT}:{role}"] if role else []) + [PROJECT])]

    def _defaults(self) -> dict:
        return {AGENT: self.role or "", THREAD: self.thread_id or "", USER: ""}

    def _refused(self, permission: str) -> Optional[str]:
        from ..kernel.governance import identity_refusal

        refusal = identity_refusal("memory", permission, self.project_id, self.db)
        return refusal.get("message") if refusal else None

    def namespaces(self) -> list:
        return list(dict.fromkeys(self.reads + self.writes))

    def read(self, namespace: str, *, query: str = "") -> str:
        from ..kernel.runfs import FSError

        ns = str(parse(namespace, defaults=self._defaults()))
        if ns not in self.reads:
            raise FSError(f"this agent does not read {ns}; it reads {', '.join(self.reads)}")
        refused = self._refused("memory:read")
        if refused:
            raise FSError(refused)
        with self.connect() as connection:
            out = read(connection, [ns], project_id=self.project_id, actor=None, role=self.role,
                       thread_id=self.thread_id, query=query, budget_chars=8_000)
        return out["text"].strip() or f"(nothing recorded in {ns} yet)"

    def write(self, namespace: str, body: str, *, kind: str = "") -> dict:
        from ..kernel.runfs import FSError

        ns = str(parse(namespace, defaults=self._defaults()))
        if ns not in self.writes:
            raise FSError(f"this agent does not write {ns}; it writes {', '.join(self.writes)}")
        refused = self._refused("memory:manage")
        if refused:
            raise FSError(refused)
        try:
            with self.connect() as connection:
                row = write(connection, ns, body, project_id=self.project_id,
                            actor=self.actor, kind=kind or None, role=self.role,
                            thread_id=self.thread_id, source_thread_id=self.thread_id)
        except MemoryAPIError as error:
            raise FSError(str(error)) from error
        if row.get("status") == "proposed":
            row["note"] = ("recorded as PROPOSED: it binds nothing until a person reviews it — "
                           "say it is waiting for review, not that it is in force")
        return row


__all__ = ["AGENT", "DEFAULT_BUDGET_CHARS", "KINDS", "MemoryAPIError", "Namespace", "ORG",
           "PRIORS", "PRIORS_JOB", "PROJECT", "RunMemory", "THREAD", "USER", "build_priors_job",
           "kg_pack", "parse", "read", "read_priors", "recall", "review", "weight_of", "write"]
