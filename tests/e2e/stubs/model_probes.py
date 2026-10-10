"""What a model provider answers Repo and models' one-question check, chosen by key value.

The model stub (model_stub.py), the API tests and the engine tests answer from here, so a
rejected key, a key with no credit or a rate-limited one looks the same everywhere:

    sk-stub-ok       200, a 1-token answer with usage
    sk-stub-401      401 (Key rejected)
    sk-stub-402      402 insufficient credit (No credit)
    sk-stub-404      404 model_not_found (Model not found)
    sk-stub-429      429 with retry-after 600 (Limited · HH:MM)

Any other key: None, and the caller answers as it always did.
"""
from __future__ import annotations

from typing import Any, Optional

MODES = {
    "sk-stub-ok": (200, {}, {"id": "chatcmpl-stub", "choices": [
        {"message": {"role": "assistant", "content": "OK"}}],
        # A scripted provider invoice value; stub/small has no real pricing table.
        # Paid authority tests must not invent a price or confuse unknown with zero.
        "usage": {"prompt_tokens": 9, "completion_tokens": 1, "cost_usd_micros": 1000}}),
    "sk-stub-401": (401, {}, {"error": {"message": "Incorrect API key provided"}}),
    "sk-stub-402": (402, {}, {"error": {"message": "Insufficient credits. Add more at billing."}}),
    "sk-stub-404": (404, {}, {"error": {"message": "The model does not exist",
                                       "code": "model_not_found"}}),
    "sk-stub-429": (429, {"retry-after": "600"}, {"error": {"message": "Rate limit reached"}}),
}

MODELS = ["stub/small", "stub/large", "openai/gpt-5"]


def answer(key: str, method: str, path: str) -> Optional[tuple[int, dict[str, str], Any]]:
    if (key or "") not in MODES:
        return None
    if method == "GET" and path.rstrip("/").endswith("/models"):
        status = MODES[key][0]
        return (200, {}, {"data": [{"id": m} for m in MODELS]}) if status == 200 \
            else MODES[key]
    return MODES[key]


def http_for_tests(calls: Optional[list] = None):
    """A `setup_checks.Http` answering from the modes above."""
    import sys
    from pathlib import Path

    engine_root = Path(__file__).resolve().parents[3] / "engine"
    if str(engine_root) not in sys.path:
        sys.path.insert(0, str(engine_root))
    from boss_engine.services.setup_checks import HttpAnswer

    def http(method: str, url: str, *, headers: dict[str, str], json_body: Any = None,
             timeout: float = 15.0) -> HttpAnswer:
        key = (headers.get("Authorization") or "").removeprefix("Bearer ").strip() \
            or headers.get("x-api-key", "")
        if calls is not None:
            calls.append((method, url))
        found = answer(key, method, url)
        if found is None:
            return HttpAnswer(0, {}, None, "ConnectError")
        status, out_headers, body = found
        return HttpAnswer(status, out_headers, body)

    return http
