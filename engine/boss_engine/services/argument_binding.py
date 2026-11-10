"""Bind a held tool call to the arguments that were actually shown.

The digest is over a canonical JSON payload so two dicts that differ only by
key order are the same call, and a changed value is not. The preview is a copy
safe to put on a wait result: secret-named values become "[redacted]".
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

_SECRET_KEYS = frozenset({
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
})


def args_sha256(tool_name: str, arguments: Any) -> str:
    """sha256 hex of the canonical {"tool", "arguments"} payload.

    Non-dict arguments are refused. Key order does not change the digest.
    """
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a dict")
    payload = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def held_call_matches(tool_name: str, arguments: Any, stored_sha: Any) -> bool:
    """True when this call is the one a person approved.

    Older decisions have no digest. A missing or empty stored_sha still matches.
    A present digest matches only the same canonical arguments, and never a
    non-dict.
    """
    if stored_sha is None or stored_sha == "":
        return True
    if not isinstance(arguments, dict):
        return False
    return args_sha256(tool_name, arguments) == stored_sha


def bind_held_call(tool_name: str, arguments: Any) -> dict:
    """Digest and redacted preview stored on an Ask decision.

    A non-dict adds nothing, so a hold still opens when the arguments are
    not a map. The signature and the receipt check are a later slice.
    """
    if not isinstance(arguments, dict):
        return {}
    return {
        "args_sha256": args_sha256(tool_name, arguments),
        "args_preview": redacted_preview(arguments),
    }


def redacted_preview(arguments: dict) -> dict:
    """Copy arguments with secret-named values replaced by "[redacted]"."""
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a dict")
    return _redact(arguments)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() in _SECRET_KEYS:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
