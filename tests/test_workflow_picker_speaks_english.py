"""The workflow picker's confirmation, in the words on the screen.

    New project.new work now uses 'research_led_discovery'.

That is what the Command Center printed beside a dropdown reading "Research-Led Discovery":
a selector value (a key in a unique index) and a workflow key, to confirm a choice the
operator had just made in plain English. Their question — "why i'm seeing this" — is the
right one. Both identifiers have display names, and both were one column away.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_workflow_picker_speaks_english.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.services.workflow_catalogue import (  # noqa: E402
    WORK_CLASSES, WORK_CLASS_LABELS, work_class_label,
)

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'' if ok else f'  <- {detail}'}")
    if not ok:
        _failures.append(name)


def main() -> int:
    print("== every work class has a label ==")
    for work_class in WORK_CLASSES:
        label = work_class_label(work_class)
        check(f"{work_class} has one", work_class in WORK_CLASS_LABELS, "missing")
        # The whole point: the label must not be the identifier.
        check(f"{work_class} is not its own key", label != work_class, label)
        check(f"{work_class} has no dotted identifier in it", "." not in label, label)

    print("== an unknown class degrades to the key, not to nothing ==")
    # A blank label would produce "Saved — now use X.", which reads as though it applies to
    # everything. The key is ugly and honest.
    check("unknown falls back", work_class_label("made.up") == "made.up")
    check("empty falls back", work_class_label("") == "")

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): {', '.join(_failures)}")
        return 1
    print("the picker speaks English")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
