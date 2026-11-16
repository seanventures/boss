"""Mark one piece of a preview: edit, restyle, comment, or regenerate.

A spend is reserved before the effect. The receipt's dedupe key is the change, so a
repeat returns that receipt and does not reserve again or write a second effect.
Regenerate uses a fixed local alternate. Nothing here calls a model vendor.
The ledger is in-process: dedupe holds for this service instance.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Optional
from uuid import uuid4


PROOF_ID = "proof-hearth"

_PRICES = {
    "edit": 50_000,
    "restyle": 80_000,
    "comment": 0,
    "regenerate": 250_000,
}
_ACTIONS = frozenset(_PRICES)
_STYLES = frozenset({"flour", "ember", "pine", "ink"})
_ALTERNATES = {
    "mast": "Hearth & Rye Co.",
    "headline": "The crust cracks when you listen.",
    "price": "$16 / loaf",
    "cta": "Hold a Saturday loaf",
    "note": "Out of the oven at five.",
}
_PIECES = (
    {"id": "mast", "role": "Mast", "text": "Hearth & Rye"},
    {"id": "headline", "role": "Headline", "text": "Bread that still remembers the oven."},
    {"id": "price", "role": "Price", "text": "$14 / loaf"},
    {"id": "cta", "role": "Button", "text": "Reserve a Saturday loaf"},
    {"id": "note", "role": "Note", "text": "Baked before dawn. Sold out by ten."},
)

Renderer = Callable[[dict, str, str], dict]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


def dedupe_key(
    *,
    org_id: str,
    project_id: str,
    preview_id: str,
    element_id: str,
    action: str,
    change: str,
) -> str:
    digest = hashlib.sha256(change.strip().encode("utf-8")).hexdigest()[:16]
    return (
        f"element-select:{org_id}:{project_id}:{preview_id}:{element_id}:{action}:{digest}"
    )


def render_mark(element: dict, action: str, change: str) -> dict:
    """Local stand-in for a model. The words change only for edit and regenerate."""
    text = str(element["text"])
    if action == "edit":
        return {"rendered": change.strip(), "style": None, "comment": None}
    if action == "restyle":
        token = change.strip().lower()
        style = token if token in _STYLES else "marked"
        return {"rendered": text, "style": style, "comment": None}
    if action == "comment":
        return {"rendered": text, "style": None, "comment": change.strip()}
    return {"rendered": _ALTERNATES[str(element["id"])], "style": None, "comment": None}


@dataclass(frozen=True)
class ChangeRequest:
    org_id: str
    workspace_id: str
    project_id: str
    preview_id: str
    element_id: str
    action: str
    change: str
    actor: str


class ElementSelectError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        dead_letter_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.dead_letter_id = dead_letter_id

    def as_detail(self) -> dict:
        return {
            "message": str(self),
            "code": self.code,
            "retryable": self.retryable,
            "dead_letter_id": self.dead_letter_id,
        }


class ElementSelectService:
    def __init__(
        self,
        *,
        budget_cap_usd_micros: int = 5_000_000,
        renderer: Optional[Renderer] = None,
    ) -> None:
        self._cap = budget_cap_usd_micros
        self._renderer = renderer or render_mark
        self._reserved = 0
        self._pieces = [dict(piece) for piece in _PIECES]
        self._by_id = {piece["id"]: piece for piece in self._pieces}
        self._receipts: dict[str, dict] = {}
        self._effects: list[dict] = []
        self._dead: list[dict] = []

    @property
    def effect_count(self) -> int:
        return len(self._effects)

    @property
    def receipt_count(self) -> int:
        return len(self._receipts)

    @property
    def reserved_usd_micros(self) -> int:
        return self._reserved

    @property
    def dead_letter_count(self) -> int:
        return len(self._dead)

    def dead_letter(self, letter_id: Optional[str]) -> dict:
        for letter in self._dead:
            if letter["id"] == letter_id:
                return dict(letter)
        raise KeyError(letter_id)

    def sheet(self, preview_id: str) -> dict:
        self._known_proof(preview_id)
        return {
            "preview_id": PROOF_ID,
            "title": "Hearth & Rye",
            "plate": "04",
            "elements": [
                {"id": piece["id"], "role": piece["role"], "text": piece["text"]}
                for piece in self._pieces
            ],
        }

    def apply(self, request: ChangeRequest, *, renderer: Optional[Renderer] = None) -> dict:
        org_id = _text(request.org_id, "An org is required.")
        _text(request.workspace_id, "A workspace is required.")
        project_id = _text(request.project_id, "A project is required.")
        preview_id = _text(request.preview_id, "That proof is not on the table.")
        element_id = _text(request.element_id, "That piece is not on this proof.")
        actor = _text(request.actor, "An actor is required.")
        action = request.action.strip()
        if action not in _ACTIONS:
            raise ElementSelectError(
                "Choose edit, restyle, comment, or regenerate.",
                code="invalid",
            )
        change = request.change.strip()
        if not change:
            raise ElementSelectError("Say what should change.", code="invalid")
        self._known_proof(preview_id)
        element = self._by_id.get(element_id)
        if element is None:
            raise ElementSelectError("That piece is not on this proof.", code="invalid")

        key = dedupe_key(
            org_id=org_id,
            project_id=project_id,
            preview_id=preview_id,
            element_id=element_id,
            action=action,
            change=change,
        )
        existing = self._receipts.get(key)
        if existing is not None:
            return self._public(existing, reused=True)

        price = _PRICES[action]
        held = False
        if price > 0:
            if self._reserved + price > self._cap:
                raise self._dead_letter(
                    code="budget_denied",
                    message="The budget cannot cover this change.",
                    retryable=False,
                    dedupe_key=key,
                    action=action,
                    element_id=element_id,
                )
            self._reserved += price
            held = True

        render = renderer or self._renderer
        try:
            outcome = render(dict(element), action, change)
            if not isinstance(outcome, dict) or not isinstance(outcome.get("rendered"), str):
                raise RuntimeError("renderer returned no text")
        except Exception as exc:
            if held:
                self._reserved -= price
            raise self._dead_letter(
                code="effect_failed",
                message=f"The {action} did not land. {exc}",
                retryable=True,
                dedupe_key=key,
                action=action,
                element_id=element_id,
            ) from exc

        if action in {"edit", "regenerate"}:
            element["text"] = outcome["rendered"]
        if action == "restyle":
            element["style"] = outcome.get("style")
        if action == "comment":
            element["comment"] = outcome.get("comment")

        effect = {
            "dedupe_key": key,
            "preview_id": preview_id,
            "element_id": element_id,
            "action": action,
            "change": change,
            "rendered": outcome["rendered"],
            "style": outcome.get("style"),
            "comment": outcome.get("comment"),
        }
        self._effects.append(effect)
        stored = {
            "id": _new_id("rcpt"),
            "dedupe_key": key,
            "status": "succeeded",
            "effect_kind": f"element_{action}",
            "reserved_usd_micros": price,
            "rendered": outcome["rendered"],
            "style": outcome.get("style"),
            "comment": outcome.get("comment"),
            "element_id": element_id,
            "action": action,
            "preview_id": preview_id,
            "project_id": project_id,
            "org_id": org_id,
            "actor": actor,
            "change": change,
        }
        self._receipts[key] = stored
        return self._public(stored, reused=False)

    def _public(self, stored: dict, *, reused: bool) -> dict:
        return {**stored, "reused": reused, "effect_count": self.effect_count}

    def _known_proof(self, preview_id: str) -> None:
        if preview_id.strip() != PROOF_ID:
            raise ElementSelectError("That proof is not on the table.", code="not_found")

    def _dead_letter(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        dedupe_key: str,
        action: str,
        element_id: str,
    ) -> ElementSelectError:
        letter_id = _new_id("dl")
        self._dead.append(
            {
                "id": letter_id,
                "code": code,
                "message": message,
                "retryable": retryable,
                "dedupe_key": dedupe_key,
                "action": action,
                "element_id": element_id,
            }
        )
        return ElementSelectError(
            message,
            code=code,
            retryable=retryable,
            dead_letter_id=letter_id,
        )


def _text(value: str, message: str) -> str:
    text = value.strip()
    if not text:
        if message == "That proof is not on the table.":
            raise ElementSelectError(message, code="not_found")
        if message == "That piece is not on this proof.":
            raise ElementSelectError(message, code="invalid")
        raise ElementSelectError(message, code="invalid")
    return text
