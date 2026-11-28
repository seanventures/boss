"""PE-A (BOSSAPP-784): Study before change — structured StudyRecord, red-first gate, and PR claim verification.

Before changing existing code or fixing a bug, BOSS builds a structured StudyRecord naming:
  - symptoms
  - reproductions (commands + observed results)
  - causes (anchored to ``file:line``, with whether each was reproduced)
  - existing_mechanisms (code/services already doing related work)
  - classification (app_bug | boss_bug | host_env | requirement_change | refactor)
  - blast_radius (callers, flows, tests, risk_level)
  - alternatives (options weighed, why one was chosen)

Bug fixes enforce a red-first failing reproduction before implementation, and PR claims are linked
back to StudyRecord entries so any unbacked claim is explicitly tagged ``unverified``.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

VALID_CLASSIFICATIONS = frozenset({
    "app_bug",
    "boss_bug",
    "host_env",
    "requirement_change",
    "refactor",
})

_FILE_LINE_RE = re.compile(r"^[^:\s]+:\d+(?:-\d+)?$")
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_./:-]{3,}")


def empty_study_record() -> dict[str, Any]:
    return {
        "symptoms": [],
        "reproductions": [],
        "causes": [],
        "existing_mechanisms": [],
        "classification": "",
        "blast_radius": {"callers": [], "flows": [], "tests": [], "risk_level": "medium"},
        "alternatives": [],
    }


def normalize_study_record(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(raw or {})
    symptoms = [str(s).strip() for s in (data.get("symptoms") or []) if str(s).strip()]
    reproductions: list[dict[str, Any]] = []
    for item in data.get("reproductions") or []:
        if isinstance(item, Mapping):
            reproductions.append({
                "command": str(item.get("command") or "").strip(),
                "observed": str(item.get("observed") or "").strip(),
                "passed": bool(item.get("passed", False)),
                **({"cause_id": str(item["cause_id"])} if item.get("cause_id") else {}),
            })
        elif str(item).strip():
            reproductions.append({"command": str(item).strip(), "observed": "", "passed": False})

    causes: list[dict[str, Any]] = []
    for idx, item in enumerate(data.get("causes") or [], start=1):
        if isinstance(item, Mapping):
            cid = str(item.get("id") or f"C{idx}").strip()
            causes.append({
                "id": cid,
                "location": str(item.get("location") or "").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "reproduced": bool(item.get("reproduced", False)),
            })
        elif str(item).strip():
            causes.append({"id": f"C{idx}", "location": "", "summary": str(item).strip(), "reproduced": False})

    mechanisms = [str(m).strip() for m in (data.get("existing_mechanisms") or []) if str(m).strip()]
    classification = str(data.get("classification") or "").strip().lower()
    raw_br = data.get("blast_radius") if isinstance(data.get("blast_radius"), Mapping) else {}
    blast_radius = {
        "callers": [str(c).strip() for c in (raw_br.get("callers") or []) if str(c).strip()],
        "flows": [str(f).strip() for f in (raw_br.get("flows") or []) if str(f).strip()],
        "tests": [str(t).strip() for t in (raw_br.get("tests") or []) if str(t).strip()],
        "risk_level": str(raw_br.get("risk_level") or "medium").strip().lower(),
    }

    alternatives: list[dict[str, Any]] = []
    for item in data.get("alternatives") or []:
        if isinstance(item, Mapping):
            alternatives.append({
                "option": str(item.get("option") or "").strip(),
                "pros": str(item.get("pros") or "").strip(),
                "cons": str(item.get("cons") or "").strip(),
                "chosen": bool(item.get("chosen", False)),
            })
        elif str(item).strip():
            alternatives.append({"option": str(item).strip(), "pros": "", "cons": "", "chosen": False})

    return {
        "symptoms": symptoms,
        "reproductions": reproductions,
        "causes": causes,
        "existing_mechanisms": mechanisms,
        "classification": classification,
        "blast_radius": blast_radius,
        "alternatives": alternatives,
    }


def validate_study_record(
    record: Mapping[str, Any] | None,
    *,
    is_fix: bool = False,
    require_red_first: bool = True,
) -> list[str]:
    """Return a list of concrete gaps in a StudyRecord (empty list == valid)."""
    rec = normalize_study_record(record)
    gaps: list[str] = []

    if not rec["symptoms"]:
        gaps.append("missing symptoms")
    if rec["classification"] not in VALID_CLASSIFICATIONS:
        gaps.append(
            f"classification must be one of {sorted(VALID_CLASSIFICATIONS)}, got {rec['classification']!r}"
        )
    if not rec["causes"]:
        gaps.append("missing causes (each cause needs file:line and summary)")
    else:
        for cause in rec["causes"]:
            loc = cause.get("location") or ""
            if not _FILE_LINE_RE.match(loc):
                gaps.append(f"cause {cause['id']} location {loc!r} must be anchored as file:line")
            if not cause.get("summary"):
                gaps.append(f"cause {cause['id']} is missing summary")
            if is_fix and not cause.get("reproduced"):
                gaps.append(f"cause {cause['id']} ({loc}) has not been reproduced")

    if not rec["existing_mechanisms"]:
        gaps.append("missing existing_mechanisms (check what already exists before writing new code)")

    if not rec["alternatives"]:
        gaps.append("missing alternatives weighed")

    br = rec["blast_radius"]
    if not (br["callers"] or br["flows"] or br["tests"]):
        gaps.append("missing blast_radius (callers, flows, or tests)")

    if is_fix and require_red_first:
        ok_red, red_gaps = enforce_red_first(rec, is_fix=True)
        if not ok_red:
            gaps.extend(red_gaps)

    return gaps


def enforce_red_first(
    record: Mapping[str, Any] | None,
    *,
    is_fix: bool = True,
) -> tuple[bool, list[str]]:
    """Enforce that every bug fix has a failing (red) reproduction recorded before implementation."""
    if not is_fix:
        return True, []
    rec = normalize_study_record(record)
    repros = rec["reproductions"]
    if not repros:
        return False, ["red-first gate: bug fix requires at least one recorded reproduction before code changes"]
    red_repros = [r for r in repros if not r.get("passed") and (r.get("command") or r.get("observed"))]
    if not red_repros:
        return False, ["red-first gate: no failing (red) reproduction recorded before fix"]
    return True, []


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(str(text or "")) if len(t) >= 3}


def link_pr_claims(
    claims: Sequence[str | Mapping[str, Any]],
    study_record: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Link each PR claim to StudyRecord entries; unlinked claims are explicitly marked unverified."""
    rec = normalize_study_record(study_record)
    evidence_index: dict[str, set[str]] = {}
    for cause in rec["causes"]:
        cid = cause["id"]
        evidence_index[cid] = _tokens(f"{cause.get('location', '')} {cause.get('summary', '')}")
    for idx, repro in enumerate(rec["reproductions"], start=1):
        rid = str(repro.get("cause_id") or f"R{idx}")
        evidence_index.setdefault(rid, set()).update(
            _tokens(f"{repro.get('command', '')} {repro.get('observed', '')}")
        )
    for idx, mech in enumerate(rec["existing_mechanisms"], start=1):
        evidence_index[f"M{idx}"] = _tokens(mech)

    linked: list[dict[str, Any]] = []
    for raw_claim in claims:
        if isinstance(raw_claim, Mapping):
            text = str(raw_claim.get("text") or raw_claim.get("claim") or "").strip()
            explicit_ref = str(raw_claim.get("study_ref") or "").strip()
        else:
            text = str(raw_claim or "").strip()
            explicit_ref = ""
        if not text:
            continue

        matched_refs: list[str] = []
        if explicit_ref and explicit_ref in evidence_index:
            matched_refs.append(explicit_ref)
        else:
            ctoks = _tokens(text)
            for ref_id, etoks in evidence_index.items():
                if not etoks:
                    continue
                # Match either explicit file:line mention or >= 2 meaningful shared tokens
                overlap = ctoks & etoks
                has_loc = any(":" in tok and "." in tok for tok in overlap)
                if has_loc or len(overlap) >= 2:
                    matched_refs.append(ref_id)

        verified = bool(matched_refs)
        linked.append({
            "claim": text,
            "study_refs": matched_refs,
            "verified": verified,
            "status": "verified" if verified else "unverified",
        })
    return linked


def render_study_section(
    study_record: Mapping[str, Any] | None,
    *,
    claims: Sequence[str | Mapping[str, Any]] = (),
) -> str:
    """Render a compact Markdown Study section for a PR description."""
    rec = normalize_study_record(study_record)
    if not any([rec["symptoms"], rec["causes"], rec["reproductions"]]):
        return ""
    lines = ["### Study record"]
    if rec["classification"]:
        lines.append(f"- **Classification**: `{rec['classification']}`")
    if rec["symptoms"]:
        lines.append("- **Symptoms**: " + "; ".join(rec["symptoms"][:5]))
    if rec["causes"]:
        lines.append("- **Root causes**:")
        for c in rec["causes"]:
            badge = "reproduced" if c.get("reproduced") else "not reproduced"
            loc = f"`{c['location']}`" if c.get("location") else "`unknown`"
            lines.append(f"  - `{c['id']}` ({loc}, {badge}): {c['summary']}")
    if rec["reproductions"]:
        lines.append("- **Reproductions**:")
        for r in rec["reproductions"]:
            state = "passed" if r.get("passed") else "failed (red)"
            lines.append(f"  - `{r['command']}` -> {state}: {r['observed']}")
    br = rec["blast_radius"]
    if br["callers"] or br["flows"] or br["tests"]:
        parts = []
        if br["callers"]:
            parts.append("callers: " + ", ".join(f"`{c}`" for c in br["callers"][:6]))
        if br["flows"]:
            parts.append("flows: " + ", ".join(br["flows"][:6]))
        if br["tests"]:
            parts.append("tests: " + ", ".join(f"`{t}`" for t in br["tests"][:6]))
        lines.append(f"- **Blast radius** (`{br['risk_level']}`): " + " | ".join(parts))
    if claims:
        checked = link_pr_claims(claims, rec)
        lines.append("- **Claim verification**:")
        for item in checked:
            if item["verified"]:
                refs = ", ".join(item["study_refs"])
                lines.append(f"  - [verified: {refs}] {item['claim']}")
            else:
                lines.append(f"  - [unverified] {item['claim']}")
    return "\n".join(lines)


def _extract_json_block(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    candidate = fenced.group(1) if fenced else raw
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(candidate[start : end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def run_study_stage(
    adapter: Any,
    *,
    task_title: str,
    task_instructions: str,
    changed_paths: Sequence[str] = (),
    kg_context: Mapping[str, Any] | None = None,
    is_fix: bool = False,
) -> dict[str, Any]:
    """Execute the Study stage via a chat/scripted adapter and return the normalized + validated record."""
    kg_json = json.dumps(dict(kg_context or {}), sort_keys=True)
    paths_str = ", ".join(changed_paths) or "(existing codebase)"
    prompt = (
        "You are running the BOSS Study stage BEFORE writing any code.\n"
        f"TASK: {task_title}\n"
        f"INSTRUCTIONS: {task_instructions}\n"
        f"PATHS: {paths_str}\n"
        f"IS_FIX: {str(bool(is_fix)).lower()}\n"
        f"KG_CONTEXT: {kg_json}\n\n"
        "Return a single JSON object with keys: symptoms, reproductions, causes (each with id, "
        "location as file:line, summary, reproduced), existing_mechanisms, classification, "
        "blast_radius (callers, flows, tests, risk_level), alternatives."
    )
    if hasattr(adapter, "create_text_response"):
        _, response_text, _ = adapter.create_text_response(prompt)
    elif hasattr(adapter, "complete"):
        response_text = str(adapter.complete(prompt))
    elif callable(adapter):
        response_text = str(adapter(prompt))
    else:
        raise TypeError("adapter must provide create_text_response(), complete(), or be callable")

    parsed = _extract_json_block(response_text)
    record = normalize_study_record(parsed)
    gaps = validate_study_record(record, is_fix=is_fix)
    return {"record": record, "valid": not gaps, "gaps": gaps}


__all__ = [
    "VALID_CLASSIFICATIONS",
    "empty_study_record",
    "enforce_red_first",
    "link_pr_claims",
    "normalize_study_record",
    "render_study_section",
    "run_study_stage",
    "validate_study_record",
]
