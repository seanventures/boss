"""Extract a rulebook's rules into the Knowledge Graph (rulebook source G5).

The ``extract_rulebook_rules`` step of the ``rulebook_extraction`` workflow. The Rulebook Analyst
(``rulebook_analyst``, "Rubric") reads the clauses of a document a person uploaded and confirmed
(``document_upload``), a batch at a time, and answers with the rules they state. Each answer is
held to the text by ``rules.propose_dq_rule``: the rule id must be printed in the clause it cites
and the quote must be in it word for word. Every rule is a PROPOSAL (``inferred``, status
``proposed``) with its Warning/Error ``dq_threshold`` nodes and the clause as evidence, waiting in
Rules review for a person.

What the step never does: invent. An unreadable answer is no answer (the batch proposes nothing
and the result names the sections left unread); a refused proposal is reported with its reason; a
table is derived only from a ``table.column`` the rule printed. The rules come FROM THE DOCUMENT:
nothing here reads the sample data a rulebook may have been generated from.

Spend: every call is reserved before it is sent and settled after, in budget category
``knowledge`` (``rule_scan.Governor``), within ``budget_usd``; the step stops on the budget and
says so.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import banking_regulatory_pipeline as _shared

CAPABILITY = "extract_rulebook_rules"
WORKFLOW_KEY = "rulebook_extraction"
ROLE = "rulebook_analyst"
DEFAULT_BUDGET_USD = 6.00
#: A batch holds at most this many sections, and about this many characters of their text.
BATCH_SECTIONS = 8
BATCH_CHARS = 14_000
CALL_ESTIMATE_USD_MICROS = 150_000

# The governed agent path, shared with the regulatory instance — module attributes so a test
# swaps `_role_agent` here exactly as it does there.
_role_agent = _shared._role_agent
_attributed = _shared._attributed
_ran_as = _shared._ran_as
_parse_judgement = _shared._parse_judgement
MAX_TURNS = _shared.MAX_TURNS


class ExtractionRefused(ValueError):
    """The extraction cannot start, for a reason a person has to change."""


def _roles_dir():
    from pathlib import Path

    return (Path(__file__).resolve().parents[4] / "packages" / "specpack" / "skills"
            / "rulebook_extraction" / "roles")


def role_prompt() -> str:
    return (_roles_dir() / "rulebook_analyst.md").read_text(encoding="utf-8")


def clauses_of(connection: Connection, project_id: str, document_id: str) -> list[dict[str, Any]]:
    """The document's live clauses, in the document's order."""
    rows = connection.execute(text(
        "SELECT id, name, body, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'clause' "
        "AND valid_to IS NULL AND attrs->>'document_id' = :d "
        "ORDER BY (attrs->>'order')::int NULLS LAST, name"), {"p": project_id, "d": document_id}).mappings().all()
    return [dict(r) for r in rows]


def document_of(connection: Connection, project_id: str, document_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT id, name, attrs FROM kg_nodes WHERE project_id = :p AND id = :d AND kind = 'document' "
        "AND valid_to IS NULL"), {"p": project_id, "d": document_id}).mappings().first()
    if row is None:
        raise ExtractionRefused(f"There is no document {document_id!r} in this project: upload it in "
                                "Knowledge Graph › Documents first.")
    return dict(row)


def batches(clauses: Sequence[Mapping[str, Any]], *, size: int = BATCH_SECTIONS,
            chars: int = BATCH_CHARS) -> list[list[Mapping[str, Any]]]:
    """Consecutive clauses, at most ``size`` per batch and about ``chars`` of text (a long
    register section rides alone)."""
    out: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    held = 0
    for clause in clauses:
        length = len(str(clause.get("body") or ""))
        if current and (len(current) >= size or held + length > chars):
            out.append(current)
            current, held = [], 0
        current.append(clause)
        held += length
    if current:
        out.append(current)
    return out


def situation(title: str, batch: Sequence[Mapping[str, Any]]) -> str:
    parts = [f"DOCUMENT: {title}", "", "SECTIONS (cite by number):"]
    for i, clause in enumerate(batch, start=1):
        attrs = clause.get("attrs") or {}
        page = attrs.get("page")
        where = f" (p. {page})" if page else ""
        parts.append(f"\n[S{i}] {clause['name']}{where}\n{clause['body']}")
    return "\n".join(parts)


def _ask(agent: Any, prompt: str) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """One role turn: (the parsed judgement or None, what it cost)."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="boss-rulebook-") as cwd:
        result = agent.run_role(prompt, Path(cwd), read_only=True, max_turns=MAX_TURNS) or {}
        parsed = _parse_judgement(str(result.get("summary") or ""))
        if parsed is None and str(result.get("summary") or "").strip():
            # ONE MORE ASK, FOR THE ANSWER ONLY (the shared role path's rule).
            followup = (f"{prompt}\n\nWHAT YOU WROTE:\n{str(result.get('summary'))[:6000]}\n\n"
                        "Answer NOW: only the JSON object between the two marker lines "
                        f"({_shared._MARK_OPEN} / {_shared._MARK_CLOSE}), nothing else.")
            result = agent.run_role(followup, Path(cwd), read_only=True, max_turns=2) or {}
            parsed = _parse_judgement(str(result.get("summary") or ""))
    usage = dict(result.get("usage") or {}) if isinstance(result.get("usage"), Mapping) else {}
    if result.get("model") and not usage.get("model"):
        usage["model"] = result["model"]
    if not result.get("cost_unreported") and result.get("cost_usd_micros") is not None:
        usage["cost_usd_micros"] = int(result.get("cost_usd_micros") or 0)
    return parsed, {"usage": usage, "cost_unreported": bool(result.get("cost_unreported"))}


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in re.split(r"[;,]", value)]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v or "").strip()]


def _tables_of(columns: Sequence[str]) -> list[str]:
    """The tables a rule's printed ``table.column`` names belong to — derived, never guessed."""
    out: list[str] = []
    for column in columns:
        if "." in column:
            table = column.rsplit(".", 1)[0]
            if table not in out:
                out.append(table)
    return out


def _level(raw: Any, level: str) -> Optional[dict[str, Any]]:
    if isinstance(raw, Mapping):
        return {"level": level, "text": raw.get("text"), "metric": raw.get("metric"), "value": raw.get("value")}
    if isinstance(raw, str) and raw.strip():
        return {"level": level, "text": raw, "metric": None, "value": None}
    return None


def run_step(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Read the document's clauses in batches and propose the rules they state."""
    from .rule_scan import BudgetRefused, Governor
    from .rules import RuleRefused, propose_dq_rule, require_dq_kinds

    run_id = str(step.get("run_id") or (context or {}).get("run_id") or "")
    step_id = str(step.get("id") or "extract")
    document_id = str(inputs.get("document_id") or "").strip()
    if not document_id:
        raise ExtractionRefused("Name the document to read (document_id): upload it in Knowledge "
                                "Graph › Documents, then press Extract rules there.")
    try:
        require_dq_kinds(connection, project_id)
    except RuleRefused as refused:
        raise ExtractionRefused(str(refused)) from refused
    document = document_of(connection, project_id, document_id)
    clauses = clauses_of(connection, project_id, document_id)
    extraction_id = f"rbx_{run_id[-12:]}" if run_id else f"rbx_{uuid.uuid4().hex[:16]}"
    budget = int(round(float(inputs.get("budget_usd") or DEFAULT_BUDGET_USD) * 1_000_000))
    size = max(1, min(int(inputs.get("batch_size") or BATCH_SECTIONS), 20))
    proposed: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    unread: list[str] = []
    spent = 0
    calls = 0
    stopped_by = "document_read"
    agent = governor = None
    estimate = CALL_ESTIMATE_USD_MICROS
    for batch in batches(clauses, size=size):
        if agent is None:
            agent, engine = _role_agent(connection, settings, project_id)
            governor = Governor(connection, settings, project_id, engine=engine, ref=extraction_id)
            estimate = int(getattr(governor, "estimate_usd_micros", CALL_ESTIMATE_USD_MICROS))
        if spent + estimate > budget:
            stopped_by = "budget"
            unread += [str(c["name"]) for c in batch]
            continue
        try:
            held = governor.reserve()
        except BudgetRefused:
            stopped_by = "budget"
            unread += [str(c["name"]) for c in batch]
            continue
        prompt = f"{role_prompt()}\n\n{situation(document['name'], batch)}"
        try:
            with _attributed(connection, project_id, run_id, step_id, ROLE):
                judged, cost = _ask(agent, prompt)
        except Exception as error:  # noqa: BLE001 — the batch goes unread, said in the result
            governor.fail(held, error)
            unread += [str(c["name"]) for c in batch]
            continue
        calls += 1
        settled = int(governor.settle(held, cost["usage"]) or 0)
        spent += settled or (estimate if cost["cost_unreported"] else 0)
        if not isinstance(judged, Mapping):
            unread += [str(c["name"]) for c in batch]
            continue
        by_ref = {f"S{i}": c for i, c in enumerate(batch, start=1)}
        for item in judged.get("skipped") or []:
            if isinstance(item, Mapping) and str(item.get("section") or "").upper() in by_ref:
                skipped.append({"section": str(by_ref[str(item["section"]).upper()]["name"]),
                                "why": " ".join(str(item.get("why") or "").split())[:200]})
        for raw in judged.get("rules") or []:
            if not isinstance(raw, Mapping):
                continue
            clause = by_ref.get(str(raw.get("section") or "").strip().upper())
            rule_id = str(raw.get("rule_id") or "").strip()
            if clause is None:
                refused.append({"rule_id": rule_id or "(no id)",
                                "why": "it cites no section of the batch it was given"})
                continue
            columns = _strings(raw.get("columns"))
            try:
                with connection.begin_nested():
                    rule = propose_dq_rule(
                        connection, project_id=project_id, rule_id=rule_id,
                        title=str(raw.get("title") or ""), statement=str(raw.get("statement") or ""),
                        clause=str(clause["id"]), quote=str(raw.get("quote") or ""),
                        version=raw.get("version") if isinstance(raw.get("version"), int) else None,
                        dimension=str(raw.get("dimension") or ""), severity=str(raw.get("severity") or ""),
                        check=str(raw.get("check") or ""), owner=str(raw.get("owner") or ""),
                        system=str(raw.get("system") or ""), feed=str(raw.get("feed") or ""),
                        cdes=_strings(raw.get("cdes")), columns=columns,
                        tables=_strings(raw.get("tables")) or _tables_of(columns),
                        report_lines=_strings(raw.get("report_lines")), mdrm=_strings(raw.get("mdrm")),
                        related=_strings(raw.get("related")),
                        legal_reference=str(raw.get("legal_reference") or ""),
                        thresholds=[t for t in (_level(raw.get("warning"), "warning"),
                                                _level(raw.get("error"), "error")) if t],
                        confidence=raw.get("confidence") if raw.get("confidence") is not None else 0.7,
                        scan_id=extraction_id, actor_kind="agent", actor_ref=ROLE)
            except (RuleRefused, LookupError) as error:
                refused.append({"rule_id": rule_id or "(no id)", "why": str(error)[:300]})
                continue
            proposed.append({"node_id": rule["node"]["id"], "rule_id": rule_id,
                             "thresholds": len((rule.get("dq") or {}).get("thresholds") or [])})
    unique = {p["rule_id"]: p for p in proposed}
    thresholds = sum(p["thresholds"] for p in unique.values())
    statement = (f"Read {len(clauses) - len(unread)} of {len(clauses)} sections of “{document['name']}” and "
                 f"proposed {len(unique)} rule(s) with {thresholds} threshold(s) for review.")
    if refused:
        statement += f" {len(refused)} proposal(s) were refused (each with its reason)."
    if unread:
        statement += (f" {len(unread)} section(s) went unread"
                      + (" (the budget ran out)." if stopped_by == "budget" else " (no readable answer)."))
    return {
        "capability": CAPABILITY, "tool": None, "status": "succeeded",
        "extraction_id": extraction_id, "scan_id": extraction_id, "document_id": document_id,
        "sections_read": len(clauses) - len(unread), "sections": len(clauses),
        "rules_proposed": len(unique), "thresholds_proposed": thresholds,
        "proposed_rule_ids": sorted(unique), "refused": refused[:50], "skipped": skipped[:200],
        "unread_sections": unread[:200], "model_calls": calls,
        "spend_usd": round(spent / 1_000_000, 4), "stopped_by": stopped_by,
        "summary": statement, "note": statement,
        "review_link": f"/?area=knowledge&kg_tab=review&kg_scan={extraction_id}",
        "produced": {"agent_decision": {"decision_type": "RulebookExtracted", "statement": statement}},
        "ran_as": _ran_as(ROLE),
    }


def proposals_of(connection: Connection, project_id: str, *, scan_id: Optional[str] = None,
                 document_id: Optional[str] = None) -> list[dict[str, Any]]:
    """The data-quality rules an extraction proposed, in the measuring shape
    (``scripts/kg/measure_rulebook_extraction.py``): the rule's fields and its thresholds."""
    from .rules import DQ_RULE_KIND, DQ_THRESHOLD_KIND

    params: dict[str, Any] = {"p": project_id, "k": DQ_RULE_KIND}
    where = "project_id = :p AND kind = :k AND (valid_to IS NULL OR attrs->>'status' = 'superseded')"
    if scan_id:
        where += " AND attrs->>'scan_id' = :s"
        params["s"] = scan_id
    if document_id:
        where += " AND attrs->'dq'->>'document_id' = :d"
        params["d"] = document_id
    rows = connection.execute(text(f"SELECT id, attrs FROM kg_nodes WHERE {where}"), params).mappings().all()
    thresholds = {}
    for t in connection.execute(text(
            "SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind = :k"),
            {"p": project_id, "k": DQ_THRESHOLD_KIND}).mappings():
        a = t["attrs"] or {}
        thresholds[(a.get("rule_id"), a.get("level"))] = a
    out = []
    for row in rows:
        attrs = row["attrs"] or {}
        dq = dict(attrs.get("dq") or {})
        rid = dq.get("rule_id")
        out.append({**dq, "status": attrs.get("status"), "statement": attrs.get("statement"),
                    "warning": thresholds.get((rid, "warning")), "error": thresholds.get((rid, "error")),
                    "links": attrs.get("links") or {}})
    return out


def as_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)
