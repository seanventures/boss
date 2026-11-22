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
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

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
        raise ValidatorsError(f"{FILE} does not parse{yaml_where(exc)}") from None
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


def yaml_where(exc: Exception) -> str:
    """Where a YAML file failed — line and column only, never its content (a config file may
    hold a secret, and the error is shown and stored)."""
    mark = getattr(exc, "problem_mark", None)
    return f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""


def read_in_repo(root: Any, rel: str) -> Optional[str]:
    """A repo file's text; None when it is missing or a symlink that leaves the repository."""
    base = Path(root).resolve()
    path = Path(root) / rel
    try:
        real = path.resolve()
        if real != base and base not in real.parents:
            return None
        return real.read_text()
    except OSError:
        return None


Reader = Callable[[str], Optional[str]]


def load(root: Any, read: Optional[Reader] = None) -> list[dict]:
    """The repo's validators; [] when it declares none. ``read(path)`` reads from a TRUSTED ref
    (the base branch) instead of the working tree."""
    text = read(FILE) if read is not None else read_in_repo(root, FILE)
    return parse(text) if text else []


def for_path(root: Any, *paths: str, read: Optional[Reader] = None) -> Optional[dict]:
    """The first declared validator whose ``applies_to`` matches one of ``paths``, as the
    contract's validator: ``{name, check, fix, adapter, rules_doc, ruleset, source}``."""
    for v in load(root, read):
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
              prev: Optional[str], *, removed: bool = False) -> Optional[str]:
    """The finding (id) one changed line of a hunk addresses: among the findings on the lines
    the hunk replaces (or the line it is inserted after), the one whose fix hint names the
    line's key; else the nearest finding above the hunk that names it; else the line before it
    in the same hunk; else the hunk's only candidate. A REMOVED partner line belongs with the
    line removed before it (a route's keys go with its `- path:`) before any finding above."""
    i1, i2 = int(hunk["i1"]), int(hunk["i2"])
    span = set(range(i1 + 1, i2 + 1)) if i2 > i1 else {i1}
    near = [f for f in before if f.get("line") in span]
    for f in near:
        if _asks_for(line, f):
            return str(f["id"])
    if removed and prev:
        return prev
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


# ── provenance: which changed line the fixer wrote, which the agent ────────────────────────────

def _pairs(h: Mapping[str, Any]) -> list[tuple[int, Optional[str], Optional[str]]]:
    """One hunk's changed lines, paired by position: ``(k, old, new)``."""
    olds, news = h["before"], h["after"]
    return [(k, olds[k] if k < len(olds) else None, news[k] if k < len(news) else None)
            for k in range(max(len(olds), len(news)))]


def changed_lines(input_text: str, out_text: Optional[str], auto_text: Optional[str],
                  hint: Sequence[Mapping[str, Any]]) -> tuple[list[dict], list[dict]]:
    """``(hunks of input → target, changed lines)``. Each line: ``{hunk_id, i, j, old, new,
    finding, kind}`` (``i`` / ``j`` 0-based input / target indexes, None for an added / removed
    line; texts UNMASKED — callers mask before storing).

    ``kind`` is PROVENANCE, not a guess: a target line is ``auto`` when it is a line the
    mechanical fixer wrote (``auto_text`` is the fixer's output on the partner's file) that
    survived into the target; a removed input line is ``auto`` when the fixer removed it too.
    Every other change is the agent's. An auto line's finding is read from the fixer's own diff
    (its clean context); an agent line's from the target's diff (``attribute``)."""
    if out_text is None:
        return [], []
    all_hunks = hunks(input_text, out_text)
    written: dict[int, Optional[str]] = {}      # fixer's output line → finding
    gone: dict[int, Optional[str]] = {}         # input line the fixer removed → finding
    from_auto: dict[int, int] = {}              # target line → fixer's output line
    if auto_text is not None:
        for h in hunks(input_text, auto_text):
            prev: Optional[str] = None
            for k, old, new in _pairs(h):
                fid = attribute(h, old if old is not None else new or "", hint, prev)
                prev = fid or prev
                if new is not None:
                    written[int(h["j1"]) + k] = fid
                if old is not None:
                    gone[int(h["i1"]) + k] = fid
        A, b = str(auto_text).splitlines(), str(out_text).splitlines()
        for blk in difflib.SequenceMatcher(a=A, b=b, autojunk=False).get_matching_blocks():
            for k in range(blk.size):
                from_auto[blk.b + k] = blk.a + k
    lines: list[dict] = []
    for h in all_hunks:
        prev = None
        for k, old, new in _pairs(h):
            i = int(h["i1"]) + k if old is not None else None
            j = int(h["j1"]) + k if new is not None else None
            if new is not None:
                src = from_auto.get(j) if j is not None else None
                auto = src is not None and src in written
                fid = written[src] if auto else None
            else:
                auto = i in gone
                fid = gone.get(i) if auto else None
            if not auto:
                fid = attribute(h, old if old is not None else new or "", hint, prev,
                                removed=old is not None and new is None)
            prev = fid or prev
            lines.append({"hunk_id": h["id"], "i": i, "j": j, "old": old, "new": new,
                          "anchor": int(h["i1"]), "finding": fid,
                          "kind": "auto" if auto else "agent"})
    return all_hunks, lines


# ── security-sensitive changes (an unexplained one blocks as P0) ───────────────────────────────

#: Key-name tokens whose values decide who may call the route and where it goes.
SENSITIVE_TOKENS = frozenset({
    "auth", "authn", "authz", "authentication", "authorization", "oauth", "oidc", "jwks", "jwt",
    "issuer", "audience", "upstream", "upstreams", "tls", "mtls", "ssl", "cert", "certs",
    "certificate", "certificates"})
_KEY = re.compile(r"\s*(?:-\s+)?([\w.\-]+)\s*:")
_URL_HOST = re.compile(r"\b[a-z][a-z0-9+.\-]*://(?:[^@/\s\"']*@)?([^/:\s\"'?#]+)", re.I)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def key_path(lines: Sequence[str], idx: int) -> list[str]:
    """The YAML key path of line ``idx`` (0-based), read by indentation: its ancestors' keys,
    then its own (``routes › path › auth › jwks_url``; ``upstreams › core › url``)."""
    if not 0 <= idx < len(lines):
        return []
    line = lines[idx]
    m = _KEY.match(line)
    path = [m.group(1)] if m else []
    cur = _indent(line)
    for j in range(idx - 1, -1, -1):
        up = lines[j]
        if not up.strip() or up.lstrip().startswith("#"):
            continue
        ind = _indent(up)
        if ind < cur:
            m = _KEY.match(up)
            if m:
                path.append(m.group(1))
            cur = ind
            if cur == 0:
                break
    return list(reversed(path))


def sensitive(path: Sequence[str]) -> bool:
    """A key path that decides who may call or where a request goes. Matched INSIDE each key
    (separators and case ignored), so `jwksUrl`, `JWKS-URI`, `upstreamURL`, `oauth2` count."""
    return any(t in re.sub(r"[^a-z0-9]+", "", k.lower()) for k in path for t in SENSITIVE_TOKENS)


def hosts(text: Optional[str]) -> set[str]:
    return {m.group(1).lower() for m in _URL_HOST.finditer(str(text or ""))}


def security_changes(input_text: str, out_text: Optional[str], lines: Sequence[Mapping[str, Any]]
                     ) -> list[dict]:
    """The changed lines on security-sensitive keys (auth, jwks, issuer, upstream, tls…) that
    nothing accounts for: ``[{line…, keys, why}]``, ``why`` one of ``unexplained`` (no finding
    asks for the line), ``host_changed`` (a fix that moves a URL to another host: a scheme fix
    keeps the host) or ``new_host`` (a host the partner's file never names)."""
    a, b = str(input_text or "").splitlines(), str(out_text or "").splitlines()
    known = hosts(input_text)
    out = []
    for ln in lines:
        path = key_path(b, ln["j"]) if ln["new"] is not None else key_path(a, ln["i"])
        old_h, new_h = hosts(ln["old"]), hosts(ln["new"])
        # A URL MOVED TO ANOTHER HOST is guarded under ANY key (`backend: {url: …}`, a renamed
        # key): a fix changes a scheme, never where a request goes.
        moved = bool(old_h and new_h and old_h != new_h)
        on_sensitive = sensitive(path)
        if not moved and not on_sensitive:
            continue
        why = []
        # The mechanical fixer's own lines are its rule's (it writes nothing else); only the
        # agent's can be unexplained.
        if on_sensitive and ln["finding"] is None and ln["kind"] != "auto" \
                and (ln["old"] or "").strip() != (ln["new"] or "").strip():
            why.append("unexplained")
        if moved:
            why.append("host_changed")
        elif on_sensitive and new_h - known:
            why.append("new_host")
        if why:
            out.append({**ln, "keys": path, "why": why})
    return out


# ── a finding's block: what "its lines" are ────────────────────────────────────────────────────

def block(lines: Sequence[str], idx: int) -> tuple[int, int]:
    """The 0-based half-open span of the YAML block line ``idx`` opens: the line and every
    following line indented deeper (a route item `- path:` and its keys). A top-level key with
    nothing under it (`tenant: x`, where a tool reports file-level findings) is the whole file."""
    if not 0 <= idx < len(lines):
        return (0, len(lines))
    base = _indent(lines[idx])
    end = idx + 1
    while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > base
                                or lines[end].lstrip().startswith("#")):
        end += 1
    if end == idx + 1 and base == 0:
        return (0, len(lines))
    return (idx, end)


def _out_span(h: Mapping[str, Any]) -> range:
    """A hunk's target lines (1-based), the line before a pure removal included."""
    j1, j2 = int(h["j1"]), int(h["j2"])
    return range(max(j1, 1), max(j2, j1 + 1) + 1) if j2 == j1 else range(j1 + 1, j2 + 1)


# ── the record ─────────────────────────────────────────────────────────────────────────────────

def _evidence(eid: str, cmd: Optional[str], exit_code: Any) -> dict:
    return {"id": eid, "kind": "command", "cmd": cmd, "exit": exit_code}


HUMAN_AUTOFIX = ("human", "never")


def _joined(texts: Iterable[Optional[str]]) -> Optional[str]:
    kept = [mask(t) for t in texts if t is not None]
    return "\n".join(x for x in kept if x is not None) if kept else None


def record(*, c: Mapping[str, Any], validator: Mapping[str, Any], version: Optional[str],
           before_raw: Optional[Sequence[Mapping[str, Any]]],
           after_raw: Optional[Sequence[Mapping[str, Any]]],
           commands: Mapping[str, Optional[str]], exits: Mapping[str, Any],
           out_text: Optional[str], auto_text: Optional[str],
           ruleset: Optional[Mapping[str, Any]] = None, guard: Optional[dict] = None,
           error: Optional[str] = None) -> dict:
    """``boss.validation/v1`` from one validation: the tool's findings on the partner's input
    (V-01…), the fixes as hunks of input → target, its findings on the target.

    Truth rules (each has a test):
    - a finding the after-run no longer reports is ``fixed``, tied to the hunk on its own line,
      else the hunk that touches its block, else ``removed_with_block`` when its block is gone —
      never left open;
    - ``fixes[]`` is ONE entry per finding (its lines grouped, by hunk); changes no finding
      accounts for are entries with ``finding: null, unexplained: true``, one per hunk;
    - ``kind`` is provenance (``changed_lines``): the mechanical fixer's lines are ``auto``;
    - a rule whose ruleset ``autofix`` is ``human`` or ``never`` is never agent-fixed: the
      finding stays open as ``human`` ("Needs you"), ``held`` until a person accepts it;
    - a changed line on an auth / jwks / issuer / upstream / tls key that nothing explains, or
      that moves a URL to another host, is a P0 ``guard`` finding U-n ("unexplained security
      change") until a person accepts it, and a P0 whose fix shares its hunk (or introduced a
      new finding) is not fixed until then."""
    inp, target = str(c["input"]), str(c["output"])
    input_text = str(c.get("_input_text") or "")
    in_lines = input_text.splitlines()
    out_lines = str(out_text or "").splitlines()
    rules_doc = validator.get("rules_doc")
    rules = (ruleset or {}).get("rules") or {}
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
    by_id = {f["id"]: f for f in findings}
    hint = [{"id": f["id"], "line": f["location"]["partner_line"], "fix": f["_fix"],
             "rule_id": f["source"]["rule_id"]} for f in findings]
    all_hunks, lines = changed_lines(input_text, out_text, auto_text, hint)
    hunk_by_id = {h["id"]: h for h in all_hunks}

    # FIXES: one entry per finding, its lines grouped by hunk; the rest, per hunk, unexplained.
    def _entry(mine: list[dict], fid: Optional[str]) -> dict:
        groups: dict[str, list[dict]] = {}
        for ln in mine:
            groups.setdefault(ln["hunk_id"], []).append(ln)
        first = mine[0]
        return {"finding": fid, "rule": by_id[fid]["source"]["rule_id"] if fid in by_id else None,
                "file": target,
                "line": first["i"] + 1 if first["old"] is not None else first["anchor"],
                "before": _joined(x["old"] for x in mine),
                "after": _joined(x["new"] for x in mine),
                "kind": "auto" if all(x["kind"] == "auto" for x in mine) else "agent",
                "hunk_id": first["hunk_id"],
                "hunks": [{"id": hid, "line": int(hunk_by_id[hid]["i1"]) + 1,
                           "before": [mask(x["old"]) for x in g if x["old"] is not None],
                           "after": [mask(x["new"]) for x in g if x["new"] is not None],
                           "kind": "auto" if all(x["kind"] == "auto" for x in g) else "agent"}
                          for hid, g in groups.items()],
                "lines": len(mine)}

    fixes: list[dict] = []
    for fid in dict.fromkeys(ln["finding"] for ln in lines if ln["finding"]):
        fixes.append(_entry([ln for ln in lines if ln["finding"] == fid], fid))
    for hid in dict.fromkeys(ln["hunk_id"] for ln in lines if not ln["finding"]):
        fixes.append({**_entry([ln for ln in lines if not ln["finding"] and ln["hunk_id"] == hid],
                               None), "unexplained": True})
    for f in findings:
        mine = [ln for ln in lines if ln["finding"] == f["id"]]
        if mine:
            f["hunk_id"] = mine[0]["hunk_id"]
            f["fixed"] = next((mask(x["new"]) for x in mine if x["new"] is not None), None)
            f["fix_kind"] = "auto" if all(x["kind"] == "auto" for x in mine) else "agent"
            f["fixed_by"] = "line"

    # AFTER: still reported (same rule at the line the input line moved to) → open; absent →
    # fixed, always tied to the change that did it.
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
                # ONE LINE BASIS (dry run 2, gap 11): `line` is the PARTNER's line (SPEC §6),
                # the target's line is `target_line`.
                f["location"]["target_line"] = hit.get("line")
                after_ids.append(f["id"])
                continue
            if not f["hunk_id"]:
                _attribute_by_block(f, all_hunks, lines, in_lines, out_lines, moved)
            if f["hunk_id"]:
                f["status"] = "fixed"
                # A removed line has no target line (target_line None).
                f["location"]["target_line"] = want
                if want is None:
                    f["location"]["line_from"] = "partner"
                f["evidence"] = [*f["evidence"], {**ev["E-3"], "absent": True}]
        for n, a in enumerate(remaining, start=len(findings) + 1):   # new on the target
            rule = str(a.get("rule_id") or "")
            level, basis = severity(rule, a.get("native_severity"), ruleset)
            line = a.get("line")
            after_extra.append({
                "id": f"V-{n:02d}",
                "source": {"kind": "validator", "name": validator.get("name"), "rule_id": rule,
                           "rule_doc": f"{rules_doc}#{rule}" if rules_doc else None},
                "severity": level, "basis": basis,
                "native_severity": a.get("native_severity") or None,
                "title": mask(a.get("title")) or None,
                # New on the target: the partner's file has no such line.
                "location": {"file": target, "line": None, "partner_line": None,
                             "target_line": line},
                "original": None,
                "fixed": mask(out_lines[line - 1]) if isinstance(line, int)
                and 0 < line <= len(out_lines) else None,
                "hunk_id": None, "fix_kind": "none", "status": "open",
                "evidence": [dict(ev["E-3"])], "override": None, "introduced": True})
            after_ids.append(f"V-{n:02d}")
    findings += after_extra

    # THE RULESET'S AUTOFIX POLICY, ENFORCED (§4): a `human` / `never` rule is never agent-fixed.
    # Its finding stays open as "Needs you"; when the agent's lines are on the target the fix is
    # HELD — it becomes `fixed` only when a person accepts it (finding_overrides.apply).
    for f in findings:
        rule = rules.get(f["source"]["rule_id"]) or {}
        if rule.get("autofix") not in HUMAN_AUTOFIX:
            continue
        if f["status"] == "fixed" and f["fix_kind"] != "auto":
            f["status"] = "open"
            f["held"] = {"autofix": rule["autofix"], "by": "ruleset"}
            f["fixed_if_released"] = True
        if f["status"] == "open":
            f["fix_kind"] = "human"

    # A FIX MAY NOT MAKE THE CONFIG LESS SAFE: unexplained / host-moving changes on security
    # keys are P0 guard findings, and a P0 sharing their hunk (or one whose hunk introduced a new
    # finding) is not fixed until a person accepts the change.
    guards: list[dict] = []
    sec = security_changes(input_text, out_text, lines)
    for n, hid in enumerate(dict.fromkeys(x["hunk_id"] for x in sec), start=1):
        mine = [x for x in sec if x["hunk_id"] == hid]
        first = mine[0]
        keys = sorted({".".join(x["keys"][-2:]) for x in mine})
        guards.append({
            "id": f"U-{n:02d}",
            "source": {"kind": "guard", "name": "boss", "rule_id": "unexplained-security-change",
                       "rule_doc": None},
            "severity": "P0", "basis": "boss guard: a change to auth or upstream keys",
            "native_severity": None,
            "title": f"Unexplained security change: {', '.join(keys)}"[:200],
            "location": {"file": target,
                         "line": first["i"] + 1 if first["i"] is not None else None,
                         "partner_line": first["i"] + 1 if first["i"] is not None else None,
                         "target_line": (first["j"] + 1 if first["j"] is not None
                                         else first["anchor"])},
            "original": _joined(x["old"] for x in mine), "fixed": _joined(x["new"] for x in mine),
            "hunk_id": hid,
            "fix_kind": "auto" if all(x["kind"] == "auto" for x in mine) else "agent",
            "status": "open", "evidence": [], "override": None,
            "security": {"keys": keys, "why": sorted({w for x in mine for w in x["why"]}),
                         "findings": sorted({x["finding"] for x in mine if x["finding"]})}})
    guard_by_hunk = {g["hunk_id"]: g["id"] for g in guards}
    for f in findings:
        if f["severity"] != "P0" or f.get("introduced") or not (
                f["status"] == "fixed" or f.get("fixed_if_released")):
            continue
        mine_hunks = {ln["hunk_id"] for ln in lines if ln["finding"] == f["id"]} | {f["hunk_id"]}
        by = [guard_by_hunk[h] for h in sorted(mine_hunks) if h in guard_by_hunk]
        spans = [n for h in mine_hunks if h in hunk_by_id for n in _out_span(hunk_by_id[h])]
        by += [x["id"] for x in after_extra if x["location"].get("target_line") in spans]
        if by:
            f["status"] = "open"
            f["blocked_by"] = sorted(set(by))
            f["fixed_if_released"] = True
    for f in findings:
        if f.get("fixed_if_released") and not any(e.get("absent") for e in f["evidence"]):
            f["evidence"] = [*f["evidence"], {**ev["E-3"], "absent": True}]
    findings += guards

    # A disabled rule's findings are `suppressed` (still counted).
    for f in findings:
        f.pop("_fix", None)
        f.pop("fixable", None)
        if (rules.get(f["source"]["rule_id"]) or {}).get("enabled") is False:
            f["status"] = "suppressed"
        loc = f["location"]
        if "target_line" not in loc:
            p = loc.get("partner_line")
            loc["target_line"] = moved.get(p) if isinstance(p, int) and moved else None
        set_change(f)
    return {
        "schema": SCHEMA,
        # The commands and the ruleset are kept as they were TRUSTED for this check (declared,
        # or read from the base branch): a re-run uses these, never the change branch's.
        "validator": {"id": validator.get("name"), "version": version,
                      "adapter": validator.get("adapter") or "json",
                      "run": validator.get("check"), "fix": validator.get("fix"),
                      "rules_doc": validator.get("rules_doc")},
        "ruleset": ({"id": ruleset.get("ruleset"), "version": ruleset.get("version"),
                     "sha": ruleset.get("sha"), "rules": ruleset.get("rules") or {},
                     "custom": ruleset.get("custom") or []} if ruleset else None),
        "input": {"file": inp, "sha256": sha256(c.get("_input_text")),
                  "from": str(c.get("from") or "partner")},
        "target": target,
        "before": {"command": commands.get("before"), "exit": exits.get("before"),
                   "findings": [f["id"] for f in findings
                                if not f.get("introduced") and f["source"]["kind"] != "guard"]},
        "fix": ({"command": commands["fix"], "exit": exits.get("fix"),
                 # Whether BOSS's mechanical pass seeded the target before any agent, and
                 # when it did not, why (input_fidelity.seed_auto_fix): never silent.
                 "seeded": c.get("_auto_seeded") is not None,
                 **({"seed": dict(c["auto_fix_seed"])}
                    if isinstance(c.get("auto_fix_seed"), Mapping) else {})}
                if commands.get("fix") else None),
        "fixes": fixes,
        "after": {"command": commands.get("after"), "exit": exits.get("after"),
                  "findings": after_ids} if after_ran else None,
        "findings": findings,
        "hunks": [{"id": h["id"], "line": h["i1"] + 1, "out_line": h["j1"] + 1,
                   "removed": len(h["before"]), "added": len(h["after"])} for h in all_hunks],
        "evidence": evidence,
        "guard": guard,
        "error": error or (guard or {}).get("code"),
        # Comments in the target that SAY a finding was fixed (review_packet judges them
        # against the findings' status: a claim for an open finding is a claim without evidence).
        "comment_claims": comment_claims(out_lines, findings, in_lines=in_lines),
    }


def _attribute_by_block(f: dict, all_hunks: Sequence[Mapping[str, Any]],
                        lines: Sequence[Mapping[str, Any]], in_lines: Sequence[str],
                        out_lines: Sequence[str], moved: Mapping[int, int]) -> None:
    """A finding the after-run no longer reports and no changed line names. When the line it is
    on (a route's `- path:`) is gone from the target, its block went with it:
    ``removed_with_block``, tied to the hunk that removed that line. Else the hunk that touches
    its BLOCK (the route, the key) did it — the one with a line its fix hint asks for, else the
    one changing most of the block — and ``fixed`` is what the block gained (lines new to it, or
    moved in from elsewhere: a rate limit that now sits under the route). Else the nearest hunk."""
    line = f["location"].get("partner_line")
    if not isinstance(line, int) or not all_hunks:
        return
    if line not in moved:
        h = next((x for x in all_hunks if int(x["i1"]) < line <= int(x["i2"])), None)
        if h is not None:
            f.update(hunk_id=h["id"], fixed=None, fixed_by="removed_with_block")
            _kind_of_hunk(f, lines)
            return
    lo, hi = block(in_lines, line - 1)
    hint = {"fix": f.get("_fix"), "rule_id": f["source"]["rule_id"]}

    def _overlap(h: Mapping[str, Any]) -> int:
        i1, i2 = int(h["i1"]), int(h["i2"])
        return max(0, min(i2, hi) - max(i1, lo)) if i2 > i1 else int(lo < i1 <= hi)

    def _asks(h: Mapping[str, Any]) -> bool:
        return any(_asks_for(x, hint) for x in [*h["before"], *h["after"]] if x)

    touching = [h for h in all_hunks if _overlap(h)]
    if touching:
        h = max(touching, key=lambda x: (_asks(x), _overlap(x), -int(x["i1"])))
        mapped_in = {moved[i + 1] for i in range(lo, hi) if (i + 1) in moved}
        start = moved.get(line, mapped_in and min(mapped_in))
        olo, ohi = block(out_lines, int(start) - 1) if start else (0, 0)
        from_outside = set(moved.values()) - mapped_in
        gained = [out_lines[j] for j in range(olo, ohi) if (j + 1) not in mapped_in
                  and ((j + 1) in from_outside or _asks_for(out_lines[j], hint))]
        f.update(hunk_id=h["id"], fixed=_joined(gained) or (mask(out_lines[start - 1])
                                                          if start else None),
                 fixed_by="block")
    else:
        h = min(all_hunks, key=lambda x: abs(int(x["i1"]) + 1 - line))
        f.update(hunk_id=h["id"], fixed=mask(out_lines[moved[line] - 1]) if line in moved
                 else None, fixed_by="nearest")
    _kind_of_hunk(f, lines)


#: A comment that says something was done…
_CLAIM = re.compile(r"\b(remov|delet|fix|resolv|remediat|address|drop|clos|secur|strip|replac)"
                    r"\w*", re.I)
#: …unless it says it was NOT (left for a person, untouched, pending, "not removed").
_NOT_DONE = re.compile(r"\b(?:(?:not|never|nothing)\s+(?:yet\s+|been\s+)?(?:remov|delet|fix|"
                       r"resolv|remediat|address|drop|clos|chang|touch)\w*|left|leave|leaves|"
                       r"untouched|pending|todo|unchanged|kept|needs?\s+a\s+(?:person|human)|"
                       r"for\s+a\s+(?:person|human))\b", re.I)


def comment_claims(out_lines: Sequence[str], findings: Sequence[Mapping[str, Any]],
                   in_lines: Optional[Sequence[str]] = None) -> list[dict]:
    """The target's comment lines that claim a finding's fix: a ``#`` comment naming a rule id
    (or a finding id) with a done-word ("removed", "fixed", …) and no not-done word.
    ``[{line, text, rule, finding}]`` (text masked). Whether the claim is TRUE is judged
    against the findings' status in the packet (review_packet.comment_claims) — an agent wrote
    "removed the inlined api_key" over a key that was still there (dry run 2, gap 8)."""
    rules = sorted({str((f.get("source") or {}).get("rule_id") or "") for f in findings
                    if (f.get("source") or {}).get("kind") == "validator"} - {""},
                   key=len, reverse=True)
    ids = {str(f.get("id")) for f in findings}
    out: list[dict] = []
    # Only comments the CHANGE wrote: a comment the partner sent is theirs, never a claim.
    sent = {x.strip() for x in in_lines or ()}
    for n, raw in enumerate(out_lines, start=1):
        if raw.strip() in sent:
            continue
        at = raw.find("#")
        if at < 0 or (raw[:at].strip() and raw[at - 1] not in " \t"):
            continue
        body = raw[at + 1:]
        if not _CLAIM.search(body) or _NOT_DONE.search(body):
            continue
        rule = next((r for r in rules if r in body), None)
        fid = next((i for i in re.findall(r"\b[VU]-\d{2}\b", body) if i in ids), None)
        if rule or fid:
            out.append({"line": n, "text": mask(raw.strip()), "rule": rule, "finding": fid})
    return out


def change_of(f: Mapping[str, Any]) -> str:
    """WHAT HAPPENED TO THE FINDING'S OWN LINE (dry run 2, gap 7), so a card never reads a
    `fixed: null` as "removed" on a finding nobody touched:
    ``none`` — open, nothing changed for it; ``edited`` — its line now reads ``fixed``;
    ``added`` — its line is kept and ``fixed`` holds the lines added for it (a rate limit under
    a route); ``removed`` — its line (block) is gone; ``elsewhere`` — its own line is unchanged
    and the fix is the hunk ``hunk_id`` (``fixed`` is None: never "true → true")."""
    kind = (f.get("source") or {}).get("kind")
    if f.get("introduced"):
        return "added"          # new on the target: ``fixed`` is the line that brought it
    if kind == "guard":
        if f.get("original") is None:
            return "added"
        return "removed" if f.get("fixed") is None else "edited"
    pending = bool(f.get("fixed_if_released") or f.get("held"))
    if f.get("status") != "fixed" and not pending:
        return "none"
    if not f.get("hunk_id") and f.get("fixed") is None:
        return "none"
    if f.get("fixed_by") == "removed_with_block" or f.get("fixed") is None:
        return "removed"
    if f.get("original") is None:
        return "added"
    if (f.get("location") or {}).get("target_line") is not None:
        return "elsewhere" if f.get("fixed") == f.get("original") else "added"
    return "edited"


def set_change(f: dict) -> dict:
    """``change`` (change_of), and the ``fixed`` text it allows: none for ``none`` (an open
    finding never carries a "removed" line) and for ``elsewhere``."""
    f["change"] = change_of(f)
    if f["change"] in ("none", "elsewhere"):
        f["fixed"] = None
    return f


def _kind_of_hunk(f: dict, lines: Sequence[Mapping[str, Any]]) -> None:
    mine = [ln for ln in lines if ln["hunk_id"] == f["hunk_id"]]
    f["fix_kind"] = "auto" if mine and all(x["kind"] == "auto" for x in mine) else "agent"
