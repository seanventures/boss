"""RULESETS: `.boss/rulesets/<name>.yaml`, a BOSS layer over a validator's own rulebook (SPEC §4).

The repo file is the one source of truth (its history is the version history); BOSS reads it by
its sha. Per rule: ``severity`` (P0–P3, the packet's P-level), ``enabled`` (false → the finding is
``suppressed``, still counted), ``autofix`` (``mechanical | agent | human | never``) and ``guide``
— the text the fixing agent receives verbatim. ``custom`` rules are agent-checked, never executed.
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping, Optional

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
        from .validators import yaml_where

        raise RulesetError(f"{path} does not parse{yaml_where(exc)}") from None
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


def load(root: Any, name: Optional[str], read: Optional[Callable[[str], Optional[str]]] = None
         ) -> Optional[dict]:
    """The ruleset ``name``: ``.boss/rulesets/<name>.yaml`` (or ``.yml``). ``read(path)`` reads
    from a TRUSTED ref (the base branch) instead of the working tree; a symlink that leaves the
    repository is not read. None when there is none."""
    from .validators import read_in_repo

    if not name or "/" in str(name) or ".." in str(name):
        return None
    for ext in (".yaml", ".yml"):
        rel = f"{DIR}/{name}{ext}"
        text = read(rel) if read is not None else read_in_repo(root, rel)
        if text:
            return parse(text, path=rel)
    return None


def unapplied(root: Any, name: Optional[str], trusted: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """A ruleset the CHANGE BRANCH edits (its sha differs from the trusted one): a pending
    ruleset change, shown, never applied to this branch's own packet — so a build cannot
    downgrade or disable the P0 it is judged by."""
    try:
        mine = load(root, name)
    except RulesetError:
        return {"ruleset": name, "sha": None, "applied": False}
    if mine is None or (trusted or {}).get("sha") == mine.get("sha"):
        return None
    return {"ruleset": name, "sha": mine.get("sha"), "version": mine.get("version"),
            "applied": False}


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
