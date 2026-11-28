"""PE-B (BOSSAPP-785): Corrections become memory; governed builds load agent setup; seeded opinions.

Covers:
  1. Recording steers, retry fix-notes, PR rejections, human PR comments, and recurring reviewer
     findings as project learnings WITH evidence (never dropped on ``evidence=[]``) and recalling
     them in subsequent builds on the same area/paths.
  2. Proposing repeated or confirmed learnings as path/step-scoped standards.
  3. Loading an agent's role instructions (console edits included), guide skills (progressive
     disclosure), agent memory, and design-stage repo standards into governed build prompts within
     a hard token budget, reporting any omissions.
  4. Seeding the 5 default Principal Engineer opinions (editable per org in ``org_settings``).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)

OPINIONS_SETTING_KEY = "pe_engineering_opinions"
CORRECTION_SOURCES = frozenset({
    "steer",
    "retry_fix_note",
    "pr_rejection",
    "pr_comment",
    "review_finding",
})

#: Default Principal Engineer opinions seeded for every org unless customised (spec §3B).
DEFAULT_ENGINEERING_OPINIONS: tuple[dict[str, str], ...] = (
    {
        "key": "dont_weigh_dev_cost",
        "title": "Do not weigh dev cost against correctness",
        "rule": "Never trade correctness, root-cause fixes, or clean architecture for smaller shortcuts.",
    },
    {
        "key": "reproduce_end_to_end",
        "title": "Reproduce end to end",
        "rule": "Reproduce failures end to end before changing code; never guess from a log snippet alone.",
    },
    {
        "key": "existing_mechanism_first",
        "title": "Existing mechanism first",
        "rule": "Inspect and extend existing mechanisms before introducing a parallel one.",
    },
    {
        "key": "never_relabel_failure",
        "title": "Never relabel a failure without proof",
        "rule": "Never turn a failure into success or not_applicable without blast-radius proof and a Study justification.",
    },
    {
        "key": "keep_existing_callers_working",
        "title": "Keep existing callers working",
        "rule": "Check callers and contracts across the blast radius and keep existing callers working.",
    },
)


def get_engineering_opinions(
    connection: Optional[Connection],
    *,
    org_id: Optional[str] = None,
) -> list[dict[str, str]]:
    """Return org-customized engineering opinions if present, else the 5 seeded defaults."""
    if connection is not None and org_id:
        try:
            with connection.begin_nested():
                raw = connection.execute(
                    text("SELECT value FROM org_settings WHERE org_id = :o AND key = :k"),
                    {"o": org_id, "k": OPINIONS_SETTING_KEY},
                ).scalar()
            if isinstance(raw, str):
                raw = json.loads(raw)
            if isinstance(raw, list) and raw:
                return [
                    {
                        "key": str(item.get("key") or f"op_{i}"),
                        "title": str(item.get("title") or ""),
                        "rule": str(item.get("rule") or ""),
                    }
                    for i, item in enumerate(raw, start=1)
                    if isinstance(item, Mapping) and str(item.get("rule") or "").strip()
                ]
        except Exception:  # noqa: BLE001 — fall back to defaults when org_settings unavailable
            log.debug("pe_memory: reading org opinions failed, using defaults", exc_info=True)
    return [dict(item) for item in DEFAULT_ENGINEERING_OPINIONS]


def set_engineering_opinions(
    connection: Connection,
    *,
    org_id: str,
    opinions: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Persist org-level custom engineering opinions."""
    cleaned = [
        {
            "key": str(item.get("key") or f"op_{i}").strip(),
            "title": str(item.get("title") or "").strip(),
            "rule": str(item.get("rule") or "").strip(),
        }
        for i, item in enumerate(opinions, start=1)
        if str(item.get("rule") or "").strip()
    ]
    connection.execute(
        text(
            "INSERT INTO org_settings (org_id, key, value) VALUES (:o, :k, CAST(:v AS JSONB)) "
            "ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
        ),
        {"o": org_id, "k": OPINIONS_SETTING_KEY, "v": json.dumps(cleaned)},
    )
    return cleaned


def _norm_area(area: str, paths: Sequence[str]) -> list[str]:
    areas: list[str] = []
    if area and area.strip():
        areas.append(area.strip().lower())
    for p in paths or ():
        clean = str(p).strip().lstrip("./")
        if clean:
            areas.append(clean.lower())
            parent = clean.rsplit("/", 1)[0] if "/" in clean else ""
            if parent and parent.lower() not in areas:
                areas.append(parent.lower())
    return areas or ["general"]


def _correction_hash(source: str, text_body: str, areas: Sequence[str]) -> str:
    payload = f"{source}|{'|'.join(sorted(set(areas)))}|{text_body.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def record_correction(
    connection: Connection,
    *,
    project_id: str,
    source: str,
    note: str,
    area: str = "",
    paths: Sequence[str] = (),
    step: str = "",
    run_id: Optional[str] = None,
    actor: str = "operator",
    confirmed: bool = False,
    evidence_excerpt: str = "",
) -> dict[str, Any]:
    """Write a steer/fix-note/PR-rejection/PR-comment/review-finding as a project learning WITH evidence.

    Also mirrors into ``agent_memory`` (kind='project' so steers/confirmed corrections are immediately
    recallable in the next build) and proposes a scoped standard once repeated (>= 2) or confirmed.
    """
    src = str(source or "steer").strip().lower()
    if src not in CORRECTION_SOURCES:
        raise ValueError(f"unsupported correction source {src!r}; expected one of {sorted(CORRECTION_SOURCES)}")
    body = str(note or "").strip()
    if not body:
        raise ValueError("correction note is required")

    areas = _norm_area(area, paths)
    ref = f"run:{run_id}" if run_id else f"{src}:{_correction_hash(src, body, areas)[:10]}"
    excerpt = (evidence_excerpt or f"{src} by {actor}: {body}")[:1000]
    evidence = [{"type": "log", "ref": ref, "observed": True, "excerpt": excerpt}]

    learning_id: Optional[str] = None
    repeat_count = 1
    try:
        from .knowledge import learning_writeback

        with connection.begin_nested():
            rec = learning_writeback.record(
                connection,
                project_id=project_id,
                learning_kind="precedent",
                title=f"[{src}] {body[:140]}",
                statement=body[:1800],
                detail={
                    "symptom": f"{src} on {', '.join(areas[:3])}",
                    "cause_summary": body[:800],
                    "fix_summary": body[:800],
                    "applies_to": list(paths or areas)[:10],
                    "reuse_count": 1,
                },
                about={"areas": areas[:10], "symbols": [], "rules": []},
                evidence=evidence,
                confidence=0.75 if confirmed else 0.6,
                observed={"source": src, "run_id": run_id, "step": step, "paths": list(paths or ())},
            )
            learning_id = str(rec.get("node_id") or rec.get("id") or "")
            repeat_count = int(rec.get("repeat_count") or rec.get("seen_count") or 1)
    except Exception:  # noqa: BLE001 — keep agent_memory mirror working even if KG table not seeded
        log.debug("pe_memory: learning_writeback.record skipped", exc_info=True)

    # Mirror into agent_memory so build N+1 recalls it cleanly through the existing memory store.
    tagged_body = f"[{src} | area:{','.join(areas[:3])}] {body}"
    memory_row: dict[str, Any] = {}
    try:
        from . import agent_memory

        with connection.begin_nested():
            memory_row = agent_memory.remember(
                connection,
                project_id=project_id,
                actor=actor,
                kind="project",
                body=tagged_body[:1950],
                scope="project",
                source_thread_id=run_id,
            )
            use_count = connection.execute(
                text("SELECT use_count FROM agent_memory WHERE id = :id"),
                {"id": memory_row["id"]},
            ).scalar()
            if use_count is not None:
                repeat_count = max(repeat_count, int(use_count))
    except Exception:  # noqa: BLE001
        log.debug("pe_memory: agent_memory.remember skipped", exc_info=True)

    proposed_standard = None
    if confirmed or repeat_count >= 2:
        proposed_standard = {
            "scope_paths": list(paths) if paths else areas[:3],
            "scope_step": step or "all",
            "rule": body,
            "source": src,
            "evidence": evidence,
            "status": "proposed",
        }

    return {
        "learning_id": learning_id,
        "memory_id": memory_row.get("id"),
        "source": src,
        "areas": areas,
        "evidence": evidence,
        "repeat_count": repeat_count,
        "proposed_standard": proposed_standard,
    }


def _matches_scope(text_val: str, touched_paths: Sequence[str], areas: Sequence[str]) -> bool:
    if not touched_paths and not areas:
        return True
    hay = text_val.lower()
    for needle in list(touched_paths) + list(areas):
        tok = str(needle or "").strip().lower().lstrip("./")
        if not tok:
            continue
        if tok in hay:
            return True
        if "/" in tok and tok.rsplit("/", 1)[0] in hay:
            return True
    return False


def _load_role_instructions(
    connection: Optional[Connection],
    project_id: str,
    role_key: Optional[str],
) -> str:
    if connection is None or not role_key:
        return ""
    try:
        with connection.begin_nested():
            row = connection.execute(
                text(
                    "SELECT display_name, spec FROM roles "
                    "WHERE project_id = :p AND role_key = :r"
                ),
                {"p": project_id, "r": str(role_key).strip().lower()},
            ).mappings().first()
        if not row:
            return ""
        spec = row["spec"]
        if isinstance(spec, str):
            spec = json.loads(spec)
        if not isinstance(spec, Mapping):
            return ""
        parts: list[str] = []
        for field_name, label in (
            ("instructions", "Role instructions"),
            ("console_instructions", "Console instructions"),
            ("mission", "Mission"),
            ("boundaries", "Boundaries"),
        ):
            val = spec.get(field_name)
            if isinstance(val, list):
                joined = "; ".join(str(x).strip() for x in val if str(x).strip())
            else:
                joined = str(val or "").strip()
            if joined:
                parts.append(f"{label}: {joined}")
        return "\n".join(parts)
    except Exception:  # noqa: BLE001
        log.debug("pe_memory: role instructions read failed", exc_info=True)
        return ""


def _load_guide_skills(
    connection: Optional[Connection],
    project_id: str,
    role_key: Optional[str],
    *,
    step: str = "",
    touched_paths: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Progressive disclosure for guide skills: summary always; full knowledge when step/path matches."""
    if connection is None or not role_key:
        return []
    try:
        from .skill_catalogue import entries as catalogue_entries

        with connection.begin_nested():
            held = {
                str(r["skill_id"])
                for r in connection.execute(
                    text("SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r"),
                    {"p": project_id, "r": str(role_key).strip().lower()},
                ).mappings().all()
            }
            cat = catalogue_entries(connection, project_id) if held else {}
        out: list[dict[str, str]] = []
        for sid in sorted(held):
            entry = cat.get(sid)
            if not entry or not entry.get("enabled"):
                continue
            knowledge = str((entry.get("spec") or {}).get("knowledge") or "").strip()
            summary = str(entry.get("summary") or entry.get("label") or sid).strip()
            if not knowledge and not summary:
                continue
            disclose_full = bool(
                knowledge
                and (
                    (step and step.lower() in f"{sid} {summary}".lower())
                    or _matches_scope(f"{sid} {summary}", touched_paths, ())
                )
            )
            out.append({
                "skill_id": sid,
                "text": f"[{sid}] {summary}" + (f"\n{knowledge[:1200]}" if disclose_full else ""),
            })
        return out
    except Exception:  # noqa: BLE001
        log.debug("pe_memory: guide skills read failed", exc_info=True)
        return []


def _load_recalled_learnings(
    connection: Optional[Connection],
    project_id: str,
    *,
    touched_paths: Sequence[str] = (),
    areas: Sequence[str] = (),
) -> list[str]:
    if connection is None:
        return []
    items: list[str] = []
    seen: set[str] = set()
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text(
                    "SELECT body FROM agent_memory "
                    "WHERE project_id = :p AND status = 'active' "
                    "ORDER BY use_count DESC, updated_at DESC LIMIT 40"
                ),
                {"p": project_id},
            ).mappings().all()
        for r in rows:
            body = str(r["body"] or "").strip()
            if not body or body in seen:
                continue
            if _matches_scope(body, touched_paths, areas):
                items.append(body)
                seen.add(body)
    except Exception:  # noqa: BLE001
        log.debug("pe_memory: agent_memory query failed", exc_info=True)

    try:
        with connection.begin_nested():
            kg_rows = connection.execute(
                text(
                    "SELECT title, summary, attrs FROM kg_nodes "
                    "WHERE project_id = :p AND kind IN ('precedent', 'decision', 'review_finding', 'fix') "
                    "ORDER BY updated_at DESC LIMIT 30"
                ),
                {"p": project_id},
            ).mappings().all()
        for r in kg_rows:
            title = str(r["title"] or "").strip()
            summary = str(r["summary"] or "").strip()
            text_line = f"{title}: {summary}" if summary and summary != title else title
            if not text_line or text_line in seen:
                continue
            attrs_blob = str(r["attrs"] or "")
            if _matches_scope(f"{text_line} {attrs_blob}", touched_paths, areas):
                items.append(text_line)
                seen.add(text_line)
    except Exception:  # noqa: BLE001
        log.debug("pe_memory: kg_nodes query skipped", exc_info=True)
    return items


def recall_for_build(
    connection: Optional[Connection],
    *,
    project_id: str,
    role_key: Optional[str] = None,
    touched_paths: Sequence[str] = (),
    step: str = "",
    areas: Sequence[str] = (),
    repo_standards: Sequence[Mapping[str, Any] | str] = (),
    token_budget: int = 1800,
    org_id: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble budgeted Principal Engineer build context: role setup, skills, memory, standards, opinions."""
    char_budget = max(256, int(token_budget) * 4)
    sections: list[tuple[str, str]] = []
    omitted: list[str] = []

    role_text = _load_role_instructions(connection, project_id, role_key)
    if role_text:
        sections.append(("AGENT ROLE SETUP", role_text))

    recalled = _load_recalled_learnings(
        connection, project_id, touched_paths=touched_paths, areas=areas
    )
    if recalled:
        sections.append(("RECALLED PROJECT LEARNINGS & STEERS", "\n".join(f"- {x}" for x in recalled)))

    scoped_standards: list[str] = []
    for std in repo_standards or ():
        if isinstance(std, Mapping):
            s_paths = std.get("paths") or ()
            s_step = str(std.get("step") or "").strip().lower()
            rule = str(std.get("rule") or std.get("text") or "").strip()
            if not rule:
                continue
            if s_step and step and s_step not in ("all", step.lower()):
                continue
            if s_paths and touched_paths and not any(
                str(tp).startswith(str(sp)) or str(sp) in str(tp)
                for tp in touched_paths
                for sp in s_paths
            ):
                continue
            scoped_standards.append(rule)
        elif str(std).strip():
            scoped_standards.append(str(std).strip())
    if scoped_standards:
        sections.append(("SCOPED REPO STANDARDS", "\n".join(f"- {s}" for s in scoped_standards)))

    skills = _load_guide_skills(
        connection, project_id, role_key, step=step, touched_paths=touched_paths
    )
    if skills:
        sections.append(("GUIDE SKILLS", "\n".join(f"- {s['text']}" for s in skills)))

    opinions = get_engineering_opinions(connection, org_id=org_id)
    if opinions:
        sections.append((
            "ENGINEERING OPINIONS",
            "\n".join(f"- {o['title']}: {o['rule']}" for o in opinions),
        ))

    used = 0
    rendered_parts: list[str] = []
    for header, body in sections:
        block = f"=== {header} ===\n{body}\n"
        if used + len(block) <= char_budget:
            rendered_parts.append(block)
            used += len(block)
        else:
            remaining = char_budget - used
            if remaining >= 120:
                clipped = block[: remaining - 24].rstrip() + "\n... [budget clipped]\n"
                rendered_parts.append(clipped)
                used += len(clipped)
                omitted.append(f"{header} (partial)")
            else:
                omitted.append(header)

    return {
        "prompt_block": "\n".join(rendered_parts),
        "role_instructions": role_text,
        "learnings": recalled,
        "standards": scoped_standards,
        "skills": [s["skill_id"] for s in skills],
        "opinions": opinions,
        "omitted": omitted,
        "chars_used": used,
        "char_budget": char_budget,
    }


__all__ = [
    "CORRECTION_SOURCES",
    "DEFAULT_ENGINEERING_OPINIONS",
    "OPINIONS_SETTING_KEY",
    "get_engineering_opinions",
    "recall_for_build",
    "record_correction",
    "set_engineering_opinions",
]
