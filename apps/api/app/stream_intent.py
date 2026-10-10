"""Durable voice HTTP intents, with bounded replay storage beside live delivery."""

from __future__ import annotations

import hashlib
import json
import re

VOICE_ROUTES = re.compile(r"^/v1/agents/[^/]+/(?:test|persona/draft)$")


def governs(method: str, path: str) -> bool:
    return method.upper() == "POST" and bool(VOICE_ROUTES.fullmatch(path))


def intent_id(principal: str, key: str) -> str:
    # A key belongs to its actual authenticated principal. The stored HTTP fingerprint
    # independently binds the exact method, path, query and raw body, including project.
    return hashlib.sha256(json.dumps([principal, key]).encode()).hexdigest()


async def record_live(iterator, *, limit: int, finish):
    """Yield immediately; remember at most limit bytes beside the live stream.

    Disconnect, exception, or overflow retains the durable claim. It is an unknown HTTP
    outcome, not permission to contact a provider again. No partial response is replayed.
    The caller's finish callback must also check trusted producer completion: an outer
    middleware iterator may exhaust normally after an upstream generator failed.
    """
    chunks = []
    size = 0
    overflow = False
    async for part in iterator:
        chunk = part if isinstance(part, bytes) else part.encode()
        size += len(chunk)
        if size <= limit and not overflow:
            chunks.append(chunk)
        else:
            overflow = True
            chunks.clear()
        yield chunk
    if not overflow:
        await finish(b"".join(chunks))
