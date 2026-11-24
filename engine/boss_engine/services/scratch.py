"""WHERE A MODEL CLI RUNS: an empty scratch folder OUTSIDE BOSS's own tree (BOSSAPP-1449).

`claude -p` and `codex exec` run as pure reasoning brains in an empty working directory, so they
load no project's CLAUDE.md or AGENTS.md. That directory used to be `<repo_root>/.local-logs/…`,
inside BOSS's own source tree. On an install whose tree is read-only (the enterprise lane, a
hardened container) the mkdir raised OSError, every typed decision caught it as "no model", and
the person was told to connect a model that was connected and fine.

Now the folder is one this user alone owns (XDG cache, ~/.cache/boss, else a per-user folder in
the temp dir, each checked: not a symlink, this uid, mode 0700), and BOSS's tree is never written. When even that cannot be made, `ScratchUnavailable` says what
happened and what to do, and `agent_decision.unavailable_reply` shows it in place of "connect a
model".
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Optional

#: The start of every ScratchUnavailable sentence: how a caller that only holds the error text
#: (a decision's recorded error) recognises it.
LEAD = "BOSS could not make the model CLI's scratch folder"


class ScratchUnavailable(RuntimeError):
    """`str()` is one sentence for a person: what happened, who acts and what to do."""


def _bases() -> list[Path]:
    """Folders this user owns first (XDG cache, ~/.cache), the temp dir last."""
    out = []
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg and os.path.isabs(xdg):
        out.append(Path(xdg) / "boss")
    home = os.path.expanduser("~")
    if home and home != "~" and os.path.isabs(home):
        out.append(Path(home) / ".cache" / "boss")
    out.append(Path(tempfile.gettempdir()) / f"boss-scratch-{os.getuid()}")
    return out


def _owned(path: Path) -> None:
    """Make `path` (and its missing parents), then hold it to: a real directory, not a symlink,
    owned by this user, mode 0700, writable. On a shared host another user could otherwise make
    the folder first and plant a CLAUDE.md the CLI would load."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PermissionError(1, "not a plain folder")
    if st.st_uid != os.getuid():
        raise PermissionError(1, "owned by another user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)
    if not os.access(path, os.W_OK | os.X_OK):
        raise PermissionError(13, "not writable by this user")


def scratch_dir(name: str) -> str:
    """An existing, writable folder this user alone owns, for one model CLI (`claude`, `codex`)."""
    last: Optional[OSError] = None
    tried = _bases()
    for base in tried:
        try:
            _owned(base)
            path = base / name
            _owned(path)
            return str(path)
        except OSError as exc:
            last = exc
    why = (last.strerror if last is not None and last.strerror else type(last).__name__)
    where = tried[-1].parent
    raise ScratchUnavailable(
        f"{LEAD} under {where} ({why}), so the model was never asked. An operator makes "
        "that folder writable for the user BOSS runs as on the BOSS host (in a container, "
        "a writable /tmp), then retries this step.") from last


def explain(error: object) -> str:
    """The ScratchUnavailable sentence inside a recorded error ("ScratchUnavailable: …"), or ""."""
    text = str(error or "")
    at = text.find(LEAD)
    return text[at:].strip() if at >= 0 else ""
