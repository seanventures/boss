"""The decision ledger: every decision that changes what BOSS does, as one act (BOSSAPP-906).

WHY AN ACT, NOT A NEW TABLE (design §11.2)
------------------------------------------
"Who did this" once had eight answers in eight tables; agent_acts.py exists to make it one. An
autonomous decision is work an agent (or a policy, a squad, a person) DID, so it is an act of
kind 'decision': who decided, when, at what cost, after whom, under whose accountability — the
columns every act reader (the chain strip, an agent's history, presence, Ask BOSS's citations
[act:...]) already reads. What only a decision carries lives in its 1:1 row in
agent_act_decisions: the question, every option and why the others lost, the rationale (at most
three sentences, the decider's own words), evidence BOSS checked exists, confidence, the
autonomy level and task class it was taken under, its reversibility and undo, the consensus
record, the override chain and, later, its outcome.

WHAT THE LEDGER REFUSES
-----------------------
* Evidence that does not exist (_common.verify_evidence): a decision never cites a ghost.
* An agent, squad or policy decision whose catalogue `action` the autonomy gate does not allow
  without a person (policy.gate): merge, production deploy, external send and payment can only
  ever be decided by `person:<id>`. The gate is enforced HERE, at the one write every
  decision passes through, not only in the caller that should have asked.
* A decision whose gate requires agreement (maker-checker, squad vote) without a consensus
  record that satisfies it (collab.satisfies).
* A rationale over three sentences: the ledger is read in seconds or not at all (§11.0).

APPEND-ONLY
-----------
Rows are never edited or deleted (a database trigger). A person's OVERRIDE is a new row whose
`overrides_act_id` points at the row it overrides; the overridden row's outcome becomes
'overridden'. That pair is what verifier_ratchet treats as ground truth. The only later write is
filling an empty outcome once (record_outcome), which also feeds the class's track record:
a revert demotes the class at once (policy.review_track_record).
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .. import agent_acts
from . import policy
from . import task_classes as tc
from ._common import (EvidenceError, clean_text, emit, party, party_id, party_kind, person,
                      serialize_row, verify_evidence)

OUTCOMES = ("accepted", "edited", "dismissed", "ignored", "merged", "reverted", "recurred",
            "overridden", "succeeded", "failed", "incident_caused")
MAX_RATIONALE_SENTENCES = 3
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")


class GateRefused(PermissionError):
    """The autonomy gate does not let this decider take this action without a person."""

    def __init__(self, verdict: Mapping[str, Any]):
        super().__init__(str(verdict.get("reason") or "not allowed"))
        self.verdict = dict(verdict)


class LedgerConflict(RuntimeError):
    """An append-only rule: an outcome is set once; a row is overridden once."""


def sentences(value: str) -> int:
    body = (value or "").strip()
    if not body:
        return 0
    ends = len(_SENTENCE_END.findall(body))
    return ends + (0 if _SENTENCE_END.search(body[-1:] + " ") else 1)


def _options(options: Iterable[Any], chosen: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for option in options or ():
        if isinstance(option, Mapping):
            key, label = str(option.get("key") or ""), str(option.get("label") or "")
        else:
            key, label = str(option), str(option)
        key = clean_text(key, 120)
        if key and key not in {o["key"] for o in out}:
            out.append({"key": key, "label": clean_text(label or key, 120)})
    if chosen not in {o["key"] for o in out}:
        out.insert(0, {"key": chosen, "label": chosen})
    return out


def record_decision(connection: Connection, *, project_id: str, decided_by: str,
                    question: str, chosen: str, rationale: str, reversibility: str,
                    options: Iterable[Any] = (), rejected: Iterable[Mapping[str, Any]] = (),
                    evidence: Iterable[Any] = (), confidence: Optional[float] = None,
                    work_item_id: Optional[str] = None, signal_ids: Iterable[str] = (),
                    task_class: Optional[str] = None, autonomy_level: Optional[str] = None,
                    action: Optional[str] = None, undo: Optional[Mapping[str, Any]] = None,
                    consensus: Optional[Mapping[str, Any]] = None,
                    proposal_id: Optional[str] = None, cost_usd_micros: int = 0,
                    overrides_act_id: Optional[str] = None, accountable: Optional[str] = None,
                    how: Optional[str] = None, parent_act_id: Optional[str] = None,
                    **links: Any) -> dict[str, Any]:
    """Append one decision. Returns the ledger row (decision detail + act fields).

    `decided_by` is agent:<role> | policy:<id> | squad:<id> | person:<id>. `options` are every
    option considered (keys or {key, label}); `chosen` is one of them; `rejected` may say why
    each other option lost ([{option, why}]). `evidence` is [{kind, ref, note?}] and every ref
    must exist. `links` are agent_acts links (workflow_run_id, code_task_run_id, decision_id …).
    """
    decider = party(decided_by, allow_boss=False)
    kind = party_kind(decider)
    asked = clean_text(question, 300)
    if not asked:
        raise ValueError("a decision states its question in one sentence")
    chosen = clean_text(chosen, 120)
    if not chosen:
        raise ValueError("a decision names the option it chose")
    why = clean_text(rationale, 800)
    if not why:
        raise ValueError("a decision gives its rationale")
    if sentences(why) > MAX_RATIONALE_SENTENCES:
        raise ValueError(f"the rationale is at most {MAX_RATIONALE_SENTENCES} sentences; put the "
                         "detail in the evidence")
    if reversibility not in tc.REVERSIBILITY:
        raise ValueError(f"reversibility is one of {', '.join(tc.REVERSIBILITY)}")
    if reversibility == "reversible" and not undo:
        raise ValueError("a reversible decision names its undo action")
    if confidence is not None and not 0 <= float(confidence) <= 1:
        raise ValueError("confidence is between 0 and 1")

    item = None
    if work_item_id:
        item = connection.execute(text(
            "SELECT id, project_id, task_class, autonomy_level FROM work_items WHERE id = :w"),
            {"w": work_item_id}).mappings().first()
        if item is None or item["project_id"] != project_id:
            raise LookupError(f"no work item {work_item_id} in this project")
    klass = task_class or (item["task_class"] if item else None)
    if klass:
        tc.task_class(klass)
    level = autonomy_level
    if level is not None and level not in tc.LEVELS:
        raise ValueError(f"autonomy level is one of {', '.join(tc.LEVELS)}")

    verdict = None
    if action:
        tc.action(action)
        if not klass:
            raise ValueError("a decision that takes an action names the task class it is under")
        verdict = policy.gate(connection, project_id, klass, action)
        level = level or verdict["level"]
        if kind != "person":
            if not verdict["allowed"] or verdict["needs_person"]:
                raise GateRefused(verdict)
            from .collab import satisfies

            if not satisfies(consensus, verdict["consensus_mode"]):
                raise GateRefused({**verdict, "reason": (
                    f"{verdict['consensus_mode'].replace('_', ' ')} agreement is required first "
                    "(collab.tally) and its record must ride on the decision")})
    if level is None and klass:
        level = policy.level_for(connection, project_id, klass)["level"]
    if kind != "person" and not list(evidence or ()):
        raise EvidenceError("an agent, squad or policy decision cites at least one piece of "
                            "evidence")
    checked = verify_evidence(connection, project_id, evidence)
    option_list = _options(options, chosen)
    rejected_list = [{"option": clean_text(r.get("option"), 120),
                      **({"why": clean_text(r.get("why"), 300)} if r.get("why") else {})}
                     for r in rejected or () if r.get("option")]
    named = {r["option"] for r in rejected_list}
    rejected_list += [{"option": o["key"]} for o in option_list
                      if o["key"] != chosen and o["key"] not in named]

    overridden = None
    if overrides_act_id:
        if kind != "person":
            raise ValueError("only a person overrides a decision")
        overridden = connection.execute(text(
            "SELECT * FROM agent_act_decisions WHERE act_id = :a FOR UPDATE"),
            {"a": overrides_act_id}).mappings().first()
        if overridden is None or overridden["project_id"] != project_id:
            raise LookupError(f"no decision {overrides_act_id} in this project")
        taken = connection.execute(text(
            "SELECT 1 FROM agent_act_decisions WHERE overrides_act_id = :a"),
            {"a": overrides_act_id}).first()
        if taken is not None:
            raise LedgerConflict(f"{overrides_act_id} is already overridden; override the "
                                 "override instead")

    role_key = party_id(decider) if kind == "agent" else None
    who_accountable = accountable or (party_id(decider) if kind == "person" else agent_acts.BOSS)
    edge = how or ("board" if work_item_id else "signal")
    handed_by_kind = "person" if kind == "person" else "boss"
    act_id = agent_acts.open_act(
        connection, project_id=project_id, kind="decision", role_key=role_key, how=edge,
        handed_by_kind=handed_by_kind,
        handed_by_id=party_id(decider) if kind == "person" else None,
        parent_act_id=parent_act_id, why=why, accountable=who_accountable,
        subject_kind="work_item" if work_item_id else None, subject_id=work_item_id,
        label=f"Decided · {asked}"[:200], status="active", **links)
    agent_acts.close_act(connection, act_id, status="complete",
                         cost_usd=max(0, int(cost_usd_micros)) / 1_000_000)
    connection.execute(text(
        "INSERT INTO agent_act_decisions (act_id, project_id, work_item_id, signal_ids, question, "
        "decided_by, chosen, options, rejected, rationale, evidence, confidence, autonomy_level, "
        "task_class, action, reversibility, undo, consensus, proposal_id, cost_usd_micros, "
        "overrides_act_id) VALUES (:a, :p, :w, CAST(:sig AS JSONB), :q, :by, :ch, "
        "CAST(:opt AS JSONB), CAST(:rej AS JSONB), :why, CAST(:ev AS JSONB), :conf, :lvl, :cls, "
        ":act, :rev, CAST(:undo AS JSONB), CAST(:cons AS JSONB), :prop, :cost, :ovr)"),
        {"a": act_id, "p": project_id, "w": work_item_id,
         "sig": json.dumps([str(s) for s in signal_ids or ()]), "q": asked, "by": decider,
         "ch": chosen, "opt": json.dumps(option_list), "rej": json.dumps(rejected_list),
         "why": why, "ev": json.dumps(checked),
         "conf": None if confidence is None else round(float(confidence), 3), "lvl": level,
         "cls": klass, "act": action, "rev": reversibility,
         "undo": json.dumps(dict(undo)) if undo else None,
         "cons": json.dumps(dict(consensus)) if consensus else None, "prop": proposal_id,
         "cost": max(0, int(cost_usd_micros)), "ovr": overrides_act_id})
    if overridden is not None and overridden["outcome"] is None:
        connection.execute(text(
            "UPDATE agent_act_decisions SET outcome = 'overridden', outcome_ref = :a, "
            "outcome_at = now() WHERE act_id = :o"), {"a": act_id, "o": overrides_act_id})
    if work_item_id:
        from .board import add_cost, link
        from .collab import post

        link(connection, work_item_id, "act", act_id, actor=decider)
        add_cost(connection, work_item_id, int(cost_usd_micros or 0))
        post(connection, work_item_id, kind="decision", sender=decider, _internal=True,
             body={"act_id": act_id}, reply_to=proposal_id, act_id=act_id)
    emit(connection, project_id, "ambient.decision_recorded", decider, "agent_act", act_id,
         {"work_item_id": work_item_id, "decided_by": decider, "chosen": chosen,
          "task_class": klass, "autonomy_level": level, "action": action,
          "overrides_act_id": overrides_act_id})
    if overridden is not None:
        emit(connection, project_id, "ambient.decision_overridden", decider, "agent_act",
             overrides_act_id, {"by_act_id": act_id, "chosen": chosen,
                                "was": overridden["chosen"]})
        if overridden["task_class"] and not str(overridden["decided_by"]).startswith("person:"):
            policy.review_track_record(connection, project_id, overridden["task_class"])
    return get_decision(connection, act_id)


def override(connection: Connection, act_id: str, *, actor: str, chosen: str, rationale: str,
             evidence: Iterable[Any] = ()) -> dict[str, Any]:
    """A person reverses a decision: a new row pointing at it, same question and options."""
    row = connection.execute(text("SELECT * FROM agent_act_decisions WHERE act_id = :a"),
                             {"a": act_id}).mappings().first()
    if row is None:
        raise LookupError(f"no decision {act_id}")
    return record_decision(
        connection, project_id=row["project_id"], decided_by=person(actor),
        question=row["question"], chosen=chosen, rationale=rationale,
        reversibility=row["reversibility"], undo=row["undo"], options=row["options"],
        evidence=evidence, work_item_id=row["work_item_id"], signal_ids=row["signal_ids"],
        task_class=row["task_class"], autonomy_level=row["autonomy_level"],
        overrides_act_id=act_id, how="approved", parent_act_id=act_id)


def record_outcome(connection: Connection, act_id: str, *, outcome: str,
                   outcome_ref: Optional[str] = None, actor: str = "boss") -> dict[str, Any]:
    """Fill a decision's outcome, once, and let the class's track record react to it."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome is one of {', '.join(OUTCOMES)}")
    row = connection.execute(text(
        "SELECT project_id, task_class, decided_by, outcome FROM agent_act_decisions "
        "WHERE act_id = :a FOR UPDATE"), {"a": act_id}).mappings().first()
    if row is None:
        raise LookupError(f"no decision {act_id}")
    if row["outcome"] is not None:
        raise LedgerConflict(f"{act_id} already has its outcome ({row['outcome']})")
    connection.execute(text(
        "UPDATE agent_act_decisions SET outcome = :o, outcome_ref = :r, outcome_at = now() "
        "WHERE act_id = :a"), {"o": outcome, "r": clean_text(outcome_ref, 300) or None,
                               "a": act_id})
    emit(connection, row["project_id"], "ambient.decision_outcome", actor, "agent_act", act_id,
         {"outcome": outcome, "outcome_ref": outcome_ref})
    review = None
    if row["task_class"] and not str(row["decided_by"]).startswith("person:"):
        review = policy.review_track_record(connection, row["project_id"], row["task_class"])
    return {**get_decision(connection, act_id), "track_record": review}


_SELECT = (
    "SELECT d.*, a.role_key, a.how, a.handed_by_kind, a.handed_by_id, a.accountable, "
    "a.parent_act_id, a.root_act_id, a.label, a.started_at, a.cost_usd AS act_cost_usd, "
    "a.workflow_run_id, a.code_task_run_id, a.meeting_run_id, a.decision_id "
    "FROM agent_act_decisions d JOIN agent_acts a ON a.act_id = d.act_id ")


def _out(row: Mapping[str, Any]) -> dict[str, Any]:
    data = serialize_row(row)
    data["edge_label"] = agent_acts.edge_label(str(row["how"]), actor=(
        party_id(row["decided_by"]) if str(row["decided_by"]).startswith("person:") else None))
    data["decider_kind"] = party_kind(str(row["decided_by"]))
    return data


def get_decision(connection: Connection, act_id: str) -> dict[str, Any]:
    row = connection.execute(text(_SELECT + "WHERE d.act_id = :a"),
                             {"a": act_id}).mappings().first()
    if row is None:
        raise LookupError(f"no decision {act_id}")
    return _out(row)


def list_decisions(connection: Connection, project_id: str, *,
                   work_item_id: Optional[str] = None, task_class: Optional[str] = None,
                   limit: int = 100) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        _SELECT + "WHERE d.project_id = :p AND (CAST(:w AS TEXT) IS NULL OR d.work_item_id = :w) "
        "AND (CAST(:c AS TEXT) IS NULL OR d.task_class = :c) "
        "ORDER BY d.created_at" + (" ASC" if work_item_id else " DESC") + ", d.act_id LIMIT :n"),
        {"p": project_id, "w": work_item_id, "c": task_class,
         "n": max(1, min(int(limit), 500))}).mappings()
    return [_out(r) for r in rows]
