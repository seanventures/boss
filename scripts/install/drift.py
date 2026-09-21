#!/usr/bin/env python3
"""How does THIS install differ from a stock one?

WHY THIS EXISTS. A setting proven on one box and set by hand months ago, written down nowhere,
is indistinguishable from a default -- until the next install behaves differently and the only
way to find out why is to read two .env files side by side. That is what happened: the
long-lived install ran ONE coding agent at a time, a fresh install shipped three, this laptop
had four, and nothing anywhere said so.

Two questions, both answerable without knowing what to look for:

  DIFFERENT   settings whose effective value is not the shipped default. This is the install's
              snowflake list. Run it on two boxes and diff the output; that is the whole trick.
  UNREAD      BOSS_* variables that are set and for which no reader can be found anywhere
              in the source tree. They look like tuning to the next person, so they get kept
              and copied forward. The check errs towards silence: three earlier versions of it
              accused live settings -- an API port read by the compose file, a flag read by a
              bare os.getenv, a lease TTL read by the worker's own settings class -- because
              each looked only at the engine's Settings.

Values are printed only for settings that cannot hold a credential. Everything else prints
`set` or `default`, because a drift report is not a reason to put secrets on a terminal.

    python3 scripts/install/drift.py [--all]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "engine"))

from boss_engine.config import (  # noqa: E402
    Settings,
    setting_env_names,
    unread_boss_variables,
)

#: A value is printable unless its name suggests it could carry a credential. Deliberately
#: crude and deliberately over-broad: the cost of hiding a boring value is that someone reads
#: their own .env, and the cost of printing a secret is a secret in a terminal, a screenshot,
#: or a support thread.
SECRETISH = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL", "DSN", "URL", "URI")

GREEN, YELLOW, BOLD, DIM, OFF = "\033[32m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"


def stock_settings() -> Settings:
    """Settings as a brand-new install sees them: no .env, and no inherited environment.

    `Settings(_env_file=None)` is NOT enough, and getting this wrong makes the tool lie in the
    one place it matters. doctor.sh sources .env before calling this, and a container has the
    variables exported outright -- in both cases `_env_file=None` still reads os.environ, so
    "stock" comes back identical to "here" and the report cheerfully announces no drift.
    """
    saved = {}
    try:
        for name in setting_env_names():
            if name in os.environ:
                saved[name] = os.environ.pop(name)
        return Settings(_env_file=None)
    finally:
        os.environ.update(saved)


def env_name(field_name: str, field) -> str:
    alias = getattr(field, "alias", None)
    if isinstance(alias, str):
        return alias.upper()
    validation_alias = getattr(field, "validation_alias", None)
    choices = getattr(validation_alias, "choices", None)
    if choices:
        for choice in choices:
            if isinstance(choice, str) and choice.upper().startswith("BOSS_"):
                return choice.upper()
        for choice in choices:
            if isinstance(choice, str):
                return choice.upper()
    if isinstance(validation_alias, str):
        return validation_alias.upper()
    return field_name.upper()


def _empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def shown(name: str, value) -> str:
    if value is None or value == "":
        return f"{DIM}unset{OFF}"
    if any(word in name for word in SECRETISH):
        return f"{DIM}set{OFF}"
    text = str(value)
    return text if len(text) <= 48 else text[:45] + "..."


def main() -> int:
    show_all = "--all" in sys.argv

    here = Settings()  # reads .env and the environment, exactly as the API does
    stock = stock_settings()

    differences = []
    for field_name, field in Settings.model_fields.items():
        mine = getattr(here, field_name)
        theirs = getattr(stock, field_name)
        if mine == theirs:
            continue
        # `None` and `""` are the same absence wearing two hats, and a report that lists
        # "unset ... unset" as a difference is a report people stop reading.
        if _empty(mine) and _empty(theirs):
            continue
        differences.append((env_name(field_name, field), mine, theirs))
    differences.sort()

    print(f"\n{BOLD}BOSS configuration drift{OFF}  ({REPO})\n")

    if differences:
        print(f"  {BOLD}DIFFERENT from a stock install ({len(differences)}){OFF}")
        print(f"  {DIM}{'variable':<44} {'here':<26} stock{OFF}")
        for name, mine, theirs in differences:
            print(f"  {YELLOW}~{OFF} {name:<42} {shown(name, mine):<26} {shown(name, theirs)}")
    else:
        print(f"  {GREEN}identical to a stock install{OFF}")

    unread = unread_boss_variables(os.environ)
    print()
    if unread:
        print(
            f"  {BOLD}SET BUT UNREAD ({len(unread)}){OFF}  — no reader found anywhere in the"
            f" source tree"
        )
        for name in unread:
            print(f"  {YELLOW}?{OFF} {name}")
    else:
        print(f"  {GREEN}every BOSS_ variable set here is read by something{OFF}")

    if show_all:
        print(f"\n  {DIM}{len(Settings.model_fields)} settings in total{OFF}")
    print(
        f"\n  {DIM}Run this on two installs and diff the output. A line here that is not"
        f"\n  deliberate is a snowflake.{OFF}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
