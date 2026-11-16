"""Selecting a preview piece to edit, restyle, comment, or regenerate writes one receipt.

The dedupe key is the change. A repeat reserves nothing more and writes no second effect.
Spend is reserved before the effect. A failed effect releases that hold. No model vendor.
"""
from __future__ import annotations

import hashlib

import pytest

from boss_engine.services.element_select import ChangeRequest, ElementSelectError, ElementSelectService


def _req(**over) -> ChangeRequest:
    base = dict(
        org_id="org_1",
        workspace_id="ws_1",
        project_id="proj_1",
        preview_id="proof-hearth",
        element_id="price",
        action="edit",
        change="Fourteen dollars",
        actor="founder",
    )
    base.update(over)
    return ChangeRequest(**base)


def _key(change: str, *, element_id: str = "price", action: str = "edit") -> str:
    digest = hashlib.sha256(change.strip().encode("utf-8")).hexdigest()[:16]
    return f"element-select:org_1:proj_1:proof-hearth:{element_id}:{action}:{digest}"


def test_the_proof_lists_the_pieces_you_can_mark():
    sheet = ElementSelectService().sheet("proof-hearth")
    assert [(e["id"], e["text"]) for e in sheet["elements"]] == [
        ("mast", "Hearth & Rye"),
        ("headline", "Bread that still remembers the oven."),
        ("price", "$14 / loaf"),
        ("cta", "Reserve a Saturday loaf"),
        ("note", "Baked before dawn. Sold out by ten."),
    ]
    assert sheet["plate"] == "04"


def test_an_edit_reserves_then_writes_one_receipt_with_a_dedupe_key():
    seen: list[int] = []
    svc = ElementSelectService(budget_cap_usd_micros=1_000_000)

    # The renderer reads the live hold, so reserve-before-effect is observed, not assumed.
    def watching(element, action, change):
        seen.append(svc.reserved_usd_micros)
        return {"rendered": change.strip(), "style": None, "comment": None}

    got = svc.apply(_req(), renderer=watching)
    assert seen == [50_000]
    assert got["reused"] is False
    assert got["status"] == "succeeded"
    assert got["effect_kind"] == "element_edit"
    assert got["reserved_usd_micros"] == 50_000
    assert got["dedupe_key"] == _key("Fourteen dollars")
    assert got["id"].startswith("rcpt_")
    assert got["rendered"] == "Fourteen dollars"
    assert svc.effect_count == 1
    assert svc.receipt_count == 1
    assert svc.reserved_usd_micros == 50_000
    assert svc.sheet("proof-hearth")["elements"][2]["text"] == "Fourteen dollars"


def test_a_repeat_of_the_same_change_does_not_write_a_second_effect():
    calls = {"n": 0}

    def renderer(element, action, change):
        calls["n"] += 1
        return {"rendered": "Fourteen dollars", "style": None, "comment": None}

    svc = ElementSelectService(budget_cap_usd_micros=1_000_000, renderer=renderer)
    first = svc.apply(_req())
    second = svc.apply(_req(change="  Fourteen dollars  "))
    assert calls["n"] == 1
    assert second["reused"] is True
    assert second["id"] == first["id"]
    assert second["dedupe_key"] == first["dedupe_key"] == _key("Fourteen dollars")
    assert second["effect_count"] == 1
    assert svc.effect_count == 1
    assert svc.receipt_count == 1
    assert svc.reserved_usd_micros == 50_000


def test_a_repeat_still_returns_the_receipt_when_the_budget_is_already_spent():
    svc = ElementSelectService(budget_cap_usd_micros=250_000)
    note = "Warmer, and shorter."
    first = svc.apply(_req(element_id="headline", action="regenerate", change=note))
    assert first["reserved_usd_micros"] == 250_000
    assert first["rendered"] == "The crust cracks when you listen."
    again = svc.apply(_req(element_id="headline", action="regenerate", change=note))
    assert again["reused"] is True
    assert again["id"] == first["id"]
    assert svc.effect_count == 1
    assert svc.reserved_usd_micros == 250_000
    with pytest.raises(ElementSelectError) as denied:
        svc.apply(_req(element_id="headline", action="regenerate", change="A different note."))
    assert denied.value.code == "budget_denied"
    assert denied.value.retryable is False
    assert str(denied.value) == "The budget cannot cover this change."
    assert svc.effect_count == 1
    assert svc.receipt_count == 1
    letter = svc.dead_letter(denied.value.dead_letter_id)
    assert letter["code"] == "budget_denied"
    assert letter["retryable"] is False


@pytest.mark.parametrize(
    ("element_id", "alternate"),
    [
        ("headline", "The crust cracks when you listen."),
        ("price", "$16 / loaf"),
        ("cta", "Hold a Saturday loaf"),
    ],
)
def test_regenerate_uses_the_local_alternate_not_a_model(element_id, alternate):
    svc = ElementSelectService(budget_cap_usd_micros=1_000_000)
    got = svc.apply(_req(element_id=element_id, action="regenerate", change="Try again."))
    assert got["rendered"] == alternate
    assert got["effect_kind"] == "element_regenerate"
    assert got["reserved_usd_micros"] == 250_000
    assert svc.effect_count == 1


def test_restyle_keeps_the_words_and_comment_spends_nothing():
    svc = ElementSelectService(budget_cap_usd_micros=0)
    comment = svc.apply(_req(action="comment", change="Too loud."))
    assert comment["reserved_usd_micros"] == 0
    assert comment["comment"] == "Too loud."
    assert comment["rendered"] == "$14 / loaf"
    assert comment["effect_kind"] == "element_comment"
    assert svc.reserved_usd_micros == 0
    assert svc.effect_count == 1
    again = svc.apply(_req(action="comment", change="Too loud."))
    assert again["reused"] is True
    assert svc.effect_count == 1

    styled = ElementSelectService(budget_cap_usd_micros=1_000_000)
    styled.apply(_req())
    restyle = styled.apply(_req(action="restyle", change="ember"))
    assert restyle["style"] == "ember"
    assert restyle["rendered"] == "Fourteen dollars"
    assert restyle["reserved_usd_micros"] == 80_000
    marked = styled.apply(_req(action="restyle", change="make it louder"))
    assert marked["style"] == "marked"
    assert marked["rendered"] == "Fourteen dollars"
    assert styled.effect_count == 3
    assert styled.reserved_usd_micros == 50_000 + 80_000 + 80_000


def test_a_different_action_on_the_same_words_is_a_second_effect():
    svc = ElementSelectService(budget_cap_usd_micros=1_000_000)
    edit = svc.apply(_req())
    comment = svc.apply(_req(action="comment"))
    assert comment["id"] != edit["id"]
    assert comment["dedupe_key"] == _key("Fourteen dollars", action="comment")
    assert svc.effect_count == 2
    assert svc.reserved_usd_micros == 50_000


def test_a_failed_effect_releases_the_hold_and_can_be_retried():
    calls = {"n": 0}

    def renderer(element, action, change):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("renderer down")
        return {"rendered": "Fourteen dollars", "style": None, "comment": None}

    svc = ElementSelectService(budget_cap_usd_micros=1_000_000, renderer=renderer)
    with pytest.raises(ElementSelectError) as failed:
        svc.apply(_req())
    assert failed.value.code == "effect_failed"
    assert failed.value.retryable is True
    assert "renderer down" in str(failed.value)
    assert svc.reserved_usd_micros == 0
    assert svc.effect_count == 0
    assert svc.receipt_count == 0
    assert svc.dead_letter_count == 1
    assert svc.dead_letter(failed.value.dead_letter_id)["retryable"] is True
    got = svc.apply(_req())
    assert got["reused"] is False
    assert got["rendered"] == "Fourteen dollars"
    assert svc.effect_count == 1
    assert svc.receipt_count == 1
    assert svc.reserved_usd_micros == 50_000


@pytest.mark.parametrize(
    ("over", "code", "message"),
    [
        ({"change": "   "}, "invalid", "Say what should change."),
        ({"action": "paint"}, "invalid", "Choose edit, restyle, comment, or regenerate."),
        ({"element_id": "logo"}, "invalid", "That piece is not on this proof."),
        ({"preview_id": "other"}, "not_found", "That proof is not on the table."),
        ({"project_id": " "}, "invalid", "A project is required."),
    ],
)
def test_a_bad_mark_is_a_visible_error_and_writes_nothing(over, code, message):
    svc = ElementSelectService(budget_cap_usd_micros=1_000_000)
    with pytest.raises(ElementSelectError) as err:
        svc.apply(_req(**over))
    assert err.value.code == code
    assert str(err.value) == message
    assert err.value.dead_letter_id is None
    assert svc.effect_count == 0
    assert svc.receipt_count == 0
    assert svc.reserved_usd_micros == 0
    assert svc.dead_letter_count == 0


def test_http_repeat_does_not_write_a_second_effect_and_a_refusal_is_visible():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers import gap_1242

    gap_1242.default_service = ElementSelectService(budget_cap_usd_micros=1_000_000)
    client = TestClient(FastAPI())
    # include after the app exists so the router closes over the replaced service
    client.app.include_router(gap_1242.router)
    body = {
        "org_id": "org_1",
        "workspace_id": "ws_1",
        "project_id": "proj_1",
        "preview_id": "proof-hearth",
        "element_id": "price",
        "action": "edit",
        "change": "Fourteen dollars",
        "actor": "founder",
    }
    first = client.post("/v1/element-select/apply", json=body)
    second = client.post("/v1/element-select/apply", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["dedupe_key"] == first.json()["dedupe_key"]
    assert second.json()["effect_count"] == 1
    assert gap_1242.default_service.effect_count == 1
    assert gap_1242.default_service.reserved_usd_micros == 50_000

    sheet = client.get("/v1/element-select/preview/proof-hearth", params={"project_id": "proj_1"})
    assert sheet.status_code == 200
    assert sheet.json()["elements"][1]["text"] == "Bread that still remembers the oven."
    missing = client.get("/v1/element-select/preview/no-such-proof", params={"project_id": "proj_1"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "not_found"

    gap_1242.default_service = ElementSelectService(budget_cap_usd_micros=1)
    refused = client.post("/v1/element-select/apply", json={**body, "action": "regenerate", "change": "Again."})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "budget_denied"
    assert refused.json()["detail"]["retryable"] is False
    assert refused.json()["detail"]["dead_letter_id"]

    def boom(element, action, change):
        raise RuntimeError("renderer down")

    gap_1242.default_service = ElementSelectService(budget_cap_usd_micros=1_000_000, renderer=boom)
    failed = client.post("/v1/element-select/apply", json=body)
    assert failed.status_code == 503
    assert failed.json()["detail"]["retryable"] is True
    assert "renderer down" in failed.json()["detail"]["message"]
    assert gap_1242.default_service.reserved_usd_micros == 0
    assert gap_1242.default_service.receipt_count == 0
