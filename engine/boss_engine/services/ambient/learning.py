"""Continuous learning: the detectors that read what BOSS already knows and find what to do next
(BOSSAPP-911; design §14.1-§14.2).

WHAT A DETECTOR IS
------------------
The operator: agents should be "constantly monitoring, learning and adapting … perform more
recommendations based on the knowledge it has", reviewed by a person "for further roadmap items
instead [of the] user coming up with that list". A DETECTOR is one small, explainable pattern
over a store BOSS already writes — the code graph and its change links, incidents and their
postmortems, review findings, run scorecards (cost, phase timings, steers, retries), PRD
decisions. It is deterministic SQL plus arithmetic, never a model: the same rows give the same
findings, and every finding carries the EVIDENCE it rests on (refs BOSS can open: an incident,
a KG node, a build, a step run, a meeting, a commit) and the NUMBERS that tripped it (`facts`).

A detector returns FINDINGS; it writes nothing. `roadmap.run` turns findings into signals
(`signals.record_signal`, the one way in), groups the findings that are about the same subject
(a hot file that also caused incidents and has no tests is ONE recommendation with three
independent sources, design §14.2), scores and ranks them, and opens the best as roadmap items.

    hotspot           code churn × size × incidents on the same file          refactor
    incident_cause    the same root cause in ≥ 2 incidents                    hardening
    missing_tests     a frequently changed file no test file covers           test_gap
    review_repeat     the same review finding across ≥ 3 builds               process
    design_drift      code that moved under a PRD/ADR/rule decision           design_fix
    cost_spike        a workflow/task class whose recent cost jumped           cost
    flaky_test        a test that failed then passed on retry, repeatedly     test_gap
    slow_stage        a pipeline stage whose median time is far over target   process
    user_friction     where people steer, retry, cancel or override most      process

TUNING (design §14.4)
---------------------
A detector's THRESHOLDS scale with what people told it: every reject filed as "too small"
raises its `threshold_scale` (roadmap.detector_tuning), so it speaks up only for bigger
patterns. Its WEIGHT (acceptance rate) scales the score; under 15 % acceptance after 20 decided
items it is muted and reported. Both come from recorded feedback, never a constant edited by hand.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ._common import clean_text, now

KINDS = ("hardening", "test_gap", "refactor", "feature", "design_fix", "cost", "process")
SHAPES = ("ticket", "epic", "prd")
#: Evidence decays with a half-life per source kind (design §14.3: incidents 90 d, cost 30 d).
HALF_LIFE_DAYS = {"incident": 90, "code": 45, "review": 60, "design": 60, "cost": 30,
                  "test": 30, "stage": 30, "friction": 30}
#: How far back a detector looks.
WINDOW_DAYS = 90


@dataclass
class Finding:
    """One pattern a detector saw. `key` is its identity within the detector (what makes two runs
    see the SAME finding); `subject` groups findings of different detectors about one thing."""

    detector: str
    key: tuple[str, ...]
    subject: str
    title: str
    why: str
    impact: int                       # 0-100: incidents avoided × severity, hours, $ saved
    effort_units: int                 # 1-13: a rough size (a ticket is 1-3, an epic 5+)
    evidence: list[dict[str, Any]]    # [{kind, ref, note?, at?}] — refs the ledger can verify
    facts: dict[str, Any]
    evidence_at: Optional[datetime]   # the newest evidence
    severity: str = "medium"
    blast_radius: int = 0             # fan-in of what it touches (code graph), 0 = unknown
    users_affected: int = 0           # incidents' reach, when known

    def fingerprint_parts(self) -> tuple[str, ...]:
        return ("learn", self.detector, *self.key)

    def evidence_digest(self) -> str:
        """What makes this observation distinct from a re-read of the same evidence: a re-run
        that finds nothing new is a `duplicate` signal; one more incident is a `repeat`."""
        refs = sorted(f"{e['kind']}:{e['ref']}" for e in self.evidence)
        payload = json.dumps([refs, sorted(map(str, self.facts.items()))], default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Detector:
    key: str
    title: str             # ≤ 4 words: a node on the diagram
    kind: str              # the recommendation kind it proposes
    shape: str             # ticket | epic | prd, when accepted as proposed
    source: str            # the signal source it records under (signals.SOURCES)
    evidence_source: str   # which half-life its evidence decays with
    squad: tuple[str, ...]
    fn: Callable[..., list[Finding]] = field(compare=False, repr=False)


DETECTORS: dict[str, Detector] = {}


def detector(key: str, *, title: str, kind: str, shape: str, source: str,
             evidence_source: str, squad: Iterable[str]) -> Callable[[Callable], Callable]:
    """Register a detector `fn(connection, project_id, *, since, now, scale) -> [Finding]`.
    `scale` (≥ 1) multiplies its thresholds: what people's "too small" rejections taught it."""
    if kind not in KINDS or shape not in SHAPES or evidence_source not in HALF_LIFE_DAYS:
        raise ValueError(f"detector {key}: bad kind/shape/evidence source")

    def deco(fn: Callable[..., list[Finding]]) -> Callable[..., list[Finding]]:
        DETECTORS[key] = Detector(key, title, kind, shape, source, evidence_source,
                                  tuple(squad), fn)
        return fn
    return deco


def detect(connection: Connection, project_id: str, *, only: Optional[Iterable[str]] = None,
           skip: Iterable[str] = (), tuning: Optional[Mapping[str, float]] = None,
           at: Optional[datetime] = None) -> dict[str, dict[str, Any]]:
    """Run each detector once, each in its own savepoint (one broken store never stops the
    others). Returns {detector: {"findings": [Finding], "error": str | None}}."""
    from . import detectors  # noqa: F401 — registers the built-in detectors

    moment = at or now()
    since = moment - timedelta(days=WINDOW_DAYS)
    wanted = set(only) if only else None
    out: dict[str, dict[str, Any]] = {}
    for key, det in DETECTORS.items():
        if (wanted is not None and key not in wanted) or key in set(skip):
            continue
        scale = max(1.0, float((tuning or {}).get(key, 1.0)))
        try:
            with connection.begin_nested():
                found = det.fn(connection, project_id, since=since, now=moment, scale=scale)
            out[key] = {"findings": list(found), "error": None}
        except Exception as exc:  # noqa: BLE001 — reported per detector, never raised
            out[key] = {"findings": [], "error": f"{type(exc).__name__}: {exc}"[:400]}
    return out


# ── grouping: one recommendation per subject ────────────────────────────────────────────────
#
# THE CHIEF OF STAFF GROUPS SIGNALS INTO A RECOMMENDATION (design §14.2). Findings about the SAME
# code subject from different detectors (a hotspot, its incidents, its missing tests) are one
# piece of work with several independent sources — which is exactly what makes evidence strong.
# The finding with the most impact leads (its title, kind and shape); the others' evidence joins.
# Findings whose subject is not code (a cost spike on a workflow, a slow stage) stand alone.

GROUPABLE = frozenset({"hotspot", "incident_cause", "missing_tests", "design_drift",
                       "flaky_test"})


@dataclass
class Group:
    fingerprint: str
    lead: Finding
    members: list[Finding]

    @property
    def evidence(self) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for f in sorted(self.members, key=lambda m: -m.impact):
            for e in f.evidence:
                k = (e["kind"], e["ref"])
                if k not in seen:
                    seen.add(k)
                    out.append(e)
        return out

    @property
    def source_kinds(self) -> list[str]:
        return sorted({DETECTORS[m.detector].evidence_source for m in self.members})

    @property
    def evidence_at(self) -> Optional[datetime]:
        stamps = [m.evidence_at for m in self.members if m.evidence_at is not None]
        return max(stamps) if stamps else None


def group(findings: Mapping[str, Mapping[str, Any]], *, project_id: str) -> list[Group]:
    from ..core import stable_dedupe_key

    by_subject: dict[str, list[Finding]] = {}
    alone: list[Finding] = []
    for result in findings.values():
        for f in result["findings"]:
            if f.detector in GROUPABLE and f.subject.startswith("file:"):
                by_subject.setdefault(f.subject, []).append(f)
            else:
                alone.append(f)
    groups: list[Group] = []
    for subject, members in by_subject.items():
        lead = max(members, key=lambda m: (m.impact, m.detector))
        groups.append(Group(stable_dedupe_key("roadmap", project_id, subject), lead, members))
    for f in alone:
        groups.append(Group(stable_dedupe_key("roadmap", project_id, f.detector, *f.key), f,
                            [f]))
    return groups


# ── small helpers the detectors share ───────────────────────────────────────────────────────

def ev(kind: str, ref: Any, note: str = "", at: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": kind, "ref": str(ref)}
    if note:
        out["note"] = clean_text(note, 200)
    if at is not None:
        out["at"] = at.isoformat() if hasattr(at, "isoformat") else str(at)
    return out


def clamp(value: float, low: float = 0, high: float = 100) -> int:
    return int(max(low, min(high, round(value))))


def log_scale(value: float, full: float) -> float:
    """0..1: how close `value` is to `full`, on a log scale (10 of 100 is not 'a tenth')."""
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(full))


def table_exists(connection: Connection, name: str) -> bool:
    return bool(connection.execute(text("SELECT to_regclass(:t) IS NOT NULL"),
                                   {"t": name}).scalar())


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


__all__ = ["KINDS", "SHAPES", "HALF_LIFE_DAYS", "Finding", "Detector", "DETECTORS", "detector",
           "detect", "Group", "group", "ev", "clamp", "log_scale", "table_exists", "plural"]
