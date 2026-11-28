"""PE-L (BOSSAPP-792): Done contracts per output kind, tool enforcement, fresh-context completion check.

Versioned checkable done contracts for each BOSS output kind:
  - ``ticket_set`` — 2+ related tickets require a shared parent/epic, explicit ordering, and spec links
  - ``pr``         — non-empty title, summary, and concrete evidence section (checks/tests/replay)
  - ``fix``        — reproduced cause, red-first test, green verification, blast-radius check
  - ``decision``   — clear question, recommendation, reversibility, evidence
  - ``doc``        — title, purpose/scope, grounded references
  - ``deploy``     — target, rollback plan, health verification

Tools call ``enforce_ticket_set_contract`` and ``enforce_pr_contract`` directly so malformed outputs
are refused at the tool boundary. Before reporting done, ``run_completion_check`` runs a fresh-context
pass returning concrete gaps back to the producing agent.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional, Sequence

CONTRACT_VERSION = "1.0"

DONE_CONTRACTS: dict[str, dict[str, Any]] = {
    "ticket_set": {
        "version": CONTRACT_VERSION,
        "required_rules": [
            "two_or_more_tickets_require_parent",
            "explicit_ordering_or_dependencies",
            "linked_to_spec_or_source",
        ],
    },
    "pr": {
        "version": CONTRACT_VERSION,
        "required_rules": [
            "non_empty_title",
            "evidence_section_present",
            "concrete_verification_lines",
        ],
    },
    "fix": {
        "version": CONTRACT_VERSION,
        "required_rules": [
            "reproduced_root_cause",
            "red_first_test_added",
            "verification_passed",
            "blast_radius_checked",
        ],
    },
    "decision": {
        "version": CONTRACT_VERSION,
        "required_rules": [
            "clear_question",
            "recommendation_present",
            "reversibility_declared",
            "evidence_cited",
        ],
    },
    "doc": {
        "version": CONTRACT_VERSION,
        "required_rules": [
            "title_present",
            "scope_or_purpose_present",
            "grounded_references",
        ],
    },
    "deploy": {
        "version": CONTRACT_VERSION,
        "required_rules": [
            "target_declared",
            "rollback_plan_present",
            "health_check_declared",
        ],
    },
}

_EVIDENCE_HEADING_RE = re.compile(
    r"(?im)^#{1,4}\s+(?:evidence|verification|checks|test(?:s|\s+results)?|replay\s+outcomes|validation)\b"
)


class DoneContractError(ValueError):
    """Raised when a tool output violates its done contract."""

    def __init__(self, output_kind: str, gaps: Sequence[str]) -> None:
        self.output_kind = output_kind
        self.gaps = list(gaps)
        super().__init__(f"{output_kind} done contract failed: " + "; ".join(self.gaps))


def enforce_ticket_set_contract(
    tickets: Sequence[Mapping[str, Any]],
) -> tuple[bool, list[str]]:
    """Refuse 2+ related tickets without a parent, ordering, or spec link."""
    items = [dict(t) for t in (tickets or ()) if isinstance(t, Mapping)]
    gaps: list[str] = []
    if not items:
        return False, ["ticket_set is empty"]

    if len(items) >= 2:
        missing_parent = [
            str(t.get("title") or t.get("key") or f"#{i}")
            for i, t in enumerate(items, start=1)
            if not (t.get("parent_id") or t.get("epic_id") or t.get("parent"))
        ]
        if missing_parent:
            gaps.append(
                f"2+ related tickets require a parent/epic; missing parent on: {', '.join(missing_parent)}"
            )
        has_ordering = all(
            t.get("order") is not None or t.get("depends_on") is not None or t.get("sequence") is not None
            for t in items
        )
        if not has_ordering:
            gaps.append("ticket_set with 2+ tickets must declare explicit order or depends_on")

    missing_links = [
        str(t.get("title") or t.get("key") or f"#{i}")
        for i, t in enumerate(items, start=1)
        if not (t.get("spec_link") or t.get("links") or t.get("spec"))
    ]
    if missing_links:
        gaps.append(f"tickets missing spec/reference link: {', '.join(missing_links)}")

    return not gaps, gaps


def enforce_pr_contract(
    title: str,
    body: str,
    *,
    evidence: Optional[Mapping[str, Any]] = None,
) -> tuple[bool, list[str]]:
    """Refuse a PR that lacks a non-empty evidence section."""
    gaps: list[str] = []
    if not str(title or "").strip():
        gaps.append("PR title is empty")

    text_body = str(body or "").strip()
    has_heading = bool(_EVIDENCE_HEADING_RE.search(text_body))
    has_evidence_dict = bool(
        isinstance(evidence, Mapping)
        and any(evidence.get(k) for k in ("checks", "tests", "replay_outcomes", "report", "verification"))
    )

    if not has_heading and not has_evidence_dict:
        gaps.append("PR body is missing an Evidence / Verification section")
    elif has_heading:
        m = _EVIDENCE_HEADING_RE.search(text_body)
        assert m is not None
        tail = text_body[m.end() :].strip()
        # Check non-empty content before next top-level heading
        next_h = re.search(r"(?m)^#{1,4}\s+", tail)
        section_body = tail[: next_h.start()].strip() if next_h else tail
        if len(section_body) < 12:
            gaps.append("PR Evidence section is empty or lacks concrete verification details")

    return not gaps, gaps


def check_done_contract(
    output_kind: str,
    output: Mapping[str, Any],
    *,
    extra_checks: Sequence[Mapping[str, Any]] = (),
) -> tuple[bool, list[str]]:
    """Deterministically check an output against its versioned done contract."""
    kind = str(output_kind or "").strip().lower()
    if kind not in DONE_CONTRACTS:
        return False, [f"unknown output_kind {kind!r}; expected one of {sorted(DONE_CONTRACTS)}"]

    gaps: list[str] = []
    if kind == "ticket_set":
        _, gaps = enforce_ticket_set_contract(output.get("tickets") or ())
    elif kind == "pr":
        _, gaps = enforce_pr_contract(
            str(output.get("title") or ""),
            str(output.get("body") or ""),
            evidence=output.get("evidence") if isinstance(output.get("evidence"), Mapping) else None,
        )
    elif kind == "fix":
        if not output.get("root_cause"):
            gaps.append("fix missing reproduced root_cause")
        if not output.get("red_first_test"):
            gaps.append("fix missing red_first_test")
        if not output.get("verification_passed"):
            gaps.append("fix missing verification_passed=True")
        if not output.get("blast_radius_checked"):
            gaps.append("fix missing blast_radius_checked=True")
    elif kind == "decision":
        if not str(output.get("question") or "").strip():
            gaps.append("decision missing clear question")
        if not str(output.get("recommendation") or "").strip():
            gaps.append("decision missing recommendation")
        if output.get("reversible") is None:
            gaps.append("decision must declare reversible (true/false)")
        if not output.get("evidence"):
            gaps.append("decision missing supporting evidence")
    elif kind == "doc":
        if not str(output.get("title") or "").strip():
            gaps.append("doc missing title")
        if not str(output.get("purpose") or output.get("body") or "").strip():
            gaps.append("doc missing scope/purpose content")
        if not output.get("references"):
            gaps.append("doc missing grounded references")
    elif kind == "deploy":
        if not str(output.get("target") or "").strip():
            gaps.append("deploy missing target")
        if not str(output.get("rollback_plan") or "").strip():
            gaps.append("deploy missing rollback_plan")
        if not str(output.get("health_check") or "").strip():
            gaps.append("deploy missing health_check")

    for chk in extra_checks or ():
        req_field = str(chk.get("required_field") or "").strip()
        if req_field and not output.get(req_field):
            gaps.append(str(chk.get("message") or f"{kind} missing required field {req_field!r}"))

    return not gaps, gaps


def run_completion_check(
    output_kind: str,
    *,
    request: str,
    output: Mapping[str, Any],
    adapter: Any = None,
    extra_checks: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Fresh-context completion check: 'could the next person or agent finish this without asking?'.

    Runs deterministic contract checks first; if they pass and an adapter is supplied, runs a
    fresh-context model pass that can surface additional gaps to return to the producing agent.
    """
    ok, gaps = check_done_contract(output_kind, output, extra_checks=extra_checks)
    model_gaps: list[str] = []
    if ok and adapter is not None:
        prompt = (
            "Fresh-context completion check. Answer ONLY as JSON "
            '{"ready": true|false, "gaps": ["..."]}.\n'
            f"Question: Could the next person or agent finish this {output_kind} without asking?\n"
            f"REQUEST: {request}\n"
            f"CONTRACT: {json.dumps(DONE_CONTRACTS[output_kind])}\n"
            f"OUTPUT: {json.dumps(dict(output), default=str)[:8000]}\n"
        )
        if hasattr(adapter, "create_text_response"):
            _, text_out, _ = adapter.create_text_response(prompt)
        elif callable(adapter):
            text_out = str(adapter(prompt))
        else:
            text_out = ""
        try:
            start, end = text_out.find("{"), text_out.rfind("}")
            if start != -1 and end > start:
                parsed = json.loads(text_out[start : end + 1])
                if isinstance(parsed, Mapping) and not parsed.get("ready", True):
                    model_gaps = [str(g).strip() for g in (parsed.get("gaps") or []) if str(g).strip()]
        except ValueError:
            pass

    all_gaps = gaps + model_gaps
    ready = not all_gaps
    feedback = (
        ""
        if ready
        else f"Completion check refused {output_kind!r}; fix these gaps before reporting done: "
        + "; ".join(all_gaps)
    )
    return {
        "ok": ready,
        "output_kind": output_kind,
        "contract_version": DONE_CONTRACTS.get(output_kind, {}).get("version", CONTRACT_VERSION),
        "gaps": all_gaps,
        "actionable_feedback": feedback,
    }


def propose_contract_check_from_correction(
    correction_text: str,
    *,
    output_kind: str,
    required_field: str = "",
) -> dict[str, Any]:
    """Turn a human structural correction into a proposed versioned contract check (extends PE-B)."""
    kind = str(output_kind or "").strip().lower()
    if kind not in DONE_CONTRACTS:
        raise ValueError(f"unknown output_kind {kind!r}")
    text_clean = str(correction_text or "").strip()
    if not text_clean:
        raise ValueError("correction_text is required")
    inferred_field = required_field.strip() or re.sub(r"[^a-z0-9]+", "_", text_clean.lower()).strip("_")[:40]
    return {
        "output_kind": kind,
        "contract_version": CONTRACT_VERSION,
        "required_field": inferred_field,
        "message": text_clean,
        "status": "proposed",
    }


__all__ = [
    "CONTRACT_VERSION",
    "DONE_CONTRACTS",
    "DoneContractError",
    "check_done_contract",
    "enforce_pr_contract",
    "enforce_ticket_set_contract",
    "propose_contract_check_from_correction",
    "run_completion_check",
]
