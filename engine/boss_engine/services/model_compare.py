"""Compare one prompt across models. Each column spends through a tenant gateway.

A budget hold is committed before the gateway is asked. The gateway is the only
path to a model: this module does not import a provider SDK. A column that
fails keeps its own receipt and does not roll back the columns that stood.
A repeat of the same prompt and model returns that receipt and does not spend
again.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .budget import BudgetGovernor
from .core import stable_dedupe_key
from .receipts import ReceiptService

EFFECT_KIND = "model.compare"
CATEGORY = "model"
HOLD_USD_MICROS = 20_000
_MAX_MODELS = 4


class CompareError(ValueError):
    """The request itself is refused, before any column spends."""


class TenantGateway(Protocol):
    """The only spend path a compare may use. Tests pass a fake."""

    def complete(
        self, *, project_id: str, model: str, prompt: str, reservation_id: str,
    ) -> Mapping[str, Any]:
        """Return text and usd_micros. Raise to fail that column."""


class UnconnectedTenantGateway:
    """No gateway is bound. The column fails in the open. Nothing is sent to a vendor."""

    def complete(
        self, *, project_id: str, model: str, prompt: str, reservation_id: str,
    ) -> Mapping[str, Any]:
        raise RuntimeError("Tenant gateway is not connected.")


def compare_prompt(
    engine: Engine,
    *,
    project_id: str,
    prompt: str,
    models: list[str],
    gateway: TenantGateway,
    actor: str = "operator",
) -> dict[str, Any]:
    text_prompt = " ".join(str(prompt or "").split())
    if not text_prompt:
        raise CompareError("Write a prompt.")
    if len(text_prompt) > 8000:
        raise CompareError("Prompt is too long.")
    lanes = _models(models)
    scope = _scope(engine, project_id)
    if scope is None:
        raise CompareError("This project has no tenant scope.")
    from ..config import get_settings

    settings = get_settings()
    columns = [
        _column(engine, settings, scope, project_id, text_prompt, model, gateway, actor)
        for model in lanes
    ]
    return {"prompt": text_prompt, "columns": columns}


def _models(models: list[str]) -> list[str]:
    lanes = [str(model or "").strip() for model in models or []]
    lanes = [model for model in lanes if model]
    if len(lanes) < 2:
        raise CompareError("Name at least two models.")
    if len(lanes) > _MAX_MODELS:
        raise CompareError("Four models is the limit.")
    if len(set(lanes)) != len(lanes):
        raise CompareError("Name each model once.")
    if any(len(model) > 80 or any(ch.isspace() for ch in model) for model in lanes):
        raise CompareError("A model name is one token.")
    return lanes


def _scope(engine: Engine, project_id: str) -> Optional[dict[str, str]]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT w.org_id, p.workspace_id FROM projects p "
                "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
            ),
            {"p": project_id},
        ).mappings().first()
    if row is None:
        return None
    return {"org_id": str(row["org_id"]), "workspace_id": str(row["workspace_id"])}


def _column(
    engine: Engine,
    settings: Any,
    scope: dict[str, str],
    project_id: str,
    prompt: str,
    model: str,
    gateway: TenantGateway,
    actor: str,
) -> dict[str, Any]:
    dedupe_key = stable_dedupe_key(EFFECT_KIND, project_id, prompt, model)
    receipts = ReceiptService()
    with engine.begin() as connection:
        existing = receipts.get_by_dedupe_key(connection, scope["org_id"], dedupe_key)
        if existing and existing["status"] in ("succeeded", "failed", "in_progress"):
            return _from_receipt(existing, model=model, reused=True)
        reservation = BudgetGovernor(settings).reserve(
            connection, project_id, CATEGORY, HOLD_USD_MICROS,
            f"{EFFECT_KIND}: {model}", 15,
        )
        if reservation.get("status") != "reserved":
            return {
                "model": model,
                "status": "failed",
                "text": None,
                "error": str(reservation.get("message") or "Budget reservation denied."),
                "receipt_id": None,
                "dedupe_key": dedupe_key,
                "usd_micros": None,
                "reused": False,
            }
        reservation_id = str(reservation["reservation_id"])
        receipt, _created = receipts.create_or_reuse(
            connection, scope["org_id"], scope["workspace_id"], project_id,
            dedupe_key, EFFECT_KIND,
            {"data_boundary": "internal", "ref": EFFECT_KIND, "model": model,
             "budget_reservation_id": reservation_id},
            {"model": model, "actor": actor},
            data_boundary="internal",
        )
        receipt_id = str(receipt["id"])
    try:
        reply = gateway.complete(
            project_id=project_id, model=model, prompt=prompt, reservation_id=reservation_id,
        )
        answer, micros = _priced(reply)
    except Exception as exc:  # a column failure stays on this column
        message = _close_failed(
            engine, settings, project_id, receipt_id, reservation_id,
            str(exc).strip() or type(exc).__name__,
        )
        return _failed(model, dedupe_key, receipt_id, message)
    try:
        _close_succeeded(engine, settings, project_id, receipt_id, reservation_id, model, answer, micros)
    except Exception as exc:
        message = _close_failed(
            engine, settings, project_id, receipt_id, reservation_id,
            str(exc).strip() or type(exc).__name__,
        )
        return _failed(model, dedupe_key, receipt_id, message)
    return {
        "model": model,
        "status": "succeeded",
        "text": answer,
        "error": None,
        "receipt_id": receipt_id,
        "dedupe_key": dedupe_key,
        "usd_micros": micros,
        "reused": False,
    }


def _priced(reply: Any) -> tuple[str, int]:
    if not isinstance(reply, Mapping):
        raise CompareError("gateway reply is not an object")
    answer = reply.get("text")
    micros = reply.get("usd_micros")
    if not isinstance(answer, str):
        raise CompareError("gateway reply has no text")
    if isinstance(micros, bool) or not isinstance(micros, int) or micros < 0:
        raise CompareError("gateway reply has no usd_micros")
    return answer, micros


def _close_succeeded(
    engine: Engine, settings: Any, project_id: str, receipt_id: str, reservation_id: str,
    model: str, answer: str, micros: int,
) -> None:
    with engine.begin() as connection:
        ReceiptService().mark_succeeded(
            connection, receipt_id, model,
            {"text": answer, "model": model, "usd_micros": micros},
        )
        BudgetGovernor(settings).settle(
            connection, project_id, reservation_id, receipt_id, CATEGORY,
            model, HOLD_USD_MICROS, micros,
        )


def _close_failed(
    engine: Engine, settings: Any, project_id: str, receipt_id: str, reservation_id: str,
    message: str,
) -> str:
    """Record the failure. If that write itself fails, say so and let the other columns finish."""
    try:
        with engine.begin() as connection:
            ReceiptService().mark_failed(connection, receipt_id, {"message": message[:500]})
            BudgetGovernor(settings).release(
                connection, project_id, reservation_id, "model.compare column failed",
            )
    except Exception as exc:
        extra = str(exc).strip() or type(exc).__name__
        return f"{message} ({extra})"[:500]
    return message


def _failed(model: str, dedupe_key: str, receipt_id: str, message: str) -> dict[str, Any]:
    return {
        "model": model,
        "status": "failed",
        "text": None,
        "error": message[:500],
        "receipt_id": receipt_id,
        "dedupe_key": dedupe_key,
        "usd_micros": None,
        "reused": False,
    }


def _json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _from_receipt(row: Mapping[str, Any], *, model: str, reused: bool) -> dict[str, Any]:
    result = _json(row.get("result"))
    error = _json(row.get("error"))
    status = str(row.get("status") or "in_progress")
    scope = _json(row.get("effect_scope"))
    named = str(scope.get("model") or result.get("model") or model)
    if status == "succeeded":
        micros = result.get("usd_micros")
        return {
            "model": named,
            "status": "succeeded",
            "text": result.get("text") if isinstance(result.get("text"), str) else "",
            "error": None,
            "receipt_id": row.get("id"),
            "dedupe_key": row.get("dedupe_key"),
            "usd_micros": micros if isinstance(micros, int) else None,
            "reused": reused,
        }
    if status == "failed":
        return _failed(named, str(row.get("dedupe_key") or ""), str(row.get("id") or ""),
                       str(error.get("message") or "This lane failed.")) | {"reused": reused}
    return {
        "model": named,
        "status": "in_progress",
        "text": None,
        "error": "This lane is still running.",
        "receipt_id": row.get("id"),
        "dedupe_key": row.get("dedupe_key"),
        "usd_micros": None,
        "reused": reused,
    }
