"""Tree-sitter parses declarations the regex scan misses."""
from __future__ import annotations

import pytest

from boss_engine.services.knowledge.code_index import _scan, parse_source, parser_backend


def test_tree_sitter_parses_an_arrow_function_the_scan_misses():
    source = (
        "const reserveStock = () => place(1);\n"
        "function release() { return reserveStock(); }\n"
    )
    scanned = _scan("stock.js", "javascript", source)
    assert "reserveStock" not in scanned.symbols
    assert parser_backend("javascript") == "tree_sitter"
    parsed = parse_source("stock.js", "javascript", source)
    assert parsed.backend == "tree_sitter"
    assert "reserveStock" in parsed.symbols
    assert "release" in parsed.symbols
    assert ("release", "reserveStock") in parsed.calls


@pytest.mark.parametrize(
    ("language", "source", "symbol"),
    [
        ("typescript", "class PickupSlot { ready(): boolean { return true } }\n", "PickupSlot"),
        ("go", "package p\nfunc Reserve() int { return 1 }\n", "Reserve"),
        ("java", "class Reservation { void hold() { release(); } }\n", "hold"),
        ("csharp", "class Reservation { void Hold() { Release(); } }\n", "Hold"),
    ],
)
def test_tree_sitter_reads_each_language(language, source, symbol):
    parsed = parse_source(f"a.{language}", language, source)
    assert parsed.backend == "tree_sitter"
    assert symbol in parsed.symbols
