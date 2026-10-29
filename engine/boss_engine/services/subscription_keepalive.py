"""The hourly control-plane-only keep-alive: catch a dying model subscription login within the
hour instead of a person noticing the Models screen said "Working" long after it stopped being
true ([L:kgn_2b552c5e148b444f825c] — a dead Codex login showed green on Setup until a build hit
a 401 with no warning).

For each connected subscription source this does a no-cost authenticated call (never an
inference call), refreshes the login first when it is near expiry, records the result on the
source, and raises exactly one ask when a login that was working just stopped.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from . import codex_login, core, model_failures, subscription_sign_in

_log = logging.getLogger("boss.subscription_keepalive")

#: Each occurrence gets its own card — a card raised for an earlier rejection that recovered and
#: died again is a NEW ask, not a repeat of the one still sitting unanswered. The timestamp (not
#: just the provider) is what keeps `core.create_blocking_decision`'s own same-question merge
#: from folding a fresh occurrence into a stale pending one.
_CARD_QUESTION = "The {provider} subscription login stopped working at {at} — sign in again?"


def _ping_codex(binary: Optional[str], spec: Any) -> tuple[bool, str]:
    """The no-cost authenticated call Codex's CLI makes to list the account's models."""
    from .cli_harness import _run_probe

    if not binary:
        return False, "codex CLI is not installed"
    exclude_env = getattr(spec, "env_exclude", ()) if spec is not None else ()
    code, output = _run_probe(binary, ("debug", "models"), timeout=20, exclude_env=exclude_env)
    return code == 0, output


#: provider name (subscription_sign_in.LOGINS key) -> the name of the no-cost ping for it, looked
#: up dynamically (by name, not by binding the function object) so tests can monkeypatch it.
_PING_FUNCS = {"codex": "_ping_codex"}


def _connection_status(conn, project_id: str, source_id: str) -> Optional[str]:
    row = conn.execute(text(
        "SELECT connection_status FROM entitlement_sources"
        " WHERE project_id = :p AND source_id = :s"),
        {"p": project_id, "s": source_id}).fetchone()
    return row[0] if row else None


def _credential_state(conn, project_id: str, source_id: str) -> Optional[str]:
    row = conn.execute(text(
        "SELECT observed_capacity->>'credential_state' FROM entitlement_sources"
        " WHERE project_id = :p AND source_id = :s"),
        {"p": project_id, "s": source_id}).fetchone()
    return row[0] if row else None


def _raise_login_card(conn, project_id: str, source_id: str, provider: str) -> None:
    question = _CARD_QUESTION.format(provider=model_failures.provider_name(provider),
                                     at=datetime.now(timezone.utc).isoformat())
    core.create_blocking_decision(
        conn, project_id, None, question,
        {"source_id": source_id},
        {},
        reason=f"the hourly keep-alive got a rejected login from {provider} source {source_id}",
        risk_tier=2,
    )


def _sweep_one(engine, settings: Any, project_id: str, source_id: str, provider: str) -> None:
    ping_name = _PING_FUNCS.get(provider)
    if ping_name is None:
        return
    ping = globals()[ping_name]
    with engine.begin() as conn:
        if _connection_status(conn, project_id, source_id) != "connected":
            return
        if provider == "codex":
            try:
                codex_login.refresh_if_due()
            except Exception:  # noqa: BLE001 — a refresh failure must not stop the ping
                _log.warning("codex_login refresh_if_due failed", exc_info=True)
        binary = None
        spec = None
        try:
            from .cli_harness import load_specs

            spec = load_specs(settings).get(provider)
            if spec is not None:
                binary = shutil.which(spec.binary)
        except Exception:  # noqa: BLE001
            spec, binary = None, None
        ok, output = ping(binary, spec)
        if ok:
            model_failures.mark_login(conn, project_id, source_id, rejected=False)
            return
        if not model_failures.is_login_rejected(output):
            return  # a transient failure (CLI missing, network blip) — leave state untouched
        was_rejected = _credential_state(conn, project_id, source_id) == "rejected"
        model_failures.mark_login(conn, project_id, source_id, rejected=True)
        if not was_rejected:
            _raise_login_card(conn, project_id, source_id, provider)


def sweep(engine, settings: Any, project_id: str) -> dict[str, Any]:
    """Check every connected subscription source this process knows how to ping."""
    sources = subscription_sign_in.sign_in_sources(settings)
    checked = 0
    for source_id, provider in sources.items():
        if provider not in _PING_FUNCS:
            continue
        _sweep_one(engine, settings, project_id, source_id, provider)
        checked += 1
    return {"checked": checked}
