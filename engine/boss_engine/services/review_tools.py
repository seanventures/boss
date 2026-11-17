"""A reviewer whose tools could not run did not review (BOSSAPP-1350).

crun_9f190ed1ede845c09cdc: Codex's read-only sandbox needs user namespaces the container does
not grant, so every command a reviewer seat ran died with `bwrap: No permissions to create a new
namespace`. The seats then judged what they could not read: the acceptance judge called 30 items
"partial: could not be verified" and the adversarial review "refuted" code that had met 29 of
30 the round before.

A seat whose tools failed says nothing about the code, so it is NOT RUN, with the reason — the
same shape as a review that never saw the change (review_input.hollow_reason), which the pipeline
already re-asks and never reads as a verdict. `blocked_reason` is the one place that says so.
"""
from __future__ import annotations

import re
from typing import Optional

#: The words a seat's record starts with when its tools could not run.
NOT_RUN = "NOT RUN"

#: How a sandbox/permission failure reads in what a reviewer says back. Strong signatures only:
#: a reviewer writing "could not be verified" about one item is a finding, not a broken tool.
_BLOCKED = re.compile(
    r"(?i)(bwrap:[^\n]{0,120}"
    r"|no permissions? to create a new (?:user )?namespace"
    r"|(?:user|mount|pid) namespaces? (?:are|is) (?:not|un)\w*"
    r"|landlock\w*[^\n]{0,60}(?:not supported|not enabled|unavailable|failed|denied)"
    r"|seccomp\w*[^\n]{0,60}(?:not supported|unavailable|failed|denied)"
    r"|sandbox (?:initiali[sz]ation|setup|start)\w* (?:failed|error)"
    r"|(?:unshare|clone)\(\)[^\n]{0,40}operation not permitted)")

#: A finding anchored at a file and line is a sign something was read.
_ANCHORED = re.compile(r"(?im)^\s*[-*]?\s*FINDING:\s*\w+\s+\S+:\d+")

#: An item's reason that says the judge never got to look.
_UNVERIFIED = re.compile(
    r"(?i)could not be (?:verified|inspected|read|checked|run|confirmed)"
    r"|unable to (?:verify|inspect|read|run|access)|no (?:command|tool)s? could (?:run|be run)")


def blocked_reason(text: Optional[str]) -> Optional[str]:
    """Why this reviewer's tools could not run (its own words, trimmed), or None.

    Only on a recognised sandbox/permission signature and no anchored finding: a reviewer that
    cites file:line read something."""
    body = str(text or "")
    hit = _BLOCKED.search(body)
    if not hit or _ANCHORED.search(body):
        return None
    return f"the reviewer's tools could not run ({hit.group(0).strip()[:120]})"


def all_unverified(items: list[dict]) -> bool:
    """True when every judged item's reason says it could not be verified: nothing was read.

    Never one item or a few: those are findings. Every item — and at least three, so a tiny
    contract is not mistaken for a blind judge — is a judge that could not look."""
    reasons = [str((i or {}).get("evidence") or "") for i in items or []]
    return len(reasons) >= 3 and all(_UNVERIFIED.search(r) for r in reasons)


def not_run(reason: str) -> str:
    return f"{NOT_RUN}: {reason}"
