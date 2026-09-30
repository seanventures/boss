#!/usr/bin/env python3
"""Bind Community owner and worker credentials without losing operator-added keys.

The installer calls this on its temporary .env after restoring the previous
credentials. It never reads values as shell code or writes credential values to
stdout/stderr. A validation failure leaves the live .env untouched.
"""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path


def _values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and not key.startswith("#"):
            values.setdefault(key, value)
    return values


def _key_list(value: str) -> list[str]:
    return [key.strip() for key in value.split(",") if key.strip()]


def _principal_map(value: str) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        delimiter = "=" if "=" in entry else ":"
        token, found, principal = entry.partition(delimiter)
        if not found or not token.strip() or not principal.strip():
            raise ValueError("Existing API principal map is malformed")
        bindings[token.strip()] = principal.strip()
    return bindings


def _set(lines: list[str], key: str, value: str) -> list[str]:
    replacement = f"{key}={value}"
    result: list[str] = []
    found = False
    for line in lines:
        if line.startswith(f"{key}="):
            if not found:
                result.append(replacement)
                found = True
        else:
            result.append(line)
    if not found:
        result.append(replacement)
    return result


def reconcile(path: Path, *, ensure_worker: bool = False) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    values = _values(lines)
    for key in (
        "BOSS_API_KEY", "BOSS_WORKER_API_KEY", "BOSS_API_KEYS",
        "BOSS_API_KEY_PRINCIPALS", "BOSS_WORKER_PRINCIPAL",
    ):
        previous = [line.split("=", 1)[1] for line in lines if line.startswith(f"{key}=")]
        if len(set(previous)) > 1:
            raise ValueError("Conflicting Community API credential settings")
    owner = values.get("BOSS_API_KEY", "").strip()
    worker = values.get("BOSS_WORKER_API_KEY", "").strip()
    if ensure_worker and not worker:
        worker = secrets.token_hex(24)
    if not owner or not worker or owner == worker:
        raise ValueError("Separate owner and worker API keys are required")
    # API key syntax is intentionally narrower than arbitrary .env syntax:
    # commas separate keys and ':'/'=' separate principal bindings.
    if any(char in key for key in (owner, worker) for char in ",:=\n\r"):
        raise ValueError("Owner or worker API key cannot be represented in the principal map")

    keys = list(dict.fromkeys([*_key_list(values.get("BOSS_API_KEYS", "")), owner, worker]))
    bindings = _principal_map(values.get("BOSS_API_KEY_PRINCIPALS", ""))
    bindings[owner] = "ceo"
    bindings[worker] = "system"
    for key, value in (
        ("BOSS_API_KEY", owner),
        ("BOSS_WORKER_API_KEY", worker),
        ("BOSS_API_KEYS", ",".join(keys)),
        ("BOSS_API_KEY_PRINCIPALS", ",".join(f"{key}:{principal}" for key, principal in bindings.items())),
        ("BOSS_WORKER_PRINCIPAL", "system"),
    ):
        lines = _set(lines, key, value)
    # Remote archive updates apply this to the live .env; replace it atomically
    # so a failed write cannot leave the previous install without credentials.
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f"{path.name}.credentials.", delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write("\n".join(lines) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2:
            reconcile(Path(sys.argv[1]))
        elif len(sys.argv) == 3 and sys.argv[1] == "--ensure-worker":
            reconcile(Path(sys.argv[2]), ensure_worker=True)
        else:
            raise ValueError("Expected [--ensure-worker] ENV_FILE")
    except (IndexError, OSError, UnicodeError, ValueError) as error:
        # Error messages above contain no credential values.
        print(f"Community API credential reconciliation failed: {error}", file=sys.stderr)
        sys.exit(1)
