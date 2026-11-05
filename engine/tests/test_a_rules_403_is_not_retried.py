"""A rules 403 is an answer, not an outage.

A release-gate build that died in bootstrap on egress's 403 (repo_not_allowed, or git's
"returned error: 403") was classed as infra and started again. The second attempt failed
the same way. A 403 from the rules is not a 5xx or a timeout: the build is not retried,
and recovery explains it and stops.
"""
from __future__ import annotations

import pytest

from boss_engine.services import step_recovery as sr
from boss_engine.services.code_task_service import classify_pipeline_exception

CLONE_403 = ("fatal: unable to access 'https://github.com/acme/web.git/': "
             "The requested URL returned error: 403")
EGRESS_BODY = '{"error":"forbidden","reason":"repo_not_allowed"}'


@pytest.mark.parametrize("reason", [
    CLONE_403,
    EGRESS_BODY,
    "GitHubRefused: repo_not_allowed",
    "bootstrap clone failed: HTTP 403 forbidden",
])
def test_a_rules_403_is_not_infra_so_the_build_is_not_auto_retried(reason):
    fclass, rcode = classify_pipeline_exception(reason)
    assert fclass != "infra", (reason, fclass, rcode)
    assert (fclass, rcode) == ("needs_human", "rules_forbidden")


@pytest.mark.parametrize("reason,expected", [
    ("GitHub pull request failed", ("infra", "github_error")),
    ("fatal: unable to access 'https://github.com/acme/web.git/': The requested URL returned error: 502",
     ("infra", "github_error")),
    ("something totally unexpected", ("infra", "pipeline_exception")),
    ("i/o timeout talking to the registry", ("infra", "pipeline_exception")),
    ("HTTP 429 rate limit exceeded", ("needs_human", "model_rate_limit")),
])
def test_a_5xx_a_timeout_and_a_github_error_without_a_rules_403_stay_as_they_were(reason, expected):
    assert classify_pipeline_exception(reason) == expected


def _coded(why: str, *, kind: str = "build"):
    failure = {"why": why, "error": why}
    signature, pattern = sr.cause_signature(kind, failure)
    return sr.coded_decision(kind, {"failure": failure}, signature=signature, pattern=pattern,
                             last={}, allowed=sr.KINDS[kind])


def test_recovery_of_a_rules_403_explains_it_and_stops():
    decision = _coded(f"{CLONE_403}\n{EGRESS_BODY}")
    assert decision["action"] == "ask_person"
    said = " ".join(str(decision.get(k) or "") for k in ("cause", "reasoning", "question", "after"))
    assert "403" in said or "repo_not_allowed" in said
    assert "retry" not in decision["action"]


def test_recovery_still_retries_a_transient_build_failure():
    assert _coded("worker lost: connection reset by peer")["action"] == "retry"
    assert _coded("i/o timeout")["action"] == "retry"


def test_recovery_does_not_ask_a_model_to_retry_a_rules_403(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a model was asked to recover a rules 403")

    def model_says_retry(*_a, **_k):
        return {"decision": {"action": "retry", "cause": "try the build again",
                             "reasoning": "one fresh attempt"},
                "cost_usd_micros": 0, "by": {"model": "stub"}}

    monkeypatch.setattr(sr, "routed_adapter", boom)
    monkeypatch.setattr(sr, "ask_model", model_says_retry)
    monkeypatch.setattr(sr, "emit_event", lambda *_a, **_k: "evt")

    class _NoDb:
        def begin_nested(self):
            raise RuntimeError("no database")

    entry = sr.decide(
        _NoDb(), object(), project_id="p", run_id="wrun", step_id="build", kind="build",
        evidence={"failure": {"why": CLONE_403, "error": EGRESS_BODY}},
        state={}, agent={"name": "Harbor", "role": "engineer"}, adapter=object())
    assert entry["action"] == "ask_person"
    assert "403" in entry["sentence"] or "repo_not_allowed" in entry["sentence"]
    assert "retry" not in str(entry["action"])
