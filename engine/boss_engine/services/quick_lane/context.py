"""QL-2 (BOSSAPP-796): Full context in before the first edit + reproduce-first enforcement.

Loads ALL five required context sources (docs/product/QUICK_LANE.md §3.4) before the builder
writes a single line of code, records every source used on the run record, renders them into
the prompt, and refuses any fix that lacks a concrete reproduction with file:line causes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)

#: The five context sources required before the first edit (BOSSAPP-796).
REQUIRED_CONTEXT_SOURCES = (
    "study_notes",
    "kg",
    "learnings",
    "standards",
    "request_evidence",
)

#: Default character budget for scoped standards so prompt context stays focused.
DEFAULT_STANDARDS_BUDGET_CHARS = 6000


class ReproductionRequiredError(ValueError):
    """Raised when a quick-lane fix tries to proceed without a verified reproduction."""


@dataclass(frozen=True)
class CauseLocation:
    """One concrete root-cause pointer in the repository."""

    file: str
    line: int
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "explanation": self.explanation}


@dataclass(frozen=True)
class ReproductionRecord:
    """Builder's pre-edit reproduction proof (QUICK_LANE.md §3.4)."""

    command: str
    exit_code: int
    output: str
    causes: tuple[CauseLocation, ...]
    existing_mechanism_checked: bool = True
    existing_mechanism_note: str = ""
    blast_radius_checked: bool = True
    checked_callers: tuple[str, ...] = ()
    real_data_shape_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "output": self.output,
            "causes": [c.to_dict() for c in self.causes],
            "existing_mechanism_checked": self.existing_mechanism_checked,
            "existing_mechanism_note": self.existing_mechanism_note,
            "blast_radius_checked": self.blast_radius_checked,
            "checked_callers": list(self.checked_callers),
            "real_data_shape_source": self.real_data_shape_source,
        }


@dataclass
class ContextBundle:
    """Everything loaded before the builder's first edit, plus audit summary."""

    study_notes: list[dict[str, Any]] = field(default_factory=list)
    kg: dict[str, Any] = field(
        default_factory=lambda: {
            "callers": [],
            "blast_radius": [],
            "precedent": [],
            "why": [],
            "consulted_node_ids": [],
        }
    )
    learnings: list[dict[str, Any]] = field(default_factory=list)
    standards: list[dict[str, Any]] = field(default_factory=list)
    standards_omitted: list[str] = field(default_factory=list)
    request_evidence: dict[str, Any] = field(default_factory=dict)

    def sources_used(self) -> dict[str, dict[str, Any]]:
        kg_refs = list(self.kg.get("consulted_node_ids") or [])
        kg_count = (
            len(self.kg.get("callers") or [])
            + len(self.kg.get("blast_radius") or [])
            + len(self.kg.get("precedent") or [])
            + len(self.kg.get("why") or [])
        )
        ev_items = [
            k for k, v in (self.request_evidence or {}).items()
            if v not in (None, "", [], {})
        ]
        return {
            "study_notes": {
                "loaded": bool(self.study_notes),
                "count": len(self.study_notes),
                "refs": [str(x.get("id") or x.get("area") or "study") for x in self.study_notes],
            },
            "kg": {
                "loaded": kg_count > 0,
                "count": kg_count,
                "callers": len(self.kg.get("callers") or []),
                "blast_radius": len(self.kg.get("blast_radius") or []),
                "precedent": len(self.kg.get("precedent") or []),
                "why": len(self.kg.get("why") or []),
                "refs": kg_refs,
            },
            "learnings": {
                "loaded": bool(self.learnings),
                "count": len(self.learnings),
                "refs": [str(x.get("id") or x.get("title") or "learning") for x in self.learnings],
            },
            "standards": {
                "loaded": bool(self.standards),
                "count": len(self.standards),
                "omitted": len(self.standards_omitted),
                "refs": [str(x.get("key") or x.get("title") or "standard") for x in self.standards],
            },
            "request_evidence": {
                "loaded": bool(ev_items),
                "count": len(ev_items),
                "refs": ev_items,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "study_notes": self.study_notes,
            "kg": self.kg,
            "learnings": self.learnings,
            "standards": self.standards,
            "standards_omitted": self.standards_omitted,
            "request_evidence": self.request_evidence,
            "sources_used": self.sources_used(),
        }


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _load_study_notes(
    connection: Optional[Connection],
    project_id: str,
    spec: dict[str, Any],
    area_hint: str,
) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    inline = spec.get("study_record") or spec.get("study_notes")
    if isinstance(inline, dict) and inline:
        notes.append({"source": "spec", **inline})
    elif isinstance(inline, list):
        for item in inline:
            if isinstance(item, dict):
                notes.append({"source": "spec", **item})
            elif isinstance(item, str) and item.strip():
                notes.append({"source": "spec", "summary": item.strip()})
    elif isinstance(inline, str) and inline.strip():
        notes.append({"source": "spec", "summary": inline.strip()})

    if connection is None:
        return notes

    try:
        with connection.begin_nested():
            rows = connection.execute(
                text(
                    "SELECT id, artifact_type, content_markdown, metadata "
                    "FROM artifacts "
                    "WHERE project_id = :project_id AND artifact_type IN ('study_record', 'study_notes') "
                    "ORDER BY created_at DESC LIMIT 5"
                ),
                {"project_id": project_id},
            ).mappings().all()
        for r in rows:
            meta = _as_dict(r.get("metadata"))
            area = str(meta.get("area") or "")
            if area_hint and area and area_hint.lower() not in area.lower() and area.lower() not in area_hint.lower():
                continue
            notes.append(
                {
                    "id": str(r["id"]),
                    "source": "artifact",
                    "area": area or area_hint,
                    "summary": str(r.get("content_markdown") or "")[:2000],
                    "metadata": meta,
                }
            )
    except Exception:  # noqa: BLE001 — optional table/rows must never crash context load
        log.debug("quick_lane.context: study artifact lookup skipped", exc_info=True)
    return notes


def _load_kg_context(
    connection: Optional[Connection],
    project_id: str,
    spec: dict[str, Any],
    symbols: Iterable[str],
    query: str,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "callers": [],
        "blast_radius": [],
        "precedent": [],
        "why": [],
        "consulted_node_ids": [],
    }
    # Allow tests or callers to supply pre-resolved KG context when running without KG tables.
    provided = spec.get("kg_context")
    if isinstance(provided, dict):
        for k in ("callers", "blast_radius", "precedent", "why", "consulted_node_ids"):
            if isinstance(provided.get(k), list):
                out[k].extend(provided[k])

    if connection is None:
        return out

    try:
        from ..knowledge.service import KnowledgeService

        svc = KnowledgeService()
        seen_ids: set[str] = set(str(x) for x in out["consulted_node_ids"])

        with connection.begin_nested():
            if query:
                prec = svc.precedent(connection, project_id=project_id, query=query)
                for hit in (prec.get("precedent") or prec.get("hits") or [])[:8]:
                    out["precedent"].append(hit)
                    nid = hit.get("node_id") or hit.get("id")
                    if nid:
                        seen_ids.add(str(nid))

            target_node_ids: list[str] = []
            for sym in symbols:
                sym = str(sym or "").strip()
                if not sym:
                    continue
                found = svc.symbol(connection, project_id=project_id, name=sym)
                for match in (found.get("matches") or found.get("symbols") or [])[:3]:
                    nid = match.get("node_id") or match.get("id")
                    if nid:
                        target_node_ids.append(str(nid))
                        seen_ids.add(str(nid))

            for nid in dict.fromkeys(target_node_ids):
                cal = svc.callers(connection, project_id=project_id, node_id=nid)
                for item in (cal.get("callers") or [])[:25]:
                    out["callers"].append(item)
                blast = svc.blast(connection, project_id=project_id, node_id=nid)
                for item in (blast.get("affected") or blast.get("blast_radius") or blast.get("nodes") or [])[:25]:
                    out["blast_radius"].append(item)
                try:
                    why = svc.why(connection, project_id=project_id, node_id=nid)
                    for item in (why.get("why") or [])[:10]:
                        out["why"].append(item)
                        wid = item.get("node_id") or item.get("id")
                        if wid:
                            seen_ids.add(str(wid))
                except LookupError:
                    pass
        out["consulted_node_ids"] = sorted(seen_ids)
    except Exception:  # noqa: BLE001 — KG may be disabled or empty in unit tests
        log.debug("quick_lane.context: KG lookup skipped", exc_info=True)
    return out


def _load_learnings(
    connection: Optional[Connection],
    project_id: str,
    spec: dict[str, Any],
    query: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in spec.get("learnings") or []:
        if isinstance(raw, dict):
            items.append(raw)
        elif isinstance(raw, str) and raw.strip():
            items.append({"title": raw.strip(), "source": "spec"})

    if connection is None:
        return items

    try:
        with connection.begin_nested():
            rows = connection.execute(
                text(
                    "SELECT id, name, summary, properties "
                    "FROM knowledge_nodes "
                    "WHERE project_id = :project_id AND layer = 'learning' "
                    "  AND COALESCE(status, 'active') NOT IN ('forgotten', 'rejected', 'superseded') "
                    "ORDER BY updated_at DESC LIMIT 12"
                ),
                {"project_id": project_id},
            ).mappings().all()
        q_tokens = {tok for tok in query.lower().split() if len(tok) >= 3}
        scored: list[tuple[int, dict[str, Any]]] = []
        for r in rows:
            blob = f"{r.get('name') or ''} {r.get('summary') or ''}".lower()
            score = sum(1 for t in q_tokens if t in blob)
            scored.append(
                (
                    score,
                    {
                        "id": str(r["id"]),
                        "title": str(r.get("name") or ""),
                        "summary": str(r.get("summary") or "")[:600],
                        "source": "knowledge_nodes",
                    },
                )
            )
        scored.sort(key=lambda pair: pair[0], reverse=True)
        for _, entry in scored[:8]:
            items.append(entry)
    except Exception:  # noqa: BLE001
        log.debug("quick_lane.context: learning lookup skipped", exc_info=True)
    return items


def _path_matches_scope(touched_paths: list[str], scopes: list[str]) -> bool:
    if not scopes or not touched_paths:
        return True
    for p in touched_paths:
        norm = p.replace("\\", "/")
        for s in scopes:
            s_norm = s.strip().replace("\\", "/").rstrip("*")
            if not s_norm or s_norm in norm or norm.startswith(s_norm):
                return True
    return False


def _load_scoped_standards(
    connection: Optional[Connection],
    project_id: str,
    spec: dict[str, Any],
    touched_paths: list[str],
    budget_chars: int = DEFAULT_STANDARDS_BUDGET_CHARS,
) -> tuple[list[dict[str, Any]], list[str]]:
    candidates: list[dict[str, Any]] = []
    for raw in spec.get("standards") or []:
        if isinstance(raw, dict):
            candidates.append(raw)
        elif isinstance(raw, str) and raw.strip():
            candidates.append({"key": "spec_standard", "body": raw.strip(), "scopes": []})

    if connection is not None:
        try:
            with connection.begin_nested():
                rows = connection.execute(
                    text(
                        "SELECT id, key, title, body, metadata "
                        "FROM platform_standards "
                        "WHERE (project_id = :project_id OR project_id IS NULL) "
                        "  AND COALESCE(enabled, true) = true "
                        "ORDER BY id ASC LIMIT 30"
                    ),
                    {"project_id": project_id},
                ).mappings().all()
            for r in rows:
                meta = _as_dict(r.get("metadata"))
                scopes = list(meta.get("paths") or meta.get("scopes") or [])
                candidates.append(
                    {
                        "key": str(r.get("key") or r["id"]),
                        "title": str(r.get("title") or r.get("key") or ""),
                        "body": str(r.get("body") or ""),
                        "scopes": scopes,
                    }
                )
        except Exception:  # noqa: BLE001
            log.debug("quick_lane.context: platform_standards lookup skipped", exc_info=True)

    # Always include the principal-engineer quick-lane floor standards when none were loaded.
    if not candidates:
        candidates = [
            {
                "key": "pe.reproduce_first",
                "title": "Reproduce before editing",
                "body": "Reproduce the failure on the branch head and name file:line causes before editing.",
                "scopes": [],
            },
            {
                "key": "pe.existing_mechanism",
                "title": "Check existing mechanisms",
                "body": "Search the codebase and KG for an existing mechanism before adding a new one.",
                "scopes": [],
            },
            {
                "key": "pe.real_data_shapes",
                "title": "Real data shapes in tests",
                "body": "Use the real data shapes produced by the system; never invent synthetic fixtures.",
                "scopes": [],
            },
        ]

    included: list[dict[str, Any]] = []
    omitted: list[str] = []
    used_chars = 0
    for item in candidates:
        key = str(item.get("key") or item.get("title") or "standard")
        scopes = [str(x) for x in (item.get("scopes") or []) if str(x).strip()]
        if not _path_matches_scope(touched_paths, scopes):
            omitted.append(f"{key} (out of path scope)")
            continue
        body = str(item.get("body") or item.get("text") or "")
        cost = len(body) + len(str(item.get("title") or ""))
        if used_chars + cost > budget_chars and included:
            omitted.append(f"{key} (over {budget_chars}-char budget)")
            continue
        used_chars += cost
        included.append(
            {
                "key": key,
                "title": str(item.get("title") or key),
                "body": body,
                "scopes": scopes,
            }
        )
    return included, omitted


def _extract_request_evidence(spec: dict[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key in (
        "repro",
        "reproduction",
        "evidence",
        "failing_run_id",
        "run_ids",
        "error",
        "errors",
        "logs",
        "stack_trace",
        "failing_test",
        "ticket_key",
        "reference_links",
    ):
        val = spec.get(key)
        if val not in (None, "", [], {}):
            evidence[key] = val
    return evidence


def load_full_context(
    connection: Optional[Connection],
    *,
    project_id: str,
    spec: dict[str, Any],
    touched_paths: Optional[Iterable[str]] = None,
    symbols: Optional[Iterable[str]] = None,
    standards_budget_chars: int = DEFAULT_STANDARDS_BUDGET_CHARS,
) -> ContextBundle:
    """Load all five context sources before the quick builder makes its first edit."""
    paths = [str(p) for p in (touched_paths or spec.get("touched_paths") or []) if str(p).strip()]
    syms = [str(s) for s in (symbols or spec.get("symbols") or []) if str(s).strip()]
    area_hint = str(spec.get("area") or (paths[0].split("/")[0] if paths else "") or "")
    query = " ".join(
        str(spec.get(k) or "").strip()
        for k in ("title", "instructions", "area", "error")
        if str(spec.get(k) or "").strip()
    )[:300]

    study_notes = _load_study_notes(connection, project_id, spec, area_hint)
    kg = _load_kg_context(connection, project_id, spec, syms, query)
    learnings = _load_learnings(connection, project_id, spec, query)
    standards, omitted = _load_scoped_standards(
        connection, project_id, spec, paths, budget_chars=standards_budget_chars
    )
    request_evidence = _extract_request_evidence(spec)

    return ContextBundle(
        study_notes=study_notes,
        kg=kg,
        learnings=learnings,
        standards=standards,
        standards_omitted=omitted,
        request_evidence=request_evidence,
    )


def render_context_prompt(bundle: ContextBundle, spec: dict[str, Any]) -> str:
    """Render the builder prompt showing each context source used and the quality floor."""
    title = str(spec.get("title") or "Quick fix")
    instructions = str(spec.get("instructions") or spec.get("description") or "")
    sources = bundle.sources_used()

    lines = [
        f"# Quick Lane Task: {title}",
        "",
        "## Request",
        instructions or "(no instructions provided)",
        "",
        "## Context Sources Loaded",
    ]
    for name in REQUIRED_CONTEXT_SOURCES:
        meta = sources[name]
        status = f"loaded ({meta['count']})" if meta["loaded"] else "none found"
        refs = ", ".join(meta.get("refs") or [])
        lines.append(f"- **{name}**: {status}" + (f" [{refs}]" if refs else ""))

    if bundle.study_notes:
        lines.extend(["", "### Study Notes"])
        for note in bundle.study_notes:
            lines.append(f"- {note.get('area') or 'area'}: {note.get('summary') or json.dumps(note, default=str)[:400]}")

    if any(bundle.kg.get(k) for k in ("callers", "blast_radius", "precedent", "why")):
        lines.extend(["", "### Knowledge Graph (Callers, Blast Radius, Precedent, Why)"])
        if bundle.kg.get("callers"):
            lines.append(f"- Callers ({len(bundle.kg['callers'])}): " + json.dumps(bundle.kg["callers"][:15], default=str))
        if bundle.kg.get("blast_radius"):
            lines.append(f"- Blast radius ({len(bundle.kg['blast_radius'])}): " + json.dumps(bundle.kg["blast_radius"][:15], default=str))
        if bundle.kg.get("precedent"):
            lines.append(f"- Precedent ({len(bundle.kg['precedent'])}): " + json.dumps(bundle.kg["precedent"][:8], default=str))
        if bundle.kg.get("why"):
            lines.append(f"- Why ({len(bundle.kg['why'])}): " + json.dumps(bundle.kg["why"][:8], default=str))

    if bundle.learnings:
        lines.extend(["", "### Project Learnings & Past Corrections"])
        for item in bundle.learnings:
            lines.append(f"- {item.get('title')}: {item.get('summary') or ''}".rstrip(": "))

    if bundle.standards:
        lines.extend(["", "### Scoped Standards"])
        for std in bundle.standards:
            lines.append(f"- [{std.get('key')}] {std.get('title')}: {std.get('body')}")
        if bundle.standards_omitted:
            lines.append(f"- Omitted standards: {', '.join(bundle.standards_omitted)}")

    if bundle.request_evidence:
        lines.extend(["", "### Request Evidence"])
        lines.append(json.dumps(bundle.request_evidence, indent=2, default=str)[:2000])

    lines.extend(
        [
            "",
            "## Builder Floor (Required Before First Edit)",
            "1. Reproduce first: run a failing check and cite concrete file:line root causes.",
            "2. Check for an existing mechanism before introducing a new one.",
            "3. Inspect blast radius callers (kg_callers / kg_blast_radius) and test affected callers.",
            "4. Use real data shapes in tests; never invent synthetic fixtures.",
        ]
    )
    return "\n".join(lines)


def validate_reproduction(repro: Any) -> ReproductionRecord:
    """Enforce the reproduce-first gate before edits/PR proceed (BOSSAPP-796).

    Raises :class:`ReproductionRequiredError` if the builder did not reproduce the failure
    with a concrete failing command/output and at least one ``file:line`` root cause.
    """
    if isinstance(repro, ReproductionRecord):
        rec = repro
    elif isinstance(repro, dict):
        raw_causes = repro.get("causes") or []
        causes: list[CauseLocation] = []
        for item in raw_causes:
            if isinstance(item, CauseLocation):
                causes.append(item)
            elif isinstance(item, dict):
                f = str(item.get("file") or "").strip()
                ln = item.get("line")
                exp = str(item.get("explanation") or item.get("why") or "").strip()
                if f and isinstance(ln, int) and ln > 0:
                    causes.append(CauseLocation(file=f, line=ln, explanation=exp))
        rec = ReproductionRecord(
            command=str(repro.get("command") or "").strip(),
            exit_code=int(repro.get("exit_code") if repro.get("exit_code") is not None else 0),
            output=str(repro.get("output") or "").strip(),
            causes=tuple(causes),
            existing_mechanism_checked=bool(repro.get("existing_mechanism_checked", False)),
            existing_mechanism_note=str(repro.get("existing_mechanism_note") or "").strip(),
            blast_radius_checked=bool(repro.get("blast_radius_checked", False)),
            checked_callers=tuple(str(c) for c in (repro.get("checked_callers") or []) if str(c).strip()),
            real_data_shape_source=str(repro.get("real_data_shape_source") or "").strip(),
        )
    else:
        raise ReproductionRequiredError(
            "A quick-lane fix cannot proceed without a recorded reproduction."
        )

    if not rec.command or not rec.output:
        raise ReproductionRequiredError(
            "Reproduction must record the failing command and failing output before editing."
        )
    if rec.exit_code == 0:
        raise ReproductionRequiredError(
            "Reproduction command exited 0; a fix requires a failing reproduction before the change."
        )
    if not rec.causes:
        raise ReproductionRequiredError(
            "Reproduction must name at least one root cause with file:line before editing."
        )
    return rec
