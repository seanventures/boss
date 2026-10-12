#!/usr/bin/env python3
"""Refuse application startup when recovery data may have lost effect history.

This module has no application imports, network access, or resume operation.
The runtime path is fixed; environment variables cannot move or clear the hold.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

INTERFACE = "boss-recovery-hold/v1"
RECOVERY_ROOT = Path("/var/lib/boss/recovery")
MARKER = "hold.json"
MAX_MARKER_BYTES = 4096


class RecoveryHeld(RuntimeError):
    """No child process may start."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def check_recovery(root: Path = RECOVERY_ROOT) -> None:
    """Check a root without following filesystem links; an empty root is fresh.

    The parameter supports isolated filesystem tests. Runtime main always uses
    the fixed default above, never an environment or command-line override.
    """
    directory = None
    marker = None
    try:
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or not info.st_mode & 0o444 or not info.st_mode & 0o111:
            raise RecoveryHeld("Recovery directory is unavailable or unsafe.")
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.stat(MARKER, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            # A fresh volume contains no hold. Unexpected entries are not fresh.
            if os.listdir(directory):
                raise RecoveryHeld("Recovery directory contains unrecognized state.")
            return
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o444 or info.st_size > MAX_MARKER_BYTES:
            raise RecoveryHeld("Recovery marker is unreadable or unsafe.")
        marker = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        opened = os.fstat(marker)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise RecoveryHeld("Recovery marker changed during startup.")
        data = os.read(marker, MAX_MARKER_BYTES + 1)
        if len(data) > MAX_MARKER_BYTES:
            raise ValueError("oversize")
        state = json.loads(data, object_pairs_hook=_unique_object)
        if not isinstance(state, dict) or state.get("interface") != INTERFACE or state.get("state") != "held":
            raise ValueError("invalid marker")
        raise RecoveryHeld("Restored state is held for provider-specific reconciliation.")
    except RecoveryHeld:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise RecoveryHeld("Recovery state cannot be safely verified.") from exc
    finally:
        if marker is not None:
            os.close(marker)
        if directory is not None:
            os.close(directory)


def main() -> int:
    try:
        check_recovery()
    except RecoveryHeld as exc:
        print(f"boss: {exc} Application startup refused; preserve recovery evidence.", file=sys.stderr)
        return 78
    if sys.argv[1:] == ["--check"]:
        return 0
    if not sys.argv[1:]:
        print("boss: recovery guard needs a child command.", file=sys.stderr)
        return 64
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
