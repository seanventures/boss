"""Isolated copies of a task, and the apply that puts a finished one back (BOSSAPP-1252).

A teammate runs work on a copy. The copy has its own branch and head. Nothing here
writes the shared branch until ``apply_copy`` of a finished copy whose base still
matches that branch. Applying an unfinished copy is refused. A copy that fails, or
whose shared branch moved, is a dead letter the board can see. Apply writes one
receipt keyed by the copy, so a repeat does not move the branch again.

The caller owns the transaction. Catch a refusal inside it if the dead letter
should stay: an exception that leaves the transaction rolls the letter back.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from .core import emit_event, make_id
from .receipts import ReceiptService

_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")
_HEAD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")

UNFINISHED = (
    "This copy is not finished. Finish it before it can touch the shared branch."
)
CONFLICT = (
    "The shared branch moved. This copy was left as a dead letter. "
    "Rebase it, then apply again."
)
DEAD_RUN = (
    "This copy failed and is a dead letter. Retry it before it can touch the shared branch."
)
RETRY_CONFLICT = "Rebase this copy onto the shared branch before you apply it."


class TaskCopyError(ValueError):
    """A request the board can show and the caller can fix."""

    code = "task_copy_invalid"


class TaskCopyMissing(TaskCopyError):
    code = "task_copy_missing"


class TaskCopyRefused(TaskCopyError):
    """The shared branch was not changed."""

    code = "task_copy_refused"


class TaskCopyConflict(TaskCopyRefused):
    """The shared branch moved under this copy. The copy is a dead letter."""

    code = "task_copy_conflict"


def _branch_name(value: str) -> str:
    branch = (value or "").strip()
    if not _BRANCH.match(branch) or ".." in branch or "//" in branch or branch.endswith("/"):
        raise TaskCopyError("Shared branch must be a branch name.")
    return branch


def _revision(value: str, label: str) -> str:
    head = (value or "").strip()
    if not _HEAD.match(head):
        raise TaskCopyError(f"{label} must be a revision token.")
    return head


def _key(value: str) -> str:
    key = (value or "").strip()
    if not _KEY.match(key):
        raise TaskCopyError("Task key must be letters or digits.")
    return key


def _words(value: str, *, empty: str, limit: int) -> str:
    cleaned = " ".join((value or "").split())
    if not cleaned or len(cleaned) > limit:
        raise TaskCopyError(empty)
    return cleaned


def _actor(value: str) -> str:
    return _words(value, empty="Name who is doing this.", limit=80)


def _project(connection: Connection, project_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT p.id, p.workspace_id, w.org_id FROM projects p "
        "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :id"),
        {"id": project_id}).mappings().first()
    if row is None:
        raise TaskCopyMissing("That project does not exist.")
    return dict(row)


def _board_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "shared_branch": row["shared_branch"],
        "shared_head": row["shared_head"],
    }


def _copy_out(row: dict[str, Any], board: dict[str, Any], *, replayed: bool = False) -> dict[str, Any]:
    return {
        "id": row["id"],
        "board_id": row["board_id"],
        "project_id": row["project_id"],
        "task_key": row["task_key"],
        "title": row["title"],
        "teammate": row["teammate"],
        "copy_branch": row["copy_branch"],
        "base_head": row["base_head"],
        "copy_head": row["copy_head"],
        "status": row["status"],
        "note": row["note"] or "",
        "failure_reason": row["failure_reason"] or "",
        "failure_kind": row["failure_kind"] or "",
        "receipt_id": row["receipt_id"],
        "shared_branch": board["shared_branch"],
        "shared_head": board["shared_head"],
        "replayed": replayed,
    }


def _pack(board: dict[str, Any], copy: dict[str, Any], *, replayed: bool = False) -> dict[str, Any]:
    return {"board": _board_out(board), "copy": _copy_out(copy, board, replayed=replayed)}


def _lock_board(connection: Connection, project_id: str, board_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT * FROM task_copy_boards WHERE id = :id AND project_id = :project FOR UPDATE"),
        {"id": board_id, "project": project_id}).mappings().first()
    if row is None:
        raise TaskCopyMissing("That board is not in this project.")
    return dict(row)


def _read_board(connection: Connection, project_id: str, board_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT * FROM task_copy_boards WHERE id = :id AND project_id = :project"),
        {"id": board_id, "project": project_id}).mappings().first()
    if row is None:
        raise TaskCopyMissing("That board is not in this project.")
    return dict(row)


def _lock_copy(connection: Connection, project_id: str, copy_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    found = connection.execute(text(
        "SELECT board_id FROM task_copies WHERE id = :id AND project_id = :project"),
        {"id": copy_id, "project": project_id}).first()
    if found is None:
        raise TaskCopyMissing("That copy is not on this board.")
    board = _lock_board(connection, project_id, found[0])
    row = connection.execute(text(
        "SELECT * FROM task_copies WHERE id = :id AND project_id = :project FOR UPDATE"),
        {"id": copy_id, "project": project_id}).mappings().first()
    if row is None:
        raise TaskCopyMissing("That copy is not on this board.")
    return dict(row), board


def _read_pair(connection: Connection, project_id: str, copy_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM task_copies WHERE id = :id AND project_id = :project"),
        {"id": copy_id, "project": project_id}).mappings().first()
    if row is None:
        raise TaskCopyMissing("That copy is not on this board.")
    copy = dict(row)
    return copy, _read_board(connection, project_id, copy["board_id"])


def _find_copy(connection: Connection, board_id: str, task_key: str, teammate: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM task_copies WHERE board_id = :board AND task_key = :task "
        "AND teammate = :teammate"),
        {"board": board_id, "task": task_key, "teammate": teammate}).mappings().first()
    return dict(row) if row else None


def _letter(connection: Connection, *, copy: dict[str, Any], board: dict[str, Any],
            kind: str, reason: str, actor: str) -> None:
    connection.execute(text(
        "INSERT INTO task_copy_dead_letters ("
        " id, copy_id, project_id, kind, reason, shared_head, copy_head"
        ") VALUES ("
        " :id, :copy_id, :project_id, :kind, :reason, :shared_head, :copy_head)"),
        {"id": make_id("tcd"), "copy_id": copy["id"], "project_id": copy["project_id"],
         "kind": kind, "reason": reason, "shared_head": board["shared_head"],
         "copy_head": copy["copy_head"]})
    connection.execute(text(
        "UPDATE task_copies SET status = 'dead', failure_kind = :kind, "
        "failure_reason = :reason, updated_at = now() WHERE id = :id"),
        {"id": copy["id"], "kind": kind, "reason": reason})
    emit_event(connection, copy["project_id"], "task_copy.dead_letter", actor, "task_copy",
               copy["id"], {"kind": kind, "reason": reason, "shared_head": board["shared_head"]})


def _settle_letter(connection: Connection, copy_id: str) -> None:
    connection.execute(text(
        "UPDATE task_copy_dead_letters SET retried_at = now() "
        "WHERE copy_id = :id AND retried_at IS NULL"),
        {"id": copy_id})


def open_board(connection: Connection, *, project_id: str, shared_branch: str, shared_head: str,
               actor: str) -> dict[str, Any]:
    """Open the shared board for one branch. A second open does not rewind its head."""
    branch = _branch_name(shared_branch)
    head = _revision(shared_head, "Starting head")
    who = _actor(actor)
    _project(connection, project_id)
    existing = connection.execute(text(
        "SELECT * FROM task_copy_boards WHERE project_id = :project AND shared_branch = :branch"),
        {"project": project_id, "branch": branch}).mappings().first()
    if existing:
        return {"board": _board_out(dict(existing)), "copies": []}
    board_id = make_id("tcb")
    nested = connection.begin_nested()
    try:
        connection.execute(text(
            "INSERT INTO task_copy_boards ("
            " id, project_id, shared_branch, shared_head, created_by"
            ") VALUES (:id, :project, :branch, :head, :actor)"),
            {"id": board_id, "project": project_id, "branch": branch, "head": head, "actor": who})
        nested.commit()
    except IntegrityError:
        nested.rollback()
        existing = connection.execute(text(
            "SELECT * FROM task_copy_boards WHERE project_id = :project AND shared_branch = :branch"),
            {"project": project_id, "branch": branch}).mappings().first()
        if existing is None:
            raise TaskCopyError("The shared board could not be opened.")
        return {"board": _board_out(dict(existing)), "copies": []}
    board = _read_board(connection, project_id, board_id)
    emit_event(connection, project_id, "task_copy.board_opened", who, "task_copy_board", board_id,
               {"shared_branch": branch, "shared_head": head})
    return {"board": _board_out(board), "copies": []}


def board_view(connection: Connection, *, project_id: str, board_id: str) -> dict[str, Any]:
    board = _read_board(connection, project_id, board_id)
    rows = connection.execute(text(
        "SELECT * FROM task_copies WHERE board_id = :board AND project_id = :project "
        "ORDER BY created_at, id"),
        {"board": board_id, "project": project_id}).mappings().all()
    return {"board": _board_out(board), "copies": [_copy_out(dict(row), board) for row in rows]}


def start_copy(connection: Connection, *, project_id: str, board_id: str, task_key: str,
               title: str, teammate: str, actor: str) -> dict[str, Any]:
    """Start an isolated copy at the shared head. The shared head is not written."""
    key = _key(task_key)
    name = _words(title, empty="Give the copy a title.", limit=200)
    person = _words(teammate, empty="Name the teammate.", limit=80)
    who = _actor(actor)
    board = _lock_board(connection, project_id, board_id)
    existing = _find_copy(connection, board_id, key, person)
    if existing:
        return _pack(board, existing)
    copy_id = make_id("tcp")
    copy_branch = f"copy/{board['shared_branch']}/{key}/{copy_id}"
    nested = connection.begin_nested()
    try:
        connection.execute(text(
            "INSERT INTO task_copies ("
            " id, board_id, project_id, task_key, title, teammate, copy_branch,"
            " base_head, copy_head, status, created_by"
            ") VALUES ("
            " :id, :board, :project, :task, :title, :teammate, :branch,"
            " :head, :head, 'running', :actor)"),
            {"id": copy_id, "board": board_id, "project": project_id, "task": key,
             "title": name, "teammate": person, "branch": copy_branch,
             "head": board["shared_head"], "actor": who})
        nested.commit()
    except IntegrityError as error:
        nested.rollback()
        existing = _find_copy(connection, board_id, key, person)
        if existing is None:
            raise TaskCopyError("That copy could not be started.") from error
        return _pack(board, existing)
    emit_event(connection, project_id, "task_copy.started", who, "task_copy", copy_id,
               {"task_key": key, "teammate": person, "copy_branch": copy_branch,
                "shared_head": board["shared_head"]})
    copy, fresh = _read_pair(connection, project_id, copy_id)
    return _pack(fresh, copy)


def record_work(connection: Connection, *, project_id: str, copy_id: str, note: str,
                copy_head: str, actor: str) -> dict[str, Any]:
    """Record work on the copy only. The shared branch is not read for writing."""
    body = _words(note, empty="A note is required.", limit=2000)
    head = _revision(copy_head, "Copy head")
    who = _actor(actor)
    copy, _board = _lock_copy(connection, project_id, copy_id)
    if copy["status"] != "running":
        raise TaskCopyRefused("This copy is not running. The shared branch was not changed.")
    connection.execute(text(
        "UPDATE task_copies SET note = :note, copy_head = :head, updated_at = now() "
        "WHERE id = :id"),
        {"id": copy_id, "note": body, "head": head})
    emit_event(connection, project_id, "task_copy.worked", who, "task_copy", copy_id,
               {"copy_head": head})
    fresh, board = _read_pair(connection, project_id, copy_id)
    return _pack(board, fresh)


def finish_copy(connection: Connection, *, project_id: str, copy_id: str, actor: str) -> dict[str, Any]:
    """Mark the copy finished. The shared branch stays where it is."""
    who = _actor(actor)
    copy, _board = _lock_copy(connection, project_id, copy_id)
    if copy["status"] != "running":
        raise TaskCopyRefused("Only a running copy can be finished.")
    if not (copy["note"] or "").strip() or copy["copy_head"] == copy["base_head"]:
        raise TaskCopyRefused("Record work on the copy before you finish it.")
    connection.execute(text(
        "UPDATE task_copies SET status = 'finished', finished_at = now(), updated_at = now() "
        "WHERE id = :id"),
        {"id": copy_id})
    emit_event(connection, project_id, "task_copy.finished", who, "task_copy", copy_id,
               {"copy_head": copy["copy_head"]})
    fresh, board = _read_pair(connection, project_id, copy_id)
    return _pack(board, fresh)


def fail_copy(connection: Connection, *, project_id: str, copy_id: str, reason: str,
              actor: str) -> dict[str, Any]:
    """Park a failed run as a dead letter. The shared branch is not changed."""
    why = _words(reason, empty="Say why the copy failed.", limit=500)
    who = _actor(actor)
    copy, board = _lock_copy(connection, project_id, copy_id)
    if copy["status"] == "dead" and copy["failure_kind"] == "run":
        return _pack(board, copy)
    if copy["status"] not in ("running", "finished"):
        raise TaskCopyRefused("This copy cannot be failed. The shared branch was not changed.")
    _letter(connection, copy=copy, board=board, kind="run", reason=why, actor=who)
    fresh, current = _read_pair(connection, project_id, copy_id)
    return _pack(current, fresh)


def retry_copy(connection: Connection, *, project_id: str, copy_id: str, actor: str) -> dict[str, Any]:
    """Put a failed run back to running. A conflict dead letter must be rebased, not retried."""
    who = _actor(actor)
    copy, board = _lock_copy(connection, project_id, copy_id)
    if copy["status"] != "dead":
        raise TaskCopyRefused("Only a dead letter can be retried.")
    if copy["failure_kind"] == "conflict":
        raise TaskCopyRefused(RETRY_CONFLICT)
    _settle_letter(connection, copy_id)
    connection.execute(text(
        "UPDATE task_copies SET status = 'running', failure_kind = '', failure_reason = '', "
        "updated_at = now() WHERE id = :id"),
        {"id": copy_id})
    emit_event(connection, project_id, "task_copy.retried", who, "task_copy", copy_id,
               {"shared_head": board["shared_head"]})
    fresh, current = _read_pair(connection, project_id, copy_id)
    return _pack(current, fresh)


def rebase_copy(connection: Connection, *, project_id: str, copy_id: str, copy_head: str,
                actor: str) -> dict[str, Any]:
    """Point the copy at the current shared head. The shared head itself is not written."""
    head = _revision(copy_head, "Copy head")
    who = _actor(actor)
    copy, board = _lock_copy(connection, project_id, copy_id)
    if copy["status"] == "applied":
        raise TaskCopyRefused("An applied copy is already on the shared branch.")
    if copy["status"] == "running":
        raise TaskCopyRefused("Finish the copy before you rebase it.")
    if copy["status"] == "dead" and copy["failure_kind"] != "conflict":
        raise TaskCopyRefused(
            "Retry this dead letter. Rebase is for a copy the shared branch moved under.")
    if copy["status"] not in ("finished", "dead"):
        raise TaskCopyRefused("This copy cannot be rebased.")
    _settle_letter(connection, copy_id)
    connection.execute(text(
        "UPDATE task_copies SET base_head = :base, copy_head = :head, status = 'finished', "
        "failure_kind = '', failure_reason = '', "
        "finished_at = COALESCE(finished_at, now()), updated_at = now() WHERE id = :id"),
        {"id": copy_id, "base": board["shared_head"], "head": head})
    emit_event(connection, project_id, "task_copy.rebased", who, "task_copy", copy_id,
               {"base_head": board["shared_head"], "copy_head": head})
    fresh, current = _read_pair(connection, project_id, copy_id)
    return _pack(current, fresh)


def apply_copy(connection: Connection, *, project_id: str, copy_id: str, actor: str) -> dict[str, Any]:
    """Move the shared branch to a finished copy, once. Unfinished work is refused."""
    who = _actor(actor)
    copy, board = _lock_copy(connection, project_id, copy_id)
    if copy["status"] == "applied":
        return _pack(board, copy, replayed=True)
    if copy["status"] == "dead" and copy["failure_kind"] == "conflict":
        raise TaskCopyConflict(CONFLICT)
    if copy["status"] == "dead":
        raise TaskCopyRefused(DEAD_RUN)
    if copy["status"] != "finished":
        raise TaskCopyRefused(UNFINISHED)
    if board["shared_head"] != copy["base_head"]:
        _letter(connection, copy=copy, board=board, kind="conflict", reason=CONFLICT, actor=who)
        raise TaskCopyConflict(CONFLICT)
    new_head = copy["copy_head"]
    connection.execute(text(
        "UPDATE task_copy_boards SET shared_head = :head, updated_at = now() WHERE id = :id"),
        {"id": board["id"], "head": new_head})
    scope = _project(connection, project_id)
    dedupe_key = f"task-copy-apply:{copy_id}"
    payload = {
        "shared_branch": board["shared_branch"],
        "from_head": copy["base_head"],
        "to_head": new_head,
        "task_key": copy["task_key"],
        "teammate": copy["teammate"],
    }
    receipt, created = ReceiptService().record_completed(
        connection, scope["org_id"], scope["workspace_id"], project_id, dedupe_key,
        "task_copy.apply",
        {"copy_id": copy_id, "board_id": board["id"], "data_boundary": "internal"},
        payload, provider_ref=copy_id)
    if not created:
        raise TaskCopyError(
            "This apply was already recorded. The shared branch was not moved again.")
    connection.execute(text(
        "UPDATE task_copies SET status = 'applied', receipt_id = :receipt, "
        "applied_at = now(), updated_at = now() WHERE id = :id"),
        {"id": copy_id, "receipt": receipt["id"]})
    emit_event(connection, project_id, "task_copy.applied", who, "task_copy", copy_id,
               {"shared_branch": board["shared_branch"], "shared_head": new_head,
                "receipt_id": receipt["id"], "dedupe_key": dedupe_key})
    fresh, current = _read_pair(connection, project_id, copy_id)
    return _pack(current, fresh, replayed=False)
