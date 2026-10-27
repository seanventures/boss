"""A build a WORKFLOW starts, and a Shepherd repair of it, base on the PROJECT'S branch.

crun_809fad7fadde46439870 (2026-10-27): a console New build -> New app ran the Minor Feature
workflow; its build step called the create_code_task tool, which wrote `base_branch = "main"`
when the step named no branch. The spec then carried "main" as if someone chose it, so
build_base_branch resolved {"source": "spec", "base_branch": "main"} although boss-platform
binds paramg/boss to rel/rc10.10-kg. The Shepherd's repair task did the same
(`spec.get("base_branch") or "main"`). Every caller now passes None when nobody chose a
branch, and the one resolver picks the project's binding.

The same spec carried `test_commands: []`; that is "not set", so the gate follows the repo's
boss.verify.yaml rather than running nothing.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import build_base_branch as bbb
from boss_engine.services import code_task_service as cts
from boss_engine.services import repo_registry, shepherd_service
from boss_engine.services import verification_target as vt
from boss_engine.tools import builtin
from boss_engine.tools.builtin import build_default_registry

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
ENGINE_SRC = Path(__file__).resolve().parents[1] / "boss_engine"

TABLES = (
    "CREATE TABLE code_task_runs (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT,"
    " spec JSONB NOT NULL DEFAULT '{}'::jsonb)",
    "CREATE TABLE github_repository_bindings (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,"
    " full_name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'primary',"
    " status TEXT NOT NULL DEFAULT 'active', default_branch TEXT)",
)


@pytest.fixture()
def db():
    """A private schema holding just the run and binding tables."""
    if not URL:
        pytest.skip("BOSS_TEST_DATABASE_URL is not set")
    assert "test" in URL.rsplit("/", 1)[-1]
    schema = f"t_wfbase_{uuid.uuid4().hex[:8]}"
    dsn = URL.replace("postgresql://", "postgresql+psycopg://")
    base = create_engine(dsn)
    with base.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
    eng = create_engine(dsn, connect_args={"options": f"-csearch_path={schema}"})
    with eng.begin() as c:
        for ddl in TABLES:
            c.execute(text(ddl))
        c.execute(text("INSERT INTO github_repository_bindings (id, project_id, full_name,"
                       " default_branch) VALUES ('b1', 'boss-platform', 'paramg/boss', 'rel/x')"))
    try:
        yield eng
    finally:
        eng.dispose()
        with base.begin() as c:
            c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        base.dispose()


def _resolve(db, explicit):
    with db.begin() as c:
        return bbb.resolve_build_base(c, project_id="boss-platform", owner="paramg",
                                      repo="boss", explicit=explicit,
                                      host_default=lambda: "main")


@pytest.fixture()
def captured(monkeypatch):
    got: dict = {}

    def start(_c, **kwargs):
        got.update(kwargs)
        return "crun_new"
    monkeypatch.setattr(cts, "start_code_task", start)
    return got


def test_a_workflow_build_step_with_no_branch_builds_on_the_projects_branch(db, captured,
                                                                          monkeypatch):
    monkeypatch.setattr(repo_registry, "resolve_repository_target", lambda *_a, **_k: {
        "owner": "paramg", "repo": "boss", "full_name": "paramg/boss",
        "source": "binding", "binding_id": "b1"})
    monkeypatch.setattr(builtin, "_building_agent", lambda *_a: None)
    monkeypatch.setattr(builtin, "_multirepo_change", lambda *_a, **_k: None)

    class _Ctx:
        jobs = None
        settings = None

        @contextmanager
        def begin(self):
            with db.begin() as c:
                yield c

    tool = build_default_registry().get("create_code_task")
    out = tool.handler(_Ctx(), project_id="boss-platform", actor="workflow_runner",
                       args={"title": "Small feature", "instructions": "Do it.",
                             "workflow_run_id": "wfr_1", "test_commands": []})
    assert out["run_id"] == "crun_new"
    assert captured["base_branch"] is None  # nobody chose a branch
    assert captured["finalize_ctx"]["workflow_run_id"] == "wfr_1"
    base = _resolve(db, captured["base_branch"])
    assert (base.clone, base.pr) == ("rel/x", "rel/x")
    assert base.clone_source == base.pr_source == bbb.SOURCE_BINDING


def test_an_asked_for_branch_still_wins(db, captured, monkeypatch):
    monkeypatch.setattr(repo_registry, "resolve_repository_target", lambda *_a, **_k: {
        "owner": "paramg", "repo": "boss", "full_name": "paramg/boss",
        "source": "binding", "binding_id": "b1"})
    monkeypatch.setattr(builtin, "_building_agent", lambda *_a: None)
    monkeypatch.setattr(builtin, "_multirepo_change", lambda *_a, **_k: None)

    class _Ctx:
        jobs = None
        settings = None

        @contextmanager
        def begin(self):
            with db.begin() as c:
                yield c

    build_default_registry().get("create_code_task").handler(
        _Ctx(), project_id="boss-platform", actor="workflow_runner",
        args={"title": "T", "instructions": "I", "base_branch": " feature/y "})
    assert captured["base_branch"] == "feature/y"
    assert _resolve(db, captured["base_branch"]).clone_source == bbb.SOURCE_SPEC


def test_a_shepherd_repair_builds_on_the_projects_branch(db, captured):
    spec = {"owner": "paramg", "repo": "boss", "base_branch": None, "test_commands": []}
    with db.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, spec) VALUES"
                       " ('crun_failed', 'boss-platform', 'App', CAST(:s AS JSONB))"),
                  {"s": json.dumps(spec)})
    settings = SimpleNamespace(target_github_owner="", target_github_repo="")
    got = shepherd_service._open_fix_task(  # noqa: SLF001
        db, settings, None, "crun_failed",
        {"class_name": "preview_boot", "evidence": "boom"}, None, "note")
    assert got == "crun_new"
    assert captured["base_branch"] is None
    base = _resolve(db, captured["base_branch"])
    assert (base.clone, base.pr, base.pr_source) == ("rel/x", "rel/x", bbb.SOURCE_BINDING)


def test_empty_test_commands_fall_through_to_the_verify_manifest(tmp_path):
    (tmp_path / "boss.verify.yaml").write_text(
        "version: 1\nsuites:\n  - name: engine\n    paths: [\"engine/**\"]\n"
        "    run: [\"python3 -m pytest -q engine/tests\"]\n", encoding="utf-8")
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "a.py").write_text("X = 1\n", encoding="utf-8")
    pinned = vt.pin_verify_manifest(tmp_path)
    for empty in ([], "", None, ()):
        plan = vt.resolve_gate_plan({"test_commands": empty}, "t", tmp_path, pinned=pinned,
                                    changed=["engine/a.py"])
        assert plan.source == "manifest", (empty, plan.source)
        assert plan.commands[-1] == "python3 -m pytest -q engine/tests"
    explicit = vt.resolve_gate_plan({"test_commands": ["python3 -m pytest -q x"]}, "t",
                                    tmp_path, pinned=pinned, changed=["engine/a.py"])
    assert explicit.source == "task"


def test_no_build_creating_caller_writes_a_main_placeholder():
    """Every caller that starts or retries a build passes None when nobody chose a branch."""
    for rel in ("tools/builtin.py", "services/shepherd_service.py",
                "multirepo/change_sets.py", "multirepo/components.py",
                "services/reliability.py"):
        source = (ENGINE_SRC / rel).read_text()
        assert not re.search(r'base_branch["\']?\s*[=:][^\n]*\bor "main"', source), rel
        assert not re.search(r'args\.get\("base_branch"\) or "main"', source), rel
    schemas = (ENGINE_SRC / "schemas.py").read_text()
    remediate = schemas[schemas.index("class ReliabilityRemediateRequest"):]
    assert 'base_branch: str = "main"' not in remediate[:400]
