"""PE-Phase 2 (BOSSAPP-790): Clean PR pipeline (§3E) and planning (§3G).

Clean PR (§3E):
  - Risk rating from blast radius, tier, touched surfaces, and test evidence (with explicit
    review-closeness guidance; low-risk PRs say why).
  - Evidence per acceptance criterion (screenshot/video for UI, test/log output for backend).
  - Docs pass detecting invalidated docs referencing changed symbols/commands/settings.
  - PR babysitter state machine (rebase on base move/conflict, fix round on CI failure, respond
    to human comments, bounded by run budget).

Planning (§3G):
  - Alternatives + standalone Option Cards for Open Questions (cannot be silently skipped by
    auto-approval).
  - Visual plan HTML + Mermaid diagram validation + repo-grounded architecture summary.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

SENSITIVE_PATH_MARKERS = (
    "auth", "identity", "secret", "billing", "payment", "budget", "migration",
    "schema.sql", "deploy", "sandbox", "policy", "workflow", "security",
)


def assess_pr_risk(
    paths: Sequence[str],
    *,
    tier: int = 1,
    blast_radius: Optional[Mapping[str, Any]] = None,
    test_evidence: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Compute structured PR risk rating + 'how closely to review' guidance (spec §3E)."""
    clean_paths = sorted({str(p).strip() for p in (paths or ()) if str(p).strip()})
    br = dict(blast_radius or {})
    callers = list(br.get("callers") or [])
    flows = list(br.get("flows") or [])
    sensitive_hits = [
        p for p in clean_paths if any(m in p.lower() for m in SENSITIVE_PATH_MARKERS)
    ]
    ev = dict(test_evidence or {})
    tests_passed = ev.get("tests_passed")

    reasons: list[str] = []
    score = 0
    if int(tier) >= 3 or sensitive_hits:
        score += 3
        if sensitive_hits:
            reasons.append(f"touches sensitive surface(s): {', '.join(sensitive_hits[:4])}")
    elif int(tier) == 2:
        score += 2
        reasons.append("tier 2 change")
    else:
        reasons.append(f"tier {int(tier)} scoped change")

    if len(callers) >= 5 or len(flows) >= 3 or str(br.get("risk_level") or "").lower() in ("high", "critical"):
        score += 2
        reasons.append(f"wide blast radius ({len(callers)} callers, {len(flows)} flows)")
    elif callers or flows:
        score += 1
        reasons.append(f"bounded blast radius ({len(callers)} callers, {len(flows)} flows)")
    else:
        reasons.append("isolated surface with no external callers affected")

    if tests_passed is False:
        score += 2
        reasons.append("verification suite has failing tests")
    elif tests_passed is True:
        reasons.append("automated verification passed")

    if score >= 5:
        level = "critical"
        guidance = "Line-by-line senior review required; verify rollback plan and security boundaries before merge."
    elif score >= 3:
        level = "high"
        guidance = "Careful review required; inspect caller contracts, edge cases, and failure paths."
    elif score >= 2:
        level = "medium"
        guidance = "Standard review; verify acceptance evidence and changed logic."
    else:
        level = "low"
        guidance = "Quick scan sufficient: " + "; ".join(reasons) + "."

    return {
        "level": level,
        "score": score,
        "reasons": reasons,
        "sensitive_paths": sensitive_hits,
        "review_guidance": guidance,
    }


def render_risk_section(risk: Mapping[str, Any] | None) -> str:
    if not isinstance(risk, Mapping) or not risk.get("level"):
        return ""
    lines = [
        "### Risk rating",
        f"- **Level**: `{str(risk['level']).upper()}`",
        f"- **How closely to review**: {risk.get('review_guidance') or ''}",
    ]
    reasons = [str(r) for r in (risk.get("reasons") or []) if str(r).strip()]
    if reasons:
        lines.append("- **Why**: " + "; ".join(reasons))
    return "\n".join(lines)


def render_criterion_evidence(criteria_evidence: Sequence[Mapping[str, Any]]) -> str:
    """Render per-acceptance-criterion proof table for PR descriptions (spec §3E)."""
    items = [dict(c) for c in (criteria_evidence or ()) if isinstance(c, Mapping)]
    if not items:
        return ""
    lines = ["### Acceptance evidence"]
    for item in items:
        crit = str(item.get("criterion") or item.get("name") or "").strip()
        kind = str(item.get("kind") or "test").strip()
        proof = str(item.get("proof") or item.get("ref") or "").strip()
        passed = item.get("passed", True)
        mark = "✅" if passed else "❌"
        if crit:
            lines.append(f"- {mark} **{crit}** ({kind}): {proof or 'missing proof'}")
    return "\n".join(lines) if len(lines) > 1 else ""


def find_invalidated_docs(
    root: Path | str,
    changed_symbols: Sequence[str],
    *,
    doc_dirs: Sequence[str] = ("docs", "README.md"),
) -> list[dict[str, Any]]:
    """Docs pass (§3E): locate docs referencing changed/renamed symbols, commands, or settings."""
    base = Path(root)
    needles = [str(s).strip() for s in (changed_symbols or ()) if len(str(s).strip()) >= 3]
    if not needles:
        return []
    hits: list[dict[str, Any]] = []
    candidates: list[Path] = []
    for entry in doc_dirs:
        target = base / entry
        if target.is_file():
            candidates.append(target)
        elif target.is_dir():
            for dirpath, _dirnames, filenames in os.walk(target):
                for fn in filenames:
                    if fn.endswith((".md", ".mdx", ".rst", ".txt")):
                        candidates.append(Path(dirpath) / fn)
    for path in sorted(set(candidates)):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matched = [n for n in needles if n in content]
        if matched:
            hits.append({
                "path": path.relative_to(base).as_posix(),
                "symbols": matched,
            })
    return hits


def plan_pr_babysit_action(
    *,
    ci_status: str,
    base_moved: bool = False,
    has_conflicts: bool = False,
    human_comments: Sequence[Mapping[str, Any]] = (),
    budget_remaining_usd: float = 1.0,
) -> dict[str, Any]:
    """Deterministic PR babysitter action planner (§3E): handle conflicts, CI failures, and comments until merge."""
    if float(budget_remaining_usd) <= 0.0:
        return {
            "action": "stop_budget",
            "reason": "PR babysitting budget exhausted; escalate to owner.",
        }
    if has_conflicts or base_moved:
        return {
            "action": "rebase_and_resolve",
            "reason": (
                "PR has merge conflicts with updated base branch; rebase onto latest base and re-run gates."
                if has_conflicts
                else "Base branch moved; rebase onto latest base before review/merge."
            ),
        }
    status = str(ci_status or "").strip().lower()
    if status in ("failed", "failure", "error"):
        return {
            "action": "start_fix_round",
            "reason": "CI check failed; launch bounded repair round from failing check evidence.",
        }
    unresolved = [c for c in (human_comments or ()) if not c.get("resolved")]
    if unresolved:
        return {
            "action": "address_human_comments",
            "count": len(unresolved),
            "reason": f"{len(unresolved)} unresolved human review comment(s); answer or fix.",
        }
    if status in ("pending", "running", "queued"):
        return {"action": "wait_ci", "reason": "CI checks still running."}
    return {"action": "ready_for_merge", "reason": "Rebased, CI green, and all review comments resolved."}


_MERMAID_HEADERS = (
    "flowchart", "graph", "sequenceDiagram", "stateDiagram", "classDiagram", "erDiagram", "xychart-beta",
)


def validate_mermaid(diagram: str) -> tuple[bool, str]:
    text_clean = str(diagram or "").strip()
    if not text_clean:
        return False, "empty mermaid diagram"
    first = text_clean.splitlines()[0].strip()
    if not any(first.startswith(h) for h in _MERMAID_HEADERS):
        return False, f"unsupported mermaid diagram header: {first!r}"
    return True, ""


def build_planning_packet(
    *,
    title: str,
    repo_context: Mapping[str, Any],
    alternatives: Sequence[Mapping[str, Any]],
    open_questions: Sequence[Mapping[str, Any]] = (),
    mermaid_diagram: str = "",
    visual_plan_html: str = "",
) -> dict[str, Any]:
    """Build a §3G reviewable planning packet with alternatives, standalone option cards, and visual plan."""
    alts = [dict(a) for a in (alternatives or ()) if isinstance(a, Mapping)]
    if len(alts) < 2:
        raise ValueError("planning packet must compare at least 2 alternatives")
    option_cards: list[dict[str, Any]] = []
    for idx, q in enumerate(open_questions or (), start=1):
        opts = [str(o) for o in (q.get("options") or []) if str(o).strip()]
        option_cards.append({
            "card_id": f"opt_{idx}",
            "question": str(q.get("question") or "").strip(),
            "options": opts,
            "recommended": str(q.get("recommended") or (opts[0] if opts else "")),
            "auto_approvable": False,  # standalone option cards cannot be silently skipped!
        })
    mermaid_ok, mermaid_err = validate_mermaid(mermaid_diagram) if mermaid_diagram else (True, "")
    return {
        "title": str(title).strip(),
        "repo_context": dict(repo_context or {}),
        "alternatives": alts,
        "option_cards": option_cards,
        "mermaid_diagram": mermaid_diagram.strip(),
        "mermaid_valid": mermaid_ok,
        "mermaid_error": mermaid_err,
        "visual_plan_html": visual_plan_html,
    }


__all__ = [
    "assess_pr_risk",
    "build_planning_packet",
    "find_invalidated_docs",
    "plan_pr_babysit_action",
    "render_criterion_evidence",
    "render_risk_section",
    "validate_mermaid",
]
