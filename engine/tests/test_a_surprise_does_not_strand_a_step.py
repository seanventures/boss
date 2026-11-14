"""An unexpected error must fail the step, not leave it leased forever.

Five deploy steps sat in `leased` for fifteen hours, showing the operator a running timer while
nothing served them. The log said:

    capability runner skipped run wfr_84f9cef08c204c8c8fe4 (non-fatal): 'deploy'

`'deploy'` is a stringified KeyError. It names neither the exception type nor where it came
from, so the message that was supposed to explain the stall was itself unreadable — and the
handler that produced it released nothing.

Only CapabilityExecutionError failed the step and released the lease. Every other exception
escaped to the run-level handler, which logs and moves to the next run, leaving a claimed step
claimed by a worker that has already gone.
"""

from __future__ import annotations

import inspect

from boss_engine.services import workflow_capability_runner as runner


def _tick_source() -> str:
    return "".join(map(inspect.getsource, runner.TICK_PARTS))


class TestAClaimedStepIsAlwaysReleased:
    @staticmethod
    def _broad_handler_block() -> str:
        """From the step-level catch-all to the run-level handler that used to swallow it.

        Bounded by the code either side rather than a character count: the comment explaining
        why this exists is longer than any window worth guessing at.
        """
        source = _tick_source()
        start = source.index("except Exception as error:  # noqa: BLE001")
        end = source.index("capability runner skipped run")
        return source[start:end]

    def test_an_unexpected_error_fails_the_step(self):
        assert "workflows.fail_step(" in self._broad_handler_block(), (
            "an exception that is not a CapabilityExecutionError must still release the lease"
        )

    def test_it_is_retryable(self):
        assert "retryable=True" in self._broad_handler_block(), (
            "a surprise is not a settled verdict; the step should be allowed another attempt"
        )

    def test_the_step_failure_is_ordered_before_the_run_level_handler(self):
        """If the run-level handler came first it would swallow this again."""
        source = _tick_source()
        assert source.index("except Exception as error:  # noqa: BLE001") < source.index(
            "capability runner skipped run"
        )


class TestTheMessageNamesTheError:
    def test_the_type_is_logged_at_step_level(self):
        source = _tick_source()
        assert "type(error).__name__" in source

    def test_the_run_level_message_carries_the_type_too(self):
        source = _tick_source()
        tail = source[source.index("capability runner skipped run"):]
        assert "type(error).__name__" in tail, (
            "'(non-fatal): deploy' cost a day of guessing; a bare str(KeyError) is not a message"
        )

    def test_the_recorded_error_is_not_a_bare_string(self):
        source = _tick_source()
        assert 'f"{type(error).__name__}: {error}"' in source
