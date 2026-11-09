"""A rules 403 opens one inbox decision and is not started again.

Classification already says needs_human / rules_forbidden, and an infra failure is the only
thing the pipeline retries. The hole was the inbox: create_blocking_decision ran only for a
model rate limit or an exhausted budget, so a release-gate build that died on egress's 403
failed with no decision to answer. The same run must not open a second card if it already
holds one, and the model-capacity card's wording stays what it was.
"""
from __future__ import annotations

import json

from boss_engine.config import Settings
from boss_engine.ports.composition import EnginePorts
from boss_engine.services import governed_code_task_service as g

CLONE_403 = (
    "fatal: unable to access 'https://github.com/acme/web.git/': "
    "The requested URL returned error: 403"
)
MODEL_CAPACITY_QUESTION = (
    "Model capacity exhausted (TPM / rate limit / credits / budget). "
    "Code task stopped — raise limits, wait for quota reset, or "
    "requeue after capacity is available."
)


class _Point:
    is_active = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def commit(self):
        return None

    def rollback(self):
        return None


class _Result:
    def mappings(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def scalar(self):
        return None

    def __iter__(self):
        return iter(())


class _Conn:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def begin(self):
        return self

    def begin_nested(self):
        return _Point()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        text = getattr(statement, "text", None) or str(statement)
        self.calls.append((text, dict(params or {})))
        return _Result()


def _decisions(conn: _Conn) -> list[dict]:
    found = []
    for statement, params in conn.calls:
        if "INSERT INTO decisions" not in statement:
            continue
        recommendation = params.get("recommendation")
        if isinstance(recommendation, str):
            recommendation = json.loads(recommendation)
        found.append({
            "id": params.get("id"),
            "question": params.get("question"),
            "recommendation": recommendation or {},
        })
    return found


def _events(conn: _Conn) -> list[str]:
    return [
        str(params.get("event_type"))
        for statement, params in conn.calls
        if "INSERT INTO events" in statement and params.get("event_type")
    ]


def _stored_decision_ids(conn: _Conn) -> list[str]:
    stored = []
    for statement, params in conn.calls:
        if "UPDATE code_task_runs" in statement and "decision_id" in statement:
            stored.append(str(params.get("d") or ""))
    return stored


def _fail_build(monkeypatch, reason: str, *, decision_id=None):
    run = {
        "id": "crun_rules",
        "project_id": "proj_1",
        "task_id": "task_1",
        "status": "running",
        "phase": "bootstrap",
        "title": "Ship the gate",
        "spec": {
            "title": "Ship the gate",
            "instructions": "Add the release gate.",
            "owner": "acme",
            "repo": "web",
            "acceptance_criteria": [],
        },
        "state": {},
        "usage": {},
        "finalize_ctx": {},
        "reservation_id": None,
        "pull_request_url": None,
        "pull_request_number": None,
        "decision_id": decision_id,
        "error": None,
    }
    conn = _Conn()

    def acquire(self, *, task_id, tenant_id=None):
        raise RuntimeError(reason)

    settings = Settings(
        _env_file=None,
        github_token="tok",
        github_app_id=None,
        github_app_private_key=None,
        github_app_private_key_path=None,
        github_app_installation_id=None,
    )
    monkeypatch.setattr(g, "load_code_task_run", lambda _c, _run_id: run)
    monkeypatch.setattr(g, "_engine_ports", lambda: EnginePorts.oss_defaults(settings))
    monkeypatch.setattr(g.LocalSubprocessProvider, "acquire", acquire)
    monkeypatch.setattr(
        "boss_engine.identity.runs.start_code_task_run", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "boss_engine.services.github_app_adapter.repo_token", lambda *_a, **_k: "tok")
    result = g._run_governed_code_task(  # noqa: SLF001 — the failure exit is the behavior
        conn, settings, object(), run["id"])
    return result, conn


def test_a_rules_403_opens_one_inbox_decision_and_is_not_retried(monkeypatch):
    result, conn = _fail_build(monkeypatch, CLONE_403)
    opened = _decisions(conn)
    assert len(opened) == 1
    decision = opened[0]
    question = decision["question"]
    assert "403" in question
    assert "not retried" in question
    assert "Model capacity" not in question
    assert decision["recommendation"]["action"] == "allow_repository_or_change_clone_target"
    assert decision["recommendation"]["reason_code"] == "rules_forbidden"
    assert decision["recommendation"]["failure_class"] == "needs_human"
    assert _stored_decision_ids(conn) == [decision["id"]]
    assert _events(conn).count("code_task.rules_forbidden_escalated") == 1
    assert result["failure_class"] == "needs_human"
    assert result["reason_code"] == "rules_forbidden"
    assert result["decision_id"] == decision["id"]
    assert result.get("auto_retry_run_id") is None
    assert result.get("auto_retry") is None


def test_a_rules_403_does_not_open_a_second_decision(monkeypatch):
    result, conn = _fail_build(monkeypatch, CLONE_403, decision_id="dec_already")
    assert _decisions(conn) == []
    assert _stored_decision_ids(conn) == []
    assert "code_task.rules_forbidden_escalated" not in _events(conn)
    assert result.get("decision_id") in (None, "dec_already")
    assert result["reason_code"] == "rules_forbidden"
    assert result.get("auto_retry_run_id") is None


def test_a_model_rate_limit_still_opens_the_capacity_card(monkeypatch):
    result, conn = _fail_build(monkeypatch, "HTTP 429 rate limit exceeded")
    opened = _decisions(conn)
    assert len(opened) == 1
    assert opened[0]["question"] == MODEL_CAPACITY_QUESTION
    assert opened[0]["recommendation"]["action"] == "restore_model_capacity_or_raise_budget"
    assert opened[0]["recommendation"]["reason_code"] == "model_rate_limit"
    assert _events(conn).count("code_task.model_capacity_escalated") == 1
    assert "code_task.rules_forbidden_escalated" not in _events(conn)
    assert result["decision_id"] == opened[0]["id"]
    assert result["reason_code"] == "model_rate_limit"


def test_an_exhausted_budget_still_opens_the_same_capacity_card(monkeypatch):
    _result, conn = _fail_build(monkeypatch, "budget reservation denied")
    opened = _decisions(conn)
    assert len(opened) == 1
    assert opened[0]["question"] == MODEL_CAPACITY_QUESTION
    assert opened[0]["recommendation"]["action"] == "restore_model_capacity_or_raise_budget"
    assert opened[0]["recommendation"]["reason_code"] == "budget_exhausted"
    assert _events(conn).count("code_task.model_capacity_escalated") == 1


def test_a_later_capacity_failure_opens_a_card_when_a_decision_is_already_held(monkeypatch):
    result, conn = _fail_build(
        monkeypatch, "HTTP 429 rate limit exceeded", decision_id="dec_plan")
    opened = _decisions(conn)
    assert len(opened) == 1
    assert opened[0]["question"] == MODEL_CAPACITY_QUESTION
    assert opened[0]["recommendation"]["reason_code"] == "model_rate_limit"
    assert _events(conn).count("code_task.model_capacity_escalated") == 1
    assert "code_task.rules_forbidden_escalated" not in _events(conn)
    assert result["decision_id"] == opened[0]["id"]
    assert result["reason_code"] == "model_rate_limit"
