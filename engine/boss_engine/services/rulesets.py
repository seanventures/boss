"""RULESETS: `.boss/rulesets/<name>.yaml`, a BOSS layer over a validator's own rulebook (SPEC §4).

The repo file is the one source of truth (its history is the version history); BOSS reads it by
its sha. Per rule: ``severity`` (P0–P3, the packet's P-level), ``enabled`` (false → the finding is
``suppressed``, still counted), ``autofix`` (``mechanical | agent | human | never``) and ``guide``
— the text the fixing agent receives verbatim. ``custom`` rules are agent-checked, never executed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Optional

DIR = ".boss/rulesets"
AUTOFIX = ("mechanical", "agent", "human", "never")
PLEVELS = ("P0", "P1", "P2", "P3")


class RulesetError(ValueError):
    pass


def parse(text: str, *, path: str = DIR) -> dict:
    import yaml

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RulesetError(f"{path} does not parse: {str(exc)[:200]}") from exc
    if not isinstance(data, Mapping) or not data.get("ruleset"):
        raise RulesetError(f"{path} needs `ruleset:`")
    rules: dict[str, dict] = {}
    for rid, r in (data.get("rules") or {}).items():
        r = dict(r or {})
        if r.get("severity") is not None and r["severity"] not in PLEVELS:
            raise RulesetError(f"{path} rule {rid}: severity must be one of {', '.join(PLEVELS)}")
        if r.get("autofix") is not None and r["autofix"] not in AUTOFIX:
            raise RulesetError(f"{path} rule {rid}: autofix must be one of {', '.join(AUTOFIX)}")
        rules[str(rid)] = {"severity": r.get("severity"), "autofix": r.get("autofix"),
                           "enabled": r.get("enabled", True) is not False,
                           "guide": str(r["guide"]) if r.get("guide") else None}
    custom = []
    for c in data.get("custom") or []:
        if not isinstance(c, Mapping) or not c.get("id") or c.get("severity") not in PLEVELS:
            raise RulesetError(f"{path} custom rules need `id` and a P-level `severity`")
        custom.append({"id": str(c["id"]), "severity": c["severity"], "check": "agent",
                       "guide": str(c["guide"]) if c.get("guide") else None})
    return {"ruleset": str(data["ruleset"]), "version": data.get("version"),
            "validator": data.get("validator"), "rules": rules, "custom": custom,
            "sha": hashlib.sha256(text.encode()).hexdigest()[:12]}


def load(root: Any, name: Optional[str]) -> Optional[dict]:
    """The ruleset named ``name`` in the repo (by its ``ruleset:`` key, or its file name);
    None when there is none."""
    if not name:
        return None
    d = Path(root) / DIR
    if not d.is_dir():
        return None
    for p in sorted([*d.glob("*.yaml"), *d.glob("*.yml")]):
        try:
            rs = parse(p.read_text(), path=f"{DIR}/{p.name}")
        except (OSError, RulesetError):
            if p.stem == name:
                raise
            continue
        if rs["ruleset"] == name or p.stem == name:
            return rs
    return None


def guides(rs: Optional[Mapping[str, Any]]) -> list[dict]:
    """The guides the fixing agent receives: ``[{rule, severity, autofix, guide, custom}]``."""
    if not rs:
        return []
    out = [{"rule": rid, "severity": r.get("severity"), "autofix": r.get("autofix"),
            "guide": r["guide"], "custom": False}
           for rid, r in (rs.get("rules") or {}).items() if r.get("guide") and r.get("enabled")]
    out += [{"rule": c["id"], "severity": c["severity"], "autofix": "agent", "guide": c["guide"],
             "custom": True} for c in rs.get("custom") or [] if c.get("guide")]
    return out


def brief(rs: Optional[Mapping[str, Any]]) -> str:
    """The guides, word for word, for the fixing agent's prompt."""
    items = guides(rs)
    if not items:
        return ""
    lines = [f"RULESET {rs.get('ruleset')} v{rs.get('version')} — follow each rule's guide when "
             "you fix its findings. A rule with autofix `human` or `never` is not yours to fix: "
             "leave it for a person."]
    for g in items:
        tag = "agent-checked, not executed" if g["custom"] else f"autofix {g['autofix'] or '-'}"
        lines.append(f"- {g['rule']} [{g['severity'] or '-'}, {tag}]: {g['guide']}")
    return "\n\n" + "\n".join(lines)
