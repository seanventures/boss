"""Meetings deliberate, workflows execute; a squad uses both (BOSSAPP-908; design §13.2).

A squad adds no executor. When it needs a deliberation it CONVENES a panel — the existing
meeting machinery, through the same `request_meeting` tool an agent calls — and when it needs
work done it STARTS a workflow through the same `start_workflow` tool (readiness, stack and
budget checks included). Each launch leaves three things on the board:

    a link             work_item_links (meeting / workflow_run), which the diagram draws as a node
    a handoff          from the lead `via` the run, carrying the HANDOFF PACKET — what was
                       decided, the goal, what was found, what was tried and failed, how to verify,
                       how to undo, and who to report back to — so the run starts warm
    squad memory       the run is an artifact the squad started

When the run or panel ends, `collect_results` brings it back: a `finding` from BOSS citing the
run (evidence checked to exist) and a `handoff` back to the lead `returned_from` it. A failed run
goes into the squad's tried-and-failed memory. It is idempotent (a result comes back once) and
is called at the start of every squad round, and may be called by the Chief-of-Staff tick.

`Launchers` is the seam: `ToolLaunchers` is production (the default tool registry, as the
person-approved proposal path in agent_loop does); tests and the demo pass their own.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import board, collab, squad_memory
from . import task_classes as tc
from ._common import clean_text, party_id

TERMINAL_RUN = ("completed", "failed", "deadlettered", "cancelled", "compensated")
FAILED_RUN = ("failed", "deadlettered", "cancelled", "compensated")
TERMINAL_MEETING = ("completed", "failed")
#: The URL a run's output names its pull request by, in the order runs write them.
_PR_KEYS = ("pull_request_url", "pr_url", "pull_request", "pr")


class Launchers(Protocol):
    """`authority` is what the launch stands on — {"work_item_id", "act_id"}: the item and the
    ledger decision (a person's approval, or the squad's own under the class level). The
    production launchers act on it as the lead's Passport, verified (identity/ambient_launch)."""

    def convene(self, connection: Connection, *, project_id: str, actor: str, directive: str,
                seats: list[str], objective: str,
                authority: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """-> {"meeting_run_id"} or {"error"}"""

    def start(self, connection: Connection, *, project_id: str, actor: str, workflow_key: str,
              directive: str, title: str, inputs: dict[str, Any],
              authority: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """-> {"run_id"} or {"error"}"""


class ToolLaunchers:
    """The doors every BOSS agent uses: `request_meeting` and `start_workflow` on the default
    tool registry, confirmed — the squad's decision already passed the gate, the consensus and
    the ledger — and decided AS THE AGENT: the lead's Passport, on behalf of the person who
    approved the card (or under the project's class level), the decision as lineage
    (identity/ambient_launch). Never as whoever happens to be on the thread: on the ambient tick
    that was the worker's sys_worker, and every launch came back `forbidden`."""

    def __init__(self, settings: Any):
        self.settings = settings

    def _invoke(self, connection: Connection, tool: str, *, project_id: str, actor: str,
                args: dict[str, Any], authority: Optional[Mapping[str, Any]],
                ref_of: Any) -> dict[str, Any]:
        from ...context import EngineContext
        from ...identity.ambient_launch import LaunchRefused, launch_context, record_launch
        from ...tools.builtin import build_default_registry

        authority = authority or {}
        try:
            auth = launch_context(connection, project_id=project_id,
                                  work_item_id=str(authority.get("work_item_id") or ""),
                                  act_id=authority.get("act_id"), role_key=actor)
        except LaunchRefused as refused:
            return {"ok": False, "error": f"no verified identity to act as: {refused.reason}"}
        ctx = EngineContext(settings=self.settings, engine=connection.engine)
        got = build_default_registry().invoke(tool, ctx, project_id=project_id, actor=actor,
                                              args=args, operator_confirmed=True, auth=auth)
        ref = ref_of(got)
        record_launch(auth, tool=tool, outcome_ok=bool(ref), ref=ref,
                      error=None if ref else str(got.get("error") or "not started"),
                      db=connection)
        return got

    def convene(self, connection, *, project_id, actor, directive, seats, objective,
                authority=None):
        got = self._invoke(connection, "request_meeting", project_id=project_id, actor=actor,
                           args={"directive": directive, "seats": seats, "objective": objective},
                           authority=authority,
                           ref_of=lambda g: (g.get("meeting") or {}).get("run_id"))
        meeting = got.get("meeting") or {}
        if not meeting.get("run_id"):
            return {"error": str(got.get("error") or "the panel did not start")[:300]}
        return {"meeting_run_id": meeting["run_id"]}

    def start(self, connection, *, project_id, actor, workflow_key, directive, title, inputs,
              authority=None):
        got = self._invoke(connection, "start_workflow", project_id=project_id, actor=actor,
                           args={"workflow_key": workflow_key, "directive": directive,
                                 "title": title, "inputs": inputs},
                           authority=authority, ref_of=lambda g: g.get("run_id"))
        if not got.get("run_id"):
            return {"error": str(got.get("error") or "the run did not start")[:300],
                    **({"missing": got["missing"]} if got.get("missing") else {})}
        return {"run_id": got["run_id"]}


def _authority(item: Mapping[str, Any], decision: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    return {"work_item_id": item["id"], "act_id": (decision or {}).get("act_id")}


# ── the handoff packet ───────────────────────────────────────────────────────────────────────

def packet(connection: Connection, item: Mapping[str, Any], *, squad: Mapping[str, Any],
           proposal: Mapping[str, Any], decision: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """What travels with work handed to a workflow or a panel: enough that it starts warm and
    knows where to report back. Built from the board and the squad's memory, never retyped."""
    memory = squad_memory.read(connection, item["id"])
    body = proposal.get("body") or {}
    return {
        "work_item_id": item["id"], "goal": item["title"], "task_class": item["task_class"],
        "subject": item.get("subject_ref") or item.get("app"),
        "decided": ({"act_id": decision["act_id"], "chosen": decision["chosen"],
                     "decided_by": decision["decided_by"]} if decision else None),
        "do": body.get("summary"), "action": body.get("action"),
        "verify": body.get("verify"), "undo": body.get("undo"),
        "found": [f["summary"] for f in memory["findings"] if f["summary"]][-6:],
        "tried_and_failed": memory["tried_and_failed"][-6:],
        "dissent": [d for d in ((decision or {}).get("consensus") or {}).get("dissent") or ()],
        "report_back_to": squad["lead"], "squad": [m["party"] for m in squad["members"]],
    }


def _directive(p: Mapping[str, Any]) -> str:
    lines = [str(p.get("do") or p["goal"]), "", f"Why: {p['goal']} ({p['task_class']})"]
    if p.get("found"):
        lines += ["", "What the squad found:"] + [f"- {f}" for f in p["found"]]
    if p.get("tried_and_failed"):
        lines += ["", "Already tried and failed (do not repeat):"] + [
            f"- {r}" for r in p["tried_and_failed"]]
    if p.get("dissent"):
        lines += ["", "Dissent on record (address it):"] + [
            f"- {d.get('party')}: {d.get('reason')}" for d in p["dissent"]]
    if p.get("verify"):
        lines += ["", f"Done when: {p['verify']}"]
    return "\n".join(lines)


# ── launching ────────────────────────────────────────────────────────────────────────────────

def launch_workflow(connection: Connection, item: Mapping[str, Any], *, squad: Mapping[str, Any],
                    proposal: Mapping[str, Any], decision: Optional[Mapping[str, Any]],
                    launchers: Launchers) -> dict[str, Any]:
    """Hand the decided work to a workflow. -> {"kind": "workflow_run", "ref"} or {"error"}."""
    p = packet(connection, item, squad=squad, proposal=proposal, decision=decision)
    key = (proposal.get("body") or {}).get("workflow") or squad["execute_workflow"]
    lead = squad["lead"]
    got = launchers.start(connection, project_id=item["project_id"], actor=party_id(lead),
                          workflow_key=key, directive=_directive(p),
                          title=clean_text(p.get("do") or item["title"], 120),
                          inputs={"ambient_work_item_id": item["id"], "handoff": p},
                          authority=_authority(item, decision))
    if not got.get("run_id"):
        why = got.get("error") or "the run did not start"
        squad_memory.note(connection, item["id"], actor=lead,
                          tried_and_failed=f"start {key}: {why}")
        collab.post(connection, item["id"], kind="finding", sender="boss",
                    body={"summary": f"Could not start {key}: {why}"})
        return {"error": why}
    run_id = got["run_id"]
    board.link(connection, item["id"], "workflow_run", run_id, actor=lead, note=key)
    collab.post(connection, item["id"], kind="handoff", sender=lead,
                body={"to": "boss", "from": lead, "via": {"kind": "workflow_run", "ref": run_id},
                      "summary": f"Handed to {key}", "packet": p})
    squad_memory.note(connection, item["id"], actor=lead, phase=f"Running {key}",
                      next_step="Read the run's result when it returns", artifact=run_id)
    return {"kind": "workflow_run", "ref": run_id, "workflow_key": key}


def convene_panel(connection: Connection, item: Mapping[str, Any], *,
                  squad: Mapping[str, Any], proposal: Mapping[str, Any],
                  decision: Optional[Mapping[str, Any]],
                  launchers: Launchers) -> dict[str, Any]:
    """Convene a panel for the squad's question. -> {"kind": "meeting", "ref"} or {"error"}."""
    p = packet(connection, item, squad=squad, proposal=proposal, decision=decision)
    lead = squad["lead"]
    seats = [party_id(m["party"]) for m in squad["members"]][:8]
    question = (proposal.get("body") or {}).get("summary") or item["title"]
    got = launchers.convene(connection, project_id=item["project_id"], actor=party_id(lead),
                            directive=_directive({**p, "do": question}), seats=seats,
                            objective=f"A decision the {squad['title']} can act on for: "
                                      f"{item['title']}",
                            authority=_authority(item, decision))
    if not got.get("meeting_run_id"):
        why = got.get("error") or "the panel did not start"
        squad_memory.note(connection, item["id"], actor=lead,
                          tried_and_failed=f"convene a panel: {why}")
        collab.post(connection, item["id"], kind="finding", sender="boss",
                    body={"summary": f"Could not convene the panel: {why}"})
        return {"error": why}
    meeting_id = got["meeting_run_id"]
    board.link(connection, item["id"], "meeting", meeting_id, actor=lead, note="panel")
    collab.post(connection, item["id"], kind="handoff", sender=lead,
                body={"to": "boss", "from": lead, "via": {"kind": "meeting", "ref": meeting_id},
                      "summary": "Asked a panel", "packet": p})
    squad_memory.note(connection, item["id"], actor=lead, phase="Panel deliberating",
                      artifact=meeting_id)
    return {"kind": "meeting", "ref": meeting_id}


#: action -> what carries it out (anything else is decided here and executed by its owner
#: stream: SRE mitigation (910), labels and nudges (904)).
EXECUTORS = {"start_workflow": launch_workflow, "open_draft_pr": launch_workflow,
             "convene_meeting": convene_panel}


def executes(action: str) -> bool:
    return action in EXECUTORS


# ── results coming back ──────────────────────────────────────────────────────────────────────

def _returned(connection: Connection, item_id: str) -> set[tuple[str, str]]:
    out = set()
    for m in collab.messages(connection, item_id, kinds=("handoff",)):
        back = (m.get("body") or {}).get("returned_from")
        if back:
            out.add((back["kind"], back["ref"]))
    return out


def _pr_of(output: Any) -> Optional[str]:
    if not isinstance(output, Mapping):
        return None
    for key in _PR_KEYS:
        value = output.get(key)
        if isinstance(value, Mapping):
            value = value.get("url") or value.get("html_url")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def collect_results(connection: Connection, item_id: str) -> list[dict[str, Any]]:
    """Bring every finished run and panel the item started back onto its board, once."""
    item = board.get(connection, item_id)
    if item is None or item["lifecycle"] in board.TERMINAL:
        return []
    from .squads import roster

    squad = roster(connection, item_id)
    lead = squad["lead"] if squad else (item.get("lease_owner") or "boss")
    done = _returned(connection, item_id)
    back: list[dict[str, Any]] = []
    for link in board.links(connection, item_id):
        kind, ref = link["kind"], link["ref"]
        if kind not in ("workflow_run", "meeting") or (kind, ref) in done:
            continue
        if kind == "workflow_run":
            row = connection.execute(text(
                "SELECT status, output, error FROM workflow_runs WHERE id = :r AND project_id = :p"),
                {"r": ref, "p": item["project_id"]}).mappings().first()
            if row is None or row["status"] not in TERMINAL_RUN:
                continue
            failed = row["status"] in FAILED_RUN
            pr = _pr_of(row["output"])
            summary = (f"Run {row['status']}" + (f": {clean_text(row['error'], 160)}"
                                                 if failed and row["error"] else "")
                       + (f"; pull request {pr}" if pr else ""))
            evidence = [{"kind": "workflow_run", "ref": ref}]
            if pr and collab_shape_ok("pull_request", pr):
                evidence.append({"kind": "pull_request", "ref": pr})
                board.link(connection, item_id, "pull_request", pr, actor="boss")
        else:
            row = connection.execute(text(
                "SELECT status, state, error FROM meeting_runs WHERE id = :r "
                "AND project_id = :p"), {"r": ref, "p": item["project_id"]}).mappings().first()
            if row is None or row["status"] not in TERMINAL_MEETING:
                continue
            failed = row["status"] == "failed"
            state = row["state"] if isinstance(row["state"], Mapping) else {}
            said = state.get("verdict") or state.get("synthesis") or ""
            if isinstance(said, Mapping):
                said = said.get("summary") or said.get("recommendation") or ""
            summary = (f"Panel {row['status']}"
                       + (f": {clean_text(said, 220)}" if said and not failed else "")
                       + (f": {clean_text(row['error'], 160)}" if failed and row["error"] else ""))
            evidence = [{"kind": "meeting", "ref": ref}]
        finding = collab.post(connection, item_id, kind="finding", sender="boss",
                              body={"summary": summary, "evidence": evidence})
        collab.post(connection, item_id, kind="handoff", sender="boss",
                    body={"to": lead, "from": "boss", "returned_from": {"kind": kind, "ref": ref},
                          "summary": "Result back", "packet": {"status": row["status"],
                                                               "finding_id": finding["id"]}})
        squad_memory.note(connection, item_id, actor="boss",
                          phase=f"{'Panel' if kind == 'meeting' else 'Run'} {row['status']}",
                          tried_and_failed=(f"{kind} {ref}: {summary}" if failed else None))
        back.append({"kind": kind, "ref": ref, "status": row["status"], "failed": failed,
                     "finding_id": finding["id"]})
    return back


def collab_shape_ok(kind: str, ref: str) -> bool:
    from ._common import SHAPE_ONLY

    return bool(SHAPE_ONLY[kind].match(ref))


__all__ = ["Launchers", "ToolLaunchers", "packet", "launch_workflow", "convene_panel",
           "EXECUTORS", "executes", "collect_results"]


def _action_title(action: str) -> str:  # used by squad_turns for labels
    found = tc.ACTIONS.get(action)
    return found.title if found else action
