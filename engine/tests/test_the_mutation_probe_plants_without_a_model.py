"""The mutation probe's plant is deterministic code, never a model session (BOSSAPP-1004).

The probe used to spend a write-capable model session whose only job was to flip one line
(87 calls, $13.93/month), while the fix round's re-probe already replanted deterministically
(mutation_probe.reapply). `plant` / `plant_first` flip the first boundary-shaped operator:
the same text plants the same bug, an unplantable file is a probe recorded as not run (never a
model fallback), and the micro-VM substrate receives the mutant as a test command.
"""
from __future__ import annotations

import base64

from boss_engine.services import mutation_probe as mp

PY = (
    "import math\n"
    "\n"
    "def in_range(n):\n"
    "    # boundary: inclusive on both ends\n"
    "    return n >= 1 and n <= 5\n"
)

TS = (
    "export function isAdult(age: number): boolean {\n"
    "  return age >= 18 && age < 120;\n"
    "}\n"
)


def test_a_comparison_is_flipped_and_the_plant_says_where():
    planted = mp.plant("app/range.py", PY)
    assert planted is not None
    assert planted.path == "app/range.py" and planted.line == 5
    assert planted.before.strip() == "return n >= 1 and n <= 5"
    assert planted.after.strip() == "return n < 1 and n <= 5"
    assert planted.mutated != PY and planted.mutated.count("\n") == PY.count("\n")
    assert "app/range.py:5" in planted.description
    assert "`n >= 1 and n <= 5`" in planted.description.replace("return ", "")


def test_the_same_text_plants_the_same_bug():
    assert mp.plant("a.py", PY) == mp.plant("a.py", PY)
    assert mp.plant("a.ts", TS) == mp.plant("a.ts", TS)


def test_js_operators_flip_too():
    planted = mp.plant("src/age.ts", TS)
    assert planted is not None
    assert ">= 18" in planted.before and "< 18" in planted.after


def test_comments_imports_main_guards_and_strings_are_never_the_target():
    text = (
        "# compare == carefully\n"
        "// a == b in a comment\n"
        "import os\n"
        "from typing import Optional\n"
        'MESSAGE = "1 + 1 == 2"\n'
        'if __name__ == "__main__":\n'
        "    print(MESSAGE)\n"
    )
    assert mp.plant("notes.py", text) is None
    css = "body { color: red; }\n"
    assert mp.plant("style.css", css) is None


def test_a_boolean_literal_is_a_last_resort_target():
    planted = mp.plant("flag.py", "ENABLED = True\n")
    assert planted is not None and planted.after == "ENABLED = False"


def test_plant_first_takes_the_first_plantable_target_in_sorted_order():
    files = {"b.py": PY, "a.py": "# only a comment\n", "c.ts": TS}
    planted = mp.plant_first(["c.ts", "b.py", "a.py"], read=files.get)
    assert planted is not None and planted.path == "b.py"
    assert mp.plant_first(["a.py"], read=files.get) is None
    assert mp.plant_first([], read=files.get) is None
    assert mp.plant_first(["missing.py"], read=lambda _p: None) is None


def test_the_planted_bug_is_capturable_and_replantable():
    """The fix-round contract: the suite is strengthened against THIS mutant, and the re-probe
    plants the same one again through the capture/reapply pair the model path used."""
    planted = mp.plant("app/range.py", PY)
    mutant = mp.capture({planted.path: PY}, {planted.path: planted.mutated})
    assert mp.reapply(PY, mutant[planted.path]) == planted.mutated
    moved = "# header\n" + PY
    assert mp.reapply(moved, mutant[planted.path]) == "# header\n" + planted.mutated


def test_the_microvm_apply_command_carries_the_exact_mutant():
    planted = mp.plant("src/age.ts", TS)
    cmd = mp.apply_command(planted)
    assert cmd.endswith("> src/age.ts")
    payload = cmd.split("printf %s ", 1)[1].split(" |", 1)[0]
    assert base64.b64decode(payload).decode("utf-8") == planted.mutated
