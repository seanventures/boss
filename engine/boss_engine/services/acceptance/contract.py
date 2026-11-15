"""THE ACCEPTANCE CONTRACT (BOSSAPP-1214, epic BOSSAPP-1213 AA-A): the list of what was asked,
frozen per build lineage, word for word, versioned when its source changes.

WHY IT EXISTS. Northgate (2026-11-15) was built in Node when its ticket said Go, and shipped about
fifteen must-haves short. The follow-up run's spec was two MINOR findings, so the follow-up "met"
its own spec while the original requirements were lost. The in-build acceptance gate (#136,
acceptance_gate.py) already holds a follow-up to its lineage's builds; this module makes the list
a stored, frozen THING that every check of the lineage — repair, follow-up, re-check, and the
Acceptance Agent's outer step (later tickets) — is judged against, and that a person can read.

WHAT IT HOLDS, in source order (each item keeps where it came from):
  1. the source TICKET's acceptance criteria (the ticket connectors' `Ticket.acceptance_criteria`,
     else the "Acceptance" section of its description), read from the run's ticket snapshot;
  2. the BRIEF's must-haves (render_brief's "Must-haves:" block);
  3. the PRD's tested-and-gating acceptance list;
  4. the run's acceptance criteria (`criteria`: the brief's "done when", or the PRD list
     prd_acceptance_inputs put there);
  5. accepted-delivery templates (acceptance_templates), when a caller opts one in;
  6. items a person adds (`revise`).
Items are deduplicated by their words and numbered AC-1..n. NOTHING IS CUT: the text is kept
verbatim (the gate's per-pass caps are the gate's, applied when it reads the contract).

FROZEN. `freeze` writes version 1 the first time a lineage is seen and never rewrites it. A
change is a NEW VERSION (`revise`): items keep their ids, new items get new ids, a removed id is
never reused, and the version says why ("PROJ-142 changed"). `revise_from_ticket` makes that
version when the run's ticket snapshot carries different acceptance criteria (a person chose
"Include it" on the mid-run change card, ticket_sync.on_card_answered).

THE ITEM EXTRACTION moved here from acceptance_gate.py unchanged (brief_must_haves,
prd_gating_criteria, _sections, _items_of, acceptance_items, items_dropped, lineage_build_rows,
union_spec); acceptance_gate re-exports them, so the gate behaves exactly as before.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Iterable, Mapping, Optional

_log = logging.getLogger(__name__)

#: The most items one check holds a build to; beyond it the check is `not_run`, never `met`.
MAX_TOTAL_ITEMS = 400
#: The gate's per-item cap when it READS the contract (the contract itself keeps every word).
MAX_ITEM_CHARS = 1000
#: A contract larger than this is still whole; the card says "large contract" (LLD §3).
LARGE_CONTRACT = 60

# ---------------------------------------------------------------------------------------------
# What was asked (moved from acceptance_gate.py, unchanged)
# ---------------------------------------------------------------------------------------------

# ---------------------------------------------------------------------------------------------

def _norm(s: Any) -> str:
    return " ".join(str(s or "").split())


def brief_must_haves(text: str) -> list[str]:
    """The "Must-haves:" block of a rendered brief (project_experience.add_project.render_brief):
    its bullet lines, until the block ends at a blank line or a line that is not a bullet."""
    lines = str(text or "").splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if re.match(r"^\s*\**must[- ]haves?\**\s*:?\s*\**\s*$", line, re.I):
            for nxt in lines[i + 1:]:
                m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)\s*$", nxt)
                if not m:
                    break
                out.append(_norm(m.group(1)))
            break
    return out


_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD_HEADING = re.compile(r"^\s*\*\*(.+?)\*\*\s*:?\s*$")
_ITEM_LINE = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+(.*\S)\s*$")
#: A sub-list that is explicitly NOT gating (stretch goals, built-but-unverified, nice-to-have).
_NOT_GATING = re.compile(r"not[\s-]+gating|non[\s-]?gating|unverified|stretch|nice[\s-]to[\s-]have"
                         r"|optional|if time allows|best[\s-]effort|out of scope", re.I)


def _sections(markdown: str) -> list[tuple[int, str, list[str]]]:
    """(level, heading, body lines) for each markdown heading; level 7 for a bold-line heading."""
    out: list[tuple[int, str, list[str]]] = []
    cur: Optional[tuple[int, str, list[str]]] = None
    for line in str(markdown or "").splitlines():
        h = _HEADING.match(line)
        b = None if h else _BOLD_HEADING.match(line)
        if h or b:
            cur = (len(h.group(1)) if h else 7, (h.group(2) if h else b.group(1)).strip(), [])
            out.append(cur)
        elif cur is not None:
            cur[2].append(line)
    return out


def prd_gating_criteria(markdown: str) -> list[str]:
    """The PRD's acceptance list: under its "Acceptance" heading, the items of a "tested and
    gating" sub-list when it has one, else every item not under a sub-list that says it is not
    gating (stretch, built-but-unverified, optional). Empty when the document has no acceptance
    section. Parsing a document's structure, not judging it."""
    secs = _sections(markdown)
    first_plain: list[str] = []
    for i, (level, heading, body) in enumerate(secs):
        if not re.search(r"acceptance", heading, re.I):
            continue
        # The acceptance section and its sub-sections (deeper headings, or bold-line headings).
        block = [(heading, body)]
        for lvl2, h2, b2 in secs[i + 1:]:
            if lvl2 <= level:
                break
            block.append((h2, b2))
        gating = [(h, b) for h, b in block[1:]
                  if re.search(r"gating|must[\s-]pass|required", h, re.I)
                  and not _NOT_GATING.search(h)]
        chosen = gating or [(h, b) for h, b in block if h == heading or not _NOT_GATING.search(h)]
        items = _items_of(chosen)
        if gating and items:
            # A labelled gating list is the PRD's own answer: it wins over a plain list (the
            # person's words quoted above the PRD often carry an earlier acceptance list).
            return items
        if items and not first_plain:
            first_plain = items
    return first_plain


def _items_of(sections: list[tuple[str, list[str]]]) -> list[str]:
    items: list[str] = []
    for _h, body in sections:
        for line in body:
            m = _ITEM_LINE.match(line)
            if m and not line.startswith(("    ", "\t")):
                items.append(_norm(m.group(1)))
    return items


def _spec_of(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def acceptance_items(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the build is held to, numbered: the brief's must-haves, the PRD's tested-and-gating
    list, then the run's acceptance criteria — deduplicated, each whole up to MAX_ITEM_CHARS, at
    most MAX_TOTAL_ITEMS (`items_dropped` says how many more there were; judged in passes of
    MAX_ITEMS by `run_check`). The existing-tests promise
    is left to the regression gate, which measures it."""
    return _items_and_dropped(spec)[0]


def items_dropped(spec: Mapping[str, Any]) -> int:
    return _items_and_dropped(spec)[1]


def item_key(text: str) -> str:
    """A short stable key for an item's text: what records which items a check judged."""
    import hashlib

    norm = re.sub(r"\W+", " ", str(text or "").lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:12]


def _raw_items(spec: Mapping[str, Any]) -> list[tuple[str, str]]:
    from ..code_task_service import EXISTING_TESTS_CRITERION

    spec = _spec_of(spec)
    if isinstance(spec.get(UNION_KEY), list):
        return [(str(i.get("source") or "criterion"), _norm(i.get("text")))
                for i in spec[UNION_KEY] if isinstance(i, Mapping)]
    instructions = str(spec.get("instructions") or "")
    raw: list[tuple[str, str]] = []
    raw += [("must_have", m) for m in brief_must_haves(instructions)]
    raw += [("prd_gating", m) for m in prd_gating_criteria(instructions)]
    raw += [("criterion", _norm(c)) for c in (spec.get("acceptance_criteria") or [])
            if _norm(c) and _norm(c) != _norm(EXISTING_TESTS_CRITERION)]
    return raw


def _items_and_dropped(spec: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    raw = _raw_items(spec)
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    dropped = 0
    for source, item in raw:
        key = re.sub(r"\W+", " ", item.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        if len(items) >= MAX_TOTAL_ITEMS:
            dropped += 1
            continue
        items.append({"n": len(items) + 1, "text": item[:MAX_ITEM_CHARS], "source": source})
    return items, dropped


#: A spec key holding a lineage's union of items (items_spec): every slice's, deduplicated.
UNION_KEY = "_acceptance_union_items"


def lineage_build_rows(connection: Any, root: str) -> list[dict]:
    """Every BUILD of the lineage — the runs that are not follow-ups, oldest first: the
    original build, its repair children (same spec) and, for a decomposed PRD, every slice
    (prd_decomposition starts slices 2..N as non-follow-ups sharing the root id, each with its
    own items). Their items together are what the pull request must deliver."""
    from sqlalchemy import text

    rows = connection.execute(text(
        "SELECT id, spec FROM code_task_runs WHERE (id = :r OR finalize_ctx->>'root_run_id' = :r) "
        "AND COALESCE(finalize_ctx->>'is_followup', 'false') <> 'true' "
        "AND finalize_ctx->>'deferred_from_run' IS NULL "
        "ORDER BY created_at, id"), {"r": root}).all()
    return [{"id": r[0], "spec": _spec_of(r[1])} for r in rows]


def union_spec(builds: list[dict]) -> dict:
    """One spec holding the union of every build's items (the first build's instructions are
    kept for its declared stack)."""
    raw: list[dict] = []
    for b in builds:
        raw += [{"source": src, "text": t} for src, t in _raw_items(b["spec"])]
    first = builds[0]["spec"] if builds else {}
    return {"instructions": first.get("instructions") or "",
            **({"required_stack": first["required_stack"]} if first.get("required_stack")
               else {}),
            UNION_KEY: raw}


# ---------------------------------------------------------------------------------------------
# The contract: sources → items → a frozen, versioned row
# ---------------------------------------------------------------------------------------------

#: Where an item came from, in priority order (LLD §3: ticket, brief, PRD, template, operator).
SOURCE_ORDER = ("ticket", "must_have", "prd_gating", "criterion", "template", "operator")
#: The contract's source KIND for each item source (acceptance_contracts.sources[].kind).
SOURCE_KIND = {"ticket": "ticket", "must_have": "brief", "prd_gating": "prd",
               "criterion": "criteria", "template": "template", "operator": "operator"}


def _key(text: Any) -> str:
    """The dedupe key: the item's words, case and punctuation folded (as the gate dedupes)."""
    return re.sub(r"\W+", " ", str(text or "").lower()).strip()


def _verbatim(text: Any) -> str:
    """The item's words as written, whitespace folded — never cut."""
    return _norm(text)


def ticket_items(ticket: Optional[Mapping[str, Any]]) -> list[str]:
    """A ticket's acceptance criteria: the connector's `acceptance_criteria` field, else the items
    under an "Acceptance" heading of its description (prd_gating_criteria — the same parser the
    PRD's list goes through). Words only, never truncated."""
    if not isinstance(ticket, Mapping):
        return []
    given = [_verbatim(c) for c in ticket.get("acceptance_criteria") or [] if _verbatim(c)]
    if given:
        return given
    body = ticket.get("body") if ticket.get("body") is not None else ticket.get("body_markdown")
    return [_verbatim(c) for c in prd_gating_criteria(str(body or "")) if _verbatim(c)]


def _ticket_ref(ticket: Mapping[str, Any]) -> str:
    return str(ticket.get("key") or ticket.get("id") or ticket.get("url") or "ticket")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def gather_sources(spec: Mapping[str, Any], *, ticket: Optional[Mapping[str, Any]] = None,
                   templates: Iterable[Mapping[str, Any]] = (),
                   operator_items: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The contract's sources: [{kind, ref, url, items: [{source, text}], digest}], ticket first.

    `spec` is the lineage's build spec (a union spec for a decomposed PRD: union_spec); its items
    are exactly the ones the gate reads today (_raw_items). `ticket` is the run's ticket snapshot
    (ticket_sync.snapshot's shape), `templates` accepted-delivery templates a caller opted in
    ({id, title, criteria}), `operator_items` words a person added."""
    out: list[dict[str, Any]] = []
    if ticket:
        words = ticket_items(ticket)
        if words:
            out.append({"kind": "ticket", "ref": _ticket_ref(ticket), "url": ticket.get("url"),
                        "provider": ticket.get("provider"),
                        "updated_at": ticket.get("updated_at"),
                        "items": [{"source": "ticket", "text": w} for w in words]})
    # The build's own items, in the order the gate reads them (_raw_items: the brief's
    # must-haves, the PRD's gating list, the run's acceptance criteria), one source per kind.
    by_kind: dict[str, list[dict[str, str]]] = {}
    for source, words in _raw_items(spec):
        if _verbatim(words):
            by_kind.setdefault(SOURCE_KIND.get(source, "criteria"), []).append(
                {"source": source, "text": _verbatim(words)})
    for kind, listed in by_kind.items():
        out.append({"kind": kind, "ref": kind, "url": None, "items": listed})
    for t in templates or ():
        words = [_verbatim(c) for c in (t.get("criteria") or []) if _verbatim(c)]
        if words:
            out.append({"kind": "template", "ref": str(t.get("id") or t.get("title") or "template"),
                        "url": None, "items": [{"source": "template", "text": w} for w in words]})
    words = [_verbatim(w) for w in operator_items or () if _verbatim(w)]
    if words:
        out.append({"kind": "operator", "ref": "operator", "url": None,
                    "items": [{"source": "operator", "text": w} for w in words]})
    for s in out:
        s["digest"] = _digest([i["text"] for i in s["items"]])
    return out


def build_items(sources: Iterable[Mapping[str, Any]], *, start: int = 1) -> list[dict[str, Any]]:
    """Numbered items AC-<start>.. from the sources, in order, deduplicated by their words. No
    cap on count or length (the gate applies its own when it reads them)."""
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for s in sources:
        for it in s.get("items") or []:
            key = _key(it.get("text"))
            if not key or key in seen:
                continue
            seen.add(key)
            n = start + len(items)
            items.append({"id": f"AC-{n}", "n": n, "text": _verbatim(it["text"]),
                          "source": str(it.get("source") or s.get("kind") or "criterion"),
                          "source_kind": str(s.get("kind") or ""),
                          "source_ref": str(s.get("ref") or ""),
                          "priority": "must", "check_hint": None})
    return items


def check_hint(item: Mapping[str, Any], *, runnable: bool) -> str:
    """How an item is checked when no plan has said: 'run' when the work is runnable, else
    'inspect'. NOT a keyword list over the item's words — the Acceptance Agent's plan decides
    run / inspect / human per item (LLD §4.3) and its answer is stored on the item."""
    hint = str(item.get("check_hint") or "").strip().lower()
    if hint in ("run", "inspect", "human"):
        return hint
    return "run" if runnable else "inspect"


def _row(r: Mapping[str, Any]) -> dict[str, Any]:
    def j(v: Any, default: Any) -> Any:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except ValueError:
                return default
        return default if v is None else v

    items = j(r.get("items"), [])
    return {"id": r["id"], "project_id": r["project_id"],
            "lineage_root_run_id": r["lineage_root_run_id"],
            "workflow_run_id": r.get("workflow_run_id"), "version": int(r["version"]),
            "supersedes_id": r.get("supersedes_id"), "sources": j(r.get("sources"), []),
            "items": items, "items_dropped": int(r.get("items_dropped") or 0),
            "reason": r.get("reason") or "", "digest": r.get("digest") or "",
            "frozen_at": r.get("frozen_at"), "created_by": r.get("created_by") or "boss",
            "large": len(items) > LARGE_CONTRACT}


_COLUMNS = ("id, project_id, lineage_root_run_id, workflow_run_id, version, supersedes_id, "
            "sources, items, items_dropped, reason, digest, frozen_at, created_by")


def latest(connection: Any, *, root_run_id: str,
           project_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The lineage's newest contract version, or None."""
    from sqlalchemy import text

    row = connection.execute(text(
        f"SELECT {_COLUMNS} FROM acceptance_contracts WHERE lineage_root_run_id = :r "
        "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p) ORDER BY version DESC LIMIT 1"),
        {"r": root_run_id, "p": project_id}).mappings().first()
    return _row(row) if row else None


def get(connection: Any, contract_id: str) -> Optional[dict[str, Any]]:
    from sqlalchemy import text

    row = connection.execute(text(f"SELECT {_COLUMNS} FROM acceptance_contracts WHERE id = :i"),
                             {"i": contract_id}).mappings().first()
    return _row(row) if row else None


def versions(connection: Any, *, root_run_id: str) -> list[dict[str, Any]]:
    """Every version of a lineage's contract, oldest first."""
    from sqlalchemy import text

    rows = connection.execute(text(
        f"SELECT {_COLUMNS} FROM acceptance_contracts WHERE lineage_root_run_id = :r "
        "ORDER BY version"), {"r": root_run_id}).mappings().all()
    return [_row(r) for r in rows]


def _insert(connection: Any, *, project_id: str, root_run_id: str, version: int,
            sources: list, items: list, workflow_run_id: Optional[str],
            supersedes_id: Optional[str], reason: str, actor: str) -> Optional[str]:
    from sqlalchemy import text

    from ..core import make_id

    cid = make_id("acn")
    got = connection.execute(text(
        "INSERT INTO acceptance_contracts (id, project_id, lineage_root_run_id, workflow_run_id, "
        "version, supersedes_id, sources, items, reason, digest, created_by) VALUES "
        "(:i, :p, :r, :w, :v, :s, CAST(:src AS JSONB), CAST(:it AS JSONB), :why, :d, :a) "
        "ON CONFLICT (project_id, lineage_root_run_id, version) DO NOTHING RETURNING id"),
        {"i": cid, "p": project_id, "r": root_run_id, "w": workflow_run_id, "v": version,
         "s": supersedes_id, "src": json.dumps(sources, default=str),
         "it": json.dumps(items), "why": reason,
         "d": _digest([i["text"] for i in items]), "a": actor}).scalar()
    return got


def freeze(connection: Any, *, project_id: str, root_run_id: str,
           sources: list[dict[str, Any]], workflow_run_id: Optional[str] = None,
           actor: str = "boss") -> dict[str, Any]:
    """Version 1 of the lineage's contract, written the FIRST time the lineage is seen. A
    lineage that already has a contract keeps it, whatever `sources` say now: a later build,
    repair or follow-up never re-freezes its own spec (I1). Idempotent and race-safe."""
    existing = latest(connection, root_run_id=root_run_id, project_id=project_id)
    if existing is not None:
        return existing
    items = build_items(sources)
    made = _insert(connection, project_id=project_id, root_run_id=root_run_id, version=1,
                   sources=_source_rows(sources), items=items, workflow_run_id=workflow_run_id,
                   supersedes_id=None, reason="frozen at the lineage's first build",
                   actor=actor)
    found = latest(connection, root_run_id=root_run_id, project_id=project_id)
    if made and found is not None:
        from ..core import emit_event

        emit_event(connection, project_id, "acceptance.contract_frozen", actor,
                   "acceptance_contract", found["id"],
                   {"lineage_root_run_id": root_run_id, "version": 1, "items": len(items),
                    "sources": [s["kind"] for s in sources]})
    assert found is not None
    return found


def _source_rows(sources: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What the row keeps per source: where it came from and its digest (the items are on the
    contract), plus the source's own item texts for a ticket (so a later change is a diff)."""
    out = []
    for s in sources:
        row = {k: s.get(k) for k in ("kind", "ref", "url", "provider", "updated_at", "digest")
               if s.get(k) is not None}
        row["count"] = len(s.get("items") or [])
        out.append(row)
    return out


def _next_number(connection: Any, root_run_id: str) -> int:
    """One past the highest item number any version of the lineage ever used: a removed id is
    never given to a different item."""
    best = 0
    for v in versions(connection, root_run_id=root_run_id):
        for it in v["items"]:
            best = max(best, int(it.get("n") or 0))
    return best + 1


def revise(connection: Any, contract_id: str, *, added: Iterable[Any] = (),
           removed: Iterable[str] = (), reason: str, actor: str,
           source_update: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """A NEW VERSION of the contract (supersedes_id = the one revised): `removed` item ids (or
    their words) leave, `added` items ({text, source, source_ref} or words) join with new ids.
    Every kept item keeps its id. Nothing to change returns the contract as it is."""
    cur = get(connection, contract_id)
    if cur is None:
        raise ValueError(f"no acceptance contract {contract_id}")
    newest = latest(connection, root_run_id=cur["lineage_root_run_id"],
                    project_id=cur["project_id"])
    if newest is not None and newest["id"] != cur["id"]:
        cur = newest                    # always revise the newest: versions form one line
    gone = {str(r) for r in removed or ()}
    gone_keys = {_key(r) for r in gone}
    kept = [i for i in cur["items"] if i["id"] not in gone and _key(i["text"]) not in gone_keys]
    have = {_key(i["text"]) for i in kept}
    n = _next_number(connection, cur["lineage_root_run_id"])
    joined: list[dict[str, Any]] = []
    for a in added or ():
        a = {"text": a} if isinstance(a, str) else dict(a)
        words = _verbatim(a.get("text"))
        if not _key(words) or _key(words) in have:
            continue
        have.add(_key(words))
        source = str(a.get("source") or "operator")
        joined.append({"id": f"AC-{n}", "n": n, "text": words, "source": source,
                       "source_kind": SOURCE_KIND.get(source, source),
                       "source_ref": str(a.get("source_ref") or ""), "priority": "must",
                       "check_hint": None})
        n += 1
    if not joined and len(kept) == len(cur["items"]):
        return cur
    sources = list(cur["sources"])
    if source_update:
        sources = [s for s in sources if s.get("kind") != source_update.get("kind")]
        sources.insert(0 if source_update.get("kind") == "ticket" else len(sources),
                       dict(source_update))
    version = cur["version"] + 1
    _insert(connection, project_id=cur["project_id"], root_run_id=cur["lineage_root_run_id"],
            version=version, sources=sources, items=kept + joined,
            workflow_run_id=cur.get("workflow_run_id"), supersedes_id=cur["id"],
            reason=str(reason or "").strip() or "revised", actor=actor)
    found = latest(connection, root_run_id=cur["lineage_root_run_id"],
                   project_id=cur["project_id"])
    assert found is not None
    from ..core import emit_event

    emit_event(connection, cur["project_id"], "acceptance.contract_revised", actor,
               "acceptance_contract", found["id"],
               {"lineage_root_run_id": cur["lineage_root_run_id"], "version": found["version"],
                "supersedes_id": cur["id"], "reason": found["reason"],
                "added": [i["id"] for i in joined],
                "removed": [i["id"] for i in cur["items"] if i not in kept]})
    return found


def revise_from_ticket(connection: Any, contract: Mapping[str, Any],
                       ticket: Optional[Mapping[str, Any]], *,
                       actor: str = "boss") -> dict[str, Any]:
    """The contract with the ticket's CURRENT acceptance criteria: a new version when they differ
    from the ticket items it holds (added ones join with new ids, dropped ones leave), else the
    contract unchanged. The run's ticket snapshot is what changes when a person includes a
    mid-run change, so reading it here is how that decision reaches the contract."""
    now = ticket_items(ticket)
    if not ticket or not now:
        return dict(contract)
    held = [i for i in contract["items"] if i.get("source") == "ticket"]
    held_keys = {_key(i["text"]) for i in held}
    now_keys = {_key(w) for w in now}
    if held_keys == now_keys:
        return dict(contract)
    ref = _ticket_ref(ticket)
    added = [{"text": w, "source": "ticket", "source_ref": ref} for w in now
             if _key(w) not in held_keys]
    removed = [i["id"] for i in held if _key(i["text"]) not in now_keys]
    n_add, n_rm = len(added), len(removed)
    words = ", ".join(x for x in (f"{n_add} item{'s' * (n_add != 1)} added" if n_add else "",
                                  f"{n_rm} removed" if n_rm else "") if x)
    source = {"kind": "ticket", "ref": ref, "url": ticket.get("url"),
              "provider": ticket.get("provider"), "updated_at": ticket.get("updated_at"),
              "digest": _digest(now), "count": len(now)}
    return revise(connection, contract["id"], added=added, removed=removed,
                  reason=f"{ref} changed: {words}", actor=actor, source_update=source)


# ---------------------------------------------------------------------------------------------
# The lineage: which contract a run is held to
# ---------------------------------------------------------------------------------------------

def is_followup(fctx: Optional[Mapping[str, Any]]) -> bool:
    fctx = fctx or {}
    return bool(fctx.get("is_followup") or fctx.get("deferred_from_run"))


def lineage_root(run_id: str, fctx: Optional[Mapping[str, Any]]) -> str:
    """The id a lineage's contract is keyed by: the ORIGINAL build's lineage root, the same one
    items_spec resolves (a follow-up's root_run_id / deferred_from_run; a repair child's or a
    decomposed slice's root_run_id; else the run itself)."""
    fctx = fctx or {}
    return str(fctx.get("root_run_id") or fctx.get("deferred_from_run") or run_id or "").strip()


def ticket_of_run(connection: Any, run_id: str) -> Optional[dict[str, Any]]:
    """The ticket snapshot a build works on (its own finalize_ctx.ticket, its workflow run's
    input.ticket, or the bound ticket), as ticket_sync resolves it for write-back. None for a
    build not from a ticket. Reads only what BOSS stored: the gate never calls out to a tracker."""
    from ..ticket_sync import resolve

    try:
        subject = resolve(connection, "code_task_run", run_id)
    except Exception:  # noqa: BLE001 — no ticket is "no ticket items", never a failed check
        _log.warning("acceptance contract: the run's ticket could not be read")
        return None
    return dict(subject.ticket) if subject is not None and subject.ticket else None


def _project_of(connection: Any, run_id: str) -> Optional[str]:
    from sqlalchemy import text

    return connection.execute(text("SELECT project_id FROM code_task_runs WHERE id = :i"),
                              {"i": run_id}).scalar()


def ensure_for_run(connection: Any, *, run_id: str, fctx: Optional[Mapping[str, Any]],
                   own_spec: Mapping[str, Any],
                   ticket: Any = "_read") -> Optional[dict[str, Any]]:
    """The contract this run is held to, frozen now if its lineage has none yet.

    Frozen from the lineage's builds (lineage_build_rows: the original build, its repairs and
    every decomposed slice) and the ticket; for a lineage whose builds cannot be found, from
    the run's own spec unless it is a follow-up (a follow-up's own spec is the findings it was
    sent to fix: never a contract). Then refreshed from the ticket snapshot (revise_from_ticket).
    None when there is nothing to hold the run to or the run is unknown."""
    root = lineage_root(run_id, fctx)
    if not root:
        return None
    project_id = _project_of(connection, run_id)
    if not project_id:
        return None
    if isinstance(ticket, str) and ticket == "_read":
        # The run's own ticket, else its lineage root's: a follow-up started outside the
        # workflow carries none of its own, and the root's snapshot is what a person updated.
        ticket = ticket_of_run(connection, run_id) or (
            ticket_of_run(connection, root) if root != run_id else None)
    found = latest(connection, root_run_id=root, project_id=project_id)
    if found is None:
        builds = lineage_build_rows(connection, root)
        if builds:
            spec: Mapping[str, Any] = union_spec(builds)
        elif not is_followup(fctx):
            spec = _spec_of(own_spec)
        else:
            return None
        sources = gather_sources(spec, ticket=ticket)
        if not build_items(sources):
            return None
        found = freeze(connection, project_id=project_id, root_run_id=root, sources=sources,
                       workflow_run_id=(fctx or {}).get("workflow_run_id"))
    return revise_from_ticket(connection, found, ticket)


def gate_items(contract: Mapping[str, Any]) -> list[dict[str, str]]:
    """The contract's items in the gate's union shape ({source, text}): what items_spec hands
    the in-build check, so the inner seat is held to the same list (LLD §3)."""
    return [{"source": str(i.get("source") or "criterion"), "text": str(i["text"])}
            for i in contract.get("items") or []]
