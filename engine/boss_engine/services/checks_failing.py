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
}
#: The gates that never open a pull request past themselves (see the module docstring).
HELD_GATES = frozenset({"template_tests", "no_committed_secrets"})
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


def _gate_label(gate: str) -> str:
    from .factory_view import gate_label

    return gate_label(gate)


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
    """One line: "Security clean, New tests pass" — security first, as everywhere."""
    labels = [str(g.get("label") or g.get("gate")) for g in (record or {}).get("gates") or []]
    return ", ".join(labels)


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
             "BOSS merges it only when a person decides to, with a reason. The failing gates "
             "and findings are queued as a follow-up build.",
             ""]
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
    return (f"CHECKS FAILING: {headline(record)}.{sec} The pull request opened anyway after "
            "the repair rounds; the failing checks are queued as a follow-up build. Merging "
            "it is your decision: say why when you merge.")


def merge_refusal(record: Mapping[str, Any]) -> str:
    """Why an approval without a person's stated reason is refused."""
    return (f"Checks are failing on this pull request ({headline(record)})"
            + (", including a security finding" if record.get("security") else "")
            + ". BOSS does not merge it silently: merge with checks failing and say why "
              "(a forced merge with a note), or keep it blocked while the follow-up build "
              "fixes them.")


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
        "UPDATE decisions SET recommendation = COALESCE(recommendation, '{}'::jsonb) "
        "|| jsonb_build_object(CAST(:k AS text), CAST(:r AS JSONB), 'summary', "
        "CAST(:lead AS text) || ' ' || COALESCE(recommendation->>'summary', '')), "
        "question = CASE WHEN question LIKE 'Checks failing%' THEN question "
        "ELSE 'Checks failing — decide the merge: ' || question END, updated_at = now() "
        "WHERE id = :id"),
        {"k": KEY, "r": json.dumps(flagged), "lead": decision_summary(record), "id": found})
    return found
