"""THE ACCEPTANCE CONTRACT (BOSSAPP-1214, epic BOSSAPP-1213 AA-A): the list of what was asked,
frozen per build lineage, word for word, versioned when its source changes.

WHY IT EXISTS. Northgate (2026-11-15) was built in Node when its ticket said Go, and shipped about
fifteen must-haves short. The follow-up run's spec was two MINOR findings, so the follow-up "met"
its own spec while the original requirements were lost. The in-build acceptance gate (#136,
acceptance_gate.py) already holds a follow-up to its lineage's builds; this module makes the list
a stored, frozen THING that every check of the lineage — repair, follow-up, re-check, and the
Acceptance Agent's outer step (later tickets) — is judged against, and that a person can read.

WHAT IT HOLDS, in source order (each item keeps where it came from):
  1. EVERY requirement the source TICKET states (BOSSAPP-1233): the ticket connectors'
     `Ticket.acceptance_criteria` and the "Acceptance" section of its description first, then
     the rest of its description read by structure (requirement_items: the Stack: line, each
     "What to build" bullet and sub-bullet, the quality bar), read from the run's ticket snapshot;
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
import os
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
    union = _spec_of(spec).get(UNION_KEY)
    tags = {re.sub(r"\W+", " ", _norm(i.get("text")).lower()).strip():
            {t: True for t in ("judgement", "needs_loader") if i.get(t)}
            for i in (union if isinstance(union, list) else []) if isinstance(i, Mapping)}
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
        items.append({"n": len(items) + 1, "text": item[:MAX_ITEM_CHARS], "source": source,
                      **tags.get(key, {})})
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


def ticket_items(ticket: Optional[Mapping[str, Any]], *,
                 prose: Optional[Iterable[str]] = None) -> list[str]:
    """EVERY requirement a ticket states (BOSSAPP-1233), word for word, never truncated: the
    connector's `acceptance_criteria` field and the items under an "Acceptance" heading of its
    description first (prd_gating_criteria — the same parser the PRD's list goes through), then
    every other requirement its description states (requirement_items: the `Stack:` line, each
    "What to build" bullet and sub-bullet, the quality bar, a deliverable written as prose).

    WHY. Northgate KAN-9 (2026-11-15) was judged on its five "Acceptance" lines only; its "What
    to build" must-haves — a >=20-rule YAML rulebook gwlint loads, JWKS-cached JWT, a JWT
    audience rule — were never items, so a build with five hard-coded lint checks and an HS256
    shared secret scored 7 of 8 met.

    `prose`: what the prose reader (ticket_prose: a model that reasons over the description's
    prose and cites each requirement word for word) found. For a BUG ticket it replaces the
    structural reading of the description — the steps to reproduce are context, the expected
    behaviour is the requirement — whenever it found anything."""
    if not isinstance(ticket, Mapping):
        return []
    body = _body_of(ticket)
    prose = [p for p in (prose or []) if _verbatim(p)]
    structural = ([] if (prose and _is_bug(ticket))
                  else [*prd_gating_criteria(body), *requirement_items(body)])
    out: list[str] = []
    seen: set[str] = set()
    for words in [*(ticket.get("acceptance_criteria") or []), *structural, *prose]:
        w = _verbatim(words)
        if w and _key(w) not in seen:
            seen.add(_key(w))
            out.append(w)
    return _drop_contained(out)


def _drop_contained(items: list[str]) -> list[str]:
    """Without an item whose words are wholly inside another item (the prose reader citing a
    clause of a sentence the structure already holds): the longer item holds it."""
    keys = [f" {_key(w)} " for w in items]
    return [w for i, w in enumerate(items)
            if not any(j != i and len(keys[j]) > len(keys[i]) and keys[i] in keys[j]
                       for j in range(len(items)))]


# ---------------------------------------------------------------------------------------------
# Every requirement a ticket/brief states, read from its STRUCTURE (BOSSAPP-1233)
# ---------------------------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*(```|~~~)")
_BULLET = re.compile(r"^(\s*)(?:[-*•+]|\d+[.)])\s+(.*\S)\s*$")
#: The `Stack:` field (stacks._STACK_LINE's form, the line the stack gate already declares from).
_STACK_FIELD = re.compile(r"^\s*[`*]*\s*stack\s*[`*]*\s*:\s*\S", re.I)
#: Sub-bullets this short with no children of their own are one requirement's parts (a list of
#: fields, of tenants): they stay with their parent as ONE item rather than one item each.
SHORT_PART = 60


def _indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _parse(markdown: str) -> list[dict[str, Any]]:
    """The document as blocks: heading {level, text}, bullet {indent, text}, para {text} and
    stack {text}. Code fences, tables and block quotes (someone else's words, an example) are
    not requirements and are left out; a bullet's wrapped continuation lines join it."""
    blocks: list[dict[str, Any]] = []
    fence = False
    para: list[str] = []
    last_bullet: Optional[dict[str, Any]] = None

    def flush() -> None:
        if para:
            blocks.append({"kind": "para", "text": _norm(" ".join(para))})
            para.clear()

    for line in str(markdown or "").splitlines():
        if _FENCE.match(line):
            flush()
            fence, last_bullet = not fence, None
            continue
        if fence:
            continue
        stripped = line.strip()
        if not stripped:
            # A blank line ends the bullet: the next paragraph is its own block, never glued
            # onto the last bullet (a bug ticket's "Environment:" after its repro steps).
            flush()
            last_bullet = None
            continue
        h = _HEADING.match(line)
        if h:
            flush()
            last_bullet = None
            blocks.append({"kind": "heading", "level": len(h.group(1)), "text": h.group(2)})
            continue
        if stripped.startswith((">", "|")):
            flush()
            last_bullet = None
            continue
        b = _BULLET.match(line)
        if b:
            flush()
            last_bullet = {"kind": "bullet", "indent": _indent(line), "text": _norm(b.group(2))}
            blocks.append(last_bullet)
            continue
        if last_bullet is not None and not para and _indent(line) > last_bullet["indent"]:
            last_bullet["text"] = _norm(last_bullet["text"] + " " + stripped)
            continue
        last_bullet = None
        if _STACK_FIELD.match(stripped):
            flush()
            blocks.append({"kind": "stack", "text": _norm(stripped)})
            continue
        para.append(stripped)
    flush()
    return blocks


def _bullet_tree(bullets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nest a section's bullets by indentation: [{text, children}]."""
    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []
    for b in bullets:
        node: dict[str, Any] = {"text": b["text"], "children": []}
        while stack and stack[-1][0] >= b["indent"]:
            stack.pop()
        (stack[-1][1]["children"] if stack else roots).append(node)
        stack.append((b["indent"], node))
    return roots


def _flat(node: Mapping[str, Any]) -> str:
    return _norm(" ".join([node["text"], *(_flat(c) for c in node["children"])]))


def _bullet_items(node: Mapping[str, Any]) -> list[str]:
    """One item per concrete requirement. A bullet with no sub-bullets is one item; a bullet
    whose sub-bullets are only short parts of it (fields, names) is ONE item holding them; a
    bullet whose sub-bullets are requirements of their own gives one item per sub-bullet, each
    led by the parent's words so it reads whole ("A rulebook ... At least 20 rules: Security:
    ..."). Every word is the document's own."""
    kids = node["children"]
    if not kids:
        return [node["text"]]
    if all(not c["children"] and len(c["text"]) <= SHORT_PART for c in kids):
        return [_flat(node)]
    out: list[str] = []
    for c in kids:
        out += [_norm(f"{node['text']} {t}") for t in _bullet_items(c)]
    return out


def requirement_items(markdown: str) -> list[str]:
    """Every concrete, checkable requirement a ticket or brief STATES in its structure, word for
    word, in document order — read from its structure, not from keywords:

      - a `Stack:` field line (the stack the work must be in);
      - every bullet (and numbered item), under a heading or before the first one, with its
        sub-bullets (_bullet_items: one item per requirement, short parts kept together);
      - the prose of a SUB-section (a heading under the document's top-level sections), with
        or without a list beside it: "### 1. `northgate-gateway`" + "A reverse-proxy API
        gateway, one static binary, configured by YAML.", "### 3. Demo content" + its paragraph.

    Prose before the first heading or directly under a top-level section is NOT read here: it
    may be context or a requirement, which is a judgement — `prose_to_read` hands it to the
    prose reader (ticket_prose), which reasons over it.

    Left out: a section whose heading says it is not gating (stretch, optional, out of scope,
    nice to have — _NOT_GATING, the PRD parser's own test) and everything under it; code
    blocks, tables and block quotes."""
    return _reading(markdown)[0]


def prose_to_read(markdown: str) -> str:
    """The prose the structural reader left for the prose reader: paragraphs before the first
    heading and directly under top-level sections (not under a not-gating heading), each under
    its heading. Empty when there is none."""
    return _reading(markdown)[1]


def _reading(markdown: str) -> tuple[list[str], str]:
    blocks = _parse(markdown)
    levels = [b["level"] for b in blocks if b["kind"] == "heading"]
    base = min(levels) if levels else 1
    if levels.count(base) == 1 and levels[0] == base and len(set(levels)) > 1:
        base = min(lv for lv in levels if lv > base)        # a lone first heading is the title
    out: list[str] = []
    left: list[str] = []
    path: list[tuple[int, str]] = []
    section: list[dict[str, Any]] = []

    def close() -> None:
        bullets = [b for b in section if b["kind"] == "bullet"]
        paras = [b["text"] for b in section if b["kind"] == "para"]
        if not any(_NOT_GATING.search(t) for _lv, t in path):
            for node in _bullet_tree(bullets):
                out.extend(_bullet_items(node))
            if path and path[-1][0] > base:
                out.extend(paras)
            elif paras:
                left.append("\n".join(([f"[{path[-1][1]}]"] if path else []) + paras))
        section.clear()

    for b in blocks:
        if b["kind"] == "heading":
            close()
            while path and path[-1][0] >= b["level"]:
                path.pop()
            path.append((b["level"], b["text"]))
        elif b["kind"] == "stack":
            out.append(b["text"])
        else:
            section.append(b)
    close()
    seen: set[str] = set()
    kept: list[str] = []
    for w in (_verbatim(x) for x in out):
        if w and _key(w) not in seen:
            seen.add(_key(w))
            kept.append(w)
    return kept, "\n\n".join(left)


# ---------------------------------------------------------------------------------------------
# The prose reader: a model reasons over what structure cannot decide (BOSSAPP-1233)
# ---------------------------------------------------------------------------------------------

#: Fewer structural items than this and the prose reader reads the WHOLE description.
MIN_STRUCTURAL_ITEMS = 3
#: The prose reader's bounds: what it is shown, and how many items it may return.
MAX_PROSE_CHARS = 12000
MAX_PROSE_ITEMS = 30
_PROSE_OPEN, _PROSE_CLOSE = "=== REQUIREMENTS ===", "=== END REQUIREMENTS ==="


def _body_of(ticket: Mapping[str, Any]) -> str:
    body = ticket.get("body") if ticket.get("body") is not None else ticket.get("body_markdown")
    return str(body or "")


#: A bug report's own parts, as a heading or a labelled line ("Steps to reproduce:", "## Expected").
_BUG_PART = re.compile(r"(?im)^\s{0,3}(?:#{1,6}\s*|\*\*)?\s*(steps\s+to\s+reproduce|repro(?:duction)?"
                       r"(?:\s+steps)?|expected(?:\s+(?:result|behaviou?r))?|actual(?:\s+(?:result|"
                       r"behaviou?r))?)\s*\**\s*(?::|$)")


def _is_bug(ticket: Mapping[str, Any]) -> bool:
    """A bug report: its type says so, or its description is laid out as one (at least two of
    steps-to-reproduce / expected / actual, as headings or labelled lines)."""
    if "bug" in str(ticket.get("type") or ticket.get("issue_type") or "").lower():
        return True
    parts = {re.sub(r"\W.*", "", m.group(1).lower())[:6] for m in _BUG_PART.finditer(
        _body_of(ticket))}
    return len(parts & {"steps", "repro", "expect", "actual"}) >= 2


def _plain(text: Any) -> str:
    """Words without markdown emphasis/code marks, whitespace folded: what "word for word" is
    checked on (a model may drop the ** a ticket wraps a label in)."""
    return _norm(re.sub(r"[*`_]+", "", str(text or "")))


def prose_input(ticket: Optional[Mapping[str, Any]]) -> str:
    """What the prose reader must read for this ticket: the whole description for a bug ticket
    or one the structure gives fewer than MIN_STRUCTURAL_ITEMS items, else only the prose the
    structure left (prose_to_read). Empty: nothing to read, no model call."""
    if not isinstance(ticket, Mapping):
        return ""
    body = _body_of(ticket)
    if not body.strip():
        return ""
    found = len(prd_gating_criteria(body)) + len(requirement_items(body))
    if _is_bug(ticket) or found < MIN_STRUCTURAL_ITEMS:
        return body
    return prose_to_read(body)


def prose_prompt(ticket: Mapping[str, Any], text: str, listed: list[str]) -> str:
    kind = "a BUG report" if _is_bug(ticket) else "a ticket"
    return (
        "You are BOSS's REQUIREMENTS READER. Below is the text of " + kind + " (\"" +
        str(ticket.get("title") or "") + "\"). List every concrete, checkable requirement it "
        "STATES that the work must deliver — what an acceptance check will hold the build to.\n"
        "RULES:\n"
        "- Copy each requirement WORD FOR WORD from the text: a whole sentence or a whole "
        "clause of one, never paraphrased, never merged across sentences. BOSS keeps only "
        "words it finds in the text.\n"
        "- One item per concrete, checkable requirement; keep trivially related parts (a list "
        "of fields) in one item.\n"
        "- Leave out context: background and motivation, who reported it, the environment, "
        "what happens today, anything the text says is out of scope or for later. In a bug "
        "report the steps to reproduce and the actual behaviour are context; the EXPECTED "
        "behaviour is the requirement.\n"
        "- Leave out what is already listed below.\n"
        f"- At most {MAX_PROSE_ITEMS} items.\n"
        "THEN, over EVERY requirement (the ones listed below and yours), copy exactly:\n"
        "- `judgement`: the ones that are a matter of taste with no objective check (naming "
        "style, clarity, 'clean' or 'idiomatic' code). A behaviour, a file, a command, a "
        "language, a limit or a test is never one.\n"
        "- `needs_loader`: the ones that name a data file the program itself must READ to "
        "deliver them (a rulebook, a schema, a config the code loads) — not a file that is the "
        "deliverable on its own (a compose file, an example, a CI workflow, a Makefile).\n\n"
        "ALREADY LISTED:\n" + ("\n".join(f"- {x}" for x in listed) or "(none)") + "\n\n"
        "THE TEXT:\n" + (text[:MAX_PROSE_CHARS] or "(no further text)") + "\n\n"
        "Answer ONLY with a JSON object between these two lines:\n" + _PROSE_OPEN + "\n"
        '{"items": [{"text": "<the words, copied>", "why": "<why it is a requirement>"}], '
        '"judgement": ["<a requirement, copied>"], "needs_loader": ["<a requirement, copied>"]}\n'
        + _PROSE_CLOSE + "\n")


def _reply_json(reply: Any) -> dict[str, Any]:
    body = str(reply or "")
    if _PROSE_OPEN in body:
        body = body.split(_PROSE_OPEN, 1)[1].split(_PROSE_CLOSE, 1)[0]
    m = re.search(r"\{.*\}", body, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def parse_tags(reply: Any, candidates: Iterable[str]) -> dict[str, list[str]]:
    """{judgement, needs_loader}: the requirements the reader tagged, each one of `candidates`
    word for word (case and punctuation folded); anything else is ignored."""
    by_key = {_key(c): c for c in candidates if _key(c)}
    data = _reply_json(reply)
    out: dict[str, list[str]] = {}
    for tag in ("judgement", "needs_loader"):
        raw = data.get(tag)
        picked = [by_key[_key(t)] for t in (raw if isinstance(raw, list) else [])
                  if isinstance(t, str) and _key(t) in by_key]
        out[tag] = list(dict.fromkeys(picked))
    return out


def parse_prose(reply: Any, text: str) -> list[str]:
    """The reader's items that are the text's own words (each checked against `text`), deduped,
    at most MAX_PROSE_ITEMS. A reply that cannot be read is no items, never invented ones."""
    raw = _reply_json(reply).get("items")
    plain_text = _plain(text)
    out: list[str] = []
    seen: set[str] = set()
    for it in raw if isinstance(raw, list) else []:
        words = _verbatim(it.get("text") if isinstance(it, Mapping) else it)
        if not words or len(_plain(words)) < 8 or _plain(words) not in plain_text:
            continue
        if _key(words) not in seen:
            seen.add(_key(words))
            out.append(words)
        if len(out) >= MAX_PROSE_ITEMS:
            break
    return out


class ProseReadInTransaction(RuntimeError):
    """The prose reader (a model call) was about to run while this context holds an open
    database transaction (BOSSAPP-1232's class: model/IO inside a transaction is ended by
    Postgres as idle in transaction)."""


#: Set (any non-empty value but 0) to make the guard raise instead of log: the tests do.
STRICT_GUARD_ENV = "BOSS_STRICT_TRANSACTION_GUARD"


def _transaction_open() -> bool:
    try:
        from ...db import open_transactions

        return bool(open_transactions())
    except Exception:  # noqa: BLE001 — no tracker is no transaction we know of
        return False


def reader_version(model: Optional[str] = None) -> str:
    """The reader's prompt (its wording, hashed) and model: part of the digest, so a new prompt
    or a different model reads the ticket again."""
    return _digest(["reader", prose_prompt({"title": ""}, "", []), str(model or "")])


def prose_digest(ticket: Optional[Mapping[str, Any]], *,
                 model: Optional[str] = None) -> Optional[str]:
    """The digest of what the reader reads for this ticket (its whole description: the reader
    also tags every item), with the reader's version; None when there is nothing to read."""
    if not isinstance(ticket, Mapping) or not _body_of(ticket).strip():
        return None
    return _digest(["prose", reader_version(model), "bug" if _is_bug(ticket) else "",
                    _body_of(ticket)])


_PROSE_KEYS = ("prose_digest", "prose_items", "judgement_items", "loader_items")


def _no_prose() -> dict[str, Any]:
    return {"prose_digest": None, "prose_items": [], "judgement_items": [], "loader_items": []}


def ticket_prose(ticket: Optional[Mapping[str, Any]], *, ask: Any = None,
                 held: Optional[Mapping[str, Any]] = None,
                 model: Optional[str] = None) -> dict[str, Any]:
    """{prose_digest, prose_items}: what the prose reader found in this ticket. The reader is
    asked ONCE per distinct prose: `held` (the contract's stored ticket source) answers for the
    same digest, so a check never re-asks and a contract never flaps on a second opinion.

    `ask(prompt) -> reply text` is a MODEL CALL: it is never made inside an open database
    transaction (prepare_for_run makes it before the gate's transaction opens; inside one it is
    refused — an error under BOSS_STRICT_TRANSACTION_GUARD, a warning otherwise). Without an
    ask (or when it fails, or is refused) what the contract already holds stands: `held`'s
    items for a changed text until the reader runs again, nothing for a new one."""
    digest = prose_digest(ticket, model=model)
    if digest is None or not isinstance(ticket, Mapping):
        return _no_prose()
    kept = {**_no_prose(), **{k: (held or {}).get(k) for k in _PROSE_KEYS
                              if (held or {}).get(k) is not None}}
    if held and held.get("prose_digest") == digest:
        return kept
    if ask is None:
        return kept
    if _transaction_open():
        message = ("acceptance contract: the prose reader (a model call) would run inside an "
                   "open database transaction; read the prose first (prepare_for_run)")
        if os.environ.get(STRICT_GUARD_ENV, "") not in ("", "0"):
            raise ProseReadInTransaction(message)
        _log.warning(message)
        return kept
    text = prose_input(ticket)
    try:
        reply = ask(prose_prompt(ticket, text, ticket_items(ticket)))
    except Exception as exc:  # noqa: BLE001 — the structural items still stand
        _log.warning("acceptance contract: the prose reader failed (%s)", type(exc).__name__)
        return kept
    found = parse_prose(reply, text)
    tags = parse_tags(reply, ticket_items(ticket, prose=found))
    return {"prose_digest": digest, "prose_items": found,
            "judgement_items": tags["judgement"], "loader_items": tags["needs_loader"]}


def _tagged(ticket_words: Iterable[str], prose: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ticket's items as source items, each with the tags the reader gave it at
    extraction (judgement: advisory in the gate; needs_loader: a MET must name its loader)."""
    judgement = {_key(t) for t in prose.get("judgement_items") or []}
    loader = {_key(t) for t in prose.get("loader_items") or []}
    out = []
    for w in ticket_words:
        it: dict[str, Any] = {"source": "ticket", "text": w}
        if _key(w) in judgement:
            it["judgement"] = True
        if _key(w) in loader:
            it["needs_loader"] = True
        out.append(it)
    return out


def _ticket_and_held(connection: Any, *, run_id: str, fctx: Optional[Mapping[str, Any]]
                     ) -> tuple[Optional[str], Optional[dict], Optional[dict]]:
    """(project id, the run's ticket snapshot, the lineage contract's ticket source)."""
    root = lineage_root(run_id, fctx)
    project_id = _project_of(connection, run_id) if root else None
    if not project_id:
        return None, None, None
    ticket = ticket_of_run(connection, run_id) or (
        ticket_of_run(connection, root) if root != run_id else None)
    cur = latest(connection, root_run_id=root, project_id=project_id)
    held = next((x for x in (cur or {}).get("sources") or [] if x.get("kind") == "ticket"),
                None)
    return project_id, ticket, held


def prepare_for_run(db_engine: Any, *, run_id: str, fctx: Optional[Mapping[str, Any]],
                    own_spec: Mapping[str, Any], ask: Any,
                    model: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Read the ticket's prose (a model call) and store it with the contract — BEFORE the
    gate's transaction opens, never inside one (BOSSAPP-1232/1233):

      1. a short read-only transaction: the run's ticket and what the contract already holds;
      2. NO transaction: the prose reader, only when the prose changed (or was never read);
      3. a short transaction: the contract frozen/revised with what was read.

    The gate's own transaction (items_spec -> ensure_for_run) then finds the prose stored and
    makes no model call. Returns the contract, or None when there was nothing to read."""
    from ...db import begin

    with begin(db_engine) as c:
        project_id, ticket, held = _ticket_and_held(c, run_id=run_id, fctx=fctx)
    digest = prose_digest(ticket, model=model)
    if not ticket or digest is None or (held and held.get("prose_digest") == digest):
        return None
    prose = ticket_prose(ticket, ask=ask, held=held, model=model)
    if prose.get("prose_digest") != digest:
        return None                       # the reader did not answer: nothing new to store
    with begin(db_engine) as c:
        return ensure_for_run(c, run_id=run_id, fctx=fctx, own_spec=own_spec, ticket=ticket,
                              prose=prose)


def _ticket_ref(ticket: Mapping[str, Any]) -> str:
    return str(ticket.get("key") or ticket.get("id") or ticket.get("url") or "ticket")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def gather_sources(spec: Mapping[str, Any], *, ticket: Optional[Mapping[str, Any]] = None,
                   templates: Iterable[Mapping[str, Any]] = (),
                   operator_items: Iterable[str] = (),
                   prose: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """The contract's sources: [{kind, ref, url, items: [{source, text}], digest}], ticket first.

    `spec` is the lineage's build spec (a union spec for a decomposed PRD: union_spec); its items
    are exactly the ones the gate reads today (_raw_items). `ticket` is the run's ticket snapshot
    (ticket_sync.snapshot's shape), `templates` accepted-delivery templates a caller opted in
    ({id, title, criteria}), `operator_items` words a person added."""
    out: list[dict[str, Any]] = []
    if ticket:
        prose = dict(prose or ticket_prose(ticket))
        words = ticket_items(ticket, prose=prose["prose_items"])
        if words:
            out.append({"kind": "ticket", "ref": _ticket_ref(ticket), "url": ticket.get("url"),
                        "provider": ticket.get("provider"),
                        "updated_at": ticket.get("updated_at"), **prose,
                        "items": _tagged(words, prose)})
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


def _src(source: str, kind: str, ref: str) -> dict[str, str]:
    return {"source": source, "source_kind": kind, "source_ref": ref}


#: Properties an item gets AT EXTRACTION (the reader, ticket_prose), never from a reviewer's
#: answer: `judgement` (a matter of taste: a PARTIAL on it is advisory, a MISSING never is) and
#: `needs_loader` (it names a data file the program must read: a MET must name its loader).
ITEM_TAGS = ("judgement", "needs_loader")


def build_items(sources: Iterable[Mapping[str, Any]], *, start: int = 1) -> list[dict[str, Any]]:
    """Numbered items AC-<start>.. from the sources, in order, deduplicated by their words. No
    cap on count or length (the gate applies its own when it reads them).

    A deduplicated item keeps EVERY source that asked for it (`sources`): "Written in Go" that
    is both a brief must-have and a ticket criterion is one item with two sources, so a ticket
    that later drops it leaves the brief's must-have standing (revise_from_ticket). `source`,
    `source_kind` and `source_ref` name the first one, the gate's label."""
    by_key: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    for s in sources:
        kind, ref = str(s.get("kind") or ""), str(s.get("ref") or "")
        for it in s.get("items") or []:
            key = _key(it.get("text"))
            if not key:
                continue
            origin = _src(str(it.get("source") or kind or "criterion"), kind, ref)
            tags = {t: True for t in ITEM_TAGS if it.get(t)}
            if key in by_key:
                held = by_key[key]["sources"]
                if origin not in held:
                    held.append(origin)
                by_key[key].update(tags)
                continue
            n = start + len(items)
            item = {"id": f"AC-{n}", "n": n, "text": _verbatim(it["text"]), **origin,
                    "sources": [origin], "priority": "must", "check_hint": None, **tags}
            by_key[key] = item
            items.append(item)
    return items


def item_sources(item: Mapping[str, Any]) -> list[dict[str, str]]:
    """Every source of an item (an item written before `sources` existed has its one)."""
    listed = [dict(x) for x in item.get("sources") or [] if isinstance(x, Mapping)]
    return listed or [_src(str(item.get("source") or "criterion"),
                           str(item.get("source_kind") or ""), str(item.get("source_ref") or ""))]


def _from_ticket(item: Mapping[str, Any]) -> bool:
    return any(x.get("source") == "ticket" for x in item_sources(item))


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
        row = {k: s.get(k) for k in ("kind", "ref", "url", "provider", "updated_at", "digest",
                                     *_PROSE_KEYS)
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


class ContractRevisionRefused(PermissionError):
    """A revision a person may not make here (or that was not made by a person)."""


#: The catalogue permission a person needs to change what a lineage is held to: deciding what
#: the work must deliver is managing the work.
REVISE_PERMISSION = "work_item:manage"


def _person(actor: Any) -> Optional[str]:
    actor = str(actor or "").strip()
    return actor if actor.startswith("usr_") else None


def _may_revise(project_id: str, actor: str) -> None:
    """PERSON-ONLY. An agent, a job or 'boss' never revises a contract through `revise` (BOSS's
    own change — the ticket's criteria — goes through revise_from_ticket, audited as such).
    Under an enforced request the PDP decides on the request's principal, who must be `actor`
    and hold REVISE_PERMISSION at the project."""
    who = _person(actor)
    if who is None:
        raise ContractRevisionRefused("only a person may revise an acceptance contract")
    from ...identity import lineage, viewer

    allowed = viewer.decide(project_id, REVISE_PERMISSION)
    if allowed is None:
        return                      # no guard enforced this call (a script, a test): person id only
    principal = getattr(lineage.current_context(), "principal", None)
    if (not allowed or principal is None or not principal.is_person
            or str(principal.id) != who):
        raise ContractRevisionRefused(
            f"revising an acceptance contract needs {REVISE_PERMISSION} at this project")


def _newest(connection: Any, contract_id: str) -> dict[str, Any]:
    cur = get(connection, contract_id)
    if cur is None:
        raise ValueError(f"no acceptance contract {contract_id}")
    newest = latest(connection, root_run_id=cur["lineage_root_run_id"],
                    project_id=cur["project_id"])
    return newest if newest is not None else cur          # versions form one line


def _new_version(connection: Any, cur: Mapping[str, Any], items: list[dict[str, Any]], *,
                 reason: str, actor: str, sources: Optional[list] = None,
                 added: Iterable[str] = (), removed: Iterable[str] = (),
                 retagged: Iterable[str] = ()) -> dict[str, Any]:
    version = int(cur["version"]) + 1
    _insert(connection, project_id=cur["project_id"], root_run_id=cur["lineage_root_run_id"],
            version=version, sources=list(sources if sources is not None else cur["sources"]),
            items=items, workflow_run_id=cur.get("workflow_run_id"), supersedes_id=cur["id"],
            reason=str(reason or "").strip() or "revised", actor=actor)
    found = latest(connection, root_run_id=cur["lineage_root_run_id"],
                   project_id=cur["project_id"])
    assert found is not None
    from ..core import emit_event

    emit_event(connection, cur["project_id"], "acceptance.contract_revised", actor,
               "acceptance_contract", found["id"],
               {"lineage_root_run_id": cur["lineage_root_run_id"], "version": found["version"],
                "supersedes_id": cur["id"], "reason": found["reason"], "by": actor,
                "added": list(added), "removed": list(removed), "retagged": list(retagged)})
    return found


def revise(connection: Any, contract_id: str, *, added: Iterable[Any] = (),
           removed: Iterable[str] = (), reason: str, actor: str) -> dict[str, Any]:
    """A PERSON's new version of the contract (supersedes_id = the newest): `removed` names item
    IDS (AC-n; words never match), `added` items ({text, source_ref} or words) join with new ids,
    attributed to the operator. Every kept item keeps its id. Refused unless `actor` is a person
    allowed to (`_may_revise`). Nothing to change returns the contract as it is."""
    cur = _newest(connection, contract_id)
    _may_revise(cur["project_id"], actor)
    gone = {str(r).strip() for r in removed or ()}
    unknown = gone - {i["id"] for i in cur["items"]}
    if unknown:
        raise ValueError(f"no such items in contract v{cur['version']}: {sorted(unknown)}")
    kept = [i for i in cur["items"] if i["id"] not in gone]
    have = {_key(i["text"]) for i in kept}
    n = _next_number(connection, cur["lineage_root_run_id"])
    joined: list[dict[str, Any]] = []
    for a in added or ():
        a = {"text": a} if isinstance(a, str) else dict(a)
        words = _verbatim(a.get("text"))
        if not _key(words) or _key(words) in have:
            continue
        have.add(_key(words))
        origin = _src("operator", "operator", str(a.get("source_ref") or actor))
        joined.append({"id": f"AC-{n}", "n": n, "text": words, **origin, "sources": [origin],
                       "priority": "must", "check_hint": None})
        n += 1
    if not joined and not gone:
        return cur
    return _new_version(connection, cur, kept + joined, reason=reason, actor=actor,
                        added=[i["id"] for i in joined], removed=sorted(gone))


def _record_reading(connection: Any, cur: Mapping[str, Any], prose: Mapping[str, Any]) -> None:
    from sqlalchemy import text

    sources = [dict(x, **{k: prose.get(k) for k in _PROSE_KEYS})
               if x.get("kind") == "ticket" else x for x in cur["sources"]]
    connection.execute(text("UPDATE acceptance_contracts SET sources = CAST(:s AS JSONB) "
                            "WHERE id = :i"), {"s": json.dumps(sources, default=str),
                                               "i": cur["id"]})


def revise_from_ticket(connection: Any, contract: Mapping[str, Any],
                       ticket: Optional[Mapping[str, Any]], *,
                       actor: str = "boss",
                       prose: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """The contract with the ticket's CURRENT acceptance criteria — BOSS's own revision, made
    because the run's ticket snapshot changed (a person chose "Include it" on the mid-run change
    card, ticket_sync.on_card_answered) and recorded as `acceptance.contract_revised` by BOSS.

    A criterion the ticket dropped loses its ticket source; the ITEM leaves only when the ticket
    was its only source (a brief must-have the ticket also listed stays, with its id). A new
    criterion that another source already asks for gains the ticket as a source; otherwise it
    joins with a new id. Unchanged criteria: the contract as it is."""
    if not ticket:
        return dict(contract)
    cur = _newest(connection, contract["id"])
    held_src = next((x for x in cur["sources"] if x.get("kind") == "ticket"), None)
    prose = dict(prose or ticket_prose(ticket, held=held_src))
    now = ticket_items(ticket, prose=prose["prose_items"])
    if not now:
        return dict(contract)
    ref = _ticket_ref(ticket)
    now_keys = {_key(w) for w in now}
    want = {_key(t["text"]): {g: True for g in ITEM_TAGS if t.get(g)}
            for t in _tagged(now, prose)}
    held_keys = {_key(i["text"]) for i in cur["items"] if _from_ticket(i)}
    held_tags = {_key(i["text"]): {g: True for g in ITEM_TAGS if i.get(g)}
                 for i in cur["items"] if _from_ticket(i)}
    if held_keys == now_keys and all(held_tags.get(k) == want.get(k) for k in now_keys):
        if held_src is not None and prose.get("prose_digest") and \
                prose.get("prose_digest") != held_src.get("prose_digest"):
            # Read again (a new prompt or model, or a first read) and nothing changed: keep the
            # reading's digest, so the next check does not ask again. Bookkeeping, not a version.
            _record_reading(connection, cur, prose)
            return _newest(connection, cur["id"])
        return cur
    origin = _src("ticket", "ticket", ref)
    items: list[dict[str, Any]] = []
    removed, retagged = [], []
    for i in cur["items"]:
        i = dict(i)
        srcs = item_sources(i)
        k = _key(i["text"])
        if k in now_keys and (_from_ticket(i) or k in want):
            tags = want.get(k) or {}
            if any(bool(i.get(g)) != bool(tags.get(g)) for g in ITEM_TAGS):
                for g in ITEM_TAGS:
                    i.pop(g, None)
                i.update(tags)
                retagged.append(i["id"])
        if _from_ticket(i) and k not in now_keys:
            rest = [x for x in srcs if x.get("source") != "ticket"]
            if not rest:
                removed.append(i["id"])
                continue
            i.update(sources=rest, **rest[0])
            retagged.append(i["id"])
        elif not _from_ticket(i) and k in now_keys:
            i["sources"] = [*srcs, origin]
            retagged.append(i["id"])
        items.append(i)
    have = {_key(i["text"]) for i in items}
    n = _next_number(connection, cur["lineage_root_run_id"])
    added = []
    for w in now:
        if _key(w) in have:
            continue
        have.add(_key(w))
        items.append({"id": f"AC-{n}", "n": n, "text": _verbatim(w), **origin,
                      "sources": [origin], "priority": "must", "check_hint": None,
                      **want.get(_key(w), {})})
        added.append(f"AC-{n}")
        n += 1
    n_add, n_rm = len(added), len(removed)
    retagged = list(dict.fromkeys(retagged))
    words = ", ".join(x for x in (f"{n_add} item{'s' * (n_add != 1)} added" if n_add else "",
                                  f"{n_rm} removed" if n_rm else "",
                                  f"{len(retagged)} re-sourced" if retagged else "") if x)
    source = {"kind": "ticket", "ref": ref, "url": ticket.get("url"),
              "provider": ticket.get("provider"), "updated_at": ticket.get("updated_at"),
              "digest": _digest(now), "count": len(now),
              **{k: v for k, v in prose.items() if v is not None}}
    # The same ticket snapshot read again by a reader that now takes in more of it (BOSSAPP-1233:
    # the whole description, not only its "Acceptance" list) did not change: say so.
    unchanged = bool(held_src and ticket.get("updated_at")
                     and str(held_src.get("updated_at")) == str(ticket.get("updated_at")))
    sources = [x for x in cur["sources"] if x.get("kind") != "ticket"]
    sources.insert(0, source)
    reason = f"{ref} read in full: {words}" if unchanged else f"{ref} changed: {words}"
    return _new_version(connection, cur, items, reason=reason, actor=actor,
                        sources=sources, added=added, removed=removed, retagged=retagged)


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
                   ticket: Any = "_read",
                   prose: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
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
        sources = gather_sources(spec, ticket=ticket, prose=prose)
        if not build_items(sources):
            return None
        found = freeze(connection, project_id=project_id, root_run_id=root, sources=sources,
                       workflow_run_id=(fctx or {}).get("workflow_run_id"))
    return revise_from_ticket(connection, found, ticket, prose=prose)


def gate_items(contract: Mapping[str, Any]) -> list[dict[str, str]]:
    """The contract's items in the gate's union shape ({source, text}): what items_spec hands
    the in-build check, so the inner seat is held to the same list (LLD §3)."""
    return [{"source": str(i.get("source") or "criterion"), "text": str(i["text"]),
             **{t: True for t in ITEM_TAGS if i.get(t)}}
            for i in contract.get("items") or []]
