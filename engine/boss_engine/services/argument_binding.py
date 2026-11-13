"""Bind a held tool call to the arguments that were actually shown.

The digest is over a canonical JSON payload so two dicts that differ only by
key order are the same call, and a changed value is not. The preview is a copy
safe to put on a wait result: secret-named values, and string values shaped
like a credential, are redacted.

BINDING VERSION 2 (BOSSAPP-816). A hold made now is stamped
``binding_version: 2`` and carries ``args_sig``: an HMAC under the identity
server key over the tool, the digest and the preview the person was shown. A
v2 decision FAILS CLOSED: no digest, no signature, a signature that does not
verify, or a server key that cannot be read, and the call does not run. A
decision without the stamp (made before this slice) keeps the lenient v1 rule
(no digest still matches), so an in-flight approval is never broken.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Mapping, Optional

#: The stamp a decision made by this code carries. Below 2 is the lenient v1 rule.
BINDING_VERSION = 2
#: The signature scheme, the prefix of ``args_sig``: HMAC-SHA256 under the identity server key.
SIGNATURE_SCHEME = "hs1"
_SIGNATURE_LABEL = "argument-binding"
#: Exactly what a redacted value reads as (apps/web/app/held-arguments.tsx relies on it).
REDACTED = "[redacted]"
#: A refusal says this, on every door (McpArgumentsChanged's words).
CHANGED = "arguments do not match the approved call"

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
    """The v1 rule: True when this call is the one a person approved.

    Older decisions have no digest. A missing or empty stored_sha still matches.
    A present digest matches only the same canonical arguments, and never a
    non-dict. New code checks with binding_holds, which applies this only to
    an unstamped decision.
    """
    if stored_sha is None or stored_sha == "":
        return True
    if not isinstance(arguments, dict):
        return False
    return args_sha256(tool_name, arguments) == stored_sha


def bind_held_call(tool_name: str, arguments: Any) -> dict:
    """Digest, redacted preview, version stamp and signature stored on an Ask decision.

    A non-dict adds nothing, so a hold still opens when the arguments are not
    a map. When the server key cannot be read the hold still opens, stamped v2
    WITHOUT a signature, so it can never be released (fail closed) rather than
    falling back to the unsigned rule. The checks are binding_holds (agent
    resume, kernel resume, code-task resume) and approved_arguments_match (the
    receipt door).
    """
    if not isinstance(arguments, dict):
        return {}
    digest = args_sha256(tool_name, arguments)
    preview = redacted_preview(arguments)
    bound = {
        "args_sha256": digest,
        "args_preview": preview,
        "binding_version": BINDING_VERSION,
    }
    signature = sign_binding(tool_name, digest, preview)
    if signature:
        bound["args_sig"] = signature
    return bound


def _signed_payload(tool_name: str, digest: str, preview: Any) -> str:
    return json.dumps(
        {"v": BINDING_VERSION, "tool": tool_name, "args_sha256": digest,
         "args_preview": preview},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sign_binding(tool_name: str, digest: str, preview: Any) -> Optional[str]:
    """``hs1:<hex>``: the HMAC of the tool, digest and preview; None without a server key."""
    try:
        from ..identity.authn_support import keyed_hash

        mac = keyed_hash(_SIGNATURE_LABEL, _signed_payload(tool_name, digest, preview))
    except Exception:  # noqa: BLE001 — no key: the hold is unsigned, so it fails closed
        return None
    return f"{SIGNATURE_SCHEME}:{mac}"


def signature_valid(tool_name: str, digest: Any, preview: Any, signature: Any) -> bool:
    """True only when ``signature`` is this server's hs1 HMAC of exactly these values."""
    if not isinstance(digest, str) or not digest or not isinstance(signature, str):
        return False
    if signature.partition(":")[0] != SIGNATURE_SCHEME:
        return False
    expected = sign_binding(tool_name, digest, preview)
    return expected is not None and hmac.compare_digest(expected, signature)


def binding_version(recommendation: Any) -> int:
    """2 when the decision is stamped, or carries a signature at all (so a stripped stamp
    cannot downgrade a signed decision to the lenient rule); else 1."""
    if not isinstance(recommendation, Mapping):
        return 1
    stamped = _int(recommendation.get("binding_version"))
    if recommendation.get("args_sig"):
        return max(BINDING_VERSION, stamped)
    return max(1, stamped)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def binding_holds(tool_name: str, arguments: Any, recommendation: Any) -> bool:
    """THE ONE CHECK that this call is the one a person approved, for every door.

    v1 (no stamp): held_call_matches, so a missing digest still matches.
    v2: the digest must be present and equal this call's, and ``args_sig``
    must verify over the tool, that digest and the stored preview. Anything
    missing refuses.
    """
    rec = recommendation if isinstance(recommendation, Mapping) else {}
    if binding_version(rec) < BINDING_VERSION:
        return held_call_matches(tool_name, arguments, rec.get("args_sha256"))
    stored = rec.get("args_sha256")
    if not isinstance(stored, str) or not stored or not isinstance(arguments, dict):
        return False
    if not hmac.compare_digest(args_sha256(tool_name, arguments), stored):
        return False
    return signature_valid(tool_name, stored, rec.get("args_preview"), rec.get("args_sig"))


def redacted_preview(arguments: dict) -> dict:
    """Copy arguments with secret-named values replaced by "[redacted]".

    A string value shaped like a credential (a provider token, a JWT, a bearer
    header, ``password=...``, a URL's password) is masked too, whatever its key
    is called (services/redaction.py's credential patterns): a value that is
    nothing but a credential becomes "[redacted]", a longer one keeps its words
    with the credential masked in place.
    """
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a dict")
    return _redact(arguments)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() in _SECRET_KEYS:
                redacted[key] = REDACTED
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_value(value)
    return value


_PLACEHOLDER = re.compile(r"\[[A-Z_]+\]")


def _redact_value(value: str) -> str:
    if len(value) < 8:
        return value
    from .redaction import redact_secrets

    masked = redact_secrets(value)
    if masked == value:
        return value
    if not _PLACEHOLDER.sub("", masked).strip(" \t\r\n\"'"):
        return REDACTED
    return masked
