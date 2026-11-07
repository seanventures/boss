"""The Recommended roadmap: findings → ranked recommendations → a person's review (BOSSAPP-911;
design §14.2-§14.4).

THE LOOP
--------
    detectors (learning.py) ─► signals (record_signal) ─► grouped by subject ─► scored, ranked
        ─► the best open as roadmap items (board.open_item, class roadmap_item, A1)
        ─► the Chief of Staff's PROPOSAL on the ledger (record_decision, action 'propose')
        ─► a person: Accept as ticket / epic / PRD · Not now (snooze) · Reject with a reason
        ─► outcome on the proposal (accepted / dismissed / ignored after 7 days / recurred)
        ─► detector weight + thresholds learn from it (learning_examples)

A RECOMMENDATION IS A BOARD ITEM. Nothing here is a second backlog: the item is an ambient
work item (one live copy per fingerprint, the loop guards, the cooldown after a reject), the
proposal and the person's answer are ledger rows, and the detail the roadmap needs (evidence,
score parts, detector, what it became) is one 1:1 row in ambient_recommendations.

SCORING REUSES THE PORTFOLIO'S MATH (design §14.3; work_value_scoring, BOSSAPP-353)
-----------------------------------------------------------------------------------
    value      = the lead finding's impact (incidents × severity, hours, $), 0-100
    evidence   = how many refs and how many INDEPENDENT sources (one source caps at 0.6)
    freshness  = 0.5 ^ (age of the newest evidence ÷ its half-life)
    effort     = the detector's size estimate, priced from this project's own median build
                 (√units: a five-unit epic is not five tickets' worth of overhead)
    risk       = 1 + blast radius (fan-in on the code graph), up to 1.5×
    priority   = compute_priority_score(value → strategic value, users → customer impact,
                 freshness → urgency, value×freshness → cost of delay, evidence → confidence)
    weighted   = priority × evidence × freshness × the detector's learned weight
    score      = rank_by_value_density(weighted, effort $ × risk) — value per dollar

REJECT TEACHES; EVIDENCE RE-OPENS
---------------------------------
A reject dismisses the item (the board's cooldown starts), suppresses its signals, records the
reason as a labelled example and lowers the detector's weight. The same fingerprint stays quiet
until its EVIDENCE GROWS — half as much again (at least two more refs) or a new independent
source — and then re-surfaces as a new item that names the one it re-opens. An item whose
evidence vanished (the hotspot was refactored) closes itself: "resolved by change".
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import board, ledger, learning, policy, signals
from ._common import (EvidenceError, clean_text, emit, iso, now, person, serialize_row,
                      short_label, verify_evidence)

TASK_CLASS = "roadmap_item"
PROPOSER = "agent:chief_of_staff"
STATUSES = ("open", "snoozed", "accepted", "rejected", "resolved_by_change")
REJECT_CATEGORIES = ("not_valuable", "wrong", "duplicate", "already_planned", "too_small",
                     "other")
#: How often the worker runs the detectors for a project (design: "constantly", but bounded).
RUN_EVERY = timedelta(hours=6)
#: New items one run may open (the board's daily attempt cap bounds the day as well).
MAX_NEW_PER_RUN = 5
#: A recommendation backed by ONE kind of source never scores above this evidence strength.
SINGLE_SOURCE_CAP = 0.6
#: A rejected fingerprint re-surfaces only when its evidence grew by this factor (and ≥ 2 refs).
RESURFACE_GROWTH = 1.5
#: An accepted recommendation whose evidence keeps arriving this long after acceptance RECURRED.
RECUR_GRACE = timedelta(days=7)
#: ... and may come back as a new recommendation after this long.
REOPEN_AFTER_ACCEPT = timedelta(days=30)
#: Muting (design §14.4): under 15 % acceptance after 20 decided items.
MUTE_MIN_DECIDED = 20
MUTE_BELOW = 0.15
#: The design's baseline cost of one merged change (≈ $18 per PR, Oct 28) when a project has no
#: build history of its own to price effort with.
BASELINE_BUILD_USD = 18.0
#: Model phrasing is optional and bounded: at most this many calls a run, each priced first.
MAX_PHRASE_CALLS = 5
PHRASE_ESTIMATE_USD_MICROS = 20_000


def disabled() -> bool:
    return os.environ.get("BOSS_AMBIENT_DISABLED", "").strip() in ("1", "true", "yes") or \
        os.environ.get("BOSS_ROADMAP_DISABLED", "").strip() in ("1", "true", "yes")


# ── optional model phrasing (stubbable; never decides) ──────────────────────────────────────
#
# Detectors write a plain title and a ≤ 3-sentence why from their numbers. A deployment may
# register a PHRASER (one call through the one agent path) to make the wording read better; it
# may change ONLY the title and the why, never the evidence, the score or the shape, and any
# failure keeps the deterministic text. Tests leave it unset: no model call anywhere here.

Phraser = Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]
_PHRASER: Optional[Phraser] = None


def set_phraser(fn: Optional[Phraser]) -> None:
    global _PHRASER
    _PHRASER = fn


def _phrase(connection: Connection, project_id: str, group: learning.Group,
            budget: dict[str, int]) -> tuple[str, str, int]:
    lead = group.lead
    title, why = clean_text(lead.title, 200), _why(group)
    if _PHRASER is None or budget["calls"] >= MAX_PHRASE_CALLS:
        return title, why, 0
    if int(policy.envelope(connection, project_id)["available_usd_micros"]) < \
            PHRASE_ESTIMATE_USD_MICROS:
        return title, why, 0
    budget["calls"] += 1
    try:
        said = _PHRASER({"title": title, "why": why, "facts": lead.facts,
                         "detectors": [m.detector for m in group.members]}) or {}
    except Exception:  # noqa: BLE001 — wording is optional; the numbers already said it
        return title, why, 0
    new_title = clean_text(said.get("title"), 200) or title
    new_why = clean_text(said.get("why"), 600) or why
    if ledger.sentences(new_why) > 3:
        new_why = why
    return new_title, new_why, max(0, int(said.get("cost_usd_micros") or 0))


def _why(group: learning.Group) -> str:
    """The lead's why, plus one sentence per other source grouped in (≤ 3 sentences)."""
    parts = [clean_text(group.lead.why, 400)]
    others = [m for m in group.members if m is not group.lead]
    if others:
        names = ", ".join(learning.DETECTORS[m.detector].title.lower() for m in others[:3])
        parts.append(f"Also seen by: {names}.")
    text_ = " ".join(p.strip() for p in parts if p.strip())
    while ledger.sentences(text_) > 3 and len(parts) > 1:
        parts.pop()
        text_ = " ".join(parts)
    return text_


# ── scoring ─────────────────────────────────────────────────────────────────────────────────

def evidence_strength(n_refs: int, n_sources: int) -> float:
    """More refs and more INDEPENDENT sources → stronger; one kind of source is capped."""
    volume = 1 - math.exp(-max(0, n_refs) / 4)
    if n_sources <= 1:
        return round(min(SINGLE_SOURCE_CAP, volume), 3)
    return round(min(1.0, volume * (0.75 + 0.15 * (n_sources - 1)) + 0.1), 3)


def freshness(evidence_at: Optional[datetime], half_life_days: float, *,
              at: Optional[datetime] = None) -> float:
    if evidence_at is None:
        return 0.5
    age = max(0.0, ((at or now()) - evidence_at).total_seconds() / 86400)
    return round(0.5 ** (age / max(1.0, half_life_days)), 3)


def risk_of(blast_radius: int) -> float:
    return round(1 + min(0.5, max(0, blast_radius) / 40), 3)


def median_build_usd(connection: Connection, project_id: str) -> float:
    """What one change costs HERE (the BudgetGovernor's forecast basis): the median build."""
    if not learning.table_exists(connection, "run_scorecards"):
        return BASELINE_BUILD_USD
    costs = [float(x) for x in connection.execute(text(
        "SELECT cost_usd FROM run_scorecards WHERE project_id = :p AND unit_kind = 'build' "
        "AND cost_usd > 0 ORDER BY ended_at DESC NULLS LAST LIMIT 200"),
        {"p": project_id}).scalars()]
    return round(median(costs), 2) if len(costs) >= 3 else BASELINE_BUILD_USD


def score(group: learning.Group, *, weight: float, build_usd: float,
          at: Optional[datetime] = None) -> dict[str, Any]:
    """The score's parts and the rank key, through work_value_scoring (design §14.3)."""
    from ..work_value_scoring import compute_priority_score, rank_by_value_density

    lead = group.lead
    det = learning.DETECTORS[lead.detector]
    evidence = group.evidence
    strength = evidence_strength(len(evidence), len(group.source_kinds))
    half_life = min(learning.HALF_LIFE_DAYS[learning.DETECTORS[m.detector].evidence_source]
                    for m in group.members)
    fresh = freshness(group.evidence_at, half_life, at=at)
    value = learning.clamp(max(m.impact for m in group.members)
                           + 5 * (len(group.members) - 1))
    blast = max(m.blast_radius for m in group.members)
    risk = risk_of(blast)
    users = max(m.users_affected for m in group.members)
    effort = max(1, min(13, max(m.effort_units for m in group.members)))
    drivers = {
        "strategic_value": value,
        "customer_impact": learning.clamp(40 + 60 * learning.log_scale(users, 1000)) if users
        else learning.clamp(value * 0.6),
        "urgency": learning.clamp(100 * fresh),
        "cost_of_delay": learning.clamp(value * fresh),
        "risk_reduction": learning.clamp(value if det.kind in ("hardening", "test_gap",
                                                               "design_fix") else value / 2),
        "dependency_pressure": learning.clamp(100 * learning.log_scale(blast, 50)),
        "confidence": learning.clamp(100 * strength),
    }
    priority = float(compute_priority_score(**drivers))
    weighted = priority * strength * fresh * max(0.0, weight)
    cost_micros = int(round(build_usd * math.sqrt(effort) * risk * 1_000_000))
    ranked = rank_by_value_density([{"id": group.fingerprint, "priority_score": weighted,
                                     "estimated_cost_usd_micros": cost_micros}])[0]
    return {"value": value, "evidence_strength": strength, "freshness": fresh,
            "effort_units": effort, "risk": risk, "detector_weight": round(weight, 3),
            "priority_score": round(priority, 2), "weighted_score": round(weighted, 2),
            "estimated_cost_usd_micros": cost_micros,
            "score": round(float(ranked["value_density"]), 3), "drivers": drivers,
            "single_source": len(group.source_kinds) <= 1}


# ── detector feedback: weight, mute, thresholds ─────────────────────────────────────────────

def detector_stats(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    """Per detector: shown, accepted, rejected, ignored, acceptance, learned weight, muted,
    threshold scale and the most common reject reasons (design §14.4)."""
    rows = connection.execute(text(
        "SELECT r.detector, count(*) AS shown, "
        "count(*) FILTER (WHERE r.status = 'accepted') AS accepted, "
        "count(*) FILTER (WHERE r.status = 'rejected') AS rejected, "
        "count(*) FILTER (WHERE d.outcome = 'ignored' AND r.status NOT IN ('accepted', "
        "'rejected')) AS ignored "
        "FROM ambient_recommendations r LEFT JOIN agent_act_decisions d "
        "ON d.act_id = r.proposal_act_id WHERE r.project_id = :p GROUP BY r.detector"),
        {"p": project_id}).mappings().all()
    reasons = connection.execute(text(
        "SELECT detector, reject_category, count(*) AS n FROM ambient_recommendations "
        "WHERE project_id = :p AND status = 'rejected' GROUP BY 1, 2 ORDER BY 3 DESC"),
        {"p": project_id}).mappings().all()
    tuning = detector_tuning(connection, project_id)
    out: dict[str, dict[str, Any]] = {}
    for key, det in learning.DETECTORS.items():
        out[key] = {"detector": key, "title": det.title, "kind": det.kind, "shown": 0,
                    "accepted": 0, "rejected": 0, "ignored": 0, "decided": 0,
                    "acceptance": None, "weight": 1.0, "muted": False,
                    "threshold_scale": tuning.get(key, 1.0), "reject_reasons": {}}
    for r in rows:
        if r["detector"] not in out:
            continue
        acc, rej, ign = int(r["accepted"]), int(r["rejected"]), int(r["ignored"])
        decided = acc + rej + ign
        stat = out[r["detector"]]
        stat.update(shown=int(r["shown"]), accepted=acc, rejected=rej, ignored=ign,
                    decided=decided)
        if decided:
            stat["acceptance"] = round(acc / decided, 3)
        # Laplace-smoothed acceptance against a 50 % prior: 1.0 until people have spoken.
        stat["weight"] = round(max(0.25, min(2.0, ((acc + 1) / (decided + 2)) / 0.5)), 3)
        stat["muted"] = decided >= MUTE_MIN_DECIDED and (acc / decided) < MUTE_BELOW
    for r in reasons:
        if r["detector"] in out:
            out[r["detector"]]["reject_reasons"][r["reject_category"] or "other"] = int(r["n"])
    return out


def detector_tuning(connection: Connection, project_id: str) -> dict[str, float]:
    """Each "too small" reject in the last 90 days raises that detector's thresholds 25 % (up to
    2×): the labelled examples in learning_examples, not a hand-edited constant."""
    rows = connection.execute(text(
        "SELECT label, count(*) FROM learning_examples WHERE project_id = :p AND agent = "
        "'roadmap' AND kind = 'recommendation_feedback' AND outcome = 'rejected' AND "
        "payload->>'category' = 'too_small' AND created_at > now() - interval '90 days' "
        "GROUP BY label"), {"p": project_id}).all()
    return {label: round(min(2.0, 1 + 0.25 * int(n)), 2) for label, n in rows}


def _feedback(connection: Connection, project_id: str, rec: Mapping[str, Any], outcome: str,
              **payload: Any) -> None:
    from ..learning_loop import LearningLoopService

    LearningLoopService().record(
        connection, project_id, "roadmap", "recommendation_feedback", label=rec["detector"],
        outcome=outcome, ref=rec["work_item_id"], source="roadmap",
        payload={"fingerprint": rec["fingerprint"], "kind": rec["kind"],
                 "evidence_count": rec["evidence_count"], **payload})


def _maybe_report_mute(connection: Connection, project_id: str, detector_key: str,
                       was_muted: bool) -> None:
    stat = detector_stats(connection, project_id).get(detector_key)
    if stat and stat["muted"] and not was_muted:
        emit(connection, project_id, "ambient.detector_muted", "boss", "project", project_id,
             {"detector": detector_key, "acceptance": stat["acceptance"],
              "decided": stat["decided"]})


# ── reading the store ───────────────────────────────────────────────────────────────────────

def _rec(connection: Connection, item_id: str, *, project_id: Optional[str] = None,
         lock: bool = False) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT * FROM ambient_recommendations WHERE work_item_id = :w "
        "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p)" + (" FOR UPDATE" if lock else "")),
        {"w": item_id, "p": project_id}).mappings().first()
    if row is None:
        raise LookupError(f"no recommendation {item_id}")
    return serialize_row(row)


def _latest_for(connection: Connection, project_id: str,
                fingerprint: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM ambient_recommendations WHERE project_id = :p AND fingerprint = :f "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id, "f": fingerprint}).mappings().first()
    return serialize_row(row) if row is not None else None


def _dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


# ── the run ─────────────────────────────────────────────────────────────────────────────────

def due(connection: Connection, project_id: str, *, every: timedelta = RUN_EVERY) -> bool:
    last = connection.execute(text(
        "SELECT last_run_at FROM signal_adapter_cursors WHERE adapter = :a"),
        {"a": f"roadmap@{project_id}"}).scalar()
    return last is None or last <= now() - every


def run(connection: Connection, project_id: str, *, settings: Any = None,
        only: Optional[Iterable[str]] = None, max_new: int = MAX_NEW_PER_RUN,
        at: Optional[datetime] = None, actor: str = PROPOSER) -> dict[str, Any]:
    """One pass of continuous learning for a project: detect, record, group, score, open the best
    as roadmap items, refresh the live ones, close the ones whose evidence vanished.

    Bounded: detectors read bounded windows; at most `max_new` items open; the stop, the kill
    switch (BOSS_AMBIENT_DISABLED / BOSS_ROADMAP_DISABLED) and the loop guards all hold."""
    report: dict[str, Any] = {"project_id": project_id, "detectors": {}, "opened": [],
                              "joined": [], "resurfaced": [], "suppressed": [], "held": [],
                              "closed": [], "refused": [], "recurred": [], "skipped": None}
    if disabled():
        report["skipped"] = "disabled"
        return report
    if policy.is_stopped(connection, project_id):
        report["skipped"] = "stopped"
        return report
    moment = at or now()
    stats = detector_stats(connection, project_id)
    muted = [k for k, s in stats.items() if s["muted"]]
    found = learning.detect(connection, project_id, only=only, skip=muted,
                            tuning={k: s["threshold_scale"] for k, s in stats.items()}, at=moment)
    for key, result in found.items():
        report["detectors"][key] = {"found": len(result["findings"]), "error": result["error"]}
    for key in muted:
        report["detectors"][key] = {"found": 0, "error": None, "muted": True}
    groups = learning.group(found, project_id=project_id)
    build_usd = median_build_usd(connection, project_id)
    budget = {"calls": 0}

    candidates: list[tuple[dict[str, Any], learning.Group, list[str], Optional[dict]]] = []
    seen_fps: set[str] = set()
    for grp in groups:
        seen_fps.add(grp.fingerprint)
        signal_ids = [_record(connection, project_id, f, actor="boss") for f in grp.members]
        weight = stats.get(grp.lead.detector, {}).get("weight", 1.0)
        parts = score(grp, weight=weight, build_usd=build_usd, at=moment)
        live = board.live_for_fingerprint(connection, project_id, grp.fingerprint)
        prior = _latest_for(connection, project_id, grp.fingerprint)
        if live is not None and prior is not None and prior["work_item_id"] == live["id"]:
            board.open_item(connection, settings, project_id=project_id,
                            fingerprint=grp.fingerprint, title=prior["title"],
                            task_class=TASK_CLASS, signal_ids=signal_ids, actor=actor)
            _refresh(connection, prior, grp, parts, at=moment)
            report["joined"].append(live["id"])
            continue
        if prior is not None and prior["status"] == "accepted":
            if _recurred(connection, prior, grp):
                report["recurred"].append(prior["work_item_id"])
            decided = _dt(prior["decided_at"]) or moment
            if not (prior.get("recurred_at") and moment - decided >= REOPEN_AFTER_ACCEPT):
                report["held"].append(prior["work_item_id"])
                continue
        if prior is not None and prior["status"] == "rejected":
            if not _grew(prior, grp):
                for sid in signal_ids:
                    signals.set_status(connection, sid, "suppressed", reason=(
                        f"rejected: {prior.get('reject_reason') or 'by a person'}; waits for "
                        "more evidence"))
                report["suppressed"].append(prior["work_item_id"])
                continue
        candidates.append((parts, grp, signal_ids, prior))

    candidates.sort(key=lambda c: (-c[0]["score"], -c[0]["priority_score"], c[1].fingerprint))
    for parts, grp, signal_ids, prior in candidates:
        if len(report["opened"]) >= max(0, int(max_new)):
            report["refused"].append({"fingerprint": grp.fingerprint, "guard": "run_cap"})
            continue
        opened = _open(connection, project_id, grp, parts, signal_ids, prior,
                       settings=settings, actor=actor, budget=budget, at=moment)
        if opened["outcome"] != "opened":
            report["refused"].append({"fingerprint": grp.fingerprint,
                                      "guard": opened.get("guard"),
                                      "reason": opened.get("reason")})
            continue
        report["opened"].append(opened["item"]["id"])
        if prior is not None and prior["status"] == "rejected":
            report["resurfaced"].append(opened["item"]["id"])

    report["closed"] = _close_vanished(connection, project_id, found, seen_fps)
    _wake_snoozed(connection, project_id)
    connection.execute(text(
        "INSERT INTO signal_adapter_cursors (adapter, cursor_at, last_run_at, last_recorded, "
        "last_error) VALUES (:a, :c, now(), :n, :e) ON CONFLICT (adapter) DO UPDATE SET "
        "cursor_at = EXCLUDED.cursor_at, last_run_at = now(), last_recorded = "
        "EXCLUDED.last_recorded, last_error = EXCLUDED.last_error"),
        {"a": f"roadmap@{project_id}", "c": moment, "n": len(report["opened"]),
         "e": "; ".join(f"{k}: {v['error']}" for k, v in report["detectors"].items()
                        if v.get("error"))[:500] or None})
    return report


def _record(connection: Connection, project_id: str, f: learning.Finding, *,
            actor: str) -> str:
    det = learning.DETECTORS[f.detector]
    refs = [{"kind": e["kind"], "ref": e["ref"], **({"note": e["note"]} if e.get("note")
                                                    else {})} for e in f.evidence[:20]]
    result = signals.record_signal(
        connection, project_id=project_id, source=det.source, kind=f"learn.{f.detector}",
        title=f.title, fingerprint_parts=f.fingerprint_parts(), subject_kind="subject",
        subject_id=f.subject, severity=f.severity, evidence=refs,
        source_ref=f"learn:{f.detector}:{f.evidence_digest()}", observed_at=f.evidence_at,
        dedupe_window_seconds=0, actor=actor)
    return result["signal"]["id"]


def _grew(prior: Mapping[str, Any], grp: learning.Group) -> bool:
    """Has the evidence grown enough since the reject to ask again?"""
    before = int(prior.get("evidence_count_at_decision") or prior.get("evidence_count") or 0)
    now_n = len(grp.evidence)
    if now_n >= max(before + 2, math.ceil(before * RESURFACE_GROWTH)):
        return True
    return bool(set(grp.source_kinds) - set(prior.get("source_kinds") or []))


def _recurred(connection: Connection, prior: Mapping[str, Any], grp: learning.Group) -> bool:
    """An accepted recommendation whose problem keeps producing NEW evidence after the grace
    period recurred: the person's accept decision gets that outcome (once)."""
    if prior.get("recurred_at"):
        return False
    decided = _dt(prior.get("decided_at"))
    newest = grp.evidence_at
    if decided is None or newest is None or newest <= decided + RECUR_GRACE:
        return False
    connection.execute(text(
        "UPDATE ambient_recommendations SET recurred_at = now(), updated_at = now() "
        "WHERE work_item_id = :w"), {"w": prior["work_item_id"]})
    act = prior.get("decision_act_id")
    if act:
        current = connection.execute(text(
            "SELECT outcome FROM agent_act_decisions WHERE act_id = :a"), {"a": act}).scalar()
        if current is None:
            ledger.record_outcome(connection, act, outcome="recurred",
                                  outcome_ref=f"evidence after {iso(decided)}")
    return True


def _verified(connection: Connection, project_id: str,
              evidence: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The refs the ledger can stand on: each checked on its own, missing ones dropped."""
    out: list[dict[str, Any]] = []
    for e in evidence:
        ref = {"kind": e["kind"], "ref": e["ref"], **({"note": e["note"]} if e.get("note")
                                                      else {})}
        try:
            with connection.begin_nested():
                verify_evidence(connection, project_id, [ref])
            out.append(ref)
        except (EvidenceError, ValueError):
            continue
    return out


def _open(connection: Connection, project_id: str, grp: learning.Group,
          parts: Mapping[str, Any], signal_ids: list[str], prior: Optional[Mapping[str, Any]],
          *, settings: Any, actor: str, budget: dict[str, int],
          at: datetime) -> dict[str, Any]:
    det = learning.DETECTORS[grp.lead.detector]
    title, why, phrase_cost = _phrase(connection, project_id, grp, budget)
    severity = max((m.severity for m in grp.members), key=signals._severity_rank)
    opened = board.open_item(
        connection, settings, project_id=project_id, fingerprint=grp.fingerprint, title=title,
        task_class=TASK_CLASS, subject_ref=grp.lead.subject, signal_ids=signal_ids,
        severity=severity, description=why, actor=actor,
        scores={k: v for k, v in parts["drivers"].items()})
    if opened["outcome"] != "opened":
        return opened
    item = opened["item"]
    evidence = grp.evidence
    connection.execute(text(
        "INSERT INTO ambient_recommendations (work_item_id, project_id, detector, detectors, "
        "kind, fingerprint, subject, title, why, proposed_shape, squad, evidence, "
        "evidence_count, source_kinds, facts, value, evidence_strength, freshness, "
        "effort_units, risk, detector_weight, priority_score, estimated_cost_usd_micros, score, "
        "evidence_at, resurfaced_from, last_seen_at) VALUES (:w, :p, :d, CAST(:ds AS JSONB), "
        ":k, :f, :s, :t, :y, :sh, CAST(:sq AS JSONB), CAST(:ev AS JSONB), :n, "
        "CAST(:sk AS JSONB), CAST(:fa AS JSONB), :v, :es, :fr, :ef, :r, :dw, :ps, :cost, :sc, "
        ":at, :rf, :seen)"),
        {"w": item["id"], "p": project_id, "d": det.key,
         "ds": json.dumps(sorted({m.detector for m in grp.members})), "k": det.kind,
         "f": grp.fingerprint, "s": grp.lead.subject, "t": title, "y": why, "sh": det.shape,
         "sq": json.dumps(sorted({r for m in grp.members
                                  for r in learning.DETECTORS[m.detector].squad})),
         "ev": json.dumps(evidence[:50], default=str), "n": len(evidence),
         "sk": json.dumps(grp.source_kinds), "fa": json.dumps(_facts(grp), default=str),
         "v": parts["value"], "es": parts["evidence_strength"], "fr": parts["freshness"],
         "ef": parts["effort_units"], "r": parts["risk"], "dw": parts["detector_weight"],
         "ps": parts["priority_score"], "cost": parts["estimated_cost_usd_micros"],
         "sc": parts["score"], "at": grp.evidence_at,
         "rf": prior["work_item_id"] if prior and prior["status"] == "rejected" else None,
         "seen": at})
    cited = [{"kind": "signal", "ref": s} for s in signal_ids[:5]] + \
        _verified(connection, project_id, evidence[:10])
    proposal = ledger.record_decision(
        connection, project_id=project_id, decided_by=actor,
        question=f"Put “{short_label(title, 8)}” on the roadmap?", chosen="recommend",
        options=[{"key": "recommend", "label": "Recommend"},
                 {"key": "observe", "label": "Only watch"}],
        rejected=[{"option": "observe", "why": "the evidence crossed the detector's bar"}],
        rationale=_rationale(parts, grp), reversibility="reversible",
        undo={"action": "dismiss", "work_item_id": item["id"]}, evidence=cited,
        confidence=parts["evidence_strength"], work_item_id=item["id"], signal_ids=signal_ids,
        task_class=TASK_CLASS, action="propose", cost_usd_micros=phrase_cost)
    connection.execute(text(
        "UPDATE ambient_recommendations SET proposal_act_id = :a WHERE work_item_id = :w"),
        {"a": proposal["act_id"], "w": item["id"]})
    emit(connection, project_id, "ambient.recommendation_opened", actor, "work_item", item["id"],
         {"detector": det.key, "kind": det.kind, "score": parts["score"],
          "resurfaced_from": prior["work_item_id"] if prior and prior["status"] == "rejected"
          else None})
    return opened


def _facts(grp: learning.Group) -> dict[str, Any]:
    return {m.detector: m.facts for m in grp.members}


def _rationale(parts: Mapping[str, Any], grp: learning.Group) -> str:
    sources = ", ".join(grp.source_kinds)
    single = " One source only, so it ranks lower." if parts["single_source"] else ""
    return (f"{len(grp.evidence)} evidence refs from {sources}; value {parts['value']}, "
            f"freshness {parts['freshness']}.{single}")


def _refresh(connection: Connection, rec: Mapping[str, Any], grp: learning.Group,
             parts: Mapping[str, Any], *, at: datetime) -> None:
    """Re-checked evidence and a re-computed score for a live recommendation (design §14.3)."""
    evidence = grp.evidence
    connection.execute(text(
        "UPDATE ambient_recommendations SET evidence = CAST(:ev AS JSONB), evidence_count = :n, "
        "source_kinds = CAST(:sk AS JSONB), facts = CAST(:fa AS JSONB), "
        "detectors = CAST(:ds AS JSONB), value = :v, evidence_strength = :es, freshness = :fr, "
        "effort_units = :ef, risk = :r, detector_weight = :dw, priority_score = :ps, "
        "estimated_cost_usd_micros = :cost, score = :sc, evidence_at = :at, last_seen_at = :seen,"
        " updated_at = now() WHERE work_item_id = :w"),
        {"ev": json.dumps(evidence[:50], default=str), "n": len(evidence),
         "sk": json.dumps(grp.source_kinds), "fa": json.dumps(_facts(grp), default=str),
         "ds": json.dumps(sorted({m.detector for m in grp.members})),
         "v": parts["value"], "es": parts["evidence_strength"], "fr": parts["freshness"],
         "ef": parts["effort_units"], "r": parts["risk"], "dw": parts["detector_weight"],
         "ps": parts["priority_score"], "cost": parts["estimated_cost_usd_micros"],
         "sc": parts["score"], "at": grp.evidence_at, "seen": at, "w": rec["work_item_id"]})


def _close_vanished(connection: Connection, project_id: str,
                    found: Mapping[str, Mapping[str, Any]], seen: set[str]) -> list[str]:
    """A live recommendation whose detector ran cleanly and no longer finds it: its evidence
    vanished (refactored, tested, aged out of the window) — it closes itself."""
    clean = [k for k, v in found.items() if not v["error"]]
    if not clean:
        return []
    rows = connection.execute(text(
        "SELECT r.work_item_id, r.fingerprint, r.detectors FROM ambient_recommendations r "
        "JOIN work_items w ON w.id = r.work_item_id WHERE r.project_id = :p "
        "AND r.status IN ('open', 'snoozed') AND w.lifecycle = ANY(:live) "
        "AND r.detector = ANY(:clean)"),
        {"p": project_id, "live": list(board.LIVE), "clean": clean}).mappings().all()
    closed: list[str] = []
    for r in rows:
        ran_all = all(d in clean for d in (r["detectors"] or []))
        if r["fingerprint"] in seen or not ran_all:
            continue
        board.finish(connection, r["work_item_id"], actor="boss", lifecycle="resolved",
                     outcome="resolved by change: the evidence is gone")
        connection.execute(text(
            "UPDATE ambient_recommendations SET status = 'resolved_by_change', updated_at = now() "
            "WHERE work_item_id = :w"), {"w": r["work_item_id"]})
        closed.append(r["work_item_id"])
    return closed


def _wake_snoozed(connection: Connection, project_id: str) -> None:
    connection.execute(text(
        "UPDATE ambient_recommendations SET status = 'open', snoozed_until = NULL, "
        "updated_at = now() WHERE project_id = :p AND status = 'snoozed' "
        "AND snoozed_until <= now()"), {"p": project_id})


# ── a person's review (design §14.4) ────────────────────────────────────────────────────────

_OPTIONS = [{"key": "accept_as_ticket", "label": "Accept as ticket"},
            {"key": "accept_as_epic", "label": "Accept as epic"},
            {"key": "accept_as_prd", "label": "Accept as PRD"},
            {"key": "snooze", "label": "Not now"}, {"key": "reject", "label": "Reject"}]


def _reviewable(connection: Connection, item_id: str, project_id: str) -> dict[str, Any]:
    rec = _rec(connection, item_id, project_id=project_id, lock=True)
    if rec["status"] not in ("open", "snoozed"):
        raise board.Conflict(f"this recommendation is already {rec['status']}")
    item = board.get(connection, item_id, project_id=project_id)
    if item is None or item["lifecycle"] not in board.LIVE:
        raise board.Conflict("this recommendation is no longer live")
    return rec


def _person_decision(connection: Connection, rec: Mapping[str, Any], *, actor: str,
                     chosen: str, rationale: str) -> dict[str, Any]:
    return ledger.record_decision(
        connection, project_id=rec["project_id"], decided_by=person(actor),
        question=f"Take “{short_label(rec['title'], 8)}” onto the roadmap?", chosen=chosen,
        options=_OPTIONS, rationale=rationale, reversibility="compensable",
        work_item_id=rec["work_item_id"], task_class=TASK_CLASS,
        evidence=[{"kind": "work_item", "ref": rec["work_item_id"]}],
        parent_act_id=rec.get("proposal_act_id"))


def _settle_proposal(connection: Connection, rec: Mapping[str, Any], outcome: str,
                     ref: Optional[str]) -> None:
    act = rec.get("proposal_act_id")
    if not act:
        return
    current = connection.execute(text(
        "SELECT outcome FROM agent_act_decisions WHERE act_id = :a"), {"a": act}).scalar()
    if current is None:
        ledger.record_outcome(connection, act, outcome=outcome, outcome_ref=ref)


def accept(connection: Connection, item_id: str, *, project_id: str, actor: str,
           as_: Optional[str] = None, note: str = "", settings: Any = None,
           plane: bool = True) -> dict[str, Any]:
    """A person takes the recommendation onto the roadmap. It becomes real work through the
    normal paths: a ticket or epic on the work board (and a Plane issue when Plane is
    configured), or a PRD panel (request_meeting) for anything that needs a design first."""
    rec = _reviewable(connection, item_id, project_id)
    shape = as_ or rec["proposed_shape"]
    if shape not in learning.SHAPES:
        raise ValueError(f"accept as one of {', '.join(learning.SHAPES)}")
    who = person(actor)
    decision = _person_decision(connection, rec, actor=actor, chosen=f"accept_as_{shape}",
                                rationale=clean_text(note, 600) or f"Accepted as a {shape}.")
    from . import roadmap_artifacts

    artifact = roadmap_artifacts.create(connection, rec, shape=shape, actor=who,
                                        settings=settings, plane=plane)
    ref = artifact.get("meeting_run_id") or artifact.get("work_item_id")
    _settle_proposal(connection, rec, "accepted", f"{shape}:{ref}")
    if artifact.get("meeting_run_id"):
        board.link(connection, item_id, "meeting", artifact["meeting_run_id"], actor=who,
                   note="accepted as PRD")
    if artifact.get("work_item_id"):
        board.link(connection, item_id, "recommendation", f"work_item:{artifact['work_item_id']}",
                   actor=who, note=f"accepted as {shape}")
    if artifact.get("plane_issue"):
        board.link(connection, item_id, "recommendation", f"plane:{artifact['plane_issue']}",
                   actor=who, note="Plane issue")
    board.finish(connection, item_id, actor=who, lifecycle="resolved",
                 outcome=f"accepted as {shape}")
    connection.execute(text(
        "UPDATE ambient_recommendations SET status = 'accepted', accepted_as = :s, "
        "artifact = CAST(:a AS JSONB), decision_act_id = :d, decided_by = :by, decided_at = now(),"
        " evidence_count_at_decision = evidence_count, snoozed_until = NULL, updated_at = now() "
        "WHERE work_item_id = :w"),
        {"s": shape, "a": json.dumps(artifact, default=str), "d": decision["act_id"], "by": who,
         "w": item_id})
    _feedback(connection, project_id, rec, "accepted", shape=shape)
    emit(connection, project_id, "ambient.recommendation_accepted", who, "work_item", item_id,
         {"as": shape, "artifact": artifact, "detector": rec["detector"]})
    return {"recommendation": get(connection, item_id, project_id=project_id),
            "artifact": artifact, "decision": decision}


def reject(connection: Connection, item_id: str, *, project_id: str, actor: str, reason: str,
           category: Optional[str] = None) -> dict[str, Any]:
    """A person says no, and why. The item is dismissed (the board's cooldown starts), its signals
    are suppressed until the evidence grows, and the reason becomes a labelled example the
    detector learns from (its weight, and its thresholds when the reason is "too small")."""
    why = clean_text(reason, 500)
    if not why:
        raise ValueError("say why: the reason teaches the detector (design §14.4)")
    kind = category or "other"
    if kind not in REJECT_CATEGORIES:
        raise ValueError(f"category is one of {', '.join(REJECT_CATEGORIES)}")
    rec = _reviewable(connection, item_id, project_id)
    was_muted = detector_stats(connection, project_id).get(rec["detector"], {}).get("muted",
                                                                                   False)
    who = person(actor)
    decision = _person_decision(connection, rec, actor=actor, chosen="reject",
                                rationale=_one_line(why))
    _settle_proposal(connection, rec, "dismissed", f"rejected:{kind}")
    board.dismiss(connection, item_id, actor=actor, reason=why)
    for sid in [l["ref"] for l in board.links(connection, item_id) if l["kind"] == "signal"]:
        signals.set_status(connection, sid, "suppressed", actor=who,
                           reason=f"rejected ({kind}): {why}"[:300])
    connection.execute(text(
        "UPDATE ambient_recommendations SET status = 'rejected', reject_reason = :r, "
        "reject_category = :c, decision_act_id = :d, decided_by = :by, decided_at = now(), "
        "evidence_count_at_decision = evidence_count, snoozed_until = NULL, updated_at = now() "
        "WHERE work_item_id = :w"),
        {"r": why, "c": kind, "d": decision["act_id"], "by": who, "w": item_id})
    _feedback(connection, project_id, rec, "rejected", category=kind, reason=why)
    _maybe_report_mute(connection, project_id, rec["detector"], was_muted)
    emit(connection, project_id, "ambient.recommendation_rejected", who, "work_item", item_id,
         {"category": kind, "detector": rec["detector"]})
    return {"recommendation": get(connection, item_id, project_id=project_id),
            "decision": decision}


def snooze(connection: Connection, item_id: str, *, project_id: str, actor: str,
           until: Any, reason: str = "") -> dict[str, Any]:
    """Not now: hidden from the roadmap until `until`, then back with re-checked evidence."""
    when = _dt(until)
    if when is None:
        raise ValueError("snooze until a date")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when <= now() or when > now() + timedelta(days=180):
        raise ValueError("snooze until a date in the next 180 days")
    rec = _reviewable(connection, item_id, project_id)
    decision = _person_decision(connection, rec, actor=actor, chosen="snooze",
                                rationale=_one_line(clean_text(reason, 300)
                                                    or f"Not now; until {when.date()}."))
    connection.execute(text(
        "UPDATE ambient_recommendations SET status = 'snoozed', snoozed_until = :u, "
        "decision_act_id = COALESCE(decision_act_id, :d), updated_at = now() "
        "WHERE work_item_id = :w"), {"u": when, "d": decision["act_id"], "w": item_id})
    _feedback(connection, project_id, rec, "snoozed", until=iso(when))
    return {"recommendation": get(connection, item_id, project_id=project_id),
            "decision": decision}


def _one_line(value: str) -> str:
    body = clean_text(value, 600)
    while ledger.sentences(body) > 3 and "." in body[:-1]:
        body = body[:body.rstrip(".").rfind(".") + 1]
    return body or "Decided on the roadmap."


# ── the read model: a ranked list of small diagrams (design §11.0) ─────────────────────────

SOURCE_LABEL = {"incident": "Incidents", "code": "Code graph", "review": "Review findings",
                "design": "PRD decisions", "cost": "Cost", "test": "Test runs",
                "stage": "Stage timings", "friction": "Overrides"}
KIND_LABEL = {"hardening": "Hardening", "test_gap": "Test gap", "refactor": "Refactor",
              "feature": "Feature", "design_fix": "Design fix", "cost": "Cost",
              "process": "Process"}
EVIDENCE_TO_SOURCE = {"incident": "incident", "kg_node": "code", "commit": "code",
                      "code_task": "review", "meeting": "design", "decision": "design",
                      "receipt": "cost", "workflow_run": "cost", "step_run": "stage",
                      "query": "code"}


#: Who else proposes on the roadmap, in words (never a raw key on a card): the onboarding agent's
#: ranked backlog (project_experience.onboarding._propose_on_roadmap).
OTHER_PROPOSERS = {"onboarding": "Onboarding"}


def _badges(rec: Mapping[str, Any]) -> dict[str, Any]:
    return {"kind": KIND_LABEL.get(rec["kind"], rec["kind"]), "shape": rec["proposed_shape"],
            "evidence": int(rec["evidence_count"] or 0),
            "sources": len(rec.get("source_kinds") or []),
            "single_source": len(rec.get("source_kinds") or []) <= 1,
            "freshness": float(rec["freshness"] or 0), "effort": int(rec["effort_units"] or 1),
            "risk": float(rec["risk"] or 1), "score": float(rec["score"] or 0),
            "value": float(rec["value"] or 0), "status": rec["status"],
            "resurfaced": bool(rec.get("resurfaced_from")),
            "detector": learning.DETECTORS[rec["detector"]].title
            if rec["detector"] in learning.DETECTORS
            else OTHER_PROPOSERS.get(rec["detector"], rec["detector"]),
            # The card's "≈ $2" (the roadmap screen): the item's own estimate, in dollars.
            "cost_usd": round(int(rec.get("estimated_cost_usd_micros") or 0) / 1_000_000, 2)}


def node_label(rec: Mapping[str, Any]) -> str:
    """≤ 4 words: the kind and what it is about ('Hardening complete_checkout.py',
    'Cost checkout_feature', 'Process design')."""
    subject = str(rec.get("subject") or "")
    head, _, tail = subject.partition(":")
    if head == "file":
        tail = tail.rsplit("/", 1)[-1]
    elif head in ("review", "friction"):
        tail = " ".join(tail.split(":", 1)[-1].split()[:2])
    return short_label(f"{KIND_LABEL.get(rec['kind'], rec['kind'])} {tail.replace('_', ' ')}"
                       if head != "file" else f"{KIND_LABEL.get(rec['kind'], rec['kind'])} {tail}")


def diagram(rec: Mapping[str, Any]) -> dict[str, Any]:
    """Sources → detector → recommendation → shape → what a person did → what it became.
    Labels ≤ 4 words; every node has a ref for its drawer; no prose on the canvas."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    rid = f"rec:{rec['work_item_id']}"
    counts: dict[str, int] = {}
    for e in rec.get("evidence") or []:
        src = EVIDENCE_TO_SOURCE.get(e.get("kind"), "code")
        counts[src] = counts.get(src, 0) + 1
    for kind in rec.get("source_kinds") or []:
        counts.setdefault(kind, 0)
    for src, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        nid = f"source:{src}"
        nodes.append({"id": nid, "type": "signal", "label": SOURCE_LABEL.get(src, src.title()),
                      "stage": 0, "badges": [f"×{n}"] if n else [],
                      "ref": {"kind": "evidence", "work_item_id": rec["work_item_id"],
                              "source": src}})
    for key in rec.get("detectors") or [rec["detector"]]:
        det = learning.DETECTORS.get(key)
        did = f"detector:{key}"
        nodes.append({"id": did, "type": "agent",
                      "label": short_label(det.title if det else OTHER_PROPOSERS.get(key, key)),
                      "stage": 1, "badges": [], "ref": {"kind": "detector", "id": key}})
        for n in nodes:
            if n["type"] == "signal":
                edges.append({"from": n["id"], "to": did, "kind": "raised", "label": "seen",
                              "dissent": False})
        edges.append({"from": did, "to": rid, "kind": "proposed", "label": "proposed",
                      "dissent": False})
    nodes.append({"id": rid, "type": "action", "label": node_label(rec), "stage": 2,
                  "badges": [KIND_LABEL.get(rec["kind"], rec["kind"]),
                             f"score {round(float(rec['score'] or 0), 1)}"],
                  "ref": {"kind": "work_item", "id": rec["work_item_id"]}})
    end = rid
    if rec["status"] in ("accepted", "rejected", "snoozed"):
        word = {"accepted": f"Accepted · {rec.get('accepted_as')}", "rejected": "Rejected",
                "snoozed": "Not now"}[rec["status"]]
        nodes.append({"id": "decision", "type": "decision", "label": word, "stage": 3,
                      "badges": ["person"] + ([rec["reject_category"]]
                                              if rec.get("reject_category") else []),
                      "ref": {"kind": "act", "id": rec.get("decision_act_id")}})
        edges.append({"from": end, "to": "decision", "kind": "decided", "label": "decided",
                      "dissent": rec["status"] == "rejected"})
        end = "decision"
    art = rec.get("artifact") or {}
    if art.get("meeting_run_id"):
        nodes.append({"id": "artifact", "type": "run", "label": "PRD panel", "stage": 4,
                      "badges": [], "ref": {"kind": "meeting", "id": art["meeting_run_id"]}})
    elif art.get("work_item_id"):
        nodes.append({"id": "artifact", "type": "run",
                      "label": "Epic" if rec.get("accepted_as") == "epic" else "Ticket",
                      "stage": 4, "badges": ["Plane"] if art.get("plane_issue") else [],
                      "ref": {"kind": "work_item", "id": art["work_item_id"]}})
    if art:
        edges.append({"from": end, "to": "artifact", "kind": "acted", "label": "became",
                      "dissent": False})
        end = "artifact"
    if rec["status"] == "resolved_by_change" or rec.get("recurred_at"):
        label = "Recurred" if rec.get("recurred_at") else "Resolved by change"
        nodes.append({"id": "outcome", "type": "outcome", "label": label, "stage": 5,
                      "badges": ["recurred" if rec.get("recurred_at") else "resolved"],
                      "ref": {"kind": "work_item", "id": rec["work_item_id"]}})
        edges.append({"from": end, "to": "outcome", "kind": "resulted", "label": "resulted",
                      "dissent": bool(rec.get("recurred_at"))})
    return {"nodes": nodes, "edges": edges}


def _summary(rec: Mapping[str, Any], rank: Optional[int] = None) -> dict[str, Any]:
    keys = ("work_item_id", "project_id", "title", "kind", "detector", "detectors", "subject",
            "proposed_shape", "status", "score", "snoozed_until", "accepted_as", "artifact",
            "reject_category", "resurfaced_from", "recurred_at", "created_at", "decided_at",
            "evidence_at")
    out = {k: rec.get(k) for k in keys}
    out["id"] = rec["work_item_id"]
    out["rank"] = rank
    # Who would do it (the card's agent pill) and why, for "Why ›" (the drawer has the rest).
    out["squad"] = list(rec.get("squad") or (learning.DETECTORS[rec["detector"]].squad
                                             if rec["detector"] in learning.DETECTORS else []))
    out["why"] = rec.get("why")
    out["badges"] = _badges(rec)
    out["strip"] = [{"type": n["type"], "label": n["label"], "badges": n["badges"][:2]}
                    for n in diagram(rec)["nodes"]][:6]
    return out


def list_roadmap(connection: Connection, project_id: str, *, status: str = "open",
                 kind: Optional[str] = None, detector: Optional[str] = None,
                 limit: int = 30) -> dict[str, Any]:
    """The Recommended roadmap tab: ranked (value density) recommendations as small diagrams,
    with counts per status and per-detector stats."""
    if status not in STATUSES + ("all",):
        raise ValueError(f"status is one of {', '.join(STATUSES + ('all',))}")
    _wake_snoozed(connection, project_id)
    # THE DETECTORS' OWN NAMES on every card ("Incident patterns", never `incident_cause`): the
    # registry fills when the detectors module is imported, which a read never did on its own.
    from . import detectors  # noqa: F401
    rows = connection.execute(text(
        "SELECT * FROM ambient_recommendations WHERE project_id = :p "
        "AND (:s = 'all' OR status = :s) AND (CAST(:k AS TEXT) IS NULL OR kind = :k) "
        "AND (CAST(:d AS TEXT) IS NULL OR detector = :d OR detectors ? :d) "
        "ORDER BY CASE WHEN status IN ('open', 'snoozed') THEN 0 ELSE 1 END, score DESC, "
        "priority_score DESC, created_at DESC LIMIT :n"),
        {"p": project_id, "s": status, "k": kind, "d": detector,
         "n": max(1, min(int(limit), 100))}).mappings().all()
    items = [_summary(serialize_row(r), rank=i + 1) for i, r in enumerate(rows)]
    counts = {r["status"]: int(r["n"]) for r in connection.execute(text(
        "SELECT status, count(*) AS n FROM ambient_recommendations WHERE project_id = :p "
        "GROUP BY status"), {"p": project_id}).mappings()}
    last = connection.execute(text(
        "SELECT last_run_at, last_error FROM signal_adapter_cursors WHERE adapter = :a"),
        {"a": f"roadmap@{project_id}"}).mappings().first()
    return {"status": status, "items": items,
            "counts": {s: counts.get(s, 0) for s in STATUSES},
            "detectors": list(detector_stats(connection, project_id).values()),
            "last_run": serialize_row(last) if last else None}


def get(connection: Connection, item_id: str, *, project_id: str) -> dict[str, Any]:
    """One recommendation: its summary, its diagram, and the drawer's detail (why, evidence,
    the score's parts, the detector's numbers, the ledger rows)."""
    rec = _rec(connection, item_id, project_id=project_id)
    detail = {k: rec.get(k) for k in ("why", "evidence", "facts", "value", "evidence_strength",
                                       "freshness", "effort_units", "risk", "detector_weight",
                                       "priority_score", "estimated_cost_usd_micros", "squad",
                                       "source_kinds", "reject_reason", "proposal_act_id",
                                       "decision_act_id", "decided_by")}
    return {**_summary(rec), **diagram(rec), "detail": detail}


def metrics(connection: Connection, project_id: str, *, days: int = 30) -> dict[str, Any]:
    """Recommendation acceptance per detector (design §8: ≥ 30 % → ≥ 50 %), noise, time to a
    person's decision, and what accepted items cost to find."""
    days = max(1, min(int(days), 365))
    row = connection.execute(text(
        "SELECT count(*) AS shown, count(*) FILTER (WHERE r.status = 'accepted') AS accepted, "
        "count(*) FILTER (WHERE r.status = 'rejected') AS rejected, "
        "count(*) FILTER (WHERE r.status = 'snoozed') AS snoozed, "
        "count(*) FILTER (WHERE r.status = 'resolved_by_change') AS resolved_by_change, "
        "count(*) FILTER (WHERE d.outcome = 'ignored') AS ignored, "
        "count(*) FILTER (WHERE r.recurred_at IS NOT NULL) AS recurred, "
        "COALESCE(sum(w.cost_usd_micros), 0) AS cost "
        "FROM ambient_recommendations r JOIN work_items w ON w.id = r.work_item_id "
        "LEFT JOIN agent_act_decisions d ON d.act_id = r.proposal_act_id "
        "WHERE r.project_id = :p AND r.created_at > now() - make_interval(days => :d)"),
        {"p": project_id, "d": days}).mappings().one()
    waits = [float(x) for x in connection.execute(text(
        "SELECT EXTRACT(EPOCH FROM (decided_at - created_at)) / 60 FROM ambient_recommendations "
        "WHERE project_id = :p AND decided_at IS NOT NULL AND created_at > now() - "
        "make_interval(days => :d)"), {"p": project_id, "d": days}).scalars()]
    shown = int(row["shown"])
    accepted, rejected, ignored = int(row["accepted"]), int(row["rejected"]), int(row["ignored"])
    decided = accepted + rejected + ignored
    from .read_model import _summary as spread

    return {"shown": shown, "accepted": accepted, "rejected": rejected, "ignored": ignored,
            "snoozed": int(row["snoozed"]), "resolved_by_change": int(row["resolved_by_change"]),
            "recurred": int(row["recurred"]),
            "acceptance_pct": round(100 * accepted / decided, 1) if decided else None,
            "noise_pct": round(100 * (rejected + ignored) / decided, 1) if decided else None,
            "time_to_decision_minutes": spread(waits),
            "cost_per_accepted_usd": round(int(row["cost"]) / 1e6 / accepted, 4)
            if accepted else None,
            "by_detector": [{k: s[k] for k in ("detector", "title", "shown", "accepted",
                                               "rejected", "ignored", "acceptance", "weight",
                                               "muted")}
                            for s in detector_stats(connection, project_id).values()
                            if s["shown"]]}


# ── the worker's tick ───────────────────────────────────────────────────────────────────────
#
# NO NEW DAEMON. The worker knocks every TICK_SECONDS (apps/worker: _tick_ambient_learning →
# POST /v1/ambient/learning/tick); this decides what is DUE: the outcome sweep (install-wide,
# batched) and the detectors for at most MAX_PROJECTS_PER_TICK projects whose last run is older
# than RUN_EVERY and that have something to learn from. Each project runs in its own transaction
# (one broken project never stops the rest); a second worker racing the first skips (try-lock).

TICK_SECONDS = 600
MAX_PROJECTS_PER_TICK = 10


def tick(engine: Any, *, settings: Any = None, max_projects: int = MAX_PROJECTS_PER_TICK,
         every: timedelta = RUN_EVERY,
         only_projects: Optional[Iterable[str]] = None) -> dict[str, Any]:
    from .. import projects_archive
    from . import outcomes

    out: dict[str, Any] = {"skipped": None, "outcomes": {}, "projects": {}}
    if disabled():
        out["skipped"] = "disabled"
        return out
    with engine.begin() as c:
        if not c.execute(text("SELECT pg_try_advisory_xact_lock(hashtextextended("
                              "'boss-ambient-learning-tick', 0))")).scalar():
            out["skipped"] = "another tick is running"
            return out
        if policy.is_stopped(c, None):
            out["skipped"] = "stopped install-wide"
            return out
        scope = list(only_projects) if only_projects is not None else None
        if scope is None:
            out["outcomes"] = outcomes.sweep(c)
        else:
            out["outcomes"] = {"ignored": 0, "recurred": 0, "merged": 0, "reverted": 0}
            for pid in scope:
                for k, v in outcomes.sweep(c, project_id=pid).items():
                    out["outcomes"][k] += v
        candidates = list(c.execute(text(
            "SELECT p.id FROM projects p LEFT JOIN signal_adapter_cursors s ON s.adapter = "
            "'roadmap@' || p.id WHERE (s.last_run_at IS NULL OR s.last_run_at <= now() - :every) "
            "AND (CAST(:only AS TEXT[]) IS NULL OR p.id = ANY(CAST(:only AS TEXT[]))) "
            "AND (EXISTS (SELECT 1 FROM kg_change_links k WHERE k.project_id = p.id) "
            "OR EXISTS (SELECT 1 FROM incidents i WHERE i.project_id = p.id) "
            "OR EXISTS (SELECT 1 FROM code_task_runs r WHERE r.project_id = p.id "
            "AND r.created_at > now() - interval '90 days')) "
            f"AND {projects_archive.live_sql('p.id')} "
            "ORDER BY s.last_run_at NULLS FIRST LIMIT :n"),
            {"every": every, "n": max(1, int(max_projects)), "only": scope}).scalars())
    for project_id in candidates:
        try:
            with engine.begin() as c:
                report = run(c, project_id, settings=settings)
            out["projects"][project_id] = {"opened": len(report["opened"]),
                                           "closed": len(report["closed"]),
                                           "skipped": report["skipped"]}
        except Exception as exc:  # noqa: BLE001 — one project's failure is reported, not raised
            out["projects"][project_id] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


__all__ = ["TASK_CLASS", "STATUSES", "REJECT_CATEGORIES", "run", "due", "accept", "reject",
           "snooze", "list_roadmap", "get", "metrics", "detector_stats", "detector_tuning",
           "score", "evidence_strength", "freshness", "set_phraser", "diagram", "tick"]
