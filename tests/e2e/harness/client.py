"""A small HTTP client for the BOSS API, speaking as a named principal.

Two of them exist per target: the OWNER (the console's key, principal `ceo`) for everything an
operator does, and the WORKER (principal `system`) only for what the worker loop does — chiefly
POST /v1/delivery-workflows/run-capabilities, which advances runs. Keeping them separate is the
point: a harness that did everything as one all-powerful key would never see the 403 an operator
gets when the console calls a worker-only route (the "Advance now" bug on boss-1).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Optional


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: Any):
        self.method, self.path, self.status, self.body = method, path, status, body
        detail = body.get("detail") if isinstance(body, dict) else body
        super().__init__(f"{method} {path} -> {status}: {str(detail)[:600]}")


@dataclass
class Api:
    base_url: str
    key: Optional[str]
    principal: str
    project_id: str
    timeout: float = 60.0

    def _request(self, method: str, path: str, *, body: Any = None,
                 query: Optional[dict[str, Any]] = None, idempotency: bool = False) -> Any:
        url = self.base_url.rstrip("/") + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}, doseq=True)
        data = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if idempotency:
            headers["Idempotency-Key"] = f"e2e-{uuid.uuid4().hex}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw.decode(errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed: Any = json.loads(raw)
            except Exception:  # noqa: BLE001
                parsed = raw.decode(errors="replace")
            raise ApiError(method, path, exc.code, parsed) from None

    def get(self, path: str, **query: Any) -> Any:
        return self._request("GET", path, query=query or None)

    def post(self, path: str, body: Any = None, **query: Any) -> Any:
        return self._request("POST", path, body=body if body is not None else {},
                             query=query or None, idempotency=True)

    def patch(self, path: str, body: Any = None) -> Any:
        return self._request("PATCH", path, body=body or {}, idempotency=True)

    def delete(self, path: str, **query: Any) -> Any:
        return self._request("DELETE", path, query=query or None)


def owner_api(target) -> Api:
    return Api(target.api_url, target.owner_key, target.owner_principal, target.project_id)


def worker_api(target) -> Optional[Api]:
    if not target.worker_key:
        return None
    return Api(target.api_url, target.worker_key, "system", target.project_id, timeout=300.0)
