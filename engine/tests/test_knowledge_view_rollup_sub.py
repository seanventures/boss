"""An area or package says what it holds: classes, or functions for Python/JS (Graph round 2)."""
from boss_engine.services.knowledge.view import rollup_sub


def test_classes_first_then_functions_never_zero_classes():
    assert rollup_sub(869, 4519, 85, "area") == "869 classes"
    assert rollup_sub(1, 3, 1, "package") == "1 class"
    assert rollup_sub(0, 24, 3, "area") == "24 functions"
    assert rollup_sub(0, 1, 1, "package") == "1 function"
    assert rollup_sub(0, 0, 4, "area") == "4 packages"
    assert rollup_sub(0, 0, 2, "package") == "2 modules"
    assert "0 classes" not in rollup_sub(0, 0, 0, "area")
