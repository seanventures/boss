"""EVERY OUTPUT OF A WORK ITEM IN ONE PLACE, AND WHAT IT MENTIONS (BOSSAPP-882).

A work item's results were spread over the things that made them: a build's pull request on
``code_task_runs.pull_request_url``, its preview on ``app_previews`` / ``preview_sandboxes``, a
deploy on ``stack_deployments``, a panel's minutes and PRD on ``meeting_runs``, an artifact on
``artifacts`` — each read on its own screen. And an item that said "blocked on WI-12, see
github.com/acme/app/pull/7" linked nothing.

OUTPUTS (``work_item_outputs``): one row per (item, kind, ref) — pull_request | preview | deploy
| artifact | document | other — with its title, status and where it came from (source_kind,
source_id). ``collect`` fills them from the runs that serve the item (work_items.runs_of): it is
called when a run completes (work_items.on_run_completed, the one completion door every run
kind already calls) and on demand (Refresh). A person may add one by hand (``record``). Reading
(``view``) refreshes each derived row's status from its source in the same read, so the list
says the build failed or the preview stopped without anyone collecting again. No network call:
a pull request's status is BOSS's own (its build and its merge decision), not GitHub's.

REFERENCES (``work_item_refs``): what an item MENTIONS, parsed from its title and description by
``parse_mentions`` — another item's key (WI-12), a pull request URL, a BOSS id (dec_…, crun_…,
wfr_…), any other http(s) link as a document — written as source='mention' and replaced each
time the text changes (``refresh_mentions``, called by work_items.create_item/update_item).
References a person adds stay (source='manual'). An item's BACKLINKS are the refs of other items
that point at it.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

log = logging.getLogger(__name__)

OUTPUT_KINDS = ("pull_request", "preview", "deploy", "artifact", "document", "other")
REF_KINDS = ("work_item", "pull_request", "decision", "code_task", "workflow_run", "document")

_PR_URL = re.compile(r"https?://[^\s<>()]+/pull/\d+")
_URL = re.compile(r"https?://[^\s<>()\"']+")
_ITEM_KEY = re.compile(r"(?<![A-Za-z0-9_-])([A-Z]{1,8}-\d{1,7})(?![A-Za-z0-9_])")
_BOSS_ID = re.compile(r"(?<![A-Za-z0-9_])((?:dec|crun|wfr)_[A-Za-z0-9]{6,40})(?![A-Za-z0-9_])")
_ID_KIND = {"dec": "decision", "crun": "code_task", "wfr": "workflow_run"}


class OutputError(ValueError):
    """An output or reference BOSS refuses."""


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


# ── mentions ────────────────────────────────────────────────────────────────────────────────

def parse_mentions(*texts: Optional[str]) -> list[tuple[str, str, str]]:
    """[(to_kind, to_ref, the text that mentioned it)] in the order they appear, deduplicated.
    A work-item key is returned as its key (the caller resolves it to an id)."""
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, ref: str, said: str) -> None:
        if (kind, ref) not in seen:
            seen.add((kind, ref))
            out.append((kind, ref, said))

    for body in texts:
        body = str(body or "")
        prs = set()
        for m in _PR_URL.finditer(body):
            prs.add(m.group(0))
            add("pull_request", m.group(0).rstrip(".,;"), m.group(0))
        for m in _URL.finditer(body):
            url = m.group(0).rstrip(".,;")
            if url not in prs and not _PR_URL.fullmatch(url):
                add("document", url, url)
        stripped = _URL.sub(" ", body)
        for m in _ITEM_KEY.finditer(stripped):
            add("work_item", m.group(1), m.group(1))
        for m in _BOSS_ID.finditer(stripped):
            add(_ID_KIND[m.group(1).split("_", 1)[0]], m.group(1), m.group(1))
    return out


def refresh_mentions(connection: Connection, *, project_id: str, item_id: str,
                     actor: str = "boss") -> list[dict[str, Any]]:
    """Re-read the item's title and description; replace its mention refs. Manual refs stay."""
    row = connection.execute(text(
        "SELECT title, description, human_key FROM work_items WHERE id = :i AND project_id = :p"),
        {"i": item_id, "p": project_id}).mappings().first()
    if row is None:
        raise LookupError(f"no work item {item_id} in this project")
    found = parse_mentions(row["title"], row["description"])
    keys = [ref for kind, ref, _ in found if kind == "work_item"]
    by_key = {r["human_key"]: r["id"] for r in connection.execute(text(
        "SELECT id, human_key FROM work_items WHERE project_id = :p AND human_key = ANY(:k)"),
        {"p": project_id, "k": keys}).mappings()} if keys else {}
    connection.execute(text(
        "DELETE FROM work_item_refs WHERE from_item_id = :i AND source = 'mention'"),
        {"i": item_id})
    for kind, ref, said in found:
        if kind == "work_item":
            ref = by_key.get(ref)
            if not ref or ref == item_id:
                continue      # a key that names no item here (or the item itself) links nothing
        connection.execute(text(
            "INSERT INTO work_item_refs (project_id, from_item_id, to_kind, to_ref, mention, "
            " source, created_by) VALUES (:p, :i, :k, :r, :m, 'mention', :a) "
            "ON CONFLICT (from_item_id, to_kind, to_ref) DO NOTHING"),
            {"p": project_id, "i": item_id, "k": kind, "r": ref, "m": said[:300], "a": actor})
    return refs_of(connection, item_id)


def add_ref(connection: Connection, *, project_id: str, item_id: str, to_kind: str, to_ref: str,
            actor: str) -> list[dict[str, Any]]:
    if to_kind not in REF_KINDS:
        raise OutputError(f"a reference is one of {', '.join(REF_KINDS)}")
    to_ref = str(to_ref or "").strip()
    if not to_ref:
        raise OutputError("name what it refers to")
    if connection.execute(text("SELECT 1 FROM work_items WHERE id = :i AND project_id = :p"),
                          {"i": item_id, "p": project_id}).first() is None:
        raise LookupError(f"no work item {item_id} in this project")
    if to_kind == "work_item" and connection.execute(text(
            "SELECT 1 FROM work_items WHERE id = :i AND project_id = :p"),
            {"i": to_ref, "p": project_id}).first() is None:
        raise LookupError(f"no work item {to_ref} in this project")
    connection.execute(text(
        "INSERT INTO work_item_refs (project_id, from_item_id, to_kind, to_ref, mention, source, "
        " created_by) VALUES (:p, :i, :k, :r, NULL, 'manual', :a) "
        "ON CONFLICT (from_item_id, to_kind, to_ref) DO UPDATE SET source = 'manual'"),
        {"p": project_id, "i": item_id, "k": to_kind, "r": to_ref[:500], "a": actor})
    emit_event(connection, project_id, "work_item.referenced", actor, "work_item", item_id,
               {"to_kind": to_kind, "to_ref": to_ref[:500]})
    return refs_of(connection, item_id)


def _ref_view(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"to_kind": r["to_kind"], "to_ref": r["to_ref"], "mention": r.get("mention"),
            "source": r["source"], "title": r.get("title"), "status": r.get("status"),
            "human_key": r.get("human_key")}


def refs_of(connection: Connection, item_id: str) -> list[dict[str, Any]]:
    return [_ref_view(r) for r in connection.execute(text(
        "SELECT r.to_kind, r.to_ref, r.mention, r.source, w.title, w.status, w.human_key "
        "FROM work_item_refs r LEFT JOIN work_items w ON r.to_kind = 'work_item' AND w.id = r.to_ref "
        "WHERE r.from_item_id = :i ORDER BY r.created_at, r.to_kind, r.to_ref"),
        {"i": item_id}).mappings()]


def backlinks(connection: Connection, item_id: str) -> list[dict[str, Any]]:
    return [{"id": r["id"], "human_key": r["human_key"], "title": r["title"],
             "status": r["status"], "source": r["source"]} for r in connection.execute(text(
        "SELECT w.id, w.human_key, w.title, w.status, r.source FROM work_item_refs r "
        "JOIN work_items w ON w.id = r.from_item_id WHERE r.to_kind = 'work_item' "
        "AND r.to_ref = :i ORDER BY r.created_at"), {"i": item_id}).mappings()]


# ── outputs ─────────────────────────────────────────────────────────────────────────────────

def _upsert(connection: Connection, *, project_id: str, item_id: str, kind: str, ref: str,
            title: str, status: Optional[str], source_kind: Optional[str],
            source_id: Optional[str], actor: str) -> None:
    connection.execute(text(
        "INSERT INTO work_item_outputs (id, project_id, work_item_id, kind, ref, title, status, "
        " source_kind, source_id, created_by) VALUES (:id, :p, :w, :k, :r, :t, :s, :sk, :si, :a) "
        "ON CONFLICT (work_item_id, kind, ref) DO UPDATE SET title = EXCLUDED.title, "
        " status = EXCLUDED.status, source_kind = COALESCE(EXCLUDED.source_kind, "
        " work_item_outputs.source_kind), source_id = COALESCE(EXCLUDED.source_id, "
        " work_item_outputs.source_id), updated_at = now()"),
        {"id": make_id("wio"), "p": project_id, "w": item_id, "k": kind, "r": ref[:1000],
         "t": (title or ref)[:300], "s": status, "sk": source_kind, "si": source_id, "a": actor})


def record(connection: Connection, *, project_id: str, item_id: str, kind: str, ref: str,
           title: str = "", status: Optional[str] = None, actor: str) -> dict[str, Any]:
    """A person (or a writer) records an output by hand."""
    if kind not in OUTPUT_KINDS:
        raise OutputError(f"an output is one of {', '.join(OUTPUT_KINDS)}")
    ref = str(ref or "").strip()
    if not ref:
        raise OutputError("an output needs a link or an id")
    if connection.execute(text("SELECT 1 FROM work_items WHERE id = :i AND project_id = :p"),
                          {"i": item_id, "p": project_id}).first() is None:
        raise LookupError(f"no work item {item_id} in this project")
    _upsert(connection, project_id=project_id, item_id=item_id, kind=kind, ref=ref,
            title=title, status=status, source_kind=None, source_id=None, actor=actor)
    emit_event(connection, project_id, "work_item.output_recorded", actor, "work_item", item_id,
               {"kind": kind, "ref": ref[:500]})
    return view(connection, project_id, item_id)


def _rows(connection: Connection, sql: str, params: dict) -> list[dict[str, Any]]:
    """A source an install may lack (preview_sandboxes before it was used) reads as empty."""
    try:
        with connection.begin_nested():
            return [dict(r) for r in connection.execute(text(sql), params).mappings()]
    except Exception:  # noqa: BLE001
        return []


def collect(connection: Connection, *, project_id: str, item_id: str,
            actor: str = "boss") -> int:
    """Fill the item's outputs from the runs that serve it. Idempotent; returns rows touched."""
    from .work_items import runs_of

    runs = runs_of(connection, project_id, item_id)
    builds = [r["id"] for r in runs if r["kind"] == "code_task"]
    flows = [r["id"] for r in runs if r["kind"] == "workflow_run"]
    panels = [r["id"] for r in runs if r["kind"] == "meeting"]
    touched = 0

    def put(kind: str, ref: Any, title: Any, status: Any, sk: str, si: Any) -> None:
        nonlocal touched
        if ref:
            _upsert(connection, project_id=project_id, item_id=item_id, kind=kind, ref=str(ref),
                    title=str(title or ref), status=str(status) if status else None,
                    source_kind=sk, source_id=str(si), actor=actor)
            touched += 1

    if builds:
        for r in _rows(connection,
                       "SELECT c.id, c.title, c.status, c.pull_request_url, c.pull_request_number, "
                       "d.status AS decision_status FROM code_task_runs c LEFT JOIN decisions d "
                       "ON d.id = c.decision_id WHERE c.id = ANY(:ids) AND c.project_id = :p",
                       {"ids": builds, "p": project_id}):
            if r["pull_request_url"]:
                n = r["pull_request_number"]
                put("pull_request", r["pull_request_url"],
                    f"PR #{n} · {r['title']}" if n else r["title"],
                    _pr_status(r["status"], r["decision_status"]), "code_task", r["id"])
        for r in _rows(connection,
                       "SELECT id, url, status FROM app_previews WHERE run_id = ANY(:ids)",
                       {"ids": builds}):
            put("preview", r["url"] or r["id"], "Preview", r["status"], "app_preview", r["id"])
        for r in _rows(connection,
                       "SELECT id, url, status FROM preview_sandboxes WHERE run_id = ANY(:ids)",
                       {"ids": builds}):
            put("preview", r["url"] or r["id"], "Preview", r["status"], "preview_sandbox", r["id"])
    if builds or flows:
        for r in _rows(connection,
                       "SELECT id, commit_sha, status, open_url FROM stack_deployments "
                       "WHERE project_id = :p AND (code_task_run_id = ANY(:b) "
                       "OR workflow_run_id = ANY(:f))",
                       {"p": project_id, "b": builds, "f": flows}):
            put("deploy", r["open_url"] or r["id"],
                f"Deploy · {(r['commit_sha'] or '')[:7]}".rstrip(" ·"), r["status"],
                "stack_deployment", r["id"])
    if panels:
        for r in _rows(connection,
                       "SELECT m.id, COALESCE(m.title, left(m.directive, 120)) AS title, "
                       "m.minutes_artifact_id, m.decision_id, d.status AS decision_status "
                       "FROM meeting_runs m LEFT JOIN decisions d ON d.id = m.decision_id "
                       "WHERE m.id = ANY(:ids) AND m.project_id = :p",
                       {"ids": panels, "p": project_id}):
            if r["decision_id"]:
                put("document", r["decision_id"], f"PRD · {r['title']}", r["decision_status"],
                    "meeting", r["id"])
            if r["minutes_artifact_id"]:
                put("artifact", r["minutes_artifact_id"], f"Minutes · {r['title']}", None,
                    "meeting", r["id"])
    return touched


def _pr_status(run_status: Any, decision_status: Any) -> str:
    if decision_status == "approved":
        return "approved"
    if decision_status in ("rejected", "withdrawn"):
        return "blocked"
    return str(run_status or "open")


def _live_status(connection: Connection, row: Mapping[str, Any]) -> Optional[str]:
    """The status now, from the thing that made it (no network)."""
    sk, si = row.get("source_kind"), row.get("source_id")
    if not sk or not si:
        return row.get("status")
    sql = {
        "code_task": "SELECT c.status AS a, d.status AS b FROM code_task_runs c LEFT JOIN decisions d "
                     "ON d.id = c.decision_id WHERE c.id = :i",
        "app_preview": "SELECT status AS a, NULL AS b FROM app_previews WHERE id = :i",
        "preview_sandbox": "SELECT status AS a, NULL AS b FROM preview_sandboxes WHERE id = :i",
        "stack_deployment": "SELECT status AS a, NULL AS b FROM stack_deployments WHERE id = :i",
    }.get(str(sk))
    if sql is None:
        return row.get("status")
    got = _rows(connection, sql, {"i": si})
    if not got:
        return "gone"
    if sk == "code_task":
        return _pr_status(got[0]["a"], got[0]["b"])
    return str(got[0]["a"] or "") or row.get("status")


def outputs_of(connection: Connection, item_id: str) -> list[dict[str, Any]]:
    out = []
    for r in connection.execute(text(
            "SELECT * FROM work_item_outputs WHERE work_item_id = :i ORDER BY created_at, kind, ref"),
            {"i": item_id}).mappings():
        out.append({"id": r["id"], "kind": r["kind"], "ref": r["ref"], "title": r["title"],
                    "status": _live_status(connection, r), "source_kind": r["source_kind"],
                    "source_id": r["source_id"], "created_by": r["created_by"],
                    "updated_at": _iso(r["updated_at"])})
    return out


def view(connection: Connection, project_id: str, item_id: str) -> dict[str, Any]:
    if connection.execute(text("SELECT 1 FROM work_items WHERE id = :i AND project_id = :p"),
                          {"i": item_id, "p": project_id}).first() is None:
        raise LookupError(f"no work item {item_id} in this project")
    return {"outputs": outputs_of(connection, item_id), "refs": refs_of(connection, item_id),
            "backlinks": backlinks(connection, item_id)}


def quietly(fn, *args: Any, **kwargs: Any) -> Any:
    """For the hooks in work_items: outputs and mentions never fail the write that called them."""
    try:
        with args[0].begin_nested():
            return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        log.warning("work item outputs/refs not updated by %s", getattr(fn, "__name__", fn),
                    exc_info=True)
        return None


__all__ = ["OUTPUT_KINDS", "REF_KINDS", "OutputError", "add_ref", "backlinks", "collect",
           "outputs_of", "parse_mentions", "quietly", "record", "refresh_mentions", "refs_of",
           "view"]
