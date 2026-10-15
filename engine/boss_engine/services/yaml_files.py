"""Read a spec-pack YAML file once per version of the file, not once per call.

The spec pack is static files BOSS ships (and an operator may edit). `EntitlementBrokerService.
list_skills` parsed every skill file on every call — 13,394 parses for three API tests' setup,
nearly all of the 17 s each API test spent before it ran. The parse is the cost, so it is done
once per version of the file (still yaml.safe_load: the same documents, read the same way).

`load_mapping` keys the parsed document on the file's CONTENT (a hash of its bytes; reading a small
file is cheap, parsing it is not), so an edited file is parsed again at once however quickly it was
rewritten, and hands every caller its own deep copy: callers mutate what they get, and a shared cached
object would carry one caller's changes into the next.
"""
from __future__ import annotations

import copy
import hashlib
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml

_CAP = 1024
_lock = threading.Lock()
_cache: "OrderedDict[str, Any]" = OrderedDict()


def load(path: Path | str) -> Any:
    """The YAML document at `path` (any type), parsed once per version of the file."""
    raw = Path(path).read_bytes()
    key = hashlib.sha256(raw).hexdigest()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return copy.deepcopy(_cache[key])
    parsed = yaml.safe_load(raw.decode("utf-8"))
    with _lock:
        _cache[key] = parsed
        while len(_cache) > _CAP:
            _cache.popitem(last=False)
    return copy.deepcopy(parsed)


def load_mapping(path: Path | str) -> dict[str, Any]:
    """`load`, refusing a file whose document is not a mapping."""
    payload = load(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain an object payload")
    return payload


def clear() -> None:
    with _lock:
        _cache.clear()
