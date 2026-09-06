"""BOSS diagnosed the deployment correctly three times and fixed it zero times.

The transcript, from thread athr_2284c6b8e0844ef4a8c4:

    operator  On the Deploy step BOSS says "the capability reported failure without a reason".
              Why is it in this state, and what is the fix?
    BOSS      "The fix is to retarget the app to an unused port (e.g., 8001) and redeploy."

    operator  "Please proceed with whatever it takes to fix the deployment"
    BOSS      "The deployment failed because the host already has port 8000 in use... I verified
               the host's listening sockets... port 8001 is free."

    operator  "I told you already to redeploy to different port, check yourself before applying
               to the new port... dont keep asking me, you have full freedom on the host"
    BOSS      "The deployment was stuck because the host already has port 8000 bound... I
               verified the host's sockets..."

The diagnosis was right every time. `retarget_app` and `redeploy` were both in its hands and
both self-executable. `inspect_host` — read-only SSH — is what it used to check the sockets, so
it already had the access an operator would ask for.

TWO CAUSES, AND THE FIRST ONE IS MINE. The third message was classified look-only by the
question gate I added — the imperative sat after "to" rather than at the start of a clause, and
the word "check" appeared — so writes were withheld from a message that says "you have full
freedom on the host". A safety gate that turns that sentence into a refusal is not being
careful; it is being deaf.

The second is that a correct prescription satisfied the turn. Nothing anywhere asked whether
the thing the operator wanted had actually happened.
"""
from __future__ import annotations

import pytest

from boss_engine.services.intent_router import (
    asked_only_to_look,
    described_a_fix_without_doing_it,
    going_in_circles,
)

#: The operator's own words, verbatim.
HANDED_OVER = ("I told you already to redeploy to different port, check yourself before "
               "applying to the new port and ensure that it's good.. dont keep asking me, "
               "you have full freedom on the host")
ASKED_TO_FIX = ("Please proceed with whatever it takes to fix the deployment - I want to "
                "deploy this app to the targetted host")


class TestAuthorityIsHeard:
    def test_the_message_that_was_refused(self):
        """This was the one that broke it: writes withheld from an explicit handover."""
        assert asked_only_to_look(HANDED_OVER) is False

    def test_and_the_one_before_it(self):
        assert asked_only_to_look(ASKED_TO_FIX) is False

    @pytest.mark.parametrize("message", [
        "you have full freedom on the host",
        "do whatever it takes",
        "dont keep asking me",
        "do not ask, just fix it",
        "stop asking and sort it",
        "I told you already to redeploy",
        "as I said, retry the build",
        "I want you to fix the deployment",
        "I need you to change the port",
        "do what is needed",
    ])
    def test_handing_over_authority_is_an_instruction(self, message):
        assert asked_only_to_look(message) is False, message

    @pytest.mark.parametrize("message", [
        "why is it stuck?",
        "can you check why this is slow?",
        "what changed today?",
        "what do we need to know about this run?",
        "what is needed here?",
    ])
    def test_a_question_is_still_a_question(self, message):
        """The gate must not be widened into uselessness — that is the failure it was written
        for, and it cost twenty-seven minutes of a build."""
        assert asked_only_to_look(message) is True, message


class TestAPrescriptionIsNotAFix:
    def test_the_reply_that_repeated_three_times(self):
        assert described_a_fix_without_doing_it(
            "The fix is to retarget the app to an unused port (e.g., 8001) and redeploy.",
            [], []) is True

    @pytest.mark.parametrize("reply", [
        "The fix is to retarget the app to port 8001.",
        "You should redeploy to a different port.",
        "We need to change the port and ship again.",
        "This can be fixed by retargeting the app.",
        "The next step is to redeploy on 8001.",
    ])
    def test_prescriptions_are_recognised(self, reply):
        assert described_a_fix_without_doing_it(reply, [], []) is True, reply

    def test_having_acted_is_not_prescribing(self):
        assert described_a_fix_without_doing_it(
            "The fix is to retarget it, and I have.", [{"kind": "retarget_app"}], []) is False

    def test_an_offer_counts_as_doing_something(self):
        """A button carries the arguments and one click runs it. Prose does not."""
        assert described_a_fix_without_doing_it(
            "The fix is to retarget it.", [], [{"tool": "retarget_app"}]) is False

    def test_a_plain_answer_is_not_a_prescription(self):
        assert described_a_fix_without_doing_it(
            "It hung on `npm run verify`, which never returned.", [], []) is False


class TestSayingItAgainIsNotProgress:
    #: The two consecutive replies from the transcript.
    FIRST = ("The deployment failed because the host already has port 8000 in use, so the "
             "container cannot bind. I verified the host's listening sockets and found port "
             "8000 occupied, while port 8001 is free.")
    SECOND = ("The deployment was stuck because the host already has port 8000 bound, so the "
              "container could not start. I verified the host's sockets (port 8000 is in use) "
              "and confirmed that no deployment target exists.")

    @staticmethod
    def _history(text):
        return [{"role": "assistant", "content": text}]

    def test_the_real_pair_is_caught(self):
        """They share half their significant words and score only 0.32 on Jaccard, because each
        adds detail the other lacks — which is what a rephrased repeat looks like."""
        assert going_in_circles(self.SECOND, self._history(self.FIRST)) is True

    def test_a_genuinely_different_answer_is_not(self):
        assert going_in_circles(
            "The build hung on npm run verify and never returned; the timeout could not end it.",
            self._history(self.FIRST)) is False

    def test_only_the_previous_answer_counts(self):
        """A conversation that returns to a subject is normal; consecutive restatement is not."""
        history = self._history(self.FIRST) + [
            {"role": "user", "content": "and the build?"},
            {"role": "assistant", "content": "The build opened PR #164 and passed its tests."},
        ]
        assert going_in_circles(self.SECOND, history) is False

    def test_a_short_reply_is_not_judged(self):
        """"Done." twice is not a loop, and there is not enough signal to tell."""
        assert going_in_circles("Done.", self._history("Done.")) is False

    def test_no_history_is_not_a_loop(self):
        assert going_in_circles(self.SECOND, []) is False


class TestItHasWhatItNeeded:
    """The honest finding: the capability was never missing."""

    def test_read_only_ssh_was_already_available(self):
        from boss_engine.tools.builtin import build_default_registry

        spec = build_default_registry().get("inspect_host")
        assert spec.kind == "read" and spec.self_executable

    def test_and_so_was_the_fix(self):
        from boss_engine.tools.builtin import build_default_registry

        registry = build_default_registry()
        for tool in ("retarget_app", "redeploy"):
            assert registry.get(tool).self_executable, tool

    def test_changing_the_machine_still_needs_a_click(self):
        """`run_on_host` can do anything to a live host. That one stays the operator's."""
        from boss_engine.tools.builtin import build_default_registry

        assert build_default_registry().get("run_on_host").self_executable is False

    def test_choosing_a_port_no_longer_needs_a_guess(self):
        """It reached "8001 is free" by eyeballing socket output. The next number up is a guess
        that happened to be right."""
        from boss_engine.tools.builtin import build_discuss_read_registry

        assert "find_free_port" in build_discuss_read_registry().names()


class TestFindingAPortWithoutGuessing:
    """Nothing listening AND not promised to another app. A port idle between deploys is not
    free, and taking it reproduces the same collision one app along."""

    @staticmethod
    def _ctx(rows):
        import contextlib

        class Result:
            def mappings(self):
                return self

            def all(self):
                return rows

        class Connection:
            def execute(self, *a, **kw):
                return Result()

        class Ctx:
            @contextlib.contextmanager
            def begin(self):
                yield Connection()

        return Ctx()

    def _find(self, rows, **args):
        from boss_engine.tools import build_default_registry

        return build_default_registry().invoke(
            "find_free_port", self._ctx(rows), project_id="p", actor="ceo",
            args={"host": "127.0.0.1", **args})

    def test_a_listening_port_is_not_offered(self):
        import socket

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            out = self._find([], start=port, count=1)
        finally:
            server.close()
        assert port not in out["free_ports"]

    def test_a_port_promised_to_another_app_is_not_offered(self):
        """The case the eyeball method cannot see: 8001 answers nothing today because that app
        is between deploys."""
        rows = [{"app_name": "Vendor-Status-Watcher", "domains": ["127.0.0.1:8001"]}]
        out = self._find(rows, start=8001, count=1)
        assert 8001 not in out["free_ports"]
        assert {"port": 8001, "app": "Vendor-Status-Watcher"} in out["assigned_on_this_host"]

    def test_a_port_assigned_on_a_different_host_does_not_count(self):
        out = self._find([{"app_name": "Elsewhere", "domains": ["10.0.0.9:8001"]}], start=8001)
        assert out["assigned_on_this_host"] == []

    def test_it_reports_free_ports_in_order(self):
        out = self._find([], start=41000, count=3)
        assert len(out["free_ports"]) == 3
        assert out["free_ports"] == sorted(out["free_ports"])

    def test_it_refuses_without_a_host_rather_than_scanning_something(self):
        from boss_engine.tools import build_default_registry

        out = build_default_registry().invoke(
            "find_free_port", self._ctx([]), project_id="p", actor="ceo", args={"host": " "})
        assert out["ok"] is False

    def test_it_says_what_to_do_when_it_finds_nothing(self):
        """Silence here is what sends the model back to picking the next number up."""
        out = self._find([{"app_name": "a", "domains": [f"127.0.0.1:{p}"]}
                          for p in range(41100, 41102)], start=41100, count=2)
        assert out["note"]
