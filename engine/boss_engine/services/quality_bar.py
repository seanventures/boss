"""THE QUALITY BAR (BOSSAPP-1362): one setting says how strict the gates are, and every gate asks
this module "does this block, at this bar?".

    prototype   blocks on: the build, the app starting, the tests the build wrote (P0), security,
                a committed secret. Everything else (P1 tests, partial must-haves, review and
                consistency findings, a weak suite) goes to the follow-ups list. No plan sign-off,
                no Pass 2 requirement.
    standard    (the default) blocks on: all of the above, P0+P1 tests written for the change,
                P1 must-haves met, a review BLOCKER, broken existing tests. P2 must-haves,
                MAJOR/MINOR findings, a weak suite and TDD order go to the follow-ups list. Pass 2
                runs alongside and never blocks.
    production  blocks on everything: P1+P2 must-haves, review BLOCKER and MAJOR, every test
                gate, and Pass 2 must have finished.

THE BAR FOLLOWS WHERE IT SHIPS: a production promotion/deploy step applies the production bar to
the run at that step (`production_refusal`): a run that passed at a lighter bar is refused, with
the list of what is still needed, and that list is the follow-up path's own ("Fix these").

Resolution order (`resolve`): the run's spec, the workflow's spec, the run's input, the project
setting `quality_bar`, then `standard`. An Advanced per-gate map (`quality_bar_gates`:
{gate: "block" | "follow_up"}) may override single gates; unknown gate names always block.

NOTHING NON-BLOCKING IS DROPPED. A gate or finding the bar lets through is recorded as a
follow-up (`BarGates.followups`, evidence["quality_bar"]), rides the PR body and becomes a
follow-up finding through the existing deferred-findings machinery.

FLAKY TESTS (a failure that passes on an immediate retry) never block at any bar: they are
reported as a follow-up (`flaky_followups`).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

SETTING = "quality_bar"
OVERRIDES_KEY = "quality_bar_gates"
PROTOTYPE, STANDARD, PRODUCTION = "prototype", "standard", "production"
PRESETS = (PROTOTYPE, STANDARD, PRODUCTION)
DEFAULT = STANDARD
#: The evidence key (quality_gates.evidence / code_task_runs.state).
KEY = "quality_bar"
#: The tier an acceptance item carries (contract items: "tier"). Untagged = P1: fail closed.
P1, P2 = "P1", "P2"
TIERS = (P1, P2)

#: gate -> does it block at (prototype, standard, production)? The judgment gates whose VALUE is
#: already bar-aware where it is computed (acceptance_met: acceptance_verdict; code_review_passed:
#: review_block_severity; consistent: consistency_passes) are listed too, so prototype demotes them.
_BLOCKS: dict[str, tuple[bool, bool, bool]] = {
    "code_written": (True, True, True),
    "app_serves": (True, True, True),
    "template_tests": (True, True, True),
    "no_committed_secrets": (True, True, True),
    "security_clean": (True, True, True),            # security findings block at every bar
    "web_package_honest": (True, True, True),
    "written_tests_pass": (True, True, True),        # the build's own tests (P0)
    "coder_did_not_edit_tests": (True, True, True),  # integrity, not polish
    "nothing_broke": (False, True, True),            # existing tests on the changed area
    "existing_tests_intact": (False, True, True),
    "brief_tests_written": (False, True, True),      # P1 tests the brief names
    "tests_written_first": (False, False, True),
    "tests_strong_enough": (False, False, True),     # mutation probe: a stricter test bar
    "code_review_passed": (False, True, True),
    "consistent": (False, True, True),
    "acceptance_met": (True, True, True),
}


def normalize(value: Any) -> Optional[str]:
    """A preset name, or None for anything that is not one."""
    word = str(value or "").strip().lower()
    return word if word in PRESETS else None


def resolve(*, run_spec: Any = None, workflow_spec: Any = None, run_input: Any = None,
            project_value: Any = None) -> str:
    """The bar for a run: run spec -> workflow spec -> run input -> project setting -> standard.
    A value that is not a preset is skipped, never an error."""
    for source in (run_spec, workflow_spec, run_input):
        if isinstance(source, Mapping):
            found = normalize(source.get(SETTING))
            if found:
                return found
    return normalize(project_value) or DEFAULT


def resolve_for_run(connection: Any, settings: Any, project_id: Optional[str], *,
                    spec: Any = None, workflow_run_id: Optional[str] = None
                    ) -> tuple[str, dict[str, str]]:
    """(the bar, the per-gate overrides) for a build, reading the workflow run's own spec and
    input and the project setting. Never raises: what cannot be read is the default."""
    workflow_spec: Any = None
    run_input: Any = None
    project_value: Any = None
    overrides: dict[str, str] = {}
    try:
        if connection is not None and workflow_run_id:
            from . import workflows

            with connection.begin_nested():
                workflow_spec = workflows._run_spec(connection, str(workflow_run_id))  # noqa: SLF001
                from sqlalchemy import text

                row = connection.execute(
                    text("SELECT input FROM workflow_runs WHERE id = :i"),
                    {"i": str(workflow_run_id)}).scalar()
            if isinstance(row, str):
                import json

                row = json.loads(row or "{}")
            run_input = row if isinstance(row, Mapping) else None
    except Exception:  # noqa: BLE001
        logger.debug("quality bar: workflow run not read", exc_info=True)
    try:
        if connection is not None and project_id:
            from . import project_settings

            with connection.begin_nested():
                project_value = project_settings.effective(connection, project_id, settings,
                                                           SETTING)
        else:
            project_value = getattr(settings, SETTING, None)
    except Exception:  # noqa: BLE001
        project_value = getattr(settings, SETTING, None)
    for source in (spec, workflow_spec, run_input):
        if isinstance(source, Mapping) and isinstance(source.get(OVERRIDES_KEY), Mapping):
            for gate, how in source[OVERRIDES_KEY].items():
                if str(how) in ("block", "follow_up"):
                    overrides.setdefault(str(gate), str(how))
    return (resolve(run_spec=spec, workflow_spec=workflow_spec, run_input=run_input,
                    project_value=project_value), overrides)


def gate_blocks(gate: str, bar: str, overrides: Optional[Mapping[str, str]] = None) -> bool:
    """Does a failing `gate` block at `bar`? An unknown gate always blocks (fail closed)."""
    how = (overrides or {}).get(gate)
    if how == "block":
        return True
    if how == "follow_up":
        return False
    row = _BLOCKS.get(gate)
    if row is None:
        return True
    return row[PRESETS.index(normalize(bar) or DEFAULT)]


def review_block_severity(bar: str) -> str:
    """The lowest review severity that blocks: BLOCKER at standard, MAJOR at production, none
    at prototype (security is a separate gate and always blocks)."""
    return {PROTOTYPE: "none", PRODUCTION: "major"}.get(normalize(bar) or DEFAULT, "blocker")


def plan_signoff_required(bar: str) -> bool:
    """Prototypes need no plan sign-off; standard and production keep it."""
    return normalize(bar) != PROTOTYPE


def pass_two_must_finish(bar: str) -> bool:
    return normalize(bar) == PRODUCTION


# ---------------------------------------------------------------------------------------------
# Tagging: the acceptance contract's items carry a tier
# ---------------------------------------------------------------------------------------------

def item_tier(item: Mapping[str, Any]) -> str:
    """P2 only when the item was tagged P2; everything else is P1 (an untagged item is held)."""
    return P2 if str((item or {}).get("tier") or "").strip().upper() == P2 else P1


def parse_tier(value: Any) -> Optional[str]:
    word = str(value or "").strip().upper()
    return word if word in TIERS else None


#: What an item says when it is the critical path: never tagged P2 whatever a reader guessed (the
#: floor under a model's tag, like contract.floor_tags: a guard on its output, not the reasoning).
_CRITICAL_WORDS = ("starts", "start up", "boots", "serves", "sign in", "sign-in", "login", "log in",
                   "authentic", "authoriz", "authoris", "password", "session")


def floor_tier(text: Any, tier: Optional[str]) -> Optional[str]:
    """`tier` under the floor: an item about the app starting, sign-in or authorization is P1."""
    if tier == P2 and any(w in str(text or "").lower() for w in _CRITICAL_WORDS):
        return None
    return tier


def acceptance_verdict(record: Optional[Mapping[str, Any]], bar: str) -> tuple[bool, list[str]]:
    """(does the acceptance gate pass at `bar`, the unmet items it lets through as follow-ups).

    met / no_items pass. A check that did not run (not_run) is the check's fault: it blocks at
    standard and production (it is re-asked, never waved through) and is a follow-up at
    prototype. Otherwise every unmet item (MISSING, PARTIAL, not judged, needs a person) blocks
    when it is P1, or when the bar is production; a P2 item is a follow-up below it. At
    prototype nothing in the must-haves blocks: all of it is follow-up work."""
    from .acceptance import verdict as _v

    bar = normalize(bar) or DEFAULT
    record = record or {}
    if record.get("verdict") in ("met", "no_items"):
        return True, []
    items = [i for i in record.get("items") or [] if isinstance(i, Mapping)]
    unmet = [i for i in items if i.get("status") != _v.MET and not _v._advisory(i)]  # noqa: SLF001
    soft: list[str] = []

    def line(i: Mapping[str, Any]) -> str:
        return (f"Must-have {i.get('n')} ({item_tier(i)}) {i.get('status')}: "
                f"{i.get('text')}")[:600]

    if bar == PROTOTYPE:
        soft = [line(i) for i in unmet]
        if record.get("why"):
            soft.append(f"Acceptance check: {record['why']}")
        return True, soft
    if record.get("verdict") != "not_met":
        return False, []             # not_run (or no record): the check never judged the code
    stack_missing = bool((record.get("stack") or {}).get("missing"))
    blocking = [i for i in unmet if item_tier(i) == P1 or bar == PRODUCTION]
    soft = [line(i) for i in unmet if i not in blocking]
    if stack_missing or blocking:
        return False, soft
    if not unmet and record.get("verdict") == "not_met":
        # not_met for a reason that names no item (unverified claims, a person's call, items
        # beyond the reviewer's passes): not a P2 gap, so it holds.
        return False, []
    return True, soft


def consistency_passes(bar: str, verdict: str, summary: str = "") -> tuple[bool, bool]:
    """(passes, is_followup) for the consistency check at `bar`. A check that never saw the change
    (hollow) never passes below prototype. Drift blocks at production; at standard only a
    BLOCKER-graded finding does; at prototype it is a follow-up."""
    bar = normalize(bar) or DEFAULT
    if verdict not in ("drift", "hollow"):
        return True, False
    if bar == PROTOTYPE:
        return True, True
    if bar == PRODUCTION or verdict == "hollow":
        return False, False
    from .review_comments import parse_findings

    graded = parse_findings(summary or "", stage="consistency_check")
    if not graded:
        return False, False
    ok = not any(c.severity == "blocker" for c in graded)
    return ok, ok


# ---------------------------------------------------------------------------------------------
# The gates dict: a gate that fails but does not block at this bar is a follow-up, not a failure
# ---------------------------------------------------------------------------------------------

class BarGates(dict):
    """The pipeline's `gates` mapping, bar-aware. Assigning False to a gate the bar does not
    block on stores True and keeps the gate in `.followups` (gate -> detail lines). Every later
    re-judgement (a fix round, a re-review) assigns through the same door, so every decision that
    reads `gates` / `failing` reads the bar without each site knowing it."""

    def __init__(self, bar: str = DEFAULT, overrides: Optional[Mapping[str, str]] = None,
                 initial: Optional[Mapping[str, Any]] = None):
        super().__init__()
        self.bar = normalize(bar) or DEFAULT
        self.overrides = dict(overrides or {})
        self.followups: dict[str, list[str]] = {}
        for k, v in (initial or {}).items():
            self[k] = v

    def __setitem__(self, gate: str, value: Any) -> None:
        self.followups.pop(gate, None)
        if not value and not gate_blocks(gate, self.bar, self.overrides):
            self.followups[gate] = []
            super().__setitem__(gate, True)
        else:
            super().__setitem__(gate, value)

    def update(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        for k, v in dict(*args, **kwargs).items():
            self[k] = v

    def setdefault(self, gate: str, default: Any = None) -> Any:
        if gate not in self:
            self[gate] = default
        return self[gate]

    def note_followup(self, gate: str, lines: Iterable[str]) -> None:
        """Keep `gate` as a follow-up with these lines (the bar let it through)."""
        self.followups[gate] = [str(x) for x in lines if str(x).strip()]

    def set_bar_aware(self, gate: str, ok: bool, soft: Iterable[str] = ()) -> None:
        """A gate whose value was decided bar-aware by its own function (acceptance_verdict)."""
        self[gate] = ok
        soft = [str(x) for x in soft if str(x).strip()]
        if ok and soft:
            self.note_followup(gate, soft)


def followups_of(gates: Any) -> dict[str, list[str]]:
    return dict(getattr(gates, "followups", None) or {})


def followup_findings(followups: Mapping[str, list[str]],
                      evidence: Optional[Mapping[str, Any]] = None,
                      flaky: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The follow-ups as findings ({severity, text}) for the deferred-findings machinery: the
    follow-up build, the PR body and Pass 2 all read this shape."""
    from . import checks_failing as _cf

    out: list[dict[str, Any]] = []
    gates = list(followups)
    seat_findings = _cf.collect([g for g in gates if g in _cf.SEAT_OF_GATE
                                 and g != "acceptance_met"], evidence or {})
    for f in seat_findings.get("findings") or []:
        where = f" {f['where']}" if f.get("where") else ""
        out.append({"severity": "security" if f.get("security") else str(
            f.get("severity") or "major"), "text": f"({f.get('stage')}){where} "
                                                   f"{str(f.get('text') or '').strip()}"})
    seen = {f["gate"] for f in seat_findings.get("findings") or []}
    for g in gates:
        lines = followups.get(g) or []
        if g in seen and not lines:
            continue
        if lines:
            out.extend({"severity": "major", "text": text} for text in lines)
        else:
            out.append({"severity": "major",
                        "text": f"Follow-up: gate '{g}' ({_cf._gate_label(g)}) did not pass; "  # noqa: SLF001
                                "it does not block at this quality bar."})
    out.extend({"severity": "minor", "text": f"Flaky test (passed on retry): {t}"}
               for t in flaky)
    return out


def stamp(evidence: dict, gates: Any, *, flaky: Iterable[str] = ()) -> dict:
    """Record the bar, its follow-ups and flaky tests on the evidence (gate evidence, PR body,
    run state). Returns the stamp."""
    followups = followups_of(gates)
    record = {"bar": getattr(gates, "bar", DEFAULT), "followups": followups,
              "flaky": [str(x) for x in flaky]}
    evidence[KEY] = record
    gates_followup = {g: (lines or ["did not pass; non-blocking at this quality bar"])
                      for g, lines in followups.items()}
    evidence["gates_followup"] = gates_followup
    return record


def pr_section(evidence: Optional[Mapping[str, Any]]) -> str:
    """The PR description's follow-ups section ("" when there are none)."""
    rec = (evidence or {}).get(KEY) or {}
    lines: list[str] = []
    for g, items in (rec.get("followups") or {}).items():
        if items:
            lines.extend(f"- {x}" for x in items[:30])
        else:
            lines.append(f"- `{g}` did not pass; not blocking at the {rec.get('bar')} bar")
    lines.extend(f"- Flaky test (passed on retry): {t}" for t in rec.get("flaky") or [])
    if not lines:
        return ""
    return (f"## Follow-ups (quality bar: {rec.get('bar') or DEFAULT})\n\n"
            "These did not block this build at its quality bar. They are queued, not dropped.\n\n"
            + "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------------------------
# The bar follows where it ships: the production rule at a deploy/promote step
# ---------------------------------------------------------------------------------------------

PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod"})


def is_production(environment: Any) -> bool:
    return str(environment or "").strip().lower() in PRODUCTION_ENVIRONMENTS


def production_needs(evidence: Optional[Mapping[str, Any]],
                     gates_failed: Iterable[str] = ()) -> list[str]:
    """What this run still needs to pass the PRODUCTION bar, from its recorded evidence (empty =
    it passes). Pure: the quality gate row's evidence and failed gates in, a list out."""
    from . import checks_failing as _cf
    from .acceptance import verdict as _v
    from .review_comments import parse_findings

    evidence = evidence or {}
    needs: list[str] = []
    for g in gates_failed or ():
        needs.append(f"Gate '{g}' ({_cf._gate_label(g)}) failed.")  # noqa: SLF001
    rec = evidence.get(KEY) or {}
    followups = dict(rec.get("followups") or evidence.get("gates_followup") or {})
    for g, lines in followups.items():
        if g in ("code_review_passed", "consistent"):
            continue                                   # judged by severity below
        if gate_blocks(g, PRODUCTION):
            needs.extend(lines or [f"Gate '{g}' ({_cf._gate_label(g)}) did not pass."])  # noqa: SLF001
    acceptance = evidence.get("acceptance") or {}
    if acceptance.get("verdict") == "not_met":
        for i in acceptance.get("items") or []:
            if isinstance(i, Mapping) and i.get("status") != _v.MET and not _v._advisory(i):  # noqa: SLF001
                needs.append(f"Must-have {i.get('n')} ({item_tier(i)}) {i.get('status')}: "
                             f"{i.get('text')}"[:600])
    for key, stage, label in (("review_summary", "review_code", "Review"),
                              ("consistency_summary", "consistency_check", "Consistency")):
        for c in parse_findings(str(evidence.get(key) or ""), stage=stage):
            if c.severity in ("blocker", "major") and not _resolved(evidence, c):
                needs.append(f"{label} finding ({c.severity.upper()}) {c.location()}: "
                             f"{c.body}"[:600])
    for t in rec.get("flaky") or []:
        pass          # flaky tests are reported, never required
    return list(dict.fromkeys(needs))


def _resolved(evidence: Mapping[str, Any], finding: Any) -> bool:
    """A finding a fix round closed (evidence["resolved_findings"] holds its text)."""
    body = " ".join(str(getattr(finding, "body", "") or "").split())
    return body in {" ".join(str(x).split()) for x in evidence.get("resolved_findings") or []}


def production_refusal(connection: Any, *, code_task_run_id: Optional[str],
                       project_id: Optional[str] = None,
                       workflow_run_id: Optional[str] = None) -> Optional[str]:
    """The words refusing a production deploy/promotion, or None when the run passes the
    production bar (or has no recorded gates to judge). Never raises: a check that cannot be
    read does not refuse a release the operator approved."""
    needs: list[str] = []
    try:
        import json

        from sqlalchemy import text

        row = None
        if not code_task_run_id and workflow_run_id:
            # The build this workflow run shipped: its newest code task run with gates recorded.
            code_task_run_id = connection.execute(text(
                "SELECT r.id FROM code_task_runs r JOIN quality_gates q "
                "ON q.code_task_run_id = r.id WHERE r.finalize_ctx->>'workflow_run_id' = :w "
                "ORDER BY r.created_at DESC LIMIT 1"), {"w": workflow_run_id}).scalar()
        if code_task_run_id:
            row = connection.execute(text(
                "SELECT status, gates_failed, evidence FROM quality_gates "
                "WHERE code_task_run_id = :r ORDER BY created_at DESC LIMIT 1"),
                {"r": code_task_run_id}).mappings().first()
        if row is not None:
            evidence = row["evidence"]
            if isinstance(evidence, str):
                evidence = json.loads(evidence or "{}")
            failed = row["gates_failed"]
            if isinstance(failed, str):
                failed = json.loads(failed or "[]")
            needs = production_needs(evidence, failed or [])
    except Exception:  # noqa: BLE001
        logger.warning("quality bar: production rule not evaluated", exc_info=True)
        return None
    try:
        with connection.begin_nested():
            needs += pass_two_needs(connection, workflow_run_id)
    except Exception:  # noqa: BLE001 — the gates' own needs stand; pass 2's could not be read
        logger.warning("quality bar: pass 2 not read", exc_info=True)
    if not needs:
        return None
    listed = "\n".join(f"- {n}" for n in needs[:40])
    return ("This is a production release, so the production quality bar applies: this build "
            "passed at a lighter bar and these are still needed. Fix them (Fix these, or the "
            "follow-up build) and release again:\n" + listed)


def pass_two_needs(connection: Any, workflow_run_id: Optional[str]) -> list[str]:
    """Production needs Pass 2 finished: the workflow run's More tests step, when it has one,
    must have succeeded."""
    if not workflow_run_id:
        return []
    from sqlalchemy import text

    from . import workflows
    from .two_pass import MORE_TESTS_CAPABILITY, _capability

    spec = workflows._run_spec(connection, str(workflow_run_id))  # noqa: SLF001
    out: list[str] = []
    for step in (spec or {}).get("steps") or []:
        if _capability(step) != MORE_TESTS_CAPABILITY:
            continue
        row = connection.execute(text(
            "SELECT status FROM workflow_step_runs WHERE workflow_run_id = :r "
            "AND step_id = :s ORDER BY iteration DESC, created_at DESC LIMIT 1"),
            {"r": workflow_run_id, "s": str(step.get("id"))}).mappings().first()
        status = str((row or {}).get("status") or "not started")
        if status != "succeeded":
            out.append(f"Pass 2 (More tests) has not finished (it is {status}): production "
                       "waits for it.")
    return out
