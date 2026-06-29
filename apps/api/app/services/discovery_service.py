"""Discovery pipeline — research-driven outcome discovery.

Given a seed (a raw idea / problem / signal) plus optional operator context, runs a governed,
web-research pipeline and produces a cited **Discovery Dossier** with candidate outcomes:

    signal_sweep (web) -> synthesize -> adversarial vet (web) -> shape outcomes -> evidence gate

The moat, applied to research: every finding is cited, the thesis is adversarially vetted, and a
fail-closed evidence gate decides "pursue" vs "needs more research". See
docs/product/BOSS_DISCOVERY_PIPELINE_DESIGN.md.

D1 scope: web-only signals; runs in a background thread; the UI polls discovery_runs for progress.
"""
from __future__ import annotations

import json
import threading
import traceback
from typing import Optional

from sqlalchemy import text

from ..config import Settings
from ..db import begin, get_engine
from .code_agent import make_code_agent
from .core import emit_event, make_id

PHASES = ["queued", "signal_sweep", "synthesize", "vet", "shape", "gate", "done"]


# --------------------------------------------------------------------------- #
#  Public API                                                                 #
# --------------------------------------------------------------------------- #
def start_discovery(*, project_id: str, title: str, seed: str, context: str,
                    settings: Settings) -> str:
    """Create the durable run row and kick off the pipeline in a background thread."""
    run_id = make_id("drun")
    engine = get_engine()
    with begin(engine) as c:
        c.execute(
            text("""
                INSERT INTO discovery_runs (id, project_id, status, phase, title, seed, context, state)
                VALUES (:id, :p, 'running', 'queued', :t, :s, :ctx, '{}')
            """),
            {"id": run_id, "p": project_id, "t": title[:300], "s": seed, "ctx": context or ""},
        )
        emit_event(c, project_id, "discovery.started", "boss", "discovery_run", run_id,
                   {"title": title[:300]})
    threading.Thread(
        target=_run_discovery, args=(run_id, project_id, seed, context, settings), daemon=True,
    ).start()
    return run_id


def list_discovery_runs(connection, project_id: str, limit: int = 50) -> list[dict]:
    rows = connection.execute(
        text("""SELECT id, project_id, status, phase, title, seed, context, state, usage, error,
                       created_at, updated_at
                FROM discovery_runs WHERE project_id=:p ORDER BY created_at DESC LIMIT :n"""),
        {"p": project_id, "n": limit},
    ).mappings().all()
    return [_row_to_dict(r) for r in rows]


def load_discovery_run(connection, run_id: str) -> Optional[dict]:
    r = connection.execute(
        text("""SELECT id, project_id, status, phase, title, seed, context, state, usage, error,
                       created_at, updated_at FROM discovery_runs WHERE id=:id"""),
        {"id": run_id},
    ).mappings().first()
    return _row_to_dict(r) if r else None


def _row_to_dict(r) -> dict:
    state = r["state"]
    state = json.loads(state) if isinstance(state, str) else (state or {})
    usage = r["usage"]
    usage = json.loads(usage) if isinstance(usage, str) else usage
    return {
        "id": r["id"], "project_id": r["project_id"], "status": r["status"], "phase": r["phase"],
        "title": r["title"], "seed": r["seed"], "context": r["context"],
        "state": state, "usage": usage, "error": r["error"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


# --------------------------------------------------------------------------- #
#  Pipeline                                                                    #
# --------------------------------------------------------------------------- #
def _run_discovery(run_id: str, project_id: str, seed: str, context: str,
                   settings: Settings) -> None:
    engine = get_engine()
    try:
        agent = make_code_agent(settings, engine="claude_code")
        if not hasattr(agent, "run_research"):
            raise RuntimeError("Discovery requires the claude_code engine (web research).")
        dossier: dict = {}
        cost = 0
        model = None

        # P1 — Signal sweep (web research)
        _phase(engine, run_id, "signal_sweep")
        r = agent.run_research(_sweep_prompt(seed, context))
        cost += r["cost_usd_micros"]; model = r.get("model")
        sweep = _extract_json(r["summary"])
        dossier["research_brief"] = sweep.get("brief", {})
        dossier["signal_findings"] = sweep.get("findings", [])
        _save(engine, run_id, dossier, cost, model)

        # P2 — Synthesize
        _phase(engine, run_id, "synthesize")
        r = agent.run_research(_synth_prompt(seed, context, dossier["signal_findings"]))
        cost += r["cost_usd_micros"]
        dossier["market_analysis"] = _extract_json(r["summary"]).get("analysis", {})
        _save(engine, run_id, dossier, cost, model)

        # P3 — Adversarial vetting (web research for disconfirming evidence)
        _phase(engine, run_id, "vet")
        r = agent.run_research(_vet_prompt(seed, dossier))
        cost += r["cost_usd_micros"]
        dossier["critique"] = _extract_json(r["summary"]).get("critique", {})
        _save(engine, run_id, dossier, cost, model)

        # P4 — Shape outcomes
        _phase(engine, run_id, "shape")
        r = agent.run_research(_shape_prompt(seed, context, dossier))
        cost += r["cost_usd_micros"]
        dossier["candidate_outcomes"] = _extract_json(r["summary"]).get("outcomes", [])
        _save(engine, run_id, dossier, cost, model)

        # P5 — Evidence gate (deterministic, fail-closed)
        _phase(engine, run_id, "gate")
        dossier["gate"] = _evaluate_gate(dossier)

        with begin(engine) as c:
            c.execute(
                text("""UPDATE discovery_runs SET status='completed', phase='done',
                        state=CAST(:s AS JSONB),
                        usage=jsonb_build_object('cost_usd_micros', CAST(:cost AS bigint),
                                                 'model', CAST(:m AS text)),
                        updated_at=now() WHERE id=:id"""),
                {"s": json.dumps(dossier), "cost": cost, "m": model, "id": run_id},
            )
            emit_event(c, project_id, "discovery.completed", "boss", "discovery_run", run_id,
                       {"verdict": dossier["gate"]["verdict"],
                        "outcomes": len(dossier.get("candidate_outcomes", []))})
    except Exception as exc:  # noqa: BLE001 — record the failure durably, never crash the thread
        detail = f"{exc}"[:1000]
        try:
            with begin(engine) as c:
                c.execute(text("UPDATE discovery_runs SET status='failed', error=:e, updated_at=now() WHERE id=:id"),
                          {"e": detail, "id": run_id})
        except Exception:
            traceback.print_exc()


# --------------------------------------------------------------------------- #
#  Persistence helpers                                                         #
# --------------------------------------------------------------------------- #
def _phase(engine, run_id: str, phase: str) -> None:
    with begin(engine) as c:
        c.execute(text("UPDATE discovery_runs SET phase=:ph, updated_at=now() WHERE id=:id"),
                  {"ph": phase, "id": run_id})


def _save(engine, run_id: str, dossier: dict, cost: int, model: Optional[str]) -> None:
    with begin(engine) as c:
        c.execute(
            text("""UPDATE discovery_runs SET state=CAST(:s AS JSONB),
                    usage=jsonb_build_object('cost_usd_micros', CAST(:cost AS bigint),
                                             'model', CAST(:m AS text)),
                    updated_at=now() WHERE id=:id"""),
            {"s": json.dumps(dossier), "cost": cost, "m": model, "id": run_id},
        )


# --------------------------------------------------------------------------- #
#  Evidence gate                                                              #
# --------------------------------------------------------------------------- #
def _evaluate_gate(dossier: dict) -> dict:
    """Fail-closed: an outcome is only worth pursuing with enough cited, vetted evidence."""
    findings = dossier.get("signal_findings") or []
    cited = [f for f in findings if (f or {}).get("citation_url")]
    crit = dossier.get("critique") or {}
    try:
        conf = float(crit.get("overall_confidence_0_1") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    outcomes = dossier.get("candidate_outcomes") or []
    checks = {
        "enough_cited_sources": len(cited) >= 4,
        "confidence_meets_bar": conf >= 0.5,
        "has_outcome": len(outcomes) >= 1,
        "outcome_has_pilot": any((o or {}).get("target_pilot") for o in outcomes),
        "disconfirming_considered": len(crit.get("disconfirming_evidence") or []) >= 1,
    }
    passed = all(checks.values())
    return {
        "verdict": "pursue" if passed else "needs_more_research",
        "checks": checks,
        "confidence": conf,
        "cited_sources": len(cited),
    }


# --------------------------------------------------------------------------- #
#  Prompts                                                                     #
# --------------------------------------------------------------------------- #
_JSON_ONLY = "Return ONLY a single JSON object — no markdown fences, no prose before or after."


def _sweep_prompt(seed: str, context: str) -> str:
    return (
        "You are a rigorous market research analyst doing PRIMARY research with web search.\n"
        f"IDEA / SEED:\n{seed}\n\n"
        f"OPERATOR CONTEXT: {context or '(none provided)'}\n\n"
        "Use WebSearch and WebFetch to gather REAL, CITED market signals. Investigate:\n"
        "1. Competitors / existing alternatives — who, positioning, pricing.\n"
        "2. Evidence of real demand — communities, forums, complaints, search interest; who has "
        "this pain and how often.\n"
        "3. Market size & trends.\n"
        "4. Timing — what changed that makes now relevant.\n"
        "Be skeptical and evidence-driven; prefer primary sources you actually open.\n\n"
        f"{_JSON_ONLY}\n"
        '{"brief": {"hypothesis": "...", "questions": ["..."], "sources_consulted": ["url"]}, '
        '"findings": [{"angle": "competitor|demand|market|timing", "finding": "concise fact", '
        '"evidence": "what the source shows", "citation_url": "https://..."}]}\n'
        "Include 6-12 findings, each with a real citation_url you visited."
    )


def _synth_prompt(seed: str, context: str, findings: list) -> str:
    return (
        "You are a market analyst. Using ONLY the researched findings below, synthesize a "
        "structured market analysis. Every claim must rest on the findings.\n"
        f"IDEA: {seed}\nCONTEXT: {context or '(none)'}\n"
        f"FINDINGS (JSON): {json.dumps(findings)[:6000]}\n\n"
        f"{_JSON_ONLY}\n"
        '{"analysis": {"problem": {"statement": "...", "is_real": true, "frequency": '
        '"rare|occasional|frequent", "severity_0_5": 3}, "target_segment": "...", '
        '"demand_evidence": "...", "competition": {"landscape": "...", "our_wedge": "..."}, '
        '"market_size": "...", "timing": "...", "risks": ["..."]}}'
    )


def _vet_prompt(seed: str, dossier: dict) -> str:
    return (
        "You are a sharp, skeptical venture investor. Challenge this analysis HARD. Use WebSearch "
        "to find DISCONFIRMING evidence. For each key claim decide if it is evidence-backed or "
        "wishful thinking. Default to skepticism.\n"
        f"IDEA: {seed}\n"
        f"ANALYSIS (JSON): {json.dumps(dossier.get('market_analysis', {}))[:5000]}\n"
        f"FINDINGS (JSON): {json.dumps(dossier.get('signal_findings', []))[:4000]}\n\n"
        f"{_JSON_ONLY}\n"
        '{"critique": {"claims": [{"claim": "...", "verdict": "supported|weak|unsupported", '
        '"reason": "..."}], "disconfirming_evidence": ["... (include a url)"], '
        '"overall_confidence_0_1": 0.5, "biggest_risk": "..."}}'
    )


def _shape_prompt(seed: str, context: str, dossier: dict) -> str:
    return (
        "You are a product strategist. Turn the VETTED research into concrete candidate outcomes "
        "to build — but ONLY those the evidence supports. Validation model: prototype -> pilot "
        "with customers who genuinely need it -> measure real usage -> THEN monetize (never "
        "pay-upfront-on-an-idea).\n"
        f"IDEA: {seed}\nCONTEXT: {context or '(none)'}\n"
        f"ANALYSIS: {json.dumps(dossier.get('market_analysis', {}))[:4000]}\n"
        f"CRITIQUE: {json.dumps(dossier.get('critique', {}))[:3000]}\n\n"
        f"{_JSON_ONLY}\n"
        '{"outcomes": [{"outcome": "what to build, scoped", "wedge": "...", "target_pilot": '
        '"who to pilot with", "first_prototype_slice": "smallest prototype to pilot", '
        '"validation_plan": "pilot -> measure usage -> monetize", "rationale": "...", '
        '"confidence_0_1": 0.6, "supporting_signals": ["finding/citation refs"]}]}\n'
        "Give 1-3 outcomes, best first. If the evidence does NOT support building, "
        'return {"outcomes": []}.'
    )


# --------------------------------------------------------------------------- #
#  JSON extraction                                                            #
# --------------------------------------------------------------------------- #
def _extract_json(text_out: str) -> dict:
    """Best-effort parse of a JSON object from an agent's text output.

    Tolerant of markdown fences and — importantly — of a response truncated mid-object (the
    web-research sweep often returns a long JSON whose final closing brace is cut off). When a
    direct parse fails we balance any unclosed strings/brackets and retry."""
    s = (text_out or "").strip()
    if not s:
        return {}
    if s.startswith("```"):
        inner = s.split("```")
        if len(inner) >= 2:
            s = inner[1]
            if s.lstrip().lower().startswith("json"):
                s = s.lstrip()[4:]
            s = s.strip()
    i = s.find("{")
    if i < 0:
        return {}
    s = s[i:]
    try:
        return json.loads(s)
    except Exception:
        pass
    # Repair: walk the text tracking string/escape state + bracket depth, then close whatever
    # is still open (handles a response cut off before its final braces/brackets).
    instr = esc = False
    stack: list[str] = []
    for ch in s:
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    repaired = s + ('"' if instr else "")
    for op in reversed(stack):
        repaired += "}" if op == "{" else "]"
    try:
        return json.loads(repaired)
    except Exception:
        return {}
