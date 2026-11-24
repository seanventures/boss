"""WHERE A MODEL CLI RUNS: an empty scratch folder OUTSIDE BOSS's own tree (BOSSAPP-1449).

`claude -p` and `codex exec` run as pure reasoning brains in an empty working directory, so they
load no project's CLAUDE.md or AGENTS.md. That directory used to be `<repo_root>/.local-logs/…`,
inside BOSS's own source tree. On an install whose tree is read-only (the enterprise lane, a
hardened container) the mkdir raised OSError, every typed decision caught it as "no model", and
the person was told to connect a model that was connected and fine.

Now the folder lives under the process's temporary directory (TMPDIR, else /tmp), per user, and
BOSS's tree is never written. When even that cannot be made, `ScratchUnavailable` says what
happened and what to do, and `agent_decision.unavailable_reply` shows it in place of "connect a
model".
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: The start of every ScratchUnavailable sentence: how a caller that only holds the error text
#: (a decision's recorded error) recognises it.
LEAD = "BOSS could not make the model CLI's scratch folder"


class ScratchUnavailable(RuntimeError):
    """`str()` is one sentence for a person: what happened, who acts and what to do."""


def scratch_dir(name: str) -> str:
    """An existing, writable, empty-by-convention folder for one model CLI (`claude`, `codex`)."""
    base = Path(tempfile.gettempdir()) / f"boss-scratch-{os.getuid()}"
    path = base / name
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not os.access(path, os.W_OK | os.X_OK):
            raise PermissionError(13, "not writable by this user")
    except OSError as exc:
        why = exc.strerror or type(exc).__name__
        raise ScratchUnavailable(
            f"{LEAD} under {base.parent} ({why}), so the model was never asked. An operator "
            "makes that folder writable for the user BOSS runs as on the BOSS host (in a "
            "container, a writable /tmp), then retries this step.") from exc
    return str(path)


def explain(error: object) -> str:
    """The ScratchUnavailable sentence inside a recorded error ("ScratchUnavailable: …"), or ""."""
    text = str(error or "")
    at = text.find(LEAD)
    return text[at:].strip() if at >= 0 else ""
