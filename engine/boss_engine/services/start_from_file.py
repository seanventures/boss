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
INPUTS = ("start_from", "start_from_content", "target_path")


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
        return {"input": dest, "output": dest, "content": str(content), "start_from": True}
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
