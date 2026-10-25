"""record_learning: an agent records what it learned, at the moment it learns it (BOSSAPP-768,
KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §4.1).

In the ``knowledge.propose`` bundle, which every agent holds. A ``write`` tool with reversibility
``safe``: what it writes is one ``inferred`` learned item — quarantined, never above a governed
or approved fact, forgettable by a person — exactly like ``kg_propose_fact``. Its provenance (the
agent, its run and step, the model) is the pipeline's LLM context, never the arguments. Later
tasks by other agents get it back through the pack ("Learned before") and kg_precedent / kg_why.

It records in every knowledge_graph mode: capture goes on when agents are given nothing, so
switching the graph back on loses nothing (runtime.MODES).
"""
from __future__ import annotations

from .registry import ToolSpec

_EVIDENCE = {
    "type": "array", "minItems": 1, "maxItems": 10,
    "description": "What shows it, at least one: a log line, a test run, a diff, a metric or a file. "
                   "With no evidence nothing is stored. A log alone caps confidence at 0.6; a "
                   "passing test (passed=true) at 0.9.",
    "items": {"type": "object", "properties": {
        "type": {"type": "string", "enum": ["log", "test", "diff", "metric", "file"]},
        "ref": {"type": "string", "description": "Where: a file:lines, a test name, a commit, a run id."},
        "excerpt": {"type": "string", "description": "The line or lines themselves (never a secret)."},
        "passed": {"type": "boolean", "description": "For a test: it passed after the change."}},
        "required": ["type"], "additionalProperties": False},
}
_PARAMS = {
    "type": "object",
    "properties": {
        "learning_kind": {
            "type": "string",
            "enum": ["investigation", "test_failure", "fix", "workaround", "review_finding", "decision",
                     "root_cause", "precedent", "fact"],
            "description": "investigation: what you tried, ruled out and found. test_failure: a test and "
                           "why it failed. fix: what made a failure pass. workaround: a build or deploy "
                           "problem and what worked instead. decision: a choice and its reasons. "
                           "root_cause, precedent (symptom → cause → fix), review_finding, fact."},
        "title": {"type": "string", "description": "One line someone scanning a list understands."},
        "statement": {"type": "string", "description": "What you learned, in a sentence or two."},
        "detail": {
            "type": "object",
            "description": "The kind's fields. investigation: tried[], ruled_out[{hypothesis, reason}], "
                           "found. test_failure: test, error_signature, cause. fix: change_summary, "
                           "files[], pr_url. workaround: problem, workaround, permanent_fix. decision: "
                           "question, chosen, reasons[], alternatives[]. precedent: symptom, "
                           "cause_summary, fix_summary, applies_to[]."},
        "about": {
            "type": "object",
            "description": "What it is about: code symbols or files (node keys, ids or exact names, "
                           "as kg_symbol returned them), rule ids, areas.",
            "properties": {"symbols": {"type": "array", "items": {"type": "string"}},
                           "rules": {"type": "array", "items": {"type": "string"}},
                           "areas": {"type": "array", "items": {"type": "string"}}},
            "additionalProperties": False},
        "evidence": _EVIDENCE,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "supersedes": {"type": "string",
                       "description": "The id of an earlier learning this one shows was wrong (it is closed, "
                                      "kept in history)."},
    },
    "required": ["learning_kind", "title", "statement", "evidence", "confidence"],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Record what you just learned while working, so the next agent on this code does not have to "
    "learn it again: a hypothesis you ruled out and why, a cause you found, a failing test and what "
    "fixed it, a build or deploy problem and the workaround, a decision and its reasons. Call it at "
    "the moment you learn it, once per thing learned, with evidence and the code it is about. It is "
    "stored as inferred (untrusted until a person promotes it) with you, your run and step as its "
    "source; a repeat of something already learned adds your evidence to it instead of a copy."
)


def _record(ctx, *, project_id, actor, args):
    from ..services.knowledge.learning_writeback import LearningRefused, record

    args = args or {}
    with ctx.begin() as connection:
        try:
            with connection.begin_nested():
                item = record(
                    connection, project_id=project_id,
                    learning_kind=str(args.get("learning_kind") or ""),
                    title=str(args.get("title") or ""), statement=str(args.get("statement") or ""),
                    detail=args.get("detail") if isinstance(args.get("detail"), dict) else {},
                    about=args.get("about") if isinstance(args.get("about"), dict) else {},
                    evidence=args.get("evidence") if isinstance(args.get("evidence"), list) else [],
                    confidence=args.get("confidence") if args.get("confidence") is not None else 0.5,
                    supersedes=str(args.get("supersedes") or "").strip() or None)
        except (LearningRefused, LookupError) as refused:
            return {"error": str(refused)}
    unresolved = [s for s in item["about"]["symbols"] if not str(s).startswith(("symbol:", "file:", "module:"))]
    note = ("Recorded as inferred knowledge; later agents working on this code are given it as "
            f"[L:{item['node']['id']}]." if item["seen_count"] == 1 else
            f"Already learned before ({item['seen_count']} times now): your evidence was added to "
            f"[L:{item['node']['id']}].")
    if unresolved:
        note += f" Not in the code graph (kept as named): {', '.join(unresolved[:5])}."
    return {"learning": {"id": item["node"]["id"], "kind": item["learning_kind"], "title": item["title"],
                         "confidence": item["confidence"], "seen_count": item["seen_count"],
                         "about": item["about"]},
            "note": note}


def register_learning_tools(reg) -> None:
    reg.register(
        ToolSpec(
            name="record_learning",
            description=DESCRIPTION,
            parameters=_PARAMS,
            handler=_record,
            kind="write",
            # One `inferred` learned item: quarantined, never binding, forgettable by a person.
            reversibility="safe",
            # A QUEUED WRITE, like file_platform_gap and the KG proposals (brec_eddfaf96…): it
            # changes nothing until a person acts, so the chat write gate runs it in the turn
            # rather than holding it behind a confirm button (brec_9157d2686df34517addc).
            queues_for_review=True,
            requires_durable_workspace=True,
            permission="knowledge:read",
        )
    )
