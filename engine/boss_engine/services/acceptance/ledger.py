"""THE FINDINGS LEDGER (AA-B, BOSSAPP-1216; boss-acceptance-agent-lld.md §5): one row per real
problem in a build lineage, whoever found it — so nothing a check or a reviewer said is lost.

WHY. Under the fast lane (`lean`) a reviewer's MAJOR and BLOCKER findings were advisory: they
lived only as JSONB on the run (`finalize_ctx.review_comments`, `deferred_findings`), the PR
opened, and the follow-up carried two MINOR findings while fifteen must-haves stayed missing
(Northgate, 2026-11-15). The ledger is the durable record every seat writes to and every
follow-up is built from.

  - EVERY acceptance gap (a contract item not met, a missing declared stack) and EVERY review
    finding of every severity — BLOCKER/MAJOR/MINOR/NIT, advisory or not — is upserted here
    the moment it is seen (`observe_check`, `observe_review`, `observe_findings`).
  - DEDUPE: a gap is keyed by its contract item (`ac:AC-15`), stable across rounds and contract
    versions; a review finding by `rv:<sha1(path, title)>`. A live row seen again counts
    (`seen_count`), it is never a second row. A finding that says "same as F-12" is merged into
    F-12 when F-12 is a live row of the lineage (BOSS checks; an id that is not live is ignored).
  - FIXED only when a later check PROVES it (an item met with verified evidence, a replayed
    probe that passed): `resolved_check_id`. A fixed row seen open again REOPENS
    (`reopened_count`): a regression is the same problem, not a new one.
  - FOLLOW-UPS are built from the open rows plus the frozen contract (`followup_spec`), and hold
    the rows they carry (`in_progress`, `followup_run_id`): a row held by a live follow-up is
    never sent twice; a follow-up that ends without fixing it releases it (`release_dead_holds`).
  - Each row owns a work item (subject_type 'finding') under the lineage's work item, linked
    both ways, so tracking and a tracker binding ride on what already exists.

No model call happens here, and every function runs inside the caller's transaction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .verdict import clip

_log = logging.getLogger(__name__)

SEVERITIES = ("nit", "minor", "major", "blocker")
KINDS = ("acceptance_gap", "review", "security", "consistency", "readiness", "pipeline")
LIVE = ("open", "in_progress")
CLOSED_BY_PERSON = ("waived", "wont_fix")
#: A follow-up holds its rows only while its run is in one of these. Every other status — done,
#: failed, cancelled, BLOCKED (a missing grant, a parked gate), anything new — has ended its
#: hold, so a blocked follow-up never strands the rows it was sent to fix.
LIVE_RUN = ("queued", "running")
#: A hold older than this is released whatever the run says (a run stuck "running" forever).
HOLD_TTL_HOURS = 48

_STAGE_KIND = {"review_code": "review", "security_check": "security",
               "consistency_check": "consistency", "readiness": "readiness",
               "acceptance_step": "review", "acceptance_check": "review"}
_SAME_AS = re.compile(r"(?i)\[?\s*same\s+as\s+F-(\d+)\s*\]?")
_NEW_TAG = re.compile(r"(?i)\s*\[\s*new\s*\]\s*$")
_FREE_TEXT = re.compile(r"^\((?P<stage>[a-z_]+)\)(?:\s+(?P<path>[^\s:]+?[./][^\s:]*?):(?P<line>\d+))?"
                        r"\s+(?P<text>.*)$", re.S)
_GATE_LINE = re.compile(r"^Failing gate '([^']+)'")


@dataclass
class LedgerDelta:
    """What one observation changed: row ids by what happened to them."""
    opened: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)

    def add(self, other: "LedgerDelta") -> "LedgerDelta":
        for k in ("opened", "seen", "reopened", "fixed", "merged"):
            getattr(self, k).extend(x for x in getattr(other, k) if x not in getattr(self, k))
        return self

    def to_dict(self) -> dict[str, list[str]]:
        return {k: list(getattr(self, k)) for k in ("opened", "seen", "reopened", "fixed",
                                                    "merged")}


# ── keys ─────────────────────────────────────────────────────────────────────────────────────

def _fold(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def title_of(text: Any) -> str:
    """A finding's title: its words up to the first em dash (the FINDING line's own split)."""
    words = " ".join(str(text or "").split())
    words = _NEW_TAG.sub("", _SAME_AS.sub("", words)).strip()
    return re.split(r"\s+[—–]\s+|\s+--\s+", words, maxsplit=1)[0].strip() or words


def gap_fingerprint(contract_item_id: str) -> str:
    return f"ac:{contract_item_id}"


def review_fingerprint(path: Optional[str], text: Any) -> str:
    """`rv:<sha1(normalised path or '-', normalised title[:80])>` (LLD §5.2)."""
    p = _fold(path).lstrip("./") or "-"
    t = re.sub(r"[^\w\s]", "", _fold(title_of(text)))[:80].strip()
    return "rv:" + hashlib.sha1(f"{p}|{t}".encode()).hexdigest()[:20]


def human_key(row: Mapping[str, Any]) -> str:
    return f"F-{row.get('number')}"


def _sev(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in ("security", "critical", "high"):
        return "blocker"
    return v if v in SEVERITIES else "major"


def _max_sev(a: str, b: str) -> str:
    return a if SEVERITIES.index(_sev(a)) >= SEVERITIES.index(_sev(b)) else b


# ── rows ─────────────────────────────────────────────────────────────────────────────────────

def _row(r: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    if r is None:
        return None
    out = dict(r)
    for k in ("evidence", "verify_with"):
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                out[k] = []
    out["key"] = human_key(out)
    return out


def get(connection: Any, finding_id: str) -> Optional[dict[str, Any]]:
    from sqlalchemy import text

    return _row(connection.execute(text("SELECT * FROM findings WHERE id = :i"),
                                   {"i": finding_id}).mappings().first())


def by_key(connection: Any, project_id: str, key: str) -> Optional[dict[str, Any]]:
    """A row by its human key (`F-12`) within the project."""
    from sqlalchemy import text

    m = re.fullmatch(r"(?i)F-(\d+)", str(key or "").strip())
    if not m:
        return None
    return _row(connection.execute(text(
        "SELECT * FROM findings WHERE project_id = :p AND number = :n"),
        {"p": project_id, "n": int(m.group(1))}).mappings().first())


def observations(connection: Any, finding_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import text

    return [dict(r) for r in connection.execute(text(
        "SELECT * FROM finding_observations WHERE finding_id = :f ORDER BY id"),
        {"f": finding_id}).mappings().all()]


def _lock(connection: Any, project_id: str) -> None:
    """Serialise ledger writes per project: the number sequence and the live-fingerprint upsert
    are read-then-write, and two seats finishing at once must not both insert."""
    from sqlalchemy import text

    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                       {"k": f"findings:{project_id}"})


def _observe(connection: Any, finding_id: str, *, check_id: Optional[str], run_id: Optional[str],
             stage: str, status_seen: str, evidence: Any) -> bool:
    """Append an observation; False when the same one (run, check, stage, what it said, its
    evidence) is already there — a re-recorded review round is not a second sighting."""
    from sqlalchemy import text

    ev = json.dumps(evidence if isinstance(evidence, list) else [evidence] if evidence else [],
                    default=str, sort_keys=True)
    digest = hashlib.sha1(json.dumps([finding_id, check_id, run_id, stage, status_seen, ev])
                          .encode()).hexdigest()
    if connection.execute(text(
            "SELECT 1 FROM finding_observations WHERE finding_id = :f AND digest = :d"),
            {"f": finding_id, "d": digest}).first():
        return False
    connection.execute(text(
        "INSERT INTO finding_observations (finding_id, check_id, run_id, stage, status_seen, "
        "evidence, digest) VALUES (:f, :c, :r, :s, :st, CAST(:e AS JSONB), :d)"),
        {"f": finding_id, "c": check_id, "r": run_id, "s": stage, "st": status_seen, "e": ev,
         "d": digest})
    return True


def _emit(connection: Any, project_id: str, event: str, row: Mapping[str, Any],
          extra: Optional[Mapping[str, Any]] = None) -> None:
    from ..core import emit_event

    emit_event(connection, project_id, event, "boss", "finding", row["id"],
               {"key": human_key(row), "kind": row.get("kind"), "severity": row.get("severity"),
                "lineage_root_run_id": row.get("lineage_root_run_id"),
                "fingerprint": row.get("fingerprint"), **dict(extra or {})})


_WORK_STATUS = {"open": "todo", "in_progress": "active", "fixed": "done", "waived": "cancelled",
                "wont_fix": "cancelled", "duplicate": "cancelled", "superseded": "cancelled"}


def _sync_work_item(connection: Any, row: Mapping[str, Any]) -> None:
    from sqlalchemy import text

    if not row.get("work_item_id"):
        return
    try:
        with connection.begin_nested():
            connection.execute(text(
                "UPDATE work_items SET status = :s, updated_at = now() WHERE id = :w"),
                {"s": _WORK_STATUS.get(str(row.get("status")), "todo"),
                 "w": row["work_item_id"]})
    except Exception:  # noqa: BLE001 — the ledger row is the record; the item follows it
        _log.warning("finding %s: work item status not synced", row.get("id"))


def _lineage_work_item(connection: Any, project_id: str, lineage: str) -> Optional[str]:
    from sqlalchemy import text

    try:
        with connection.begin_nested():
            return connection.execute(text(
                "SELECT work_item_id FROM code_task_runs WHERE id = :r AND project_id = :p"),
                {"r": lineage, "p": project_id}).scalar()
    except Exception:  # noqa: BLE001 — a lineage without a run row (a web-app root) has none
        return None


def _attach_work_item(connection: Any, row: dict[str, Any]) -> None:
    """The row's own work item (subject_type 'finding', titled with its key) under the
    lineage's work item, linked both ways (`finding` on the lineage's item, `pull_request` on
    the finding's). Never fails the ledger write: the row is the record."""
    from sqlalchemy import text

    from .. import work_items
    from ..ambient import board

    try:
        with connection.begin_nested():
            parent = _lineage_work_item(connection, row["project_id"],
                                        row["lineage_root_run_id"])
            item = work_items.create_item(
                connection, project_id=row["project_id"],
                title=f"{human_key(row)} [{row['severity'].upper()}] {row['title']}"[:300],
                description=str(row.get("body") or "")[:8000], parent_id=parent,
                subject_type="finding", subject_id=row["id"], status="todo", actor="boss")
            connection.execute(text("UPDATE findings SET work_item_id = :w WHERE id = :i"),
                               {"w": item["id"], "i": row["id"]})
            row["work_item_id"] = item["id"]
            if parent:
                board.link(connection, parent, "finding", row["id"], note=human_key(row))
            if row.get("pr_url"):
                board.link(connection, item["id"], "pull_request", row["pr_url"])
    except Exception:  # noqa: BLE001
        _log.warning("finding %s: work item not created", row.get("id"), exc_info=True)


# ── the write path ───────────────────────────────────────────────────────────────────────────

def _live(connection: Any, project_id: str, lineage: str, fingerprint: str
          ) -> Optional[dict[str, Any]]:
    from sqlalchemy import text

    return _row(connection.execute(text(
        "SELECT * FROM findings WHERE project_id = :p AND lineage_root_run_id = :r "
        "AND fingerprint = :f AND status IN ('open', 'in_progress') FOR UPDATE"),
        {"p": project_id, "r": lineage, "f": fingerprint}).mappings().first())


def _closed(connection: Any, project_id: str, lineage: str, fingerprint: str
            ) -> Optional[dict[str, Any]]:
    from sqlalchemy import text

    return _row(connection.execute(text(
        "SELECT * FROM findings WHERE project_id = :p AND lineage_root_run_id = :r "
        "AND fingerprint = :f ORDER BY updated_at DESC, number DESC LIMIT 1 FOR UPDATE"),
        {"p": project_id, "r": lineage, "f": fingerprint}).mappings().first())


def upsert(connection: Any, *, project_id: str, lineage: str, kind: str, severity: str,
           fingerprint: str, title: str, body: str = "", evidence: Any = (),
           verify_with: Any = (), path: Optional[str] = None, line: Optional[int] = None,
           stage: str = "", run_id: Optional[str] = None, check_id: Optional[str] = None,
           contract_id: Optional[str] = None, contract_item_id: Optional[str] = None,
           pr_url: Optional[str] = None) -> tuple[dict[str, Any], str]:
    """Record that a problem was seen open. Returns (row, what happened): 'opened' (a new row),
    'seen' (a live row seen again: seen_count+1, the higher severity kept), 'reopened' (a row
    proved fixed is open again: a regression), or 'kept' (a person waived it, or the same
    observation was already recorded: nothing changes but the observation)."""
    from sqlalchemy import text

    from ..core import make_id

    kind = kind if kind in KINDS else "review"
    severity = _sev(severity)
    _lock(connection, project_id)
    evidence = list(evidence or [])
    row = _live(connection, project_id, lineage, fingerprint)
    if row is not None:
        if not _observe(connection, row["id"], check_id=check_id, run_id=run_id, stage=stage,
                        status_seen="open", evidence=evidence):
            return row, "kept"
        connection.execute(text(
            "UPDATE findings SET seen_count = seen_count + 1, last_seen_at = now(), "
            "severity = :s, evidence = CASE WHEN CAST(:e AS JSONB) = '[]'::jsonb THEN evidence "
            "ELSE CAST(:e AS JSONB) END, verify_with = CASE WHEN CAST(:v AS JSONB) = '[]'::jsonb "
            "THEN verify_with ELSE CAST(:v AS JSONB) END, pr_url = COALESCE(:u, pr_url), "
            "updated_at = now() WHERE id = :i"),
            {"s": _max_sev(row["severity"], severity), "e": json.dumps(evidence, default=str),
             "v": json.dumps(list(verify_with or []), default=str), "u": pr_url, "i": row["id"]})
        return get(connection, row["id"]) or row, "seen"
    old = _closed(connection, project_id, lineage, fingerprint)
    if old is not None and old["status"] in CLOSED_BY_PERSON:
        _observe(connection, old["id"], check_id=check_id, run_id=run_id, stage=stage,
                 status_seen="open", evidence=evidence)
        return old, "kept"
    if old is not None and old["status"] == "duplicate" and old.get("duplicate_of"):
        target = get(connection, old["duplicate_of"])
        if target is not None and target["status"] in LIVE:
            _observe(connection, target["id"], check_id=check_id, run_id=run_id, stage=stage,
                     status_seen="open", evidence=evidence)
            return target, "kept"
    if old is not None and old["status"] == "fixed":
        # A REGRESSION: the same problem, open again after a check proved it fixed.
        _observe(connection, old["id"], check_id=check_id, run_id=run_id, stage=stage,
                 status_seen="open", evidence=evidence)
        connection.execute(text(
            "UPDATE findings SET status = 'open', reopened_count = reopened_count + 1, "
            "resolved_check_id = NULL, followup_run_id = NULL, seen_count = seen_count + 1, "
            "last_seen_at = now(), severity = :s, evidence = CAST(:e AS JSONB), "
            "pr_url = COALESCE(:u, pr_url), updated_at = now() WHERE id = :i"),
            {"s": _max_sev(old["severity"], severity), "e": json.dumps(evidence, default=str),
             "u": pr_url, "i": old["id"]})
        row = get(connection, old["id"]) or old
        _sync_work_item(connection, row)
        _emit(connection, project_id, "finding.reopened", row, {"check_id": check_id})
        return row, "reopened"
    number = int(connection.execute(text(
        "SELECT COALESCE(MAX(number), 0) + 1 FROM findings WHERE project_id = :p"),
        {"p": project_id}).scalar())
    fid = make_id("fnd")
    connection.execute(text(
        "INSERT INTO findings (id, project_id, number, lineage_root_run_id, contract_id, "
        "contract_item_id, kind, severity, fingerprint, title, body, evidence, verify_with, path, "
        "line, first_seen_check_id, first_seen_run_id, pr_url) VALUES (:i, :p, :n, :r, :c, :ci, "
        ":k, :s, :f, :t, :b, CAST(:e AS JSONB), CAST(:v AS JSONB), :path, :line, :chk, :run, :u)"),
        {"i": fid, "p": project_id, "n": number, "r": lineage, "c": contract_id,
         "ci": contract_item_id, "k": kind, "s": severity, "f": fingerprint,
         "t": str(title or "")[:300] or fingerprint, "b": str(body or "")[:8000],
         "e": json.dumps(evidence, default=str),
         "v": json.dumps(list(verify_with or []), default=str), "path": path,
         "line": int(line) if line else None, "chk": check_id, "run": run_id, "u": pr_url})
    _observe(connection, fid, check_id=check_id, run_id=run_id, stage=stage,
             status_seen="open", evidence=evidence)
    row = get(connection, fid)
    assert row is not None
    _attach_work_item(connection, row)
    _emit(connection, project_id, "finding.opened", row, {"check_id": check_id, "stage": stage})
    return row, "opened"


def mark_fixed(connection: Any, finding_id: str, *, check_id: Optional[str],
               run_id: Optional[str] = None, stage: str = "", evidence: Any = ()
               ) -> Optional[dict[str, Any]]:
    """A check PROVED the row fixed: status fixed, `resolved_check_id`. Only for a live row; the
    proof is the caller's (an item met with verified evidence, a replayed probe that passed)."""
    from sqlalchemy import text

    row = get(connection, finding_id)
    if row is None or row["status"] not in LIVE:
        return None
    _observe(connection, finding_id, check_id=check_id, run_id=run_id, stage=stage,
             status_seen="fixed", evidence=list(evidence or []))
    connection.execute(text(
        "UPDATE findings SET status = 'fixed', resolved_check_id = :c, last_seen_at = now(), "
        "updated_at = now() WHERE id = :i"), {"c": check_id, "i": finding_id})
    row = get(connection, finding_id) or row
    _sync_work_item(connection, row)
    _emit(connection, row["project_id"], "finding.fixed", row, {"check_id": check_id})
    return row


def transition(connection: Any, finding_id: str, to: str, *, actor: str,
               reason: Optional[str] = None, check_id: Optional[str] = None
               ) -> dict[str, Any]:
    """Move a row by hand: waived / wont_fix need a person and a reason; open re-opens one."""
    from sqlalchemy import text

    row = get(connection, finding_id)
    if row is None:
        raise LookupError(f"no finding {finding_id}")
    if to == "fixed":
        got = mark_fixed(connection, finding_id, check_id=check_id, stage="transition")
        if got is None:
            raise ValueError(f"{human_key(row)} is {row['status']}, not open")
        return got
    if to in CLOSED_BY_PERSON:
        if not str(reason or "").strip():
            raise ValueError("a waiver needs a reason")
        if not str(actor or "").startswith(("usr_", "person:")):
            raise PermissionError("only a person waives a finding")
    if to not in ("open", "in_progress", *CLOSED_BY_PERSON):
        raise ValueError(f"cannot move a finding to {to}")
    connection.execute(text(
        "UPDATE findings SET status = :s, waived_by = CASE WHEN :s IN ('waived', 'wont_fix') "
        "THEN :a ELSE waived_by END, waive_reason = CASE WHEN :s IN ('waived', 'wont_fix') "
        "THEN :why ELSE waive_reason END, updated_at = now() WHERE id = :i"),
        {"s": to, "a": actor, "why": reason, "i": finding_id})
    row = get(connection, finding_id) or row
    _sync_work_item(connection, row)
    if to in CLOSED_BY_PERSON:
        _emit(connection, row["project_id"], "finding.waived", row, {"reason": reason,
                                                                    "by": actor})
    return row


ALSO = "ALSO REPORTED"
_STOP = frozenset("""the and for with that this from into when then than not none have has had
does done only also must should would could item items file files code line lines missing
partial found there their they them what which while where""".split())


def _words(text: Any) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", _fold(text)) if len(w) >= 4 and w not in _STOP}


def close_enough(path: Optional[str], text: Any, target: Mapping[str, Any]) -> bool:
    """Whether a finding the agent declared "same as F-n" plausibly IS F-n: the same file, or a
    significant word in common (stems by a 4-letter prefix: "Makefile" and "make"). A model
    cannot bury a finding in an unrelated row by naming it."""
    p = _fold(path).lstrip("./")
    if p and p == _fold(target.get("path")).lstrip("./"):
        return True
    mine = _words(title_of(text))
    theirs = _words(f"{target.get('title') or ''} {target.get('body') or ''} "
                    f"{target.get('path') or ''}")
    return any(a[:4] == b[:4] for a in mine for b in theirs)


def _carry(connection: Any, target_id: str, *, stage: str, severity: str, text: Any,
           path: Optional[str] = None, line: Optional[int] = None) -> None:
    """Keep a merged finding's own words on the row it merged into (the follow-up line and the
    ticket read them), once."""
    from sqlalchemy import text as sql

    where = f" {path}:{line}" if path and line else f" {path}" if path else ""
    note = f"{ALSO} ({stage}, {_sev(severity).upper()}){where}: {' '.join(str(text).split())}"
    connection.execute(sql(
        "UPDATE findings SET body = body || CASE WHEN position(:n in body) > 0 THEN '' "
        "ELSE E'\n' || :n END, updated_at = now() WHERE id = :t"),
        {"n": note[:1500], "t": target_id})


def merge_into(connection: Any, finding_id: str, target_id: str, *, check_id: Optional[str] = None,
               run_id: Optional[str] = None, stage: str = "") -> Optional[dict[str, Any]]:
    """Merge a live row into another live row of the same lineage (`duplicate`, duplicate_of):
    only the target keeps a ticket."""
    from sqlalchemy import text

    row, target = get(connection, finding_id), get(connection, target_id)
    if (row is None or target is None or row["id"] == target["id"]
            or row["status"] not in LIVE or target["status"] not in LIVE
            or row["lineage_root_run_id"] != target["lineage_root_run_id"]):
        return None
    connection.execute(text(
        "UPDATE findings SET status = 'duplicate', duplicate_of = :t, updated_at = now() "
        "WHERE id = :i"), {"t": target["id"], "i": row["id"]})
    connection.execute(text(
        "UPDATE findings SET seen_count = seen_count + :n, severity = :s, last_seen_at = now(), "
        "updated_at = now() WHERE id = :t"),
        {"n": int(row.get("seen_count") or 1), "s": _max_sev(target["severity"], row["severity"]),
         "t": target["id"]})
    _observe(connection, target["id"], check_id=check_id, run_id=run_id, stage=stage,
             status_seen="open", evidence=[{"kind": "merged", "ref": human_key(row)}])
    _carry(connection, target["id"], stage=stage or "merged", severity=row["severity"],
           text=row.get("body") or row.get("title"), path=row.get("path"), line=row.get("line"))
    row = get(connection, finding_id) or row
    _sync_work_item(connection, row)
    _emit(connection, row["project_id"], "finding.merged", row, {"into": human_key(target)})
    return get(connection, target["id"])


# ── lineage ──────────────────────────────────────────────────────────────────────────────────

def lineage_of(connection: Any, run_id: str) -> Optional[dict[str, Any]]:
    """{project_id, lineage, pr_url} of a code-task run: its lineage root (a follow-up's or a
    repair child's root_run_id, else the run itself) — the key the contract uses too."""
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT project_id, COALESCE(NULLIF(finalize_ctx->>'root_run_id', ''), "
        "NULLIF(finalize_ctx->>'deferred_from_run', ''), id) AS lineage, pull_request_url "
        "FROM code_task_runs WHERE id = :i"), {"i": run_id}).mappings().first()
    if row is None:
        return None
    return {"project_id": row["project_id"], "lineage": row["lineage"],
            "pr_url": row["pull_request_url"]}


def _contract_ids(contract: Optional[Mapping[str, Any]]) -> dict[str, str]:
    from . import contract as _contract

    return {_contract._key(i.get("text")): str(i["id"])  # noqa: SLF001
            for i in (contract or {}).get("items") or [] if i.get("id")}


def _item_id(item: Mapping[str, Any], ids: Mapping[str, str]) -> Optional[str]:
    from . import contract as _contract

    own = str(item.get("id") or "")
    if own.startswith("AC-"):
        return own
    return ids.get(_contract._key(item.get("text")))  # noqa: SLF001


def _gap_key(item: Mapping[str, Any], item_id: Optional[str]) -> str:
    if item_id:
        return gap_fingerprint(item_id)
    from .contract import item_key

    return "ac:t:" + hashlib.sha1(item_key(str(item.get("text") or "")).encode()).hexdigest()[:16]


_GAP_WORD = {"missing": "missing", "partial": "only partial", "human": "needs a person",
             "unjudged": "not proven (evidence not verified)"}


def _gap_of(item: Mapping[str, Any]) -> Optional[str]:
    """The severity of the gap an item is, or None when it is not one (met, or simply not
    answered — a check that did not run opens nothing and fixes nothing)."""
    status = str(item.get("status") or "")
    if status == "missing":
        return "major"
    if status == "partial":
        return "minor" if item.get("judgement") else "major"
    if status == "unjudged" and item.get("unverified"):
        return "major"
    if status == "human":
        return "minor"
    return None


def observe_check(connection: Any, *, project_id: str, lineage: str, check: Mapping[str, Any],
                  record: Mapping[str, Any], run_id: Optional[str] = None,
                  stage: str = "acceptance_check", pr_url: Optional[str] = None,
                  contract: Optional[Mapping[str, Any]] = None,
                  findings_text: Optional[str] = None,
                  finding_results: Iterable[Mapping[str, Any]] = (),
                  probes: Iterable[Mapping[str, Any]] = ()) -> LedgerDelta:
    """One acceptance check into the ledger (LLD §5.1):
      - every item NOT MET (missing, partial — advisory taste items too, as minor — a MET whose
        evidence did not verify, a HUMAN item) → an `acceptance_gap` row, `ac:<item id>`;
      - every item MET with verified evidence → its live gap row FIXED (resolved_check_id);
      - a missing declared stack → a blocker gap `ac:stack:<name>`; present again → fixed;
      - `findings_text`'s FINDING lines (the Acceptance Agent's findings beyond the contract)
        → review rows, "same as F-n" merged into F-n when F-n is live here;
      - `finding_results` ({finding: id or F-n, result: pass, command_ids}) — a replayed
        verify_with probe that passed → that row FIXED."""
    delta = LedgerDelta()
    check_id = check.get("id")
    held_to = (record.get("source") or {}).get("contract") or {}
    if contract is None and held_to.get("id"):
        from . import contract as _contract

        contract = _contract.get(connection, held_to["id"])
    ids = _contract_ids(contract)
    contract_id = (contract or {}).get("id")
    probes_by_item: dict[str, list] = {}
    for p in probes or []:
        probes_by_item.setdefault(str(p.get("item")), []).append(dict(p))
    for item in record.get("items") or []:
        item_id = _item_id(item, ids)
        fp = _gap_key(item, item_id)
        status = str(item.get("status") or "")
        refs = list(item.get("evidence_refs") or []) or (
            [{"kind": "answer", "text": clip(str(item.get("evidence") or ""), 600)}]
            if item.get("evidence") else [])
        if status == "met":
            live = _live(connection, project_id, lineage, fp)
            if live is not None and mark_fixed(connection, live["id"], check_id=check_id,
                                               run_id=run_id, stage=stage, evidence=refs):
                delta.fixed.append(live["id"])
            continue
        severity = _gap_of(item)
        if severity is None:
            continue
        label = item_id or f"Must-have {item.get('n')}"
        text_words = " ".join(str(item.get("text") or "").split())
        row, what = upsert(
            connection, project_id=project_id, lineage=lineage, kind="acceptance_gap",
            severity=severity, fingerprint=fp,
            title=f"{label} {_GAP_WORD.get(status, status)}: {text_words}"[:300],
            body=(f"THE REQUIREMENT (word for word): {text_words}\n\n"
                  f"WHAT IS WRONG: {item.get('evidence') or status}")[:8000],
            evidence=refs, verify_with=probes_by_item.get(str(item_id or item.get("n")), []),
            stage=stage, run_id=run_id, check_id=check_id, contract_id=contract_id,
            contract_item_id=item_id, pr_url=pr_url)
        _count(delta, row, what)
    stack = record.get("stack") or {}
    for r in stack.get("required") or []:
        fp = f"ac:stack:{r.get('stack')}"
        if r.get("stack") in (stack.get("missing") or []):
            row, what = upsert(
                connection, project_id=project_id, lineage=lineage, kind="acceptance_gap",
                severity="blocker", fingerprint=fp,
                title=f"Required stack '{r.get('stack')}' has no manifest in the tree",
                body=(f"Expected {' or '.join(r.get('manifests') or [])}; declared by "
                      f"{r.get('because', 'the spec')}."),
                stage=stage, run_id=run_id, check_id=check_id, contract_id=contract_id,
                pr_url=pr_url)
            _count(delta, row, what)
        elif (stack.get("found") or {}).get(r.get("stack")):
            live = _live(connection, project_id, lineage, fp)
            if live is not None and mark_fixed(
                    connection, live["id"], check_id=check_id, run_id=run_id, stage=stage,
                    evidence=[{"kind": "file", "ref": stack["found"][r["stack"]],
                               "verified": True}]):
                delta.fixed.append(live["id"])
    if findings_text:
        from ..review_comments import parse_findings

        delta.add(observe_review(connection, project_id=project_id, lineage=lineage,
                                 run_id=run_id, comments=parse_findings(findings_text,
                                                                       stage=stage),
                                 check_id=check_id, pr_url=pr_url, stage=stage))
    for res in finding_results or []:
        if str(res.get("result")) != "pass":
            continue
        ref = str(res.get("finding") or "")
        row = by_key(connection, project_id, ref) if ref.upper().startswith("F-") else get(
            connection, ref)
        if row is None or row["lineage_root_run_id"] != lineage:
            continue
        if mark_fixed(connection, row["id"], check_id=check_id, run_id=run_id, stage=stage,
                      evidence=[{"kind": "replay", "ref": c, "verified": True}
                                for c in res.get("command_ids") or []]):
            delta.fixed.append(row["id"])
    return delta


def _count(delta: LedgerDelta, row: Mapping[str, Any], what: str) -> None:
    bucket = {"opened": delta.opened, "seen": delta.seen, "reopened": delta.reopened,
              "kept": delta.seen}[what]
    if row["id"] not in bucket:
        bucket.append(row["id"])


def observe_review(connection: Any, *, project_id: str, lineage: str, run_id: Optional[str],
                   comments: Iterable[Any], check_id: Optional[str] = None,
                   pr_url: Optional[str] = None, stage: Optional[str] = None,
                   seats: Iterable[str] = (), reviewed_paths: Iterable[str] = (),
                   blind_seats: Iterable[str] = ()) -> LedgerDelta:
    """Reviewer findings (review_comments.ReviewComment, any severity, advisory or not) into
    the ledger: `rv:` fingerprint dedupe; "same as F-n" merges into F-n only when F-n is a live
    row of this lineage AND plausibly the same problem (close_enough: the same file, or a word
    in common) AND it would not bury a finding two severities above F-n — otherwise the finding
    stands on its own. A merge keeps the finding's words on F-n (`ALSO REPORTED`).

    `seats` / `reviewed_paths` (L4): the seats that reviewed this round and the files it
    changed. A live review row a seat raised earlier, on one of those files, that the same seat
    did not report again this round is FIXED BY THAT RE-REVIEW (`fixed_by_review`), so a
    follow-up stops resending what a repair round already fixed.

    `blind_seats` (BOSSAPP-1360): seats recorded not_run / hollow / tools_blocked. What a blind
    seat said, and any finding that only describes BOSS's own tooling failure, is never a row."""
    from .. import review_tools

    delta = LedgerDelta()
    reported: set[str] = set()
    blind = set(blind_seats or ())
    comments = review_tools.drop_tooling_failures(comments, {s: True for s in blind})
    for c in comments or []:
        get_ = (c.get if isinstance(c, Mapping) else (lambda k, d=None, _c=c: getattr(_c, k, d)))
        body = str(get_("body") or get_("text") or "").strip()
        if not body:
            continue
        seat = str(get_("stage") or stage or "review_code")
        path, line = get_("path"), get_("line")
        fp = review_fingerprint(path, body)
        same = _SAME_AS.search(body)
        target = by_key(connection, project_id, f"F-{same.group(1)}") if same else None
        if target is not None and (
                target["status"] not in LIVE or target["lineage_root_run_id"] != lineage
                or not close_enough(path, body, target)
                or SEVERITIES.index(_sev(get_("severity")))
                - SEVERITIES.index(_sev(target["severity"])) >= 2):
            target = None
        ev = [{"kind": "file", "ref": f"{path}:{line}" if line else path}] if path else []
        if target is not None:
            own = _live(connection, project_id, lineage, fp)
            if own is not None and own["id"] != target["id"]:
                merge_into(connection, own["id"], target["id"], check_id=check_id,
                           run_id=run_id, stage=seat)
                delta.merged.append(own["id"])
            elif _observe(connection, target["id"], check_id=check_id, run_id=run_id,
                          stage=seat, status_seen="open", evidence=ev + [{"kind": "said",
                                                                         "text": body[:300]}]):
                from sqlalchemy import text

                _carry(connection, target["id"], stage=seat, severity=str(get_("severity")),
                       text=_SAME_AS.sub("", body).strip(), path=path, line=line)
                connection.execute(text(
                    "UPDATE findings SET seen_count = seen_count + 1, last_seen_at = now(), "
                    "severity = :s, updated_at = now() WHERE id = :i"),
                    {"s": _max_sev(target["severity"], _sev(get_("severity"))),
                     "i": target["id"]})
            if target["id"] not in delta.seen:
                delta.seen.append(target["id"])
            reported.add(fp)
            reported.add(target["fingerprint"])
            continue
        title = title_of(body)
        row, what = upsert(
            connection, project_id=project_id, lineage=lineage,
            kind=_STAGE_KIND.get(seat, "pipeline"), severity=_sev(get_("severity")),
            fingerprint=fp, title=title[:300], body=body, evidence=ev, path=path,
            line=line, stage=seat, run_id=run_id, check_id=check_id, pr_url=pr_url)
        _count(delta, row, what)
        reported.add(fp)
    delta.add(fixed_by_review(connection, project_id=project_id, lineage=lineage, run_id=run_id,
                              seats=seats, reviewed_paths=reviewed_paths, reported=reported,
                              check_id=check_id))
    return delta


def fixed_by_review(connection: Any, *, project_id: str, lineage: str, run_id: Optional[str],
                    seats: Iterable[str], reviewed_paths: Iterable[str],
                    reported: Iterable[str], check_id: Optional[str] = None) -> LedgerDelta:
    """Close the live review rows a re-review no longer reports (L4): raised by one of `seats`,
    anchored to a file in `reviewed_paths` (what this round's change touched, so the seat read
    it), and not among the fingerprints the seat `reported` now. A seat that did not review, or
    a row with no file, is never closed this way — and neither is a SECURITY finding or a
    BLOCKER: silence is not proof for those; a check must show them fixed."""
    from sqlalchemy import text

    delta = LedgerDelta()
    seats = [s for s in seats or [] if s]
    paths = {_fold(p).lstrip("./") for p in reviewed_paths or [] if p}
    if not seats or not paths:
        return delta
    rows = connection.execute(text(
        "SELECT f.id, f.path, f.fingerprint FROM findings f WHERE f.project_id = :p "
        "AND f.lineage_root_run_id = :r AND f.status IN ('open', 'in_progress') "
        "AND f.kind IN ('review', 'consistency') AND f.severity <> 'blocker' "
        "AND f.path IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM finding_observations o WHERE o.finding_id = f.id "
        "            AND o.stage IN :seats AND o.status_seen = 'open')"
        ).bindparams(_expanding("seats")),
        {"p": project_id, "r": lineage, "seats": list(seats)}).mappings().all()
    done = set(reported or ())
    for r in rows:
        if r["fingerprint"] in done or _fold(r["path"]).lstrip("./") not in paths:
            continue
        if mark_fixed(connection, r["id"], check_id=check_id, run_id=run_id,
                      stage="re-review", evidence=[{"kind": "re-review", "ref": run_id,
                                                    "seats": list(seats),
                                                    "path": r["path"]}]):
            delta.fixed.append(r["id"])
    return delta


def observe_findings(connection: Any, *, project_id: str, lineage: str, run_id: Optional[str],
                     findings: Iterable[Mapping[str, Any]], stage: str = "followup",
                     pr_url: Optional[str] = None) -> tuple[LedgerDelta, list[str]]:
    """The free-text findings a follow-up was about to carry ({severity, text}: a failing
    gate, a seat's finding, a deferred one) into the ledger, so the follow-up is built from
    rows. Returns (delta, the row ids they are). An acceptance seat's line is the SAME row as
    the gap its check recorded (`ac:` fingerprint) when that row is live; when it is not (the
    check's own write failed, or the item was not judged) it is recorded here — a follow-up is
    never lost because the check's bookkeeping was."""
    delta = LedgerDelta()
    ids: list[str] = []
    contract_ids: Optional[dict[str, str]] = None
    for f in findings or []:
        words = " ".join(str(f.get("text") or "").split())
        if not words:
            continue
        m = _FREE_TEXT.match(words)
        seat = str(f.get("stage") or (m.group("stage") if m else "") or stage)
        path = f.get("path") or (m.group("path") if m else None)
        line = f.get("line") or (int(m.group("line")) if m and m.group("line") else None)
        body = re.sub(r"^[\s—–:\-]+", "", m.group("text") if m else words) or words
        if seat == "acceptance_check":
            if contract_ids is None:
                from . import contract as _contract

                contract_ids = _contract_ids(_contract.latest(
                    connection, root_run_id=lineage, project_id=project_id))
            got = _acceptance_line(connection, project_id=project_id, lineage=lineage,
                                   body=body, severity=str(f.get("severity") or "major"),
                                   ids=contract_ids, run_id=run_id, pr_url=pr_url)
            if got is not None:
                row, what = got
                if what != "live":
                    _count(delta, row, what)
                ids.append(row["id"])
            continue
        gate = _GATE_LINE.match(body)
        sev = str(f.get("severity") or "major")
        kind = ("security" if sev == "security" else "pipeline" if gate
                else _STAGE_KIND.get(seat, "review"))
        fp = f"gate:{gate.group(1)}" if gate else review_fingerprint(path, body)
        live = _live(connection, project_id, lineage, fp)
        if live is not None and _seen_in_run(connection, live["id"], run_id):
            # The seat that found it already recorded it for this run: handing it to the
            # follow-up is not a second sighting.
            ids.append(live["id"])
            continue
        row, what = upsert(connection, project_id=project_id, lineage=lineage, kind=kind,
                           severity=sev, fingerprint=fp, title=title_of(body)[:300], body=body,
                           evidence=[], path=path, line=line, stage=seat, run_id=run_id,
                           pr_url=pr_url)
        _count(delta, row, what)
        ids.append(row["id"])
    return delta, list(dict.fromkeys(ids))


_MUST_HAVE_LINE = re.compile(r"^Must-have (\d+) (MISSING|only PARTIAL|not judged|needs a person)"
                             r":\s*(.*)$", re.S)
_STACK_LINE = re.compile(r"^Required stack '([^']+)'")


def _acceptance_line(connection: Any, *, project_id: str, lineage: str, body: str,
                     severity: str, ids: Mapping[str, str], run_id: Optional[str],
                     pr_url: Optional[str]) -> Optional[tuple[dict[str, Any], str]]:
    """The ledger row of one acceptance seat line (acceptance_gate.findings_summary's words):
    (row, 'live') when its gap row is already live, else (row, what upsert did)."""
    from . import contract as _contract

    m = _MUST_HAVE_LINE.match(body)
    stack = _STACK_LINE.match(body)
    if m:
        words = m.group(3)
        said = _contract._key(words)  # noqa: SLF001
        hits = sorted(((len(k), iid) for k, iid in ids.items() if k and said.startswith(k)),
                      reverse=True)
        item_id = hits[0][1] if hits else None
        head = re.split(r"\s+[—–]\s+", words, maxsplit=1)[0]
        fp = gap_fingerprint(item_id) if item_id else _gap_key({"text": head}, None)
        title = f"{item_id or 'Must-have ' + m.group(1)} {m.group(2).lower()}: {head}"
        kind, contract_item_id = "acceptance_gap", item_id
    elif stack:
        fp, title = f"ac:stack:{stack.group(1)}", body
        kind, contract_item_id = "acceptance_gap", None
    else:
        fp, title = "gate:acceptance_check", title_of(body)
        kind, contract_item_id = "pipeline", None
    live = _live(connection, project_id, lineage, fp)
    if live is not None:
        return live, "live"
    return upsert(connection, project_id=project_id, lineage=lineage, kind=kind,
                  severity=severity, fingerprint=fp, title=title[:300], body=body,
                  stage="acceptance_check", run_id=run_id, contract_item_id=contract_item_id,
                  pr_url=pr_url)


def _seen_in_run(connection: Any, finding_id: str, run_id: Optional[str]) -> bool:
    from sqlalchemy import text

    if not run_id:
        return False
    return connection.execute(text(
        "SELECT 1 FROM finding_observations WHERE finding_id = :f AND run_id = :r LIMIT 1"),
        {"f": finding_id, "r": run_id}).first() is not None


# ── reading, holds and follow-ups ────────────────────────────────────────────────────────────

def release_dead_holds(connection: Any, *, project_id: str, lineage: str) -> list[str]:
    """Rows held (`in_progress`) by a follow-up run that has ENDED go back to open: the run
    that carried them did not prove them fixed (LLD §5.3: back to open if that run fails)."""
    from sqlalchemy import text

    rows = connection.execute(text(
        "UPDATE findings f SET status = 'open', updated_at = now() "
        "WHERE f.project_id = :p AND f.lineage_root_run_id = :r AND f.status = 'in_progress' "
        "AND (f.followup_run_id IS NULL "
        "  OR f.held_at IS NULL OR f.held_at < now() - make_interval(hours => :ttl) "
        "  OR NOT EXISTS (SELECT 1 FROM code_task_runs c "
        "     WHERE c.id = f.followup_run_id AND c.status IN :live)) RETURNING f.id"
        ).bindparams(_expanding("live")),
        {"p": project_id, "r": lineage, "live": list(LIVE_RUN),
         "ttl": HOLD_TTL_HOURS}).scalars().all()
    for fid in rows:
        row = get(connection, fid)
        if row:
            _sync_work_item(connection, row)
    return list(rows)


def _expanding(name: str) -> Any:
    from sqlalchemy import bindparam

    return bindparam(name, expanding=True)


def open_for(connection: Any, *, project_id: str, lineage: Optional[str] = None,
             pr_url: Optional[str] = None, min_severity: str = "nit",
             include_held: bool = True) -> list[dict[str, Any]]:
    """The live rows of a lineage (or a pull request), most severe first, then oldest."""
    from sqlalchemy import text

    floor = SEVERITIES.index(_sev(min_severity))
    statuses = list(LIVE) if include_held else ["open"]
    rows = connection.execute(text(
        "SELECT * FROM findings WHERE project_id = :p "
        "AND (CAST(:r AS TEXT) IS NULL OR lineage_root_run_id = :r) "
        "AND (CAST(:u AS TEXT) IS NULL OR pr_url = :u) AND status IN :st "
        "ORDER BY number").bindparams(_expanding("st")),
        {"p": project_id, "r": lineage, "u": pr_url, "st": statuses}).mappings().all()
    out = [r for r in (_row(x) for x in rows)
           if r and SEVERITIES.index(_sev(r["severity"])) >= floor]
    # Security first (the checks-failing follow-up's order), then most severe, gaps before
    # reviews, oldest first.
    out.sort(key=lambda r: (0 if r["kind"] == "security" else 1,
                            -SEVERITIES.index(_sev(r["severity"])),
                            0 if r["kind"] == "acceptance_gap" else 1, r["number"]))
    return out


def held_by_live_followup(connection: Any, ids: Iterable[str]) -> list[str]:
    """Which of these rows a LIVE follow-up already carries."""
    from sqlalchemy import text

    ids = [i for i in ids or [] if i]
    if not ids:
        return []
    return list(connection.execute(text(
        "SELECT f.id FROM findings f JOIN code_task_runs c ON c.id = f.followup_run_id "
        "WHERE f.id IN :ids AND f.status = 'in_progress' AND c.status IN :live "
        "AND f.held_at >= now() - make_interval(hours => :ttl)"
        ).bindparams(_expanding("ids"), _expanding("live")),
        {"ids": ids, "live": list(LIVE_RUN), "ttl": HOLD_TTL_HOURS}).scalars().all())


def hold(connection: Any, ids: Iterable[str], *, followup_run_id: str) -> int:
    """A follow-up started with these rows: in_progress, held by that run."""
    from sqlalchemy import text

    ids = [i for i in ids or [] if i]
    if not ids:
        return 0
    got = connection.execute(text(
        "UPDATE findings SET status = 'in_progress', followup_run_id = :r, held_at = now(), "
        "updated_at = now() "
        "WHERE id IN :ids AND status IN ('open', 'in_progress') RETURNING id"
        ).bindparams(_expanding("ids")), {"r": followup_run_id, "ids": ids}).scalars().all()
    for fid in got:
        row = get(connection, fid)
        if row:
            _sync_work_item(connection, row)
    return len(got)


#: A check that did not judge: what it saw is no finding (BOSSAPP-1355).
BLIND_VERDICTS = ("not_run", "no_items")


def stale_rows(connection: Any, rows: Iterable[Mapping[str, Any]], *, project_id: str,
               lineage: str) -> dict[str, str]:
    """Which open rows a follow-up must NOT carry, and why (BOSSAPP-1355): {row id: reason}.
      - a row only checks that did not run (`not_run`/`no_items`) ever observed: nothing judged it;
      - a pipeline gate line (`gate:acceptance_met`) whose run's check did not run, or that a
        LATER judged check of the lineage superseded — one that recorded `ac:` rows of its own
        (a check that judged everything missing is a judgement, never dropped by its counts);
      - a second row with the same fingerprint (the newest wins)."""
    from sqlalchemy import text

    rows = list(rows or [])
    out: dict[str, str] = {}
    seen: dict[str, Mapping[str, Any]] = {}
    from .. import review_tools

    for r in rows:
        fp = str(r.get("fingerprint") or "")
        # A finding about BOSS's own tooling failing (a blind seat's "every command failed",
        # `bwrap: ...`) is no finding about the app: never carried to a builder (BOSSAPP-1360).
        if r.get("kind") != "acceptance_gap" and review_tools.is_tooling_failure(
                f"{r.get('title') or ''} {r.get('body') or ''}", r.get("path")):
            out[r["id"]] = "describes BOSS's own tooling failure (a blind review seat)"
            continue
        if fp and fp in seen:
            older = r if int(r.get("number") or 0) < int(seen[fp].get("number") or 0) else seen[fp]
            out[older["id"]] = "duplicate fingerprint"
        if fp and (fp not in seen or int(r.get("number") or 0) > int(seen[fp].get("number") or 0)):
            seen[fp] = r
        obs = connection.execute(text(
            "SELECT o.check_id, o.run_id, c.verdict, rc.verdict AS run_verdict "
            "FROM finding_observations o LEFT JOIN acceptance_checks c ON c.id = o.check_id "
            "LEFT JOIN acceptance_checks rc ON rc.code_task_run_id = o.run_id "
            "AND rc.lineage_root_run_id = :l WHERE o.finding_id = :f"),
            {"f": r["id"], "l": lineage}).mappings().all()
        if fp == "gate:acceptance_met":
            if obs and any(o["run_verdict"] in BLIND_VERDICTS for o in obs) and not any(
                    o["run_verdict"] in ("met", "not_met") for o in obs):
                out[r["id"]] = "its check was blind (not_run)"
                continue
            later = connection.execute(text(
                "SELECT 1 FROM acceptance_checks c WHERE c.project_id = :p "
                "AND c.lineage_root_run_id = :l AND c.verdict IN ('met', 'not_met') "
                "AND c.created_at > :since AND EXISTS (SELECT 1 FROM finding_observations o "
                "JOIN findings f ON f.id = o.finding_id WHERE o.check_id = c.id "
                "AND f.fingerprint LIKE 'ac:%') LIMIT 1"),
                {"p": project_id, "l": lineage,
                 "since": r.get("updated_at") or r.get("created_at")}).first()
            if later:
                out[r["id"]] = "superseded by a later judged check"
            continue
        if obs and all(o["check_id"] and o["verdict"] in BLIND_VERDICTS for o in obs):
            out[r["id"]] = "its source check did not run"
    return out


def drop_stale(connection: Any, rows: Iterable[Mapping[str, Any]], *, project_id: str,
               lineage: str) -> dict[str, str]:
    """`stale_rows`, and each moved to the terminal `superseded` state with its reason (once:
    only a row still open or held moves), so counts, estimates and badges stop including it."""
    from sqlalchemy import text

    why = stale_rows(connection, rows, project_id=project_id, lineage=lineage)
    for fid, reason in why.items():
        got = connection.execute(text(
            "UPDATE findings SET status = 'superseded', waive_reason = :w, "
            "followup_run_id = NULL, updated_at = now() "
            "WHERE id = :i AND status IN ('open', 'in_progress') RETURNING id"),
            {"w": reason, "i": fid}).scalar()
        if got:
            row = get(connection, fid)
            if row:
                _sync_work_item(connection, row)
    return why


def followup_line(row: Mapping[str, Any]) -> str:
    """One open row as the follow-up's finding line: its key, what it is, where, the proof
    that would close it."""
    where = f" {row['path']}:{row['line']}" if row.get("path") and row.get("line") else (
        f" {row['path']}" if row.get("path") else "")
    seen = f" (seen {row['seen_count']}x)" if int(row.get("seen_count") or 1) > 1 else ""
    reopened = " REGRESSED" if int(row.get("reopened_count") or 0) else ""
    body = " ".join(str(row.get("body") or row.get("title") or "").split())
    if row.get("kind") == "acceptance_gap":
        also = [ln.strip() for ln in str(row.get("body") or "").splitlines()
                if ln.strip().startswith(ALSO)]
        body = " ".join(" ".join([str(row.get("title") or ""), *also]).split())
    return f"{human_key(row)}{where}{reopened}{seen}: {body}"[:1200]


def followup_spec(connection: Any, *, project_id: str, lineage: str,
                  only: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """What a follow-up is built from (LLD §5.1): the lineage's open rows NOT held by a live
    follow-up (most severe first, gaps before reviews), and the frozen contract word for word
    with its open items marked.

    {"findings": [{severity, text, ledger_id, key}], "ledger_ids", "contract_block",
     "acceptance_criteria": [the open contract items, verbatim], "contract_id"}"""
    from . import contract as _contract

    release_dead_holds(connection, project_id=project_id, lineage=lineage)
    rows = open_for(connection, project_id=project_id, lineage=lineage, include_held=True)
    held = set(held_by_live_followup(connection, [r["id"] for r in rows]))
    rows = [r for r in rows if r["id"] not in held]
    if only is not None:
        keep = set(only)
        rows = [r for r in rows if r["id"] in keep]
    stale = drop_stale(connection, rows, project_id=project_id, lineage=lineage)
    rows = [r for r in rows if r["id"] not in stale]
    contract = _contract.latest(connection, root_run_id=lineage, project_id=project_id)
    open_items = {r.get("contract_item_id"): r for r in open_for(
        connection, project_id=project_id, lineage=lineage)
        if r["kind"] == "acceptance_gap" and r.get("contract_item_id")}
    block = ""
    criteria: list[str] = []
    if contract and contract.get("items"):
        lines = []
        for it in contract["items"]:
            gap = open_items.get(it.get("id"))
            mark = f" [OPEN {human_key(gap)}]" if gap else ""
            lines.append(f"{it['id']}{mark}: {it['text']}")
            if gap:
                criteria.append(f"{it['id']}: {it['text']}")
        block = (f"THE ACCEPTANCE CONTRACT (v{contract['version']}, word for word — the whole "
                 "lineage is held to every item; the ones marked OPEN are not met yet):\n"
                 + "\n".join(lines))
    return {"findings": [{"severity": r["severity"], "text": followup_line(r),
                          "ledger_id": r["id"], "key": human_key(r)} for r in rows],
            "ledger_ids": [r["id"] for r in rows], "contract_block": block,
            "acceptance_criteria": criteria, "contract_id": (contract or {}).get("id")}


__all__ = [
    "KINDS", "LIVE", "LedgerDelta", "SEVERITIES", "by_key", "followup_line", "followup_spec",
    "gap_fingerprint", "get", "held_by_live_followup", "hold", "human_key", "lineage_of",
    "drop_stale", "mark_fixed", "merge_into", "stale_rows", "observations", "observe_check", "observe_findings",
    "observe_review", "open_for", "release_dead_holds", "review_fingerprint", "title_of",
    "transition", "upsert",
]
