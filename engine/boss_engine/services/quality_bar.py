"""THE QUALITY BAR (BOSSAPP-1362): one setting says how strict the gates are, and every gate asks
this module "does this block, at this bar?".

    prototype   blocks on: the build, the app starting, the tests the build wrote (P0), security,
                a committed secret, the tests' integrity (existing tests deleted or weakened). Everything else (P1 tests, partial must-haves, review and
                consistency findings, a weak suite) goes to the follow-ups list. No plan sign-off,
                no Pass 2 requirement.
    standard    (the default; Easy Mode, templates, Try a sample, New work) THE LIGHT LANE
                (operator, 2026-11-18: "there is no need to fail ... only agents should fix it"):
                blocks on the floor, a review BLOCKER, a consistency finding about security or
                secrets, and a declared stack built in another one. Everything else — missing or
                partial must-haves (P1 too), an acceptance check that did not run, the brief's
                tests not written, broken existing tests off the P0 paths, a review that refuted
                without naming an issue, MAJOR/MINOR findings, consistency drift, a weak suite,
                TDD order — is a follow-up BOSS queues and fixes itself. Pass 2 never blocks.
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
    "nothing_broke": (True, True, True),             # computed: broken_tests_block (by path)
    "existing_tests_intact": (True, True, True),     # integrity: existing tests deleted/weakened
    "brief_tests_written": (False, False, True),     # P1 tests the brief names: a follow-up below production
    "tests_written_first": (False, False, True),
    "tests_strong_enough": (False, False, True),     # mutation probe: a stricter test bar
    "code_review_passed": (False, True, True),
    "consistent": (False, True, True),
    "acceptance_met": (True, True, True),
    "input_fidelity": (True, True, True),            # a remediation's output comes from its input
}


def normalize(value: Any) -> Optional[str]:
    """A preset name, or None for anything that is not one."""
    word = str(value or "").strip().lower()
    return word if word in PRESETS else None


#: Written ONLY by the server (workflows.start_run, after the permission check at the start door).
#: A caller's own copy of these input keys is dropped there, so an agent's start_workflow inputs
#: can set neither a bar nor a per-gate map.
BY_KEY = "_quality_bar_by"
OVERRIDE_PERMISSION = "settings:update"
RANK = {PROTOTYPE: 0, STANDARD: 1, PRODUCTION: 2}

#: The gates no override may relax, at any bar: the build, the app starting, the tests the build
#: wrote, their integrity, security, secrets and an applied template. (Unknown gates block too.)
FLOOR = frozenset({"code_written", "app_serves", "template_tests", "no_committed_secrets",
                   "security_clean", "web_package_honest", "written_tests_pass",
                   "coder_did_not_edit_tests", "existing_tests_intact", "input_fidelity"})


class QualityBarOverrideRefused(PermissionError):
    """Lowering the bar (or overriding single gates) needs a person with settings:update."""


def check_override_permission(project_id: str, actor: Any) -> None:
    """Who may start a run below the project's bar, set a per-gate map, or set a bar on a workflow:
    ENFORCED identity: the request's own principal must be the person `actor` and hold
    `settings:update` at the project (a real PDP check; a name alone is never enough). NOT
    enforced (a legacy install, a script): the project's owner actors (`ceo`...) may, which the
    start door already established with require_project_actor; an agent, a job or 'boss' never.
    The grant is recorded on the run / workflow (`_quality_bar_by`, an event, the evidence).

    LEGACY INSTALLS (identity not enforced) ARE A WEAKER GUARANTEE, by design: the check is a
    denylist of non-person actor names, so anything that publishes a workflow or starts a run AS
    an owner actor such as `ceo` can lower a bar (the project's own API keys are who may act as
    that actor). Turn identity enforcement on for the real `settings:update` decision."""
    who = str(actor or "").strip()
    if not who or who.lower() in ("boss", "system", "worker") or who.startswith(
            ("agent", "agt_", "job_", "svc_", "sa_")):
        raise QualityBarOverrideRefused("only a person may change a quality bar")
    from ..identity import lineage, viewer

    allowed = viewer.decide(project_id, OVERRIDE_PERMISSION)
    if allowed is None:
        return                      # identity not enforced: the legacy owner actor
    principal = getattr(lineage.current_context(), "principal", None)
    if (not allowed or principal is None or not principal.is_person
            or str(principal.id) != who):
        raise QualityBarOverrideRefused(
            f"changing a quality bar needs {OVERRIDE_PERMISSION} at this project")


def resolve_detail(*, run_spec: Any = None, workflow_spec: Any = None, run_input: Any = None,
                   project_value: Any = None) -> dict[str, Any]:
    """{bar, source, lowered_by, overrides} for a run. Order: the RUN SPEC (server-owned), the
    run input (only when the server stamped it: BY_KEY, after the permission check), the
    WORKFLOW spec (it may raise the project's bar; it may LOWER it only when publish_version stamped
    it, i.e. a person with settings:update published it that way), the project setting, `standard`.
    A value that is not a preset is skipped, never an error. The per-gate map is honoured only
    from the run spec, a stamped run input or a stamped workflow spec."""
    project_bar = normalize(project_value) or DEFAULT
    spec = run_spec if isinstance(run_spec, Mapping) else {}
    inp = run_input if isinstance(run_input, Mapping) else {}
    wf = workflow_spec if isinstance(workflow_spec, Mapping) else {}
    stamped = bool(str(inp.get(BY_KEY) or "").strip())
    by = str(spec.get(BY_KEY) or inp.get(BY_KEY) or "").strip() or None
    bar, source = project_bar, "project" if normalize(project_value) else "default"
    if normalize(spec.get(SETTING)):
        bar, source = normalize(spec.get(SETTING)), "run_spec"
    elif stamped and normalize(inp.get(SETTING)):
        bar, source = normalize(inp.get(SETTING)), "run_input"
    elif normalize(wf.get(SETTING)) and (
            RANK[normalize(wf.get(SETTING))] >= RANK[project_bar]
            or str(wf.get(BY_KEY) or "").strip()):
        # A workflow may RAISE the bar freely; it may LOWER it only when a person with
        # settings:update published it that way (stamped by publish_version, never by a caller).
        bar, source = normalize(wf.get(SETTING)), "workflow"
        by = by or str(wf.get(BY_KEY) or "").strip() or None
    overrides: dict[str, str] = {}
    for src_ok, src in ((True, spec), (stamped, inp), (bool(str(wf.get(BY_KEY) or "").strip()), wf)):
        if src_ok and isinstance(src.get(OVERRIDES_KEY), Mapping):
            for gate, how in src[OVERRIDES_KEY].items():
                if str(how) in ("block", "follow_up"):
                    overrides.setdefault(str(gate), str(how))
    lowered = RANK[bar] < RANK[project_bar] or any(
        how == "follow_up" for how in overrides.values())
    return {"bar": bar, "source": source, "overrides": overrides,
            "lowered_by": (by or "server") if lowered else None}


def resolve(*, run_spec: Any = None, workflow_spec: Any = None, run_input: Any = None,
            project_value: Any = None) -> str:
    return resolve_detail(run_spec=run_spec, workflow_spec=workflow_spec, run_input=run_input,
                          project_value=project_value)["bar"]


def _configured_review_severity(settings: Any) -> Optional[str]:
    """BOSS_CODE_REVIEW_BLOCK_SEVERITY, when it was set explicitly (never the default): it may
    only make the review stricter than the bar says (review_block_severity)."""
    try:
        if "code_review_block_severity" not in getattr(settings, "model_fields_set", ()):
            return None
        word = str(getattr(settings, "code_review_block_severity", "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return None
    return word if word in ("nit", "minor", "major", "blocker") else None


def launched_by(connection: Any, project_id: Optional[str], workflow_run_id: Optional[str],
                run_id: Optional[str]) -> bool:
    """Did THIS workflow run launch build `run_id`, in the same project? A build's
    `finalize_ctx.workflow_run_id` can be written by a caller (POST /code-tasks, create_code_task),
    so it is believed only when the workflow run is in the build's project and one of its step
    runs names the build in a STRUCTURED field: `launched_code_task_run_id`, written by the runner
    in the launching transaction (workflow_capability_runner._record_launch), or the step's
    final output. Otherwise the field is dropped and the build takes the project's bar."""
    if not (connection and project_id and workflow_run_id and run_id):
        return False
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT 1 FROM workflow_runs w WHERE w.id = :w AND w.project_id = :p AND EXISTS ("
        "SELECT 1 FROM workflow_step_runs s WHERE s.workflow_run_id = w.id AND "
        "(s.output->>'launched_code_task_run_id' = :r OR s.output->>'code_task_run_id' = :r "
        " OR s.output->>'run_id' = :r OR s.output->'await'->>'id' = :r)) LIMIT 1"),
        {"w": workflow_run_id, "p": project_id, "r": run_id}).first()
    return row is not None


def resolve_for_run(connection: Any, settings: Any, project_id: Optional[str], *,
                    spec: Any = None, workflow_run_id: Optional[str] = None,
                    run_id: Optional[str] = None
                    ) -> tuple[str, dict[str, str], dict[str, Any]]:
    """(the bar, the per-gate overrides, {source, lowered_by, review_severity}) for a build,
    reading the workflow run's own spec and input and the project setting. Never raises: what
    cannot be read is the default."""
    workflow_spec: Any = None
    run_input: Any = None
    project_value: Any = None
    try:
        if workflow_run_id and not launched_by(connection, project_id, workflow_run_id, run_id):
            workflow_run_id = None      # not this build's workflow run: drop the field
    except Exception:  # noqa: BLE001
        workflow_run_id = None
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
    found = resolve_detail(run_spec=spec if isinstance(spec, Mapping) else None,
                           workflow_spec=workflow_spec, run_input=run_input,
                           project_value=project_value)
    info = {"source": found["source"], "lowered_by": found["lowered_by"],
            "review_severity": _configured_review_severity(settings),
            # THE WORKFLOW'S "CHECKS MUST PASS" (spec `checks_must_pass`): the repo's checkers'
            # open P0/P1 findings block the merge (input_fidelity.repo_checks); else reported.
            "checks_must_pass": checks_must_pass(spec, workflow_spec)}
    return found["bar"], found["overrides"], info


def checks_must_pass(run_spec: Any = None, workflow_spec: Any = None) -> bool:
    """The workflow setting "Checks must pass" (`checks_must_pass: true` on the workflow's spec,
    or on the build's own spec). Off by default: the light lane."""
    for spec in (run_spec, workflow_spec):
        if isinstance(spec, Mapping) and spec.get("checks_must_pass") is not None:
            return bool(spec.get("checks_must_pass"))
    return False


def gate_blocks(gate: str, bar: str, overrides: Optional[Mapping[str, str]] = None) -> bool:
    """Does a failing `gate` block at `bar`? An unknown gate and the FLOOR always block: no
    override relaxes them. Other known gates honour a per-gate override."""
    row = _BLOCKS.get(gate)
    if row is None or gate in FLOOR:
        return True
    how = (overrides or {}).get(gate)
    if how == "block":
        return True
    if how == "follow_up":
        return False
    return row[PRESETS.index(normalize(bar) or DEFAULT)]


_SEVERITY_RANK = ("nit", "minor", "major", "blocker", "none")


def review_block_severity(bar: str, configured: Optional[str] = None) -> str:
    """The lowest review severity that blocks: BLOCKER at standard, MAJOR at production, none
    at prototype (security is a separate gate and always blocks). `configured` is an explicit
    BOSS_CODE_REVIEW_BLOCK_SEVERITY: it can only make this stricter, never looser."""
    base = {PROTOTYPE: "none", PRODUCTION: "major"}.get(normalize(bar) or DEFAULT, "blocker")
    if configured in _SEVERITY_RANK and _SEVERITY_RANK.index(configured) < _SEVERITY_RANK.index(base):
        return configured
    return base


#: From this risk tier up, a plan is signed off by a person at every bar.
SIGNOFF_TIER = 3


def plan_signoff_required(bar: str, risk_tier: Optional[int] = None,
                          sensitive: bool = False) -> bool:
    """Prototypes skip the plan sign-off of the DEFAULT sensitive tier only: risk tier >= 3, or
    work with sensitive markers, is signed off at every bar. Standard and production keep it."""
    if normalize(bar) != PROTOTYPE:
        return True
    return bool(sensitive) or int(risk_tier or 0) >= SIGNOFF_TIER


#: THE LIGHT LANE'S FIX-ROUND CAP (operator, 2026-11-18): below production a build gets at most
#: this many review fix rounds (warm, in the run; and cold, a new run), then hands over — ready
#: with follow-ups, or the floor/blocker card. Recipe box's follow-up looped three rounds on
#: wording-level must-haves. The build's own implement/test repair loop is not capped here: a
#: floor failure (its tests cannot run) is the agents' to fix.
LIGHT_LANE_FIX_ROUNDS = 1


def fix_rounds(bar: str, configured: int) -> int:
    """The review fix rounds a build at `bar` may run: `configured` at production, at most
    LIGHT_LANE_FIX_ROUNDS below it."""
    n = max(0, int(configured or 0))
    return n if normalize(bar) == PRODUCTION else min(n, LIGHT_LANE_FIX_ROUNDS)


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

    met / no_items pass. Below production (the light lane) every gap is a follow-up — a missing
    P1 must-have and a check that did not run included — except, at standard, a declared stack
    built in another one. At production a check that did not run blocks (never waved through). Otherwise every unmet item (MISSING, PARTIAL, not judged, needs a person) blocks
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

    if bar in (PROTOTYPE, STANDARD):
        # THE LIGHT LANE: every gap is a follow-up BOSS fixes (named on the ready card) — a
        # missing P1 must-have, a check that could not judge. Only a declared stack built in
        # another one holds at standard: that build is not the one the brief asked for.
        if bar == STANDARD and (record.get("stack") or {}).get("missing"):
            return False, []
        soft = [line(i) for i in unmet]
        if record.get("why"):
            soft.append(f"Acceptance check: {record['why']}")
        if not record or record.get("verdict") in (None, "", "not_run", "not_checked"):
            soft.append("Acceptance check did not judge the change; BOSS re-checks it in the "
                        "follow-up")
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


#: A failing test whose name or path says it guards the critical path (P0): startup, health,
#: sign-in/authorization. Matched on the test's own words, like quality_bar.floor_tier.
P0_TEST_WORDS = ("startup", "start-up", "boot", "health", "auth", "login", "log-in", "signin",
                 "sign-in", "sign_in", "session", "password", "serves", "smoke")


def _words(text: Any) -> set[str]:
    import re

    return {w for w in re.split(r"[^a-z0-9]+", str(text or "").lower()) if len(w) >= 5}


def broken_tests_block(bar: str, failing: Iterable[str]) -> tuple[bool, str]:
    """(does a broken EXISTING test block at `bar`, why it did not when it does not).

    production: always. prototype and standard (the light lane): only a P0 test (its name or
    file marks startup, health, auth or sign-in) holds; the rest is a follow-up. Failing tests
    that cannot be named block at production and are a follow-up below it."""
    bar = normalize(bar) or DEFAULT
    if bar == PRODUCTION:
        return True, ""
    names = [str(n) for n in failing or () if str(n).strip()]
    if not names:
        return False, f"failing existing tests could not be named; not a P0 at the {bar} bar"
    p0 = any(w in n.lower() for n in names for w in P0_TEST_WORDS)
    return p0, (f"{len(names)} existing test(s) failing, none on a P0 path "
                f"(startup, auth, health): {'; '.join(names[:5])}")


#: Words that make a consistency finding a security/secrets one (it then holds at every bar).
_SECURITY_WORDS = ("security", "secret", "credential", "password", "api key", "token leak",
                   "private key", "injection", "xss", "csrf", "ssrf", "auth bypass",
                   "unauthenticated", "privilege", "vulnerab")


def consistency_passes(bar: str, verdict: str, summary: str = "") -> tuple[bool, bool]:
    """(passes, is_followup) for the consistency check at `bar`. A check that never saw the change
    (hollow) never passes below prototype. Drift blocks at production; at standard only a
    BLOCKER-graded finding does; at prototype it is a follow-up."""
    bar = normalize(bar) or DEFAULT
    if verdict not in ("drift", "hollow"):
        return True, False
    if bar == PRODUCTION:
        return False, False
    # THE LIGHT LANE (standard, prototype): drift, even a BLOCKER, or a check that did not see
    # the change is a follow-up — unless what it names is about security or secrets, which
    # holds at every bar.
    low = str(summary or "").lower()
    if verdict == "drift" and any(w in low for w in _SECURITY_WORDS):
        return False, False
    return True, True


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
            # A must-have line carries its tier ("Must-have 3 (P2) partial: …"): P2 is minor.
            out.extend({"severity": "minor" if f"({P2})" in text[:40] else "major",
                        "text": text} for text in lines)
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
    info = dict(getattr(gates, "info", None) or {})
    record = {"bar": getattr(gates, "bar", DEFAULT), "followups": followups,
              "flaky": [str(x) for x in flaky],
              "source": info.get("source"), "lowered_by": info.get("lowered_by"),
              "overrides": dict(getattr(gates, "overrides", None) or {})}
    evidence[KEY] = record
    gates_followup = {g: (lines or ["did not pass; non-blocking at this quality bar"])
                      for g, lines in followups.items()}
    evidence["gates_followup"] = gates_followup
    return record


#: evidence key: who takes this build's follow-ups, written by the pipeline before the pull
#: request opens: "queued" (BOSS starts a follow-up build), "pass_two" (the workflow's More
#: tests pass on this branch takes them), "owner" (the build already working on this branch
#: carries them), or false (they are listed for later: a follow-up, a re-check, follow-ups off).
#: Read by the merge card's words and the PR body, so neither says "BOSS is fixing it" when no
#: follow-up build will start.
QUEUED_KEY = "followups_queued"
HANDLED_BY = ("queued", "pass_two", "owner")


def handled_by(value: Any) -> Optional[str]:
    """The QUEUED_KEY value as one of HANDLED_BY, or None. `True` (older records) is queued."""
    if value is True:
        return "queued"
    word = str(value or "").strip().lower()
    return word if word in HANDLED_BY else None
#: The merge card's follow-ups record (decision recommendation).
CARD_KEY = "followups"


def finding_tier(finding: Mapping[str, Any]) -> str:
    """A follow-up's tier: P1 for what a stricter bar would have blocked on (a blocker, a major,
    a security note), P2 for the rest (minor, nit, a flaky test)."""
    sev = str(finding.get("severity") or "major").strip().lower()
    return P1 if sev in ("blocker", "major", "security", "critical", "high") else P2


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def followup_items(evidence: Optional[Mapping[str, Any]], limit: int = 40
                   ) -> list[dict[str, Any]]:
    """The build's follow-ups as one list, P1 first: every deferred finding (review findings
    below the bar's blocking severity, the gates the bar let through, flaky tests), each
    {tier, severity, text}."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in (evidence or {}).get("deferred_findings") or []:
        if not isinstance(f, Mapping):
            continue
        text = _one_line(f.get("text"))
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        where = _one_line(f.get("path"))
        if where and f.get("line"):
            where = f"{where}:{f.get('line')}"
        seat = next((str(f[k]) for k in ("seat", "reviewer", "role", "lens")
                     if isinstance(f.get(k), str) and f.get(k)), None)
        out.append({"tier": finding_tier(f), "severity": str(f.get("severity") or "major").lower(),
                    "text": (f"{where} — {text}" if where and where not in text else text)[:400],
                    # Which seat raised it (a security reviewer's high on a P0 line is never
                    # deferred: review_packet.for_run).
                    **({"seat": seat} if seat else {})})
    out.sort(key=lambda i: 0 if i["tier"] == P1 else 1)
    return out[:limit]


def merge_followups(evidence: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """What the merge card says about a build whose open items are only follow-ups: {count, p1,
    queued, bar, items}. None when there are none. A follow-up never makes the card a failure:
    the card is a normal "ready to merge" one, and BOSS (not the person) fixes the follow-ups."""
    items = followup_items(evidence, limit=1000)
    if not items:
        return None
    card = {"count": len(items), "p1": sum(1 for i in items if i["tier"] == P1),
            "queued": handled_by((evidence or {}).get(QUEUED_KEY)) == "queued",
            "handled_by": handled_by((evidence or {}).get(QUEUED_KEY)),
            "bar": ((evidence or {}).get(KEY) or {}).get("bar") or DEFAULT, "items": items[:40]}
    card["words"] = followup_words(card)
    return card


def followup_words(followups: Mapping[str, Any]) -> str:
    """"1 follow-up queued, BOSS is fixing it" / "3 follow-ups listed for later"."""
    n = int(followups.get("count") or 0)
    s = "" if n == 1 else "s"
    how = handled_by(followups.get("handled_by")) or ("queued" if followups.get("queued") else None)
    if how == "queued":
        return f"{n} follow-up{s} queued, BOSS is fixing {'it' if n == 1 else 'them'}"
    if how == "pass_two":
        return f"{n} follow-up{s} go{'es' if n == 1 else ''} to the More tests pass next"
    if how == "owner":
        return f"{n} follow-up{s} for the build already working on this branch"
    return f"{n} follow-up{s} listed for later"


def ready_words(name: str, followups: Mapping[str, Any]) -> str:
    """The merge card's headline for a build whose open items are only follow-ups:
    "Recipe box is ready — 1 follow-up queued, BOSS is fixing it"."""
    return f"{name} is ready — {followup_words(followups)}"


def pr_section(evidence: Optional[Mapping[str, Any]]) -> str:
    """The PR description's follow-ups section ("" when there are none): the gates the bar let
    through, the flaky tests, then every other follow-up (a deferred review finding, P1/P2)."""
    rec = (evidence or {}).get(KEY) or {}
    lines: list[str] = []
    for g, items in (rec.get("followups") or {}).items():
        if items:
            lines.extend(f"- {x}" for x in items[:30])
        else:
            lines.append(f"- `{g}` did not pass; not blocking at the {rec.get('bar')} bar")
    lines.extend(f"- Flaky test (passed on retry): {t}" for t in rec.get("flaky") or [])
    listed = _one_line(" ".join(lines)).lower()
    for item in followup_items(evidence):
        core = item["text"].split(" — ", 1)[-1].lower()
        if core in listed or core.startswith(("follow-up: gate", "flaky test (passed on retry)")):
            continue
        lines.append(f"- {item['tier']} [{item['severity'].upper()}] {item['text']}")
    if not lines:
        return ""
    who = {"queued": "BOSS has queued a follow-up build that fixes them on this branch.",
           "pass_two": "The workflow's More tests pass takes them next, on this branch.",
           "owner": "The build already working on this branch carries them."}.get(
        handled_by((evidence or {}).get(QUEUED_KEY)) or "", "They are recorded, not dropped.")
    return (f"## Follow-ups (quality bar: {rec.get('bar') or DEFAULT})\n\n"
            f"These did not block this build at its quality bar. {who}\n\n"
            + "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------------------------
# The bar follows where it ships: the production rule at a deploy/promote step
# ---------------------------------------------------------------------------------------------

def is_production(environment: Any) -> bool:
    """Is this environment a production one: the same answer the identity layer gives
    (object_rules.is_production_environment). Callers pass the TARGET's own environment, not the
    name a step or a caller typed."""
    from ..identity.object_rules import is_production_environment

    return is_production_environment(environment)


NOT_JUDGED = ("This is a production release, so the production quality bar applies, and nothing "
              "was judged for this build ({why}). Run the build's checks (a re-check) and "
              "release again.")


#: The action a refusal for an unconvened seat points at (change_recheck: quality_bar="production").
RECHECK_ACTION = ("Re-check at the production bar (it convenes every review seat whatever the "
                  "build's profile), then release again.")
#: The judgement gates a production build must have had a verdict from.
SEAT_GATES_REQUIRED = ("code_review_passed", "security_clean", "consistent", "acceptance_met")


def production_needs(evidence: Optional[Mapping[str, Any]],
                     gates_failed: Iterable[str] = (),
                     gates_passed: Optional[Iterable[str]] = None) -> list[str]:
    """What this run still needs to pass the PRODUCTION bar, from its recorded evidence (empty =
    it passes). Pure: the quality gate row's evidence and failed gates in, a list out."""
    from . import checks_failing as _cf
    from . import seat_policy as _seat_policy
    from .acceptance import verdict as _v
    from .review_comments import parse_findings

    evidence = evidence or {}
    needs: list[str] = []
    for g in gates_failed or ():
        needs.append(f"Gate '{g}' ({_cf._gate_label(g)}) failed.")  # noqa: SLF001
    # A check that did not run is not a pass: a seat the profile did not convene is needed.
    unrun = dict(_seat_policy.skipped_gates(evidence))
    if gates_passed is not None:
        # A build whose tier or profile never convened a seat (a tier-0 build has no review at
        # all) records no verdict for it: absent from passed, failed and follow-ups = not run.
        have = set(gates_passed) | set(gates_failed or ()) | set(
            (evidence.get(KEY) or {}).get("followups") or {})
        for g in SEAT_GATES_REQUIRED:
            if g not in have:
                unrun.setdefault(g, "no verdict was recorded for this build")
    for g, why in unrun.items():
        needs.append(f"Gate '{g}' ({_cf._gate_label(g)}) was not run ({why}); "  # noqa: SLF001
                     "production needs it.")
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
    for key, stage, label, gate in (
            ("review_summary", "review_code", "Review", "code_review_passed"),
            ("consistency_summary", "consistency_check", "Consistency", "consistent")):
        covered = False
        for c in parse_findings(str(evidence.get(key) or ""), stage=stage):
            if c.severity in ("blocker", "major") and not _resolved(evidence, c):
                covered = True
                needs.append(f"{label} finding ({c.severity.upper()}) {c.location()}: "
                             f"{c.body}"[:600])
        if gate in followups and not covered:
            # JUDGED AT A LIGHTER BAR WITH NOTHING PARSED TO SHOW FOR IT (a refutation that
            # named no issue, a check that never saw the change): production re-checks it.
            why = "; ".join(str(x) for x in followups.get(gate) or [])[:400]
            needs.append(f"{label} was judged at a lighter bar"
                         + (f" ({why})" if why else "")
                         + "; production needs a re-check at the production bar.")
    # (A flaky test is reported, never required: it already passed its re-runs, see the build.)
    return list(dict.fromkeys(needs))


def _resolved(evidence: Mapping[str, Any], finding: Any) -> bool:
    """A finding a fix round closed (evidence["resolved_findings"] holds its text)."""
    body = " ".join(str(getattr(finding, "body", "") or "").split())
    return body in {" ".join(str(x).split()) for x in evidence.get("resolved_findings") or []}


def _workflow_run_of(fctx: Any) -> Optional[str]:
    fctx = fctx if isinstance(fctx, Mapping) else {}
    origin = fctx.get("origin") if isinstance(fctx.get("origin"), Mapping) else {}
    return str(fctx.get("workflow_run_id") or origin.get("workflow_run_id") or "") or None


def _run_needs(connection: Any, run_id: str) -> tuple[list[str], Optional[str]]:
    """(what this run's own recorded gates still need for production, why it could not be judged).
    Fail closed: no record, or one that cannot be read, is `why`, never a pass."""
    import json

    from sqlalchemy import text

    try:
        row = connection.execute(text(
            "SELECT status, gates_passed, gates_failed, evidence FROM quality_gates "
            "WHERE code_task_run_id = :r ORDER BY created_at DESC LIMIT 1"),
            {"r": run_id}).mappings().first()
        if row is None:
            return [], "it has no quality-gate record"

        def j(v: Any, default: Any) -> Any:
            return json.loads(v or default) if isinstance(v, str) else v

        return production_needs(j(row["evidence"], "{}") or {}, j(row["gates_failed"], "[]") or [],
                                j(row["gates_passed"], "[]") or []), None
    except Exception:  # noqa: BLE001 — unreadable evidence is not evidence of a pass
        logger.warning("quality bar: production rule could not be evaluated", exc_info=True)
        return [], "its quality-gate record could not be read"


def production_rechecks(connection: Any, run_id: str) -> list[str]:
    """The completed re-checks AT THE PRODUCTION BAR in this build's lineage, newest first (a
    re-check records `recheck_bar: production` on its run: change_recheck)."""
    import json

    from sqlalchemy import text

    row = connection.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id = :i"),
                             {"i": run_id}).scalar()
    fctx = json.loads(row) if isinstance(row, str) else (row or {})
    root = str((fctx or {}).get("root_run_id") or run_id)
    return [str(r[0]) for r in connection.execute(text(
        "SELECT id FROM code_task_runs WHERE status = 'completed' "
        "AND finalize_ctx->>'recheck_bar' = 'production' "
        "AND (finalize_ctx->>'recheck_of' = :r OR finalize_ctx->>'root_run_id' = :root) "
        "ORDER BY created_at DESC"), {"r": run_id, "root": root})]


def judging_run(connection: Any, run_id: str) -> tuple[Optional[str], list[str], Optional[str]]:
    """(the run whose record clears `run_id` for production, what is still needed, why it could
    not be judged). A build clears on its own record; otherwise the newest re-check at the
    production bar in its lineage that needs nothing clears it (so the Deploy step and deploy_app,
    which name the ORIGINAL build, work after a re-check). The release must then ship a commit
    the judging run judged (`_commit_check`)."""
    needs, why = _run_needs(connection, run_id)
    if why is None and not needs:
        return run_id, [], None
    try:
        for rc in production_rechecks(connection, run_id):
            n, w = _run_needs(connection, rc)
            if w is None and not n:
                return rc, [], None
    except Exception:  # noqa: BLE001
        logger.warning("quality bar: re-checks not read", exc_info=True)
    return None, needs, why


def production_refusal(connection: Any, *, code_task_run_id: Optional[str] = None,
                       workflow_run_id: Optional[str] = None,
                       deployment_run_id: Optional[str] = None) -> Optional[str]:
    """The words refusing a production release, or None when the build passes the production
    bar. FAIL CLOSED: no build to judge, no gate record, or a record that cannot be read is
    "nothing was judged for this build" and refuses.

    The build is named by `code_task_run_id`, or found from `deployment_run_id` (a config
    promotion: the deployment's release packet names its build or its workflow run), or from
    `workflow_run_id`. A config promotion with NO build behind it at all (a config-only workflow)
    has nothing for the bar to judge and is not refused here; a build that IS behind it must
    have been judged."""
    import json

    from sqlalchemy import text

    config_only_ok = False
    try:
        if not code_task_run_id and deployment_run_id:
            config_only_ok = True
            dep = connection.execute(text(
                "SELECT release_packet FROM deployment_runs WHERE id = :i"),
                {"i": deployment_run_id}).scalar()
            packet = json.loads(dep) if isinstance(dep, str) else (dep or {})
            code_task_run_id = str(packet.get("code_task_run_id") or "") or None
            workflow_run_id = workflow_run_id or (str(packet.get("workflow_run_id") or "") or None)
        if not code_task_run_id and workflow_run_id:
            code_task_run_id = connection.execute(text(
                "SELECT id FROM code_task_runs WHERE project_id IS NOT NULL AND "
                "(finalize_ctx->>'workflow_run_id' = :w OR finalize_ctx->'origin'->>'workflow_run_id' = :w) "
                "ORDER BY created_at DESC LIMIT 1"), {"w": workflow_run_id}).scalar()
        if not code_task_run_id:
            return None if config_only_ok else NOT_JUDGED.format(why="no build could be found")
        run = connection.execute(text(
            "SELECT finalize_ctx FROM code_task_runs WHERE id = :i"),
            {"i": code_task_run_id}).mappings().first()
        if run is None:
            return NOT_JUDGED.format(why=f"build {code_task_run_id} was not found")
        fctx = run["finalize_ctx"]
        fctx = json.loads(fctx) if isinstance(fctx, str) else (fctx or {})
        workflow_run_id = workflow_run_id or _workflow_run_of(fctx)
        judging, needs, why = judging_run(connection, code_task_run_id)
        if why:
            return NOT_JUDGED.format(why=why)
    except Exception:  # noqa: BLE001 — unreadable evidence is not evidence of a pass
        logger.warning("quality bar: production rule could not be evaluated", exc_info=True)
        return NOT_JUDGED.format(why="its quality-gate record could not be read")
    try:
        with connection.begin_nested():
            needs += pass_two_needs(connection, workflow_run_id)
    except Exception:  # noqa: BLE001
        logger.warning("quality bar: pass 2 not read", exc_info=True)
        needs.append("Pass 2 (More tests) could not be read; production waits for it.")
    if not needs:
        return None
    listed = "\n".join(f"- {n}" for n in needs[:40])
    unrun = any("was not run" in n for n in needs)
    return ("This is a production release, so the production quality bar applies: this build "
            "passed at a lighter bar and these are still needed. Fix them (Fix these, or the "
            "follow-up build) and release again:\n" + listed
            + (f"\n{RECHECK_ACTION}" if unrun else ""))


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


def _reships_a_prior_production_run(connection: Any, target: Mapping[str, Any],
                                    commit_ref: Optional[str], reships: Optional[str]) -> bool:
    """TRUSTED (never a request or packet field): `reships` is the prior deployment run a
    redeploy/rollback was built from by delivery_pipeline.redeploy. It exempts the release only
    when that run was on THIS target, in production, and shipped EXACTLY this commit: it passed
    the bar when it was planned. A different commit is a new release and is judged."""
    if not (reships and commit_ref and (target or {}).get("id")):
        return False
    from sqlalchemy import text

    prior = connection.execute(text(
        "SELECT deployment_target_id, environment, commit_ref FROM deployment_runs WHERE id = :i"),
        {"i": str(reships)}).mappings().first()
    return bool(prior and str(prior["deployment_target_id"]) == str(target["id"])
                and is_production(prior["environment"])
                and str(prior["commit_ref"] or "").strip()
                and str(prior["commit_ref"]).strip() == str(commit_ref).strip())


_HEX = __import__("re").compile(r"^[0-9a-f]{7,40}$")

NOT_THE_JUDGED_COMMIT = ("the commit is not the one the build judged ({given} is not among the "
                         "build's judged commits: {known}). A production release ships the commit "
                         "its named build passed the bar on; release that commit, or re-check "
                         "this one.")


def build_commits(connection: Any, run_id: str) -> list[str]:
    """The commits a build judged, best first: its recorded `final_head_sha`, its merge commit,
    and the head its pull request / merge card recorded (`recommendation.head_sha`)."""
    import json

    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT r.finalize_ctx, d.recommendation FROM code_task_runs r "
        "LEFT JOIN decisions d ON d.id = r.decision_id WHERE r.id = :i"), {"i": run_id}
    ).mappings().first()
    if row is None:
        return []

    def j(v: Any) -> dict:
        v = json.loads(v) if isinstance(v, str) else v
        return v if isinstance(v, dict) else {}

    fctx, rec = j(row["finalize_ctx"]), j(row["recommendation"])
    merged = str((fctx.get("merge") or {}).get("sha") or "").strip().lower()
    if _HEX.match(merged):
        # A MERGED build ships what main holds: its merge commit (judged through its pull
        # request). The pre-merge PR head is not on main after a squash, so it is not accepted.
        return [merged]
    found = [fctx.get("final_head_sha"), rec.get("head_sha")]
    return [str(x).strip().lower() for x in found
            if x and _HEX.match(str(x).strip().lower())]


def _commit_check(connection: Any, run_id: str, commit_ref: Optional[str],
                  also_judged: Iterable[str] = ()) -> tuple[Optional[str], Optional[str]]:
    """(refusal, the commit to ship) for a release of build `run_id` at `commit_ref`. A release
    ships the commit its build judged: a given commit must be one of the build's; a release with
    no commit, or a named ref (a branch such as `main`), takes the build's own commit. A build that
    recorded no commit cannot be verified and is refused."""
    judging = judging_run(connection, run_id)[0] or run_id
    known = build_commits(connection, judging)
    if judging != run_id:
        # CLEARED BY A RE-CHECK: a MERGED build still ships its merge commit (what main holds,
        # judged through its pull request and the re-check); an unmerged one ships a commit the
        # re-check judged.
        own = build_commits(connection, run_id)
        row = connection.execute(__import__("sqlalchemy").text(
            "SELECT finalize_ctx->'merge'->>'sha' FROM code_task_runs WHERE id = :i"),
            {"i": run_id}).scalar()
        if row and _HEX.match(str(row).strip().lower()):
            known = own
    known = known + [
        str(x).strip().lower() for x in also_judged or () if _HEX.match(str(x).strip().lower())]
    given = str(commit_ref or "").strip().lower()
    if not known:
        return (NOT_JUDGED.format(why="the build recorded no commit, so the release cannot be "
                                      "checked against what it judged"), None)
    if not given or not _HEX.match(given):
        return None, known[0]
    if any(k.startswith(given) or given.startswith(k) for k in known):
        return None, str(commit_ref).strip()
    return NOT_THE_JUDGED_COMMIT.format(given=given[:12], known=", ".join(k[:12] for k in known)), None


def plan_decision(connection: Any, *, target: Mapping[str, Any],
                  release_packet: Optional[Mapping[str, Any]],
                  commit_ref: Optional[str] = None,
                  reships: Optional[str] = None,
                  also_judged: Iterable[str] = ()) -> tuple[Optional[str], Optional[str]]:
    """THE ONE PLACE every production deployment passes (ResourcePlaneService.plan_deployment_run):
    (refusal, commit to ship) for a release to a production target, whichever door it came
    through (a deploy, a promotion, a manual deploy, the API, a skill).

    The ONLY exemptions: a redeploy/rollback that re-ships the exact commit of a prior production
    deployment on this target (`reships`, passed by trusted code only, verified here), and a
    config-only target (a directory of files: its own promotion checks apply). Every other
    production release must name a build that passes the production bar (the packet's
    `code_task_run_id`, or for a promotion the build behind the deployment it was promoted from)
    AND ship the commit that build judged (`_commit_check`). A packet's own `source`
    ("redeploy"...) exempts nothing. No build named is refused."""
    if not is_production((target or {}).get("environment")):
        return None, None
    if str((target or {}).get("target_kind") or "").strip().lower() == "config_dir":
        return None, None
    if _reships_a_prior_production_run(connection, target, commit_ref, reships):
        return None, None
    packet = dict(release_packet or {})
    run_id = str(packet.get("code_task_run_id") or "") or None
    if not run_id:
        # A PROMOTION: the build is behind the deployment it was promoted from. The staging
        # deployment the orchestrator plans for the promotion carries no build of its own, so the
        # original staged deployment (`promoted_from_run`) is read first.
        import json

        from sqlalchemy import text

        for dep_id in (packet.get("promoted_from_run"), packet.get("promoted_from"),
                       (packet.get("staging_gate") or {}).get("deployment_run_id")):
            if not dep_id:
                continue
            found = connection.execute(text(
                "SELECT release_packet FROM deployment_runs WHERE id = :i"),
                {"i": str(dep_id)}).scalar()
            found = json.loads(found) if isinstance(found, str) else (found or {})
            if found.get("code_task_run_id"):
                run_id = str(found["code_task_run_id"])
                break
    if not run_id:
        return NOT_JUDGED.format(why="this production release names no build"), None
    refused = production_refusal(connection, code_task_run_id=run_id)
    if refused:
        return refused, None
    return _commit_check(connection, run_id, commit_ref, also_judged)


def plan_refusal(connection: Any, *, target: Mapping[str, Any],
                 release_packet: Optional[Mapping[str, Any]],
                 commit_ref: Optional[str] = None,
                 reships: Optional[str] = None) -> Optional[str]:
    """The refusal half of `plan_decision`."""
    return plan_decision(connection, target=target, release_packet=release_packet,
                         commit_ref=commit_ref, reships=reships)[0]


def _stack_rollback_ok(connection: Any, target_id: str, rollback_of: Optional[str],
                       ref: Optional[str]) -> bool:
    """TRUSTED (rollback_stack_deploy): a stack rollback is exempt only when it ships the previous
    healthy commit of THIS target, earlier than the deployment being rolled back."""
    if not (rollback_of and ref):
        return False
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT target_id, created_at FROM stack_deployments WHERE id = :i"),
        {"i": rollback_of}).mappings().first()
    if row is None or str(row["target_id"]) != str(target_id):
        return False
    given = str(ref).strip().lower()
    for (sha,) in connection.execute(text(
            "SELECT commit_sha FROM stack_deployments WHERE target_id = :t AND status = 'healthy' "
            "AND apply_started_at IS NOT NULL AND created_at < :c"),
            {"t": target_id, "c": row["created_at"]}):
        sha = str(sha or "").strip().lower()
        if sha and (sha.startswith(given) or given.startswith(sha)):
            return True
    return False


def stack_plan_decision(connection: Any, *, target: Mapping[str, Any], run_id: Optional[str],
                        ref: Optional[str], rollback_of: Optional[str] = None,
                        also_judged: Iterable[str] = ()) -> tuple[Optional[str], Optional[str]]:
    """THE SAME CHOKEPOINT for a STACK plan (plan_stack_deploy): (refusal, commit to ship). A
    production stack release must name a build that passes the production bar and ship the commit
    it judged; the only exemption is a rollback to the target's previous healthy commit."""
    if not is_production((target or {}).get("environment")):
        return None, None
    if _stack_rollback_ok(connection, (target or {}).get("id"), rollback_of, ref):
        return None, None
    return plan_decision(connection, target=target,
                         release_packet={"code_task_run_id": run_id} if run_id else {},
                         commit_ref=ref, also_judged=also_judged)
