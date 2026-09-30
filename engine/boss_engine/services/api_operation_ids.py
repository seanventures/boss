"""Stable public names for mounted HTTP operations.

FastAPI's default ID is based on a handler name and a set of methods. A route
that accepts several methods can therefore publish the same ID for each one,
and the chosen suffix can change between processes. Method and path are the
HTTP operation's actual identity.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def stable_operation_id(method: str, path: str) -> str:
    method = method.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", f"{method}_{path}".lower()).strip("_")
    digest = hashlib.sha256(f"{method} {path}".encode()).hexdigest()[:8]
    return f"{slug[:68]}_{digest}"


def stable_operation_ids(document: dict[str, Any]) -> None:
    """Assign unique IDs to every HTTP operation in an OpenAPI document."""
    ids: set[str] = set()
    for path, operations in document["paths"].items():
        # FastAPI iterates a set for multi-method routes. Make the schema order
        # deterministic as well as its identifiers.
        document["paths"][path] = dict(sorted(operations.items()))
        for method, operation in document["paths"][path].items():
            if method not in HTTP_METHODS:
                continue
            operation_id = stable_operation_id(method, path)
            if operation_id in ids:
                raise ValueError(f"duplicate stable operation ID: {operation_id}")
            operation["operationId"] = operation_id
            ids.add(operation_id)
