"""Balanced runs the full panel, ships the PR anyway, and the app stays previewable.

A reviewer's opinion must not withhold delivery. The point of the review panel in `balanced`
is to ATTACH findings to a PR a human can look at — "22 runs, 2 PRs" is what happens when
dissent blocks instead, and the operator ends up adjudicating exactly the small defects the
agents were hired to handle.

This pins the two halves of that promise so a future change to the gate logic cannot quietly
withdraw it:

    a refuting review still ships       — resolve_gate_failure_action
    a shipped run is previewable        — assert_previewable

    PYTHONPATH=engine .venv/bin/python tests/test_balanced_ships_and_previews.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.services.governed_code_task_service import (  # noqa: E402
    _ADVISORY_HARD_FLOOR, resolve_gate_failure_action,
)
from boss_engine.services.pipeline_profiles import PROFILES  # noqa: E402
from boss_engine.services.preview_service import (  # noqa: E402
    PreviewNotReady, assert_previewable,
)

_failures: list[str] = []


def check(name: str, ok: bool, why: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        _failures.append(f"{name}{f' — {why}' if why else ''}")


def ship(failing: list[str]) -> str:
    return resolve_gate_failure_action(
        failing=failing, advisory_gate=True, rounds_used=0, max_repair_rounds=2)


def main() -> int:
    print("== balanced is advisory, and runs the full panel ==")
    balanced = PROFILES["balanced"]
    check("balanced is advisory", bool(balanced.get("advisory_gate")))
    check("it still uses strong models for review",
          balanced["model"].get("review_code") == "strong"
          and balanced["model"].get("security_check") == "strong")

    print("\n== a refuting reviewer cannot withhold the PR ==")
    for gate in ("review_code", "security_check", "consistency_check", "tests_strong_enough",
                 "tests_passed"):
        check(f"`{gate}` failing still ships", ship([gate]) == "ship")
    check("several failing at once still ships",
          ship(["review_code", "security_check", "tests_passed"]) == "ship")

    print("\n== what legitimately does NOT ship ==")
    # Only "there is nothing to ship" survives as a blocker. That is a fact, not an opinion.
    check("no code produced is repaired, not shipped", ship(["code_written"]) == "repair")
    check("the hard floor is exactly that one thing",
          set(_ADVISORY_HARD_FLOOR) == {"code_written"}, str(_ADVISORY_HARD_FLOOR))

    print("\n== a shipped run is previewable, findings or not ==")
    shipped_with_dissent = {
        "pull_request_url": "https://github.com/o/r/pull/1",
        "status": "completed", "phase": "done",
    }
    try:
        assert_previewable(shipped_with_dissent, "crun_x")
        check("a PR with failing checks is previewable", True)
    except PreviewNotReady:
        check("a PR with failing checks is previewable", False,
              "preview refused a run that legitimately shipped with dissent")
    try:
        assert_previewable({"pull_request_url": "", "status": "running", "phase": "implement"},
                           "crun_y")
        check("a run with no PR yet is refused", False, "would preview a nonexistent build")
    except PreviewNotReady:
        check("a run with no PR yet is refused", True)

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("balanced ships its PR through dissent, and the result can be previewed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
