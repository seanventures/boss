"""ACCEPTANCE TICKET FILING & STATUS SYNC (AA-J, BOSSAPP-1225; boss-acceptance-agent-lld.md §9).

Routes every finding in a lineage to the project's connected ticket system (or BOSS work items
only when no external tracker with `create` capability is connected, or when filing is switched
off for the project):

  - BLOCKER / MAJOR / `kind='acceptance_gap'`: auto-filed as individual tickets (up to
    `policy.max_new_tickets`, default 10, per check; overflow goes into one digest ticket).
    Child of the source ticket when the connector supports parenting, else linked via
    `adapter.link` + labels `["boss-acceptance", severity]`.
  - MINOR (non-gap): batched into ONE `"Polish: <title>"` ticket per lineage, updated in place
    with a comment when new minor items arrive.
  - NIT: ledger only (never filed externally).
  - Fixed rows: comment `"Verified fixed by acceptance check <id>: <evidence>"` + transition `done`.
  - Reopened rows (regressions): comment + transition `todo`.
  - Waived / wont_fix rows: comment with reason (no transition unless explicitly requested).
  - Merged/duplicate rows: comment `"Merged into F-<n>"` when the duplicate already had a ticket.

Every external write is preceded by a `Receipt` (`ReceiptService.create_or_reuse`) with dedupe
key `ticket-sync:{project}:{provider}:{key}:{lineage}:{action}:{op}:{digest}`, so retries and
replays NEVER create duplicate tickets or duplicate comments.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from sqlalchemy import text

from . import ledger as _ledger
from .policy import AcceptancePolicy

_log = logging.getLogger(__name__)

MAX_NEW_TICKETS_DEFAULT = 10
MINOR_BATCH_SUBJECT_TYPE = "finding_minor_batch"
DIGEST_SUBJECT_TYPE = "finding_digest"


@dataclass
class FilingResult:
    mode: str = "synced"  # synced | boss_work_items_only | disabled
    provider: Optional[str] = None
    filed: list[dict[str, Any]] = field(default_factory=list)
    batched_minors: list[dict[str, Any]] = field(default_factory=list)
    digest_ticket: Optional[dict[str, Any]] = None
    closed: list[dict[str, Any]] = field(default_factory=list)
    reopened: list[dict[str, Any]] = field(default_factory=list)
    waived_commented: list[dict[str, Any]] = field(default_factory=list)
    merged_commented: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    receipts: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _digest(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _evidence_text(row: Mapping[str, Any]) -> str:
    ev = row.get("evidence")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except ValueError:
            return ev.strip()
    if isinstance(ev, list):
        parts = []
        for item in ev:
            if isinstance(item, Mapping):
                ref = item.get("ref") or item.get("path") or ""
                excerpt = item.get("excerpt") or item.get("text") or item.get("why") or ""
                parts.append(f"{ref}: {excerpt}".strip(": "))
            elif item:
                parts.append(str(item))
        return "; ".join(p for p in parts if p)
    return str(ev or "").strip()


def _verify_with_text(row: Mapping[str, Any]) -> str:
    probes = row.get("verify_with")
    if isinstance(probes, str):
        try:
            probes = json.loads(probes)
        except ValueError:
            return probes.strip()
    if not isinstance(probes, list) or not probes:
        return ""
    lines = []
    for p in probes:
        if isinstance(p, Mapping):
            action = p.get("action") or {}
            expect = p.get("expect") or {}
            cmd = p.get("cmd") or (
                " ".join(str(a) for a in action.get("argv") or [])
                if isinstance(action, Mapping) and action.get("argv")
                else f"{action.get('method', 'GET')} {action.get('url', '')}".strip()
                if isinstance(action, Mapping) and action.get("url")
                else ""
            )
            exp = ", ".join(f"{k}={v}" for k, v in expect.items()) if isinstance(expect, Mapping) else str(expect)
            lines.append(f"`{cmd}` -> {exp}" if cmd and exp else (f"`{cmd}`" if cmd else exp))
        elif p:
            lines.append(str(p))
    return "\n".join(f"- {ln}" for ln in lines if ln)


def ticket_body(
    row: Mapping[str, Any],
    *,
    pr_url: Optional[str] = None,
    branch: Optional[str] = None,
    run_url: Optional[str] = None,
    source_ticket: Optional[Mapping[str, Any]] = None,
    contract_text: Optional[str] = None,
) -> str:
    """Markdown body for a filed finding ticket (LLD §9).

    TOP line carries PR link · branch · run link · source ticket · status
    (tickets-must-link-the-work rule), followed by The requirement, What is wrong,
    Done when (verify_with probe), and History.
    """
    pr = str(pr_url or row.get("pr_url") or "").strip()
    src_key = str((source_ticket or {}).get("key") or "").strip()
    src_url = str((source_ticket or {}).get("url") or "").strip()
    src_label = f"[{src_key}]({src_url})" if src_key and src_url else src_key

    top_bits: list[str] = []
    if pr:
        top_bits.append(f"**PR:** {pr}")
    if branch:
        top_bits.append(f"**Branch:** `{branch}`")
    if run_url:
        top_bits.append(f"**Run:** {run_url}")
    if src_label:
        top_bits.append(f"**Source ticket:** {src_label}")
    top_bits.append(f"**Status:** {row.get('status') or 'open'}")
    top_bits.append(f"**Finding:** {row.get('key') or _ledger.human_key(row)}")

    lines = [" · ".join(top_bits), ""]
    req = str(contract_text or row.get("body") or row.get("title") or "").strip()
    lines += ["### The requirement", "", req, ""]

    ev = _evidence_text(row)
    where = ""
    if row.get("path"):
        where = f"`{row['path']}:{row['line']}`" if row.get("line") else f"`{row['path']}`"
    wrong = " — ".join(p for p in (where, ev or str(row.get("body") or "")) if p)
    lines += ["### What is wrong", "", wrong or str(row.get("title") or ""), ""]

    verify = _verify_with_text(row)
    if verify:
        lines += ["### Done when", "", verify, ""]

    seen = int(row.get("seen_count") or 1)
    reopened = int(row.get("reopened_count") or 0)
    hist = f"Seen {seen}x" + (f" · reopened {reopened}x" if reopened else "")
    lines += ["### History", "", hist]
    return "\n".join(lines).strip()


def _scope_row(db: Any, project_id: str) -> Mapping[str, Any]:
    row = db.execute(
        text(
            "SELECT p.workspace_id, w.org_id FROM projects p "
            "  JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": project_id},
    ).mappings().first()
    if not row:
        raise ValueError(f"acceptance filing: unknown project {project_id}")
    return row


def _default_provider_row(db: Any, project_id: str) -> Optional[dict[str, Any]]:
    from .. import ticket_sync

    rows = db.execute(
        text(
            "SELECT id, project_id, provider_key, display_name, status, is_default, "
            "       configuration, created_by "
            "  FROM work_tracker_providers "
            " WHERE project_id = :p AND status = 'active' "
            " ORDER BY is_default DESC, created_at ASC"
        ),
        {"p": project_id},
    ).mappings().all()
    for r in rows:
        out = dict(r)
        cfg = out.get("configuration")
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                cfg = {}
        out["configuration"] = dict(cfg or {})
        home = ticket_sync.home_row(db, out)
        if home:
            out["home"] = home
        return out
    return None


def _auto_file_enabled(provider_row: Mapping[str, Any], policy: AcceptancePolicy) -> bool:
    if policy.file in ("off", "ledger_only"):
        return False
    cfg = dict(provider_row.get("configuration") or {})
    settings_block = cfg.get("settings") if isinstance(cfg.get("settings"), Mapping) else {}
    if settings_block.get("auto_file_acceptance_findings") is False:
        return False
    if cfg.get("auto_file_acceptance_findings") is False:
        return False
    return True


def _binding_for_work_item(db: Any, project_id: str, work_item_id: Optional[str],
                           provider_id: str) -> Optional[dict[str, Any]]:
    if not work_item_id:
        return None
    row = db.execute(
        text(
            "SELECT * FROM work_tracker_bindings "
            " WHERE project_id = :p AND work_item_id = :w AND provider_id = :pid "
            " ORDER BY updated_at DESC LIMIT 1"
        ),
        {"p": project_id, "w": work_item_id, "pid": provider_id},
    ).mappings().first()
    return dict(row) if row else None


def _save_binding(
    db: Any,
    *,
    project_id: str,
    work_item_id: str,
    provider_id: str,
    provider_key: str,
    external_id: Optional[str],
    external_key: str,
    external_url: str,
    external_state: str = "todo",
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    from ..core import make_id

    existing = _binding_for_work_item(db, project_id, work_item_id, provider_id)
    if existing:
        db.execute(
            text(
                "UPDATE work_tracker_bindings "
                "   SET external_issue_id = :eid, external_key = :ek, external_url = :eu, "
                "       external_state = :st, sync_status = 'synced', sync_error = '{}'::jsonb, "
                "       last_synced_at = now(), updated_at = now() "
                " WHERE id = :id"
            ),
            {"eid": external_id, "ek": external_key, "eu": external_url,
             "st": external_state, "id": existing["id"]},
        )
        return str(existing["id"])
    bid = make_id("wtb")
    db.execute(
        text(
            "INSERT INTO work_tracker_bindings "
            "  (id, project_id, work_item_id, provider_id, "
            "   external_issue_id, external_key, external_url, external_state, "
            "   sync_status, metadata, last_synced_at) "
            "VALUES (:id, :p, :w, :pid, :eid, :ek, :eu, :st, 'synced', "
            "        CAST(:m AS JSONB), now())"
        ),
        {
            "id": bid, "p": project_id, "w": work_item_id, "pid": provider_id,
            "eid": external_id, "ek": external_key,
            "eu": external_url, "st": external_state,
            "m": json.dumps({**dict(metadata or {}), "provider_key": provider_key}, default=str),
        },
    )
    return bid


def _ensure_receipt_and_run(
    db: Any,
    *,
    project_id: str,
    provider: str,
    key_part: str,
    lineage: str,
    action: str,
    op: str,
    digest_input: Any,
    scope: Mapping[str, Any],
    fn: Any,
    result_acc: FilingResult,
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """Create-or-reuse Receipt before calling external tracker; returns (executed_now, output, err)."""
    from ..receipts import ReceiptService

    dedupe = ":".join((
        "ticket-sync", project_id, provider, str(key_part), lineage,
        action, op, _digest(digest_input),
    ))
    where = _scope_row(db, project_id)
    receipts = ReceiptService()
    receipt, created = receipts.create_or_reuse(
        db, where["org_id"], where["workspace_id"], project_id, dedupe,
        f"ticket.{op}", dict(scope), {"action": action, "op": op},
    )
    result_acc.receipts.append(str(receipt["id"]))
    if not created:
        prev_out = receipt.get("output")
        if isinstance(prev_out, str):
            try:
                prev_out = json.loads(prev_out)
            except ValueError:
                prev_out = {}
        return False, dict(prev_out or {}), None
    try:
        out = dict(fn() or {})
        receipts.mark_succeeded(
            db, receipt["id"], str(out.get("key") or key_part),
            {**out, "op": op, "action": action},
        )
        return True, out, None
    except Exception as exc:  # noqa: BLE001 — filing failure never crashes the check
        err = f"{type(exc).__name__}: {exc}"[:300]
        receipts.mark_failed(db, receipt["id"], {"reason": err, "op": op, "action": action})
        result_acc.errors.append({"key": key_part, "action": action, "op": op, "error": err})
        _log.warning("acceptance filing %s/%s failed for %s: %s", action, op, key_part, err)
        return True, None, err


def _ensure_work_item(db: Any, row: dict[str, Any]) -> str:
    """Guarantee a finding row owns a work_items row."""
    if row.get("work_item_id"):
        return str(row["work_item_id"])
    _ledger._attach_work_item(db, row)  # noqa: SLF001
    if row.get("work_item_id"):
        return str(row["work_item_id"])
    from .. import work_items

    item = work_items.create_item(
        db, project_id=row["project_id"],
        title=f"{_ledger.human_key(row)} [{str(row.get('severity') or 'major').upper()}] {row.get('title')}"[:300],
        description=str(row.get("body") or "")[:8000],
        subject_type="finding", subject_id=row["id"], status="todo", actor="boss",
    )
    db.execute(text("UPDATE findings SET work_item_id = :w WHERE id = :i"),
               {"w": item["id"], "i": row["id"]})
    row["work_item_id"] = item["id"]
    return str(item["id"])


def _lineage_special_work_item(db: Any, project_id: str, lineage: str,
                               subject_type: str, title: str, body: str) -> str:
    """Idempotent BOSS work item per `(project_id, lineage, subject_type)` (minor batch / digest)."""
    existing = db.execute(
        text(
            "SELECT id FROM work_items "
            " WHERE project_id = :p AND subject_type = :st AND subject_id = :sid LIMIT 1"
        ),
        {"p": project_id, "st": subject_type, "sid": lineage},
    ).scalar()
    if existing:
        return str(existing)
    from .. import work_items

    parent = _ledger._lineage_work_item(db, project_id, lineage)  # noqa: SLF001
    item = work_items.create_item(
        db, project_id=project_id, title=title[:300], description=body[:8000],
        parent_id=parent, subject_type=subject_type, subject_id=lineage,
        status="todo", actor="boss",
    )
    return str(item["id"])


def _source_ref(adapter: Any, source_ticket: Optional[Mapping[str, Any]]) -> Any:
    if not source_ticket:
        return None
    from ...connectors.tickets import TicketRef

    key = source_ticket.get("key")
    url = source_ticket.get("url")
    tid = source_ticket.get("id")
    if not (key or url or tid):
        return None
    return TicketRef(adapter.provider_key, key=key, url=url, id=tid)


def _create_external_ticket(
    db: Any,
    *,
    conn: Any,
    adapter: Any,
    provider_row: Mapping[str, Any],
    project_id: str,
    lineage: str,
    work_item_id: str,
    key_part: str,
    title: str,
    body_md: str,
    labels: list[str],
    source_ticket: Optional[Mapping[str, Any]],
    res: FilingResult,
) -> Optional[dict[str, Any]]:
    src_ref = _source_ref(adapter, source_ticket)
    supports_parent = bool(
        src_ref is not None
        and getattr(adapter, "provider_key", "") != "github"
    )
    project_key = str((provider_row.get("configuration") or {}).get("project_key") or "").strip() or None

    def _do_create() -> dict[str, Any]:
        created = adapter.create(
            conn,
            title=title[:240],
            body_markdown=body_md,
            type="bug" if "blocker" in labels else "task",
            labels=labels,
            project=project_key,
            parent=src_ref if supports_parent else None,
        )
        return {
            "id": str(created.external_issue_id),
            "key": str(created.external_key or created.external_issue_id),
            "url": str(created.external_url or ""),
            "parented": supports_parent,
        }

    executed, out, err = _ensure_receipt_and_run(
        db,
        project_id=project_id,
        provider=conn.provider,
        key_part=key_part,
        lineage=lineage,
        action="file_finding",
        op="create",
        digest_input={"work_item_id": work_item_id, "key_part": key_part},
        scope={"project_id": project_id, "work_item_id": work_item_id, "lineage": lineage},
        fn=_do_create,
        result_acc=res,
    )
    if err or not out or not out.get("key"):
        return None
    _save_binding(
        db,
        project_id=project_id,
        work_item_id=work_item_id,
        provider_id=str(provider_row["id"]),
        provider_key=conn.provider,
        external_id=str(out.get("id") or out["key"]),
        external_key=str(out["key"]),
        external_url=str(out.get("url") or ""),
        external_state="todo",
        metadata={"lineage": lineage, "key_part": key_part},
    )
    # Relates-to link when the connector did not attach as child of source_ticket.
    if executed and src_ref is not None and not supports_parent and out.get("url") and adapter.can("link"):
        def _do_link() -> dict[str, Any]:
            linked = adapter.link(conn, src_ref, str(out["url"]), title[:180])
            return {"url": linked.url, "created": linked.created}

        _ensure_receipt_and_run(
            db,
            project_id=project_id,
            provider=conn.provider,
            key_part=str(source_ticket.get("key") or key_part),
            lineage=lineage,
            action="link_finding",
            op="link",
            digest_input={"source": source_ticket.get("key"), "target": out["key"]},
            scope={"project_id": project_id, "work_item_id": work_item_id, "lineage": lineage},
            fn=_do_link,
            result_acc=res,
        )
    return out


def _sync_existing_row(
    db: Any,
    *,
    conn: Any,
    adapter: Any,
    provider_id: str,
    project_id: str,
    lineage: str,
    row: Mapping[str, Any],
    check_id: Optional[str],
    res: FilingResult,
) -> None:
    """Close on fixed, reopen on regression, comment on waived/wont_fix/duplicate."""
    from ...connectors.tickets import TicketRef

    binding = _binding_for_work_item(db, project_id, row.get("work_item_id"), provider_id)
    if not binding:
        return
    ext_key = str(binding.get("external_key") or binding.get("external_issue_id") or "")
    if not ext_key:
        return
    ref = TicketRef(conn.provider, key=ext_key, id=binding.get("external_issue_id"),
                    url=binding.get("external_url"))
    st = str(row.get("status") or "").lower()
    ext_state = str(binding.get("external_state") or "").lower()
    cid = str(check_id or row.get("resolved_check_id") or "latest")
    ev = _evidence_text(row)

    if st == "fixed" and ext_state != "done":
        comment_md = (
            f"Verified fixed by acceptance check `{cid}`"
            + (f": {ev}" if ev else ".")
        )
        if adapter.can("write_comment"):
            _ensure_receipt_and_run(
                db, project_id=project_id, provider=conn.provider, key_part=ext_key,
                lineage=lineage, action="finding_fixed", op="comment",
                digest_input={"finding": row["id"], "check": cid, "status": "fixed"},
                scope={"project_id": project_id, "finding_id": row["id"]},
                fn=lambda: {"at": adapter.comment(conn, ref, comment_md).at},
                result_acc=res,
            )
        if adapter.can("transition"):
            _, out, err = _ensure_receipt_and_run(
                db, project_id=project_id, provider=conn.provider, key_part=ext_key,
                lineage=lineage, action="finding_fixed", op="transition",
                digest_input={"finding": row["id"], "check": cid, "to": "done"},
                scope={"project_id": project_id, "finding_id": row["id"]},
                fn=lambda: {
                    "to": adapter.transition(conn, ref, "done").to_status,
                    "key": ext_key,
                },
                result_acc=res,
            )
            if err is None:
                db.execute(
                    text(
                        "UPDATE work_tracker_bindings SET external_state = 'done', "
                        "       last_synced_at = now(), updated_at = now() WHERE id = :id"
                    ),
                    {"id": binding["id"]},
                )
                res.closed.append({"finding_id": row["id"], "key": ext_key, "check_id": cid})
        return

    if st in _ledger.LIVE and int(row.get("reopened_count") or 0) > 0 and ext_state == "done":
        reopen_n = int(row.get("reopened_count") or 1)
        comment_md = (
            f"Reopened by acceptance check `{cid}` (regression #{reopen_n})"
            + (f": {ev}" if ev else ".")
        )
        if adapter.can("write_comment"):
            _ensure_receipt_and_run(
                db, project_id=project_id, provider=conn.provider, key_part=ext_key,
                lineage=lineage, action="finding_reopened", op="comment",
                digest_input={"finding": row["id"], "reopened": reopen_n},
                scope={"project_id": project_id, "finding_id": row["id"]},
                fn=lambda: {"at": adapter.comment(conn, ref, comment_md).at},
                result_acc=res,
            )
        if adapter.can("transition"):
            _, out, err = _ensure_receipt_and_run(
                db, project_id=project_id, provider=conn.provider, key_part=ext_key,
                lineage=lineage, action="finding_reopened", op="transition",
                digest_input={"finding": row["id"], "reopened": reopen_n, "to": "todo"},
                scope={"project_id": project_id, "finding_id": row["id"]},
                fn=lambda: {
                    "to": adapter.transition(conn, ref, "todo").to_status,
                    "key": ext_key,
                },
                result_acc=res,
            )
            if err is None:
                db.execute(
                    text(
                        "UPDATE work_tracker_bindings SET external_state = 'todo', "
                        "       last_synced_at = now(), updated_at = now() WHERE id = :id"
                    ),
                    {"id": binding["id"]},
                )
                res.reopened.append({"finding_id": row["id"], "key": ext_key, "reopened": reopen_n})
        return

    if st in ("waived", "wont_fix") and adapter.can("write_comment"):
        reason = str(row.get("waive_reason") or "no reason recorded")
        who = str(row.get("waived_by") or "operator")
        comment_md = f"Marked `{st}` by {who}: {reason}"
        executed, _, err = _ensure_receipt_and_run(
            db, project_id=project_id, provider=conn.provider, key_part=ext_key,
            lineage=lineage, action=f"finding_{st}", op="comment",
            digest_input={"finding": row["id"], "status": st, "reason": reason},
            scope={"project_id": project_id, "finding_id": row["id"]},
            fn=lambda: {"at": adapter.comment(conn, ref, comment_md).at},
            result_acc=res,
        )
        if executed and err is None:
            res.waived_commented.append({"finding_id": row["id"], "key": ext_key, "status": st})
        return

    if st == "duplicate" and row.get("duplicate_of") and adapter.can("write_comment"):
        target = _ledger.get(db, str(row["duplicate_of"]))
        target_label = _ledger.human_key(target) if target else str(row["duplicate_of"])
        comment_md = f"Merged into {target_label}."
        executed, _, err = _ensure_receipt_and_run(
            db, project_id=project_id, provider=conn.provider, key_part=ext_key,
            lineage=lineage, action="finding_merged", op="comment",
            digest_input={"finding": row["id"], "duplicate_of": row["duplicate_of"]},
            scope={"project_id": project_id, "finding_id": row["id"]},
            fn=lambda: {"at": adapter.comment(conn, ref, comment_md).at},
            result_acc=res,
        )
        if executed and err is None:
            res.merged_commented.append({"finding_id": row["id"], "key": ext_key,
                                         "merged_into": target_label})


def file_and_sync(
    connection: Any,
    settings: Any,
    *,
    project_id: str,
    lineage: str,
    delta: Optional[_ledger.LedgerDelta] = None,
    check: Optional[Mapping[str, Any]] = None,
    policy: Optional[AcceptancePolicy] = None,
    pr_url: Optional[str] = None,
    branch: Optional[str] = None,
    run_url: Optional[str] = None,
    source_ticket: Optional[Mapping[str, Any]] = None,
    lineage_title: Optional[str] = None,
) -> FilingResult:
    """File new qualifying findings and sync status changes to the project's tracker (LLD §9)."""
    from ...connectors.tickets import get_ticket_adapter
    from .. import ticket_sync
    from ..core import emit_event

    pol = policy if isinstance(policy, AcceptancePolicy) else AcceptancePolicy.from_mapping(policy)
    res = FilingResult()

    if pol.file == "off":
        res.mode = "disabled"
        return res

    # Load all rows for this lineage so filing + fixed/reopened/waived/merged sync all see truth.
    raw_rows = connection.execute(
        text("SELECT * FROM findings WHERE project_id = :p AND lineage_root_run_id = :l ORDER BY number"),
        {"p": project_id, "l": lineage},
    ).mappings().all()
    all_rows = [_ledger._row(r) for r in raw_rows]  # noqa: SLF001
    all_rows = [r for r in all_rows if r is not None]

    # Always guarantee BOSS work items exist for every live finding (even with no external tracker).
    for row in all_rows:
        if row["status"] in _ledger.LIVE:
            _ensure_work_item(connection, row)

    provider_row = _default_provider_row(connection, project_id)
    if provider_row is None or not _auto_file_enabled(provider_row, pol):
        res.mode = "boss_work_items_only"
        return res

    conn = ticket_sync.open_connection(
        connection, project_id, connection_id=str(provider_row["id"]),
        provider=str(provider_row["provider_key"]), app_settings=settings,
    )
    if conn is None:
        res.mode = "boss_work_items_only"
        return res
    ticket_sync._apply_status_map(conn)  # noqa: SLF001
    adapter = get_ticket_adapter(conn.provider)
    if not adapter.can("create"):
        res.mode = "boss_work_items_only"
        return res

    res.provider = conn.provider
    provider_id = str(provider_row["id"])
    check_id = str((check or {}).get("id") or "")

    # 1. Sync existing ticket-bound rows (fixed -> done, reopened -> todo, waived/merged -> comment)
    for row in all_rows:
        _sync_existing_row(
            connection, conn=conn, adapter=adapter, provider_id=provider_id,
            project_id=project_id, lineage=lineage, row=row, check_id=check_id, res=res,
        )

    # 2. Partition live rows needing external tickets
    individual_candidates: list[dict[str, Any]] = []
    minor_candidates: list[dict[str, Any]] = []
    for row in all_rows:
        if row["status"] not in _ledger.LIVE:
            continue
        existing = _binding_for_work_item(connection, project_id, row.get("work_item_id"), provider_id)
        if existing:
            continue
        sev = str(row.get("severity") or "major").lower()
        kind = str(row.get("kind") or "review").lower()
        if sev == "nit":
            res.skipped.append({"finding_id": row["id"], "reason": "nit is ledger-only"})
            continue
        if kind == "acceptance_gap" or sev in ("blocker", "major"):
            individual_candidates.append(row)
        else:
            minor_candidates.append(row)

    # Sort highest severity first (blocker -> major -> minor), gaps before review, oldest first
    individual_candidates.sort(key=lambda r: (
        -_ledger.SEVERITIES.index(_ledger._sev(r["severity"])),  # noqa: SLF001
        0 if r["kind"] == "acceptance_gap" else 1,
        int(r.get("number") or 0),
    ))

    cap = max(1, int(pol.max_new_tickets or MAX_NEW_TICKETS_DEFAULT))
    to_file = individual_candidates[:cap]
    overflow = individual_candidates[cap:]

    for row in to_file:
        sev = str(row.get("severity") or "major").lower()
        title = f"[{sev.upper()}] {row['title']}"
        body_md = ticket_body(
            row, pr_url=pr_url or row.get("pr_url"), branch=branch,
            run_url=run_url, source_ticket=source_ticket,
        )
        created = _create_external_ticket(
            connection, conn=conn, adapter=adapter, provider_row=provider_row,
            project_id=project_id, lineage=lineage, work_item_id=str(row["work_item_id"]),
            key_part=row["fingerprint"], title=title, body_md=body_md,
            labels=["boss-acceptance", sev], source_ticket=source_ticket, res=res,
        )
        if created:
            res.filed.append({
                "finding_id": row["id"], "finding_key": _ledger.human_key(row),
                "key": created["key"], "url": created.get("url"),
                "parented": bool(created.get("parented")),
            })
            try:
                with connection.begin_nested():
                    emit_event(
                        connection, project_id, "finding.filed", "boss", "finding", row["id"],
                        {"finding_key": _ledger.human_key(row), "provider": conn.provider,
                         "ticket_key": created["key"], "ticket_url": created.get("url")},
                    )
            except Exception:  # noqa: BLE001
                pass

    # 3. Overflow -> one digest ticket
    if overflow:
        digest_lines = [
            " · ".join(p for p in (
                f"**PR:** {pr_url}" if pr_url else "",
                f"**Branch:** `{branch}`" if branch else "",
                f"**Run:** {run_url}" if run_url else "",
            ) if p),
            "",
            f"{len(overflow)} additional findings exceeded the per-check ticket cap ({cap}):",
            "",
        ]
        for r in overflow:
            digest_lines.append(
                f"- **{_ledger.human_key(r)} [{str(r['severity']).upper()}]** {r['title']}"
            )
        digest_body = "\n".join(digest_lines).strip()
        digest_title = f"[Acceptance] +{len(overflow)} additional findings for {lineage_title or lineage}"
        digest_wi = _lineage_special_work_item(
            connection, project_id, lineage, DIGEST_SUBJECT_TYPE, digest_title, digest_body,
        )
        created = _create_external_ticket(
            connection, conn=conn, adapter=adapter, provider_row=provider_row,
            project_id=project_id, lineage=lineage, work_item_id=digest_wi,
            key_part=f"digest:{check_id or lineage}", title=digest_title, body_md=digest_body,
            labels=["boss-acceptance", "digest"], source_ticket=source_ticket, res=res,
        )
        if created:
            # Bind each overflow finding to the digest ticket (external_issue_id=None respects
            # uq_work_tracker_external_issue while ensuring idempotency on replay).
            for r in overflow:
                if r.get("work_item_id"):
                    _save_binding(
                        connection,
                        project_id=project_id,
                        work_item_id=str(r["work_item_id"]),
                        provider_id=provider_id,
                        provider_key=conn.provider,
                        external_id=None,
                        external_key=str(created["key"]),
                        external_url=str(created.get("url") or ""),
                        external_state="todo",
                        metadata={"lineage": lineage, "in_digest": True},
                    )
            res.digest_ticket = {
                "key": created["key"], "url": created.get("url"),
                "count": len(overflow), "finding_ids": [r["id"] for r in overflow],
            }

    # 4. MINOR findings -> one batched "Polish: <title>" ticket per lineage
    if minor_candidates:
        polish_title = f"Polish: {lineage_title or lineage}"
        checklist = "\n".join(
            f"- [ ] **{_ledger.human_key(r)}** {r['title']}"
            + (f" (`{r['path']}:{r['line']}`)" if r.get("path") and r.get("line") else "")
            for r in minor_candidates
        )
        polish_body = (
            (f"**PR:** {pr_url}\n\n" if pr_url else "")
            + "Batched minor acceptance & review polish items:\n\n"
            + checklist
        )
        polish_wi = _lineage_special_work_item(
            connection, project_id, lineage, MINOR_BATCH_SUBJECT_TYPE, polish_title, polish_body,
        )
        existing_polish = _binding_for_work_item(connection, project_id, polish_wi, provider_id)
        if existing_polish:
            # Update in place via comment on change
            from ...connectors.tickets import TicketRef

            pkey = str(existing_polish.get("external_key") or existing_polish.get("external_issue_id"))
            pref = TicketRef(conn.provider, key=pkey, url=existing_polish.get("external_url"))
            if adapter.can("write_comment"):
                _ensure_receipt_and_run(
                    connection, project_id=project_id, provider=conn.provider, key_part=pkey,
                    lineage=lineage, action="minor_batch_update", op="comment",
                    digest_input=sorted(r["id"] for r in minor_candidates),
                    scope={"project_id": project_id, "work_item_id": polish_wi},
                    fn=lambda: {"at": adapter.comment(connection and conn or conn, pref, polish_body).at},
                    result_acc=res,
                )
            polish_key = pkey
            polish_url = existing_polish.get("external_url")
        else:
            created = _create_external_ticket(
                connection, conn=conn, adapter=adapter, provider_row=provider_row,
                project_id=project_id, lineage=lineage, work_item_id=polish_wi,
                key_part=f"minor-batch:{lineage}", title=polish_title, body_md=polish_body,
                labels=["boss-acceptance", "minor"], source_ticket=source_ticket, res=res,
            )
            polish_key = created["key"] if created else None
            polish_url = created.get("url") if created else None
        if polish_key:
            for r in minor_candidates:
                res.batched_minors.append({
                    "finding_id": r["id"], "finding_key": _ledger.human_key(r),
                    "batch_ticket_key": polish_key, "batch_ticket_url": polish_url,
                })

    return res


__all__ = [
    "FilingResult",
    "MAX_NEW_TICKETS_DEFAULT",
    "file_and_sync",
    "ticket_body",
]
