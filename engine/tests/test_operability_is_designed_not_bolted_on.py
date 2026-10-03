"""Operability moves into the design, and readiness becomes a step before production.

Two changes, one idea: the question "can anyone RUN this?" belongs before launch, not after
the first bad night.

WHY IT WAS NEEDED. Observability was a rule the PLATFORM imposed on the builder — expose
/metrics, these four series — and never a thing the DESIGN discussed. The panel argued about
features; operational requirements arrived, if at all, as a paragraph the builder was told to
obey. Three apps then reached a host with no metrics at all, and a monitoring page linking to
dashboards for data nobody was collecting.

Naming the signal for a feature is cheapest while that feature is still being argued about, and
the operations lens is the person in the room whose job that is.

AND PREVIEW IS NOT READINESS. Preview answers "does it do the thing". It says nothing about
whether a failing feature is visible, whether a bad release can be undone, or what the first
command is at 3am. PLRR sits between them and closes those gaps IN CODE — a review that only
files findings leaves the operator to do the work the review exists to prevent.
"""
from __future__ import annotations

from boss_engine.services.meeting_service import DEFAULT_ROLE_KEYS, ROLE_CATALOG
from boss_engine.services.workflow_capabilities import CAPABILITIES
from boss_engine.services.workflow_catalogue import CATALOGUE


def _standard() -> dict:
    return next(e for e in CATALOGUE if e["key"] == "standard_product_delivery")


class TestOperationsIsInTheRoom:
    def test_the_role_exists(self):
        assert "operations" in ROLE_CATALOG

    def test_it_is_staffed_by_default(self):
        """A role nobody staffs is a role that does not exist."""
        assert "operations" in DEFAULT_ROLE_KEYS

    def test_its_lens_demands_a_signal_per_feature(self):
        lens = ROLE_CATALOG["operations"]["lens"]
        assert "for EVERY feature" in lens
        assert "a feature with no signal is not finished" in lens

    def test_it_asks_for_concrete_series_not_add_monitoring(self):
        """"Add monitoring" is the advice that produced apps with no metrics."""
        lens = ROLE_CATALOG["operations"]["lens"]
        assert "verifications_completed_total" in lens
        assert "beats 'add monitoring'" in lens

    def test_it_covers_rollback_and_first_diagnostic_step(self):
        lens = ROLE_CATALOG["operations"]["lens"]
        assert "rolled back" in lens
        assert "first diagnostic step" in lens

    def test_it_is_asked_which_failures_would_be_invisible(self):
        """The gap that costs a night is the one nobody named."""
        assert "INVISIBLE" in ROLE_CATALOG["operations"]["lens"]


class TestReadinessHasItsOwnStep:
    def test_plrr_is_a_declared_capability(self):
        assert "plrr" in CAPABILITIES
        assert CAPABILITIES["plrr"].is_async, "it starts repair work and waits for it"

    def test_it_runs_after_preview_and_before_deploy(self):
        steps = [s["id"] for s in _standard()["steps"]]
        assert steps.index("preview") < steps.index("plrr") < steps.index("deploy")

    def test_the_app_is_previewed_again_after_readiness_changes_code(self):
        """PLRR writes code. Deploying straight after would ship something nobody looked at,
        which voids the gate the first preview exists to be."""
        steps = [s["id"] for s in _standard()["steps"]]
        assert steps.index("plrr") < steps.index("deploy")
        assert "preview_final" not in steps

    def test_it_awaits_a_code_task_like_build_does(self):
        from boss_engine.services.workflow_capability_runner import _AWAIT_KINDS

        assert _AWAIT_KINDS["plrr"][0] == "code_task"


class TestTheReviewFixesRatherThanReports:
    def _brief(self) -> str:
        import inspect

        from boss_engine.services import workflow_capability_runner

        return inspect.getsource(workflow_capability_runner._plrr_brief)

    def test_it_is_told_to_change_code_not_write_a_report(self):
        assert "not to write a report" in self._brief()

    def test_it_reviews_against_this_run_s_own_criteria(self):
        """A generic checklist verifies somebody else's app."""
        brief = self._brief()
        assert "ACCEPTANCE CRITERIA" in brief
        assert "For every acceptance criterion above" in brief

    def test_it_covers_the_six_things_that_cost_a_night(self):
        brief = self._brief()
        for topic in ("SIGNAL PER FEATURE", "/metrics", "HEALTH AND READINESS",
                      "FAILURE BEHAVIOUR", "DIAGNOSABILITY", "ROLLBACK"):
            assert topic in brief, topic

    def test_every_closed_gap_gets_a_failing_test_first(self):
        brief = self._brief()
        assert "gets a test that fails without the fix" in brief

    def test_it_may_not_weaken_the_existing_suite(self):
        assert "Do NOT weaken or delete existing tests" in self._brief()

    def test_it_must_say_what_it_could_not_close(self):
        """Silence about a gap is how a gap reaches production."""
        assert "the ones you did not" in self._brief()

    def test_a_run_that_built_nothing_is_not_blocked(self):
        import inspect

        from boss_engine.services import workflow_capability_runner

        body = inspect.getsource(workflow_capability_runner._plrr_step)
        assert "not_applicable" in body
