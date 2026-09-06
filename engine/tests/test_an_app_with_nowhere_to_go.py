"""BOSS wrote SQL because that was the only route left, and the SQL was wrong.

Thread athr_50fc273050ba410c82f1, the whole chain, from the operator's screen inward:

    Deploy step   "deploy failed and gave no error message. What it did return: reason=3 active
                  'staging' targets share repo 'boss-sandbox', and this run does not name one
                  unambiguously. Pass target_id — deploying the wrong app is worse than not d"

    BOSS (×2)     **What I FOUND** / **What I COULD NOT DETERMINE** / **One check that would
                  settle it** / **Single next action** — the same four headings twice, with the
                  action described and never taken.

    operator      "Yes, please do anything to fix it"

    BOSS          write_db INSERT INTO deployment_targets (…) VALUES ('deptgt_9f1c…',
                  (SELECT project_id FROM code_tasks WHERE run_id='crun_e917…'), …,
                  'container_ref_placeholder', …)
                  → relation "code_tasks" does not exist

Six defects, each independently sufficient to produce that screen:

1.  THE ERROR WAS A LIE. "Change Calendar" had no target at all. Every app in this project is
    built into the one sandbox repo, so the repo+environment query returned the OTHER apps'
    targets and the code called that ambiguity — naming Meeting cost meter and
    Vendor-Status-Watcher as the CANDIDATES for deploying a change calendar.
2.  "PASS TARGET_ID" COULD NOT BE FOLLOWED. No target for this app existed to pass, and no tool
    could create one. The only remaining route was raw SQL, which is what BOSS took.
3.  THE APP NAME WAS NEVER READ. `finalize_ctx` was not in the SELECT, so the match always fell
    back to `title` — the operator's entire directive — compared by substring.
4.  write_db RAN WITHOUT ANYONE ASKED. It is declared `destroys_work` at its definition, but
    the discuss registry rebuilt every spec and dropped `reversibility`, which defaults to
    "safe" — so the confirmation gate was disarmed on the only registry the chat agent uses.
5.  A FABRICATED VALUE WAS ACCEPTED. `propose_action` refuses `container_ref_placeholder`;
    write_db executed it. The guard was on the offer path and not the write path.
6.  THE FAILURE WAS NOT MENTIONED. The reply after the failed INSERT described the situation as
    though nothing had been attempted.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services import delivery_pipeline
from boss_engine.services.intent_router import (
    _say_what_did_not_work,
    hid_a_failed_action,
)
from boss_engine.tools import build_default_registry, build_discuss_read_registry


class TestMissingIsNotAmbiguous:
    """Two different situations, and one of them has an answer in the candidate list."""

    SRC = inspect.getsource(delivery_pipeline.deploy_build)

    def test_an_app_with_no_target_is_told_it_has_no_target(self):
        assert "has no" in self.SRC and "deployment target of its own" in self.SRC

    def test_the_other_apps_are_not_offered_as_candidates(self):
        """`candidates` is the field the model reads as "pick one of these". Someone else's
        running app is not a candidate for this deployment at any confidence."""
        i = self.SRC.index("deployment target of its own")
        block = self.SRC[i - 600:i + 1400]
        assert "other_apps_targets" in block
        assert '"candidates"' not in block

    def test_it_says_what_would_happen_if_you_did_pick_one(self):
        assert "overwrite a running app" in self.SRC

    def test_it_names_the_tool_that_fixes_it(self):
        assert "create_deployment_target" in self.SRC

    def test_and_carries_the_fix_as_data_not_only_prose(self):
        """The step's stored output is what BOSS reads back when the operator asks about it."""
        assert '"fix": {"tool": "create_deployment_target"' in self.SRC
        assert '"missing": "deployment_target"' in self.SRC

    def test_real_ambiguity_is_still_a_refusal(self):
        """Two targets for the SAME app is the case the original guard was written for, and it
        must keep refusing — a wrong deployment reporting success is the worst outcome here."""
        assert "are registered for" in self.SRC
        assert "Pass target_id" in self.SRC

    def test_an_unnameable_run_still_refuses_rather_than_guessing(self):
        assert "does not name an app to match them against" in self.SRC


class TestTheAppNameIsActuallyRead:
    SRC = inspect.getsource(delivery_pipeline.deploy_build)

    def test_finalize_ctx_is_selected(self):
        """It was read and never fetched, so `fctx.get("app_name")` was always None and the
        match silently compared target names against the operator's whole directive."""
        assert "SELECT id, project_id, status, title, spec, finalize_ctx" in self.SRC

    def test_the_title_falls_back_through_the_same_namer_the_console_uses(self):
        assert "project_name(str(run.get(\"title\") or \"\"))" in self.SRC

    def test_matching_is_on_identity_not_substring(self):
        assert "identity_key(str(t.get(\"app_name\") or \"\")) == wanted" in self.SRC

    @pytest.mark.parametrize("app,target,same", [
        ("Change Calendar", "change-calendar", True),
        ("Change Calendar", "Change Calendar", True),
        ("Change Calendar", "Meeting cost meter", False),
        # The substring rule matched this pair, which is how a target named "Change" would have
        # captured a deploy of "Change Calendar".
        ("Change Calendar", "Change", False),
    ])
    def test_the_comparison_itself(self, app, target, same):
        from boss_engine.services.naming import identity_key

        assert (identity_key(app) == identity_key(target)) is same


class TestSomewhereToDeployTo:
    """The capability whose absence forced the SQL."""

    def test_the_tool_exists(self):
        assert "create_deployment_target" in build_default_registry().names()

    def test_the_chat_agent_can_reach_it(self):
        assert "create_deployment_target" in build_discuss_read_registry().names()

    def test_it_is_a_write_that_does_not_need_a_click(self):
        """Creating a destination is undoable — the row can be retired — and requiring a click
        for it would leave the same dead end that produced the hand-written INSERT."""
        spec = build_discuss_read_registry().get("create_deployment_target")
        assert spec.kind == "write" and spec.self_executable

    def test_it_creates_a_destination_and_ships_nothing(self):
        from boss_engine.services import deployment_fleet
        from engine.tests._source import code_only

        body = inspect.getsource(deployment_fleet.create_deployment_target)
        assert "Nothing has been deployed yet" in body
        # code_only strips the docstring, which mentions deploy_build by name while explaining
        # why this function does not call it.
        assert "deploy_build" not in code_only(deployment_fleet.create_deployment_target)

    def test_one_app_gets_one_target_per_environment(self):
        """Two targets for one app is precisely the ambiguity this incident began with."""
        from boss_engine.services.deployment_fleet import create_deployment_target

        body = inspect.getsource(create_deployment_target)
        assert "adopted_existing" in body
        assert 'identity_key(str(row["app_name"] or "")) != wanted' in body

    def test_a_retired_target_is_revived_rather_than_duplicated(self):
        from boss_engine.services.deployment_fleet import create_deployment_target

        body = inspect.getsource(create_deployment_target)
        assert "'active', updated_at = now()" in body

    def test_the_host_must_be_verified(self):
        from boss_engine.services.deployment_fleet import create_deployment_target

        body = inspect.getsource(create_deployment_target)
        assert "not verified" in body
        assert "fails at deploy time" in body

    def test_it_does_not_ask_which_host_when_there_is_one_answer(self):
        """A project deploying to a single machine should not be interrogated about it."""
        from boss_engine.services.deployment_fleet import create_deployment_target

        body = inspect.getsource(create_deployment_target)
        assert "len(verified) == 1" in body
        assert "SELECT DISTINCT host_id FROM deployment_targets" in body

    def test_a_port_must_be_free_on_both_counts(self):
        from boss_engine.services.deployment_fleet import _first_free_port

        body = inspect.getsource(_first_free_port)
        assert "_ports_assigned_on" in body
        assert "socket" in body

    def test_a_bare_ip_is_probed_over_http(self):
        """Defaulting to https had a healthy app failing its gate with WRONG_VERSION_NUMBER."""
        from boss_engine.services.deployment_fleet import create_deployment_target

        body = inspect.getsource(create_deployment_target)
        assert 'scheme = "http" if (looks_like_ip or ":" in address) else "https"' in body

    def test_the_health_check_is_declared_with_a_kind(self):
        """A check with no `kind` was skipped by the observer, and a target whose every check
        was skipped reported healthy. Every target this creates says what to probe."""
        from boss_engine.services.deployment_fleet import create_deployment_target

        body = inspect.getsource(create_deployment_target)
        assert '"kind": "http"' in body and '"expect_status": 200' in body

    def test_target_ref_is_derived_from_the_host_record(self):
        from boss_engine.services.deployment_fleet import create_deployment_target

        body = inspect.getsource(create_deployment_target)
        assert "target_ref_for(host)" in body
        assert "attach_target(" in body


class TestTheGateWasDisarmedByTheRegistryTheAgentUses:
    """The safety property held in `build_default_registry`, which the chat path never calls."""

    def test_the_two_registries_agree_on_every_admitted_tool(self):
        default, discuss = build_default_registry(), build_discuss_read_registry()
        for name in discuss.names():
            a, b = default.get(name), discuss.get(name)
            assert (a.kind, a.reversibility, a.self_executable) == \
                   (b.kind, b.reversibility, b.self_executable), name

    def test_arbitrary_sql_needs_a_person(self):
        spec = build_discuss_read_registry().get("write_db")
        assert spec.reversibility == "destroys_work"
        assert spec.self_executable is False

    def test_and_so_does_a_shell_on_a_host(self):
        spec = build_discuss_read_registry().get("run_on_host")
        assert spec.self_executable is False

    def test_the_ordinary_fixes_still_run_when_asked(self):
        """Widening the gate until nothing runs is the other way to fail this."""
        discuss = build_discuss_read_registry()
        for name in ("retarget_app", "redeploy", "retry_code_task",
                     "create_deployment_target"):
            assert discuss.get(name).self_executable, name


class TestAnInventedValueIsNotWritten:
    @staticmethod
    def _write(sql: str):
        class Ctx:
            def begin(self):
                raise AssertionError("EXECUTED — the placeholder check did not run")

        return build_default_registry().invoke(
            "write_db", Ctx(), project_id="p", actor="ceo",
            args={"sql": sql, "reason": "test", "_operator_confirmed": True})

    def test_the_statement_boss_actually_ran(self):
        out = self._write(
            "INSERT INTO deployment_targets (id, target_ref) VALUES "
            "('deptgt_9f1c2a3b4c5d6e7f8a9b', 'container_ref_placeholder')")
        assert out["ok"] is False
        assert "invented" in out["error"]

    def test_it_points_at_the_tool_that_knows_the_real_value(self):
        out = self._write("INSERT INTO deployment_targets (target_ref) VALUES ('placeholder')")
        assert "create_deployment_target" in out["error"]

    def test_a_real_statement_is_not_blocked_by_this_check(self):
        """It must reach the database — `invoke` catches the sentinel, so the proof that the
        guard let it past is the sentinel coming back as the error."""
        out = self._write("UPDATE deployment_targets SET paused = false "
                          "WHERE id = 'deptgt_2d4ebcee897343599227'")
        assert "EXECUTED" in str(out.get("error") or "")


class TestAFailedAttemptIsReported:
    TRIED_AND_FAILED = [{"tool": "write_db", "ok": False},
                        {"tool": "query_db", "ok": True}]

    def test_the_reply_that_said_nothing_about_it(self):
        assert hid_a_failed_action(
            "The Deploy step failed because BOSS could not pick a unique staging target. "
            "Single next action: create a target and redeploy.", self.TRIED_AND_FAILED) is True

    def test_a_failure_word_about_the_SUBJECT_does_not_count_as_owning_one(self):
        """The exact escape the real reply would have used: it opens "The Deploy step failed"
        while saying nothing about the INSERT it had just had rejected."""
        assert hid_a_failed_action(
            "The Deploy step failed because BOSS could not pick a unique staging target for "
            "repository boss-sandbox, and four targets share that repo.",
            self.TRIED_AND_FAILED) is True

    def test_naming_the_tool_is_enough(self):
        assert hid_a_failed_action(
            "I tried write_db and the INSERT was rejected, so I used the proper tool instead.",
            self.TRIED_AND_FAILED) is False

    def test_so_is_saying_plainly_that_it_failed(self):
        assert hid_a_failed_action(
            "My first attempt failed — the table name was wrong — so I did it the other way.",
            self.TRIED_AND_FAILED) is False

    def test_the_human_spelling_counts(self):
        assert hid_a_failed_action(
            "The write db call was refused because of a placeholder value.",
            self.TRIED_AND_FAILED) is False

    def test_a_turn_where_nothing_failed_is_not_a_fault(self):
        assert hid_a_failed_action("All good.", [{"tool": "query_db", "ok": True}]) is False

    def test_no_tool_calls_at_all(self):
        assert hid_a_failed_action("All good.", []) is False

    def test_the_propose_action_refusals_the_operator_never_saw(self):
        """Two offers were refused for containing a placeholder. The operator got no button and
        no idea one had been attempted — they were told "I will insert the new target"."""
        assert hid_a_failed_action(
            "Single next action: create a proper staging deployment target and retry. "
            "I will insert the new target and immediately invoke redeploy.",
            [{"tool": "propose_action", "ok": False},
             {"tool": "propose_action", "ok": False}]) is True

    def test_it_is_wired_into_the_resolution_loop(self):
        from boss_engine.services import intent_router

        body = inspect.getsource(intent_router)
        assert "_hid_failure = hid_a_failed_action(reply, tool_calls)" in body
        assert "or _hid_failure" in body
        assert "and it FAILED, and your answer" in body


class TestTheInstructionIsNotATemplate:
    """My own gap text came back as section headings, twice, with nothing done."""

    BODY = None

    @classmethod
    def setup_class(cls):
        import re as _re

        from boss_engine.services import intent_router

        # The instruction is a multi-line string literal in the source, so the sentences are
        # split across lines with quotes between them. Normalising is what lets the test assert
        # on the sentence the model sees rather than on how it is typed.
        raw = inspect.getsource(intent_router)
        cls.BODY = _re.sub(r'"\s*\n\s*"', "", raw)

    def test_the_headings_it_produced_are_now_forbidden_by_name(self):
        assert "What I could not determine" in self.BODY
        assert "One check that would settle it" in self.BODY
        assert "Do not reply with headed sections" in self.BODY

    def test_naming_a_check_is_replaced_by_running_it(self):
        assert "RUN THE CHECK" in self.BODY
        assert "http_probe" in self.BODY and "find_free_port" in self.BODY

    def test_giving_up_requires_having_tried(self):
        assert "Only say something cannot be determined after a tool has actually failed" \
            in self.BODY

    def test_it_still_forbids_the_yes_no_ending(self):
        assert "never with a plan, and never with a yes/no question" in self.BODY


class TestTheDeadEndIsAppendedWhenTheModelWillNotSayIt:
    """The continuation ASKS the model to own its failed attempts. This is what happens when it
    does not — measured on the deployed build: `read_logs` failed, the continuation fired, and
    the rewritten answer still opened with a "What I did" list that left it out.

    A guarantee, not a request. The operator concluded twice that BOSS "could not fix the
    problem"; a system that quietly drops its own dead ends is unreadable from outside, and the
    dropped step is often the most informative thing in the turn.
    """
    # staticmethod: assigning the function in the class body would bind `self` as
    # its first argument.
    _note = staticmethod(_say_what_did_not_work)

    CALLS = [{"tool": "read_logs", "ok": False, "error": "no container named worker"},
             {"tool": "diagnose_run", "ok": True}]

    def test_it_is_appended_when_the_reply_omits_it(self):
        out = self._note("The build succeeded and there is no deployment target.", self.CALLS)
        assert "read_logs" in out
        assert "no container named worker" in out

    def test_the_original_answer_is_left_intact(self):
        reply = "The build succeeded and there is no deployment target."
        assert self._note(reply, self.CALLS).startswith(reply)

    def test_nothing_is_appended_when_the_reply_already_says_it(self):
        reply = "I tried read_logs and it failed, so I used diagnose_run instead."
        assert self._note(reply, self.CALLS) == reply

    def test_nothing_is_appended_when_nothing_failed(self):
        reply = "All good."
        assert self._note(reply, [{"tool": "query_db", "ok": True}]) == reply

    def test_it_stops_at_three(self):
        calls = [{"tool": f"tool_{i}", "ok": False, "error": "x"} for i in range(9)]
        assert self._note("Answer.", calls).count(";") <= 2

    def test_a_failure_with_no_message_still_names_the_tool(self):
        assert "read_logs" in self._note("Answer.", [{"tool": "read_logs", "ok": False}])

    def test_the_error_is_carried_onto_the_call_record(self):
        """Without this the note could say something failed and never say what."""
        from boss_engine.services import intent_router

        body = inspect.getsource(intent_router)
        assert body.count('"error": str(result.get("error") or "")') >= 2

    def test_it_runs_after_the_reconcilers_and_before_the_repetition_filter(self):
        """Appended too early it would be stripped as a repetition; too late and a reconciler
        would not see the text it added."""
        from boss_engine.services import intent_router

        body = inspect.getsource(intent_router)
        assert body.index("_no_action_was_claimed(reply, actions)") \
            < body.index("_say_what_did_not_work(reply, tool_calls)") \
            < body.index("without_what_was_already_said(reply, history)")


class TestADeployQuestionIsAnsweredByTheDeployment:
    """Asked why a STAGING DEPLOY failed, BOSS ran diagnose_run twice, read the app's code, and
    answered about the build — never once looking at the deployment target, which was the whole
    answer. The thin-answer instruction named diagnose_run → read_logs → the code, which is the
    right chain for a stalled build and points away from everything else."""

    @staticmethod
    def _body():
        from boss_engine.services import intent_router

        return inspect.getsource(intent_router)

    def test_a_deploy_question_gets_the_deployment_chain(self):
        body = self._body()
        assert "_DEPLOY_SUBJECT.search(latest)" in body
        assert "describe_deployment_target and list_deployments for " in body

    def test_it_says_why(self):
        assert "answered by the deployment, not by " in self._body()

    def test_the_build_chain_is_still_there_for_a_build_question(self):
        # Split across source lines in the literal; the sentence is what matters.
        assert "read_logs, then the code" in self._body()

    @pytest.mark.parametrize("message", [
        "The staging Deploy step for Change Calendar failed. Why?",
        "why is the deployment broken",
        "the app's url returns nothing",
        "which port is it on?",
        "is it live yet",
    ])
    def test_these_are_deploy_questions(self, message):
        from boss_engine.services.intent_router import _DEPLOY_SUBJECT

        assert _DEPLOY_SUBJECT.search(message), message

    @pytest.mark.parametrize("message", [
        "why is the build stuck",
        "what changed today?",
        "why did the tests fail",
    ])
    def test_and_these_are_not(self, message):
        from boss_engine.services.intent_router import _DEPLOY_SUBJECT

        assert not _DEPLOY_SUBJECT.search(message), message


class TestTheLogToolPointsSomewhereReal:
    """`read_logs` cannot work in production — the services log to stdout in their containers —
    and its refusal said "read them with `docker logs`, or set BOSS_WORKER_LOG", neither of which
    BOSS can do from inside its own container. So the model treated logging as a closed door.

    BOSS records every model turn and every step transition in its own tables. That is what the
    operator means by "check the activity logs", and those tools work."""

    @staticmethod
    def _error():
        from boss_engine.tools import builtin

        body = inspect.getsource(builtin)
        i = body.index("no log file readable from this process")
        return body[i:i + 1200]

    def test_it_names_the_tools_that_do_work(self):
        error = self._error()
        for tool in ("get_run_activity", "read_run_turns", "diagnose_run", "query_db"):
            assert tool in error, tool

    def test_it_says_the_failure_is_permanent_rather_than_transient(self):
        assert "will not " in self._error() and "start working later" in self._error()

    def test_it_forbids_reporting_the_logs_as_unavailable(self):
        assert "Do not report this as" in self._error()

    def test_the_alternatives_are_machine_readable_too(self):
        assert '"use_instead"' in self._error()

    def test_those_tools_actually_exist(self):
        names = build_discuss_read_registry().names()
        for tool in ("get_run_activity", "read_run_turns", "diagnose_run", "query_db"):
            assert tool in names, tool
