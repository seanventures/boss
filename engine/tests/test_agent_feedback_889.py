"""BOSSAPP-889: agents file complaints and suggestions as typed records a person reads.

report_friction (every agent's baseline) -> one agent_feedback row, typed by what it is about,
with who filed it and in which run taken from the pipeline, never the arguments -> idempotent per
run -> no credential survives into the body -> Activity › Feedback groups by target -> a person
closes it.
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import agent_feedback as fb
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM agent_feedback LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'w4o1 889', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("agent_feedback", "events"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _tool():
    from boss_engine.tools.builtin import build_default_registry

    return build_default_registry().get("report_friction")


def _report(eng, project, args, *, actor="agent:forge"):
    ctx = SimpleNamespace(begin=eng.begin, settings=None)
    return _tool().handler(ctx, project_id=project, actor=actor, args=args)


ARGS = {"kind": "complaint", "target_kind": "tool", "target": "read_logs",
        "body": "read_logs returned nothing for the worker for ten minutes.",
        "evidence": ["read_logs(service=worker) -> []"]}


def test_report_friction_is_a_baseline_write_that_only_queues():
    from boss_engine.services import agent_baseline as ab

    spec = _tool()
    assert spec.kind == "write" and spec.reversibility == "safe" and spec.queues_for_review
    assert spec.permission == "system:read"
    assert "report_friction" in ab.FUNDAMENTALS_TOOLS
    assert ab.may_write("boss.fundamentals", "report_friction")


def test_who_and_where_come_from_the_pipeline_and_a_run_files_once(eng, project):
    set_llm_context(role_key="forge", run_id="crun_abc")
    try:
        first = _report(eng, project, {**ARGS, "role_key": "someone_else"})
        again = _report(eng, project, {**ARGS, "body": "said differently, same thing"})
    finally:
        clear_llm_context()
    assert first["ok"] and first["created"] is True
    assert again["ok"] and again["created"] is False
    assert again["feedback_id"] == first["feedback_id"]
    with eng.begin() as c:
        rows = fb.list_feedback(c, project)
    assert len(rows) == 1
    assert rows[0]["role_key"] == "forge" and rows[0]["run_ref"] == "crun_abc"
    assert rows[0]["filed_by"] == "agent:forge"
    # Another run is another report.
    set_llm_context(role_key="forge", run_id="crun_def")
    try:
        other = _report(eng, project, ARGS)
    finally:
        clear_llm_context()
    assert other["created"] is True


def test_refusals_are_words_not_rows(eng, project):
    out = _report(eng, project, {**ARGS, "target_kind": "everything"})
    assert out["ok"] is False and "target_kind" in out["error"]
    out = _report(eng, project, {**ARGS, "body": "   "})
    assert out["ok"] is False
    with eng.begin() as c:
        assert fb.list_feedback(c, project) == []


def test_no_credential_reaches_the_record(eng, project):
    key = "sk-ant-api03-" + "A" * 40
    out = _report(eng, project, {**ARGS, "body": f"the call failed with key {key} in the header",
                                 "evidence": [f"Authorization: Bearer {key}"]})
    assert out["ok"]
    with eng.begin() as c:
        row = c.execute(text("SELECT body, evidence::text FROM agent_feedback WHERE id=:i"),
                        {"i": out["feedback_id"]}).one()
    assert key not in row[0] and key not in row[1]


def test_grouped_by_target_and_closed_by_a_person(eng, project):
    with eng.begin() as c:
        fb.file_feedback(c, project, kind="complaint", target_kind="tool", target="read_logs",
                         body="empty", role_key="forge", run_ref="r1", actor="agent:forge")
        fb.file_feedback(c, project, kind="suggestion", target_kind="tool", target="read_logs",
                         body="add a since filter", role_key="harbor", run_ref="r2",
                         actor="agent:harbor")
        made = fb.file_feedback(c, project, kind="complaint", target_kind="grant",
                                target="github.write", body="could not push", role_key="forge",
                                run_ref="r1", actor="agent:forge")
        groups = fb.grouped(fb.list_feedback(c, project))
        assert [(g["target_kind"], g["target"]) for g in groups] == [
            ("tool", "read_logs"), ("grant", "github.write")]
        assert groups[0]["complaints"] == 1 and groups[0]["suggestions"] == 1
        assert sorted(groups[0]["roles"]) == ["forge", "harbor"]
        closed = fb.set_status(c, project, made["feedback"]["id"], status="resolved",
                               actor="usr_ceo", resolution="granted github.write")
        assert closed["status"] == "resolved" and closed["resolved_by"] == "usr_ceo"
        assert fb.list_feedback(c, project, status="open").__len__() == 2
        with pytest.raises(LookupError):
            fb.set_status(c, project, "afb_missing", status="resolved", actor="usr_ceo")


def test_the_api_offers_no_way_to_file_as_an_agent():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "apps/api/app/routers/agent_feedback.py").read_text()
    assert "@router.post" not in source, "only report_friction files feedback"
    assert '@router.get("/v1/agent-feedback")' in source
