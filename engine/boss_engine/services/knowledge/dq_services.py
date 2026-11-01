"""The DQ pipeline's two shared services (design §4.4; the consultant's "Shared services" row).

CONTEXT CURATOR — ranks and trims the context every LLM step is given to that step's token
budget, and REPORTS WHAT IT DROPPED: a step never silently reasons over less than it was told it
had. Deterministic: sections are kept in priority order (a `must` section is never dropped — if
the must-haves alone overrun the budget, they are kept and the overrun is said), items within a
section in the order the caller ranked them, and whatever does not fit is listed by section with
how many items and roughly how many tokens went. Routes nothing.

PROFILE SERVICE — the deterministic exception profile of a data platform (A-D41): through the
project's registered Olympus connector, the rule's 30-day baseline failure rate, its spread and
the last 7 COBs, and how far today's measure sits from it. A platform or rule Olympus does not
profile is said so, never estimated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy.engine import Connection

#: Roughly four characters a token — the same rule of thumb the budgeted learning context uses.
CHARS_PER_TOKEN = 4
DEFAULT_BUDGET_TOKENS = 6000


def tokens(text_value: str) -> int:
    return max(1, (len(text_value or "") + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


@dataclass
class Section:
    """One block of context: a heading and its items (already ranked by the caller)."""
    name: str
    items: Sequence[str]
    priority: int = 50
    must: bool = False
    intro: str = ""


@dataclass
class Curated:
    text: str
    budget: int
    used: int
    kept: dict[str, int] = field(default_factory=dict)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    over_budget: bool = False

    def report(self) -> dict[str, Any]:
        return {"service": "context_curator", "budget_tokens": self.budget,
                "used_tokens": self.used, "kept": dict(self.kept), "dropped": list(self.dropped),
                "over_budget": self.over_budget}

    def note(self) -> str:
        if not self.dropped:
            return f"Context Curator kept everything ({self.used} of {self.budget} tokens)."
        parts = "; ".join(f"{d['section']}: {d['items']} item(s), ~{d['tokens']} tokens"
                          for d in self.dropped)
        return (f"Context Curator trimmed to {self.used} of {self.budget} tokens and dropped "
                f"{parts}.")


def curate(sections: Sequence[Section], budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> Curated:
    """Pack `sections` into `budget_tokens`, highest priority first, and say what was dropped."""
    budget = max(200, int(budget_tokens or DEFAULT_BUDGET_TOKENS))
    ordered = sorted(enumerate(sections), key=lambda pair: (not pair[1].must,
                                                            -pair[1].priority, pair[0]))
    chosen: dict[int, list[str]] = {}
    used = 0
    kept: dict[str, int] = {}
    dropped: list[dict[str, Any]] = []
    over = False
    for index, section in ordered:
        head = f"{section.name.upper()}:" + (f" {section.intro}" if section.intro else "")
        items = [str(i) for i in section.items if str(i or "").strip()]
        if not items and not section.intro:
            continue
        head_cost = tokens(head) + 1
        if not section.must and used + head_cost > budget:
            dropped.append({"section": section.name, "items": len(items),
                            "tokens": head_cost + sum(tokens(i) + 1 for i in items),
                            "why": "no room left in the budget"})
            continue
        used += head_cost
        take: list[str] = []
        for n, item in enumerate(items):
            cost = tokens(item) + 1
            if used + cost > budget and not section.must:
                rest = items[n:]
                dropped.append({"section": section.name, "items": len(rest),
                                "tokens": sum(tokens(i) + 1 for i in rest),
                                "why": "lowest-ranked items past the budget"})
                break
            used += cost
            take.append(item)
        if section.must and used > budget:
            over = True
        chosen[index] = [head, *take]
        kept[section.name] = len(take)
    blocks = ["\n".join(chosen[i]) for i in sorted(chosen)]
    return Curated(text="\n\n".join(blocks), budget=budget, used=used, kept=kept,
                   dropped=dropped, over_budget=over)


# ── Profile Service ─────────────────────────────────────────────────────────────────────────

def exception_profile(connection: Connection, project_id: str, *, platform: str, rule_id: str,
                      measured: Optional[float] = None,
                      calls: Optional[list[dict[str, Any]]] = None
                      ) -> tuple[Optional[dict[str, Any]], str]:
    """(profile, "") or (None, why) — the rule's baseline on its platform, via Olympus."""
    from .dq_mcp import call

    args: dict[str, Any] = {"platform": platform, "rule_id": rule_id}
    if measured is not None:
        args["measured"] = measured
    result, gap = call(connection, project_id, "olympus", "get_exception_profile", args, calls)
    if result is None:
        return None, gap
    if not result.get("profiled"):
        return None, (f"Olympus does not profile {rule_id} on {platform}: "
                      f"{result.get('note') or 'no baseline'}")
    return result, ""


def describe_profile(profile: Mapping[str, Any]) -> str:
    base = profile.get("baseline_rate_30d")
    measured = profile.get("measured")
    out = (f"30-day baseline {base} (spread {profile.get('stdev_30d')}), last 7 COBs "
           f"{profile.get('last_7_cob')}")
    if measured is not None:
        out += (f"; measured {measured} = {profile.get('ratio_to_baseline')}x the baseline, "
                f"z = {profile.get('z_score')}")
    return out
