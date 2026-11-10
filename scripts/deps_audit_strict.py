"""Runtime pins and fail-on-high scoring for the dependency audit gate.

Reads engine/requirements.txt, apps/api/requirements.txt, and
apps/worker/requirements.txt. Drops the dev-only names pytest, playwright, and
ruff. High or critical findings fail. Moderate, low, unknown, and missing
severity do not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

_REQUIREMENT_FILES = (
    Path("engine/requirements.txt"),
    Path("apps/api/requirements.txt"),
    Path("apps/worker/requirements.txt"),
)
_DEV_ONLY = frozenset({"pytest", "playwright", "ruff"})
_TWO_CHAR_OPERATORS = ("==", ">=", "<=", "~=", "!=")
_ONE_CHAR_CUTS = frozenset("[;><")
_BLOCKING_SEVERITIES = frozenset({"high", "critical"})


class RuntimePin(NamedTuple):
    name: str


def _package_name(line: str) -> str | None:
    text = line.strip()
    if not text or text[0] == "#":
        return None
    cut = len(text)
    index = 0
    while index < cut:
        if text.startswith(_TWO_CHAR_OPERATORS, index):
            cut = index
            break
        if text[index] in _ONE_CHAR_CUTS:
            cut = index
            break
        index += 1
    name = text[:cut].strip().lower()
    if not name or name in _DEV_ONLY:
        return None
    return name


def load_runtime_pins(root: Path) -> list[RuntimePin]:
    pins: list[RuntimePin] = []
    for relative in _REQUIREMENT_FILES:
        path = Path(root) / relative
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            name = _package_name(line)
            if name is not None:
                pins.append(RuntimePin(name))
    return pins


def evaluate(root: Path, python: str, json_in: Path) -> tuple[int, list[str]]:
    """Return (1, ids) when any vuln severity is high or critical, else (0, []).

    Missing or unknown severity is not a block. root and python stay in the
    signature for the gate and are not used.
    """
    _ = (root, python)
    document = json.loads(Path(json_in).read_text(encoding="utf-8"))
    blocking: list[str] = []
    for dependency in document.get("dependencies") or []:
        if not isinstance(dependency, dict):
            continue
        for vuln in dependency.get("vulns") or []:
            if not isinstance(vuln, dict):
                continue
            severity = vuln.get("severity")
            if isinstance(severity, str) and severity.casefold() in _BLOCKING_SEVERITIES:
                vuln_id = vuln.get("id")
                blocking.append(vuln_id if isinstance(vuln_id, str) else "")
    if blocking:
        return (1, blocking)
    return (0, [])
