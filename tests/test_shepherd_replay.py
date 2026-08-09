"""Replay real production shapes through the shepherd.

Five versions of the in-flight shepherd shipped with every unit test green and every one of
them wrong about production. The unit tests asserted what a snapshot MEANS; none asserted what
the database actually PUTS IN ONE. `was_leased` was false where I assumed true, `job_status`
said `deadlettered` where I assumed queued, and a carrying job went terminal one minute after
a run was launched — so the shepherd killed work an operator had just asked for.

This replays rows captured from the real databases against the classifier and the safety
gate. Every case records how it was previously got wrong, so a regression names its own
history instead of just going red.

    PYTHONPATH=engine .venv/bin/python tests/test_shepherd_replay.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "engine")

from boss_engine.services.shepherd_watch import (  # noqa: E402
    RunSnapshot, act_now, assess,
)

FIXTURE = Path(__file__).parent / "fixtures" / "shepherd_production_snapshots.json"
_failures: list[str] = []


def check(name: str, got, want, extra: str = "") -> None:
    if got == want:
        print(f"  PASS  {name}")
        return
    _failures.append(f"{name}: got {got!r}, want {want!r}{f' ({extra})' if extra else ''}")
    print(f"  FAIL  {name}\n          got  {got!r}\n          want {want!r}")
    if extra:
        print(f"          {extra}")


def main() -> int:
    data = json.loads(FIXTURE.read_text())
    cases = data["cases"]
    print(f"replaying {len(cases)} shapes captured from real databases\n")

    for case in cases:
        print(f"— {case['name']}")
        snap = RunSnapshot(**case["snapshot"])
        verdict = assess(snap)
        acts, why = act_now(verdict, snap)
        history = case.get("previously_misclassified_as")
        hint = f"previously: {', '.join(history)}" if history else ""

        check("   classification", verdict.name, case["expect_class"], hint)
        check("   action", verdict.action, case["expect_action"])
        check("   acts unattended", acts, case["expect_acts_now"], why)

    # A property no individual case can express: nothing destructive may fire on young work
    # unless it is a duplicate, whose safety comes from the earlier twin still running. This
    # is the rule the two-minute kill violated, stated once rather than per class.
    print("\n— the safety floor holds across every class")
    from boss_engine.services.shepherd_watch import (
        DESTRUCTIVE_ACTIONS, MIN_AGE_FOR_DESTRUCTIVE_MINUTES, REDUNDANT_DUPLICATE,
    )
    violations = []
    for case in cases:
        snap = RunSnapshot(**case["snapshot"])
        verdict = assess(snap)
        acts, _ = act_now(verdict, snap)
        if (acts and verdict.action in DESTRUCTIVE_ACTIONS
                and snap.minutes_running < MIN_AGE_FOR_DESTRUCTIVE_MINUTES
                and verdict.klass != REDUNDANT_DUPLICATE):
            violations.append(f"{case['name']} ({snap.minutes_running}m)")
    check("   no destructive action on young work", violations, [])

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"all {len(cases)} production shapes classify correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
