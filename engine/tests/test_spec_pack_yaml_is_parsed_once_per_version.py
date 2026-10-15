"""A spec-pack YAML file is parsed once per version of its content (services/yaml_files.py).

list_skills re-parsed every skill file on every call: 13,394 parses during three API tests' setup,
nearly all of the ~17 s each API test spent before it ran. The cache must never serve a stale or
shared document.
"""
from __future__ import annotations

import pytest

from boss_engine.services import yaml_files


@pytest.fixture(autouse=True)
def _fresh():
    yaml_files.clear()
    yield
    yaml_files.clear()


def test_a_file_is_parsed_once_while_it_is_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "skill.yaml"
    path.write_text("id: a\ntools: [x]\n")
    parses = []
    real = yaml_files.yaml.safe_load
    monkeypatch.setattr(yaml_files.yaml, "safe_load", lambda s: parses.append(1) or real(s))
    for _ in range(5):
        assert yaml_files.load_mapping(path) == {"id": "a", "tools": ["x"]}
    assert len(parses) == 1


def test_an_edit_is_read_at_once_even_at_the_same_size_and_instant(tmp_path):
    path = tmp_path / "skill.yaml"
    path.write_text("id: a\n")
    assert yaml_files.load_mapping(path)["id"] == "a"
    path.write_text("id: b\n")                       # same size, same mtime granularity
    assert yaml_files.load_mapping(path)["id"] == "b"


def test_every_caller_gets_its_own_copy(tmp_path):
    path = tmp_path / "skill.yaml"
    path.write_text("id: a\ntools: [x]\n")
    first = yaml_files.load_mapping(path)
    first["tools"].append("mutated")
    first["id"] = "changed"
    assert yaml_files.load_mapping(path) == {"id": "a", "tools": ["x"]}


def test_a_file_that_is_not_a_mapping_is_refused_and_a_missing_one_raises(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="object payload"):
        yaml_files.load_mapping(path)
    assert yaml_files.load(path) == ["a", "b"]
    with pytest.raises(FileNotFoundError):
        yaml_files.load_mapping(tmp_path / "missing.yaml")
