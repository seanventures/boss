"""PIN A HOST'S KEY: a governed SSH connection talks only to the machine that was verified.

Every SSH call BOSS made said `StrictHostKeyChecking=no`, so a deploy would hand its build (and
the deploy key's session) to whatever answered at the host's address. Verifying a host proved
the key worked once; nothing remembered WHICH machine had answered.

Now:
  * Verify records the host's key (a known_hosts entry and its fingerprint) from the
    authenticated session it opens — the operator's deliberate act of trust. Re-verifying
    replaces it, which is how an operator accepts a host that was legitimately rebuilt.
  * Every governed connection (a governed deploy's preflight, transfer, health gate and
    rollback) uses that entry with `StrictHostKeyChecking=yes`. A different key is refused
    with "Host key changed — re-verify the host in Infra."
  * A host verified before pinning existed (hetzner-ci-01) has no entry yet: its first governed
    connection records the key it is shown (`accept-new`), says so on the host, and is strict
    from then on.

The pin is carried in a context variable set around the governed work, so deploy_service._ssh
and its transfer pipeline pick it up without every caller passing it through. Outside a pin,
nothing changes.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from sqlalchemy import text

CHANGED = "Host key changed — re-verify the host in Infra."
_MISMATCH_MARKERS = ("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key verification failed",
                     "host key for", "Offending ")


@dataclass
class Pin:
    host_id: str
    path: str
    strict: bool
    changed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


_PIN: contextvars.ContextVar[Optional[Pin]] = contextvars.ContextVar("boss_host_key_pin",
                                                                     default=None)


def ssh_options() -> list[str]:
    """The host-key options for an ssh command line, under the current pin (if any)."""
    pin = _PIN.get()
    if pin is None:
        return ["-o", "StrictHostKeyChecking=no"]
    return ["-o", f"UserKnownHostsFile={pin.path}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"StrictHostKeyChecking={'yes' if pin.strict else 'accept-new'}"]


def note_result(returncode: int, stderr: str) -> Optional[str]:
    """After an ssh call: the refusal in words when the host's key did not match the pin."""
    pin = _PIN.get()
    if pin is None or not pin.strict or returncode == 0:
        return None
    if any(m in (stderr or "") for m in _MISMATCH_MARKERS):
        pin.changed = True
        return CHANGED
    return None


def fingerprint(known_hosts: str) -> Optional[str]:
    """SHA256 fingerprint (as ssh prints it) of the first key in a known_hosts text."""
    for line in (known_hosts or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            blob = parts[2] + "=" * (-len(parts[2]) % 4)
            try:
                raw = base64.b64decode(blob)
            except ValueError:
                raw = parts[2].encode()
            digest = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
            return f"SHA256:{digest}"
    return None


def stored(connection, host_id: str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT host_key_known_hosts, host_key_fingerprint, host_key_pinned_at, "
             "host_key_pinned_by FROM deployment_hosts WHERE id = :i"),
        {"i": host_id}).mappings().first()
    return dict(row) if row else {}


def _save(connection, host_id: str, known_hosts: str, how: str) -> None:
    connection.execute(
        text("UPDATE deployment_hosts SET host_key_known_hosts = :k, host_key_fingerprint = :f, "
             "host_key_pinned_at = now(), host_key_pinned_by = :b, updated_at = now() "
             "WHERE id = :i"),
        {"k": known_hosts, "f": fingerprint(known_hosts), "b": how, "i": host_id})


@contextmanager
def _db(source):
    """A connection from an engine (its own transaction) or the caller's connection."""
    if hasattr(source, "execute") and not hasattr(source, "connect"):
        yield source
        return
    from ..db import begin

    with begin(source) as connection:
        yield connection


@contextmanager
def pinned(source, host_id: Optional[str], *, capture: bool = False) -> Iterator[Optional[Pin]]:
    """Run governed SSH work against `host_id`'s pinned key.

    `capture=True` (Verify) records whatever key the authenticated session is shown and
    replaces any earlier pin. Otherwise the stored key is enforced; a host with none records
    the key on this first governed connection. No host id: no pin (a legacy target)."""
    if not host_id:
        yield None
        return
    with _db(source) as connection:
        known = "" if capture else str(stored(connection, host_id).get("host_key_known_hosts")
                                       or "")
    fd, path = tempfile.mkstemp(suffix=".known_hosts")
    with os.fdopen(fd, "w") as handle:
        handle.write(known)
    os.chmod(path, 0o600)
    pin = Pin(host_id=host_id, path=path, strict=bool(known))
    token = _PIN.set(pin)
    try:
        yield pin
    finally:
        _PIN.reset(token)
        try:
            captured = open(path).read() if not pin.strict else ""
        except OSError:
            captured = ""
        try:
            os.unlink(path)
        except OSError:
            pass
        if captured.strip() and not pin.changed:
            with _db(source) as connection:
                _save(connection, host_id, captured, "verify" if capture else "first_deploy")


def describe(row: dict[str, Any]) -> dict[str, Any]:
    """The host's key state, in words, for the host drawer."""
    fp = row.get("host_key_fingerprint")
    how = row.get("host_key_pinned_by")
    if not fp:
        return {"pinned": False, "fingerprint": None,
                "words": "Not pinned yet — BOSS records the host's key on the next Verify or "
                         "deploy, and refuses a different key after that."}
    when = {"verify": "when it was verified",
            "first_deploy": "on its first deploy (it was verified before keys were pinned)"}
    return {"pinned": True, "fingerprint": fp,
            "words": f"Key pinned {when.get(str(how), '')}. A different key is refused until "
                     "the host is verified again."}
