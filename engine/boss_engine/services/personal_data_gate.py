"""Local personal-data gate for a builder message, before any budget reservation.

Deterministic patterns only. No external classifier. The reconciler calls
`before_builder_reserve` at the start of a builder reserve. A card number or a
national id is a block and does not reserve. An email or a phone number is an
ask: the gate returns a decision and does not reserve until `allowed` is true.
A normal app prompt is allowed through to `reserve`.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

# Stable order. A block kind wins over an ask kind when both are present.
_ORDER = ("card", "national_id", "email", "phone")
_BLOCK = frozenset({"card", "national_id"})

# An scp remote (`git@github.com:org/repo`) is not an email address.
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![:\w])"
)
# A separator is required, so a bare digit run (an order id, a port) is not a phone.
_NANP = re.compile(r"(?<!\d)(?:\+1[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)")
_INTL = re.compile(r"(?<!\d)\+\d{1,3}(?:[\s.\-]\d{2,4}){2,4}(?!\d)")
# 13–19 digits, spaces or dashes allowed. Kept only when the digits pass Luhn,
# so an order number of the same length is not a card.
_CARD = re.compile(r"\b\d(?:[ \-]?\d){12,18}\b")
_NATIONAL_ID = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _luhn(digits: str) -> bool:
    total = 0
    alternate = False
    for char in reversed(digits):
        value = int(char)
        if alternate:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
        alternate = not alternate
    return total % 10 == 0


def _has_card(text: str) -> bool:
    for match in _CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def findings_in(message: str) -> list[str]:
    """Kinds present in `message`, in stable order. The matched text is not returned."""
    found: set[str] = set()
    if _has_card(message):
        found.add("card")
    if _NATIONAL_ID.search(message):
        found.add("national_id")
    if _EMAIL.search(message):
        found.add("email")
    if _NANP.search(message) or _INTL.search(message):
        found.add("phone")
    return [kind for kind in _ORDER if kind in found]


def _decision(status: str, findings: list[str]) -> dict[str, Any]:
    return {"kind": "personal_data", "status": status, "findings": list(findings)}


def before_builder_reserve(
    message: Optional[str],
    *,
    allowed: bool = False,
    reserve: Optional[Callable[[], Any]] = None,
) -> dict[str, Any]:
    """Decide whether a builder reserve may run.

    `reserve` is called only when the message is clear, or an ask has been
    allowed. A block never calls it. An ask returns a pending decision and does
    not call it. A refusal from `reserve` is not swallowed.
    """
    text = "" if message is None else str(message)
    findings = findings_in(text)
    if any(kind in _BLOCK for kind in findings):
        return {
            "action": "block",
            "findings": findings,
            "decision": _decision("blocked", findings),
            "reserved": False,
            "reservation": None,
            "error": "personal_data_blocked",
            "message": (
                "A card number or a national id blocks this builder message "
                "before any budget reservation."
            ),
        }
    if findings and not allowed:
        return {
            "action": "ask",
            "findings": findings,
            "decision": _decision("pending", findings),
            "reserved": False,
            "reservation": None,
        }
    reservation = None
    reserved = False
    if reserve is not None:
        reservation = reserve()
        reserved = True
    return {
        "action": "allow",
        "findings": findings,
        "decision": _decision("allowed", findings) if findings else None,
        "reserved": reserved,
        "reservation": reservation,
    }
