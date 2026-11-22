"""VALIDATORS: the repo's `.boss/validators.yaml` and the typed record ``boss.validation/v1``.

SPEC docs/design/validation-review/SPEC.md §1.2, §1.3, §2. The producer stays
``input_fidelity.run_validation`` (one validation path); this module holds what it needs:

- ``load`` / ``for_path``: the validator a project declares for a file (id, applies_to globs,
  run / fix command templates with ``{file}``, output adapter, rules_doc, ruleset);
- ``severity``: a finding's P-level — from the ruleset when one names the rule, else from the
  tool's own severity word (``basis`` says which);
- ``mask``: secrets out of every stored line (``original``, ``fixed``, fix before/after);
- ``hunks`` / ``attribute``: the diff input → target as hunks h-01…, each line tied to the
  finding it addresses;
- ``substituted``: the input-substitution guard (§1.3);
- ``record``: the ``boss.validation/v1`` record itself.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

SCHEMA = "boss.validation/v1"
FINDING_SCHEMA = "boss.finding/v1"
FILE = ".boss/validators.yaml"
PLEVELS = ("P0", "P1", "P2", "P3")
_NATIVE = {"critical": "P1", "error": "P1", "high": "P1", "2": "P1",
           "warn": "P2", "warning": "P2", "medium": "P2", "moderate": "P2", "1": "P2",
           "note": "P3", "info": "P3", "low": "P3", "style": "P3"}


class ValidatorsError(ValueError):
    pass


# ── .boss/validators.yaml ──────────────────────────────────────────────────────────────────────

def parse(text: str) -> list[dict]:
    """The declared validators, checked: each needs ``id``, ``applies_to`` and ``run``."""
    import yaml

    from .validator_adapters import REGISTRY

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValidatorsError(f"{FILE} does not parse: {str(exc)[:200]}") from exc
    items = data.get("validators") if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        raise ValidatorsError(f"{FILE} needs a `validators:` list")
    out: list[dict] = []
    for i, v in enumerate(items):
        if not isinstance(v, Mapping) or not v.get("id") or not v.get("run"):
            raise ValidatorsError(f"{FILE} validators[{i}] needs `id` and `run`")
        globs = v.get("applies_to") or []
        globs = [globs] if isinstance(globs, str) else [str(g) for g in globs]
        if not globs:
            raise ValidatorsError(f"{FILE} validator {v['id']!r} needs `applies_to`")
        for key in ("run", "fix"):
            if v.get(key) and "{file}" not in str(v[key]):
                raise ValidatorsError(f"{FILE} validator {v['id']!r}: `{key}` needs {{file}}")
        output = v.get("output") if isinstance(v.get("output"), Mapping) else {}
        adapter = output.get("adapter")
        if adapter and adapter not in REGISTRY:
            raise ValidatorsError(f"{FILE} validator {v['id']!r}: unknown adapter {adapter!r}")
        out.append({"id": str(v["id"]), "applies_to": globs, "run": str(v["run"]),
                    "fix": str(v["fix"]) if v.get("fix") else None,
                    "adapter": str(adapter) if adapter else None,
                    "rules_doc": str(v["rules_doc"]) if v.get("rules_doc") else None,
                    "ruleset": str(v["ruleset"]) if v.get("ruleset") else None})
    return out


def load(root: Any) -> list[dict]:
    """The repo's validators; [] when it declares none."""
    path = Path(root) / FILE
    try:
        text = path.read_text()
    except OSError:
        return []
    return parse(text)


def for_path(root: Any, *paths: str) -> Optional[dict]:
    """The first declared validator whose ``applies_to`` matches one of ``paths``, as the
    contract's validator: ``{name, check, fix, adapter, rules_doc, ruleset, source}``."""
    for v in load(root):
        if any(fnmatch.fnmatch(p, g) for p in paths if p for g in v["applies_to"]):
            return {"name": v["id"], "check": v["run"], "fix": v["fix"], "adapter": v["adapter"],
                    "rules_doc": v["rules_doc"], "ruleset": v["ruleset"], "source": FILE}
    return None


# ── severity and masking ───────────────────────────────────────────────────────────────────────

def severity(rule_id: str, native: Any, ruleset: Optional[Mapping[str, Any]] = None) -> tuple[str, str]:
    """(P-level, basis). The ruleset decides when it names the rule (§1.1); else the tool's own
    severity word, said as ``native:<word>``."""
    rules = (ruleset or {}).get("rules") or {}
    rule = rules.get(rule_id) if isinstance(rules, Mapping) else None
    if isinstance(rule, Mapping) and rule.get("severity") in PLEVELS:
        return str(rule["severity"]), f"ruleset {ruleset.get('ruleset')} v{ruleset.get('version')}"
    word = str(native or "").strip().lower()
    return _NATIVE.get(word, "P2"), f"native:{word or 'none'}"


def mask(text: Optional[str]) -> Optional[str]:
    """A stored line with every credential replaced (services/redaction)."""
    if text is None:
        return None
    from .redaction import redact_secrets

    return redact_secrets(str(text))


def sha256(text: Optional[str]) -> Optional[str]:
    return hashlib.sha256(str(text).encode()).hexdigest() if text is not None else None


# ── the diff, as hunks tied to findings ────────────────────────────────────────────────────────

def hunks(input_text: str, output_text: str) -> list[dict]:
    """Input → target as hunks: ``{id: h-NN, i1, i2, j1, j2, before: [...], after: [...]}``
    (0-based half-open line spans, difflib opcodes)."""
    a, b = str(input_text or "").splitlines(), str(output_text or "").splitlines()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag != "equal":
            out.append({"id": f"h-{len(out) + 1:02d}", "i1": i1, "i2": i2, "j1": j1, "j2": j2,
                        "before": a[i1:i2], "after": b[j1:j2]})
    return out


def line_map(input_text: str, output_text: str) -> dict[int, int]:
    """Unchanged input line (1-based) → its line in the output."""
    a, b = str(input_text or "").splitlines(), str(output_text or "").splitlines()
    out: dict[int, int] = {}
    for blk in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            out[blk.a + k + 1] = blk.b + k + 1
    return out


def _words(text: Any) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", str(text or "").lower()) if w]


def _asks_for(line: str, finding: Mapping[str, Any]) -> bool:
    """The line's key is named by the finding's fix hint (`max_body_bytes` ← "Add
    max_body_bytes"; `url` ← "Change the upstream url")."""
    m = re.match(r"\s*-?\s*([\w-]+)\s*:", line or "")
    if not m:
        return False
    toks = _words(m.group(1))[:2]
    words = _words(f"{finding.get('fix') or ''} {finding.get('rule_id') or ''}")
    return bool(toks) and all(any(w.startswith(t) or (len(w) >= 3 and t.startswith(w))
                                  for w in words) for t in toks)


def attribute(hunk: Mapping[str, Any], line: str, before: Sequence[Mapping[str, Any]],
              prev: Optional[str]) -> Optional[str]:
    """The finding (id) one changed line of a hunk addresses: among the findings on the lines
    the hunk replaces (or the line it is inserted after), the one whose fix hint names the
    line's key; else the nearest finding above the hunk that names it; else the line before it
    in the same hunk; else the hunk's only candidate."""
    i1, i2 = int(hunk["i1"]), int(hunk["i2"])
    span = set(range(i1 + 1, i2 + 1)) if i2 > i1 else {i1}
    near = [f for f in before if f.get("line") in span]
    for f in near:
        if _asks_for(line, f):
            return str(f["id"])
    above = sorted((f for f in before if (f.get("line") or 0) <= max(span) and _asks_for(line, f)),
                   key=lambda f: -(f.get("line") or 0))
    if above:
        return str(above[0]["id"])
    if prev:
        return prev
    return str(near[0]["id"]) if len(near) == 1 else None


# ── the guard ──────────────────────────────────────────────────────────────────────────────────

def substituted(root: Any, c: Mapping[str, Any], out_text: Optional[str],
                candidates: Iterable[str]) -> Optional[dict]:
    """INPUT SUBSTITUTED (§1.3): the input in the tree is no longer the bytes the run received,
    or the target is byte-for-byte another repo file (an answer such as `*.good.yaml`, any
    other data file) or the target as it stood before the build. None when honest."""
    root = Path(root)
    inp, outp = str(c["input"]), str(c["output"])
    received = c.get("_input_text")
    try:
        now = (root / inp).read_text(errors="replace")
    except OSError:
        now = None
    if received is not None and now is not None and now != received:
        return {"code": "input_substituted", "source": inp, "why": "input_changed"}
    if out_text is None or outp == inp:
        return None
    if c.get("_output_base") is not None and out_text == c["_output_base"]:
        return {"code": "input_substituted", "source": outp, "why": "target_unchanged"}
    texts = dict(c.get("_answer_texts") or {})
    for path in candidates:
        if path in (inp, outp) or path in texts:
            continue
        try:
            texts[path] = (root / path).read_text(errors="replace")
        except OSError:
            continue
    for path, text in texts.items():
        if path not in (inp, outp) and text == out_text:
            return {"code": "input_substituted", "source": path, "why": "copy_of_repo_file"}
    return None


# ── the record ─────────────────────────────────────────────────────────────────────────────────

def _evidence(eid: str, cmd: Optional[str], exit_code: Any) -> dict:
    return {"id": eid, "kind": "command", "cmd": cmd, "exit": exit_code}


def record(*, c: Mapping[str, Any], validator: Mapping[str, Any], version: Optional[str],
           before_raw: Optional[Sequence[Mapping[str, Any]]],
           after_raw: Optional[Sequence[Mapping[str, Any]]],
           commands: Mapping[str, Optional[str]], exits: Mapping[str, Any],
           out_text: Optional[str], auto_text: Optional[str],
           ruleset: Optional[Mapping[str, Any]] = None, guard: Optional[dict] = None,
           error: Optional[str] = None) -> dict:
    """``boss.validation/v1`` from one validation: the tool's findings on the partner's input
    (V-01…), the fixes as hunks of input → target, its findings on the target. A finding is
    ``fixed`` only with an after-run that no longer reports it AND a hunk that addresses it."""
    inp, target = str(c["input"]), str(c["output"])
    input_text = str(c.get("_input_text") or "")
    in_lines = input_text.splitlines()
    rules_doc = validator.get("rules_doc")
    evidence = []
    if commands.get("before"):
        evidence.append(_evidence("E-1", commands["before"], exits.get("before")))
    if commands.get("fix"):
        evidence.append(_evidence("E-2", commands["fix"], exits.get("fix")))
    after_ran = commands.get("after") is not None and after_raw is not None
    if commands.get("after"):
        evidence.append(_evidence("E-3", commands["after"], exits.get("after")))

    ev = {e["id"]: e for e in evidence}
    findings: list[dict] = []
    for n, f in enumerate(before_raw or [], start=1):
        rule = str(f.get("rule_id") or "")
        level, basis = severity(rule, f.get("native_severity"), ruleset)
        line = f.get("line")
        findings.append({
            "id": f"V-{n:02d}",
            "source": {"kind": "validator", "name": validator.get("name"), "rule_id": rule,
                       "rule_doc": f"{rules_doc}#{rule}" if rules_doc else None},
            "severity": level, "basis": basis,
            "native_severity": f.get("native_severity") or None,
            "title": mask(f.get("title")) or None,
            "location": {"file": target, "line": line, "partner_line": line},
            "original": mask(in_lines[line - 1]) if isinstance(line, int)
            and 0 < line <= len(in_lines) else None,
            "fixed": None, "hunk_id": None, "fix_kind": "none", "status": "open",
            "evidence": [dict(ev["E-1"])] if "E-1" in ev else [], "override": None,
            "_fix": f.get("fix"), "fixable": f.get("fixable")})

    # Fixes: every changed line of input → target, tied to a hunk and a finding.
    # AUTO: the same line the fixer wrote at the same place of the input (its own hunks), so a
    # line the input already has elsewhere (`rate_limit:` on another route) still counts.
    auto_added: set = set()
    auto_removed: set = set()
    for h in hunks(input_text, auto_text) if auto_text is not None else []:
        auto_added |= {(h["i1"], line) for line in h["after"]}
        auto_removed |= {(h["i1"] + k, line) for k, line in enumerate(h["before"])}
    by_id = {f["id"]: f for f in findings}
    hint = [{"id": f["id"], "line": f["location"]["partner_line"], "fix": f["_fix"],
             "rule_id": f["source"]["rule_id"]} for f in findings]
    fixes: list[dict] = []
    all_hunks = hunks(input_text, out_text) if out_text is not None else []
    for h in all_hunks:
        prev: Optional[str] = None
        olds, news = h["before"], h["after"]
        for k in range(max(len(olds), len(news))):
            old = olds[k] if k < len(olds) else None
            new = news[k] if k < len(news) else None
            fid = attribute(h, old if old is not None else new or "", hint, prev)
            prev = fid or prev
            fixes.append({"finding": fid,
                          "rule": by_id[fid]["source"]["rule_id"] if fid in by_id else None,
                          "file": target, "line": h["i1"] + k + 1 if old is not None else h["i1"],
                          "before": mask(old), "after": mask(new),
                          "kind": "auto" if ((h["i1"], new) in auto_added if new is not None
                                             else (h["i1"] + k, old) in auto_removed) else "agent",
                          "hunk_id": h["id"]})
    for f in findings:
        mine = [x for x in fixes if x["finding"] == f["id"]]
        if mine:
            f["hunk_id"] = mine[0]["hunk_id"]
            f["fixed"] = next((x["after"] for x in mine if x["after"] is not None), None)
            f["fix_kind"] = "auto" if all(x["kind"] == "auto" for x in mine) else "agent"

    # After: still reported (same rule at the line the input line moved to) → open.
    moved = line_map(input_text, out_text) if out_text is not None else {}
    after_ids: list[str] = []
    after_extra: list[dict] = []
    if after_ran:
        remaining = list(after_raw or [])
        for f in findings:
            rule, line = f["source"]["rule_id"], f["location"]["partner_line"]
            want = moved.get(line) if isinstance(line, int) else None
            hit = next((a for a in remaining if a.get("rule_id") == rule
                        and (a.get("line") is None or want is None or a.get("line") == want)),
                       None)
            if hit is not None:
                remaining.remove(hit)
                f["location"]["line"] = hit.get("line")
                after_ids.append(f["id"])
            elif f["hunk_id"]:
                f["status"] = "fixed"
                f["location"]["line"] = None if f["fixed"] is None else want
                f["evidence"] = [*f["evidence"], {**ev["E-3"], "absent": True}]
        for n, a in enumerate(remaining, start=len(findings) + 1):   # new on the target
            rule = str(a.get("rule_id") or "")
            level, basis = severity(rule, a.get("native_severity"), ruleset)
            line = a.get("line")
            out_lines = str(out_text or "").splitlines()
            after_extra.append({
                "id": f"V-{n:02d}",
                "source": {"kind": "validator", "name": validator.get("name"), "rule_id": rule,
                           "rule_doc": f"{rules_doc}#{rule}" if rules_doc else None},
                "severity": level, "basis": basis,
                "native_severity": a.get("native_severity") or None,
                "title": mask(a.get("title")) or None,
                "location": {"file": target, "line": line, "partner_line": None},
                "original": None,
                "fixed": mask(out_lines[line - 1]) if isinstance(line, int)
                and 0 < line <= len(out_lines) else None,
                "hunk_id": None, "fix_kind": "none", "status": "open",
                "evidence": [dict(ev["E-3"])], "override": None, "introduced": True})
            after_ids.append(f"V-{n:02d}")
    for f in findings:
        f.pop("_fix", None)
        f.pop("fixable", None)
    findings += after_extra
    return {
        "schema": SCHEMA,
        "validator": {"id": validator.get("name"), "version": version,
                      "adapter": validator.get("adapter") or "json"},
        "ruleset": ({"id": ruleset.get("ruleset"), "version": ruleset.get("version"),
                     "sha": ruleset.get("sha")} if ruleset else None),
        "input": {"file": inp, "sha256": sha256(c.get("_input_text")),
                  "from": str(c.get("from") or "partner")},
        "target": target,
        "before": {"command": commands.get("before"), "exit": exits.get("before"),
                   "findings": [f["id"] for f in findings if not f.get("introduced")]},
        "fix": ({"command": commands["fix"], "exit": exits.get("fix")}
                if commands.get("fix") else None),
        "fixes": fixes,
        "after": {"command": commands.get("after"), "exit": exits.get("after"),
                  "findings": after_ids} if after_ran else None,
        "findings": findings,
        "hunks": [{"id": h["id"], "line": h["i1"] + 1, "removed": len(h["before"]),
                   "added": len(h["after"])} for h in all_hunks],
        "evidence": evidence,
        "guard": guard,
        "error": error or (guard or {}).get("code"),
    }
