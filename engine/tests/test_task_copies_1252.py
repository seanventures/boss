"""Teammates run a task on an isolated copy. The shared branch stays put until a
finished copy is applied. Applying an unfinished copy is refused (BOSSAPP-1252).
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import get_settings
from boss_engine.db import ensure_schema
from boss_engine.services import task_copies
from boss_engine.services.receipts import ReceiptService

_URL = "postgresql+psycopg://boss:boss@127.0.0.1:55770/boss_test_1252"
_BRANCH = "main"
_SHARED = "head-main-100"
_ADA = "head-ada-200"
_BAO = "head-bao-300"
_BAO_ON_ADA = "head-bao-300-on-ada"


@pytest.fixture(scope="module")
def eng():
    os.environ["DATABASE_URL"] = _URL
    get_settings.cache_clear()
    engine = create_engine(_URL, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:  # pragma: no cover - the container is this ticket's database
        pytest.skip(f"test database unavailable: {error}")
    ensure_schema(engine, get_settings())
    return engine


@pytest.fixture()
def project(eng):
    org = f"org_{uuid.uuid4().hex[:8]}"
    workspace = f"ws_{org}"
    project_id = f"p_{uuid.uuid4().hex[:8]}"
    with eng.begin() as connection:
        connection.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, 'copies')"), {"id": org})
        connection.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org, 'copies')"),
            {"id": workspace, "org": org})
        connection.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:id, :workspace, 'Copies', :slug)"),
            {"id": project_id, "workspace": workspace, "slug": f"copies-{project_id}"})
    yield {"org_id": org, "workspace_id": workspace, "project_id": project_id}
    with eng.begin() as connection:
        connection.execute(text("DELETE FROM receipts WHERE project_id = :id"), {"id": project_id})
        connection.execute(text("DELETE FROM events WHERE project_id = :id"), {"id": project_id})
        connection.execute(
            text("DELETE FROM task_copy_dead_letters WHERE project_id = :id"), {"id": project_id})
        connection.execute(text("DELETE FROM task_copies WHERE project_id = :id"), {"id": project_id})
        connection.execute(
            text("DELETE FROM task_copy_boards WHERE project_id = :id"), {"id": project_id})
        connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})
        connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace})
        connection.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org})


def _open(connection, project_id: str):
    return task_copies.open_board(
        connection, project_id=project_id, shared_branch=_BRANCH, shared_head=_SHARED,
        actor="ada")


def _start(connection, project_id: str, board_id: str, *, teammate: str, task_key: str, title: str):
    return task_copies.start_copy(
        connection, project_id=project_id, board_id=board_id, task_key=task_key, title=title,
        teammate=teammate, actor=teammate)


def test_a_copy_does_not_change_the_shared_branch_until_apply(eng, project):
    project_id = project["project_id"]
    with eng.begin() as connection:
        opened = _open(connection, project_id)
        board_id = opened["board"]["id"]
        ada = _start(
            connection, project_id, board_id, teammate="Ada", task_key="LATCH",
            title="Fix the latch")
        assert ada["board"]["shared_head"] == _SHARED
        assert ada["copy"]["copy_branch"] != _BRANCH
        assert ada["copy"]["copy_branch"].startswith("copy/")
        assert ada["copy"]["base_head"] == _SHARED
        assert ada["copy"]["copy_head"] == _SHARED
        assert ada["copy"]["status"] == "running"

        worked = task_copies.record_work(
            connection, project_id=project_id, copy_id=ada["copy"]["id"], note="Latch holds.",
            copy_head=_ADA, actor="Ada")
        assert worked["copy"]["copy_head"] == _ADA
        assert worked["board"]["shared_head"] == _SHARED

        finished = task_copies.finish_copy(
            connection, project_id=project_id, copy_id=ada["copy"]["id"], actor="Ada")
        assert finished["copy"]["status"] == "finished"
        assert finished["board"]["shared_head"] == _SHARED

        applied = task_copies.apply_copy(
            connection, project_id=project_id, copy_id=ada["copy"]["id"], actor="Ada")
        assert applied["copy"]["status"] == "applied"
        assert applied["copy"]["replayed"] is False
        assert applied["board"]["shared_head"] == _ADA
        assert applied["copy"]["receipt_id"]

        again = task_copies.apply_copy(
            connection, project_id=project_id, copy_id=ada["copy"]["id"], actor="Ada")
        assert again["copy"]["replayed"] is True
        assert again["copy"]["receipt_id"] == applied["copy"]["receipt_id"]
        assert again["board"]["shared_head"] == _ADA

        receipts = ReceiptService().list_by_dedupe_prefix(
            connection, project["org_id"], f"task-copy-apply:{ada['copy']['id']}")
        assert len(receipts) == 1
        assert receipts[0]["status"] == "succeeded"
        assert receipts[0]["dedupe_key"] == f"task-copy-apply:{ada['copy']['id']}"

        reopened = _open(connection, project_id)
        assert reopened["board"]["id"] == board_id
        assert reopened["board"]["shared_head"] == _ADA


def test_apply_of_an_unfinished_copy_is_refused(eng, project):
    project_id = project["project_id"]
    with eng.begin() as connection:
        opened = _open(connection, project_id)
        started = _start(
            connection, project_id, opened["board"]["id"], teammate="Ada", task_key="LATCH",
            title="Fix the latch")
        with pytest.raises(task_copies.TaskCopyRefused) as refused:
            task_copies.apply_copy(
                connection, project_id=project_id, copy_id=started["copy"]["id"], actor="Ada")
        assert "not finished" in str(refused.value).lower()
        view = task_copies.board_view(
            connection, project_id=project_id, board_id=opened["board"]["id"])
        assert view["board"]["shared_head"] == _SHARED
        assert view["copies"][0]["status"] == "running"
        letters = connection.execute(
            text("SELECT count(*) FROM task_copy_dead_letters WHERE project_id = :id"),
            {"id": project_id}).scalar()
        assert letters == 0


def test_two_teammates_stay_isolated_until_each_finished_copy_is_applied(eng, project):
    project_id = project["project_id"]
    with eng.begin() as connection:
        opened = _open(connection, project_id)
        board_id = opened["board"]["id"]
        ada = _start(
            connection, project_id, board_id, teammate="Ada", task_key="LATCH",
            title="Fix the latch")
        bao = _start(
            connection, project_id, board_id, teammate="Bao", task_key="LATCH",
            title="Fix the latch")
        assert ada["copy"]["id"] != bao["copy"]["id"]
        assert ada["copy"]["copy_branch"] != bao["copy"]["copy_branch"]

        task_copies.record_work(
            connection, project_id=project_id, copy_id=ada["copy"]["id"], note="Latch holds.",
            copy_head=_ADA, actor="Ada")
        view = task_copies.board_view(connection, project_id=project_id, board_id=board_id)
        by_teammate = {copy["teammate"]: copy for copy in view["copies"]}
        assert by_teammate["Ada"]["copy_head"] == _ADA
        assert by_teammate["Bao"]["copy_head"] == _SHARED
        assert view["board"]["shared_head"] == _SHARED

        task_copies.finish_copy(
            connection, project_id=project_id, copy_id=ada["copy"]["id"], actor="Ada")
        task_copies.apply_copy(
            connection, project_id=project_id, copy_id=ada["copy"]["id"], actor="Ada")

        task_copies.record_work(
            connection, project_id=project_id, copy_id=bao["copy"]["id"], note="Latch still open.",
            copy_head=_BAO, actor="Bao")
        task_copies.finish_copy(
            connection, project_id=project_id, copy_id=bao["copy"]["id"], actor="Bao")
        with pytest.raises(task_copies.TaskCopyConflict) as conflict:
            task_copies.apply_copy(
                connection, project_id=project_id, copy_id=bao["copy"]["id"], actor="Bao")
        assert "dead letter" in str(conflict.value).lower()
        view = task_copies.board_view(connection, project_id=project_id, board_id=board_id)
        assert view["board"]["shared_head"] == _ADA
        bao_row = next(copy for copy in view["copies"] if copy["teammate"] == "Bao")
        assert bao_row["status"] == "dead"
        assert bao_row["failure_kind"] == "conflict"
        assert bao_row["copy_head"] == _BAO

        with pytest.raises(task_copies.TaskCopyRefused) as retry:
            task_copies.retry_copy(
                connection, project_id=project_id, copy_id=bao["copy"]["id"], actor="Bao")
        assert "rebase" in str(retry.value).lower()
        assert task_copies.board_view(
            connection, project_id=project_id, board_id=board_id)["board"]["shared_head"] == _ADA

        rebased = task_copies.rebase_copy(
            connection, project_id=project_id, copy_id=bao["copy"]["id"], copy_head=_BAO_ON_ADA,
            actor="Bao")
        assert rebased["board"]["shared_head"] == _ADA
        assert rebased["copy"]["status"] == "finished"
        assert rebased["copy"]["base_head"] == _ADA
        assert rebased["copy"]["copy_head"] == _BAO_ON_ADA

        landed = task_copies.apply_copy(
            connection, project_id=project_id, copy_id=bao["copy"]["id"], actor="Bao")
        assert landed["board"]["shared_head"] == _BAO_ON_ADA
        assert landed["copy"]["status"] == "applied"


def test_a_failed_run_is_a_dead_letter_and_apply_is_refused_until_retry(eng, project):
    project_id = project["project_id"]
    reason = "Tests failed on the copy."
    with eng.begin() as connection:
        opened = _open(connection, project_id)
        started = _start(
            connection, project_id, opened["board"]["id"], teammate="Ada", task_key="LATCH",
            title="Fix the latch")
        task_copies.record_work(
            connection, project_id=project_id, copy_id=started["copy"]["id"],
            note="Latch holds.", copy_head=_ADA, actor="Ada")
        failed = task_copies.fail_copy(
            connection, project_id=project_id, copy_id=started["copy"]["id"], reason=reason,
            actor="Ada")
        assert failed["copy"]["status"] == "dead"
        assert failed["copy"]["failure_kind"] == "run"
        assert failed["copy"]["failure_reason"] == reason
        assert failed["board"]["shared_head"] == _SHARED

        with pytest.raises(task_copies.TaskCopyRefused) as refused:
            task_copies.apply_copy(
                connection, project_id=project_id, copy_id=started["copy"]["id"], actor="Ada")
        assert "dead letter" in str(refused.value).lower()
        assert task_copies.board_view(
            connection, project_id=project_id, board_id=opened["board"]["id"]
        )["board"]["shared_head"] == _SHARED

        retried = task_copies.retry_copy(
            connection, project_id=project_id, copy_id=started["copy"]["id"], actor="Ada")
        assert retried["copy"]["status"] == "running"
        assert retried["board"]["shared_head"] == _SHARED

        task_copies.finish_copy(
            connection, project_id=project_id, copy_id=started["copy"]["id"], actor="Ada")
        applied = task_copies.apply_copy(
            connection, project_id=project_id, copy_id=started["copy"]["id"], actor="Ada")
        assert applied["board"]["shared_head"] == _ADA


def test_the_apply_routes_are_declared():
    from apps.api.app.routers import gap_1252

    paths = {getattr(route, "path", "") for route in gap_1252.router.routes}
    assert "/v1/projects/{project_id}/task-copies/{copy_id}/apply" in paths
    assert gap_1252.router.routes
