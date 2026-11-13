"""Architecture approval: an architect agent turns the run's APPROVED PRD into an architecture
document, and a person approves it before anything is built.

THE GAP THIS CLOSES. A delivery run had a PRD and a PRD approval, then went straight to the
build. "How is this put together" was decided by whichever engineering agent happened to start
coding, and nobody ever saw it before the pull request. The workaround on a real install was a
second panel and a manual gate, which produced a meeting transcript, not a document a person
could approve or send back.

The shape, modeled on the PRD path:

  * `start` — the workflow step (capability `architecture`). It REFUSES, in words, when the run
    has no PRD upstream or the PRD is not approved: an architecture for a spec nobody agreed to
    is a design for the wrong thing. Otherwise the architect writes version 1, it is stored as an
    `architecture_document` artifact, and a decision (action `review_architecture`) is raised
    showing the document. The step waits on the `architecture_reviews` row.
  * A person answers in Approvals or on the step's panel: Approve, Request changes (a note is
    required), or Reject. `on_decision_answered` records the answer on the review.
  * Request changes: `revise_sent_back`, called by every runner tick, has the architect write
    the next version from the previous one and the note, and raises a fresh decision for it.
  * Approved: the step succeeds carrying the approved document, and every later build step
    receives it in its brief. Rejected: the step fails and the run stops there.

AN AGENT CAN NEVER SATISFY THIS GATE. `require_a_person` refuses an approval by a policy, by
BOSS itself, by the architect who wrote the document, by any agent of the project, and by
anything acting under an agent's run token — before the approval is recorded.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import create_artifact, emit_event, make_id, now_utc

log = logging.getLogger(__name__)

CAPABILITY = "architecture"
ARTIFACT_TYPE = "architecture_document"
#: The recommendation action of the decision a person answers. Classified in decision_actions.
DECISION_ACTION = "review_architecture"
DEFAULT_ARCHITECT = "chief_architect"

#: The sections every architecture document has, in order. (key, heading, what the architect
#: is asked to write under it.) The console renders them in this order, and a missing one is
#: named rather than silently absent.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("context", "Context",
     "What is being built and why, the constraints from the PRD, and what is out of scope."),
    ("components", "Components",
     "Each component or service, its responsibility, and how they talk to each other. Include "
     "a ```mermaid flowchart of the components."),
    ("data_model", "Data model",
     "The entities, their key fields and relationships, and where each is stored."),
    ("apis", "APIs",
     "The interfaces: endpoints or operations, request and response shapes, errors."),
    ("security", "Security and authorization",
     "Authentication, who may do what (authorization model), secrets, data protection."),
    ("deployment", "Deployment",
     "How it runs: processes or containers, configuration, environments, health checks."),
    ("risks", "Risks and trade-offs",
     "What could go wrong, what was traded for what, and how each risk is mitigated."),
    ("decisions", "Decisions and alternatives",
     "Each significant decision: what was decided, the alternatives considered, and why this "
     "one won."),
)
SECTION_KEYS = tuple(key for key, _h, _w in SECTIONS)

#: Words a heading may use for a section, beyond its own heading.
_ALIASES: dict[str, tuple[str, ...]] = {
    "context": ("context", "overview", "background"),
    "components": ("components", "component", "architecture overview", "system design"),
    "data_model": ("data model", "data", "schema", "storage"),
    "apis": ("apis", "api", "interfaces", "endpoints"),
    "security": ("security", "authorization", "authz", "auth"),
    "deployment": ("deployment", "operations", "infrastructure", "runtime"),
    "risks": ("risks", "trade-offs", "tradeoffs", "risk"),
    "decisions": ("decisions", "alternatives", "adrs", "decision"),
}

OPEN = frozenset({"drafting", "awaiting_approval", "revision_requested"})
OK_STATES = frozenset({"approved"})
BAD_STATES = frozenset({"rejected", "cancelled", "failed"})

#: How much of the PRD the architect reads, and how much approved architecture travels into a
#: build brief. Generous: a requirement dropped by truncation is a requirement not designed for.
_PRD_CHARS = 40000
ARCHITECTURE_BRIEF_CHARS = 30000

#: Names that are never a person, whatever a caller sends.
_MACHINE_ACTORS = frozenset({
    "auto_approval", "system", "boss", "metatron", "workflow", "workflow-policy",
    "capability-runner", "worker", "scheduler", "agent",
})


class ArchitectureRefused(Exception):
    """The step cannot run here, in words an operator can act on. Settled: a retry without a
    change upstream would be refused the same way."""


class ArchitectureDraftError(Exception):
    """The architect could not write the document this time (the model failed). Retryable."""


class ArchitectureApprovalRefused(Exception):
    """An approval that is not a person's. Raised before anything is recorded."""


# ── The approved PRD ─────────────────────────────────────────────────────────────────────────

def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def _decision_status(connection: Connection, decision_id: str, project_id: str
                     ) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT id, project_id, task_id, status, approved_by, recommendation "
             "FROM decisions WHERE id = :i AND project_id = :p"),
        {"i": decision_id, "p": project_id},
    ).mappings().first()
    return dict(row) if row else None


def _prd_decision(row: Mapping[str, Any], *, task_id: str, markdown: str) -> bool:
    """A typed publication decision for this task and this exact PRD text."""
    if not task_id or str(row.get("task_id") or "") != task_id:
        return False
    rec = _json(row.get("recommendation"), {}) or {}
    args = rec.get("args") or {}
    recorded = str(args.get("markdown") or "").strip()
    if recorded and recorded != markdown:
        return False  # a prior PRD revision cannot approve the current text
    return (rec.get("action") == "execute_agent_tool" and
            rec.get("tool") == "publish_document" and recorded == markdown)


def approved_prd(connection: Connection, project_id: str, context: Mapping[str, Any],
                 spec_steps: Optional[Mapping[str, Mapping[str, Any]]] = None, *,
                 run_id: Optional[str] = None) -> dict[str, Any]:
    """Read a canonical project PRD and its approval; run input is only a locator."""
    steps = context.get("steps") or {}
    run_input = context.get("input") or {}
    if run_id:
        run = connection.execute(
            text("SELECT input FROM workflow_runs WHERE id=:r AND project_id=:p"),
            {"r": run_id, "p": project_id},
        ).mappings().first()
        if not run:
            raise ArchitectureRefused("The architecture workflow belongs to another project or is missing.")
        run_input = _json(run["input"], {}) or {}
        produced_rows = connection.execute(
            text("SELECT step_id, status, output FROM workflow_step_runs "
                 "WHERE workflow_run_id=:r AND project_id=:p ORDER BY created_at, iteration"),
            {"r": run_id, "p": project_id},
        ).mappings().all()
        steps = {}
        for produced_row in produced_rows:
            steps.pop(produced_row["step_id"], None)
            if produced_row["status"] == "succeeded":
                steps[produced_row["step_id"]] = _json(produced_row["output"], {}) or {}
    source_step = ""
    produced = {}
    for step_id, candidate in steps.items():
        if isinstance(candidate, dict) and str(candidate.get("prd_markdown") or "").strip():
            source_step, produced = str(step_id), candidate
            break
    meeting_id = str(produced.get("meeting_run_id") or
                     run_input.get("adopt_meeting_run_id") or "").strip()
    meeting = connection.execute(
        text("SELECT state, task_id, decision_id FROM meeting_runs "
             "WHERE id = :m AND project_id = :p"),
        {"m": meeting_id, "p": project_id},
    ).mappings().first() if meeting_id else None
    markdown = str((_json(meeting["state"], {}) or {}).get("prd_markdown") or "").strip() \
        if meeting else ""
    claimed = str(produced.get("prd_markdown") or "").strip()
    if not markdown or (claimed and claimed != markdown) or (
            produced.get("task_id") and str(produced["task_id"]) != str(meeting["task_id"])):
        raise ArchitectureRefused(
            "There is no PRD to design from that matches this project and run. Complete a panel, approve its "
            "current PRD, then retry Architecture. A foreign or changed PRD is not approved.")
    task_id = str(meeting["task_id"] or "")
    approval = None
    rejected = False
    current_decision_id = None
    # A task may also have unrelated decisions. Only the exact PRD decision counts.
    rows = connection.execute(
        text("SELECT id, task_id, status, approved_by, recommendation FROM decisions "
             "WHERE project_id = :p AND task_id = :t ORDER BY created_at DESC"),
        {"p": project_id, "t": task_id},
    ).mappings().all()
    for row in rows:
        if not _prd_decision(row, task_id=task_id, markdown=markdown):
            continue
        current_decision_id = row["id"]
        rejected = str(row["status"]) == "rejected"
        if str(row["status"]) in {"approved", "executed", "completed"}:
            approval = {"how": "the panel's PRD decision", "decision_id": row["id"],
                        "approved_by": row["approved_by"]}
        break  # a newer pending/rejected PRD gate is not bypassed by an older one
    # A spec_review of this run must point back to a real approved decision for
    # this PRD, never merely carry an 'approved' output string.
    candidates = []
    for step_id, spec in (spec_steps or {}).items():
        if str((spec.get("binding") or {}).get("capability") or "") == "spec_review":
            out = steps.get(step_id) or {}
            if isinstance(out, dict) and out.get("decision_id"):
                candidates.append((str(out["decision_id"]), "the PRD approval step"))
    if run_input.get("adopt_decision_id"):
        candidates.append((str(run_input["adopt_decision_id"]), "the adopted PRD decision"))
    if approval is None and not rejected:
        for decision_id, how in candidates:
            row = _decision_status(connection, decision_id, project_id)
            if row and (not current_decision_id or row["id"] == current_decision_id) and _prd_decision(row, task_id=task_id, markdown=markdown) and \
                    str(row["status"]) in {"approved", "executed", "completed"}:
                approval = {"how": how, "decision_id": row["id"],
                            "approved_by": row["approved_by"]}
                break
    if approval is None:
        raise ArchitectureRefused(
            "The current project PRD was rejected or has not been approved. Approve its PRD "
            "decision before Architecture; another project's or another document's approval "
            "cannot release this work.")
    import hashlib
    return {"markdown": markdown[:_PRD_CHARS], "source_step": source_step or "adopted panel",
            "meeting_run_id": meeting_id, "task_id": task_id,
            "prd_sha256": hashlib.sha256(markdown.encode()).hexdigest(), **approval}


# workflow_capability_runner.LEASE_SECONDS: the claim a step's tick takes.
_STEP_LEASE_SECONDS = 120


def _renew_step_lease(connection, step_run_id: str, owner: str, attempt) -> bool:
    """Keep this step's claim alive across a model call longer than the claim.

    The tick that claims a step is one transaction and holds the step row locked until it
    commits, so no other worker can take the step while the architect writes. Only the claim's
    clock ran out: a reply that took longer than two minutes came back to "no current
    workflow lease", was kept unsettled, and every retry dead-lettered on it. Renewal is fenced
    exactly as workflows.heartbeat_step (same owner, still leased or running, not expired as
    of this transaction's start) and also on the attempt, so a step another claim took over is
    never renewed.
    """
    renewed = connection.execute(
        text("UPDATE workflow_step_runs SET heartbeat_at = now(), updated_at = now(), "
             "lease_expires_at = clock_timestamp() + make_interval(secs => :ttl) "
             "WHERE id = :id AND lease_owner = :owner AND attempt = :attempt "
             "AND status IN ('leased', 'running') AND lease_expires_at >= now() "
             "RETURNING id"),
        {"id": step_run_id, "owner": owner, "attempt": attempt, "ttl": _STEP_LEASE_SECONDS},
    ).first()
    return renewed is not None


def _draft_authority(connection, project_id, run_id, step_id, *, version, review_id=None,
                     held: Optional[Mapping[str, Any]] = None):
    """Current claimed step, or the exact person-requested revision of a waiting step.

    `held`: the claim this draft already proved (owner and attempt). It is renewed rather
    than refused when only its clock ran out during the model call."""
    row = connection.execute(
        text("SELECT s.id AS step_run_id, r.status AS run_status, r.actor, s.status, "
             "s.lease_owner, s.attempt, "
             "s.lease_expires_at > clock_timestamp() AS lease_live "
             "FROM workflow_runs r JOIN workflow_step_runs s ON s.workflow_run_id = r.id "
             "WHERE r.id = :r AND r.project_id = :p AND s.project_id = :p AND s.step_id = :s "
             "ORDER BY s.iteration DESC LIMIT 1"),
        {"r": run_id, "p": project_id, "s": step_id},
    ).mappings().first()
    if not row or row["run_status"] not in {"running", "waiting"}:
        raise ArchitectureRefused("Architecture run is missing, stopped or belongs to another project.")
    if version == 1 and row["status"] in {"leased", "running"} and row["lease_owner"] \
            and held and row["lease_owner"] == held.get("lease_owner") \
            and row["attempt"] == held.get("attempt"):
        if _renew_step_lease(connection, row["step_run_id"], row["lease_owner"], row["attempt"]):
            return {**dict(row), "lease_live": True}
    if version == 1 and row["status"] in {"leased", "running"} and \
            row["lease_owner"] and row["lease_live"]:
        return dict(row)
    if version > 1 and row["status"] == "waiting" and review_id:
        review = _review(connection, review_id)
        decision = _decision_status(connection, str(review.get("decision_id") or ""), project_id) \
            if review else None
        rec = _json(decision.get("recommendation"), {}) if decision else {}
        if (review and review["project_id"] == project_id and review["workflow_run_id"] == run_id
                and review["step_id"] == step_id and review["status"] == "revision_requested"
                and int(review["current_version"]) + 1 == version and decision
                and decision["status"] == "revision_requested" and rec.get("action") == DECISION_ACTION
                and rec.get("architecture_review_id") == review_id
                and int(rec.get("version") or 0) == version - 1):
            return dict(row)
    raise ArchitectureRefused("Architecture has no current workflow lease or person-requested revision.")


# ── Writing the document ─────────────────────────────────────────────────────────────────────

def architect_adapter(connection: Connection, settings: Any, project_id: str, *,
                      architect: str, run_id: str, brief: str):
    """The model the architect writes with: the project's own route, as the PRD's writer uses.
    Tests replace this function with a scripted model."""
    from . import model_routing
    from .model_router.router import RouteRequest

    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    purpose = RouteRequest(kind="panel", step="panel_facilitator", project_id=project_id,
                           unit_id=run_id, role=architect, text=brief[:6000],
                           ref=f"architecture:{run_id}")
    with model_routing.routing_purpose(purpose):
        return model_routing.project_chat_adapter(connection, settings, project_id)


def _system_prompt(architect: str) -> str:
    return (
        f"You are the {architect.replace('_', ' ')} of this team: the architect who answers for "
        "how the product is put together. You write the architecture document a person will "
        "approve before any code is written. Be concrete and decisive; ground every choice in "
        "the approved PRD; name trade-offs honestly; never invent requirements the PRD does "
        "not state (list them as open questions under Risks instead).")


def _instructions() -> str:
    lines = ["Write the document in markdown with EXACTLY these second-level headings, in this "
             "order, and nothing before the first heading:"]
    for _key, heading, what in SECTIONS:
        lines.append(f"## {heading}\n   {what}")
    lines.append("Under 'Decisions and alternatives', write each decision as a bullet: "
                 "**Decision** — what; **Alternatives** — what else was considered; "
                 "**Why** — the reason this one won.")
    return "\n".join(lines)


def _prompt(prd: str, *, title: str, constraints: str, previous: str = "", note: str = "",
            version: int = 1) -> str:
    parts = [f"Product: {title}" if title else "",
             "THE APPROVED PRD (this is the agreed specification):\n\n" + prd]
    if constraints:
        parts.append("Constraints the workflow gives the architecture:\n" + constraints)
    if previous:
        parts.append(f"YOUR PREVIOUS VERSION (version {version - 1}):\n\n{previous}")
        parts.append("A PERSON REVIEWED IT AND ASKED FOR CHANGES:\n\n" + note + "\n\n"
                     f"Write version {version}: the whole document again, with those changes "
                     "made. Keep what was not asked to change.")
    parts.append(_instructions())
    # WHAT BOSS CAN BUILD, so the architect never designs a stack the build cannot build, and
    # the one line the approval check reads it from (services.stacks).
    from . import stacks

    parts.append(stacks.prompt_block() + "\n" + stacks.STACK_LINE_INSTRUCTION)
    return "\n\n".join(p for p in parts if p)


def _normalise(heading: str) -> str:
    heading = re.sub(r"^[\s#]*", "", heading)
    heading = re.sub(r"^(\d+[.)]?|[ivx]+[.)])\s+", "", heading.strip().lower())
    return heading.strip(" :*_`").replace("&", "and")


def _section_for(heading: str) -> Optional[str]:
    name = _normalise(heading)
    for key, title, _what in SECTIONS:
        if name == title.lower():
            return key
    for key, words in _ALIASES.items():
        if any(name == w or name.startswith(w) for w in words):
            return key
    return None


def parse_sections(markdown: str) -> dict[str, str]:
    """The document's sections by key, from its second-level headings. Text under a heading
    that is not one of SECTIONS stays with the section before it, so nothing is lost."""
    found: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in str(markdown or "").splitlines():
        if line.startswith("## "):
            key = _section_for(line[3:])
            if key is not None:
                current = key
                found.setdefault(key, [])
                continue
        if current is not None:
            found[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in found.items()
            if "\n".join(lines).strip()}


def render(title: str, sections: Mapping[str, str], version: int) -> str:
    """The document as one markdown text, sections in the fixed order."""
    out = [f"# Architecture: {title or 'this product'}", "", f"_Version {version}_", ""]
    for key, heading, _what in SECTIONS:
        body = str(sections.get(key) or "").strip() or (
            "_Not covered by the architect. Request changes to have it added._")
        out += [f"## {heading}", "", body, ""]
    return "\n".join(out).rstrip() + "\n"


def draft(connection: Connection, settings: Any, project_id: str, *, architect: str,
          run_id: str, prd: str, title: str, constraints: str = "", previous: str = "",
          note: str = "", version: int = 1, step_id: str = CAPABILITY,
          review_id: Optional[str] = None, prd_sha256: Optional[str] = None) -> dict[str, Any]:
    """Have the architect write one version. Raises ArchitectureDraftError when the model fails
    or returns nothing — never a placeholder passed off as a design."""
    prompt = _prompt(prd, title=title, constraints=constraints, previous=previous, note=note,
                     version=version)
    held: dict[str, Any] = {}

    def check_work():
        authority = _draft_authority(connection, project_id, run_id, step_id,
                                     version=version, review_id=review_id, held=held or None)
        if not held and version == 1:
            held.update(lease_owner=authority.get("lease_owner"),
                        attempt=authority.get("attempt"))
        current = approved_prd(connection, project_id, {}, run_id=run_id)
        if current["markdown"] != prd or (prd_sha256 and current["prd_sha256"] != prd_sha256):
            raise ArchitectureRefused("The approved PRD changed before architecture submission.")
        return authority

    try:
        check_work()
        adapter = architect_adapter(connection, settings, project_id, architect=architect,
                                    run_id=run_id, brief=prd)
        from . import architecture_spend
        _ref, written, usage = architecture_spend.generate(
            connection, _settings(settings), project_id, run_id=run_id, step_id=step_id,
            adapter=adapter, architect=architect, prompt=prompt, system_prompt=_system_prompt(architect),
            version=version, check_work=check_work,
        )
    except Exception as error:  # noqa: BLE001 — said in words on the step
        from .redaction import redact_secrets
        raise ArchitectureDraftError(
            f"the architect could not write version {version}: {type(error).__name__}: "
            f"{redact_secrets(str(error))[:300]}") from error
    written = str(written or "").strip()
    if not written:
        raise ArchitectureDraftError(
            f"the architect's model returned an empty document for version {version}.")
    sections = parse_sections(written)
    if not sections:
        # No recognised headings: keep the whole text as the context rather than drop it, and
        # say which sections are missing so the reviewer can ask for them.
        sections = {"context": written}
    usage = usage if isinstance(usage, dict) else {}
    try:
        from .openai_adapter import estimate_cost_usd_micros

        cost = int(estimate_cost_usd_micros(usage) or 0)
    except Exception:  # noqa: BLE001 — a cost estimate is never a reason to lose the document
        cost = 0
    return {
        "sections": sections,
        "missing_sections": [k for k in SECTION_KEYS if k not in sections],
        "markdown": render(title, sections, version),
        "model": usage.get("model"),
        "cost_usd_micros": cost,
    }


# ── Reviews ──────────────────────────────────────────────────────────────────────────────────

def _review(connection: Connection, review_id: str, *, lock: bool = False
            ) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM architecture_reviews WHERE id = :i"
             + (" FOR UPDATE" if lock else "")),
        {"i": review_id},
    ).mappings().first()
    return dict(row) if row else None


def _settings(settings: Any) -> Any:
    if settings is not None and getattr(settings, "artifact_root", None) is not None:
        return settings
    from ..config import get_settings

    return get_settings()


def _store_version(connection: Connection, settings: Any, review: Mapping[str, Any],
                   version: int, written: Mapping[str, Any], *, note: str = "",
                   document_base: Optional[str] = None) -> str:
    # THE DOCUMENT BEHIND THE ARCHITECTURE (BOSSAPP-874): every version is also the next revision
    # of one document people can comment on and edit. Written FIRST, from the revision the
    # architect read: a person's edit saved while the architect was writing is a conflict
    # (documents.DocumentConflict) the caller turns into "written again next tick".
    _record_document(connection, review, written["markdown"], base=document_base)
    return create_artifact(
        connection, _settings(settings), review["project_id"], None, ARTIFACT_TYPE,
        f"Architecture v{version}: {review.get('title') or 'this product'}"[:200],
        {
            "architecture_review_id": review["id"],
            "workflow_run_id": review["workflow_run_id"],
            "step_id": review["step_id"],
            "version": version,
            "architect": review["architect"],
            "markdown": written["markdown"],
            "sections": dict(written["sections"]),
            "missing_sections": list(written.get("missing_sections") or []),
            "revised_for": note or None,
            "prd_source": _json(review.get("prd_source"), {}),
            "model": written.get("model"),
            "cost_usd_micros": written.get("cost_usd_micros") or 0,
        },
    )


def _document_ref(review: Mapping[str, Any]) -> str:
    return f"architecture_review:{review['id']}"


def _record_document(connection: Connection, review: Mapping[str, Any], markdown: str, *,
                     base: Optional[str]) -> None:
    """Write the architect's version as the document's next revision. A conflict propagates
    (the revision loop retries); any other failure is logged and never stops the review."""
    from . import documents

    try:
        with connection.begin_nested():
            work_item_id = connection.execute(
                text("SELECT work_item_id FROM workflow_runs WHERE id = :r"),
                {"r": review["workflow_run_id"]}).scalar()
            documents.record_agent_revision(
                connection, project_id=review["project_id"], source_ref=_document_ref(review),
                kind="architecture",
                title=f"Architecture: {review.get('title') or 'this product'}",
                body=markdown, author=f"agent:{review['architect']}", base_revision_id=base,
                work_item_id=work_item_id)
    except documents.DocumentConflict:
        raise
    except Exception:  # noqa: BLE001 — the review's own artifact and decision must stand
        log.warning("architecture document not recorded for %s", review.get("id"), exc_info=True)


def _open_decision(connection: Connection, review: Mapping[str, Any], version: int,
                   artifact_id: str, markdown: str, *, step_label: str, run_title: str,
                   app_name: str, note: str = "") -> str:
    from .action_preview import gate_question
    from .run_decisions import open_step_decision

    revised = f" Version {version} answers: “{note[:300]}”." if note else ""
    decision_id = open_step_decision(
        connection, review["project_id"], None,
        gate_question(f"{step_label} v{version}", run_title),
        {
            "action": "review_architecture",
            "capability": CAPABILITY,
            "architecture_review_id": review["id"],
            "artifact_id": artifact_id,
            "version": version,
            "architect": review["architect"],
            "step_label": step_label,
            "run_title": run_title,
            "app_name": app_name[:120],
            # The document itself, so the Approvals card shows what is being approved.
            "document_markdown": markdown[:ARCHITECTURE_BRIEF_CHARS],
            "gate_completes_step": True,
        },
        {"estimated_usd": 0.0},
        (f"The {review['architect'].replace('_', ' ')} wrote version {version} of the "
         f"architecture from the approved PRD.{revised} Approve it, request changes with a "
         "note, or reject it. Build steps receive the approved version."),
        run_id=review["workflow_run_id"], step_id=review["step_id"], risk_tier=2,
    )
    connection.execute(
        text("UPDATE decisions SET options = CAST(:o AS JSONB) WHERE id = :i"),
        {"i": decision_id, "o": json.dumps([
            {"action": "approve", "label": "Approve"},
            {"action": "revise", "label": "Request changes"},
            {"action": "reject", "label": "Reject"},
        ])},
    )
    return decision_id


def handle(review: Mapping[str, Any]) -> dict[str, Any]:
    """What the step records while it waits on `review`."""
    return {
        "capability": CAPABILITY,
        "tool": None,
        "status": "awaiting",
        "await": {"kind": CAPABILITY, "id": review["id"]},
        "architecture_review_id": review["id"],
        "architecture_version": int(review.get("current_version") or 0),
        "architecture_doc_id": review.get("current_artifact_id"),
        "decision_id": review.get("decision_id"),
        "architect": review.get("architect"),
        "note": status_words(review),
    }


def start(connection: Connection, settings: Any, *, project_id: str, run_id: str,
          step: Mapping[str, Any], context: Mapping[str, Any],
          spec_steps: Optional[Mapping[str, Mapping[str, Any]]] = None,
          constraints: str = "") -> dict[str, Any]:
    """The Architecture step: design from the approved PRD, then wait for a person.

    Idempotent per run and step: a step claimed again while its review is open (a crashed
    worker, a lease that lapsed) waits on the same review rather than writing a second one.
    A review that was rejected or failed is not reused — retrying the step starts a new one.
    """
    step_id = str(step.get("id") or CAPABILITY)
    existing = connection.execute(
        text("SELECT * FROM architecture_reviews WHERE workflow_run_id = :r AND step_id = :s AND project_id = :p "
             "AND status IN ('drafting','awaiting_approval','revision_requested','approved') "
             "ORDER BY created_at DESC LIMIT 1"),
        {"r": run_id, "s": step_id, "p": project_id},
    ).mappings().first()
    if existing and existing["current_artifact_id"]:
        return handle(dict(existing))

    prd = approved_prd(connection, project_id, context, spec_steps, run_id=run_id)
    from .action_preview import run_title as _run_title

    run_input = context.get("input") or {}
    title = _run_title(run_input, str(context.get("workflow_name") or ""))
    app_name = str(run_input.get("app_name") or run_input.get("title") or "").strip()
    architect = str(step.get("captain") or "").strip().lower() or DEFAULT_ARCHITECT
    if architect == "human":
        architect = DEFAULT_ARCHITECT
    step_label = str(step.get("label") or step.get("name") or "Architecture approval").strip()

    written = draft(connection, settings, project_id, architect=architect, run_id=run_id,
                    prd=prd["markdown"], title=app_name or title, constraints=constraints,
                    step_id=step_id, prd_sha256=prd["prd_sha256"])
    review_id = make_id("arch")
    connection.execute(
        text(
            """
            INSERT INTO architecture_reviews (id, project_id, workflow_run_id, step_id, status,
                architect, title, prd_source, current_version)
            VALUES (:i, :p, :r, :s, 'drafting', :a, :t, CAST(:prd AS JSONB), 1)
            """
        ),
        {"i": review_id, "p": project_id, "r": run_id, "s": step_id, "a": architect,
         "t": (app_name or title)[:200],
         "prd": json.dumps({k: v for k, v in prd.items() if k != "markdown"}, default=str)},
    )
    review = _review(connection, review_id) or {}
    artifact_id = _store_version(connection, settings, review, 1, written)
    decision_id = _open_decision(connection, review, 1, artifact_id, written["markdown"],
                                 step_label=step_label, run_title=title, app_name=app_name)
    connection.execute(
        text("UPDATE architecture_reviews SET status = 'awaiting_approval', "
             "current_artifact_id = :a, decision_id = :d, updated_at = now() WHERE id = :i"),
        {"a": artifact_id, "d": decision_id, "i": review_id},
    )
    emit_event(connection, project_id, "architecture.drafted", architect,
               "architecture_review", review_id,
               {"version": 1, "artifact_id": artifact_id, "decision_id": decision_id,
                "workflow_run_id": run_id, "step_id": step_id,
                "missing_sections": written["missing_sections"]})
    return handle(_review(connection, review_id) or {})


# ── Who may approve ──────────────────────────────────────────────────────────────────────────

def _member_type(connection: Connection, project_id: str, actor: str) -> Optional[str]:
    """'human', 'system', or None when `actor` is not a member of the project."""
    try:
        row = connection.execute(
            text("SELECT member_type FROM project_members WHERE project_id = :p "
                 "AND member_id = :a AND status = 'active' LIMIT 1"),
            {"p": project_id, "a": actor},
        ).first()
    except Exception:  # noqa: BLE001 — no member table: nobody is a member
        return None
    return str(row[0]) if row else None


def _is_agent(connection: Connection, project_id: str, actor: str) -> bool:
    from .workflow_capabilities import HUMAN_ROLES, REGISTERED_ROLES

    kind = _member_type(connection, project_id, actor)
    if kind == "human":
        # An approver SEAT a person holds (the ceo, a cto seat) can share its name with an
        # agent role; a human member with that seat is a person.
        return False
    if kind is not None:
        return True  # a system member: BOSS's own principals
    return actor in (REGISTERED_ROLES - HUMAN_ROLES) or bool(connection.execute(
        text("SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r LIMIT 1"),
        {"p": project_id, "r": actor},
    ).first())


def require_a_person(connection: Connection, decision: Mapping[str, Any], actor: str, *,
                     system_policy: bool = False) -> None:
    """Refuse an approval of an architecture that is not a person's. No-op for any other
    decision. Raised BEFORE the approval is recorded."""
    rec = _json(decision.get("recommendation"), {}) or {}
    if not isinstance(rec, dict) or rec.get("action") != DECISION_ACTION:
        return
    who = str(actor or "").strip()
    lowered = who.lower()
    from .decisions import SYSTEM_ACTOR

    why = ""
    if system_policy or who == SYSTEM_ACTOR or lowered in _MACHINE_ACTORS:
        why = f"'{who or 'a policy'}' is BOSS acting by itself, not a person"
    elif lowered.startswith(("agent:", "agent_", "role:")):
        why = f"'{who}' is an agent"
    elif lowered == str(rec.get("architect") or DEFAULT_ARCHITECT).lower():
        why = f"'{who}' wrote this architecture and cannot approve its own document"
    elif _is_agent(connection, str(decision.get("project_id") or ""), lowered):
        why = f"'{who}' is an agent of this project"
    else:
        try:
            from ..identity import lineage

            acting = lineage.current()
        except Exception:  # noqa: BLE001
            acting = None
        if acting is not None and (getattr(acting, "run_id", None)
                                   or getattr(acting, "chain", None)):
            why = "this approval comes from an agent's run, not from a person"
    if why:
        raise ArchitectureApprovalRefused(
            f"An architecture is approved only by a person: {why}. Open Approvals, read the "
            "document, and approve it there yourself.")


def on_decision_answered(connection: Connection, decision: Mapping[str, Any], *, action: str,
                         actor: str, note: Optional[str]) -> None:
    """Record a person's answer on the review. The runner acts on it at its next tick."""
    rec = _json(decision.get("recommendation"), {}) or {}
    review_id = str(rec.get("architecture_review_id") or "")
    review = _review(connection, review_id, lock=True) if review_id else None
    if review is None or review.get("decision_id") != decision.get("id"):
        # An answer to a superseded version changes nothing: only the current one counts.
        return
    if review["status"] != "awaiting_approval":
        return
    version = int(rec.get("version") or review["current_version"] or 0)
    if action == "approve":
        refused = _unbuildable_stack(connection, review)
        if refused:
            # APPROVED, AND NOT BUILDABLE: the step fails here, with the words, before a build
            # is started and paid for (services.stacks). A person's approval cannot make BOSS
            # build Go; the fix is a revised architecture or a turned-on toolchain.
            connection.execute(
                text("UPDATE architecture_reviews SET status = 'failed', error = :e, "
                     "updated_at = now() WHERE id = :i"),
                {"e": refused[:500], "i": review_id},
            )
            emit_event(connection, review["project_id"], "architecture.stack_unsupported", actor,
                       "architecture_review", review_id,
                       {"version": version, "decision_id": decision.get("id"),
                        "message": refused, "workflow_run_id": review["workflow_run_id"]})
            return
        connection.execute(
            text("UPDATE architecture_reviews SET status = 'approved', approved_by = :a, "
                 "approved_at = now(), approved_version = :v, updated_at = now() WHERE id = :i"),
            {"a": actor, "v": version, "i": review_id},
        )
        event = "architecture.approved"
    elif action == "revise":
        notes = list(_json(review.get("notes"), []) or [])
        notes.append({"version": version, "note": str(note or "").strip(), "by": actor,
                      "at": now_utc().isoformat()})
        connection.execute(
            text("UPDATE architecture_reviews SET status = 'revision_requested', "
                 "notes = CAST(:n AS JSONB), updated_at = now() WHERE id = :i"),
            {"n": json.dumps(notes), "i": review_id},
        )
        event = "architecture.changes_requested"
    elif action == "reject":
        from .people_names import who

        reason = (f"rejected by {who(actor, connection)}"
                  + (f": {note.strip()}" if note and note.strip() else ""))
        connection.execute(
            text("UPDATE architecture_reviews SET status = 'rejected', error = :e, "
                 "updated_at = now() WHERE id = :i"),
            {"e": f"the architecture (version {version}) was {reason}"[:500], "i": review_id},
        )
        event = "architecture.rejected"
    else:
        return
    emit_event(connection, review["project_id"], event, actor, "architecture_review",
               review_id, {"version": version, "decision_id": decision.get("id"),
                           "note": note, "workflow_run_id": review["workflow_run_id"]})


def _unbuildable_stack(connection: Connection, review: Mapping[str, Any]) -> Optional[str]:
    """Why the architecture being approved declares a stack BOSS cannot build, or None."""
    from . import stacks

    content = _json(connection.execute(
        text("SELECT content FROM artifacts WHERE id = :a"),
        {"a": review.get("current_artifact_id")}).scalar(), {}) or {}
    verdict = stacks.check_architecture(str(content.get("markdown") or ""))
    if verdict.ok:
        return None
    return (verdict.message + " Nothing was built. Retry the Architecture step asking for a "
            "supported stack (or turn the toolchain on first).")


# ── The revision loop (runner tick) ──────────────────────────────────────────────────────────

def revise_sent_back(connection: Connection, settings: Any, *, project_id: str,
                     run_id: str) -> list[dict[str, Any]]:
    """Write the next version of every architecture of this run a person sent back, and ask
    again. A model failure leaves the review sent back, says why on it, and is retried by the
    next tick; it never approves or fails anything by itself."""
    rows = connection.execute(
        text("SELECT id FROM architecture_reviews WHERE workflow_run_id = :r AND project_id = :p "
             "AND status = 'revision_requested'"),
        {"r": run_id, "p": project_id},
    ).scalars().all()
    done: list[dict[str, Any]] = []
    for review_id in rows:
        review = _review(connection, review_id, lock=True)
        if review is None or review["status"] != "revision_requested":
            continue
        notes = list(_json(review.get("notes"), []) or [])
        note = str((notes[-1] if notes else {}).get("note") or "").strip()
        previous = connection.execute(
            text("SELECT content FROM artifacts WHERE id = :a"),
            {"a": review["current_artifact_id"]},
        ).scalar()
        previous = _json(previous, {}) or {}
        version = int(review["current_version"] or 0) + 1
        # THE DOCUMENT AS PEOPLE LEFT IT (BOSSAPP-874): their edits are what the architect
        # revises, and their open comment threads — each with the passage it is anchored to —
        # are part of what it is asked to answer. `doc_base` is the revision it read.
        from . import documents

        doc = documents.for_source(connection, project_id, _document_ref(review))
        doc_base = doc["current_revision_id"] if doc else None
        previous_markdown = (doc["body"] if doc and doc.get("body")
                             else str(previous.get("markdown") or ""))
        threads_brief = documents.open_threads_brief(connection, doc["id"]) if doc else ""
        asked = "\n\n".join(p for p in (note, threads_brief) if p)
        try:
            prd_markdown = _prd_for(connection, review)
            written = draft(connection, settings, project_id, architect=review["architect"],
                            run_id=run_id, prd=prd_markdown, title=review.get("title") or "",
                            previous=previous_markdown, note=asked,
                            version=version, step_id=review["step_id"], review_id=review_id,
                            prd_sha256=(_json(review.get("prd_source"), {}) or {}).get("prd_sha256"))
        except (ArchitectureDraftError, ArchitectureRefused) as error:
            connection.execute(
                text("UPDATE architecture_reviews SET error = :e, updated_at = now() "
                     "WHERE id = :i"), {"e": str(error)[:500], "i": review_id})
            done.append({"run_id": run_id, "step_id": review["step_id"],
                         "outcome": "architecture_revision_failed", "error": str(error)})
            continue
        try:
            artifact_id = _store_version(connection, settings, review, version, written,
                                         note=note, document_base=doc_base)
        except documents.DocumentConflict as conflict:
            connection.execute(
                text("UPDATE architecture_reviews SET error = :e, updated_at = now() "
                     "WHERE id = :i"),
                {"e": ("The document was edited while the architect wrote; it writes again "
                       f"from the new text next tick ({conflict})")[:500], "i": review_id})
            done.append({"run_id": run_id, "step_id": review["step_id"],
                         "outcome": "architecture_revision_conflict", "error": str(conflict)})
            continue
        old = _json(connection.execute(
            text("SELECT recommendation FROM decisions WHERE id = :d"),
            {"d": review["decision_id"]}).scalar(), {}) or {}
        decision_id = _open_decision(
            connection, review, version, artifact_id, written["markdown"],
            step_label=str(old.get("step_label") or "Architecture approval"),
            run_title=str(old.get("run_title") or review.get("title") or ""),
            app_name=str(old.get("app_name") or ""), note=note)
        connection.execute(
            text("UPDATE architecture_reviews SET status = 'awaiting_approval', "
                 "current_version = :v, current_artifact_id = :a, decision_id = :d, "
                 "error = NULL, updated_at = now() WHERE id = :i"),
            {"v": version, "a": artifact_id, "d": decision_id, "i": review_id},
        )
        # The step shows the version it is waiting on now.
        connection.execute(
            text("UPDATE workflow_step_runs SET output = COALESCE(output, '{}'::jsonb) "
                 "|| CAST(:o AS JSONB), updated_at = now() WHERE workflow_run_id = :r "
                 "AND step_id = :s AND status = 'waiting'"),
            {"r": run_id, "s": review["step_id"], "o": json.dumps({
                "architecture_version": version, "architecture_doc_id": artifact_id,
                "decision_id": decision_id})},
        )
        emit_event(connection, project_id, "architecture.revised", review["architect"],
                   "architecture_review", review_id,
                   {"version": version, "artifact_id": artifact_id, "decision_id": decision_id,
                    "answers": note})
        done.append({"run_id": run_id, "step_id": review["step_id"],
                     "outcome": "architecture_revised", "version": version})
    return done


def _prd_for(connection: Connection, review: Mapping[str, Any]) -> str:
    """Revisions retain the original canonical PRD text and approval lineage."""
    source = _json(review.get("prd_source"), {}) or {}
    current = approved_prd(connection, review["project_id"], {
        "input": {"adopt_meeting_run_id": source.get("meeting_run_id"),
                  "adopt_decision_id": source.get("decision_id")},
    }, run_id=review["workflow_run_id"])
    if current["meeting_run_id"] != source.get("meeting_run_id") or (
            source.get("prd_sha256") and source["prd_sha256"] != current["prd_sha256"]):
        raise ArchitectureDraftError("The approved PRD lineage changed; start a fresh architecture review.")
    return current["markdown"]


# ── Reading it ───────────────────────────────────────────────────────────────────────────────

def status_words(review: Mapping[str, Any]) -> str:
    """Where the review stands, in one sentence."""
    status = str(review.get("status") or "")
    version = int(review.get("current_version") or 0)
    architect = str(review.get("architect") or DEFAULT_ARCHITECT).replace("_", " ")
    if status == "drafting":
        return f"The {architect} is writing version {version or 1}."
    if status == "awaiting_approval":
        return (f"Version {version} is waiting for a person to approve it, request changes, "
                "or reject it.")
    if status == "revision_requested":
        notes = _json(review.get("notes"), []) or []
        asked = str((notes[-1] if notes else {}).get("note") or "")
        failed = f" Last attempt failed: {review['error']}" if review.get("error") else ""
        return (f"Changes were requested on version {version}" + (f": “{asked}”" if asked else "")
                + f". The {architect} is writing version {version + 1}.{failed}")
    if status == "approved":
        return (f"Version {review.get('approved_version') or version} was approved by "
                f"{review.get('approved_by')}. Build steps receive it in their brief.")
    if status == "rejected":
        return f"Rejected — {review.get('error') or 'a person said no'}. The run stopped here."
    if status == "cancelled":
        return "Cancelled with its run."
    return f"{status or 'unknown'}" + (f": {review['error']}" if review.get("error") else "")


def evidence(connection: Connection, review_id: str) -> dict[str, Any]:
    """What a finished review hands its step, and through it every later step."""
    review = _review(connection, review_id)
    if review is None:
        return {}
    content = _json(connection.execute(
        text("SELECT content FROM artifacts WHERE id = :a"),
        {"a": review["current_artifact_id"]}).scalar(), {}) or {}
    out: dict[str, Any] = {
        "architecture_review_id": review_id,
        "architecture_doc_id": review["current_artifact_id"],
        "architecture_version": int(review["current_version"] or 0),
        "decision_id": review["decision_id"],
        "note": status_words(review),
    }
    if review["status"] == "approved":
        out["architecture_markdown"] = str(content.get("markdown") or "")[
            :ARCHITECTURE_BRIEF_CHARS]
        out["architecture_approved_by"] = review["approved_by"]
    return out


def _decision_card(connection: Connection, decision_id: Optional[str]) -> Optional[dict]:
    if not decision_id:
        return None
    row = connection.execute(
        text("SELECT id, status, approved_by, resolution_note, recommendation, risk_tier, "
             "default_action, deadline_at, delegated_to FROM decisions WHERE id = :d"),
        {"d": decision_id},
    ).mappings().first()
    if row is None:
        return None
    from . import decision_actions

    card = {**dict(row), "recommendation": _json(row["recommendation"], {})}
    return {"id": row["id"], "status": row["status"], "approved_by": row["approved_by"],
            "note": row["resolution_note"],
            "actions": decision_actions.allowed_actions(card)}


def _public(connection: Connection, review: Mapping[str, Any], *, versions: bool) -> dict:
    out = {
        "id": review["id"],
        "project_id": review["project_id"],
        "workflow_run_id": review["workflow_run_id"],
        "step_id": review["step_id"],
        "status": review["status"],
        "status_words": status_words(review),
        "architect": review["architect"],
        "title": review.get("title"),
        "current_version": int(review["current_version"] or 0),
        "current_artifact_id": review["current_artifact_id"],
        "approved_by": review.get("approved_by"),
        "approved_version": review.get("approved_version"),
        "approved_at": review["approved_at"].isoformat() if review.get("approved_at") else None,
        "prd_source": _json(review.get("prd_source"), {}),
        "notes": _json(review.get("notes"), []),
        "error": review.get("error"),
        "decision": _decision_card(connection, review.get("decision_id")),
        "sections": [{"key": k, "heading": h} for k, h, _w in SECTIONS],
        "updated_at": review["updated_at"].isoformat() if review.get("updated_at") else None,
    }
    if versions:
        rows = connection.execute(
            text("SELECT id, content, created_at FROM artifacts WHERE project_id = :p "
                 "AND artifact_type = :t AND content->>'architecture_review_id' = :i "
                 "ORDER BY (content->>'version')::int"),
            {"p": review["project_id"], "t": ARTIFACT_TYPE, "i": review["id"]},
        ).mappings().all()
        out["versions"] = [{
            "artifact_id": r["id"],
            "version": int((_json(r["content"], {}) or {}).get("version") or 0),
            "markdown": (_json(r["content"], {}) or {}).get("markdown") or "",
            "sections": (_json(r["content"], {}) or {}).get("sections") or {},
            "missing_sections": (_json(r["content"], {}) or {}).get("missing_sections") or [],
            "revised_for": (_json(r["content"], {}) or {}).get("revised_for"),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        } for r in rows]
        current = next((v for v in out["versions"]
                        if v["artifact_id"] == review["current_artifact_id"]), None)
        out["markdown"] = (current or {}).get("markdown") or ""
    return out


def get_review(connection: Connection, project_id: str, review_id: str) -> Optional[dict]:
    review = _review(connection, review_id)
    if review is None or review["project_id"] != project_id:
        return None
    return _public(connection, review, versions=True)


def list_reviews(connection: Connection, project_id: str, *,
                 workflow_run_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    rows = connection.execute(
        text("SELECT * FROM architecture_reviews WHERE project_id = :p "
             "AND (CAST(:r AS TEXT) IS NULL OR workflow_run_id = :r) "
             "ORDER BY updated_at DESC LIMIT :n"),
        {"p": project_id, "r": workflow_run_id, "n": max(1, min(int(limit), 200))},
    ).mappings().all()
    return [_public(connection, dict(r), versions=False) for r in rows]


__all__ = [
    "ARTIFACT_TYPE", "ArchitectureApprovalRefused", "ArchitectureDraftError",
    "ArchitectureRefused", "BAD_STATES", "CAPABILITY", "DECISION_ACTION", "OK_STATES",
    "SECTIONS", "approved_prd", "draft", "evidence", "get_review", "list_reviews",
    "on_decision_answered", "parse_sections", "render", "require_a_person",
    "revise_sent_back", "start", "status_words",
]
