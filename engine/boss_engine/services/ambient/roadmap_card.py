"""THE RECOMMENDATION CARD'S DIAGRAM (Roadmap, screen 07): evidence → detector → the agent who
would do it, each node a few words of THIS recommendation's own record — "2 incidents" and
"sla.py hot" → "Incident hotspot" → "Test + fix", "No CI on main" → "CI gap" → "Set up CI".

Never the provenance chain ("Code graph → Roadmap → Onboarding"): that is where the item came
from, not why it matters, and it read the same on every card. Provenance stays in Why ›.

Every label comes from the stored facts and evidence; a fact that is not there is a node that
is not drawn, never a filler word. Icons are Phosphor names the console draws."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import learning
from ._common import short_label

#: What the agent does, by the detector (the card's last node, ≤ 3 words).
DOES = {"incident_cause": "Test + fix", "hotspot": "Refactor", "missing_tests": "Add tests",
        "review_repeat": "Add a rule", "design_drift": "Write PRD", "cost_spike": "Cut cost",
        "flaky_test": "Fix flaky test", "slow_stage": "Speed it up", "user_friction": "Fix the flow",
        "feature_demand": "Write PRD", "alert_gap": "Add alert", "slow_query": "Migration"}

#: A recommendation with no detector of its own (a seeded or imported item): what its kind does.
KIND_DOES = {"hardening": "Test + fix", "test_gap": "Add tests", "refactor": "Refactor",
             "design_fix": "Fix design", "cost": "Cut cost", "process": "Improve it",
             "feature": "Build it"}

#: Onboarding's proposals by what found them (the candidate's `ref` prefix, or its signal's
#: kind): the detector's name and icon, and what the agent does.
ONBOARDING_FINDERS: dict[str, tuple[str, str, str]] = {
    "tests": ("Failing tests", "test-tube", "Fix tests"),
    "ci": ("Red CI", "warning", "Fix CI"),
    "issue": ("Open issue", "chats", "Fix issue"),
    "pr": ("Open PR", "git-pull-request", "Finish PR"),
    "deferred": ("Review follow-up", "magnifying-glass", "Fix findings"),
    "build": ("Unfinished build", "warning", "Finish it"),
    "alert": ("Firing alert", "bell-slash", "Fix cause"),
    "scan.no_ci": ("CI gap", "gauge", "Set up CI"),
    "scan.missing_tests": ("Test gap", "test-tube", "Add tests"),
    "scan.untested_hotspot": ("Test gap", "test-tube", "Add tests"),
    "scan.todo_cluster": ("TODO cluster", "fire", "Clear TODOs"),
    "scan.no_docs": ("Docs gap", "compass", "Write docs"),
    "osv.vulnerable_dependency": ("Vulnerable dependency", "warning", "Patch it"),
}


#: What found an onboarding proposal, by its source, when its ref names nothing more specific.
SOURCE_FINDERS: dict[str, tuple[str, str, str]] = {
    "ci": ONBOARDING_FINDERS["ci"], "tests": ONBOARDING_FINDERS["tests"],
    "bug": ("Open bug", "warning", "Fix bug"), "ticket": ("Open ticket", "chats", "Do ticket"),
    "boss": ("BOSS follow-up", "magnifying-glass", "Finish it"), "alert": ONBOARDING_FINDERS["alert"],
    "roadmap": ("Code scan", "magnifying-glass", "Do it"),
}

#: A kind's own name, for an item with no detector of its own (never a card of one bare node).
KIND_NAME = {"hardening": ("Hardening", "warning"), "test_gap": ("Test gap", "test-tube"),
             "refactor": ("Refactor", "fire"), "design_fix": ("Design fix", "compass"),
             "cost": ("Cost", "coins"), "process": ("Process", "gauge"),
             "feature": ("Feature", "trend-up")}


def _n(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _file(subject: str) -> str:
    head, _, tail = str(subject or "").partition(":")
    return tail.rsplit("/", 1)[-1] if head == "file" and tail else ""


def _chip(label: str, icon: str) -> dict[str, str]:
    return {"label": short_label(label, 4), "icon": icon}


def signal_kinds(connection: Connection, rows: list[Mapping[str, Any]]) -> dict[str, str]:
    """{signal id: kind} for every onboarding proposal that came from a scan signal, in one
    read (the card names "CI gap", "Test gap"… from it)."""
    ids = [str(r.get("subject") or "").split(":", 1)[1] for r in rows
           if r.get("detector") == "onboarding" and str(r.get("subject") or "").startswith("signal:")]
    if not ids:
        return {}
    return {str(r["id"]): str(r["kind"]) for r in connection.execute(text(
        "SELECT id, kind FROM signals WHERE id = ANY(:ids)"), {"ids": ids}).mappings()}


def _learning_evidence(rec: Mapping[str, Any]) -> list[dict[str, str]]:
    det = str(rec.get("detector") or "")
    facts = (rec.get("facts") or {}).get(det) or {}
    facts = facts if isinstance(facts, Mapping) else {}
    file = _file(str(rec.get("subject") or ""))
    hot = [_chip(f"{file} hot", "fire")] if file else []
    evidence = rec.get("evidence") or []
    incidents = _n(facts.get("incidents")) or sum(1 for e in evidence if e.get("kind") == "incident")
    out: list[dict[str, str]] = []
    if det == "incident_cause":
        out = ([_chip(_plural(incidents, "incident"), "warning")] if incidents else []) + hot
    elif det == "hotspot":
        out = ([_chip(_plural(_n(facts.get("prs")), "PR"), "git-pull-request")]
               if _n(facts.get("prs")) else []) + hot
    elif det == "missing_tests":
        out = ([_chip(f"{_plural(_n(facts.get('prs')), 'PR')} changed it", "git-pull-request")]
               if _n(facts.get("prs")) else []) + ([_chip(f"{file} untested", "test-tube")]
                                                   if file else [])
    elif det in ("review_repeat", "design_drift", "flaky_test"):
        builds = _n(facts.get("builds")) or _n(facts.get("seen_count"))
        out = [_chip(f"{_plural(builds, 'build')} flagged", "magnifying-glass")] if builds else []
    elif det == "cost_spike":
        now_, was = facts.get("median_now_usd"), facts.get("median_before_usd")
        out = [_chip(f"${now_}/run, was ${was}", "coins")] if now_ and was else []
    elif det == "slow_stage":
        stage = str(rec.get("subject") or "").partition(":")[2].replace("_", " ")
        med = facts.get("median_min")
        out = [_chip(f"{(stage or 'stage').capitalize()} {med} min", "timer")] if med else []
    elif det == "user_friction":
        n = _n(facts.get("count"))
        out = [_chip(_plural(n, "override"), "hand-pointing")] if n else []
    elif det == "alert_gap":
        app, env = str(facts.get("app") or ""), str(facts.get("environment") or "")
        out = [_chip(f"{app or 'App'} live{' on ' + env if env else ''}", "plug")]
        out += [_chip(_plural(incidents, "incident"), "warning")] if incidents else []
    if not out:
        # A detector this card does not know yet: its evidence, counted by source, in words.
        counts: dict[str, int] = {}
        for e in evidence:
            counts[str(e.get("kind") or "signal")] = counts.get(str(e.get("kind") or "signal"), 0) + 1
        out = [_chip(_plural(n, k.replace("_", " ")), "chart-line-up")
               for k, n in sorted(counts.items(), key=lambda kv: -kv[1])] + hot
    return out[:2]


def _onboarding(rec: Mapping[str, Any], kinds: Mapping[str, str]) -> tuple[
        list[dict[str, str]], Optional[tuple[str, str, str]]]:
    facts = rec.get("facts") or {}
    facts = facts if isinstance(facts, Mapping) else {}
    ref = str(facts.get("ref") or rec.get("subject") or "")
    head, _, tail = ref.partition(":")
    key = kinds.get(tail, "") if head == "signal" else head
    finder = ONBOARDING_FINDERS.get(key) or SOURCE_FINDERS.get(str(facts.get("source") or ""))
    why = str(facts.get("why_short") or "").strip()
    where = str(facts.get("where") or "").rsplit("/", 1)[-1]
    if key == "scan.untested_hotspot" and where:
        return [_chip(f"{where} hot", "fire"), _chip("No tests", "test-tube")], finder
    if key == "scan.no_ci":
        return [_chip("No CI on main", "warning")], finder
    if key == "scan.missing_tests":
        return [_chip("No tests found", "test-tube")], finder
    if key == "scan.no_docs":
        return [_chip("No README", "compass")], finder
    if key == "alert" and tail:
        return ([_chip(tail, "bell-slash")] + ([_chip(why, "warning")] if why else [])), finder
    # The candidate's own few words of why ("deferred on PR #3", "bug #12 · 5d open"), one
    # chip per part, never cut mid-phrase.
    parts = [p.strip() for p in why.split(" · ") if p.strip()]
    out = [_chip(p[:1].upper() + p[1:], "chart-line-up") for p in parts]
    if where:
        out.append(_chip(where, "fire"))
    return out[:2], finder


def card(rec: Mapping[str, Any], *, kinds: Optional[Mapping[str, str]] = None,
         build_usd: Optional[float] = None) -> dict[str, Any]:
    """{evidence: [{label, icon}] (≤ 2), detector: {label, icon} | None, does: str,
    cost_usd: float | None, cost_basis: str}."""
    det = str(rec.get("detector") or "")
    cost = round(_n(rec.get("estimated_cost_usd_micros")) / 1_000_000, 2) or None
    basis = "this item's estimate" if cost else ""
    if det == "onboarding":
        evidence, finder = _onboarding(rec, kinds or {})
        detector = {"label": finder[0], "icon": finder[1]} if finder else None
        does = finder[2] if finder else ("Write PRD" if rec.get("proposed_shape") == "prd"
                                         else KIND_DOES.get(str(rec.get("kind") or ""), "Do it"))
        if cost is None and build_usd:
            # Onboarding proposes before any estimate: what a ticket costs HERE (New work's
            # own estimate, from this project's past runs), said as such.
            cost, basis = round(float(build_usd), 2), "a ticket here, from past runs"
    else:
        known = learning.DETECTORS.get(det)
        detector = ({"label": short_label(known.title), "icon":
                     learning.DETECTOR_ICONS.get(det, "magnifying-glass")} if known else None)
        evidence = _learning_evidence(rec)
        does = "Write PRD" if rec.get("proposed_shape") == "prd" else DOES.get(
            det, KIND_DOES.get(str(rec.get("kind") or ""), "Do it"))
    if detector is None:
        # No detector of its own (a seeded or imported item): its kind names the node, and its
        # evidence is counted, so the card is never the agent alone (tester 07-20).
        name, icon = KIND_NAME.get(str(rec.get("kind") or ""), ("Proposal", "magnifying-glass"))
        detector = {"label": name, "icon": icon}
        n = _n(rec.get("evidence_count")) or len(rec.get("evidence") or [])
        if not evidence and n:
            evidence = [_chip(_plural(n, "signal"), "chart-line-up")]
    if not evidence and str(rec.get("why") or "").strip():
        # NO CARD WITHOUT ITS EVIDENCE NODE (07-20): an item with no recorded evidence still has
        # the reason it was proposed — its why's lead, in ≤ 4 words ("Customers email support
        # today", "sla.mjs changed 9 times") — so the card reads evidence → detector → agent.
        from ..project_experience.titles import shorten

        import re

        # A path leads with its file's name only ("sla.mjs changed 9 times").
        lead = re.sub(r"^\S*/(\S+)", r"\1", shorten(str(rec["why"]), 4))
        if lead:
            evidence = [_chip(lead, "chart-line-up")]
    return {"evidence": evidence, "detector": detector, "does": does,
            "cost_usd": cost, "cost_basis": basis}


__all__ = ["card", "signal_kinds", "DOES", "KIND_DOES", "ONBOARDING_FINDERS"]
