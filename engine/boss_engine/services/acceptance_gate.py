"""THE ACCEPTANCE GATE (`acceptance_met`, BOSSAPP-1211 / RCA-E): did the build deliver what was
asked, on the whole branch, before anyone is asked to merge it?

WHY IT EXISTS. The Northgate build (2026-11-15) shipped missing about fifteen must-haves. Its
reviewers said so (adversarial refuted, consistency drift, review BLOCKERs), but under the
community default profile `lean` those were advisory opinions, the PR opened, and the follow-up
run's spec was two MINOR findings, so its merge card said "Automated checks: passed". Nothing in
the pipeline ever held the build up against the brief's own must-haves.

WHAT IT DOES.
  - The items are the ROOT run's: the brief's must-haves (render_brief's "Must-haves:" block),
    the PRD's tested-and-gating acceptance list, and the run's acceptance criteria. A follow-up
    run inherits its root's items (its own spec is a handful of findings) — `items_spec`.
  - An LLM acceptance reviewer (the consistency lane's agent and model, read-only, the WHOLE
    branch tree, not the diff) answers every item MET / PARTIAL / MISSING with file evidence.
    It is a reviewer that thinks, not a keyword list: the code decides only the protocol (every
    item answered, evidence cited).
  - A deterministic check that the stack the spec requires has its manifest in the tree (a Go
    brief has a go.mod). `required_stack` is the one function RCA-A's structured
    `required_stack` replaces.
  - A MISSING item or a missing manifest is a fact, not an opinion: the gate is in the advisory
    hard floor (governed_code_task_service._ADVISORY_HARD_FLOOR), so under `lean` it gets its
    repair rounds and then a labelled checks-failing draft PR with a follow-up carrying the
    missing items — never a "passed" card.
  - The merge card reads the UNION of the gates the lineage shipped past (`lineage_checks`), so
    a clean follow-up on a PR whose earlier builds failed can never say "passed".
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

GATE = "acceptance_met"
SEAT = "acceptance_check"
#: The key on the evidence (code_task_runs.state), the merge decision and the PR body.
KEY = "acceptance"
#: The evidence key holding the gate's findings as FINDING lines (checks_failing.SEAT_OF_GATE).
SUMMARY_KEY = "acceptance_summary"
MAX_ITEMS = 40
MAX_ITEM_CHARS = 1000
MAX_TREE_FILES = 600

MET, PARTIAL, MISSING, UNJUDGED = "met", "partial", "missing", "unjudged"

_SKIP_DIRS = frozenset({".git", "node_modules", "vendor", ".venv", "venv", "dist", "build",
                        "__pycache__", ".next", "target", ".pytest_cache", ".mypy_cache"})


# ---------------------------------------------------------------------------------------------
# What was asked
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
    list, then the run's acceptance criteria — deduplicated, each whole up to MAX_ITEM_CHARS.
    The existing-tests promise is left to the regression gate, which measures it."""
    from .code_task_service import EXISTING_TESTS_CRITERION

    spec = _spec_of(spec)
    instructions = str(spec.get("instructions") or "")
    raw: list[tuple[str, str]] = []
    raw += [("must_have", m) for m in brief_must_haves(instructions)]
    raw += [("prd_gating", m) for m in prd_gating_criteria(instructions)]
    raw += [("criterion", _norm(c)) for c in (spec.get("acceptance_criteria") or [])
            if _norm(c) and _norm(c) != _norm(EXISTING_TESTS_CRITERION)]
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for source, item in raw:
        key = re.sub(r"\W+", " ", item.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        items.append({"n": len(items) + 1, "text": item[:MAX_ITEM_CHARS], "source": source})
        if len(items) >= MAX_ITEMS:
            break
    return items


def items_spec(connection: Any, *, run_id: str, fctx: Mapping[str, Any],
               own_spec: Mapping[str, Any]) -> dict:
    """Whose items a run is held to. A FOLLOW-UP inherits its lineage root's: its own spec is
    the findings it was sent to fix, and judged against those alone it "passes" while the
    must-haves the root missed stay missing (crun_15f0504ac9f64eaeb4a1). Every other run (a
    repair child copies its parent's spec) is held to its own."""
    root = str((fctx or {}).get("root_run_id") or "").strip()
    if not (fctx or {}).get("is_followup") or not root or root == run_id or connection is None:
        return _spec_of(own_spec)
    from sqlalchemy import text

    row = connection.execute(text("SELECT spec FROM code_task_runs WHERE id = :id"),
                             {"id": root}).first()
    root_spec = _spec_of(row[0]) if row else {}
    return root_spec if acceptance_items(root_spec) else _spec_of(own_spec)


# ---------------------------------------------------------------------------------------------
# The required stack (RCA-A replaces `required_stack` with the spec's structured field)
# ---------------------------------------------------------------------------------------------

_NEGATED = re.compile(r"(?:\bno|\bnot|\bwithout|\binstead of|\bnever|\bavoid)\s+(?:\w+\s+){0,1}$",
                      re.I)
STACKS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("go", ("go.mod",),
     (r"\bGolang\b", r"\(\s*Go\s*\)", r"\b(?:in|using|with|written in)\s+Go\b(?!\s+to\b)",
      r"\bGo\s+(?:\d|service|binary|module|CLI|gateway|server|backend|API|program|code)\b",
      r"\bgo\.mod\b")),
    ("rust", ("Cargo.toml",), (r"\bRust\b", r"\bCargo\.toml\b")),
    ("python", ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile"),
     (r"\bPython\b", r"\bFastAPI\b", r"\bDjango\b", r"\bFlask\b")),
    ("node", ("package.json",),
     (r"\bNode(?:\.js)?\b", r"\bTypeScript\b", r"\bNext\.js\b", r"\bReact\b", r"\bExpress\b",
      r"\bNestJS\b")),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts"),
     (r"\bJava\b", r"\bSpring Boot\b", r"\bKotlin\b")),
)


def _stack_text(spec: Mapping[str, Any]) -> str:
    """Where a stack REQUIREMENT is stated: the title, the must-haves and any "Stack:" line.
    Never the whole PRD, which names rejected alternatives and the tools around the app."""
    spec = _spec_of(spec)
    instructions = str(spec.get("instructions") or "")
    first = instructions.strip().splitlines()[0] if instructions.strip() else ""
    stack_lines = re.findall(r"(?im)^\s*\**stack\**\s*:\s*(.+)$", instructions)
    return "\n".join([first, *brief_must_haves(instructions), *stack_lines])


def required_stack(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stacks the spec REQUIRES, conservatively: named in the title, a must-have or a
    "Stack:" line, not negated ("no Python"). [{stack, manifests, because}]. THE ONE PLACE RCA-A
    replaces with the spec's structured `required_stack`."""
    spec = _spec_of(spec)
    structured = spec.get("required_stack")
    if isinstance(structured, list) and structured:
        known = {s: m for s, m, _ in STACKS}
        return [{"stack": str(s), "manifests": list(known.get(str(s), ())),
                 "because": "the spec's required_stack"}
                for s in structured if str(s) in known]
    text = _stack_text(spec)
    out = []
    for stack, manifests, patterns in STACKS:
        for pat in patterns:
            hit = next((m for m in re.finditer(pat, text)
                        if not _NEGATED.search(text[max(0, m.start() - 24):m.start()])), None)
            if hit:
                lo, hi = max(0, hit.start() - 40), min(len(text), hit.end() + 40)
                out.append({"stack": stack, "manifests": list(manifests),
                            "because": _norm(text[lo:hi])})
                break
    return out


def _walk(root: Path, max_depth: int = 6) -> Iterable[Path]:
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        rel_depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and rel_depth < max_depth)
        for f in sorted(filenames):
            yield Path(dirpath) / f


def stack_manifest_check(root: Any, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Each required stack and the manifest found for it (None = missing) in the branch tree."""
    required = required_stack(spec)
    if not required or root is None:
        return {"required": required, "found": {}, "missing": []}
    wanted = {m for r in required for m in r["manifests"]}
    present: dict[str, str] = {}
    for p in _walk(Path(root)):
        if p.name in wanted and p.name not in present:
            present[p.name] = str(p.relative_to(root))
    found = {r["stack"]: next((present[m] for m in r["manifests"] if m in present), None)
             for r in required}
    return {"required": required, "found": found,
            "missing": [s for s, path in found.items() if not path]}


def tree_listing(root: Any, limit: int = MAX_TREE_FILES) -> str:
    """The branch's files, relative, for the reviewer's orientation (it reads them itself)."""
    if root is None:
        return ""
    paths = []
    for p in _walk(Path(root)):
        paths.append(str(p.relative_to(root)))
        if len(paths) >= limit:
            paths.append(f"... (more than {limit} files; open the tree)")
            break
    return "\n".join(paths)


# ---------------------------------------------------------------------------------------------
# The reviewer
# ---------------------------------------------------------------------------------------------

def acceptance_prompt(items: list[dict], *, title: str, stack: Mapping[str, Any],
                      tree: str) -> str:
    listed = "\n".join(f"ITEM {i['n']} [{i['source']}]: {i['text']}" for i in items)
    stack_lines = ""
    if stack.get("required"):
        stack_lines = "\n\nREQUIRED STACK (from the spec): " + ", ".join(
            f"{r['stack']} ({'manifest ' + str(stack['found'].get(r['stack'])) if stack['found'].get(r['stack']) else 'NO manifest found: ' + '/'.join(r['manifests'])})"
            for r in stack["required"])
    return (
        "You are BOSS's ACCEPTANCE REVIEWER for the build \"" + str(title or "") + "\". "
        "Your one question: does the code ON THIS BRANCH — the whole working tree in front of "
        "you, not only the latest change — deliver each item the person and the approved PRD "
        "asked for? Read the files. Judge what the code does, not what its README, comments or "
        "commit messages claim. A stub, a TODO, a hard-coded demo answer, a test that asserts "
        "nothing, or a feature present in a different language/stack than required is not MET. "
        "Do NOT edit any file.\n\n"
        "THE ITEMS:\n" + listed + stack_lines + "\n\n"
        "Answer EVERY item, one line each, in this exact form:\n"
        "  ITEM <n>: MET — <the file(s) and function(s)/test(s) that deliver it>\n"
        "  ITEM <n>: PARTIAL — <what exists (files) and what is still missing>\n"
        "  ITEM <n>: MISSING — <what you looked for and where; what is absent>\n"
        "MET requires file evidence (a path in the tree). When unsure, say PARTIAL and why.\n"
        "End with EXACTLY one final line:\n  ACCEPTANCE: MET\n  ACCEPTANCE: NOT MET\n\n"
        "THE BRANCH'S FILES:\n" + (tree or "(empty)") + "\n"
    )


_ANSWER = re.compile(
    r"(?im)^[\s>*\-•]*\**\s*ITEM\s*#?\s*(\d+)\s*\**\s*[:.)\-]\s*\**\s*(MET|PARTIAL|MISSING)\b\**"
    r"\s*(?:[—–:\-]+\s*)?(.*)$")
_PATHISH = re.compile(r"[\w.\-]+/[\w.\-/]+|\b[\w\-]+\.(?:go|py|js|mjs|ts|tsx|jsx|rs|java|kt|rb|"
                      r"php|cs|yaml|yml|json|toml|md|sql|sh|html|css|mod)\b")


def parse_acceptance(reply: str, items: list[dict]) -> list[dict[str, Any]]:
    """Each item with the reviewer's status and evidence (the LAST answer for an item wins).
    A MET with no file cited reads as PARTIAL: the protocol asks for evidence, and an
    unevidenced claim is what this gate exists to stop."""
    answers: dict[int, tuple[str, str]] = {}
    for m in _ANSWER.finditer(str(reply or "")):
        answers[int(m.group(1))] = (m.group(2).lower(), _norm(m.group(3))[:600])
    out = []
    for it in items:
        status, why = answers.get(int(it["n"]), (UNJUDGED, ""))
        if status == MET and not _PATHISH.search(why):
            status, why = PARTIAL, (why + " (no file evidence cited)").strip()
        out.append({**it, "status": status, "evidence": why})
    return out


def judge(items: list[dict], reply: Optional[str], stack: Mapping[str, Any]) -> dict[str, Any]:
    """The gate's record. verdict:
      met       every item MET or PARTIAL, every required manifest present
      not_met   an item MISSING, or a required stack's manifest absent (facts)
      not_run   the reviewer answered nothing parseable (the check's fault, not the code's)
      no_items  nothing to hold the build to (and no stack requirement)"""
    stack = dict(stack or {})
    judged = parse_acceptance(reply or "", items) if items else []
    answered = [i for i in judged if i["status"] != UNJUDGED]
    missing = [i["n"] for i in judged if i["status"] == MISSING]
    partial = [i["n"] for i in judged if i["status"] == PARTIAL]
    unjudged = [i["n"] for i in judged if i["status"] == UNJUDGED]
    stack_missing = list(stack.get("missing") or [])
    if stack_missing or missing:
        verdict = "not_met"
    elif not items:
        verdict = "no_items"
    elif not answered:
        verdict = "not_run"
    else:
        verdict = "met"
    return {"verdict": verdict, "items": judged, "missing": missing, "partial": partial,
            "unjudged": unjudged, "stack": stack,
            "counts": {s: sum(1 for i in judged if i["status"] == s)
                       for s in (MET, PARTIAL, MISSING, UNJUDGED)}}


def passed(record: Optional[Mapping[str, Any]]) -> bool:
    return (record or {}).get("verdict") in ("met", "no_items")


def findings_summary(record: Optional[Mapping[str, Any]]) -> str:
    """The gate's findings as FINDING lines (review_comments.parse_findings reads them), so the
    checks-failing PR, its card and its follow-up carry each missing item verbatim."""
    record = record or {}
    lines = []
    for stack in (record.get("stack") or {}).get("missing") or []:
        req = next((r for r in (record.get("stack") or {}).get("required") or []
                    if r.get("stack") == stack), {})
        lines.append(f"FINDING: MAJOR — Required stack '{stack}' has no manifest in the tree "
                     f"(expected {' or '.join(req.get('manifests') or [])}; the spec says: "
                     f"\"{req.get('because', '')}\").")
    for i in record.get("items") or []:
        if i.get("status") == MISSING:
            lines.append(f"FINDING: MAJOR — Must-have {i['n']} MISSING: {i['text']}"
                         + (f" — {i['evidence']}" if i.get("evidence") else ""))
    for i in record.get("items") or []:
        if i.get("status") == PARTIAL:
            lines.append(f"FINDING: MAJOR — Must-have {i['n']} only PARTIAL: {i['text']}"
                         + (f" — {i['evidence']}" if i.get("evidence") else ""))
    if record.get("verdict") == "not_run":
        lines.append("The acceptance reviewer answered no item; the must-haves were not checked.")
    return "\n".join(lines)


def partial_followups(record: Optional[Mapping[str, Any]]) -> list[dict[str, str]]:
    """PARTIAL items of a build that otherwise met its must-haves: queued as follow-up work
    (deferred findings), never dropped."""
    return [{"severity": "major",
             "text": f"Must-have {i['n']} only partially delivered: {i['text']}"
                     + (f" — {i['evidence']}" if i.get("evidence") else "")}
            for i in (record or {}).get("items") or [] if i.get("status") == PARTIAL]


_STATUS_WORD = {MET: "met", PARTIAL: "partial", MISSING: "MISSING", UNJUDGED: "not judged"}


def must_have_table(record: Optional[Mapping[str, Any]]) -> str:
    """The must-have table (markdown) for the PR body and the merge card."""
    record = record or {}
    items = record.get("items") or []
    if not items and not (record.get("stack") or {}).get("required"):
        return ""
    def cell(s: Any) -> str:
        return _norm(s).replace("|", "\\|")
    rows = ["| # | Must-have | Status | Evidence |", "|---|---|---|---|"]
    rows += [f"| {i['n']} | {cell(i['text'])} | {_STATUS_WORD.get(i['status'], i['status'])} "
             f"| {cell(i.get('evidence'))} |" for i in items]
    for r in (record.get("stack") or {}).get("required") or []:
        path = ((record.get("stack") or {}).get("found") or {}).get(r["stack"])
        rows.append(f"| stack | {r['stack']} manifest ({' or '.join(r['manifests'])}) "
                    f"| {'met' if path else 'MISSING'} | {cell(path or 'not in the tree')} |")
    return "\n".join(rows)


def headline(record: Optional[Mapping[str, Any]]) -> str:
    c = (record or {}).get("counts") or {}
    if not (record or {}).get("items"):
        return ""
    bits = [f"{c.get(MET, 0)} met", f"{c.get(PARTIAL, 0)} partial", f"{c.get(MISSING, 0)} missing"]
    if c.get(UNJUDGED):
        bits.append(f"{c[UNJUDGED]} not judged")
    if (record.get("stack") or {}).get("missing"):
        bits.append("required stack missing: " + ", ".join(record["stack"]["missing"]))
    return "Must-haves: " + ", ".join(bits)


def pr_section(record: Optional[Mapping[str, Any]]) -> str:
    table = must_have_table(record)
    if not table:
        return ""
    return f"## Must-haves (acceptance check)\n\n{headline(record)}.\n\n{table}\n\n"


# ---------------------------------------------------------------------------------------------
# The lineage, for the merge card
# ---------------------------------------------------------------------------------------------

def _gates_shipped_past(fctx: Mapping[str, Any]) -> list[str]:
    fctx = fctx or {}
    out: list[str] = []
    out += [str(g) for g in fctx.get("advisory_failing_gates") or []]
    out += [str(g) for g in (fctx.get("unresolved_dissent") or {}).get("unresolved_gates") or []]
    out += [str(g.get("gate")) for g in (fctx.get("checks_failing") or {}).get("gates") or []
            if isinstance(g, Mapping) and g.get("gate")]
    return [g for g in dict.fromkeys(out) if g]


def lineage_failures(connection: Any, *, run_id: str,
                     current_acceptance: Optional[Mapping[str, Any]] = None,
                     current_failing: Iterable[str] = ()) -> dict[str, Any]:
    """The gates this pull request's builds SHIPPED PAST, across the lineage (root_run_id):
    {"gates": {gate: [run ids]}, "acceptance": the latest acceptance record}. A run that went
    on to a repair child never shipped (its child re-judged the whole spec) and is not counted.
    `acceptance_met` clears when the latest acceptance check (the root's must-haves, whole
    tree) met them; no other gate clears, because a follow-up re-judges only its own delta."""
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT project_id, COALESCE(finalize_ctx->>'root_run_id', id) FROM code_task_runs "
        "WHERE id = :id"), {"id": run_id}).first()
    gates: dict[str, list[str]] = {}
    for g in current_failing or ():
        gates.setdefault(str(g), []).append(run_id)
    latest_acc = dict(current_acceptance) if current_acceptance else None
    if row is None:
        return {"gates": gates, "acceptance": latest_acc}
    project_id, root = row[0], row[1]
    rows = connection.execute(text(
        "SELECT id, status, pull_request_url, finalize_ctx, state->'acceptance' AS acc "
        "FROM code_task_runs WHERE project_id = :p "
        "AND (id = :root OR finalize_ctx->>'root_run_id' = :root) ORDER BY created_at"),
        {"p": project_id, "root": root}).mappings().all()
    acc_seen, acc_run = None, None
    for r in rows:
        fctx = _spec_of(r["finalize_ctx"])
        shipped = (r["id"] == run_id or r["pull_request_url"]
                   or r["status"] == "completed")
        if shipped and r["status"] not in ("repairing", "cancelled", "failed"):
            for g in _gates_shipped_past(fctx):
                gates.setdefault(g, [])
                if r["id"] not in gates[g]:
                    gates[g].append(r["id"])
        acc = _spec_of(r["acc"])
        if acc.get("verdict") in ("met", "not_met") and r["id"] != run_id:
            acc_seen, acc_run = acc, r["id"]
    if latest_acc is None or latest_acc.get("verdict") not in ("met", "not_met"):
        latest_acc, latest_run = (acc_seen, acc_run) if acc_seen else (latest_acc, run_id)
    else:
        latest_run = run_id
    if latest_acc and latest_acc.get("verdict") == "met":
        gates.pop(GATE, None)
    elif latest_acc and latest_acc.get("verdict") == "not_met":
        runs = gates.setdefault(GATE, [])
        if latest_run and latest_run not in runs:
            runs.append(latest_run)
    return {"gates": gates, "acceptance": latest_acc}


def card_checks_failing(lineage: Mapping[str, Any], *, run_id: str) -> Optional[dict[str, Any]]:
    """A checks-failing record (checks_failing.collect's shape) for a merge card whose own run
    passed but whose lineage shipped past failing gates. None when nothing failed."""
    from . import checks_failing as cf

    gates = dict((lineage or {}).get("gates") or {})
    if not gates:
        return None
    rec = cf.collect(list(gates))
    for g in rec["gates"]:
        runs = [r for r in gates.get(g["gate"]) or [] if r != run_id]
        if runs:
            g["detail"] = ("shipped failing by an earlier build of this pull request ("
                           + ", ".join(runs[:4]) + ")")
    acc = (lineage or {}).get("acceptance")
    if GATE in gates and acc:
        from .review_comments import parse_findings

        rec["findings"] = [{"gate": GATE, "stage": SEAT, "security": False,
                            "severity": c.severity, "text": c.body}
                           for c in parse_findings(findings_summary(acc), stage=SEAT)]
    rec["lineage"] = True
    return rec


def merge_card_checks(connection: Any, *, run_id: Optional[str], evidence: Any,
                      checks_failing: Optional[dict]) -> tuple[Optional[dict], Optional[dict]]:
    """(the checks-failing record the merge card carries, the acceptance record it shows).

    The run's own checks-failing record when it has one; else the lineage's (`lineage_failures`)
    — so a follow-up whose own checks passed still shows the gates its PR's earlier builds
    shipped past. Never raises: a card whose lineage cannot be read still opens with its own."""
    import logging

    acceptance = evidence.get(KEY) if isinstance(evidence, Mapping) else None
    card = checks_failing
    if not run_id:
        return card, acceptance
    try:
        from . import checks_failing as cf

        with connection.begin_nested():
            lineage = lineage_failures(connection, run_id=run_id, current_acceptance=acceptance,
                                       current_failing=cf.gate_names(checks_failing))
        acceptance = lineage.get("acceptance") or acceptance
        if not checks_failing:
            card = card_checks_failing(lineage, run_id=run_id)
    except Exception:  # noqa: BLE001 — the card still opens; its own checks still show
        logging.getLogger(__name__).warning("lineage checks not read for %s", run_id,
                                            exc_info=True)
    return card, acceptance


__all__ = [
    "merge_card_checks",
    "GATE", "SEAT", "KEY", "SUMMARY_KEY", "acceptance_items", "acceptance_prompt",
    "brief_must_haves", "card_checks_failing", "findings_summary", "headline", "items_spec",
    "judge", "lineage_failures", "must_have_table", "parse_acceptance", "partial_followups",
    "passed", "pr_section", "prd_gating_criteria", "required_stack", "stack_manifest_check",
    "tree_listing",
]
