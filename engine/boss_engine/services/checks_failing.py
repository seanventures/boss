"""A build whose checks still fail after its repair rounds opens its pull request anyway.

THE OPERATOR'S RULE (2026-10-17). HelpDesk AI's first build sat parked for hours as "Checks
failed · needs waiver" on one SSRF finding: the build had done its work, its fix rounds were
spent, and nothing moved until a person came to waive. A failing check must never stop a build
to wait for a waiver. So, after the repair rounds:

1. the pull request opens anyway: a DRAFT where the git host has drafts, labelled
   `checks failing`, its description opening with every failing gate and every open finding,
   word for word, security first;
2. the failing gates and findings become the build's follow-up task (the deferred-findings
   mechanism, governed_code_task_service._spawn_deferred_findings_task);
3. the workflow goes on to its next step, the person's merge decision, whose card says that the
   checks are failing (security flagged) and which merges only when a person says so, with a
   reason (decisions.transition: an approval of it is a forced merge, never a silent one);
4. the build reads "PR open · checks failing", never "Checks failed · needs waiver".

The gates themselves are untouched: each one is judged exactly as before, and its verdict, its
findings and its failure are all on the run, the pull request and the card. Only the stop moved.

Not opened, whatever the setting: a build that needs a person to set a secret before it can pass
(that is an act, not a waiver), and an applied template's two gates (`template_tests`,
`no_committed_secrets`: their hold keeps every push back, so nothing of such a build reaches a
branch). A run that opens no pull request at all (open_pr=false, a non-final slice) has none to
open either.

The project setting `open_pr_when_checks_fail` (default 1, BOSS_OPEN_PR_WHEN_CHECKS_FAIL) turns
it off: 0 is the waiver stop as it was.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

#: The project setting (project_settings.KNOBS) and its Settings attribute.
SETTING = "open_pr_when_checks_fail"
#: The label the pull request carries on GitHub.
LABEL = "checks failing"
#: The key on the run (finalize_ctx), the decision (recommendation) and the step's output.
KEY = "checks_failing"
#: The resolve_gate_failure_action outcome for "open the PR with its checks failing".
ACTION = "ship_failing"
#: Events.
OPENED_EVENT = "code_task.pr_opened_with_checks_failing"
MERGE_EVENT = "decision.merged_with_checks_failing"

#: Gates whose failure is about security: listed (and flagged) first everywhere.
SECURITY_GATES = frozenset({"security_clean", "no_committed_secrets"})
#: Each judgment gate's seat and the evidence key holding that seat's own words.
SEAT_OF_GATE: dict[str, tuple[str, str]] = {
    "security_clean": ("security_check", "security_summary"),
    "code_review_passed": ("review_code", "review_summary"),
    "consistent": ("consistency_check", "consistency_summary"),
    # The acceptance check (acceptance_gate.py): each missing must-have as a FINDING line.
    "acceptance_met": ("acceptance_check", "acceptance_summary"),
}
#: The gates that never open a pull request past themselves (see the module docstring).
#: `input_fidelity` (input_fidelity.py): a remediation whose output is a copied answer or adds
#: what the input never had is not a draft to merge later — it is the wrong file.
HELD_GATES = frozenset({"template_tests", "no_committed_secrets", "input_fidelity"})
#: How much of one seat's prose is carried when it wrote no FINDING lines.
MAX_SEAT_CHARS = 3_000
MAX_FINDINGS = 40


def enabled(connection: Any, settings: Any, project_id: Optional[str]) -> bool:
    """The project's choice, else the deployment's (default on). Never raises: a setting that
    cannot be read is the default, and the default is the operator's rule."""
    from . import project_settings

    value: Any = None
    try:
        if connection is not None and project_id:
            with connection.begin_nested():
                value = project_settings.effective(connection, project_id, settings, SETTING)
        else:
            value = getattr(settings, SETTING, 1)
    except Exception:  # noqa: BLE001
        value = getattr(settings, SETTING, 1)
    try:
        return int(1 if value in (None, "") else value) == 1
    except (TypeError, ValueError):
        return True


def enabled_for(engine: Any, settings: Any, project_id: Optional[str]) -> bool:
    """`enabled`, opening its own short transaction."""
    try:
        with engine.begin() as connection:
            return enabled(connection, settings, project_id)
    except Exception:  # noqa: BLE001
        return enabled(None, settings, project_id)


def may_open(failing: Iterable[str], *, faults: Optional[Mapping[str, str]] = None,
             open_pr: bool = True) -> bool:
    """Whether a build stopped on these failing gates opens its pull request instead.

    No for a build that opens no PR at all, one waiting on a person's act (a secret only they
    can set: classify_gate_faults PERSON), and one whose applied template's gate failed."""
    failing = [str(g) for g in failing or ()]
    if not open_pr or not failing:
        return False
    if faults and any(faults.get(g) == "PERSON" for g in failing):
        return False
    return not (set(failing) & HELD_GATES)


#: The stop when BOSS has made all its own attempts on the lineage (lineage_cap.REASON).
_ATTEMPTS_SPENT = "attempts_spent"
#: Supervisor stop reasons whose failures a follow-up build would only hit again.
NO_FOLLOWUP_REASONS = ("environment_only", "refusal")
_STOP_WORDS = {"environment_only": "environment problem", "refusal": "an agent refused",
               "person": "needs a person"}
#: The decision a person-stopped build waits on: "Retry once set" re-runs its gates on the same
#: branch and pull request (decisions: approve → retry_code_task, by the person).
SECRET_RETRY_ACTION = "retry_when_secret_set"


def environment_stop(failing: Iterable[str], *, faults: Optional[Mapping[str, str]] = None,
                     reason: Optional[str] = None) -> Optional[str]:
    """Why a stop must NOT queue a follow-up, or None: the run supervisor stopped for
    `environment_only` or `refusal`, or every failing gate's fault is the environment
    (HARNESS) or a person's act (PERSON). A follow-up would hit the same broken environment or
    wait on the same person; `repeated_failure` and `stalled` still get their one follow-up."""
    if reason in NO_FOLLOWUP_REASONS:
        return str(reason)
    failing = [str(g) for g in failing or ()]
    kinds = {(faults or {}).get(g) for g in failing}
    if failing and kinds and kinds <= {"HARNESS", "PERSON"}:
        return "refusal" if kinds == {"PERSON"} else "environment_only"
    return None


def stopped_record(reason: str, failing: Iterable[str], *,
                   needs: Optional[Iterable[str]] = None,
                   why: Optional[str] = None) -> dict[str, Any]:
    """What the PR and its card say about such a stop, in the words a person acts on:

      a secret only a person can set (`needs`, the app_serves check's `needs_secret`) —
        "stopped: needs a person, App starts. Set STRIPE_KEY for environments … in the app's
        Secrets, then press Retry …" (reason `person`: the build waits for that person);
      an agent refusing — "stopped: an agent refused: <its reason>";
      the environment — "stopped: environment problem, <gates>"."""
    labels = ", ".join(_gate_label(g) for g in _ordered(failing))
    needs = [str(n) for n in (needs or ()) if str(n).strip()]
    if needs:
        from ..templates.run_env import needs_secret_action

        action = needs_secret_action(needs)
        return {"reason": "person", "no_followup": True, "needs_secret": needs,
                "action": action, "words": f"stopped: needs a person, {labels}. {action}"}
    if reason == _ATTEMPTS_SPENT:
        # ONE CAP PER LINEAGE (lineage_cap): BOSS made its attempts; a person decides next.
        from .lineage_cap import NEXT_STEP, words

        return {"reason": reason, "no_followup": True, "action": NEXT_STEP,
                "words": f"stopped: {words()}, {labels}. {NEXT_STEP}"}
    if reason == "refusal":
        said = " ".join(str(why or "").split())[:300] or labels
        return {"reason": reason, "no_followup": True,
                "words": f"stopped: an agent refused: {said}"}
    return {"reason": reason, "no_followup": True,
            "words": f"stopped: {_STOP_WORDS.get(reason, reason)}, {labels}"}


def wait_for_person(connection: Any, *, run_id: str, project_id: str, task_id: Optional[str],
                    record: Mapping[str, Any], pull_request_url: Optional[str]) -> Optional[str]:
    """A build stopped on a secret only a person can set has opened its draft PR; it now WAITS
    for that person instead of ending: status `waiting_on_person` (phase `awaiting_secret`) and
    one card saying what to set and where, whose "Retry once set" runs the gates again on the
    same branch and pull request. Returns the card's id (None when there is nothing to set)."""
    from sqlalchemy import text

    from .core import create_blocking_decision, emit_event

    stopped = dict((record or {}).get("stopped") or {})
    needs = list(stopped.get("needs_secret") or [])
    if stopped.get("reason") != "person" or not needs:
        return None
    action = str(stopped.get("action") or "")
    names = ", ".join(needs)
    decision_id = create_blocking_decision(
        connection, project_id, task_id,
        f"Set {names}, then retry the build" + (" (its draft pull request is open)"
                                               if pull_request_url else ""),
        {"action": SECRET_RETRY_ACTION, "run_id": run_id, "needs_secret": needs,
         "next_step": action, "pull_request_url": pull_request_url,
         "summary": f"{action} The build's checks run again on the same branch and pull "
                    "request when you press Retry once set."},
        {"phase": "app_serves"}, f"{stopped.get('words') or ''}", risk_tier=1)
    connection.execute(text(
        "UPDATE decisions SET deadline_at = NULL WHERE id = :d"), {"d": decision_id})
    connection.execute(text(
        "UPDATE code_task_runs SET status = 'waiting_on_person', phase = 'awaiting_secret', "
        "error = :e, finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
        "|| jsonb_build_object('secret_wait_decision_id', CAST(:d AS TEXT)), updated_at = now() "
        "WHERE id = :id"),
        {"e": f"Waiting for a person: {action}"[:2000], "d": decision_id, "id": run_id})
    emit_event(connection, project_id, "code_task.waiting_for_secret", "boss", "code_task_run",
               run_id, {"needs_secret": needs, "decision_id": decision_id,
                        "pull_request_url": pull_request_url})
    return decision_id


def stop_or_open(action: str, failing: Iterable[str], *,
                 faults: Optional[Mapping[str, str]] = None, open_pr: bool = True,
                 enabled: bool = True, reason: Optional[str] = None) -> str:
    """THE ONE EXIT FROM A GATE STOP (BOSSAPP-1319). Whatever decided to stop the build — the
    gate disposition (resolve_gate_failure_action), the run supervisor's stop_report at its
    round cap, an inconclusive check whose escalations are spent — a ``"block"`` that
    `may_open` allows becomes ``ACTION``: the PR opens with its checks failing and a follow-up
    starts for them. Any other action is returned unchanged.

    crun_f5868a67 (northgate-gateway, 2026-11-16): its fourth round failed only acceptance_met,
    the supervisor's round cap judged stop_report, and that short-circuit set "block" without
    ever reaching this rule, so the build parked at awaiting_gate_override with no follow-up,
    while the two rounds before it (under the cap) each opened one."""
    if action == "block" and enabled and may_open(failing, faults=faults, open_pr=open_pr):
        return ACTION
    # AN ENVIRONMENT OR REFUSAL STOP STILL OPENS THE PR (the operator's rule: a PR always opens),
    # labelled, with no follow-up (environment_stop); held gates still hold.
    failing = [str(g) for g in failing or ()]
    if (action == "block" and enabled and open_pr and failing
            and not (set(failing) & HELD_GATES)
            and environment_stop(failing, faults=faults, reason=reason)):
        return ACTION
    return action


def _gate_label(gate: str) -> str:
    """A FAILING gate in words, as what went wrong ("Review asked for changes"). Never its pass
    label: this record exists only for gates that FAILED, and "Review passed, Must-haves met"
    in a card titled "its checks fail" read as success (BOSSAPP-1354)."""
    from .plain_words import gate_failed_words

    return gate_failed_words(gate)


def _ordered(gates: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for g in gates or ():
        g = str(g)
        if g and g not in seen:
            seen.append(g)
    return sorted(seen, key=lambda g: 0 if g in SECURITY_GATES else 1)


def _seat_words(summary: str) -> str:
    """A seat's own words when it wrote no FINDING lines: its Findings section (else the whole
    summary) without the verdict line, bounded — verbatim, never paraphrased."""
    import re

    body = str(summary or "")
    cut = body.find("## Findings")
    body = body[cut:] if cut >= 0 else body
    body = re.sub(r"(?im)^\s*VERDICT:\s*\w+\s*$", "", body).strip()
    if len(body) > MAX_SEAT_CHARS:
        body = body[:MAX_SEAT_CHARS].rstrip() + " …"
    return body


def collect(failing: Iterable[str], evidence: Optional[Mapping[str, Any]] = None, *,
            details: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """The record of what is failing, security first: every failing gate (with its one-line
    detail where the build has one) and every open finding of each failing judgment seat, in
    the seat's own words.

    {"gates": [{gate, label, security, detail?}], "findings": [{gate, stage, security,
     severity, where?, text}], "security": bool}"""
    from .review_comments import parse_findings

    evidence = evidence or {}
    details = details or {}
    gates = _ordered(failing)
    out_gates = []
    for g in gates:
        entry: dict[str, Any] = {"gate": g, "label": _gate_label(g),
                                 "security": g in SECURITY_GATES}
        if details.get(g):
            entry["detail"] = str(details[g])[:1200]
        out_gates.append(entry)
    findings: list[dict[str, Any]] = []
    for g in gates:
        seat = SEAT_OF_GATE.get(g)
        if not seat:
            continue
        stage, key = seat
        summary = str(evidence.get(key) or "")
        parsed = parse_findings(summary, stage=stage)
        if parsed:
            for c in sorted(parsed, key=lambda c: -c.blocking_rank):
                findings.append({"gate": g, "stage": stage, "security": g in SECURITY_GATES,
                                 "severity": c.severity,
                                 **({"where": c.location()} if c.anchored else {}),
                                 "text": c.body})
        elif summary.strip():
            findings.append({"gate": g, "stage": stage, "security": g in SECURITY_GATES,
                             "severity": "finding", "text": _seat_words(summary)})
    return {"gates": out_gates, "findings": findings[:MAX_FINDINGS],
            "security": any(x["security"] for x in out_gates)}


def gate_names(record: Optional[Mapping[str, Any]]) -> list[str]:
    return [str(g.get("gate")) for g in (record or {}).get("gates") or [] if g.get("gate")]


def headline(record: Optional[Mapping[str, Any]]) -> str:
    """One line of what failed: "Security findings open, New tests fail" — security first."""
    # From the gate KEY, never the stored label: a record written before BOSSAPP-1354 carries
    # the pass label ("Review passed"), and reading it here put it back in every card title.
    labels = [_gate_label(str(g.get("gate"))) if g.get("gate") else str(g.get("label") or "")
              for g in (record or {}).get("gates") or [] if isinstance(g, Mapping)]
    return ", ".join(x for x in labels if x)


def for_work(raw: Any) -> Optional[dict[str, Any]]:
    """THE WORK ROW'S AND PANEL'S VIEW OF THE SAME RECORD THE MERGE CARD SHOWS (enterprise
    install 2026-11-20: the Work panel said "Ready to merge. Change #3 is built, tested and
    reviewed." over a pull request opened with its checks failing, while its merge card said
    CHECKS FAILING). The merge decision's `checks_failing` (what the card leads with), else the
    run's own: {gates: [gate keys], security: bool, words: 'security, tests'}, or None when
    nothing is failing. `words` names each gate as the check it is (factory_view.gate_noun),
    security first: its pass-label ("Review passed") beside "failing" read as a success. Accepts
    the stored JSON as text. Never raises."""
    import json

    rec = raw
    if isinstance(rec, (str, bytes)):
        try:
            rec = json.loads(rec)
        except ValueError:
            return None
    if not isinstance(rec, Mapping):
        return None
    entries = [g if isinstance(g, Mapping) else {"gate": str(g)}
               for g in rec.get("gates") or [] if g]
    names = [str(g.get("gate")) for g in entries if g.get("gate")]
    if not names:
        return None
    from .factory_view import gate_noun

    ordered = sorted(dict.fromkeys(names), key=lambda n: 0 if n in SECURITY_GATES else 1)
    return {"gates": ordered,
            "security": bool(rec.get("security")) or any(n in SECURITY_GATES for n in names),
            "words": ", ".join(gate_noun(n) for n in ordered)}


def tip(record: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The Factory row's tooltip: what is failing, security flagged."""
    if not record or not record.get("gates"):
        return None
    lead = "Security finding open · " if record.get("security") else ""
    return (lead + headline(record))[:120]


def pr_section(record: Optional[Mapping[str, Any]], *, followup_run_id: Optional[str] = None
               ) -> str:
    """The PR description's first section: each failing gate and finding, word for word,
    security first. Empty when nothing is failing."""
    if not record or not record.get("gates"):
        return ""
    lines = ["## Checks failing",
             "",
             "BOSS's checks still fail after the build's repair rounds. This pull request is "
             "open so the work can be read; it is a draft and labelled `checks failing`, and "
             "BOSS merges it only when a person decides to, with a reason. "
             + ("No follow-up build is queued: it would hit the same problem."
                if (record.get("stopped") or {}).get("no_followup")
                else "The failing gates and findings are queued as a follow-up build."),
             ""]
    if record.get("stopped"):
        said = str(record["stopped"].get("words") or "")
        lines += [f"**{said[:1].upper()}{said[1:].rstrip('.')}.**", ""]
    if record.get("security"):
        lines += ["**Security:** a security check is failing. Read the security findings "
                  "first.", ""]
    lines.append("### Failing gates")
    for g in record.get("gates") or []:
        flag = "SECURITY · " if g.get("security") else ""
        detail = f": {g['detail']}" if g.get("detail") else ""
        lines.append(f"- {flag}**{g.get('label') or g.get('gate')}** (`{g.get('gate')}`){detail}")
    found = record.get("findings") or []
    if found:
        lines += ["", "### Open findings"]
        for f in found:
            flag = "SECURITY · " if f.get("security") else ""
            where = f" `{f['where']}`" if f.get("where") else ""
            text = str(f.get("text") or "").strip()
            if "\n" in text:
                quoted = "\n".join(f"  > {ln}" for ln in text.splitlines())
                lines.append(f"- {flag}[{str(f.get('severity')).upper()}]"
                             f" ({f.get('stage')}){where}\n{quoted}")
            else:
                lines.append(f"- {flag}[{str(f.get('severity')).upper()}]"
                             f" ({f.get('stage')}){where} {text}")
    if followup_run_id:
        lines += ["", f"Follow-up build: `{followup_run_id}`."]
    return "\n".join(lines).rstrip() + "\n\n"


def followup_findings(record: Optional[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The follow-up task's findings ({severity, text}), security first: each failing gate,
    then each open finding verbatim."""
    out: list[dict[str, str]] = []
    for g in (record or {}).get("gates") or []:
        detail = f": {g['detail']}" if g.get("detail") else ""
        out.append({"severity": "security" if g.get("security") else "blocker",
                    "text": f"Failing gate '{g.get('gate')}' ({g.get('label')}) when the PR "
                            f"opened{detail}"[:600]})
    for f in (record or {}).get("findings") or []:
        where = f" {f['where']}" if f.get("where") else ""
        sev = "security" if f.get("security") else str(f.get("severity") or "major")
        out.append({"severity": sev,
                    "text": f"({f.get('stage')}){where} {str(f.get('text') or '').strip()}"})
    return out


def decision_summary(record: Optional[Mapping[str, Any]]) -> str:
    """The merge card's lead sentence."""
    if not record or not record.get("gates"):
        return ""
    sec = (" Security finding open: read the security findings first."
           if record.get("security") else "")
    stopped = record.get("stopped") or {}
    if stopped.get("no_followup"):
        return (f"CHECKS FAILING ({stopped.get('words')}).{sec} The pull request opened anyway; "
                "no follow-up build is queued, since it would hit the same problem. Merging it "
                "is your decision: say why when you merge it on this card.")
    return (f"CHECKS FAILING: {headline(record)}.{sec} The pull request opened anyway after "
            "the repair rounds; the failing checks are queued as a follow-up build. Merging "
            "it is your decision: say why when you merge it on this card.")


def merge_refusal(record: Mapping[str, Any]) -> str:
    """Why an approval without a person's stated reason is refused."""
    return (f"Checks are failing on this pull request ({headline(record)})"
            + (", including a security finding" if record.get("security") else "")
            + ". BOSS does not merge it silently: merge with checks failing on this card and "
              "say why (a forced merge with a note), or keep it blocked while the follow-up "
              "build fixes them.")


def of_decision(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The checks-failing record a merge decision carries, else None."""
    rec = row.get("recommendation") if isinstance(row, Mapping) else None
    if isinstance(rec, str):
        import json

        try:
            rec = json.loads(rec)
        except ValueError:
            rec = None
    found = (rec or {}).get(KEY) if isinstance(rec, Mapping) else None
    return dict(found) if isinstance(found, Mapping) and found.get("gates") else None


def flag_existing_decision(connection: Any, project_id: str, pull_request_url: str,
                           record: Mapping[str, Any], *,
                           decision_id: Optional[str] = None) -> Optional[str]:
    """Tell an EXISTING open merge card that its checks are failing (a parked build's PR,
    opened by the exit rescue with an ordinary card): it then leads with the failing checks and
    merges only on a person's stated reason, like a card opened with them. Returns its id."""
    import json

    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT id FROM decisions WHERE project_id = :p "
        "AND recommendation->>'action' = 'review_pull_request' "
        "AND (id = :d OR recommendation->>'pull_request_url' = :u) "
        "AND status IN ('pending', 'delegated', 'expired', 'revision_requested') "
        "ORDER BY (id = :d) DESC, created_at DESC LIMIT 1"),
        {"p": project_id, "d": decision_id or "", "u": pull_request_url}).first()
    if row is None:
        return decision_id
    found = str(row[0])
    flagged = {**dict(record), "draft": False, "label": LABEL}
    connection.execute(text(
        # A card that read "ready — N follow-ups" (quality_bar.merge_followups) now leads with
        # its failing checks instead: its follow-ups record goes, so no surface says both.
        "UPDATE decisions SET recommendation = (COALESCE(recommendation, '{}'::jsonb) - 'followups') "
        "|| jsonb_build_object(CAST(:k AS text), CAST(:r AS JSONB), 'summary', "
        "CAST(:lead AS text) || ' ' || COALESCE(recommendation->>'summary', '')), "
        "question = CASE WHEN question LIKE 'Checks failing%' THEN question "
        "ELSE 'Checks failing — decide the merge: ' || question END, updated_at = now() "
        "WHERE id = :id"),
        {"k": KEY, "r": json.dumps(flagged), "lead": decision_summary(record), "id": found})
    return found
