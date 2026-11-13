"""BOSSAPP-892: an external memory backend behind remember / recall / forget.

BOSS's memory stays canonical. Every memory that may leave is queued as a snapshot for review;
only an approved snapshot is sent, a forgotten memory's copy is deleted, actor-scoped memories
and unreviewed lessons never leave, and recall marks outside hits as outside context.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from ._ops2_support import fresh_engine, make_project
from boss_engine.services import agent_memory, memory_backends as mb


class Recorder:
    """A mem0 stand-in: records every call, answers like mem0's REST API."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str, dict, object]] = []
        self.fail = fail
        self.n = 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if self.fail:
            return 503, {"detail": "down"}
        if url.endswith("/v1/memories/") and method == "POST":
            self.n += 1
            return 200, {"results": [{"id": f"m0_{self.n}", "event": "ADD"}]}
        if url.endswith("/v1/memories/search/"):
            return 200, [{"id": "m0_x", "memory": "Deploys go out on Tuesdays.", "score": 0.9}]
        return 204, None


@pytest.fixture(scope="module")
def eng():
    engine, drop = fresh_engine("membk")
    yield engine
    drop()


@pytest.fixture()
def project(eng):
    return make_project(eng)


def _backend(eng, project, **kw):
    with eng.begin() as c:
        return mb.configure(c, project_id=project, actor="ceo", name="team mem0", kind="mem0",
                            base_url="https://mem0.example.test", **kw)


def _remember(eng, project, body, kind="project", scope="project", scope_ref=None):
    with eng.begin() as c:
        return agent_memory.remember(c, project_id=project, actor="ceo", kind=kind, body=body,
                                     scope=scope, scope_ref=scope_ref)


def test_without_a_backend_nothing_is_queued(eng, project):
    _remember(eng, project, "We deploy from main.")
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM memory_exports WHERE project_id = :p"),
                         {"p": project}).scalar() == 0


def test_only_the_approved_snapshot_leaves_and_forget_takes_it_back(eng, project):
    backend = _backend(eng, project)
    row = _remember(eng, project, "We deploy from main.")
    _remember(eng, project, "Alice likes terse answers.", kind="preference", scope="actor",
              scope_ref="alice")                                     # one person's: never queued
    lesson = _remember(eng, project, "Always run the frontend checks.", kind="lesson")
    with eng.begin() as c:
        queued = mb.pending(c, project)
    assert [q["body"] for q in queued] == ["We deploy from main."]   # not the unreviewed lesson
    wire = Recorder()
    assert mb.sync(eng, project_id=project, backend_id=backend["id"],
                   transport=wire)["sent"] == 0                       # nothing approved yet
    assert wire.calls == []
    with eng.begin() as c:
        mb.review(c, project_id=project, actor="ceo", export_ids=[queued[0]["id"]],
                  decision="approve")
    report = mb.sync(eng, project_id=project, backend_id=backend["id"], transport=wire)
    assert report == {"sent": 1, "failed": 0, "skipped": 0}
    method, url, _, body = wire.calls[0]
    assert (method, url) == ("POST", "https://mem0.example.test/v1/memories/")
    assert body["messages"][0]["content"] == "We deploy from main."
    assert body["metadata"]["boss_memory_id"] == row["id"] and body["infer"] is False
    # The lesson, once a person lets it bind, is queued for review too.
    with eng.begin() as c:
        agent_memory.review(c, project_id=project, memory_id=lesson["id"], actor="ceo",
                            decision="active")
        assert [q["body"] for q in mb.pending(c, project)] == ["Always run the frontend checks."]
        agent_memory.forget(c, project_id=project, memory_id=row["id"], actor="ceo")
    report = mb.sync(eng, project_id=project, backend_id=backend["id"], transport=wire)
    assert report["sent"] == 1
    assert wire.calls[-1][:2] == ("DELETE", "https://mem0.example.test/v1/memories/m0_1/")


def test_a_rejected_snapshot_never_leaves_and_a_failure_is_retried(eng, project):
    backend = _backend(eng, project)
    _remember(eng, project, "Staging is at stage.example.")
    _remember(eng, project, "Prod freezes on Fridays.")
    with eng.begin() as c:
        first, second = mb.pending(c, project)
        mb.review(c, project_id=project, actor="ceo", export_ids=[first["id"]],
                  decision="reject")
        mb.review(c, project_id=project, actor="ceo", export_ids=[second["id"]],
                  decision="approve")
    down = Recorder(fail=True)
    assert mb.sync(eng, project_id=project, backend_id=backend["id"],
                   transport=down)["failed"] == 1
    up = Recorder()
    assert mb.sync(eng, project_id=project, backend_id=backend["id"], transport=up)["sent"] == 1
    assert [c[3]["messages"][0]["content"] for c in up.calls] == ["Prod freezes on Fridays."]
    with eng.begin() as c:
        attempts = c.execute(text("SELECT attempts, status FROM memory_exports WHERE id = :i"),
                             {"i": second["id"]}).one()
    assert tuple(attempts) == (2, "written")


def test_a_memory_retired_after_approval_is_not_sent(eng, project):
    backend = _backend(eng, project)
    row = _remember(eng, project, "Use the blue theme.")
    with eng.begin() as c:
        (queued,) = mb.pending(c, project)
        mb.review(c, project_id=project, actor="ceo", export_ids=[queued["id"]],
                  decision="approve")
        agent_memory.forget(c, project_id=project, memory_id=row["id"], actor="ceo")
    wire = Recorder()
    assert mb.sync(eng, project_id=project, backend_id=backend["id"], transport=wire)["sent"] == 0
    assert wire.calls == []


def test_recall_marks_outside_hits_and_survives_the_store_being_down(eng, project):
    _backend(eng, project, read_enabled=True)
    with eng.begin() as c:
        hits = mb.recall_external(c, project_id=project, query="deploy day",
                                  transport=Recorder())
        assert hits == [{"id": "m0_x", "body": "Deploys go out on Tuesdays.", "score": 0.9,
                         "source": "team mem0", "kind": "mem0"}]
        assert mb.recall_external(c, project_id=project, query="deploy day",
                                  transport=Recorder(fail=True)) == []


def test_a_credential_is_named_never_stored_and_must_be_allowlisted(eng, project):
    with pytest.raises(mb.MemoryBackendError, match="cannot name the environment variable"):
        _backend(eng, project, credential_env="DATABASE_URL")
    with pytest.raises(mb.MemoryBackendError, match="kind must be"):
        with eng.begin() as c:
            mb.configure(c, project_id=project, actor="ceo", name="z", kind="zep",
                         base_url="https://z.example")


def test_the_recall_tool_is_unchanged_without_a_backend(eng, project):
    from boss_engine.tools.builtin import build_default_registry

    _remember(eng, project, "We deploy from main.")
    spec = build_default_registry().get("recall")
    out = spec.handler(SimpleNamespace(begin=eng.begin), project_id=project, actor="ceo",
                       args={"query": "deploy"})
    assert set(out) == {"ok", "count", "memories", "note"}
