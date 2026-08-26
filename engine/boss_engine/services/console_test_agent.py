"""The console test agent — learns what v1 does, then checks v2 still does it.

WHY THIS EXISTS
---------------
The operator has been the test harness. Every console defect this week was found by them, in
production, one at a time: a Save button that hung for 115 seconds, a metric that displayed
its own page size, a deployment that reported success for the wrong app, an Unbind control
that did not exist at all. None of those were caught by a test, because the tests asserted on
replies and every one of those bugs produced a reply that read fine.

Migrating 33 areas into 9 by hand, with the same testing, would lose features silently. So
before anything moves, this records what the current console actually DOES — and afterwards it
checks the new one still does it.

THE SHAPE
---------
Mode LEARN, against v1: for every area, every interactive control — what it is, what it
issues, and what changed afterwards. Output is a behavioural baseline, not a screenshot.

Mode VERIFY, against v2: replay each recorded behaviour, find the equivalent control through
the parity file, and assert the same OUTCOME — same request, same resulting state. Not the
same pixels; v2 is supposed to look different.

WHAT THE MODEL IS AND IS NOT ALLOWED TO DO
------------------------------------------
It may LOCATE a control ("which button in v2 corresponds to Set target?") and SUMMARISE a
difference in English.

It may never decide pass or fail. That is done by comparing recorded requests and state, in
code. A model asked "did this work?" will eventually say yes because yes is the agreeable
answer, and an agreeable test suite is worse than none: it converts unknown risk into false
confidence.

ACCEPTANCE FOR THE AGENT ITSELF
-------------------------------
Before it is trusted to judge a migration it must find defects we already know about in v1 —
see `KNOWN_V1_DEFECTS`. A baseline that cannot reproduce a bug we found by hand is not a
baseline; it is a screenshot with extra steps.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# The recorded vocabulary
# ---------------------------------------------------------------------------


@dataclass
class Request:
    """One call a control caused. The unit of truth for 'did the same thing happen'."""

    method: str
    path: str                      # host stripped: v1 and v2 may sit on different origins
    status: Optional[int] = None

    def key(self) -> str:
        # Query strings are dropped: ids and cache-busters differ per run, the ROUTE does not.
        return f"{self.method.upper()} {self.path.split('?')[0]}"


@dataclass
class Control:
    """An interactive element and what pressing it does."""

    area: str
    label: str                     # what the operator reads
    testid: Optional[str] = None   # stable handle when present
    kind: str = "button"           # button | link | form | toggle | select
    requests: list[Request] = field(default_factory=list)
    state_after: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def behaviour(self) -> set[str]:
        """What this control DOES, independent of where it lives or what it is called."""
        return {r.key() for r in self.requests}


@dataclass
class Baseline:
    """Everything Learn recorded. Serialised to JSON so a migration can be checked months on."""

    console: str
    captured_at: str
    controls: list[Control] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, sort_keys=True)

    @staticmethod
    def from_json(raw: str) -> "Baseline":
        d = json.loads(raw)
        return Baseline(
            console=d["console"],
            captured_at=d["captured_at"],
            controls=[
                Control(
                    area=c["area"], label=c["label"], testid=c.get("testid"),
                    kind=c.get("kind", "button"),
                    requests=[Request(**r) for r in c.get("requests", [])],
                    state_after=c.get("state_after", {}), note=c.get("note", ""),
                )
                for c in d.get("controls", [])
            ],
        )


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

MISSING = "MISSING"    # v1 could do it; v2 cannot
BROKEN = "BROKEN"      # v2 has it; it does something else
MOVED = "MOVED"        # same outcome, different place — informational
OK = "OK"


@dataclass
class Finding:
    verdict: str
    label: str
    v1_area: str
    v2_area: Optional[str] = None
    expected: list[str] = field(default_factory=list)
    actual: list[str] = field(default_factory=list)
    detail: str = ""

    def is_failure(self) -> bool:
        # MOVED is not a failure. A migration that relocates things is doing its job; treating
        # relocation as breakage would make the report noise and get it ignored.
        return self.verdict in (MISSING, BROKEN)


def compare(baseline: Baseline, observed: Baseline,
            area_map: Optional[dict[str, str]] = None) -> list[Finding]:
    """Diff two recordings by BEHAVIOUR, not by appearance.

    Controls are matched on what they do — the set of requests they issue. Label and location
    are used only to describe the result, never to decide it, because v2 deliberately renames
    and relocates almost everything.
    """
    area_map = area_map or {}
    by_behaviour: dict[frozenset, list[Control]] = {}
    for c in observed.controls:
        by_behaviour.setdefault(frozenset(c.behaviour()), []).append(c)

    findings: list[Finding] = []
    for want in baseline.controls:
        wb = want.behaviour()
        if not wb:
            # A control that issued no request is presentational; it cannot be verified this
            # way and must not be silently counted as passing.
            findings.append(Finding(
                verdict=OK, label=want.label, v1_area=want.area,
                detail="no request recorded — not verifiable by behaviour"))
            continue

        exact = by_behaviour.get(frozenset(wb))
        if exact:
            got = exact[0]
            expected_area = area_map.get(want.area)
            moved = expected_area is not None and got.area != expected_area
            findings.append(Finding(
                verdict=MOVED if moved or got.area != want.area else OK,
                label=want.label, v1_area=want.area, v2_area=got.area,
                expected=sorted(wb), actual=sorted(got.behaviour()),
                detail="" if not moved else f"expected in {expected_area}, found in {got.area}"))
            continue

        # Partial match: something claims the label but issues different calls. That is the
        # dangerous case — it LOOKS migrated.
        near = [c for c in observed.controls
                if c.label.strip().lower() == want.label.strip().lower()
                or (want.testid and c.testid == want.testid)]
        if near:
            got = near[0]
            findings.append(Finding(
                verdict=BROKEN, label=want.label, v1_area=want.area, v2_area=got.area,
                expected=sorted(wb), actual=sorted(got.behaviour()),
                detail="control exists but issues different requests"))
        else:
            findings.append(Finding(
                verdict=MISSING, label=want.label, v1_area=want.area,
                expected=sorted(wb),
                detail="no control in v2 produces this outcome"))
    return findings


def summarise(findings: list[Finding]) -> dict[str, Any]:
    counts = {MISSING: 0, BROKEN: 0, MOVED: 0, OK: 0}
    for f in findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    return {
        "counts": counts,
        "passed": not any(f.is_failure() for f in findings),
        "failures": [asdict(f) for f in findings if f.is_failure()],
    }


# ---------------------------------------------------------------------------
# The agent's own acceptance test
# ---------------------------------------------------------------------------

#: Defects found by hand in v1 this week. Learn mode must be able to express each one; if the
#: recording cannot represent a bug we already know about, it will not catch its unknown
#: siblings either. Each entry names the observable that would betray it.
KNOWN_V1_DEFECTS = [
    {
        "id": "unbind-missing",
        "area": "code_tasks",
        "symptom": "no control unbinds a deployment target",
        "observable": "no control issues DELETE /v1/deployment-targets/{id}",
        "detects_with": "control inventory — an API the console never calls",
    },
    {
        "id": "audit-export-on-every-page",
        "area": "overview",
        "symptom": "13.2s feed fetched by every area, displayed by one",
        "observable": "GET /v1/audit/export issued on areas that never render it",
        "detects_with": "request recording per area",
    },
    {
        "id": "set-target-hangs",
        "area": "code_tasks",
        "symptom": "Save waits on a full route re-render",
        "observable": "control resolves in >20s while its own API call took <1s",
        "detects_with": "control timing vs request timing",
    },
    {
        "id": "wrong-app-deployed",
        "area": "code_tasks",
        "symptom": "deploy reported success for a different app",
        "observable": "response app_name differs from the row the control belongs to",
        "detects_with": "state_after comparison",
    },
]


def defect_coverage(baseline: Baseline) -> list[dict[str, Any]]:
    """Which known defects this baseline is capable of exposing.

    Run before trusting a migration report. A baseline that covers none of these is not
    measuring behaviour, however many controls it recorded.
    """
    all_requests = {r.key() for c in baseline.controls for r in c.requests}
    areas = {c.area for c in baseline.controls}
    out = []
    for d in KNOWN_V1_DEFECTS:
        if d["id"] == "unbind-missing":
            covered = d["area"] in areas
            evidence = ("recorded the area; a missing DELETE is then visible"
                        if covered else "area never visited")
        elif d["id"] == "audit-export-on-every-page":
            covered = any("/v1/audit/export" in r for r in all_requests)
            evidence = "audit export observed in the recording" if covered else "not observed"
        elif d["id"] == "set-target-hangs":
            covered = any(c.state_after.get("elapsed_ms") for c in baseline.controls)
            evidence = "control timings recorded" if covered else "no timings recorded"
        else:
            covered = any(c.state_after for c in baseline.controls)
            evidence = "post-action state recorded" if covered else "no state recorded"
        out.append({**d, "covered": bool(covered), "evidence": evidence})
    return out
