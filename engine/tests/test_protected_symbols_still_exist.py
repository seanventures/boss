"""P0: every symbol the self-fix policy protects still exists (BOSSAPP-571, spec §8.1, F17).

The `#gates` rules protect named top-level symbols inside files a self-fix may otherwise change.
A rename that is not followed by the policy would silently unprotect a gate, so this fails the
moment any listed symbol (or a `words_from` symbol) is gone from the tree.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services import self_fix_policy as sp

ROOT = Path(__file__).resolve().parents[2]
SYMBOLS = sp.protected_symbols()


def test_the_policy_names_symbols():
    assert len(SYMBOLS) >= 10


@pytest.mark.parametrize("path, name", SYMBOLS, ids=[f"{p}#{n}" for p, n in SYMBOLS])
def test_each_protected_symbol_exists(path, name):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert name in sp.symbol_spans(source), f"{path} no longer defines {name}: update " \
        "packages/specpack/policies/self_fix_paths.yaml with its new name"


def test_the_gate_words_are_read_from_the_tree():
    source = (ROOT / "engine/boss_engine/services/governed_code_task_service.py").read_text()
    words = sp.symbol_words(source, ["_TEST_GATES", "_ALL_GATE_NAMES"])
    assert {"existing_tests_intact", "written_tests_pass"} <= words
