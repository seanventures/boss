"""Parsing a reviewer's verdict — the one grammar, in one place, measured by one eval.

These were private helpers inside the governed pipeline, which meant the words that decide
whether a change ships were scattered next to ten thousand lines of pipeline and could not be
measured without importing all of it. They are pure text functions; they now live here so
`evals/task_rigor` (family `verdict`) can score them the way it scores the other decision rules,
and the floor in thresholds.json fails CI when a change makes them worse.

`plan_verdict` replaces the one genuinely keyword-shaped parse: `plan_ok` was computed as
`"REVISE" not in summary.upper()`, so a reviewer writing "no need to revise anything" — or
hedging "I considered PLAN: REVISE for section 2, but it is sound" — failed the plan on every
run measured (the pipeline's own comment: plan_ok was false on EVERY run, including ones that
shipped clean). The prompt orders the reviewer to END with `PLAN: OK` or `PLAN: REVISE`; the
parse now reads that marker, last one wins, and answers `not_run` when the reviewer never wrote
one — refused, not coerced, like every other verdict here.
"""
from __future__ import annotations

import re
from typing import Optional

#: The plan reviewer's closing marker. Anchored to a line start so prose ABOUT the marker
#: ("…I would say PLAN: REVISE…" mid-sentence) does not decide; the LAST marker wins, because
#: the prompt asks for it as the final line and reviewers who restate it correct themselves.
_PLAN = re.compile(r"^\s*(?:#+\s*|\*+\s*)?PLAN:\s*(OK|REVISE)\b", re.IGNORECASE | re.MULTILINE)


def plan_verdict(summary: str) -> str:
    """'ok' | 'revise' | 'not_run' from a plan review's words."""
    found = _PLAN.findall(summary or "")
    if not found:
        return "not_run"
    return "ok" if found[-1].upper() == "OK" else "revise"


def parse_verdict(summary: str) -> str:
    """'survived' | 'refuted' | 'not_run' from an adversarial review's words."""
    up = (summary or "").upper()
    m = re.findall(r"VERDICT:\s*(SURVIVED|REFUTED)", up)
    if not m:  # the model sometimes omits the "VERDICT:" prefix — accept the keyword anywhere
        m = re.findall(r"\b(SURVIVED|REFUTED)\b", up)
    if not m:
        return "not_run"
    verdict = "survived" if m[-1] == "SURVIVED" else "refuted"
    if verdict == "refuted" and not refutation_is_substantiated(summary):
        # A refutation MUST cite a CONCRETE defect (the reviewer's own instruction). A bare
        # "VERDICT: REFUTED" with no articulated defect must NOT autonomously fail-close the gate.
        return "survived"
    return verdict


def refutation_is_substantiated(summary: str) -> bool:
    """True only if the reviewer articulated an actual defect, not just the verdict token."""
    body = re.sub(r"(?im)^\s*(?:#+\s*)?(?:review findings|verdict)\b.*$", "", summary or "")
    body = re.sub(r"(?im)\bVERDICT:\s*(?:SURVIVED|REFUTED)\b", "", body)
    return len(body.strip()) >= 40


def parse_security(summary: str) -> str:
    """'clean' | 'issues' | 'not_run' from a security review's words."""
    m = re.findall(r"VERDICT:\s*(CLEAN|ISSUES)", (summary or "").upper())
    if not m:
        return "not_run"
    return "clean" if m[-1] == "CLEAN" else "issues"


def parse_consistency(summary: str) -> str:
    """'consistent' | 'drift' | 'not_run' from a consistency review's words."""
    m = re.findall(r"CONSISTENT:\s*(YES|NO)", (summary or "").upper())
    if not m:
        return "not_run"
    return "consistent" if m[-1] == "YES" else "drift"


def aggregate_panel(verdicts: "list[str]", quorum: float) -> "tuple[str, float]":
    """Combine an N-member refute panel into one verdict + a confidence score (#9).

    Each element of ``verdicts`` is 'survived' | 'refuted' | 'not_run' (only the first two
    count as a cast vote). ``confidence`` is the fraction of *cast* votes that SURVIVED —
    i.e. how many independent adversarial reviewers could NOT refute the change. The panel
    SURVIVES only if confidence >= ``quorum`` (fail-closed: a tie below quorum, or no votes
    at all, refutes). At panel_size 1 with quorum 0.5 this is exactly the pre-#9 behavior:
    one 'survived' -> (survived, 1.0); one 'refuted' -> (refuted, 0.0).
    """
    cast = [v for v in verdicts if v in ("survived", "refuted")]
    if not cast:
        return "not_run", 0.0
    survived = sum(1 for v in cast if v == "survived")
    confidence = survived / len(cast)
    return ("survived" if confidence >= quorum else "refuted"), confidence
