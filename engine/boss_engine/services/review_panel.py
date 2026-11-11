"""ONE review session sits the panel's seats over ONE diff (BOSSAPP-1000, audit 997 §7.3).

Why
---
The review panel ran review_code, security_check and consistency_check as three separate full
CLI sessions over the SAME inline change. Each paid its own repo onboarding and re-read the
same diff; measured over a month the three seats were $375.98 of $1,717 (the panel group,
with check_tests, 22.7% of all ledgered spend). The seats never talk to each other — they are
three judgements of one text — so one read can cast all three verdicts.

This module is the one place the merged session's prompt is assembled and its answer is split
back into the seats' own summaries. The VERDICT GRAMMAR IS UNCHANGED: each section ends with
its seat's existing marker (``VERDICT: SURVIVED|REFUTED``, ``VERDICT: CLEAN|ISSUES``,
``CONSISTENT: YES|NO``), parsed by the same `review_verdict` functions and measured by the same
eval family. Downstream — gates, findings, review rounds, PR comments, repair scoping — keeps
reading one summary per seat, so nothing after the session can tell the difference.

A section the session did not deliver is judged by that seat's OWN session, exactly as before
(the same shape the joint design session uses): a short answer never becomes a silently
skipped review. The caller decides when merging applies at all (panel of one, no fix lane);
a refute panel of k > 1 keeps its independent review sessions.

The self-fix pipeline's review seats (BOSSAPP-1002) batch through these same helpers, so the
two pipelines cannot drift apart on how a merged panel asks or parses.
"""
from __future__ import annotations

import re

#: The merged session's ledger stage: one llm_calls row that IS the panel, never booked under
#: a single seat's name (the test arbiter taught us what overloading `review_code` costs).
STAGE = "review_panel"

#: Section heading per seat, in panel order. The response mirrors them (`=== CODE REVIEW ===`).
SECTION_HEADS: dict[str, str] = {
    "review_code": "CODE REVIEW",
    "security_check": "SECURITY REVIEW",
    "consistency_check": "CONSISTENCY REVIEW",
}

#: Panel order: the order sections are asked for and read back.
SEATS: tuple[str, ...] = tuple(SECTION_HEADS)


def _head_re(head: str) -> re.Pattern:
    # Tolerates the decorations models add to a heading (##, **, trailing **).
    return re.compile(rf"^\s*(?:#+\s*|\*+\s*)?=+\s*{re.escape(head)}\s*=+(?:\s*\*+)?\s*$",
                      re.IGNORECASE | re.MULTILINE)


def combined_prompt(sections: "list[tuple[str, str]]", *, common: str = "") -> str:
    """One prompt that sits every seat in ``sections`` ([(seat, that seat's prompt)]).

    ``common`` is what every seat shares — the task context and the inlined change — included
    ONCE here instead of once per seat (the whole point: one session, one read). Each seat's
    prompt keeps its own wording and verdict contract; where it says to end *the response*
    with its verdict line, that line ends the seat's SECTION instead, and the preamble says so.
    """
    heads = [SECTION_HEADS.get(seat, seat.upper()) for seat, _ in sections]
    listed = "\n".join(f"  {i}. === {h} ===" for i, h in enumerate(heads, start=1))
    out = [
        "You sit EVERY seat of this change's review panel in one session: read the change "
        "once, then judge it once per seat below. Answer with one marked section per seat, "
        "each starting with its heading line EXACTLY as shown:\n" + listed + "\n"
        "Each seat's instructions follow. Where a seat's instructions say to end your "
        "response with a final verdict line, end THAT SEAT'S SECTION with it — every section "
        "must close with its own seat's verdict line. Judge each seat independently: a defect "
        "one seat finds does not decide another seat's verdict.\n",
        common,
    ]
    for i, ((seat, prompt), head) in enumerate(zip(sections, heads), start=1):
        out.append(f"SEAT {i} — {head} (answer it in your `=== {head} ===` section):\n\n{prompt}")
    return "\n\n".join(part for part in out if part and part.strip())


def split(text: str, seats: "list[str]") -> dict:
    """The merged answer, one summary per seat; a missing section is "" (its seat re-runs
    alone). Text before the first heading (the shared ``REVIEWED:`` line) is prepended to the
    FIRST section found, so a hollow check of a single section still sees what was read."""
    out = {seat: "" for seat in seats}
    marks = []
    first_any = None  # the first heading ANYWHERE: only text before it is shared preface
    for seat in seats:
        found = list(_head_re(SECTION_HEADS.get(seat, seat.upper())).finditer(text or ""))
        if found:  # the LAST heading wins, as the verdict parsers read markers
            m = found[-1]
            marks.append((m.start(), m.end(), seat))
            if first_any is None or found[0].start() < first_any:
                first_any = found[0].start()
    marks.sort()
    for i, (_start, end, seat) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text or "")
        out[seat] = (text or "")[end:stop].strip()
    if marks and first_any is not None:
        preface = (text or "")[:first_any].strip()
        first = marks[0][2]
        if preface and out[first]:
            out[first] = preface + "\n" + out[first]
    return out
