"""One prompt on a project's one model, spent through the tenant model gateway.

The completion is ``model_routing.project_chat_adapter`` — the adapter every
project chat turn already uses. This module does not call a provider SDK.
Budget is reserved and one receipt is written before that call, on the same
governor and receipt service the model gateway uses. A repeat of the same
prompt and model returns that receipt and does not call again.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import text

EFFECT = "prompt.playground"
CATEGORY = "model"


class PlaygroundError(RuntimeError):
    """The run cannot start. Nothing was reserved."""


class PlaygroundRefused(PlaygroundError):
    """The budget refused the run. The gateway was not called."""


class PlaygroundFailed(PlaygroundError):
    """The gateway failed, or a previous failure is being shown again."""

    def __init__(self, message: str, *, receipt_id: str, replay: bool = False) -> None:
        super().__init__(message)
        self.receipt_id = receipt_id
        self.replay = replay


class PlaygroundPending(PlaygroundError):
    """A run with this prompt is still open. It is not called again."""

    def __init__(self, message: str, *, receipt_id: str) -> None:
        super().__init__(message)
        self.receipt_id = receipt_id


def call_tenant_model_gateway(
    *, connection: Any, settings: Any, project_id: str, prompt: str, model: str
) -> dict[str, Any]:
    """One completion through the project's tenant model gateway.

    That gateway is ``project_chat_adapter``. Tests pass their own callable
    and never reach here.
    """
    from .model_routing import project_chat_adapter

    adapter = project_chat_adapter(connection, settings, project_id)
    result = adapter.create_text_response(prompt, model=model)
    if not isinstance(result, tuple) or len(result) < 2:
        raise PlaygroundError("The gateway returned no output.")
    if len(result) >= 3:
        provider_ref, text_out, usage = result[0], result[1], result[2]
    else:
        provider_ref, text_out, usage = None, result[0], result[1]
    if not isinstance(text_out, str):
        raise PlaygroundError("The gateway returned no output.")
    if not isinstance(usage, dict):
        usage = {}
    return {"output": text_out, "usage": usage, "provider_ref": provider_ref}


def _dedupe(project_id: str, model: str, prompt: str) -> str:
    material = json.dumps(
        {"model": model, "project_id": project_id, "prompt": prompt},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode()).hexdigest()
    return f"prompt-playground:{digest}"


def _as_dict(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _follow(receipt: dict, prompt: str, model: str) -> dict[str, str]:
    status = receipt.get("status")
    receipt_id = str(receipt["id"])
    if status == "succeeded":
        result = _as_dict(receipt.get("result"))
        output = result.get("output")
        if not isinstance(output, str):
            raise PlaygroundFailed(
                "This run has no output.", receipt_id=receipt_id, replay=True
            )
        stored_prompt = result.get("prompt")
        stored_model = result.get("model")
        return {
            "prompt": stored_prompt if isinstance(stored_prompt, str) else prompt,
            "model": stored_model if isinstance(stored_model, str) else model,
            "output": output,
            "receipt_id": receipt_id,
        }
    if status == "failed":
        error = _as_dict(receipt.get("error"))
        message = str(error.get("message") or "This run failed.")
        raise PlaygroundFailed(message, receipt_id=receipt_id, replay=True)
    raise PlaygroundPending(
        f"This run is still open. Receipt {receipt_id}.", receipt_id=receipt_id
    )


def _money(usage: dict, estimate: int) -> tuple[int, bool]:
    included = usage.get("included_in_subscription") is True or usage.get("billing") in {
        "included",
        "subscription",
    }
    explicit = usage.get("cost_usd_micros")
    known = isinstance(explicit, int) and not isinstance(explicit, bool) and explicit >= 0
    return (int(explicit) if known else estimate), included


def _scope(connection: Any, project_id: str) -> Optional[dict[str, str]]:
    row = connection.execute(
        text(
            "SELECT p.workspace_id, w.org_id FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": project_id},
    ).mappings().first()
    if row is None:
        return None
    return {"workspace_id": str(row["workspace_id"]), "org_id": str(row["org_id"])}


def run(
    engine: Any,
    settings: Any,
    project_id: str,
    *,
    prompt: str,
    model: str,
    actor: str = "playground",
    gateway: Any = None,
) -> dict[str, str]:
    """Reserve, call the gateway once, settle one receipt.

    ``gateway`` defaults to :func:`call_tenant_model_gateway`. A fake gateway
    must accept ``prompt`` and ``model`` and return ``{"output", "usage"}``.
    """
    prompt = str(prompt or "").strip()
    model = str(model or "").strip()
    if not prompt:
        raise PlaygroundError("Write a prompt.")
    if not model:
        raise PlaygroundError("Name one model.")
    if len(prompt) > 8000:
        raise PlaygroundError("That prompt is too long.")
    if len(model) > 200 or any(char in model for char in "\n\r"):
        raise PlaygroundError("That model name is not usable.")
    project_id = str(project_id or "").strip()
    if not project_id:
        raise PlaygroundError("Name a project.")

    from .budget import BudgetGovernor
    from .call_classes import CallClassRefused, authorize
    from .receipts import ReceiptService

    estimate = int(getattr(settings, "agent_turn_budget_usd_micros", None) or 20_000)
    dedupe = _dedupe(project_id, model, prompt)
    receipts = ReceiptService()
    budget = BudgetGovernor(settings)
    replay: Optional[dict] = None
    refusal: Optional[str] = None
    receipt_id = ""
    reservation_id = ""

    with engine.begin() as connection:
        scope = _scope(connection, project_id)
        if scope is None:
            raise PlaygroundError("Project not found.")
        existing = receipts.get_by_dedupe_key(connection, scope["org_id"], dedupe)
        if existing is not None:
            replay = existing
        else:
            try:
                authorize(connection, settings, project_id, stage="playground")
            except CallClassRefused as exc:
                raise PlaygroundRefused(str(exc)) from exc
            reservation = budget.reserve(
                connection,
                project_id,
                CATEGORY,
                estimate,
                f"prompt.playground: {model}",
                15,
            )
            if reservation.get("status") != "reserved":
                refusal = str(reservation.get("message") or "Budget reservation denied.")
            else:
                reservation_id = str(reservation["reservation_id"])
                receipt, created = receipts.create_or_reuse(
                    connection,
                    scope["org_id"],
                    scope["workspace_id"],
                    project_id,
                    dedupe,
                    EFFECT,
                    {
                        "data_boundary": "internal",
                        "ref": dedupe,
                        "model": model,
                        "actor": actor,
                        "budget_reservation_id": reservation_id,
                    },
                    {"prompt": prompt, "model": model},
                    data_boundary="internal",
                )
                if not created:
                    budget.release(
                        connection,
                        project_id,
                        reservation_id,
                        "prompt playground already has this run",
                    )
                    replay = receipt
                else:
                    receipt_id = str(receipt["id"])

    if replay is not None:
        return _follow(replay, prompt, model)
    if refusal is not None:
        raise PlaygroundRefused(refusal)

    try:
        answer = _invoke(engine, settings, project_id, prompt, model, gateway)
        if not isinstance(answer, dict) or not isinstance(answer.get("output"), str):
            raise RuntimeError("The gateway returned no output.")
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:500]
        with engine.begin() as connection:
            receipts.mark_failed(connection, receipt_id, {"message": message})
            budget.release(connection, project_id, reservation_id, "prompt playground failed")
        raise PlaygroundFailed(message, receipt_id=receipt_id) from exc

    output = str(answer["output"])
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    worth, included = _money(usage, estimate)
    provider_ref = str(answer.get("provider_ref") or model)[:200]
    with engine.begin() as connection:
        receipts.mark_succeeded(
            connection,
            receipt_id,
            provider_ref,
            {
                "prompt": prompt,
                "model": model,
                "output": output,
                "usage": usage,
                "budget_reservation_id": reservation_id,
            },
        )
        budget.settle(
            connection,
            project_id,
            reservation_id,
            receipt_id,
            CATEGORY,
            model,
            estimate,
            worth,
            included_in_subscription=included,
        )
    return {"prompt": prompt, "model": model, "output": output, "receipt_id": receipt_id}


def _invoke(engine, settings, project_id: str, prompt: str, model: str, gateway: Any) -> Any:
    fn = gateway or call_tenant_model_gateway
    with engine.connect() as connection:
        return fn(
            connection=connection,
            settings=settings,
            project_id=project_id,
            prompt=prompt,
            model=model,
        )
