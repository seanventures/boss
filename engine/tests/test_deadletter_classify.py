"""Deadletter error-class taxonomy + model-capacity escalation helpers (P0-2)."""

from __future__ import annotations

import pytest

from boss_engine.services.code_task_service import classify_pipeline_exception
from boss_engine.services.deadletters import (
    DEADLETTER_ERROR_CLASSES,
    classify_deadletter_error,
    is_model_capacity_exhaustion,
)


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("No such file or directory: 'git'", "git"),
        ("fatal: not a git repository", "git"),
        ("git clone failed: permission denied", "git"),
        ("sandbox exec not allowlisted: curl", "sandbox"),
        ("microVM rootfs missing", "sandbox"),
        ("sandbox host 404 for task", "sandbox"),
        ("Groq TPM 6k limit exceeded (qwen3-32b)", "model"),
        ("rate limit exceeded: 429 too many requests", "model"),
        ("OpenRouter credits exhausted", "model"),
        ("budget reservation denied for code_task", "model"),
        ("error_max_turns reached", "model"),
        ("GitHub API 403: Resource not accessible by integration", "github"),
        ("Failed to mint installation token (422)", "github"),
        ("pull request create failed: bad credentials", "github"),
        ("unexpected NoneType has no attribute get", "other"),
        ("terminal failure", "other"),
    ],
)
def test_classify_deadletter_error_classes(reason: str, expected: str):
    assert classify_deadletter_error(reason) == expected
    assert expected in DEADLETTER_ERROR_CLASSES


def test_classify_uses_payload_and_job_kind():
    assert (
        classify_deadletter_error(
            "worker failed",
            {"message": "rate limit: tokens per minute"},
            job_kind="code.task",
        )
        == "model"
    )
    assert (
        classify_deadletter_error(
            "failed",
            {"error": {"message": "sandbox task 404"}},
        )
        == "sandbox"
    )


def test_is_model_capacity_exhaustion():
    assert is_model_capacity_exhaustion("Groq TPM limit exceeded")
    assert is_model_capacity_exhaustion("429 rate_limit")
    assert is_model_capacity_exhaustion("insufficient credits on OpenRouter")
    assert is_model_capacity_exhaustion("budget reservation denied")
    # max turns is model class but not capacity exhaustion for escalation
    assert not is_model_capacity_exhaustion("error_max_turns")
    assert not is_model_capacity_exhaustion("No such file or directory: 'git'")


@pytest.mark.parametrize(
    "reason,fclass,rcode",
    [
        ("error_max_turns at step implement", "recoverable", "max_turns_exhausted"),
        ("HTTP 429 rate limit exceeded", "needs_human", "model_rate_limit"),
        ("TPM tokens per minute exceeded", "needs_human", "model_rate_limit"),
        ("budget reservation denied", "needs_human", "budget_exhausted"),
        ("No such file or directory: 'git'", "infra", "missing_git"),
        ("sandbox not allowlisted", "infra", "sandbox_error"),
        ("GitHub pull request failed", "infra", "github_error"),
        ("something totally unexpected", "infra", "pipeline_exception"),
    ],
)
def test_classify_pipeline_exception_taxonomy(reason: str, fclass: str, rcode: str):
    assert classify_pipeline_exception(reason) == (fclass, rcode)
