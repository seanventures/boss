"""THE ONE CHECK of a host-attached delegated run token (P8b S13, BOSSAPP-1074; HOST_API.md §4.8).

boss-hostd attaches, on every forward to a destination, the run's token minted for THAT
destination's audience (`X-Boss-Delegation`) and a DPoP proof its per-run key signed over the
forwarded method and URL (`DPoP`). Every destination — the model gateway (boss:model), the MCP
gateway (boss:mcp), the run plane (boss:run_plane) and egress (boss:egress) — calls `check` with
its own audience, so a token for one is refused by all the others (`bad_aud`), a proof from
another key is `dpop_key_mismatch`, a replayed proof `dpop_replay`, and a token for another run
`mismatch`.

Required or not is the caller's answer (fleet.host_tokens_required: per host, from the
`host_tokens` capability it advertised; sticky). A host that predates S13 may forward without one.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

DELEGATION_HEADER = "x-boss-delegation"
DPOP_HEADER = "dpop"


class DelegationRefused(Exception):
    """`reason` is the code the destination answers with: run_token_<reason>."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def presented(headers: Mapping[str, str]) -> bool:
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    return bool((h.get(DELEGATION_HEADER) or "").strip())


def runs_agree(bound: str, run_id: str) -> bool:
    """A token for the run, or for its parent / child run (`<run>/<name>`)."""
    bound, run_id = str(bound or ""), str(run_id or "")
    return bool(bound) and (bound == run_id or run_id.startswith(bound + "/")
                            or bound.startswith(run_id + "/"))


def check(connection: Any, headers: Mapping[str, str], *, audience: str, run_id: str,
          method: str, url: str, required: bool) -> Optional[Any]:
    """The verified claims, or None when no token was attached and none is required. Raises
    DelegationRefused. The proof's jti is spent in `connection` (commit it even on a refusal)."""
    from . import tokens

    h = {str(k).lower(): str(v) for k, v in headers.items()}
    token = (h.get(DELEGATION_HEADER) or "").strip()
    if not token:
        if required:
            raise DelegationRefused("required")
        return None
    try:
        claims = tokens.verify_run_token(token, audience=audience,
                                         dpop_proof=(h.get(DPOP_HEADER) or "").strip() or None,
                                         htm=method, htu=url, db=connection)
    except tokens.TokenError as exc:
        raise DelegationRefused(str(exc.reason)) from None
    if not runs_agree(str(claims.task.run_id or ""), run_id):
        raise DelegationRefused("mismatch")
    return claims


__all__ = ["DELEGATION_HEADER", "DPOP_HEADER", "DelegationRefused", "check", "presented",
           "runs_agree"]
