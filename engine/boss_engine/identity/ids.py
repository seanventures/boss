"""Identifiers with kind prefixes (IDENTITY.md §3, §11). FROZEN IN PHASE 0, implemented.

Public ids only: an id is never a credential. Tokens and keys are minted by their owners' modules.
"""
from __future__ import annotations

import base64
import secrets

PREFIXES = frozenset({
    "usr", "sva", "agt", "sys", "grp", "arl", "rbd", "ses", "key", "pky", "inv", "idp", "dlg", "acr",
    "rt", "vlt", "egr", "evt", "pcn",
})


def new_id(prefix: str) -> str:
    """`<prefix>_` + 26 lowercase base32 characters (130 random bits)."""
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}")
    raw = base64.b32encode(secrets.token_bytes(17)).decode("ascii").rstrip("=").lower()
    return f"{prefix}_{raw[:26]}"
