"""A reviewer whose tools could not run did not review (BOSSAPP-1350).

crun_9f190ed1ede845c09cdc: Codex's read-only sandbox needs user namespaces the container does
not grant, so every command a reviewer seat ran died with `bwrap: No permissions to create a new
namespace`. The seats then judged what they could not read: the acceptance judge called 30 items
"partial: could not be verified" and the adversarial review "refuted" code that had met 29/30.

A seat whose tools failed says nothing about the code, so it is NOT RUN, with the reason — the
same shape as a review that never saw the change (review_input.hollow_reason).

THE SIGNAL IS THE ENGINE'S, NEVER THE MODEL'S PROSE. A real review of a sandbox or namespace
feature talks about seccomp and namespaces for pages. `blocked_by_transport` reads only what the
CLI itself printed (its stderr, and the output of the commands the agent ran: a Codex
`command_execution` event) and the engine hands the finding to its result as `tools_blocked`.
"""
from __future__ import annotations

import json
import re
from typing import Optional

#: The words a seat's record starts with when its tools could not run.
NOT_RUN = "NOT RUN"

#: How a sandbox/permission failure reads when a CLI or a command prints it. Conservative: only
#: the signatures this failure class is known to print.
_BLOCKED = re.compile(
    r"(?i)(bwrap:[^\n]{0,120}"
    r"|no permissions? to create a new (?:user )?namespace"
    r"|landlock\w*[^\n]{0,60}(?:not supported|not enabled|unavailable|failed|denied)"
    r"|sandbox\((?:landlockrestrict|seccompinstall|seccompbackend)\)"
    r"|(?:unshare|clone)\(\)[^\n]{0,40}operation not permitted)")

#: A file and line in an item's reason: evidence something was read.
_CITES = re.compile(r"[\w./-]+\.\w{1,6}:\d+")


def _hit(text: Optional[str]) -> Optional[str]:
    found = _BLOCKED.search(str(text or ""))
    return found.group(0).strip()[:120] if found else None


def blocked_by_transport(stderr: Optional[str], stdout: Optional[str] = None) -> Optional[str]:
    """Why the engine's tools could not run, from the CLI's own output only, or None.

    `stderr` is read whole. `stdout` is the CLI's JSON event stream: only a command's recorded
    output (`command_execution` events) is read — never an `agent_message`, which is the model
    talking."""
    said = _hit(stderr)
    if not said:
        for line in str(stdout or "").splitlines():
            if "command_execution" not in line:
                continue
            try:
                item = (json.loads(line) or {}).get("item") or {}
            except ValueError:
                continue
            if item.get("type") == "command_execution":
                said = _hit(item.get("aggregated_output") or item.get("output"))
                if said:
                    break
    return f"the reviewer's tools could not run ({said})" if said else None


def reads_nothing(items: list[dict]) -> bool:
    """A judge that cited no file:line anywhere and claimed nothing missing or met: every item
    partial/unjudged and none with evidence. Never when any item is MISSING (a finding)."""
    if not items:
        return False
    if any(str((i or {}).get("status")) not in ("partial", "unjudged") for i in items):
        return False
    return not any(_CITES.search(str((i or {}).get("evidence") or "")) for i in items)


def not_run(reason: str) -> str:
    return f"{NOT_RUN}: {reason}"
