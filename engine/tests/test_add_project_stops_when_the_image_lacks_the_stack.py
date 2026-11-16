"""Add project and Add feature stop before anything starts when the build image
does not provide the stack (BOSSAPP-1161).

"Stack: Go" is a stack even though the word "go" is not a language on its own.
The fix is to add that language to the build image, or to choose one the image
already has. The opt-in "boss toolchains enable go" sentence stays the start
door's, for a language the image can turn on.
"""
from __future__ import annotations

import json

import pytest

from boss_engine.services import stacks, toolchains


@pytest.fixture
def image_without_go(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))


@pytest.fixture
def image_with_go(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"go": {"go": "1.27.4"}}), encoding="utf-8")
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))


def test_the_image_manifest_names_what_it_provides(image_with_go):
    assert {"python", "node", "go"} <= set(stacks.image_languages())


def test_an_image_without_go_provides_python_and_node(image_without_go):
    assert set(stacks.image_languages()) == {"python", "node"}


def test_stack_go_is_refused_before_start_when_the_image_has_no_go(image_without_go):
    blocked = stacks.image_stack_block("Stack: Go")
    assert blocked is not None
    message = blocked["message"]
    assert "add Go to the build image" in message
    assert "choose Node" in message
    assert "boss toolchains enable" not in message
    assert stacks.image_stack_block("Stack: Node") is None
    assert stacks.image_stack_block("Users log in, go to the dashboard and go live") is None
    assert stacks.image_stack_block(
        "Ship the API.", required_stack=[{"component": "api", "language": "go"}]) is not None


def test_stack_go_is_allowed_when_the_image_provides_go(image_with_go):
    assert stacks.image_stack_block("Stack: Go") is None
    assert "go" in stacks.image_languages()


def test_add_project_and_add_feature_ask_the_image_before_they_start():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    add = (root / "apps/api/app/routers/add_project.py").read_text(encoding="utf-8")
    feature = (root / "apps/api/app/routers/project_experience.py").read_text(encoding="utf-8")
    create = add.split("def _create_and_start", 1)[1].split("\ndef ", 1)[0]
    work = feature.split("def project_new_work", 1)[1].split("\ndef ", 1)[0]
    assert "stacks.image_stack_block" in create
    assert "stacks.image_stack_block" in work
    assert create.index("stacks.image_stack_block") < create.index("project_service.create(")
    assert work.index("stacks.image_stack_block") < work.rindex("start_delivery_workflow(")
