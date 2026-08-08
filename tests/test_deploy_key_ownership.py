"""The deploy path must never delete a key it does not own.

This is a regression test for a bug that destroyed a real credential. `_ssh_key_file` returns
a path from two sources with different ownership:

  * BOSS_SSH_HOST_PRIVATE_KEY_FILE — the OPERATOR'S key, merely referenced.
  * BOSS_SSH_HOST_PRIVATE_KEY      — material BOSS writes to a temp file and therefore owns.

The cleanup in `deploy_ssh` unlinked whatever it got back, so every deploy in the (preferred,
documented) file-reference mode deleted the operator's private key. The next deploy failed with
"points at ..., which does not exist" — one run AFTER the cause, which is why this survived so
long and why ~/.ssh/kaappu-demo.pem was missing with nobody able to say when it went.

The tests below pin ownership, not the current implementation: a future refactor may change how
cleanup is tracked, but a referenced key must still be there afterwards.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_deploy_key_ownership.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "engine")

from boss_engine.services import deploy_service as ds  # noqa: E402


class _Settings:
    """Only the two fields _ssh_key_file reads."""

    def __init__(self, key_file: str = "", key_material: str = ""):
        self.ssh_host_private_key_file = key_file
        self.ssh_host_private_key = key_material


_FAKE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n-----END OPENSSH PRIVATE KEY-----\n"

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {detail}")


def test_referenced_key_survives_cleanup() -> None:
    """The operator's own key file must still exist after a deploy cleans up."""
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / "operator_key"
        key.write_text(_FAKE_KEY)
        key.chmod(0o600)

        resolved = ds._ssh_key_file(_Settings(key_file=str(key)))
        check("referenced key resolves to the operator's path", resolved == str(key),
              f"got {resolved!r}")

        ds._discard_key_file(resolved)
        check("referenced key SURVIVES cleanup", key.is_file(),
              "the operator's private key was deleted by the deploy cleanup")


def test_materialised_key_is_removed() -> None:
    """A key BOSS wrote from env material is BOSS's to delete — a secret must not linger."""
    resolved = ds._ssh_key_file(_Settings(key_material=_FAKE_KEY))
    check("materialised key was written", os.path.isfile(resolved))
    check("materialised key is 0600", (os.stat(resolved).st_mode & 0o077) == 0)

    ds._discard_key_file(resolved)
    check("materialised key IS removed by cleanup", not os.path.exists(resolved),
          "temp key material was left on disk")


def test_cleanup_is_idempotent_and_safe_on_unknown_paths() -> None:
    """Cleanup runs in a `finally` after arbitrary failures; it must never raise or over-reach."""
    with tempfile.TemporaryDirectory() as tmp:
        stranger = Path(tmp) / "not_ours"
        stranger.write_text("x")
        try:
            ds._discard_key_file(str(stranger))
            ds._discard_key_file(str(stranger))
            ds._discard_key_file("/nonexistent/path/at/all")
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = True
            print(f"    raised: {type(exc).__name__}: {exc}")
        check("cleanup never raises", not raised)
        check("cleanup does not delete unknown files", stranger.is_file(),
              "cleanup removed a file BOSS never created")


def test_referenced_key_survives_repeated_deploys() -> None:
    """The original bug only showed on the SECOND deploy. Prove N deploys leave the key intact."""
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / "operator_key"
        key.write_text(_FAKE_KEY)
        key.chmod(0o600)
        settings = _Settings(key_file=str(key))
        for _ in range(3):
            ds._discard_key_file(ds._ssh_key_file(settings))
        check("key survives three consecutive deploys", key.is_file(),
              "key vanished across repeated deploys — the original failure mode")


if __name__ == "__main__":
    for fn in (test_referenced_key_survives_cleanup,
               test_materialised_key_is_removed,
               test_cleanup_is_idempotent_and_safe_on_unknown_paths,
               test_referenced_key_survives_repeated_deploys):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all deploy key-ownership checks passed")
