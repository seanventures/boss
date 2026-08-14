"""Agent process — PRD handoff chain, reviews, certify, accept."""

from boss_engine.services.agent_process import (
    PRD_TO_ACCEPTED_SPEC,
    improve_prd,
    review_prd,
    validate_process_spec,
)


THIN_PRD = "Build a thing."
RICH_PRD = """
# HookWatch PRD
Users create webhook inboxes. Operators capture events.
Acceptance: must create an inbox, must verify replay.
Data model: inbox, event. API routes documented.
Risks: no arbitrary egress. Security: allowlisted replay only.
Success metric: operator captures one event end to end.
Deploy via one-command verify.
"""


def test_default_spec_validates_and_covers_the_requested_handoffs():
    spec = validate_process_spec(PRD_TO_ACCEPTED_SPEC)
    ids = [step["id"] for step in spec["steps"]]
    assert ids[0] == "intake_prd"
    assert "review_architect" in ids and "review_cto" in ids and "review_vp" in ids
    assert ids[-1] == "captain_accept"
    actions = {step["action"] for step in spec["steps"]}
    assert actions >= {
        "accept_prd", "review_prd", "improve_prd", "build",
        "qa_review", "ux_review", "verify_build", "certify_spec", "accept_work",
    }


def test_role_reviews_are_deterministic_and_do_not_invent_scores():
    thin = review_prd(THIN_PRD, "chief_architect")
    assert thin["verdict"] == "revise"
    assert thin["failed"]
    rich = review_prd(RICH_PRD, "chief_architect")
    assert rich["verdict"] == "approve"
    assert review_prd(RICH_PRD, "cto")["verdict"] == "approve"
    assert review_prd(RICH_PRD, "vp_product")["verdict"] == "approve"


def test_improve_prd_appends_review_findings():
    reviews = [review_prd(THIN_PRD, "cto"), review_prd(THIN_PRD, "vp_product")]
    improved = improve_prd(THIN_PRD, reviews)
    assert "Improvements required by review" in improved
    assert "cto" in improved


def test_panel_certify_and_accept_require_prior_handoffs():
    from boss_engine.services.agent_process import AgentProcessService

    failed = AgentProcessService._panel_certify({}, {})
    assert failed["verdict"] == "not_certified"
    done = {
        "qa_check": {"status": "succeeded"},
        "ux_review": {"status": "succeeded"},
        "captain_verify": {"status": "succeeded"},
    }
    planned = AgentProcessService._panel_certify(done, {"simulated": True})
    assert planned["verdict"] == "certified_planned"
    live = AgentProcessService._panel_certify(done, {"build_status": "completed", "tests_passed": True})
    assert live["verdict"] == "certified"


def test_custom_spec_rejects_unknown_roles_and_cycles_of_missing_targets():
    try:
        validate_process_spec({
            "entry": "a",
            "steps": [{"id": "a", "kind": "role", "role": "wizard", "action": "accept_prd"}],
        })
    except ValueError as exc:
        assert "role" in str(exc)
    else:
        raise AssertionError("unknown role must fail")
    try:
        validate_process_spec({
            "entry": "a",
            "steps": [{
                "id": "a", "kind": "role", "role": "qa", "action": "qa_review", "next": ["missing"],
            }],
        })
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("missing next must fail")
