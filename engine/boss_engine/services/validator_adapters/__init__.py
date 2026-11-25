"""VALIDATOR ADAPTERS: a tool's own report → the raw fields of ``boss.finding/v1`` (SPEC §2).

One small function per output format, named in ``.boss/validators.yaml`` as ``output.adapter``.
Each takes the tool's stdout and the file it checked and returns ``[{rule_id, native_severity,
file, line, title, why, fix, fixable}]`` — or None when the text is not that tool's report (then
the validation says so; an unreadable report is never "0 findings"). Severity is NOT decided
here: the ruleset maps ``rule_id`` to a P-level (``validators.severity``).

ESLint, ruff, SARIF or a custom script needs one adapter here and no UI change.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

Raw = dict[str, Any]
Adapter = Callable[[str, str], Optional[list[Raw]]]


def load_json(text: str) -> Any:
    """The JSON a tool printed, tolerating a log line before it. None when there is none."""
    text = str(text or "").strip()
    for start in (0, text.find("{"), text.find("[")):
        if start < 0:
            continue
        try:
            return json.loads(text[start:])
        except ValueError:
            continue
    return None


def raw(rule_id: Any, native: Any, file: Any, line: Any, title: Any = "", *, why: Any = None,
        fix: Any = None, fixable: Any = None) -> Raw:
    return {"rule_id": str(rule_id or ""), "native_severity": str(native or ""),
            "file": str(file or ""), "line": line if isinstance(line, int) and line > 0 else None,
            "title": str(title or "")[:300], "why": str(why)[:600] if why else None,
            "fix": str(fix)[:600] if fix else None, "fixable": fixable}


from . import eslint_json, gwlint_json, ruff_json, sarif  # noqa: E402  (they use raw/load_json)

REGISTRY: dict[str, Adapter] = {
    "gwlint-json": gwlint_json.parse,
    "sarif": sarif.parse,
    "eslint-json": eslint_json.parse,
    "ruff-json": ruff_json.parse,
}


def names() -> list[str]:
    return sorted(REGISTRY)


def parse(adapter: Optional[str], stdout: str, file: str) -> Optional[list[Raw]]:
    """The adapter's findings, or — no adapter named — the generic JSON reader
    (input_fidelity.parse_findings) mapped to the same raw fields."""
    fn = REGISTRY.get(str(adapter or ""))
    if fn is not None:
        return fn(stdout, file)
    if adapter:
        raise KeyError(f"unknown validator adapter {adapter!r}; known: {', '.join(names())}")
    from ..input_fidelity import parse_findings

    found = parse_findings(stdout, file)
    if found is None:
        return None
    return [raw(f.get("rule"), f.get("severity"), f.get("file") or file, f.get("line"),
                f.get("message"), why=f.get("why"), fix=f.get("fix"), fixable=f.get("fixable"))
            for f in found]
