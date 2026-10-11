"""docs/community/support-matrix.md is generated from the registry and must match it.

Failing? Run `python -m boss_engine.platform matrix --write` and commit the result."""
from __future__ import annotations

from boss_engine.platform.docs import render_support_matrix, support_matrix_path


def test_the_committed_support_matrix_matches_the_registry():
    assert support_matrix_path().read_text(encoding="utf-8") == render_support_matrix()


def test_the_matrix_lists_every_shipped_plugin():
    from boss_engine.platform.registry import discover, plugins_root
    text = render_support_matrix()
    root = plugins_root().resolve()
    for p in discover(root):
        if p.manifest and root in p.directory.resolve().parents:
            assert f"`{p.id}`" in text
