#!/usr/bin/env python3
"""Retired: BOSS no longer takes a subscription credential from you (BOSSAPP-769).

This script used to copy a Codex auth file or a Claude setup token into BOSS. BOSS refused that
import for every caller, so it only ever printed a confusing error. A subscription now signs in
INSIDE BOSS, through the provider's own login: `boss connect codex`, `boss connect claude` or
`boss connect grok` (or + Add model › Subscription › Sign in in the console). Nothing is pasted,
copied or imported. This file stays only to say so; it reads nothing and sends nothing.
"""
from __future__ import annotations

import sys

COMMAND = {"codex": "codex", "claude_code": "claude", "claude": "claude", "grok": "grok"}

MESSAGE = ("BOSS no longer takes a subscription credential from this machine. Sign {what} in "
           "inside BOSS through its own login instead: boss connect {command}  "
           "(or the console: + Add model › Subscription › Sign in). Nothing was read or sent.")


def message(argv: list[str]) -> str:
    provider = next((COMMAND[a] for a in argv if a in COMMAND), "")
    return MESSAGE.format(what="the subscription" if not provider else
                          {"codex": "Codex", "claude": "Claude Code", "grok": "Grok Build"}[provider],
                          command=provider or "codex|claude|grok")


if __name__ == "__main__":
    print(message(sys.argv[1:]), file=sys.stderr)
    raise SystemExit(2)
