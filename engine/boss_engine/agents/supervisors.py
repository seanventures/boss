"""SUPERVISOR SPECS: coordination as plain `boss.agent/v1` documents (AP-P7, BOSSAPP-1058;
docs/design/AGENT_PLATFORM.md §4.5 "Supervisor", decision D13).

A supervisor is just an agent whose spec lists sub-agents. Nothing here runs anything: each
function returns a document that `spec.load_spec` validates and the kernel runs with the
`subagents` middleware (kernel/subagents.py) — the same caps, slices, exchanged tokens and single
writer for every supervisor.

THE STUDIO PANEL TOPOLOGIES, re-implemented (D13; Agent Studio's deterministic panel runtime is
retired). Each is a reusable template:

  producer_reviewer   ONE writer produces, read-only reviewers review it in parallel, the producer
                      revises on the blockers — at most `rounds` rounds. The single writer is the
                      producer (D12).
  fan_out             every member answers the same brief independently and in parallel; the
                      supervisor consolidates, evidence-ranked, keeping each member's dissent.
  bounded_debate      debaters state positions in parallel, then answer each other (fork mode,
                      so each reads the others' arguments) for a bounded number of rounds; the
                      supervisor decides and says why.

`from_panel` turns a shipped Studio panel (packages/specpack/panels/*.yaml) into the template its
`topology` names.

THE SHIPPED SUPERVISORS. The engineering captain's static `ROSTER`
(services/captain_coordination.py) becomes its spec's `subagents` (`captain_subagents`), and the
chief of staff routes what BOSS notices to read-only investigators it starts in the background.
Both ship with `coordination.enabled: false` — multi-agent is opt-in per agent (design §5 Phase
7 risks); turning it on is one field, and their budgets are already in the spec.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from . import spec as S

TOPOLOGIES = ("producer_reviewer", "fan_out", "bounded_debate")
#: A supervisor's default budget for its sub-agents (tokens across every child of one run).
DEFAULT_MAX_TOKENS = 200_000

#: The captain's roster ids → the shipped agents that do that work (packages/specpack/roles).
#: `vp_product` has no shipped agent; the factory driver is the one writer.
ROSTER_AGENTS: dict = {
    "product_manager": "pm",
    "chief_architect": "chief_architect",
    "cto": "cto",
    "qa": "qa",
    "ux_engineer": "design_critic",
    "factory_driver": "software_engineer",
    "reviewer": "adversarial_reviewer",
    "queue_helper": "operations_engineer",
}
ROSTER_WRITERS = frozenset({"factory_driver"})


def _doc(key: str, name: str, *, mission: str, instructions: Sequence[str], subagents: list,
         coordination: dict, sections: Sequence[str] = (), artifact_type: str = "",
         planning: bool = False, tier: str = "frontier") -> dict:
    body: dict = {
        "mission": mission,
        "instructions": list(instructions),
        "model": {"tier": tier},
        "subagents": subagents,
        "coordination": coordination,
    }
    if sections:
        body["output_contract"] = {"sections": list(sections),
                                   **({"artifact_type": artifact_type} if artifact_type else {})}
        body["self_check"] = {"enabled": True, "max_attempts": 2}
    if planning:
        body["planning"] = {"write_todos": True}
    doc = {"apiVersion": S.API_VERSION, "kind": S.KIND,
           "metadata": {"key": key, "name": name}, "spec": body}
    return S.dump_spec(S.load_spec(doc))


def _member(ref: str, description: str = "", **extra: Any) -> dict:
    return {"ref": ref, **({"description": description} if description else {}), **extra}


def _budget(max_tokens: Optional[int], max_usd: Optional[float]) -> dict:
    out: dict = {}
    if max_tokens:
        out["max_tokens"] = int(max_tokens)
    if max_usd:
        out["max_usd"] = float(max_usd)
    return out or {"max_tokens": DEFAULT_MAX_TOKENS}


def producer_reviewer(key: str, *, producer: str, reviewers: Sequence[str], rounds: int = 2,
                      name: str = "", mission: str = "", enabled: bool = True,
                      max_tokens: Optional[int] = None, max_usd: Optional[float] = None,
                      sections: Sequence[str] = ("deliverable", "review", "verdict")) -> dict:
    """One writer, parallel read-only reviewers, at most `rounds` revise-and-review rounds."""
    rounds = max(1, int(rounds))
    reviewers = [r for r in reviewers if r != producer]
    subs = [_member(producer, "the producer: the ONLY sub-agent that writes", writes=True)]
    subs += [_member(r, "a reviewer: reads the producer's work and reports blockers with "
                        "evidence; never changes it") for r in reviewers]
    return _doc(
        key, name or key.replace("_", " ").title(),
        mission=mission or "Deliver one reviewed piece of work: produced once, reviewed "
                           "independently, revised on the blockers.",
        instructions=[
            f"Round 1: `task` the {producer} with the brief. It is the only writer.",
            f"Then `task` every reviewer ({', '.join(reviewers)}) in ONE message, each with "
            "the producer's report; they run in parallel and do not see each other.",
            f"If any reviewer reports a blocker, `task` the {producer} again (mode fork) with "
            f"the blockers. Stop after {rounds} rounds, or sooner when no blocker is left.",
            "Answer with the deliverable, every reviewer's verdict (dissent kept), and the "
            "verdict: accepted, or what is still blocking.",
        ],
        subagents=subs,
        coordination={"enabled": bool(enabled), "topology": "producer_reviewer",
                      "rounds": rounds, "max_subagents": min(50, rounds * (1 + len(reviewers))),
                      "max_parallel": max(1, len(reviewers)), "max_depth": 1,
                      "general_purpose": False, **_budget(max_tokens, max_usd)},
        sections=sections, artifact_type="reviewed_deliverable", planning=True)


def fan_out(key: str, *, members: Sequence[Any], name: str = "", mission: str = "",
            enabled: bool = True, max_tokens: Optional[int] = None,
            max_usd: Optional[float] = None,
            sections: Sequence[str] = ("findings", "dissent", "evidence",
                                       "recommendation")) -> dict:
    """Every member, the same brief, in parallel; consolidated evidence-ranked, dissent kept.
    `members` are agent keys or {ref, description}."""
    subs = [m if isinstance(m, Mapping) else _member(str(m)) for m in members]
    subs = [{**dict(m), "writes": False} for m in subs]
    n = len(subs)
    return _doc(
        key, name or key.replace("_", " ").title(),
        mission=mission or "Get independent views on one question and consolidate them "
                           "without erasing dissent.",
        instructions=[
            "Send EVERY member the same brief with `task` in ONE message: they run in parallel, "
            "independently, and never see each other's answers.",
            "Consolidate: rank findings by the evidence behind them, attribute each to the "
            "member who found it, and keep every dissent in its own section.",
            "Do not ask a member twice; a member that failed is reported as missing.",
        ],
        subagents=subs,
        coordination={"enabled": bool(enabled), "topology": "fan_out", "max_subagents": n,
                      "max_parallel": n, "max_depth": 1, "general_purpose": False,
                      **_budget(max_tokens, max_usd)},
        sections=sections, artifact_type="panel_review")


def bounded_debate(key: str, *, debaters: Sequence[str], rounds: int = 2, name: str = "",
                   mission: str = "", enabled: bool = True, max_tokens: Optional[int] = None,
                   max_usd: Optional[float] = None,
                   sections: Sequence[str] = ("positions", "rebuttals", "decision")) -> dict:
    """Positions in parallel, then `rounds - 1` rounds of rebuttal (fork mode), then a decision."""
    rounds = max(1, int(rounds))
    subs = [_member(d, "a debater: argues one position with evidence") for d in debaters]
    n = len(subs)
    return _doc(
        key, name or key.replace("_", " ").title(),
        mission=mission or "Decide a contested question after a bounded, evidenced debate.",
        instructions=[
            "Round 1: `task` every debater in ONE message for its position (parallel, "
            "independent).",
            f"Rounds 2..{rounds}: `task` each debater again with mode fork, giving it the other "
            "positions to answer. Never more rounds than that.",
            "Decide: say which position wins and why, and keep the losing arguments.",
        ],
        subagents=subs,
        coordination={"enabled": bool(enabled), "topology": "bounded_debate", "rounds": rounds,
                      "max_subagents": min(50, rounds * n), "max_parallel": n, "max_depth": 1,
                      "general_purpose": False, **_budget(max_tokens, max_usd)},
        sections=sections, artifact_type="debate_decision")


def from_panel(panel: Mapping[str, Any], *, enabled: bool = True) -> dict:
    """A Studio panel (packages/specpack/panels/*.yaml) as the supervisor its topology names."""
    version = dict(panel.get("version") or {})
    topology = str(version.get("topology") or "fan_out")
    members = [dict(m) for m in version.get("members") or []]
    key = str(panel.get("id") or panel.get("key"))
    bounds = dict(version.get("bounds") or {})
    contract = dict(version.get("output_contract") or {})
    sections = tuple(contract.get("sections") or ())
    name, mission = str(panel.get("name") or ""), str(panel.get("purpose") or "")
    refs = [str(m.get("agent_key")) for m in members if m.get("agent_key")]
    if topology == "producer_reviewer" and refs:
        return producer_reviewer(key, producer=refs[0], reviewers=refs[1:],
                                 rounds=int(bounds.get("max_rounds") or 2), name=name,
                                 mission=mission, enabled=enabled,
                                 **({"sections": sections} if sections else {}))
    if topology == "bounded_debate":
        return bounded_debate(key, debaters=refs, rounds=int(bounds.get("max_rounds") or 2),
                              name=name, mission=mission, enabled=enabled,
                              **({"sections": sections} if sections else {}))
    return fan_out(key, members=[_member(str(m["agent_key"]), str(m.get("perspective") or ""))
                                 for m in members if m.get("agent_key")],
                   name=name, mission=mission, enabled=enabled,
                   **({"sections": sections} if sections else {}))


# ── The shipped supervisors ────────────────────────────────────────────────────────────────────

def captain_subagents() -> list:
    """The engineering captain's ROSTER as its spec's `subagents` (design §4.5 "Supervisor")."""
    from ..services.captain_coordination import ROSTER

    out: list = []
    for item in ROSTER:
        ref = ROSTER_AGENTS.get(str(item.get("id")))
        if not ref:
            continue
        entry = {"ref": ref, "description": f"{item['name']}: "
                 + ", ".join(str(s) for s in item.get("serves") or [])}
        if item["id"] in ROSTER_WRITERS:
            entry["writes"] = True
        out.append(entry)
    return out


CAPTAIN_COORDINATION = {"enabled": False, "topology": "supervisor", "max_subagents": 6,
                        "max_depth": 1, "max_parallel": 3, "max_tokens": DEFAULT_MAX_TOKENS,
                        "general_purpose": False}

CHIEF_OF_STAFF_SUBAGENTS = [
    {"ref": "sre", "description": "investigates a failing service, alert or deploy", "async": True},
    {"ref": "security_reviewer", "description": "investigates a vulnerability or a risky change",
     "async": True},
    {"ref": "finops_sentinel", "description": "investigates a cost spike", "async": True},
    {"ref": "incident_commander", "description": "triages a live incident's blast radius",
     "async": True},
]
CHIEF_OF_STAFF_COORDINATION = {"enabled": False, "topology": "supervisor", "max_subagents": 4,
                               "max_depth": 1, "max_parallel": 2, "max_tokens": 120_000,
                               "general_purpose": False, "async": True}


__all__ = ["CAPTAIN_COORDINATION", "CHIEF_OF_STAFF_COORDINATION", "CHIEF_OF_STAFF_SUBAGENTS",
           "DEFAULT_MAX_TOKENS", "ROSTER_AGENTS", "TOPOLOGIES", "bounded_debate",
           "captain_subagents", "fan_out", "from_panel", "producer_reviewer"]
