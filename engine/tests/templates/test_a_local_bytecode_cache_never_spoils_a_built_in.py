"""A local test run's __pycache__ never makes a built-in template invalid.

Running the template tests writes .pyc files under templates/apps/<t>/stacks/python/tests. The
validator counted them as binary files, so every built-in with Python tests became "invalid" and
was silently dropped from the gallery, locally AND in an image built from that checkout (the
.dockerignore did not exclude them).
"""
from __future__ import annotations

from pathlib import Path

from boss_engine.templates.validator import tree_from_dir, validate_tree

from ._fixtures import REPO_ROOT, write_template


def test_a_pycache_in_a_built_in_directory_is_not_part_of_the_template(tmp_path):
    root = write_template(tmp_path / "t")
    cache = root / "stacks" / "python" / "tests" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "test_health.cpython-313-pytest-9.0.2.pyc").write_bytes(b"\x00\x01binary")
    tree = tree_from_dir(root)
    assert not any("__pycache__" in p for p in tree.files())
    report = validate_tree(tree, source="builtin")
    assert report.ok, report.reasons


def test_the_image_never_takes_bytecode_caches():
    ignored = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    assert "**/__pycache__" in ignored and "**/*.pyc" in ignored
    assert Path(REPO_ROOT / "templates" / "apps").is_dir()
