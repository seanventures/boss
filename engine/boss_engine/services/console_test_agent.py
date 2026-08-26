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
UNVERIFIABLE = "UNVERIFIABLE"   # issued no request; this method cannot speak to it either way

#: Request keys that identify nothing. A Server Action POST carries no operation name, so every
#: one of them looks alike to a behaviour diff.
_OPAQUE = frozenset({"POST (server-action)"})


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
            # NOT "OK". Counting an unchecked control as a pass is how a parity report comes
            # to say 111/111 while having actually tested 51 things.
            findings.append(Finding(
                verdict=UNVERIFIABLE, label=want.label, v1_area=want.area,
                detail="no request recorded — not verifiable by behaviour"))
            continue

        # An opaque behaviour cannot identify a control. Next serialises every Server Action to
        # a POST at the page's own URL, so eleven different v1 buttons all recorded the single
        # key "POST (server-action)" -- and a match on that would let ONE v2 action stand in for
        # all eleven. Saying so is the only honest verdict available.
        if wb <= _OPAQUE:
            findings.append(Finding(
                verdict=UNVERIFIABLE, label=want.label, v1_area=want.area,
                expected=sorted(wb),
                detail="only behaviour is an opaque server action — indistinguishable from "
                       "every other server action, so a match would prove nothing"))
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


def _area_name(area: str) -> str:
    """Last path segment, so "/v2/panels" and "panels" are the same screen."""
    return area.rstrip("/").rsplit("/", 1)[-1] or area


def crawl_gaps(observed: Baseline, area_map: dict[str, str]) -> list[str]:
    """v2 screens the parity map says must carry v1 features, which this crawl never visited.

    Without this, a verify run over a subset of v2 reports every feature belonging to an
    unvisited screen as MISSING. A smoke run against /v2/panels alone produced 29 MISSING and
    10 BROKEN -- a picture of catastrophic feature loss, entirely manufactured by not having
    looked at the other nine screens. The tool has to know the difference between "v2 does not
    do this" and "I did not check".
    """
    visited = {_area_name(c.area) for c in observed.controls}
    return sorted({dest for dest in set(area_map.values()) if dest not in visited})


def summarise(findings: list[Finding], gaps: Optional[list[str]] = None) -> dict[str, Any]:
    counts = {MISSING: 0, BROKEN: 0, MOVED: 0, OK: 0, UNVERIFIABLE: 0}
    for f in findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    checked = counts[OK] + counts[MOVED] + counts[MISSING] + counts[BROKEN]
    return {
        "counts": counts,
        # How much of the baseline this run could speak to at all. A caller that reports
        # "no failures" without this number is reporting the size of its blind spot as success.
        "coverage": {
            "checked": checked,
            "total": len(findings),
            "unverifiable": counts[UNVERIFIABLE],
        },
        # A run that did not visit every destination cannot conclude anything about the
        # features that live there, in either direction. Neither pass nor fail: inconclusive.
        "inconclusive": bool(gaps),
        "unvisited": list(gaps or []),
        "passed": (not gaps) and not any(f.is_failure() for f in findings),
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
        # FIXED: gated to the ops area. A baseline can no longer observe this, and that is the
        # point of fixing it. Without this marker the coverage report says "widen the areas",
        # which is advice to go looking for a bug that is not there.
        "fixed": "gated to the ops area — the feed is no longer fetched where it is not shown",
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
        fixed = d.get("fixed")
        out.append({
            **d,
            "covered": bool(covered),
            "evidence": evidence,
            # A defect that has been FIXED cannot be reproduced, and that is success rather
            # than a gap in the baseline. Conflating the two turns a green result into an
            # instruction to hunt something that is not there.
            "status": (
                "covered" if covered
                else "fixed" if fixed
                else "not-covered"
            ),
        })
    return out


# ---------------------------------------------------------------------------
# Telling a slow control apart from a stalled harness
# ---------------------------------------------------------------------------

#: How many times slower than its own siblings a reading must be before it is treated as a
#: measurement fault rather than a finding.
OUTLIER_FACTOR = 20


def slow_controls(baseline: Baseline, *, threshold_ms: int = 3_000) -> list[dict[str, Any]]:
    """Controls that took a long time, each labelled as a finding or as an outlier.

    THE FIRST BASELINE RECORDED A CONTROL AT 16.7 MINUTES.
    The same control, on six other areas, took 2,020-2,029ms — and issued no requests at all.
    Six identical readings and one enormous one is far better explained by the harness stalling
    than by the console hanging, and reporting it as a defect would have sent someone hunting a
    bug that was never there.

    So a reading is compared against the same LABEL elsewhere. A control with no sibling is
    reported plainly; one that is wildly out of line with its own siblings is reported as
    suspect, with the comparison shown, so the reader can judge rather than being told.

    This cannot distinguish the two cases by itself — nothing can, from one run. What it can do
    is refuse to present a measurement fault as a finding.
    """
    by_label: dict[str, list[int]] = {}
    for control in baseline.controls:
        elapsed = int(control.state_after.get("elapsed_ms") or 0)
        if elapsed:
            by_label.setdefault(control.label, []).append(elapsed)

    out: list[dict[str, Any]] = []
    for control in baseline.controls:
        elapsed = int(control.state_after.get("elapsed_ms") or 0)
        if elapsed < threshold_ms:
            continue
        siblings = [m for m in by_label.get(control.label, []) if m != elapsed]
        request_ms = int(control.state_after.get("slowest_request_ms") or 0)
        median = sorted(siblings)[len(siblings) // 2] if siblings else None

        if median is not None and elapsed > median * OUTLIER_FACTOR:
            verdict = "outlier"
            detail = (
                f"the same control took {median}ms elsewhere ({len(siblings)} other readings) — "
                "far more likely a stalled harness than a hung control"
            )
        elif request_ms and elapsed > request_ms * 3:
            # The gap IS the defect: waiting far longer than any request took means the UI made
            # you wait for something other than the work.
            verdict = "finding"
            detail = f"waited {elapsed}ms while its slowest request took {request_ms}ms"
        elif request_ms:
            verdict = "finding"
            detail = f"its own request took {request_ms}ms"
        else:
            verdict = "unexplained"
            detail = f"waited {elapsed}ms and issued no requests at all"

        out.append({
            "area": control.area,
            "label": control.label,
            "elapsed_ms": elapsed,
            "request_ms": request_ms,
            "siblings": siblings,
            "verdict": verdict,
            "detail": detail,
        })

    out.sort(key=lambda row: -row["elapsed_ms"])
    return out
