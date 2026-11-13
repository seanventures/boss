"""BOSSAPP-866: exact-value secret redaction, per run.

Pattern redaction (services/redaction.py) knows what a credential LOOKS like: a provider prefix,
a JWT, ``password=...``. A secret with no recognisable shape — a database password, a webhook
signing secret, a Stripe restricted key pasted under a custom name — passes it untouched and
lands in turns, tool logs and PR text.

This registry knows the VALUES instead. Every value BOSS hands out (secret_backends.
resolve_secret_ref, THE place a reference becomes a value) is registered here against the run it
was resolved for, with the forms output tends to re-print it in: the raw value, its base64
(standard and URL-safe, with and without padding) and its URL-encoded forms. ``scrub`` replaces
every live form, in any run's output, by ``[SECRET]``.

Rules:

  * **A value is never written anywhere.** The registry is process memory only. Nothing here
    logs, persists or returns a registered value; ``live_count`` is the only introspection.
  * **Too short to be safe is not registered.** A value under ``MIN_LENGTH`` characters would
    scrub ordinary words out of honest output (a 4-digit PIN, ``admin``); pattern redaction is
    still in force for those.
  * **A run's values outlive the run for a grace period** (a late event, the failure reason) and
    are then forgotten (``release``), as build_services keeps its live passwords.

Empty registry -> ``scrub`` returns its input unchanged (the default path is byte-identical).
"""
from __future__ import annotations

import base64
import threading
import time
from collections.abc import Mapping
from typing import Any, Optional
from urllib.parse import quote, quote_plus

#: Shorter values are not registered: they would mask ordinary words.
MIN_LENGTH = 8
#: How long a run's values stay registered after the run releases them (seconds).
GRACE_SECONDS = 3600.0
#: A value registered outside any run is forgotten this long after its last registration.
UNSCOPED_TTL_SECONDS = 3600.0
#: The placeholder a scrubbed value reads as (the same word build_services uses).
PLACEHOLDER = "[SECRET]"
#: The run key for a value handed out outside any governed run.
UNSCOPED = "-"

_LOCK = threading.Lock()
#: run_id -> {form: expires_at or None (held while the run is live)}
_BY_RUN: dict[str, dict[str, Optional[float]]] = {}


def forms(value: str) -> list[str]:
    """The raw value and the encodings output re-prints a secret in, longest first (so a form
    that contains another is replaced whole)."""
    if not isinstance(value, str) or len(value) < MIN_LENGTH:
        return []
    raw = value.encode("utf-8", "surrogatepass")
    out = {value}
    for enc in (base64.b64encode(raw).decode("ascii"),
                base64.urlsafe_b64encode(raw).decode("ascii")):
        out.add(enc)
        out.add(enc.rstrip("="))
    out.add(quote(value, safe=""))
    out.add(quote_plus(value, safe=""))
    return sorted((f for f in out if len(f) >= MIN_LENGTH), key=lambda f: (-len(f), f))


def current_run_id() -> Optional[str]:
    """The governed run this code is executing for, from the latches a run already sets: the
    harness-failure run scope, the model-call context, then the identity lineage."""
    try:
        from .harness_failures import current_run

        run = current_run()
        if run:
            return str(run)
    except Exception:  # noqa: BLE001 — attribution is best effort, never a failure
        pass
    try:
        from .llm_telemetry import _llm_ctx

        run = (_llm_ctx.get() or {}).get("run_id")
        if run:
            return str(run)
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..identity import lineage

        acting = lineage.current()
        if acting is not None and getattr(acting, "run_id", None):
            return str(acting.run_id)
    except Exception:  # noqa: BLE001
        pass
    return None


def register(value: Optional[str], *, run_id: Optional[str] = None) -> int:
    """Remember `value` (and its encodings) for `run_id` (default: the current run, else
    unscoped). Returns how many forms are now held for it — never the value."""
    held = forms(value or "")
    if not held:
        return 0
    key = str(run_id or current_run_id() or UNSCOPED)
    expires = (time.monotonic() + UNSCOPED_TTL_SECONDS) if key == UNSCOPED else None
    with _LOCK:
        bucket = _BY_RUN.setdefault(key, {})
        for f in held:
            if key == UNSCOPED:
                bucket[f] = max(bucket.get(f) or 0.0, expires or 0.0)
            else:
                bucket[f] = None
    return len(held)


def release(run_id: str, *, grace_seconds: float = GRACE_SECONDS) -> None:
    """The run is over: its values stay scrubbed for `grace_seconds`, then are forgotten."""
    expires = time.monotonic() + max(0.0, grace_seconds)
    with _LOCK:
        bucket = _BY_RUN.get(str(run_id))
        if bucket is not None:
            for f in bucket:
                bucket[f] = expires


def _prune_locked(now: float) -> None:
    for run in list(_BY_RUN):
        bucket = _BY_RUN[run]
        for f in [f for f, exp in bucket.items() if exp is not None and exp <= now]:
            bucket.pop(f, None)
        if not bucket:
            _BY_RUN.pop(run, None)


def known_forms(run_id: Optional[str] = None) -> list[str]:
    """Every live form, longest first: of one run, or (None) of every run in this process."""
    now = time.monotonic()
    with _LOCK:
        _prune_locked(now)
        if run_id is not None:
            pool = set(_BY_RUN.get(str(run_id), {}))
        else:
            pool = {f for bucket in _BY_RUN.values() for f in bucket}
    return sorted(pool, key=lambda f: (-len(f), f))


def live_count(run_id: Optional[str] = None) -> int:
    """How many forms are held (for a run, or overall). Never the values."""
    return len(known_forms(run_id))


def scrub(value: Any, *, run_id: Optional[str] = None) -> Any:
    """`value` with every live registered form replaced by ``[SECRET]`` — a string, or (in a
    dict/list/tuple) every string inside it. A run's output is scrubbed of EVERY run's values,
    not only its own: an exact secret match is never honest output wherever it appears.
    `run_id` narrows to one run's values (tests, and a reader that wants only those)."""
    known = known_forms(run_id)
    if not known:
        return value
    return _scrub(value, known, 0)


def _scrub(value: Any, known: list[str], depth: int) -> Any:
    if isinstance(value, str):
        for form in known:
            if form in value:
                value = value.replace(form, PLACEHOLDER)
        return value
    if depth > 20:
        return value
    if isinstance(value, Mapping):
        return {k: _scrub(v, known, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, known, depth + 1) for v in value)
    return value


def reset_for_tests() -> None:
    with _LOCK:
        _BY_RUN.clear()
