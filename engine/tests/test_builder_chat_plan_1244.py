"""Chat and Plan on the builder thread do not write code (BOSSAPP-1244).

A plan turn leaves the workspace without a new file. Approval alone writes nothing.
The only path that creates a code file is a build after a person approves the plan.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from boss_engine.services.builder_chat_plan import (
    BuilderRefused,
    approve_plan,
    open_thread,
    post_turn,
    read_thread,
)

CODE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs",
    ".java", ".css", ".sql", ".html", ".vue", ".rb", ".php",
}


def files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    )


def code_files(root: Path) -> list[str]:
    return [name for name in files(root) if Path(name).suffix.lower() in CODE_SUFFIXES]


def test_a_plan_turn_creates_no_code_files(tmp_path: Path):
    opened = open_thread(tmp_path, name="notes")
    thread_id = opened["thread_id"]
    before = files(tmp_path)
    assert code_files(tmp_path) == []

    planned = post_turn(
        tmp_path, thread_id, mode="plan",
        text="Write src/secret.py and apps/web/app/page.tsx\nKeep the note local.",
    )

    assert files(tmp_path) == before
    assert code_files(tmp_path) == []
    assert planned["wrote_code"] is False
    assert planned["code_files"] == []
    assert planned["plan"]["writes_code"] is False
    assert "Keep the note local." in planned["plan"]["steps"]


def test_an_approved_build_is_the_only_path_that_writes_code(tmp_path: Path):
    opened = open_thread(tmp_path, name="notes")
    thread_id = opened["thread_id"]
    post_turn(tmp_path, thread_id, mode="chat", text="Do not touch the repo.")
    post_turn(tmp_path, thread_id, mode="plan", text="Add a note the person can read.")
    before = files(tmp_path)

    with pytest.raises(BuilderRefused) as refused:
        post_turn(tmp_path, thread_id, mode="build", text="write it anyway")
    assert refused.value.code == "approval_required"
    assert refused.value.message
    assert files(tmp_path) == before
    seen = read_thread(tmp_path, thread_id)
    assert seen["refusals"][-1]["error"] == "approval_required"

    approved = approve_plan(tmp_path, thread_id, person_id="sam")
    assert approved["wrote_code"] is False
    assert files(tmp_path) == before

    post_turn(tmp_path, thread_id, mode="chat", text="Still just talking.")
    assert files(tmp_path) == before

    built = post_turn(tmp_path, thread_id, mode="build")
    created = f"{thread_id}/src/approved_build.py"
    assert files(tmp_path) == sorted([*before, created])
    assert built["wrote_code"] is True
    assert built["code_files"] == ["src/approved_build.py"]
    source = (tmp_path / created).read_text(encoding="utf-8")
    assert "def run(" in source
    assert "Add a note the person can read." in source

    again = post_turn(tmp_path, thread_id, mode="build")
    assert again["deduped"] is True
    assert again["wrote_code"] is False
    assert (tmp_path / created).read_text(encoding="utf-8") == source
    assert code_files(tmp_path) == [created]

    post_turn(tmp_path, thread_id, mode="plan", text="Change the note colour.")
    assert code_files(tmp_path) == [created]
    held = (tmp_path / created).read_text(encoding="utf-8")
    with pytest.raises(BuilderRefused) as closed:
        post_turn(tmp_path, thread_id, mode="build")
    assert closed.value.code == "approval_required"
    assert (tmp_path / created).read_text(encoding="utf-8") == held
    assert code_files(tmp_path) == [created]


def test_the_builder_cannot_approve_a_build(tmp_path: Path):
    opened = open_thread(tmp_path, name="notes")
    thread_id = opened["thread_id"]
    post_turn(tmp_path, thread_id, mode="plan", text="Add a note.")
    before = files(tmp_path)
    for actor in ("builder", "Agent", "system", ""):
        with pytest.raises(BuilderRefused) as refused:
            approve_plan(tmp_path, thread_id, person_id=actor)
        assert refused.value.code == "person_required"
        assert files(tmp_path) == before
    with pytest.raises(BuilderRefused):
        post_turn(tmp_path, thread_id, mode="build")
    assert code_files(tmp_path) == []


def test_http_plan_writes_nothing_until_a_person_approves_the_build(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOSS_BUILDER_THREAD_ROOT", str(tmp_path))
    from apps.api.app.routers.gap_1244 import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    opened = client.post("/v1/builder/threads", json={"name": "notes"})
    assert opened.status_code == 201
    thread_id = opened.json()["thread_id"]
    before = files(tmp_path)

    planned = client.post(
        f"/v1/builder/threads/{thread_id}/turns",
        json={"mode": "plan", "text": "Add a note the person can read."},
    )
    assert planned.status_code == 200
    assert planned.json()["wrote_code"] is False
    assert planned.json()["code_files"] == []
    assert files(tmp_path) == before
    assert code_files(tmp_path) == []

    refused = client.post(
        f"/v1/builder/threads/{thread_id}/turns",
        json={"mode": "build", "text": "skip the person"},
    )
    assert refused.status_code == 409
    assert refused.json()["error"] == "approval_required"
    assert refused.json()["message"]
    assert refused.json()["wrote_code"] is False
    assert files(tmp_path) == before

    approved = client.post(
        f"/v1/builder/threads/{thread_id}/approvals",
        json={"person_id": "sam"},
    )
    assert approved.status_code == 200
    assert approved.json()["wrote_code"] is False
    assert files(tmp_path) == before

    built = client.post(
        f"/v1/builder/threads/{thread_id}/turns",
        json={"mode": "build"},
    )
    assert built.status_code == 200
    assert built.json()["wrote_code"] is True
    created = f"{thread_id}/src/approved_build.py"
    assert code_files(tmp_path) == [created]
    assert "def run(" in (tmp_path / created).read_text(encoding="utf-8")

    repeated = client.post(
        f"/v1/builder/threads/{thread_id}/turns",
        json={"mode": "build"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["deduped"] is True
    assert code_files(tmp_path) == [created]
