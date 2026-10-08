"""Full auto — and everything it must still refuse.

The operator asked for the artifact's autonomy switch built for real rather than as a control
that changes nothing. This is a genuine reduction in human oversight of a system that spends
money and deploys code, so most of these tests are about what stays impossible.

The design question was never "how do I auto-approve a gate" — that is one UPDATE. It is "what
must remain true afterwards".
"""

import pytest

from boss_engine.services.autonomy import (
    DEFAULT_THRESHOLD_USD,
    FULL_AUTO,
    MAX_AUTO_RISK_TIER,
    SUPERVISED,
    AutonomyPolicy,
    describe,
    evaluate,
    policy_from_row,
)

AUTO = AutonomyPolicy(mode=FULL_AUTO, threshold_usd=500.0, set_by="ceo")


def gate(question="Approve model spend for the review pass?", *, usd=12.0, tier=1,
         status="pending", estimated=False, cost=None):
    summary = cost if cost is not None else ({"usd": usd, "estimated": estimated} if usd is not None else {})
    return {"question": question, "risk_tier": tier, "status": status, "cost_summary": summary}


# --------------------------------------------------------------------------- #
# The default is always the safe one
# --------------------------------------------------------------------------- #
def test_supervised_approves_nothing():
    assert evaluate(gate(), AutonomyPolicy()).approve is False


def test_full_auto_is_never_the_default():
    """It has to be turned on, by someone, on purpose."""
    assert AutonomyPolicy().mode == SUPERVISED
    assert policy_from_row(None).mode == SUPERVISED
    assert policy_from_row({}).mode == SUPERVISED


def test_an_unrecognised_mode_is_treated_as_supervised_not_as_permission():
    """A typo, a partial migration, a corrupted row — none of them may widen the gate."""
    assert policy_from_row({"mode": "FULL-AUTO-PLEASE"}).mode == SUPERVISED
    assert policy_from_row({"mode": "yolo"}).enabled is False


# --------------------------------------------------------------------------- #
# Irreversible acts, whatever they cost
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "Deploy Shorty to production?",
    "Merge PR #212 into main?",
    "Delete the staging database?",
    "Send the launch email to 4,000 subscribers?",
    "Rotate the GitHub credential?",
    "Purchase the domain genevault.bio?",
    "Publish the marketing site?",
    "Grant Sam admin permission?",
    "Terminate the idle GCP fleet?",
    "Charge the customer for the overage?",
])
def test_nothing_that_cannot_be_taken_back_is_ever_auto_approved(question):
    """Money spent on a model can be stopped tomorrow. A deploy cannot be un-deployed and an
    email cannot be un-sent. The threshold is about COST, and cost is not the only consequence.
    """
    out = evaluate(gate(question, usd=1.0, tier=1), AUTO)
    assert out.approve is False
    assert "taken back" in out.reason


def test_the_text_check_beats_a_low_risk_tier():
    """Risk tier is set by whoever wrote the gate and can be missing or wrong. The question
    describes what will actually happen, so it is checked first."""
    out = evaluate(gate("Deploy to production", usd=0.01, tier=0), AUTO)
    assert out.approve is False


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #
def test_a_small_reversible_spend_approves_itself():
    """The thing Full auto is actually for: removing the wait on decisions that are small,
    reversible and well understood."""
    out = evaluate(gate(usd=12.0), AUTO)
    assert out.approve is True
    assert "$12.00" in out.reason and "$500.00" in out.reason


def test_over_the_threshold_waits():
    out = evaluate(gate(usd=501.0), AUTO)
    assert out.approve is False
    assert "over the" in out.reason


def test_exactly_at_the_threshold_is_allowed():
    assert evaluate(gate(usd=500.0), AUTO).approve is True


def test_a_cost_that_cannot_be_read_is_not_a_small_cost():
    """The most important refusal here. A gate with no cost recorded is unknown, not free."""
    for missing in ({}, {"note": "tbd"}, {"usd": "lots"}, None):
        out = evaluate(gate(usd=None, cost=missing if missing is not None else {}), AUTO)
        assert out.approve is False, missing
        assert "no cost recorded" in out.reason or "estimate" in out.reason


def test_an_estimated_cost_is_not_a_basis_for_spending():
    """An estimate is a guess, and a guess is not a reason to spend without asking."""
    out = evaluate(gate(usd=5.0, estimated=True), AUTO)
    assert out.approve is False
    assert "estimate" in out.reason


def test_a_cost_in_micros_is_understood():
    assert evaluate(gate(usd=None, cost={"cost_usd_micros": 4_000_000}), AUTO).approve is True


# --------------------------------------------------------------------------- #
# Risk tier
# --------------------------------------------------------------------------- #
def test_a_dangerous_action_is_refused_however_cheap():
    out = evaluate(gate(usd=0.01, tier=MAX_AUTO_RISK_TIER + 1), AUTO)
    assert out.approve is False
    assert "risk tier" in out.reason


def test_the_configured_ceiling_cannot_be_raised_past_the_hard_limit():
    """A policy row asking for tier 9 does not get tier 9."""
    assert policy_from_row({"mode": FULL_AUTO, "max_risk_tier": 9}).max_risk_tier == MAX_AUTO_RISK_TIER


def test_an_absurd_threshold_is_clamped():
    assert policy_from_row({"mode": FULL_AUTO, "threshold_usd": -5}).threshold_usd == 0.0
    assert policy_from_row({"mode": FULL_AUTO, "threshold_usd": 10**9}).threshold_usd <= 100_000.0


# --------------------------------------------------------------------------- #
# Already-decided gates
# --------------------------------------------------------------------------- #
def test_a_resolved_gate_is_left_alone():
    for status in ("approved", "rejected", "delegated", "revision_requested"):
        assert evaluate(gate(status=status), AUTO).approve is False


# --------------------------------------------------------------------------- #
# The trail
# --------------------------------------------------------------------------- #
def test_an_auto_approval_records_that_no_human_saw_it():
    """Later, "approved" with an actor is ambiguous. This is not."""
    record = evaluate(gate(usd=3.0), AUTO).as_record(AUTO)
    assert record["auto_approved"] is True
    assert record["human_reviewed"] is False
    assert record["mode"] == FULL_AUTO
    assert record["threshold_usd"] == 500.0
    assert record["policy_set_by"] == "ceo"


def test_a_refusal_always_says_why():
    """A refusal with no reason is indistinguishable from a bug, and the operator cannot tell
    whether to change the policy or the gate."""
    for g in (gate("Deploy it"), gate(usd=9999.0), gate(usd=None, cost={}),
              gate(tier=5), gate(status="approved")):
        out = evaluate(g, AUTO)
        assert out.approve is False
        assert len(out.reason) > 10, g


# --------------------------------------------------------------------------- #
# What the console is told
# --------------------------------------------------------------------------- #
def test_full_auto_is_described_by_what_it_still_refuses():
    """A mode described only by what it permits invites the assumption that it permits
    everything."""
    text = describe(AUTO)["detail"]
    assert "$500" in text
    assert "still wait for you" in text
    for word in ("Deploys", "merges", "sends"):
        assert word in text


def test_supervised_is_described_plainly():
    d = describe(AutonomyPolicy())
    assert d["mode"] == "Supervised"
    assert "Full auto" in d["modes_available"]


# --------------------------------------------------------------------------- #
# Applying the policy — the only place an approval is written
# --------------------------------------------------------------------------- #
class FakeConn:
    def __init__(self, policy_row=None, pending=()):
        self.policy_row = policy_row
        self.pending = list(pending)
        self.updates = []
        self.statements = []

    def execute(self, statement, params=None):
        raw = str(statement)
        self.statements.append((raw, dict(params or {})))
        low = raw.lower()
        conn = self
        if "update decisions" in low:
            self.updates.append(dict(params or {}))
            rows = [{"id": (params or {}).get("id")}]           # RETURNING id: it was approved
        elif "from console_autonomy" in low:
            rows = [conn.policy_row] if conn.policy_row else []
        elif "insert into console_autonomy" in low:
            rows = []
        elif "for update" in low:                              # decisions.approve_by_policy's read
            rows = [r for r in conn.pending if r["id"] == (params or {}).get("id")]
        else:
            rows = conn.pending
        captured = [r for r in rows if r]

        class _R:
            def mappings(self):
                return self

            def all(self):
                return [dict(r) for r in captured]

            def first(self):
                return dict(captured[0]) if captured else None

        return _R()


def pending(did, question="Approve model spend?", usd=12.0, tier=1):
    return {"id": did, "question": question, "risk_tier": tier,
            "cost_summary": {"usd": usd}, "status": "pending"}


def test_supervised_is_inert_not_merely_restrictive():
    """It must not even LOOK at the pending gates.

    An earlier version of this test asserted only that nothing was approved, which passed with
    the enabled-check deleted — because evaluate() independently refuses under Supervised. Two
    independent refusals is good design and a bad test: it proved the second one worked while
    claiming to prove the first.

    The observable difference is that Supervised never reads the decisions table at all.
    """
    from boss_engine.services.autonomy import apply_policy

    conn = FakeConn(pending=[pending("d1")])
    out = apply_policy(conn, "p")
    assert out["approved"] == []
    assert conn.updates == []
    assert not [s for s, _ in conn.statements if "FROM decisions" in s], (
        "supervised mode must not query pending gates at all"
    )


def test_evaluate_refuses_under_supervised_even_if_reached():
    """The second of the two independent refusals, tested on its own so neither can hide the
    other's failure."""
    assert evaluate(gate(usd=1.0), AutonomyPolicy()).approve is False


AUTO_ROW = {"mode": FULL_AUTO, "threshold_usd": 500.0, "max_risk_tier": 1,
            "set_by": "ceo", "set_at": None}


def test_a_qualifying_gate_is_approved_and_marked_as_unreviewed():
    from boss_engine.services.autonomy import apply_policy

    conn = FakeConn(policy_row=AUTO_ROW, pending=[pending("d1", usd=12.0)])
    out = apply_policy(conn, "p")
    assert [a["id"] for a in out["approved"]] == ["d1"]
    update = conn.updates[0]
    # Named so the trail can never read as a person: "approved by ceo" and "approved by the
    # policy ceo turned on" are different facts.
    assert update["actor"] == "autonomy:full_auto"
    assert "no human reviewed this" in update["note"]


def test_the_update_cannot_resolve_an_already_resolved_gate():
    """A concurrent human approval must win; the UPDATE is guarded on status."""
    from boss_engine.services.autonomy import apply_policy

    conn = FakeConn(policy_row=AUTO_ROW, pending=[pending("d1")])
    apply_policy(conn, "p")
    update_sql = [s for s, _ in conn.statements if "UPDATE decisions" in s][0]
    assert "status = 'pending'" in update_sql


def test_refusals_are_returned_with_their_reasons():
    """A run reporting only what it approved is unreviewable: the operator cannot tell whether
    the policy is working, too tight, or silently doing nothing."""
    from boss_engine.services.autonomy import apply_policy

    conn = FakeConn(policy_row=AUTO_ROW, pending=[
        pending("d1", question="Deploy to production?"),
        pending("d2", usd=9_999.0),
    ])
    out = apply_policy(conn, "p")
    assert out["approved"] == []
    assert {d["id"] for d in out["declined"]} == {"d1", "d2"}
    assert all(d["reason"] for d in out["declined"])
    assert conn.updates == []


def test_the_run_is_bounded():
    """An unbounded loop would, on a bad policy, approve everything pending before anyone could
    look — and the point of a threshold is that it is a brake."""
    from boss_engine.services.autonomy import apply_policy

    conn = FakeConn(policy_row=AUTO_ROW, pending=[])
    apply_policy(conn, "p", limit=5)
    select = [p for s, p in conn.statements if "FROM decisions" in s][0]
    assert select["lim"] == 5


def test_setting_the_mode_requires_an_actor():
    """An autonomy change with no owner is the one kind this system must never accept: it is
    the act that makes every later act unattributable."""
    from boss_engine.services.autonomy import set_policy

    with pytest.raises(ValueError, match="actor"):
        set_policy(FakeConn(), "p", mode=FULL_AUTO, actor="")


def test_setting_an_unknown_mode_is_refused():
    from boss_engine.services.autonomy import set_policy

    with pytest.raises(ValueError, match="mode"):
        set_policy(FakeConn(), "p", mode="yolo", actor="ceo")


def test_changing_the_mode_records_who_did_it():
    from boss_engine.services.autonomy import set_policy

    conn = FakeConn()
    set_policy(conn, "p", mode=FULL_AUTO, actor="ceo", threshold_usd=250)
    insert = [p for s, p in conn.statements if "INSERT INTO console_autonomy" in s][0]
    assert insert["a"] == "ceo"
    assert insert["m"] == FULL_AUTO
    assert insert["t"] == 250
