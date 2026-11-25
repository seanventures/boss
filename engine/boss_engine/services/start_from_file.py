"""START FROM A FILE: a run's input type for "here is a file, fix it".

Any workflow's build step can take it (workflow_capabilities: ``start_from``,
``start_from_content``, ``target_path``): the run is given a file — a path in the repository, or
an uploaded file's name and text — and the build fixes it IN PLACE at its target. BOSS commits the
original as the change's FIRST commit (governed build, ``input_fidelity.place_original``), so the
change's normal diff reads original → fixed and "Revert to original" is a plain file revert. The
build is held to the remediation contract (input_fidelity): the validator, rules and guides come
from the repository (`.boss/validators.yaml`, `.boss/rulesets/`), never from the workflow.

``remediate(inputs)`` turns those three inputs into the build's ``remediate`` contract.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Mapping, Optional

#: The build inputs this input type reads (popped before the build tool is called).
INPUTS = ("start_from", "start_from_content", "target_path", "start_from_content_sha256")
#: The run input that carries the TRUE upload's sha256 once its text was masked at the start.
CONTENT_SHA_KEY = "start_from_content_sha256"


def _sha(text: str) -> str:
    from .validators import sha256

    return str(sha256(text))


def masked(text: str) -> str:
    """The upload with its secret values replaced by the masker's placeholder; every other byte
    (indentation, the final newline) kept exactly."""
    from .validators import mask

    return str(mask(text) if text else text)


def mask_run_input(input_data: Any) -> Any:
    """A workflow run's input as it is STORED: an uploaded file's secrets masked, the true
    bytes' sha256 kept beside it. Returns the input unchanged when it carries no upload."""
    if not isinstance(input_data, Mapping):
        return input_data
    content = input_data.get("start_from_content")
    if not isinstance(content, str) or not content:
        return input_data
    safe = masked(content)
    if safe == content:
        return input_data
    out = dict(input_data)
    out["start_from_content"] = safe
    out.setdefault(CONTENT_SHA_KEY, _sha(content))
    return out


class StartFromFileError(ValueError):
    pass


def _rel(value: Any, what: str) -> str:
    p = str(value or "").strip().strip("`'\" ")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or "\\" in p or ".." in PurePosixPath(p).parts:
        raise StartFromFileError(f"{what} {p!r} is not a path inside the repository.")
    return p


def remediate(inputs: Mapping[str, Any]) -> Optional[dict]:
    """The build's ``remediate`` contract from "Start from a file", or None when the run did not
    start from one. ``start_from`` is a repo path, or the uploaded file's name when
    ``start_from_content`` carries its text (or an object ``{path|name, content, target}``);
    ``target_path`` is where the fixed file goes (default: the path itself)."""
    value = inputs.get("start_from")
    content = inputs.get("start_from_content")
    target = inputs.get("target_path")
    if isinstance(value, Mapping):
        content = value.get("content", content)
        target = value.get("target") or value.get("target_path") or target
        value = value.get("path") or value.get("name")
    if value in (None, "") and content in (None, ""):
        return None
    if content not in (None, ""):
        name = _rel(value or target, "The uploaded file's name")
        dest = _rel(target, "The target") if target else name
        # NEVER STORED, NEVER COMMITTED: the upload's credential-shaped values are masked to a
        # placeholder by the one masker (validators.mask) and the masked bytes are the change's
        # original; the TRUE bytes' sha256 is kept (``content_sha256``) for the record and the
        # substitution guard. Every other byte is kept exactly, the final newline included.
        raw = str(content)
        given_sha = inputs.get(CONTENT_SHA_KEY) if isinstance(inputs, Mapping) else None
        return {"input": dest, "output": dest, "content": masked(raw), "start_from": True,
                "content_sha256": str(given_sha or _sha(raw))}
    src = _rel(value, "The file to start from")
    dest = _rel(target, "The target") if target else src
    return {"input": dest, "output": dest, "original_path": src, "start_from": True}


def apply(inputs: Mapping[str, Any]) -> dict:
    """A build step's inputs with "Start from a file" turned into ``remediate`` (an explicit
    ``remediate`` wins). The three inputs never reach the build tool."""
    out = {k: v for k, v in inputs.items() if k not in INPUTS}
    if out.get("remediate"):
        return out
    contract = remediate(inputs)
    if contract:
        out["remediate"] = contract
    return out
