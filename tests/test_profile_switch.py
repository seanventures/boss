"""A profile can be withdrawn platform-wide, and put back without a code change.

Strict is the most expensive setting BOSS has — strong model on every step, tier floor 2 —
and the QA benchmark endpoint hardcoded it. Withdrawing it by editing that endpoint would
mean finding every other hardcoded `profile="strict"` too, and finding them all again when it
is re-enabled. So the switch is config, applied at the one place every caller funnels
through.

Downgrade rather than refuse: a benchmark that errors because a profile is withdrawn is a
dead end for whoever pressed the button. Running it at `balanced` and recording the
substitution keeps the work moving and keeps the change visible.

    PYTHONPATH=engine .venv/bin/python tests/test_profile_switch.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.config import Settings  # noqa: E402
from boss_engine.services.pipeline_profile_service import (  # noqa: E402
    DEFAULT_PROFILE, FALLBACK_PROFILE, usable_profile,
)
from boss_engine.services.pipeline_profiles import PROFILES  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, why: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        _failures.append(f"{name}{f' — {why}' if why else ''}")


def main() -> int:
    s = Settings()

    print("== strict is withdrawn by default, for now ==")
    got, why = usable_profile("strict", s)
    check("a strict request runs balanced", got == "balanced", f"got {got}")
    check("and says why, naming the switch", bool(why) and "BOSS_DISABLED_PROFILES" in why)
    check("the fallback is a real profile", FALLBACK_PROFILE in PROFILES)

    print("\n== everything else is untouched ==")
    for name in ("balanced", "lean", "vag"):
        got, why = usable_profile(name, s)
        check(f"`{name}` runs as asked", got == name and why is None)
    got, why = usable_profile(None, s)
    check("no request means the platform default", got == DEFAULT_PROFILE)

    print("\n== re-enabling is config, not code ==")
    s.disabled_profiles = ""
    check("clearing the setting restores strict", usable_profile("strict", s)[0] == "strict")
    s.disabled_profiles = "lean,vag"
    check("any profile can be withdrawn", usable_profile("lean", s)[0] == "balanced")
    check("and the rest still run", usable_profile("strict", s)[0] == "strict")
    # Whitespace and case are what an operator actually types into an env var.
    s.disabled_profiles = " Strict , LEAN "
    check("the list tolerates spacing and case",
          usable_profile("strict", s)[0] == "balanced"
          and usable_profile("lean", s)[0] == "balanced")

    print("\n== the downgrade cannot recurse ==")
    s.disabled_profiles = "balanced"
    got, _ = usable_profile("balanced", s)
    check("withdrawing the fallback itself does not loop", got == FALLBACK_PROFILE)

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("profiles can be withdrawn and restored without touching code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
