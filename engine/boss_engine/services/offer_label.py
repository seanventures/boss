"""The text on an offered action's button, when the model did not write one worth reading.

THREE BUTTONS, ONE WITH NOTHING ON IT. Asked to set up three projects, the agent offered three
held writes; two carried the model's labels and the first read "set project setting" — no
project, no key, no value, so the operator could not tell what pressing it would do. The
fallback was the tool's name in words, in five places. It is one function now, and it says what
the call does from the call itself: the tool, the project it acts in, and its arguments
(`key = value` when it has both).

    Set project setting in northpeak-bank-a: knowledge_graph = off
"""
from __future__ import annotations

from typing import Any, Optional

MAX_LEN = 90

#: Arguments that say nothing a person reads on a button.
_SILENT = frozenset({"project_id", "reason", "why", "label", "note", "notes", "description"})


def _said(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = " ".join(value.split())
        return text if len(text) <= 40 else text[:39] + "…"
    return ""


def fallback_label(tool: str, args: Optional[dict] = None) -> str:
    """Button text built from the call: tool in words, `in <project>`, then its arguments."""
    name = str(tool or "").strip()
    head = (name.replace("_", " ").strip() or "run this action")
    head = head[:1].upper() + head[1:]
    args = {k: v for k, v in dict(args or {}).items()
            if not str(k).startswith("_") and v not in (None, "", [], {})}
    project = _said(args.pop("project", ""))
    if project:
        head += f" in {project}"
    parts: list[str] = []
    if "key" in args and "value" in args:
        key, value = _said(args.pop("key")), _said(args.pop("value"))
        if key:
            parts.append(f"{key} = {value}" if value else key)
    for k, v in args.items():
        if k in _SILENT:
            continue
        text = _said(v)
        if not text:
            continue
        # A boolean reads only with its name ("allowed: no"); a value names itself.
        parts.append(f"{str(k).replace('_', ' ')}: {text}" if isinstance(v, bool) else text)
    label = head + (": " + ", ".join(parts) if parts else "")
    return label if len(label) <= MAX_LEN else label[:MAX_LEN - 1] + "…"


def _bare(label: str, tool: str) -> bool:
    """A label that is only the tool's name says nothing the button did not already."""
    norm = lambda t: " ".join(str(t or "").replace("_", " ").lower().split())  # noqa: E731
    return not norm(label) or norm(label) == norm(tool)


def button_label(label: Any, tool: str, args: Optional[dict] = None) -> str:
    """The model's label when it wrote one, else the call said in words — never blank, never
    just the tool's name."""
    text = " ".join(str(label or "").split())
    if text and not _bare(text, tool):
        return text
    return fallback_label(tool, args)
