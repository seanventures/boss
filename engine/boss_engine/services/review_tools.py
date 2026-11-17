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
from typing import Any, Optional

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


def is_tooling_failure(text: Optional[str], path: Optional[str] = None) -> bool:
    """Whether a finding's words are BOSS's own sandbox failing (BOSSAPP-1360, F-42: "every
    command failed before execution because the BOSS command sandbox could not create a
    namespace (`bwrap: ...`)"): no file anchors it and it carries the sandbox's signature.

    ONLY A SIGNATURE: never the reason to drop a finding by itself. Callers apply it to what a
    seat recorded hollow / not_run / tools_blocked said (`drop_blind`, `ledger.stale_rows`) —
    a security finding, or a seat that ran, is never judged by its prose."""
    return not path and bool(_hit(text))


def drop_blind(comments: Any, hollow: Any = None) -> list:
    """`comments` (ReviewComment objects or mappings) without those a BLIND seat raised
    (`hollow`: {stage: reason}, the seats recorded not_run / hollow / tools_blocked)."""
    blind = set(hollow or {})
    out = []
    for c in comments or []:
        get = c.get if isinstance(c, dict) else (lambda k, d=None, _c=c: getattr(_c, k, d))
        if str(get("stage") or "") not in blind:
            out.append(c)
    return out


def blocked_by_transport(stderr: Optional[str], stdout: Optional[str] = None) -> Optional[str]:
    """Why the engine's tools could not run, from the CLI's own output only, or None.

    `stderr` is the CLI's own and is read whole. `stdout` is its JSON event stream: only the
    recorded output of commands (`command_execution` events) is read — never an
    `agent_message`, which is the model talking — and only when EVERY command failed with the
    signature. One command printing it is the app's own test output (`runner_test.go:40: bwrap:
    setting up uid map`), not a sandbox that cannot start."""
    said = _hit(stderr)
    if not said:
        outputs = []
        for line in str(stdout or "").splitlines():
            if "command_execution" not in line:
                continue
            try:
                item = (json.loads(line) or {}).get("item") or {}
            except ValueError:
                continue
            if item.get("type") == "command_execution":
                outputs.append(item.get("aggregated_output") or item.get("output"))
        hits = [_hit(o) for o in outputs]
        if hits and all(hits):
            said = hits[0]
    return f"the reviewer's tools could not run ({said})" if said else None


def reads_nothing(items: list[dict], root: Any = None) -> bool:
    """A judge that cited nothing it could have read, and claimed nothing missing or met: every
    item partial/unjudged, none citing a file:line or a path that is in the tree (`root`). Never
    when any item is MISSING (a finding)."""
    if not items:
        return False
    if any(str((i or {}).get("status")) not in ("partial", "unjudged") for i in items):
        return False
    for i in items:
        why = str((i or {}).get("evidence") or "")
        if _CITES.search(why):
            return False
        if root is not None:
            from .acceptance.verdict import evidence_paths

            if evidence_paths(root, why)[0]:
                return False
    return True


def unseen_reason(result: dict, hollow: Optional[str]) -> Optional[str]:
    """Why a review seat's session did not review: its tools could not run (the ENGINE's
    report, `tools_blocked`) unless the seat still anchored findings, else `hollow`
    (review_input.hollow_reason). NOT RUN — never a refuted or failed verdict."""
    from . import review_comments

    blocked = (result or {}).get("tools_blocked")
    if blocked and not review_comments.parse_findings(str((result or {}).get("summary") or "")):
        return not_run(str(blocked))
    return hollow


def not_run(reason: str) -> str:
    return f"{NOT_RUN}: {reason}"
