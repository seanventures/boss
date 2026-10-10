"""A build refused its authority ends BLOCKED with what to do; the worker gets an answer, not a 500.

The defect (the Captain's run, 2026-10-09): /v1/code-tasks/step answered 500 "authority refused:
no_grant". identity.runs.start_code_task_run refused the build (BOSS itself started it and no
person's grant covered its builder), and both build paths turned that into a bare RuntimeError
that escaped the endpoint: the run stayed `running`, the worker saw a crash.
"""
from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

from sqlalchemy import text

from apps.api.app import main
from boss_engine.services import code_task_service

ROOT = Path(__file__).resolve().parents[3]


def _running_run(engine) -> tuple[str, str]:
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        project = db.execute(text("SELECT id FROM projects ORDER BY id LIMIT 1")).scalar()
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
            "VALUES (:id, :p, 'running', 'coding', 'Captain: quality pass', '{}'::jsonb, '{}'::jsonb, "
            "CAST(:f AS JSONB))"), {"id": run_id, "p": project, "f": json.dumps({})})
    return run_id, project


def test_a_refused_authority_blocks_the_build_with_the_next_step(engine, monkeypatch):
    run_id, _ = _running_run(engine)

    def refused(*a, **kw):
        raise code_task_service.AuthorityRefused("no_grant")

    monkeypatch.setattr(code_task_service, "_run_code_task", refused)
    out = code_task_service.run_code_task(engine, main.settings, main.jobs, run_id)
    assert out["status"] == "blocked" and out["done"] is True and out["reason"] == "no_authority"
    assert "console" in out["message"] and "consent" in out["message"]
    with engine.begin() as db:
        row = db.execute(text("SELECT status, error FROM code_task_runs WHERE id = :i"), {"i": run_id}).mappings().one()
    assert row["status"] == "blocked"
    said = json.dumps(row["error"]) if not isinstance(row["error"], str) else row["error"]
    assert "No one's authority covers this build" in said and "no_grant" not in said


def test_both_build_paths_raise_the_refusal_never_a_bare_runtime_error():
    for rel in ("engine/boss_engine/services/code_task_service.py",
                "engine/boss_engine/services/governed_code_task_service.py"):
        tree = ast.parse((ROOT / rel).read_text())
        bare = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                and getattr(n.exc.func, "id", "") == "RuntimeError"
                and any(isinstance(a, ast.JoinedStr) and "authority refused" in ast.unparse(a) for a in n.exc.args)]
        assert not bare, f"{rel}:{bare} raises a bare RuntimeError for a refused authority"
        assert "AuthorityRefused(" in (ROOT / rel).read_text()


def test_the_words_name_the_next_step_for_each_refusal():
    words = code_task_service.authority_refusal_words
    assert "My account ▸ Agents acting for me" in words("no_grant")
    assert "Passport" in words("builder_has_no_passport")
    assert "no longer covers" in words("grant_revoked")
