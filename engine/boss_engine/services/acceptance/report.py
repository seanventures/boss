"""ACCEPTANCE REPORT v2, CARD PAYLOAD & TICKET COMMENT (AA-H, BOSSAPP-1223; LLD §7).

One merged run report (`report_version: 2`) — never a second report. Leads with:
  1. Verdict banner (respecting I4: never says "Accepted" or "passed" while open >= MAJOR
     ledger rows or open contract items remain)
  2. Must-haves table (missing -> partial -> unjudged -> human -> met; evidence + ticket)
  3. Open gaps & tickets (ledger rows >= minor, follow-up run link, filed tickets)
  4. Details divider -> What it is / What shipped / How to use it / How it was checked /
     What to do next / Problems during the run / Run facts.

Also supplies:
  - `latest_for_run` — newest acceptance check for a workflow run / lineage (`layer='step'`
    preferred over `layer='build_gate'` on the same head SHA)
  - `card_payload` — superset of PR #136's `recommendation.acceptance`
  - `ticket_comment` + `comment_digest` — compact markdown comment posted only on change
  - `refresh_run_report` — updates the single `workflow_report` artifact in place (same ID)
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text

from . import checks as _checks
from . import contract as _contract
from . import ledger as _ledger
from . import verdict as _verdict

REPORT_VERSION = 2

#: Ordering in the Must-haves table (LLD §7): missing -> partial -> unjudged -> human -> met.
_STATUS_ORDER: dict[str, int] = {
    _verdict.MISSING: 0,
    _verdict.PARTIAL: 1,
    _verdict.UNJUDGED: 2,
    _verdict.HUMAN: 3,
    _verdict.MET: 4,
}

_STATUS_LABEL: dict[str, str] = {
    _verdict.MET: "met",
    _verdict.PARTIAL: "PARTIAL",
    _verdict.MISSING: "MISSING",
    _verdict.UNJUDGED: "NOT JUDGED",
    _verdict.HUMAN: "NEEDS PERSON",
}

_BLOCKING_SEVERITIES = frozenset({"blocker", "major"})


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except ValueError:
            return {}
    return {}


def _ticket_bindings(connection: Any, project_id: str,
                     work_item_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """{work_item_id: {provider, key, url, state, sync_status}} from work_tracker_bindings."""
    ids = [str(w) for w in work_item_ids if w]
    if not ids or connection is None:
        return {}
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text(
                    "SELECT b.work_item_id, p.provider_key, b.external_key, b.external_issue_id, "
                    "       b.external_url, b.external_state, b.sync_status "
                    "  FROM work_tracker_bindings b "
                    "  LEFT JOIN work_tracker_providers p ON p.id = b.provider_id "
                    " WHERE b.project_id = :p AND b.work_item_id = ANY(:ids) "
                    " ORDER BY b.updated_at DESC"
                ),
                {"p": project_id, "ids": ids},
            ).mappings().all()
    except Exception:  # noqa: BLE001 — optional bindings never fail a report
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        wid = str(r["work_item_id"])
        if wid in out:
            continue
        out[wid] = {
            "provider": r["provider_key"],
            "key": r["external_key"] or r["external_issue_id"],
            "url": r["external_url"],
            "state": r["external_state"],
            "sync_status": r["sync_status"],
        }
    return out


def enrich_ledger_rows(connection: Any, project_id: str,
                       rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Attach linked external ticket metadata (`ticket`) to each ledger row."""
    materialised = [dict(r) for r in (rows or [])]
    bindings = _ticket_bindings(connection, project_id,
                                [r.get("work_item_id") for r in materialised if r.get("work_item_id")])
    out: list[dict[str, Any]] = []
    for r in materialised:
        wid = str(r.get("work_item_id") or "")
        if wid and wid in bindings and not r.get("ticket"):
            r["ticket"] = bindings[wid]
        if not r.get("key") and r.get("number") is not None:
            r["key"] = _ledger.human_key(r)
        out.append(r)
    return out


def has_blocking_open_rows(ledger_rows: Iterable[Mapping[str, Any]]) -> bool:
    """Invariant I4: True if any live row is severity >= MAJOR or an open acceptance_gap."""
    for r in ledger_rows or []:
        st = str(r.get("status") or "open").lower()
        if st not in _ledger.LIVE:
            continue
        if str(r.get("kind") or "").lower() == "acceptance_gap":
            return True
        if str(r.get("severity") or "").lower() in _BLOCKING_SEVERITIES:
            return True
    return False


def _format_diff(diff: Optional[Mapping[str, Any]]) -> str:
    d = dict(diff or {})
    parts: list[str] = []
    newly = list(d.get("newly_met") or [])
    reg = list(d.get("regressed") or [])
    still = list(d.get("still_open") or [])
    if newly:
        parts.append(f"+{len(newly)} met")
    if reg:
        parts.append(f"{len(reg)} regressed")
    if still and not parts:
        parts.append(f"{len(still)} still open")
    return ", ".join(parts)


def verdict_banner(check: Mapping[str, Any], *,
                   open_rows: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Verdict banner data for the report and UI, enforcing Invariant I4."""
    c = dict(check.get("counts") or {})
    verdict = str(check.get("verdict") or "not_run").lower()
    open_list = list(open_rows or [])
    blocking = has_blocking_open_rows(open_list)
    items = list(check.get("items") or [])
    open_items = [i for i in items if str(i.get("status") or "") not in (_verdict.MET,)]

    accepted = verdict in ("met", "no_items") and not blocking and not open_items

    bits: list[str] = []
    met_n = int(c.get(_verdict.MET, 0) or 0)
    partial_n = int(c.get(_verdict.PARTIAL, 0) or 0)
    missing_n = int(c.get(_verdict.MISSING, 0) or 0)
    human_n = int(c.get(_verdict.HUMAN, 0) or 0)
    unjudged_n = int(c.get(_verdict.UNJUDGED, 0) or 0)

    if met_n or verdict == "met":
        bits.append(f"{met_n} met")
    if partial_n:
        bits.append(f"{partial_n} partial")
    if missing_n:
        bits.append(f"{missing_n} missing")
    if unjudged_n:
        bits.append(f"{unjudged_n} not judged")
    if human_n:
        bits.append(f"{human_n} need a person")
    if blocking and verdict in ("met", "no_items"):
        open_major = sum(
            1 for r in open_list
            if str(r.get("status") or "open") in _ledger.LIVE
            and (r.get("kind") == "acceptance_gap"
                 or str(r.get("severity") or "").lower() in _BLOCKING_SEVERITIES)
        )
        bits.append(f"{open_major} open finding{'s' if open_major != 1 else ''}")

    counts_text = " · ".join(bits) if bits else (check.get("why") or verdict)
    if accepted:
        headline = f"VERDICT: ✓ Accepted — {counts_text}"
    elif verdict == "not_run":
        headline = f"VERDICT: ! Not checked — {check.get('why') or 'check did not run'}"
    else:
        headline = f"VERDICT: ✗ Not accepted — {counts_text}"

    mode = str(check.get("mode") or "static")
    sha = str(check.get("head_sha") or "")[:7]
    contract = check.get("contract") if isinstance(check.get("contract"), Mapping) else {}
    sources_str = str(contract.get("sources_label") or "")
    if not sources_str and isinstance(contract.get("sources"), list):
        kinds = [str(s.get("ref") or s.get("kind") or "") for s in contract["sources"]
                 if isinstance(s, Mapping) and (s.get("ref") or s.get("kind"))]
        sources_str = " + ".join(dict.fromkeys(kinds))
    ver = contract.get("version")
    n_items = contract.get("items_count") or len(items)

    detail_parts = [f"Checked {mode}" + (f" at `{sha}`" if sha else "")]
    if sources_str:
        detail_parts[0] += f" against {sources_str}"
    if ver or n_items:
        cv = f"contract v{ver}" if ver else "contract"
        detail_parts[0] += f" ({cv}, {n_items} items)"
    diff_line = _format_diff(check.get("diff"))
    if diff_line:
        detail_parts.append(f"since last check: {diff_line}")
    if check.get("why") == "budget":
        detail_parts.append(str(check.get("budget_note") or "stopped at budget"))

    return {
        "accepted": accepted,
        "verdict": "met" if accepted else ("not_run" if verdict == "not_run" else "not_met"),
        "headline": headline,
        "detail": " · ".join(detail_parts),
        "diff_line": diff_line,
        "counts": {
            "met": met_n, "partial": partial_n, "missing": missing_n,
            "human": human_n, "unjudged": unjudged_n,
        },
    }


def ordered_items(check: Mapping[str, Any], *,
                  ledger_rows: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """Contract items ordered missing -> partial -> unjudged -> human -> met, with ticket info."""
    by_item: dict[str, dict[str, Any]] = {}
    for row in ledger_rows or []:
        cid = str(row.get("contract_item_id") or "").strip()
        fp = str(row.get("fingerprint") or "").strip()
        key = cid or (fp[3:] if fp.startswith("ac:") else "")
        if key and key not in by_item:
            by_item[key] = dict(row)

    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(check.get("items") or [], start=1):
        it = dict(raw)
        item_id = str(it.get("id") or f"AC-{it.get('n') or idx}")
        matched = by_item.get(item_id)
        ticket = matched.get("ticket") if matched else it.get("ticket")
        out.append({
            "n": int(it.get("n") or idx),
            "id": item_id,
            "text": str(it.get("text") or ""),
            "status": str(it.get("status") or _verdict.UNJUDGED).lower(),
            "evidence": str(it.get("evidence") or ""),
            "source": str(it.get("source") or ""),
            "ticket": ticket,
            "finding_key": (matched.get("key") or _ledger.human_key(matched)) if matched else None,
            "replayed": bool(it.get("replayed")),
            "flaky": bool(it.get("flaky")),
        })
    out.sort(key=lambda r: (_STATUS_ORDER.get(r["status"], 9), r["n"]))
    return out


def _cell(value: Any) -> str:
    return " ".join(str(value or "").split()).replace("|", "\\|")


def _ticket_md(ticket: Optional[Mapping[str, Any]], finding_key: Optional[str] = None) -> str:
    if isinstance(ticket, Mapping) and ticket.get("key"):
        key = str(ticket["key"])
        url = str(ticket.get("url") or "").strip()
        return f"[{key}]({url})" if url else key
    return str(finding_key or "—")


def must_haves_markdown(check: Mapping[str, Any], *,
                        ledger_rows: Iterable[Mapping[str, Any]] = ()) -> str:
    """The Must-haves markdown table (LLD §7): `| # | Must-have | Status | Evidence | Ticket |`."""
    rows = ordered_items(check, ledger_rows=ledger_rows)
    if not rows:
        return ""
    lines = ["| # | Must-have | Status | Evidence | Ticket |", "|---|---|---|---|---|"]
    for r in rows:
        st = _STATUS_LABEL.get(r["status"], r["status"])
        ev = _cell(r["evidence"][:240]) or "—"
        tk = _ticket_md(r.get("ticket"), r.get("finding_key"))
        lines.append(f"| {r['id']} | {_cell(r['text'][:200])} | {st} | {ev} | {tk} |")
    return "\n".join(lines)


def card_payload(
    check: Mapping[str, Any],
    ledger_rows: Iterable[Mapping[str, Any]] = (),
    *,
    report_artifact_id: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> dict[str, Any]:
    """Merge/step card `recommendation.acceptance` payload (LLD §7).

    Superset of PR #136's shape (`verdict, counts, items, stack, why, items_dropped, table`)
    plus `layer, mode, check_id, diff, open_tickets, report_artifact_id, accepted, headline`.
    """
    rows = list(ledger_rows or [])
    banner = verdict_banner(check, open_rows=rows)
    items = ordered_items(check, ledger_rows=rows)
    open_tickets: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("status") or "open") not in _ledger.LIVE:
            continue
        tk = r.get("ticket") if isinstance(r.get("ticket"), Mapping) else None
        open_tickets.append({
            "finding_id": r.get("id"),
            "finding_key": r.get("key") or (_ledger.human_key(r) if r.get("number") else None),
            "severity": r.get("severity"),
            "kind": r.get("kind"),
            "title": r.get("title"),
            "followup_run_id": r.get("followup_run_id"),
            **({"ticket_key": tk.get("key"), "ticket_url": tk.get("url")} if tk else {}),
        })

    return {
        "verdict": banner["verdict"],
        "accepted": banner["accepted"],
        "headline": banner["headline"],
        "detail": banner["detail"],
        "counts": banner["counts"],
        "items": items,
        "stack": dict(check.get("stack") or {}),
        "why": str(check.get("why") or ""),
        "items_dropped": int(check.get("items_dropped") or 0),
        "table": must_haves_markdown(check, ledger_rows=rows),
        "layer": str(check.get("layer") or "build_gate"),
        "mode": str(check.get("mode") or "static"),
        "check_id": check.get("id") or check.get("check_id"),
        "head_sha": head_sha or check.get("head_sha"),
        "diff": dict(check.get("diff") or {}),
        "open_tickets": open_tickets,
        "report_artifact_id": report_artifact_id or check.get("report_artifact_id"),
    }


def comment_digest(check: Mapping[str, Any], *,
                   open_rows: Iterable[Mapping[str, Any]] = ()) -> str:
    """Stable hash of `(verdict, item statuses)` so ticket comments post ONLY on change (LLD §7)."""
    items = [
        (str(i.get("id") or i.get("n")), str(i.get("status") or ""))
        for i in (check.get("items") or [])
    ]
    items.sort()
    banner = verdict_banner(check, open_rows=open_rows)
    payload = json.dumps({"verdict": banner["verdict"], "items": items}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def ticket_comment(
    check: Mapping[str, Any],
    ledger_rows: Iterable[Mapping[str, Any]] = (),
    *,
    report_url: Optional[str] = None,
    max_rows: int = 12,
) -> str:
    """Compact markdown comment for the source ticket (LLD §7): verdict, <=12 rows (missing
    first, then '+N more'), open gap tickets, and report link. Enforces I4."""
    rows = list(ledger_rows or [])
    banner = verdict_banner(check, open_rows=rows)
    items = ordered_items(check, ledger_rows=rows)
    lines = [f"**{banner['headline']}**", f"_{banner['detail']}_", ""]
    if items:
        shown = items[:max_rows]
        lines += ["| # | Must-have | Status | Ticket |", "|---|---|---|---|"]
        for it in shown:
            st = _STATUS_LABEL.get(it["status"], it["status"])
            tk = _ticket_md(it.get("ticket"), it.get("finding_key"))
            lines.append(f"| {it['id']} | {_cell(it['text'][:140])} | {st} | {tk} |")
        extra = len(items) - len(shown)
        if extra > 0:
            lines.append(f"\n_+{extra} more item{'s' if extra != 1 else ''}_")
        lines.append("")
    open_filed = [
        r for r in rows
        if str(r.get("status") or "open") in _ledger.LIVE
        and isinstance(r.get("ticket"), Mapping) and r["ticket"].get("key")
    ]
    if open_filed:
        chips = [_ticket_md(r["ticket"]) for r in open_filed[:12]]
        lines.append("**Gap tickets:** " + ", ".join(chips))
    if report_url:
        lines.append(f"**Full run report:** {report_url}")
    return "\n".join(lines).strip()


def latest_for_lineage(
    connection: Any,
    project_id: str,
    *,
    lineage_root_run_id: Optional[str] = None,
    workflow_run_id: Optional[str] = None,
    pr_url: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Newest acceptance check for a lineage / workflow / PR, preferring `layer='step'` over
    `layer='build_gate'` when both checked the same head SHA (LLD §7)."""
    if connection is None:
        return None
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text(
                    "SELECT * FROM acceptance_checks "
                    " WHERE project_id = :p "
                    "   AND ("
                    "     (CAST(:l AS TEXT) IS NOT NULL AND lineage_root_run_id = :l) "
                    "     OR (CAST(:w AS TEXT) IS NOT NULL AND workflow_run_id = :w) "
                    "     OR (CAST(:u AS TEXT) IS NOT NULL AND pr_url = :u)"
                    "   ) "
                    "   AND (CAST(:h AS TEXT) IS NULL OR head_sha = :h) "
                    "   AND COALESCE(status, 'completed') = 'completed' "
                    " ORDER BY created_at DESC, id DESC LIMIT 10"
                ),
                {"p": project_id, "l": lineage_root_run_id, "w": workflow_run_id,
                 "u": pr_url, "h": head_sha},
            ).mappings().all()
    except Exception:  # noqa: BLE001 — missing column/table degrades gracefully
        return None
    if not rows:
        return None
    materialised = [_checks._row(r) for r in rows]  # noqa: SLF001
    # Prefer layer='step' over layer='build_gate' on the newest head SHA.
    newest_sha = materialised[0].get("head_sha")
    if newest_sha:
        same_head = [r for r in materialised if r.get("head_sha") == newest_sha]
        step_row = next((r for r in same_head if r.get("layer") == "step"), None)
        chosen = step_row or same_head[0]
    else:
        step_row = next((r for r in materialised if r.get("layer") == "step"), None)
        chosen = step_row or materialised[0]

    # Attach contract metadata when available.
    if chosen.get("contract_id"):
        try:
            with connection.begin_nested():
                c_row = _contract.get(connection, str(chosen["contract_id"]))
            if c_row:
                chosen["contract"] = {
                    "id": c_row["id"],
                    "version": c_row.get("version"),
                    "sources": list(c_row.get("sources") or []),
                    "items_count": len(c_row.get("items") or []),
                }
        except Exception:  # noqa: BLE001
            pass
    return chosen


def latest_for_run(
    connection: Any,
    project_id: str,
    workflow_run_id: str,
    *,
    build_run_id: Optional[str] = None,
    pr_url: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Newest acceptance check for one workflow run (called by `run_retrospective.assemble_workflow`)."""
    lineage = None
    if build_run_id:
        try:
            with connection.begin_nested():
                lin = _ledger.lineage_of(connection, build_run_id)
                lineage = (lin or {}).get("lineage") or build_run_id
        except Exception:  # noqa: BLE001
            lineage = build_run_id
    check = latest_for_lineage(
        connection,
        project_id,
        lineage_root_run_id=lineage,
        workflow_run_id=workflow_run_id,
        pr_url=pr_url,
    )
    if check is None:
        return None
    lin_id = str(check.get("lineage_root_run_id") or lineage or "")
    open_rows: list[dict[str, Any]] = []
    if lin_id:
        try:
            with connection.begin_nested():
                open_rows = enrich_ledger_rows(
                    connection, project_id,
                    _ledger.open_for(connection, project_id=project_id, lineage=lin_id,
                                     min_severity="minor"),
                )
        except Exception:  # noqa: BLE001
            open_rows = []
    check["banner"] = verdict_banner(check, open_rows=open_rows)
    check["ordered_items"] = ordered_items(check, ledger_rows=open_rows)
    check["open_rows"] = open_rows
    return check


def refresh_run_report(
    connection: Any,
    project_id: str,
    workflow_run_id: str,
    *,
    settings: Any = None,
) -> dict[str, Any]:
    """Assemble and save/update the single `workflow_report` artifact IN PLACE (LLD §7).

    Keeps the same artifact ID when one already exists for `workflow_run_id`, so links never go
    stale when acceptance runs after the `report` step or on a re-check.
    """
    from ...config import get_settings
    from ..core import create_artifact
    from ..run_retrospective import assemble_workflow, workflow_markdown

    report = assemble_workflow(connection, project_id, workflow_run_id)
    if not report.get("ok"):
        return {"ok": False, "error": report.get("error") or "workflow run not found"}

    markdown = workflow_markdown(report)
    title = "Run report — " + str(
        (report.get("product") or {}).get("name")
        or report.get("workflow")
        or workflow_run_id
    )
    content = {
        "markdown": markdown,
        "run_id": workflow_run_id,
        "report_version": int(report.get("report_version") or (REPORT_VERSION if report.get("acceptance") else 1)),
        "report": report,
    }

    existing = connection.execute(
        text(
            "SELECT id FROM artifacts WHERE project_id = :p "
            "  AND artifact_type = 'workflow_report' "
            "  AND content->>'run_id' = :r ORDER BY created_at DESC LIMIT 1"
        ),
        {"p": project_id, "r": workflow_run_id},
    ).mappings().first()
    if existing:
        connection.execute(
            text("UPDATE artifacts SET title = :t, content = CAST(:c AS JSONB) WHERE id = :a"),
            {"t": title, "c": json.dumps(content, default=str), "a": existing["id"]},
        )
        return {
            "ok": True,
            "artifact_id": str(existing["id"]),
            "created": False,
            "report_version": content["report_version"],
        }

    cfg = settings if settings is not None else get_settings()
    artifact_id = create_artifact(
        connection, cfg, project_id, None, "workflow_report", title, content,
    )
    return {
        "ok": True,
        "artifact_id": str(artifact_id),
        "created": True,
        "report_version": content["report_version"],
    }


__all__ = [
    "REPORT_VERSION",
    "card_payload",
    "comment_digest",
    "enrich_ledger_rows",
    "has_blocking_open_rows",
    "latest_for_lineage",
    "latest_for_run",
    "must_haves_markdown",
    "ordered_items",
    "refresh_run_report",
    "ticket_comment",
    "verdict_banner",
]
