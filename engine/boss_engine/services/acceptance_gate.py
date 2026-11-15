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
  - The gate FAILS CLOSED: it passes only when every item is MET with evidence that exists in
    the tree (not docs alone) and the reviewer's own summary line agrees. A PARTIAL, MISSING or
    unanswered item, a contradiction, or items that could not be found never pass.
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
    list, then the run's acceptance criteria — deduplicated, each whole up to MAX_ITEM_CHARS, at
    most MAX_ITEMS (`items_dropped` says how many more there were). The existing-tests promise
    is left to the regression gate, which measures it."""
    return _items_and_dropped(spec)[0]


def items_dropped(spec: Mapping[str, Any]) -> int:
    return _items_and_dropped(spec)[1]


def _items_and_dropped(spec: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
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
    dropped = 0
    for source, item in raw:
        key = re.sub(r"\W+", " ", item.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        if len(items) >= MAX_ITEMS:
            dropped += 1
            continue
        items.append({"n": len(items) + 1, "text": item[:MAX_ITEM_CHARS], "source": source})
    return items, dropped


def lineage_root_row(connection: Any, root: str) -> Optional[dict]:
    """The lineage's ORIGINAL build: the earliest run of the lineage that is not a follow-up.
    Not simply the `root_run_id` row: a web-app build and a decomposed PRD mint a root id with
    no code_task_runs row of its own (start_web_app_build, prd_decomposition), and falling back
    to the follow-up's own "Fixed: [MAJOR] …" criteria is how a follow-up "met" its must-haves."""
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT id, spec FROM code_task_runs WHERE (id = :r OR finalize_ctx->>'root_run_id' = :r) "
        "AND COALESCE(finalize_ctx->>'is_followup', 'false') <> 'true' "
        "AND finalize_ctx->>'deferred_from_run' IS NULL "
        "ORDER BY created_at, id LIMIT 1"), {"r": root}).first()
    return {"id": row[0], "spec": _spec_of(row[1])} if row else None


def items_spec(connection: Any, *, run_id: str, fctx: Mapping[str, Any],
               own_spec: Mapping[str, Any]) -> tuple[dict, dict]:
    """(the spec whose items this run is held to, where they came from {run_id, root}).

    A FOLLOW-UP is held to its lineage's ORIGINAL build (`lineage_root_row`): its own spec is the
    findings it was sent to fix, and judged against those alone it "passes" while the must-haves
    the root missed stay missing (crun_15f0504ac9f64eaeb4a1). When that build cannot be found
    the follow-up has NO items (`root: False`, and the check is not_run) — never its own. Every
    other run (a repair child copies its parent's spec) is held to its own, which is the root's."""
    fctx = fctx or {}
    is_followup = bool(fctx.get("is_followup") or fctx.get("deferred_from_run"))
    if not is_followup:
        return _spec_of(own_spec), {"run_id": run_id, "root": True}
    root = str(fctx.get("root_run_id") or fctx.get("deferred_from_run") or "").strip()
    found = lineage_root_row(connection, root) if (root and connection is not None) else None
    if found and found["id"] != run_id and acceptance_items(found["spec"]):
        return found["spec"], {"run_id": found["id"], "root": True}
    return {}, {"run_id": None, "root": False,
                "why": "the lineage's original build (and so its must-haves) could not be found"}


# ---------------------------------------------------------------------------------------------
# The required stack (RCA-A replaces `required_stack` with the spec's structured field)
# ---------------------------------------------------------------------------------------------

def _language_key(word: str) -> Optional[str]:
    from . import stacks

    word = re.split(r"[\s/\-_.,;:()]+", str(word or "").strip().lower(), maxsplit=1)[0]
    key = stacks._normalise_language(word)  # noqa: SLF001
    return key if key in stacks.LANGUAGES else None


def required_stack(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stacks the spec names, each `hard` or not: [{stack, manifests, because, hard}].

    HARD (a missing manifest fails the gate): the spec's structured `required_stack` (RCA-A), or
    a `Stack:` line (stacks.declared_stack — the add-project start's and the architecture's).
    SOFT: languages the brief's words name (stacks.languages_named on the directive, with its
    negation and migration-source handling). Words are not a declaration ("React to alerts"),
    so a soft stack never fails the gate by itself: the reviewer is told and judges it, and the
    record carries a note. THE ONE PLACE RCA-A replaces with the structured field."""
    from . import stacks

    spec = _spec_of(spec)
    out: list[dict[str, Any]] = []

    def add(key: Optional[str], because: str, hard: bool) -> None:
        if not key or any(o["stack"] == key for o in out):
            return
        out.append({"stack": key, "manifests": list(stacks.LANGUAGES[key].manifests),
                    "because": because, "hard": hard})

    structured = spec.get("required_stack")
    if isinstance(structured, list) and structured:
        for s in structured:
            add(_language_key(s.get("language") if isinstance(s, Mapping) else s),
                "the spec's required_stack", True)
        return out
    instructions = str(spec.get("instructions") or "")
    for d in stacks.declared_stack(instructions) or []:
        add(_language_key(d.get("language")), f"Stack: line ({d.get('service')})", True)
    if out:
        return out
    directive = stacks.directive_text(instructions)
    first = directive.strip().splitlines()[0] if directive.strip() else ""
    words = "\n".join([first, *brief_must_haves(directive)])
    for key in stacks.languages_named(words):
        add(key, "named in the brief's words", False)
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
    """Each named stack and the manifest found for it (None = absent). `missing` holds only the
    HARD stacks with no manifest (they fail the gate); `notes` the soft ones (they do not)."""
    required = required_stack(spec)
    if not required or root is None:
        return {"required": required, "found": {}, "missing": [], "notes": []}
    wanted = {m for r in required for m in r["manifests"]}
    present: dict[str, str] = {}
    for p in _walk(Path(root)):
        if p.name in wanted and p.name not in present:
            present[p.name] = str(p.relative_to(root))
    found = {r["stack"]: next((present[m] for m in r["manifests"] if m in present), None)
             for r in required}
    return {"required": required, "found": found,
            "missing": [r["stack"] for r in required if r["hard"] and not found[r["stack"]]],
            "notes": [f"the brief's words name {r['stack']} but no {'/'.join(r['manifests'])} "
                      "is in the tree (not a declared stack, so the reviewer judges it)"
                      for r in required if not r["hard"] and not found[r["stack"]]]}


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

def _stack_lines(stack: Mapping[str, Any]) -> str:
    lines = []
    for r in stack.get("required") or []:
        path = (stack.get("found") or {}).get(r["stack"])
        state = f"manifest {path}" if path else "NO manifest found: " + "/".join(r["manifests"])
        kind = ("REQUIRED (declared)" if r["hard"]
                else "named in the brief's words (judge whether it is really required)")
        lines.append(f"- {r['stack']}: {kind}; {state}")
    return ("\n\nSTACK:\n" + "\n".join(lines)) if lines else ""


def acceptance_prompt(items: list[dict], *, title: str, stack: Mapping[str, Any],
                      tree: str) -> str:
    listed = "\n".join(f"ITEM {i['n']} [{i['source']}]: {i['text']}" for i in items)
    return (
        "You are BOSS's ACCEPTANCE REVIEWER for the build \"" + str(title or "") + "\". "
        "Your one question: does the code ON THIS BRANCH — the whole working tree in front of "
        "you, not only the latest change — deliver each item the person and the approved PRD "
        "asked for? Read the files. Judge what the code does, not what its README, comments or "
        "commit messages claim. A stub, a TODO, a hard-coded demo answer, a test that asserts "
        "nothing, or a feature present in a different language/stack than required is not MET. "
        "Do NOT edit any file.\n\n"
        "THE ITEMS:\n" + listed + _stack_lines(stack) + "\n\n"
        "Answer EVERY item, one line each, in this exact form:\n"
        "  ITEM <n>: MET — <the source file path(s) in this tree, and the function or test that "
        "delivers it>\n"
        "  ITEM <n>: PARTIAL — <what exists (files) and what is still missing>\n"
        "  ITEM <n>: MISSING — <what you looked for and where; what is absent>\n"
        "MET requires a path that exists in the tree and is code, configuration or a test — a "
        "README or other document alone is not evidence. When unsure, say PARTIAL and why.\n"
        "End with EXACTLY one final line:\n  ACCEPTANCE: MET\n  ACCEPTANCE: NOT MET\n\n"
        "THE BRANCH'S FILES:\n" + (tree or "(empty)") + "\n"
    )


_ANSWER = re.compile(
    r"(?im)^[\s>*\-•]*\**\s*ITEM\s*#?\s*(\d+)\s*\**\s*[:.)\-]\s*\**\s*(MET|PARTIAL|MISSING)\b\**"
    r"\s*(?:[—–:\-]+\s*)?(.*)$")
_SUMMARY = re.compile(r"(?im)^[\s>*`]*ACCEPTANCE\s*:\s*\**\s*(NOT\s+MET|MET)\b")
_PATHISH = re.compile(r"(?<![\w/.-])([\w.\-]+(?:/[\w.\-]+)+|[\w\-]+\.[A-Za-z][\w]{0,8})"
                      r"(?::\d+)?(?![\w/])")
_DOC_PATH = re.compile(r"(?i)(^|/)(readme[^/]*|changelog[^/]*|docs?/.*|[^/]*\.(md|rst|txt|adoc))$")


def _resolve(root: Path, cited: str) -> Optional[str]:
    """The tree path a cited path names (exact, else a unique suffix match), or None."""
    cited = cited.strip("`'\".,;:()[]").lstrip("./")
    if not cited or ".." in Path(cited).parts:
        return None
    direct = root / cited
    if direct.is_file():
        return cited
    hits = [p for p in _walk(root) if str(p.relative_to(root)).endswith("/" + cited)
            or (("/" not in cited) and p.name == cited)]
    return str(hits[0].relative_to(root)) if len(hits) == 1 else None


def evidence_paths(root: Any, why: str) -> tuple[list[str], list[str]]:
    """(cited paths that exist in the tree, cited paths that do not)."""
    if root is None:
        return [], []
    found, absent = [], []
    for m in _PATHISH.finditer(str(why or "")):
        cited = m.group(1)
        if re.fullmatch(r"\d+(\.\d+)*", cited):
            continue
        hit = _resolve(Path(root), cited)
        (found if hit else absent).append(hit or cited)
    return list(dict.fromkeys(found)), list(dict.fromkeys(absent))


def parse_acceptance(reply: str, items: list[dict], *, root: Any) -> list[dict[str, Any]]:
    """Each item with the reviewer's status and evidence (the LAST answer for an item wins).

    A MET stands only on evidence: at least one cited path that EXISTS in the tree and is not a
    document (README, *.md, docs/). Otherwise it reads as PARTIAL, saying why — an unevidenced
    or invented claim is what this gate exists to stop."""
    answers: dict[int, tuple[str, str]] = {}
    for m in _ANSWER.finditer(str(reply or "")):
        answers[int(m.group(1))] = (m.group(2).lower(), _norm(m.group(3))[:600])
    out = []
    for it in items:
        status, why = answers.get(int(it["n"]), (UNJUDGED, ""))
        if status == MET:
            found, absent = evidence_paths(root, why)
            code = [f for f in found if not _DOC_PATH.search(f)]
            if not code:
                reason = ("only documents cited" if found
                          else f"cited paths not in the tree: {', '.join(absent[:4])}" if absent
                          else "no file evidence cited")
                status, why = PARTIAL, f"{why} ({reason})".strip()
        out.append({**it, "status": status, "evidence": why})
    return out


def judge(items: list[dict], reply: Optional[str], stack: Mapping[str, Any], *,
          root: Any, source: Optional[Mapping[str, Any]] = None,
          dropped: int = 0) -> dict[str, Any]:
    """The gate's record. It FAILS CLOSED; verdict:
      met       EVERY item MET with evidence in the tree, every declared manifest present, and
                the reviewer's own summary line says MET
      not_met   an item MISSING or PARTIAL, a declared stack's manifest absent, or the reviewer's
                summary contradicting its items (facts — repair, then a checks-failing PR)
      not_run   the reviewer left items unanswered, or the items could not be found (the
                check's fault, not the code's: it is re-run, and never passes)
      no_items  the run states nothing to hold it to (a non-follow-up with no criteria)"""
    stack = dict(stack or {})
    source = dict(source or {"root": True})
    judged = parse_acceptance(reply or "", items, root=root) if items else []
    m = list(_SUMMARY.finditer(str(reply or "")))
    summary = (" ".join(m[-1].group(1).upper().split()) if m else None)
    missing = [i["n"] for i in judged if i["status"] == MISSING]
    partial = [i["n"] for i in judged if i["status"] == PARTIAL]
    unjudged = [i["n"] for i in judged if i["status"] == UNJUDGED]
    met = [i["n"] for i in judged if i["status"] == MET]
    why = ""
    if stack.get("missing") or missing or partial:
        verdict = "not_met"
    elif not source.get("root"):
        verdict, why = "not_run", str(source.get("why") or "the must-haves were not found")
    elif not items:
        verdict = "no_items"
    elif unjudged:
        verdict, why = "not_run", f"{len(unjudged)} of {len(items)} items were not answered"
    elif summary != "MET":
        verdict = "not_met"
        why = ("the reviewer's summary says NOT MET although every item reads met"
               if summary == "NOT MET" else "the reviewer gave no ACCEPTANCE summary line")
    else:
        verdict = "met"
    return {"verdict": verdict, "why": why, "summary_line": summary, "items": judged,
            "missing": missing, "partial": partial, "unjudged": unjudged, "met": met,
            "stack": stack, "source": source, "items_dropped": int(dropped or 0),
            "counts": {s: sum(1 for i in judged if i["status"] == s)
                       for s in (MET, PARTIAL, MISSING, UNJUDGED)}}


def passed(record: Optional[Mapping[str, Any]]) -> bool:
    return (record or {}).get("verdict") in ("met", "no_items")


def findings_summary(record: Optional[Mapping[str, Any]]) -> str:
    """The gate's findings as FINDING lines (review_comments.parse_findings reads them), so the
    checks-failing PR, its card and its follow-up carry each unmet item verbatim."""
    record = record or {}
    lines = []
    stack = record.get("stack") or {}
    for name in stack.get("missing") or []:
        req = next((r for r in stack.get("required") or [] if r.get("stack") == name), {})
        lines.append(f"FINDING: MAJOR — Required stack '{name}' has no manifest in the tree "
                     f"(expected {' or '.join(req.get('manifests') or [])}; declared by "
                     f"{req.get('because', 'the spec')}).")
    for status, word in ((MISSING, "MISSING"), (PARTIAL, "only PARTIAL"),
                         (UNJUDGED, "not judged")):
        for i in record.get("items") or []:
            if i.get("status") == status:
                lines.append(f"FINDING: MAJOR — Must-have {i['n']} {word}: {i['text']}"
                             + (f" — {i['evidence']}" if i.get("evidence") else ""))
    if record.get("why"):
        lines.append(f"FINDING: MAJOR — Acceptance check: {record['why']}.")
    for note in stack.get("notes") or []:
        lines.append(f"Note: {note}.")
    if record.get("items_dropped"):
        lines.append(f"Note: {record['items_dropped']} further items beyond the first "
                     f"{MAX_ITEMS} were not checked.")
    return "\n".join(lines)


_STATUS_WORD = {MET: "met", PARTIAL: "PARTIAL", MISSING: "MISSING", UNJUDGED: "NOT JUDGED"}


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
        status = "met" if path else ("MISSING" if r.get("hard") else "not declared, not found")
        rows.append(f"| stack | {r['stack']} manifest ({' or '.join(r['manifests'])}) "
                    f"| {status} | {cell(path or 'not in the tree')} |")
    if record.get("items_dropped"):
        rows.append(f"\n{record['items_dropped']} further items beyond the first {MAX_ITEMS} "
                    "were not checked.")
    return "\n".join(rows)


def headline(record: Optional[Mapping[str, Any]]) -> str:
    record = record or {}
    c = record.get("counts") or {}
    if record.get("verdict") == "not_run" and not record.get("items"):
        return f"Must-haves: not checked ({record.get('why') or 'no answer'})"
    if not record.get("items"):
        return ""
    bits = [f"{c.get(MET, 0)} met", f"{c.get(PARTIAL, 0)} partial", f"{c.get(MISSING, 0)} missing"]
    if c.get(UNJUDGED):
        bits.append(f"{c[UNJUDGED]} not judged")
    if (record.get("stack") or {}).get("missing"):
        bits.append("required stack missing: " + ", ".join(record["stack"]["missing"]))
    if record.get("items_dropped"):
        bits.append(f"{record['items_dropped']} not checked (over {MAX_ITEMS})")
    return "Must-haves: " + ", ".join(bits)


def pr_section(record: Optional[Mapping[str, Any]]) -> str:
    table = must_have_table(record)
    if not table:
        return ""
    return f"## Must-haves (acceptance check)\n\n{headline(record)}.\n\n{table}\n\n"


# ---------------------------------------------------------------------------------------------
# The lineage, for the merge card
# ---------------------------------------------------------------------------------------------

#: Gates a later build judges on the WHOLE tree, so its pass clears an earlier failure.
#: Judgment seats (review, security, consistency) re-judge only a follow-up's own delta and do
#: not; `acceptance_met` clears only by a check against the ROOT's items (see lineage_failures).
WHOLE_TREE_GATES = frozenset({"nothing_broke", "app_serves", "existing_tests_intact"})


def _gates_shipped_past(fctx: Mapping[str, Any]) -> list[str]:
    fctx = fctx or {}
    out: list[str] = []
    out += [str(g) for g in fctx.get("advisory_failing_gates") or []]
    out += [str(g) for g in (fctx.get("unresolved_dissent") or {}).get("unresolved_gates") or []]
    out += [str(g.get("gate")) for g in (fctx.get("checks_failing") or {}).get("gates") or []
            if isinstance(g, Mapping) and g.get("gate")]
    return [g for g in dict.fromkeys(out) if g]


def _gates_passed(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return set()
    return {str(g) for g in value or []} if isinstance(value, list) else set()


def lineage_failures(connection: Any, *, run_id: str, pr_url: Optional[str] = None,
                     current_acceptance: Optional[Mapping[str, Any]] = None,
                     current_failing: Iterable[str] = ()) -> dict[str, Any]:
    """The gates THIS pull request's builds shipped past, across the lineage (root_run_id):
    {"gates": {gate: [run ids]}, "acceptance": the latest acceptance record against the root's
    items (None: never checked)}.

    Scoped to the pull request (`pr_url`; the current run is always in): an earlier, merged PR
    of the same lineage is not this one. A run that went on to a repair child never shipped and
    is not counted. A WHOLE_TREE_GATES failure clears when a later build of the PR passed that
    gate; `acceptance_met` clears only when the latest acceptance check AGAINST THE ROOT'S ITEMS
    met them; a judgment seat's failure never clears (a follow-up re-judges its own delta)."""
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT project_id, COALESCE(finalize_ctx->>'root_run_id', id) FROM code_task_runs "
        "WHERE id = :id"), {"id": run_id}).first()
    gates: dict[str, list[str]] = {}
    for g in current_failing or ():
        gates.setdefault(str(g), []).append(run_id)
    cur = dict(current_acceptance) if current_acceptance else None
    cur_ok = bool(cur and cur.get("verdict") in ("met", "not_met")
                  and (cur.get("source") or {"root": True}).get("root"))
    if row is None:
        acc = cur if cur_ok else None
        if acc and acc.get("verdict") == "not_met":
            gates.setdefault(GATE, [run_id])
        return {"gates": gates, "acceptance": acc}
    project_id, root = row[0], row[1]
    rows = connection.execute(text(
        "SELECT r.id, r.status, r.pull_request_url, r.finalize_ctx, r.state->'acceptance' AS acc, "
        "q.gates_passed FROM code_task_runs r LEFT JOIN quality_gates q "
        "ON q.code_task_run_id = r.id AND q.project_id = r.project_id "
        "WHERE r.project_id = :p AND (r.id = :root OR r.finalize_ctx->>'root_run_id' = :root) "
        "ORDER BY r.created_at, r.id"),
        {"p": project_id, "root": root}).mappings().all()
    acc_seen, acc_run = None, None
    passed_after: dict[str, int] = {}
    failed_at: dict[str, int] = {}
    for idx, r in enumerate(rows):
        this = r["id"] == run_id
        if not this and pr_url and r["pull_request_url"] != pr_url:
            continue
        fctx = _spec_of(r["finalize_ctx"])
        shipped = this or r["pull_request_url"] or r["status"] == "completed"
        if shipped and r["status"] not in ("repairing", "cancelled", "failed"):
            for g in _gates_shipped_past(fctx):
                runs = gates.setdefault(g, [])
                if r["id"] not in runs:
                    runs.append(r["id"])
                failed_at[g] = idx
            for g in _gates_passed(r["gates_passed"]) & WHOLE_TREE_GATES:
                passed_after[g] = idx
        acc = _spec_of(r["acc"])
        if (acc.get("verdict") in ("met", "not_met") and not this
                and (acc.get("source") or {"root": True}).get("root")):
            acc_seen, acc_run = acc, r["id"]
    for g in list(gates):
        if g in WHOLE_TREE_GATES and passed_after.get(g, -1) > failed_at.get(g, 10 ** 9):
            gates.pop(g)
    latest, latest_run = (cur, run_id) if cur_ok else (acc_seen, acc_run)
    if latest and latest.get("verdict") == "met":
        gates.pop(GATE, None)
    elif latest and latest.get("verdict") == "not_met":
        runs = gates.setdefault(GATE, [])
        if latest_run and latest_run not in runs:
            runs.append(latest_run)
    return {"gates": gates, "acceptance": latest}


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
                      checks_failing: Optional[dict], pr_url: Optional[str] = None
                      ) -> tuple[Optional[dict], Optional[dict]]:
    """(the checks-failing record the merge card carries, the acceptance record it shows).

    The run's own checks-failing record when it has one; else the pull request's lineage's
    (`lineage_failures`) — so a follow-up whose own checks passed still shows the gates its PR's
    earlier builds shipped past. The acceptance record is None when the must-haves were never
    checked against the root's items: the card then says so, never "passed". Never raises."""
    import logging

    acceptance = evidence.get(KEY) if isinstance(evidence, Mapping) else None
    card = checks_failing
    if not run_id:
        return card, acceptance
    try:
        from . import checks_failing as cf

        with connection.begin_nested():
            lineage = lineage_failures(connection, run_id=run_id, pr_url=pr_url,
                                       current_acceptance=acceptance,
                                       current_failing=cf.gate_names(checks_failing))
        acceptance = lineage.get("acceptance") or (
            acceptance if isinstance(acceptance, Mapping) else None)
        if not checks_failing:
            card = card_checks_failing(lineage, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — the card still opens; its own checks still show
        # Never the exception text: a driver error can carry the connection URL.
        logging.getLogger(__name__).warning("lineage checks not read for %s (%s)", run_id,
                                            type(exc).__name__)
    return card, acceptance


def card_words(card: Optional[Mapping[str, Any]], acceptance: Optional[Mapping[str, Any]],
               validation_status: str) -> str:
    """The card's "Automated checks" sentence. Never "passed" while a gate of the PR failed or
    the must-haves were not checked, met in full, against the root's items."""
    from . import checks_failing as cf

    if card:
        return f"Automated checks: failing ({cf.headline(card)}); CI {validation_status}"
    if not acceptance or acceptance.get("verdict") not in ("met", "no_items"):
        why = headline(acceptance) if acceptance else "Must-haves: not checked"
        return f"Automated checks: CI {validation_status}; {why}"
    return f"Automated checks: {validation_status}"


__all__ = [
    "card_words", "evidence_paths", "items_dropped", "lineage_root_row", "merge_card_checks",
    "GATE", "SEAT", "KEY", "SUMMARY_KEY", "acceptance_items", "acceptance_prompt",
    "brief_must_haves", "card_checks_failing", "findings_summary", "headline", "items_spec",
    "judge", "lineage_failures", "must_have_table", "parse_acceptance",
    "passed", "pr_section", "prd_gating_criteria", "required_stack", "stack_manifest_check",
    "tree_listing",
]
