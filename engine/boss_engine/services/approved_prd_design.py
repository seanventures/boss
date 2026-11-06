"""AN APPROVED PRD IS THE BUILD'S DESIGN.

THE CASE. HelpDesk AI's SLA-timers build (workflow wfr_c8dbccd76a92488c99dc, build
crun_c8e2a5f8ed5246308016) came from an agent panel whose PRD the owner approved (decision
dec_13a8cd75a6404e539289). That PRD already carried a "7. Proposed Solution" section — the
architecture, the shared helper, the layout sketches, the chip states — and the build then spent
572 s (9.5 min) in its design phase re-deriving standards, a spec and a plan for what a person had
just signed off, followed by a fresh-context plan review of that re-derivation. Of a 28-minute
build, a third was re-planning a decided plan.

So when the build's origin carries an APPROVED PRD with a real solution section, that section IS
the plan. What the PRD cannot know — this repository's conventions, which files the solution
lands in, how the change splits into parallel streams, and whether the code contradicts what the
PRD assumes — comes from ONE short, turn-bounded, read-only pass. Every field the full design
produces (standards, spec, plan, plan_review, plan_ok, packages) is still produced, so write_tests,
implement, the stream split, the consistency review and the database detection read exactly what
they read today.

QUALITY FALLS BACK, IT NEVER GUESSES. No approved PRD, a PRD without a solution section, a short
pass that does not answer every section, or a pass that finds the repository contradicting the
PRD: the normal design phase runs, and the reason is recorded. Nothing here assumes an engine —
the pass is an ordinary run_role call with the plan stage's route.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import text as sql

from . import parallel_build as _parallel_build

logger = logging.getLogger(__name__)

#: The bounded pass's turn budget. The full joint design session is unbounded and measured at
#: several minutes; a handful of file reads to map a decided solution onto the repo is ~1-2 min.
PRD_DESIGN_MAX_TURNS = 10

#: A solution section shorter than this is a heading with a sentence under it, not a design.
MIN_SOLUTION_CHARS = 200

# The panel's PRD template names it "Proposed Solution" (meeting_service); measured on every
# approved PRD in the decisions table, sometimes with a suffix ("Proposed Solution — Architecture").
# The other names are what a hand-written PRD calls the same section.
_SOLUTION_HEAD = re.compile(
    r"^(#{1,4})[ \t]+(?:\d+(?:\.\d+)*[.)]?[ \t]*)?"
    r"(?:proposed[ \t]+solution|solution(?:[ \t]+design)?|technical[ \t]+(?:design|approach|plan)"
    r"|implementation[ \t]+plan)\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE)
_ANY_HEAD = re.compile(r"^(#{1,6})[ \t]+\S", re.MULTILINE)

_HEADS = (("standards", "STANDARDS"), ("plan", "PLAN"), ("contradictions", "CONTRADICTIONS"),
          ("packages", _parallel_build.PACKAGES_HEAD))


def solution_section(markdown: str) -> str:
    """The PRD's solution section (heading included, sub-sections kept), or "" when it has none
    worth the name."""
    text = str(markdown or "")
    m = _SOLUTION_HEAD.search(text)
    if not m:
        return ""
    level = len(m.group(1))
    stop = len(text)
    for nxt in _ANY_HEAD.finditer(text, m.end()):
        if len(nxt.group(1)) <= level:
            stop = nxt.start()
            break
    section = text[m.start():stop].strip()
    body = section[len(m.group(0)):].strip()
    return section if len(body) >= MIN_SOLUTION_CHARS else ""


def _decision_ids(connection, fctx: dict) -> list[str]:
    """Where the approved PRD's decision is named. Two real paths reach a build from a panel:
      - decisions._handle_approved (approve the PRD → build): origin.decision_id;
      - a workflow (Standard Product Delivery…): the build step's origin.step_run_id, whose run
        has a SUCCEEDED meeting step that awaited the `spec_decision` — a step succeeds only
        once that decision is approved."""
    origin = fctx.get("origin") if isinstance(fctx.get("origin"), dict) else {}
    ids: list[str] = []
    if origin.get("decision_id"):
        ids.append(str(origin["decision_id"]))
    wfr = str(fctx.get("workflow_run_id") or "").strip()
    if not wfr and origin.get("step_run_id"):
        wfr = str(connection.execute(sql(
            "SELECT workflow_run_id FROM workflow_step_runs WHERE id = :s"),
            {"s": str(origin["step_run_id"])}).scalar() or "")
    if wfr:
        rows = connection.execute(sql(
            "SELECT COALESCE(output->'await'->>'id', decision_id) FROM workflow_step_runs "
            "WHERE workflow_run_id = :w AND status = 'succeeded' "
            "AND output->'await'->>'kind' = 'spec_decision' "
            "ORDER BY completed_at DESC NULLS LAST, created_at DESC"), {"w": wfr}).fetchall()
        ids.extend(str(r[0]) for r in rows if r[0])
    return list(dict.fromkeys(ids))


def find_approved_prd(connection, project_id: str, fctx: dict) -> Optional[dict]:
    """{"decision_id", "markdown"} for the APPROVED PRD this build came from, or None. Never
    raises: a lookup that fails means the full design runs."""
    try:
        for decision_id in _decision_ids(connection, fctx or {}):
            row = connection.execute(sql(
                "SELECT recommendation->'args'->>'markdown' FROM decisions "
                "WHERE id = :d AND project_id = :p AND status = 'approved'"),
                {"d": decision_id, "p": project_id}).first()
            markdown = str((row[0] if row else "") or "").strip()
            if markdown:
                return {"decision_id": decision_id, "markdown": markdown}
    except Exception:  # noqa: BLE001 — no PRD found is the normal design, never a failure
        logger.warning("approved PRD lookup failed", exc_info=True)
    return None


def prompt(ctx_block: str, solution: str) -> str:
    """The short pass: map a DECIDED solution onto this repository, do not redesign it."""
    return (
        ctx_block
        + "A person has APPROVED the PRD in the brief above. Its solution section, repeated "
        "below, IS the design — do not redesign it, do not re-specify it. Your only job is to "
        "map it onto THIS repository. Read only what you need (a few files: the ones the "
        "solution names or plainly touches); do NOT edit anything. Answer with these four "
        "sections, each starting with its heading line exactly as shown, and nothing else.\n\n"
        "=== STANDARDS ===\nA short bulleted list of the conventions this change must follow "
        "here: style, naming, error handling, test layout and the test command.\n\n"
        "=== PLAN ===\nThe files to add or change for this solution (real paths in this repo), "
        "the function signatures, and the edge cases the PRD names. End with the line "
        "`database: postgres` if the app needs a relational database, otherwise "
        "`database: none`.\n\n"
        "=== CONTRADICTIONS ===\nThe single word NONE when the repository fits the solution. "
        "Otherwise, one line per thing the solution assumes that the code contradicts (a "
        "model, route, framework or file it names that is absent or different)."
        + _parallel_build.design_prompt_section()
        + f"\n\nTHE APPROVED SOLUTION:\n{solution}"
    )


def split(text: str) -> dict:
    """The pass's sections; a missing one is "" (a missing verdict falls back unless the plan
    maps the solution — see design_from_answer)."""
    out = {key: "" for key, _ in _HEADS}
    marks = []
    for key, head in _HEADS:
        m = re.search(rf"^\s*=+\s*{re.escape(head)}\s*=+\s*$", text or "", re.MULTILINE)
        if m:
            marks.append((m.start(), m.end(), key))
    marks.sort()
    for i, (_start, end, key) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out[key] = text[end:stop].strip()
    return out


#: The PLAN's closing line, as the prompt asks for it — its presence means the plan was not cut off.
_PLAN_END = re.compile(r"^\W*database:\s*(?:postgres|none)\b", re.IGNORECASE | re.MULTILINE)
#: A repository path: a token with a directory separator or a file extension.
_PLAN_PATH = re.compile(r"[\w.-]+/[\w./-]*\w|\b[\w-]+\.[A-Za-z][A-Za-z0-9]{0,5}\b")


def plan_maps_solution(plan: str) -> bool:
    """True when the short pass's PLAN parses (it reached its closing `database:` line) and maps
    the solution onto this repository (it names at least one file)."""
    plan = str(plan or "")
    return bool(_PLAN_END.search(plan) and _PLAN_PATH.search(_PLAN_END.sub("", plan)))


def design_from_answer(prd: dict, solution: str, answer: str) -> tuple[Optional[dict], str]:
    """(design, "") when the short pass confirms the PRD fits the repo, else (None, why)."""
    parts = split(answer)
    verdict = parts["contradictions"].strip()
    if not verdict:
        # crun_719a19f0b9c74bae8bfd: the pass mapped the solution file by file and simply left
        # the CONTRADICTIONS section out — declining cost a 7.3 min full design. A plan that is
        # complete (ends on its `database:` line) and names files is the verdict: nothing it
        # found contradicted the PRD. A plan cut short or naming no file still falls back.
        if not plan_maps_solution(parts["plan"]):
            return None, "the short pass gave no contradictions verdict"
        verdict = "NONE"
    if not re.match(r"^\W*none\b", verdict, re.IGNORECASE):
        return None, f"the repository contradicts the PRD: {verdict[:300]}"
    if not parts["plan"].strip():
        return None, "the short pass gave no file plan"
    decision_id = prd["decision_id"]
    design = {
        "standards": parts["standards"],
        # The approved PRD is the agreed spec (consistency review reads `spec`).
        "spec": prd["markdown"],
        "plan": (f"THE APPROVED PRD'S SOLUTION IS THE PLAN (decision {decision_id}).\n\n"
                 f"{solution}\n\nWHERE IT LANDS IN THIS REPOSITORY:\n{parts['plan']}"),
        # No fresh-context review of a plan a person already approved; the code review panel
        # still judges the result against the PRD.
        "plan_review": (f"PLAN: OK — the approved PRD (decision {decision_id}) is the plan; "
                        "the repository was checked against it and contradicts nothing."),
        "plan_ok": True,
        "packages": parts["packages"],
        "source": "approved_prd",
        "prd_decision_id": decision_id,
    }
    return design, ""


def brief_addendum(instructions: str, design: dict) -> str:
    """What write_tests/implement get appended: the plan, and the PRD only when the brief does not
    already carry it (the workflow and approve paths both put it there — 10k chars twice is
    noise, not context)."""
    prd = str(design.get("spec") or "")
    probe = prd[:400].strip()
    spec_line = ("the approved PRD above" if probe and probe in (instructions or "") else prd)
    return f"\n\nREFINED SPEC:\n{spec_line}\n\nTECHNICAL PLAN:\n{design.get('plan', '')}"

