"""Every save of an authored skill is a version, and the history is append-only (BOSSAPP-842).

WHY. A skill is the unit an agent is given and a workflow step runs; its knowledge body is the
house style, its config is the program or the tool it reaches. Saving used to overwrite the row
in place, so "what did the editor run last Tuesday" and "put it back how it was" had no answer.

THE ONE WRITER. `skill_definitions.save_skill` is the only code that writes a skill's content,
and it calls :func:`record` after every write — so the console, the API, an approved agent draft,
a SKILL.md upload, a seed and a source sync all version by the same road. A save that changes
nothing (same content hash as the current version) does not add a row: re-running an idempotent
seed or script is not a new version.

RESTORE IS A NEW VERSION. :func:`restore` re-saves an old version's content through save_skill,
so the restored state is version N+1 with `restored_from` naming where it came from. Nothing is
ever deleted or rewritten here.

PINS. `role_skills.pinned_version_id` keeps one agent on a chosen version while the skill moves
on (NULL follows the current version). :func:`as_held_by` is what the executors that read a
skill's content at run time (guide, agent program) call to honour it. :func:`publish` moves every
holder to one version — gated on that version's tests (skill_tests.publish_gate).
"""
from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

#: What a version holds. `trusted` and `enabled` are the operator's switches about the skill, not
#: its content: a restore never flips them, and an agent never proposes them.
CONTENT_FIELDS = ("label", "summary", "behaviour_kind", "knowledge", "inputs", "produces",
                  "config", "risk_tier")
AUTHOR_KINDS = ("person", "agent", "system", "sync")
#: connection.info key a version-pinned test run sets so the executors run THAT version's
#: content (skill_tests.run_cases; read by skill_executors._as_held).
VERSION_OVERRIDE = "boss.skill_versions.override"


class SkillVersionError(ValueError):
    """Something the operator can fix, phrased for them."""

    status = 422


class VersionNotFound(SkillVersionError):
    status = 404


def snapshot(item: dict[str, Any]) -> dict[str, Any]:
    """The content of a skill (a skill_definitions row as _row returns it, or a prepared one)."""
    out = {field: item.get(field) for field in CONTENT_FIELDS}
    out["risk_tier"] = int(out["risk_tier"] or 0)
    for field in ("inputs", "produces"):
        out[field] = out[field] or []
    out["config"] = out["config"] or {}
    out["knowledge"] = str(out["knowledge"] or "")
    out["summary"] = str(out["summary"] or "")
    return out


def content_hash(content: dict[str, Any], files: Optional[dict[str, Any]] = None) -> str:
    body = json.dumps({"content": snapshot(content), "files": files or {}}, sort_keys=True,
                      default=str, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _version(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "skill_key": row["skill_key"],
        "version_no": int(row["version_no"]),
        "content": _loads(row["content"], {}),
        "files": _loads(row["files"], {}),
        "content_hash": row["content_hash"],
        "author_kind": row["author_kind"],
        "author_id": row["author_id"],
        "source_note": row["source_note"],
        "restored_from": row["restored_from"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def current(connection: Connection, project_id: str, skill_key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT v.* FROM skill_versions v JOIN skill_definitions d "
             "ON d.current_version_id = v.id WHERE d.project_id = :p AND d.skill_key = :k"),
        {"p": project_id, "k": skill_key},
    ).mappings().first()
    return _version(row) if row else None


def record(
    connection: Connection,
    *,
    project_id: str,
    skill: dict[str, Any],
    author_kind: str,
    author_id: str,
    source_note: str = "",
    files: Optional[dict[str, Any]] = None,
    restored_from: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Append a version for the content `skill` now holds; None when it equals the current one.

    Called by save_skill inside its transaction, right after the row was written, so a save that
    rolls back takes its version with it."""
    kind = author_kind if author_kind in AUTHOR_KINDS else "person"
    digest = content_hash(skill, files)
    now = current(connection, project_id, skill["skill_key"])
    if now is not None and now["content_hash"] == digest and restored_from is None:
        return None
    # Serialise version numbering per skill: the row lock is the skill itself.
    connection.execute(text("SELECT 1 FROM skill_definitions WHERE id = :i FOR UPDATE"),
                       {"i": skill["id"]})
    number = int(connection.execute(
        text("SELECT COALESCE(MAX(version_no), 0) FROM skill_versions WHERE skill_id = :s"),
        {"s": skill["id"]}).scalar() or 0) + 1
    row = connection.execute(
        text("""
            INSERT INTO skill_versions (id, project_id, skill_id, skill_key, version_no, content,
                                        files, content_hash, author_kind, author_id,
                                        source_note, restored_from)
            VALUES (:id, :p, :s, :k, :n, CAST(:c AS JSONB), CAST(:f AS JSONB), :h, :ak, :ai,
                    :note, :rf)
            RETURNING *
        """),
        {"id": make_id("skv"), "p": project_id, "s": skill["id"], "k": skill["skill_key"],
         "n": number, "c": json.dumps(snapshot(skill), default=str),
         "f": json.dumps(files or {}), "h": digest, "ak": kind,
         "ai": str(author_id or "")[:200], "note": str(source_note or "")[:500],
         "rf": restored_from},
    ).mappings().one()
    connection.execute(
        text("UPDATE skill_definitions SET current_version_id = :v WHERE id = :s"),
        {"v": row["id"], "s": skill["id"]})
    return _version(row)


def list_versions(connection: Connection, project_id: str, skill_key: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT * FROM skill_versions WHERE project_id = :p AND skill_key = :k "
             "AND skill_id = (SELECT id FROM skill_definitions WHERE project_id = :p "
             "                AND skill_key = :k) "
             "ORDER BY version_no DESC"),
        {"p": project_id, "k": skill_key},
    ).mappings().all()
    cur = connection.execute(
        text("SELECT current_version_id FROM skill_definitions WHERE project_id = :p "
             "AND skill_key = :k"), {"p": project_id, "k": skill_key}).scalar()
    out = []
    for row in rows:
        item = _version(row)
        item["current"] = item["id"] == cur
        out.append(item)
    return out


def get_version(connection: Connection, project_id: str, skill_key: str,
                version: Any) -> dict[str, Any]:
    """One version by id or by number."""
    ref = str(version or "").strip()
    if ref.isdigit():
        row = connection.execute(
            text("SELECT v.* FROM skill_versions v JOIN skill_definitions d ON d.id = v.skill_id "
                 "WHERE d.project_id = :p AND d.skill_key = :k AND v.version_no = :n"),
            {"p": project_id, "k": skill_key, "n": int(ref)}).mappings().first()
    else:
        row = connection.execute(
            text("SELECT v.* FROM skill_versions v JOIN skill_definitions d ON d.id = v.skill_id "
                 "WHERE d.project_id = :p AND d.skill_key = :k AND v.id = :i"),
            {"p": project_id, "k": skill_key, "i": ref}).mappings().first()
    if row is None:
        raise VersionNotFound(f"{skill_key} has no version {ref!r}.")
    return _version(row)


def _lines(content: dict[str, Any]) -> list[str]:
    """A version as text a person can diff: one block per field, knowledge line by line."""
    lines: list[str] = []
    for field in CONTENT_FIELDS:
        value = content.get(field)
        if field == "knowledge":
            lines.append("knowledge:")
            lines.extend(f"  {line}" for line in str(value or "").splitlines())
        else:
            shown = value if isinstance(value, str) else json.dumps(value, sort_keys=True,
                                                                     default=str)
            lines.append(f"{field}: {shown}")
    return lines


def diff_text(before: Optional[dict[str, Any]], after: dict[str, Any], *,
              before_name: str = "before", after_name: str = "after") -> str:
    """A unified diff of two contents ('' when they are the same)."""
    return "\n".join(difflib.unified_diff(
        _lines(before) if before else [], _lines(after), fromfile=before_name,
        tofile=after_name, lineterm=""))


def diff_versions(connection: Connection, project_id: str, skill_key: str, from_version: Any,
                  to_version: Any) -> dict[str, Any]:
    old = get_version(connection, project_id, skill_key, from_version)
    new = get_version(connection, project_id, skill_key, to_version)
    return {"from": old["version_no"], "to": new["version_no"],
            "diff": diff_text(old["content"], new["content"],
                              before_name=f"v{old['version_no']}",
                              after_name=f"v{new['version_no']}")}


def restore(connection: Connection, *, project_id: str, actor: str, skill_key: str,
            version: Any) -> dict[str, Any]:
    """Make an old version current again — as a NEW version copying it. A person's act.

    Goes through save_skill like every other write, so the restored content is validated by
    today's rules (an MCP server since removed is refused with a sentence, not restored broken).
    """
    from .skill_definitions import get_skill, save_skill

    old = get_version(connection, project_id, skill_key, version)
    live = get_skill(connection, project_id, skill_key)
    if live is None:
        raise VersionNotFound(f"no skill {skill_key!r} in this project.")
    content = old["content"]
    saved = save_skill(
        connection, project_id=project_id, actor=actor, skill_key=skill_key,
        label=content.get("label") or live["label"],
        behaviour_kind=content.get("behaviour_kind") or live["behaviour_kind"],
        summary=content.get("summary") or "", knowledge=content.get("knowledge") or "",
        inputs=content.get("inputs") or [], produces=content.get("produces") or [],
        config=content.get("config") or {}, risk_tier=content.get("risk_tier", 1),
        trusted=live["trusted"], enabled=live["enabled"], mode="update", origin="person",
        version_note=f"restored v{old['version_no']}", restored_from=old["id"],
        files=old["files"])
    emit_event(connection, project_id, "skill.version_restored", actor, "skill_definition",
               saved["id"], {"skill_key": skill_key, "restored": old["version_no"],
                             "version_id": saved.get("version_id")})
    return {"skill": saved, "restored_from": old["version_no"],
            "version": current(connection, project_id, skill_key)}


# ── Pins ──────────────────────────────────────────────────────────────────────────────────────


def pin(connection: Connection, *, project_id: str, actor: str, role_key: str, skill_key: str,
        version: Any = None) -> dict[str, Any]:
    """Keep one agent on one version (or, with version None, let it follow the current one)."""
    version_id = None
    if version not in (None, ""):
        version_id = get_version(connection, project_id, skill_key, version)["id"]
    row = connection.execute(
        text("UPDATE role_skills SET pinned_version_id = :v WHERE project_id = :p "
             "AND role_key = :r AND skill_id = :k RETURNING id"),
        {"v": version_id, "p": project_id, "r": role_key, "k": skill_key}).first()
    if row is None:
        raise SkillVersionError(f"{role_key} does not hold {skill_key!r}.")
    emit_event(connection, project_id, "skill.version_pinned", actor, "role_skill", str(row[0]),
               {"role_key": role_key, "skill_key": skill_key, "version_id": version_id})
    return {"role_key": role_key, "skill_key": skill_key, "pinned_version_id": version_id}


def publish(connection: Connection, *, project_id: str, actor: str, skill_key: str,
            version: Any) -> dict[str, Any]:
    """Put every agent holding the skill on `version` — refused when its tests failed (844).

    Publishing the CURRENT version clears the pins (holders follow it from now on); an older
    version pins every holder to it."""
    from .skill_tests import publish_gate

    chosen = get_version(connection, project_id, skill_key, version)
    gate = publish_gate(connection, project_id, skill_key, chosen["id"])
    if not gate["allowed"]:
        raise SkillVersionError(gate["reason"])
    now = current(connection, project_id, skill_key)
    target = None if now and now["id"] == chosen["id"] else chosen["id"]
    holders = connection.execute(
        text("UPDATE role_skills SET pinned_version_id = :v WHERE project_id = :p "
             "AND skill_id = :k RETURNING role_key"),
        {"v": target, "p": project_id, "k": skill_key}).scalars().all()
    emit_event(connection, project_id, "skill.version_published", actor, "skill_definition",
               skill_key, {"skill_key": skill_key, "version_no": chosen["version_no"],
                           "holders": list(holders)})
    return {"skill_key": skill_key, "version_no": chosen["version_no"],
            "holders": sorted(holders), "pinned": target is not None, "gate": gate}


def holders(connection: Connection, project_id: str, skill_key: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT r.role_key, r.pinned_version_id, v.version_no FROM role_skills r "
             "LEFT JOIN skill_versions v ON v.id = r.pinned_version_id "
             "WHERE r.project_id = :p AND r.skill_id = :k ORDER BY r.role_key"),
        {"p": project_id, "k": skill_key}).mappings().all()
    return [{"role_key": r["role_key"], "pinned_version_id": r["pinned_version_id"],
             "pinned_version_no": r["version_no"]} for r in rows]


def as_held_by(connection: Connection, project_id: str, skill: dict[str, Any],
               caller: Optional[str]) -> dict[str, Any]:
    """The skill as `caller` runs it: its pinned version's content when it holds a pin, else as is.

    Read at the moment of the call, like the grant itself (skill_runtime._assigned_to_agent)."""
    if not caller:
        return skill
    row = connection.execute(
        text("SELECT v.content FROM role_skills r JOIN skill_versions v "
             "ON v.id = r.pinned_version_id WHERE r.project_id = :p AND r.role_key = :r "
             "AND r.skill_id = :k"),
        {"p": project_id, "r": caller, "k": skill["skill_key"]}).first()
    if row is None:
        return skill
    return {**skill, **snapshot(_loads(row[0], {}))}
