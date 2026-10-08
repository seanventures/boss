"""Full auto: gates that approve themselves, within limits that are actually enforced.

The artifact offers a switch — Supervised or Full auto, "gates auto-approve under $500". BOSS
has only ever had Supervised, and the operator asked for the real thing rather than a control
that changes nothing.

This is a genuine reduction in human oversight of a system that spends money and deploys code,
so it is written defensively. The design question was not "how do I auto-approve a gate" — that
is one UPDATE — it is "what must remain impossible after I do".

WHAT FULL AUTO MAY NEVER DO
---------------------------
1. Approve anything IRREVERSIBLE. A deploy, a merge, a deletion, an external send, a payment,
   a credential rotation. Money spent on a model can be stopped tomorrow; a deploy to
   production cannot be un-deployed, and an email cannot be un-sent. The threshold is about
   COST, and cost is not the only kind of consequence.
2. Approve a gate whose cost it cannot READ. An unknown cost is not a small cost. A gate with
   no cost recorded stays with the human, always.
3. Approve above the threshold, obviously — but also approve a gate whose cost is only an
   estimate rather than a measured figure.
4. Approve anything at a risk tier above the configured ceiling, independently of cost. A
   cheap action can still be a dangerous one.
5. Approve silently. Every auto-approval writes a decision record naming the mode, the
   threshold in force, and that no human saw it — so the trail shows what was decided FOR the
   operator, not just what was decided.

The result: Full auto removes the wait on small, reversible, well-understood spend decisions,
and nothing else. That is the honest version of the artifact's promise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

SUPERVISED = "supervised"
FULL_AUTO = "full_auto"
MODES = (SUPERVISED, FULL_AUTO)

#: Ceiling on a single auto-approval, in US dollars. The artifact says $500; this is the
#: default and is configurable per project, because $500 means something different to a
#: two-person company than to a funded one.
DEFAULT_THRESHOLD_USD = 500.0

#: Nothing above this risk tier is ever auto-approved, whatever it costs. A tier-3 action is
#: one someone classified as dangerous, and cheapness does not make it safe.
MAX_AUTO_RISK_TIER = 1

#: Questions that describe an act which cannot be taken back. Matched on the gate's own text,
#: because the risk tier is set by whoever wrote the gate and may be wrong or missing, and this
#: is the backstop for when it is.
#:
#: Deliberately broad. A false positive costs a human ten seconds; a false negative deploys
#: something nobody approved.
IRREVERSIBLE = re.compile(
    r"\b(deploy|deploys|deployment|ship|release|publish|merge|merges|merging"
    r"|delete|deletes|destroy|drop|purge|wipe|teardown|terminate"
    r"|send|email|notify|post|tweet|announce"
    r"|pay|payment|charge|purchase|buy|refund|transfer"
    r"|rotate|revoke|grant|permission|credential|secret|key"
    r"|production|prod\b|customer|external)\b",
    re.I,
)


@dataclass(frozen=True)
class AutonomyPolicy:
    """What the project has been configured to allow."""

    mode: str = SUPERVISED
    threshold_usd: float = DEFAULT_THRESHOLD_USD
    max_risk_tier: int = MAX_AUTO_RISK_TIER
    #: Who turned it on. An autonomy change is itself an act with an owner.
    set_by: Optional[str] = None
    set_at: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self.mode == FULL_AUTO


@dataclass(frozen=True)
class AutoDecision:
    """Whether a gate may approve itself, and why — the reason is the point.

    A refusal that does not say why is indistinguishable from a bug, and an approval that does
    not say why cannot be audited.
    """

    approve: bool
    reason: str

    def as_record(self, policy: AutonomyPolicy) -> dict[str, Any]:
        """What gets written alongside the decision, so the trail is readable later."""
        return {
            "auto_approved": self.approve,
            "reason": self.reason,
            "mode": policy.mode,
            "threshold_usd": policy.threshold_usd,
            "max_risk_tier": policy.max_risk_tier,
            "policy_set_by": policy.set_by,
            # Stated explicitly. Later, "approved" with an actor is ambiguous; this is not.
            "human_reviewed": False,
        }


def _cost_of(decision: dict) -> tuple[Optional[float], bool]:
    """(dollars, is_measured) for a gate.

    Returns (None, False) when the cost cannot be read. An unknown cost is not a small cost,
    and the caller must treat it as a refusal rather than a zero.
    """
    summary = decision.get("cost_summary") or {}
    if not isinstance(summary, dict):
        return None, False

    for key in ("usd", "amount_usd", "total_usd", "cost_usd"):
        if key in summary:
            try:
                return float(summary[key]), not bool(summary.get("estimated"))
            except (TypeError, ValueError):
                return None, False

    micros = summary.get("cost_usd_micros")
    if micros is not None:
        try:
            return float(micros) / 1_000_000, not bool(summary.get("estimated"))
        except (TypeError, ValueError):
            return None, False
    return None, False


def evaluate(decision: dict, policy: AutonomyPolicy) -> AutoDecision:
    """May this gate approve itself?

    Every branch here is a refusal except the last. That ordering is deliberate: the safe answer
    is the default, and a new gate shape that nobody anticipated falls through to a human rather
    than through the net.
    """
    if not policy.enabled:
        return AutoDecision(False, "supervised mode — every gate waits for you")

    if str(decision.get("status") or "") != "pending":
        return AutoDecision(False, "already resolved")

    question = str(decision.get("question") or "")

    # THE BACKSTOP, CHECKED FIRST. Risk tier is set by whoever wrote the gate and can be
    # missing or wrong; the text describes what will actually happen.
    match = IRREVERSIBLE.search(question)
    if match:
        return AutoDecision(
            False,
            f"cannot be taken back — mentions {match.group(0)!r}; a human decides this one",
        )

    tier = int(decision.get("risk_tier") or 1)
    if tier > policy.max_risk_tier:
        # Cheapness does not make a dangerous action safe.
        return AutoDecision(False, f"risk tier {tier} is above the auto ceiling {policy.max_risk_tier}")

    cost, measured = _cost_of(decision)
    if cost is None:
        # An unknown cost is not a small cost.
        return AutoDecision(False, "no cost recorded — cannot judge it against the threshold")
    if not measured:
        # An estimate is a guess, and a guess is not a basis for spending without asking.
        return AutoDecision(False, f"cost ${cost:,.2f} is an estimate, not a measured figure")
    if cost > policy.threshold_usd:
        return AutoDecision(False, f"${cost:,.2f} is over the ${policy.threshold_usd:,.2f} threshold")

    return AutoDecision(
        True,
        f"reversible, risk tier {tier}, ${cost:,.2f} under the "
        f"${policy.threshold_usd:,.2f} threshold",
    )


def policy_from_row(row: Optional[dict]) -> AutonomyPolicy:
    """Read the stored policy, defaulting to Supervised.

    A missing row means Supervised. Full auto is never the default and never arrives by
    accident: it has to be turned on, by someone, on purpose.
    """
    if not row:
        return AutonomyPolicy()
    mode = str(row.get("mode") or SUPERVISED)
    if mode not in MODES:
        # An unrecognised mode is treated as Supervised rather than as permission.
        mode = SUPERVISED
    try:
        threshold = float(row.get("threshold_usd") or DEFAULT_THRESHOLD_USD)
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD_USD
    try:
        tier = int(row.get("max_risk_tier") or MAX_AUTO_RISK_TIER)
    except (TypeError, ValueError):
        tier = MAX_AUTO_RISK_TIER
    return AutonomyPolicy(
        mode=mode,
        # A negative or absurd threshold must not widen the gate.
        threshold_usd=max(0.0, min(threshold, 100_000.0)),
        max_risk_tier=max(0, min(tier, MAX_AUTO_RISK_TIER)),
        set_by=row.get("set_by"),
        set_at=row.get("set_at").isoformat() if hasattr(row.get("set_at"), "isoformat") else row.get("set_at"),
    )


def describe(policy: AutonomyPolicy) -> dict[str, Any]:
    """What the console shows, in the operator's terms."""
    if not policy.enabled:
        return {
            "mode": "Supervised",
            "detail": "Spend & deploys pause for you.",
            "modes_available": ["Supervised", "Full auto"],
            "threshold_usd": policy.threshold_usd,
        }
    return {
        "mode": "Full auto",
        # Says what it does AND what it still will not do. A mode described only by what it
        # permits invites the assumption that it permits everything.
        "detail": (
            f"Reversible gates under ${policy.threshold_usd:,.0f} approve themselves. "
            "Deploys, merges, sends and anything irreversible still wait for you."
        ),
        "modes_available": ["Supervised", "Full auto"],
        "threshold_usd": policy.threshold_usd,
        "set_by": policy.set_by,
        "set_at": policy.set_at,
    }


# --------------------------------------------------------------------------- #
# Applying the policy
#
# Separated from evaluate() so the judgement stays pure and testable, and so the only place
# that writes an approval is small enough to read in one sitting.
# --------------------------------------------------------------------------- #

def load_policy(connection, project_id: str) -> AutonomyPolicy:
    """The project's policy. A missing row is Supervised."""
    from sqlalchemy import text as _sql

    row = connection.execute(
        _sql(
            """
            SELECT mode, threshold_usd, max_risk_tier, set_by, set_at
            FROM console_autonomy
            WHERE project_id = :p
            """
        ),
        {"p": project_id},
    ).mappings().first()
    return policy_from_row(dict(row) if row else None)


def set_policy(connection, project_id: str, *, mode: str, actor: str,
               threshold_usd: Optional[float] = None,
               max_risk_tier: Optional[int] = None) -> AutonomyPolicy:
    """Change the mode. Requires an actor, always.

    An autonomy change with no owner is the one kind this system must never accept: it is the
    act that makes every later act unattributable.
    """
    from sqlalchemy import text as _sql

    if not str(actor or "").strip():
        raise ValueError("an autonomy change requires an actor")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")

    current = load_policy(connection, project_id)
    threshold = current.threshold_usd if threshold_usd is None else float(threshold_usd)
    tier = current.max_risk_tier if max_risk_tier is None else int(max_risk_tier)

    connection.execute(
        _sql(
            """
            INSERT INTO console_autonomy
              (project_id, mode, threshold_usd, max_risk_tier, set_by, set_at)
            VALUES (:p, :m, :t, :r, :a, now())
            ON CONFLICT (project_id) DO UPDATE SET
              mode = EXCLUDED.mode,
              threshold_usd = EXCLUDED.threshold_usd,
              max_risk_tier = EXCLUDED.max_risk_tier,
              set_by = EXCLUDED.set_by,
              set_at = now()
            """
        ),
        {"p": project_id, "m": mode, "t": threshold, "r": tier, "a": actor},
    )
    return load_policy(connection, project_id)


def apply_policy(connection, project_id: str, *, limit: int = 25) -> dict[str, Any]:
    """Approve the gates that qualify, and say why each other one did not.

    THE REFUSALS ARE RETURNED TOO. A run that reports only what it approved is unreviewable:
    the operator cannot tell whether the policy is working, too tight, or silently doing
    nothing because every gate is missing a cost.

    Bounded per call. An unbounded loop here would, on a bad policy, approve everything pending
    before anyone could look — and the whole point of a threshold is that it is a brake.
    """
    from sqlalchemy import text as _sql

    policy = load_policy(connection, project_id)
    if not policy.enabled:
        return {"mode": policy.mode, "approved": [], "declined": [],
                "note": "supervised — nothing is auto-approved"}

    rows = [dict(r) for r in connection.execute(
        _sql(
            """
            SELECT id, question, risk_tier, cost_summary, status
            FROM decisions
            WHERE project_id = :p AND status = 'pending'
            ORDER BY created_at ASC
            LIMIT :lim
            """
        ),
        {"p": project_id, "lim": int(limit)},
    ).mappings().all()]

    approved, declined = [], []
    for row in rows:
        verdict = evaluate(row, policy)
        if not verdict.approve:
            declined.append({"id": row["id"], "question": str(row.get("question") or "")[:160],
                             "reason": verdict.reason})
            continue

        record = verdict.as_record(policy)
        # C-1: through decisions.approve_by_policy, which refuses any tier that needs a seat or
        # a second person (those only ever go through decisions.transition).
        from .decisions import approve_by_policy

        if not approve_by_policy(
            connection, row["id"], project_id=project_id,
            # Named so the trail never reads as a person. "approved by ceo" and "approved
            # by the policy ceo turned on" are different facts.
            actor=f"autonomy:{policy.mode}",
            note=f"AUTO-APPROVED (no human reviewed this): {verdict.reason}",
        ):
            declined.append({"id": row["id"], "question": str(row.get("question") or "")[:160],
                             "reason": "needs a seat or a second person; a person approves it"})
            continue
        approved.append({"id": row["id"], "question": str(row.get("question") or "")[:160],
                         "reason": verdict.reason, "record": record})

    return {
        "mode": policy.mode,
        "threshold_usd": policy.threshold_usd,
        "approved": approved,
        "declined": declined,
        "note": (
            f"{len(approved)} auto-approved, {len(declined)} left for you"
            if approved or declined else "no pending gates"
        ),
    }
