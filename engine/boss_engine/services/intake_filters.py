"""THE ROUTE FILTER LANGUAGE (P9a-2, BOSSAPP-1094; boss-eventing-lld.md §5.2).

A route's `match` says which events it takes, in a small JSON language evaluated over the
envelope (services/intake_router): booleans `all` / `any` / `not`, and leaves of the form
`{"field": <path>, "op": <op>, "value": <v>}`.

    {"all": [{"field": "type", "op": "in", "value": ["com.slack.message.app_mention"]},
             {"field": "data.channel", "op": "eq", "value": "C0SUPPORT"},
             {"not": {"field": "data.text", "op": "matches", "value": "(?i)^\\s*ignore"}}]}

  Depth ≤ 6, ≤ 64 leaves.                     A larger filter is refused when saved.
  eq ne in nin                                 exact (strings compare as strings, numbers as
                                               numbers, booleans as booleans)
  prefix suffix contains                       case-insensitive text
  exists                                       the field is present and not null (value: bool)
  gt gte lt lte                                numbers, or ISO-8601 instants (both sides)
  matches                                      a regular expression over the field's text — RE2
                                               syntax through `google-re2` when it is installed;
                                               otherwise the `regex` package with a 2 ms timeout
                                               and a 2 KB pattern cap. NEVER Python `re` on text
                                               an outsider wrote (backtracking is a denial of
                                               service a filter author may not mean).
  changed                                      record sources: `value` (a field name, or a list)
                                               is in `data.changed_fields`
  glob                                         a path glob (or a list of them) over a path, or
                                               over a list of paths / {filename} objects (any
                                               one matches): `*` and `?` stay inside one path
                                               segment, `**` spans segments, `[abc]` a class.
                                               {"field": "data.changed_files", "op": "glob",
                                                "value": "tenants/*.yaml"}

Fields are dotted envelope paths: `type`, `source`, `subject`, `data.*`, `bossactor.kind`,
`bossactor.email_domain` (derived), `bossscreen.verdict`, `bossconversation.*`.

A filter is COMPILED ONCE per trigger version and cached (5 s TTL + explicit invalidation, the
subscription-cache pattern of event_dispatch), and evaluated in the processor only — the intake
route never looks at a filter (LLD finding F9).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

MAX_DEPTH = 6
MAX_LEAVES = 64
MAX_PATTERN_BYTES = 2048
#: The fallback engine's budget for one match (seconds).
MATCH_TIMEOUT_SECONDS = 0.002
#: Text longer than this is matched on its head only (a pattern over a 256 KB body is not a filter).
MAX_MATCH_TEXT = 16 * 1024
CACHE_TTL_SECONDS = 5.0

BOOL_OPS = ("all", "any", "not")
LEAF_OPS = ("eq", "ne", "in", "nin", "prefix", "suffix", "contains", "exists",
            "gt", "gte", "lt", "lte", "matches", "changed", "glob")
#: A glob is a path pattern, not a program: short, and a bounded number of wildcards.
MAX_GLOB_CHARS = 256
MAX_GLOB_PATTERNS = 16
#: A path longer than this never matches (no file list entry is this long).
MAX_GLOB_PATH = 1024


class FilterError(ValueError):
    """A filter that cannot be compiled, said for the person who wrote it."""


# ---------------------------------------------------------------------------------------------
# Regular expressions: RE2 when present, else `regex` with a timeout; never `re` on untrusted text
# ---------------------------------------------------------------------------------------------

def _regex_engine() -> tuple[str, Any]:
    try:
        import re2  # type: ignore[import-not-found]  # google-re2

        return "re2", re2
    except Exception:  # noqa: BLE001
        pass
    try:
        import regex  # type: ignore[import-not-found]

        return "regex", regex
    except Exception:  # noqa: BLE001
        return "none", None


REGEX_ENGINE, _REGEX = _regex_engine()


class RegexTimeout(Exception):
    """The fallback engine ran out of its 2 ms budget: the leaf answers False and the pattern is
    marked so the route's health shows it."""


def compile_pattern(pattern: str) -> Callable[[str], bool]:
    """A `matches` leaf's predicate. Refuses a pattern the engine cannot take (syntax, size)."""
    if not isinstance(pattern, str) or not pattern:
        raise FilterError("matches needs a pattern (text).")
    if len(pattern.encode("utf-8")) > MAX_PATTERN_BYTES:
        raise FilterError(f"a matches pattern is at most {MAX_PATTERN_BYTES} bytes.")
    if REGEX_ENGINE == "re2":
        try:
            compiled = _REGEX.compile(pattern)
        except Exception as error:  # noqa: BLE001 — re2's own error type
            raise FilterError(f"matches: not an RE2 pattern ({error}).") from None

        def run_re2(text: str) -> bool:
            return compiled.search(text[:MAX_MATCH_TEXT]) is not None

        return run_re2
    if REGEX_ENGINE == "regex":
        try:
            compiled = _REGEX.compile(pattern)
        except Exception as error:  # noqa: BLE001
            raise FilterError(f"matches: not a valid pattern ({error}).") from None

        def run_regex(text: str) -> bool:
            try:
                return compiled.search(text[:MAX_MATCH_TEXT], timeout=MATCH_TIMEOUT_SECONDS) \
                    is not None
            except TimeoutError:
                raise RegexTimeout(pattern[:80]) from None

        return run_regex
    raise FilterError("matches needs the google-re2 or regex package installed on the API.")


# ---------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------

def lookup(envelope: Mapping[str, Any], path: str) -> Any:
    """A dotted path over the envelope dict; `bossactor.email_domain` is derived from the email.
    Missing → None."""
    if path == "bossactor.email_domain":
        email = str(((envelope.get("bossactor") or {}).get("email")) or "")
        return email.rsplit("@", 1)[-1].lower() if "@" in email else None
    cur: Any = envelope
    for part in path.split("."):
        if isinstance(cur, Mapping):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        import json

        return json.dumps(value, default=str, sort_keys=True)
    return str(value)


def _as_instant(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    ln, rn = _as_number(left), _as_number(right)
    if ln is not None and rn is not None and not isinstance(left, str) and \
            not isinstance(right, str):
        return ln == rn
    if isinstance(left, (list, dict)) or isinstance(right, (list, dict)):
        return left == right
    return _as_text(left) == _as_text(right)


def _ordered(left: Any, right: Any) -> Optional[int]:
    """-1 / 0 / 1 for numbers or instants; None when the two cannot be compared."""
    ln, rn = _as_number(left), _as_number(right)
    if ln is not None and rn is not None:
        return (ln > rn) - (ln < rn)
    li, ri = _as_instant(left), _as_instant(right)
    if li is not None and ri is not None:
        return (li > ri) - (li < ri)
    return None


# ---------------------------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------------------------

@dataclass
class Compiled:
    """A compiled filter: `matches(envelope)` is pure and fast. `timeouts` counts `matches`
    leaves that ran out of their budget (the route's health shows it)."""

    spec: Any
    leaves: int
    _fn: Callable[[Mapping[str, Any]], bool] = field(repr=False)
    timeouts: int = 0

    def matches(self, envelope: Mapping[str, Any]) -> bool:
        try:
            return bool(self._fn(envelope))
        except RegexTimeout:
            self.timeouts += 1
            return False

    def explain(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Which leaves held, for the console's "would match" preview."""
        return {"matched": self.matches(envelope), "leaves": _explain(self.spec, envelope)}


def _explain(node: Any, envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(node, Mapping):
        return out
    for key in BOOL_OPS:
        if key in node:
            children = node[key] if isinstance(node[key], list) else [node[key]]
            for child in children:
                out.extend(_explain(child, envelope))
            return out
    try:
        held = compile_filter(node).matches(envelope)
    except FilterError:
        held = False
    out.append({"field": node.get("field"), "op": node.get("op"), "value": node.get("value"),
                "actual": lookup(envelope, str(node.get("field") or "")), "held": held})
    return out


_ALWAYS = Compiled(spec=None, leaves=0, _fn=lambda _e: True)


def compile_filter(spec: Any) -> Compiled:
    """Compile `spec`; `None` / `{}` is "everything". FilterError names what is wrong."""
    if spec in (None, {}, []):
        return _ALWAYS
    if not isinstance(spec, Mapping):
        raise FilterError("match must be an object.")
    counter = {"leaves": 0}
    fn = _compile_node(spec, depth=1, counter=counter)
    return Compiled(spec=dict(spec), leaves=counter["leaves"], _fn=fn)


def _compile_node(node: Any, *, depth: int, counter: dict) -> Callable[[Mapping[str, Any]], bool]:
    if depth > MAX_DEPTH:
        raise FilterError(f"match nests deeper than {MAX_DEPTH} levels.")
    if not isinstance(node, Mapping):
        raise FilterError("every rule is an object ({field, op, value} or all/any/not).")
    bools = [k for k in BOOL_OPS if k in node]
    if len(bools) > 1 or (bools and "field" in node):
        raise FilterError("a rule is one of all / any / not, or one {field, op, value} leaf.")
    if bools:
        key = bools[0]
        if key == "not":
            inner = _compile_node(node["not"], depth=depth + 1, counter=counter)
            return lambda e: not inner(e)
        children = node[key]
        if not isinstance(children, list):
            raise FilterError(f"{key} takes a list of rules.")
        # An empty `all` holds (nothing to fail) and an empty `any` never does, as in Python.
        compiled = [_compile_node(c, depth=depth + 1, counter=counter) for c in children]
        if key == "all":
            return lambda e: all(c(e) for c in compiled)
        return lambda e: any(c(e) for c in compiled)
    return _compile_leaf(node, counter)


def _compile_leaf(node: Mapping[str, Any], counter: dict) -> Callable[[Mapping[str, Any]], bool]:
    counter["leaves"] += 1
    if counter["leaves"] > MAX_LEAVES:
        raise FilterError(f"match has more than {MAX_LEAVES} conditions.")
    path = node.get("field")
    op = str(node.get("op") or "").strip().lower()
    value = node.get("value")
    if not isinstance(path, str) or not path.strip():
        raise FilterError("a condition names a field (type, subject, data.channel, ...).")
    if any(ch.isspace() for ch in path) or path.startswith(".") or path.endswith("."):
        raise FilterError(f"not a field path: {path!r}.")
    if op not in LEAF_OPS:
        raise FilterError(f"op must be one of {', '.join(LEAF_OPS)}, not {op!r}.")
    path = path.strip()
    if op in ("eq", "ne"):
        return (lambda e: _equal(lookup(e, path), value)) if op == "eq" else \
            (lambda e: not _equal(lookup(e, path), value))
    if op in ("in", "nin"):
        if not isinstance(value, list):
            raise FilterError(f"{op} takes a list of values.")
        choices = list(value)

        def _in(e: Mapping[str, Any]) -> bool:
            got = lookup(e, path)
            if isinstance(got, list):
                return any(_equal(g, c) for g in got for c in choices)
            return any(_equal(got, c) for c in choices)

        return _in if op == "in" else (lambda e: not _in(e))
    if op in ("prefix", "suffix", "contains"):
        if not isinstance(value, str):
            raise FilterError(f"{op} takes text.")
        needle = value.lower()
        if op == "prefix":
            return lambda e: _as_text(lookup(e, path)).lower().startswith(needle)
        if op == "suffix":
            return lambda e: _as_text(lookup(e, path)).lower().endswith(needle)
        return lambda e: needle in _as_text(lookup(e, path)).lower()
    if op == "exists":
        want = True if value is None else bool(value)
        return lambda e: (lookup(e, path) is not None) == want
    if op in ("gt", "gte", "lt", "lte"):
        if _as_number(value) is None and _as_instant(value) is None:
            raise FilterError(f"{op} compares numbers or ISO-8601 instants.")
        wanted = {"gt": (1,), "gte": (1, 0), "lt": (-1,), "lte": (-1, 0)}[op]

        def _cmp(e: Mapping[str, Any]) -> bool:
            got = _ordered(lookup(e, path), value)
            return got is not None and got in wanted

        return _cmp
    if op == "matches":
        predicate = compile_pattern(value)
        return lambda e: predicate(_as_text(lookup(e, path)))
    if op == "glob":
        globs = compile_globs(value)

        def _glob(e: Mapping[str, Any]) -> bool:
            return any(g(p) for p in _paths(lookup(e, path)) for g in globs)

        return _glob
    # changed: `value` names the field(s); `path` is where the changed list lives (default
    # data.changed_fields when the author writes the conventional field).
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise FilterError("changed takes a field name or a list of field names.")
    where = path if path != "changed" else "data.changed_fields"

    def _changed(e: Mapping[str, Any]) -> bool:
        got = lookup(e, where)
        if isinstance(got, Mapping):
            got = list(got.keys())
        if not isinstance(got, list):
            return False
        have = {str(g) for g in got}
        return any(n in have for n in names)

    return _changed


# ---------------------------------------------------------------------------------------------
# Path globs (AMEX-G11): `tenants/*.yaml` over a pull request's changed files
# ---------------------------------------------------------------------------------------------

def _paths(value: Any) -> list[str]:
    """The paths a field holds: one path, or a list of paths or of {filename|path} objects."""
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            item = item.get("filename") or item.get("path")
        if isinstance(item, str) and item and len(item) <= MAX_GLOB_PATH:
            out.append(item)
    return out


def _segment_tokens(segment: str) -> list[tuple[str, Any]]:
    """`*` → ("star",), `?` → ("any",), `[a-z]` / `[!x]` → ("class", (negated, ranges)),
    anything else → ("char", c)."""
    tokens: list[tuple[str, Any]] = []
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "*":
            if not tokens or tokens[-1][0] != "star":
                tokens.append(("star", None))
        elif ch == "?":
            tokens.append(("any", None))
        elif ch == "[" and "]" in segment[i + 2:] or (ch == "[" and segment[i + 1:i + 2] == "]"):
            close = segment.index("]", i + 2)
            body = segment[i + 1:close]
            negated = body[:1] in ("!", "^")
            body = body[1:] if negated else body
            ranges = []
            k = 0
            while k < len(body):
                if k + 2 < len(body) and body[k + 1] == "-":
                    ranges.append((body[k], body[k + 2]))
                    k += 3
                else:
                    ranges.append((body[k], body[k]))
                    k += 1
            tokens.append(("class", (negated, tuple(ranges))))
            i = close
        else:
            tokens.append(("char", ch))
        i += 1
    return tokens


def _segment_matcher(segment: str) -> Callable[[str], bool]:
    """One path segment's pattern, matched WITHOUT regular expressions (the text is a file name
    an outsider chose): the classic one-star-backtrack walk, O(len(text) × len(pattern))."""
    if not any(ch in segment for ch in "*?["):
        return lambda text: text == segment
    tokens = _segment_tokens(segment)

    def one(token: tuple[str, Any], ch: str) -> bool:
        kind, arg = token
        if kind == "any":
            return True
        if kind == "char":
            return ch == arg
        negated, ranges = arg
        inside = any(lo <= ch <= hi for lo, hi in ranges)
        return inside != negated

    def match(text: str) -> bool:
        t = p = 0
        star, mark = -1, 0
        while t < len(text):
            if p < len(tokens) and tokens[p][0] != "star" and one(tokens[p], text[t]):
                t += 1
                p += 1
            elif p < len(tokens) and tokens[p][0] == "star":
                star, mark = p, t
                p += 1
            elif star >= 0:
                p = star + 1
                mark += 1
                t = mark
            else:
                return False
        while p < len(tokens) and tokens[p][0] == "star":
            p += 1
        return p == len(tokens)

    return match


def compile_globs(value: Any) -> list[Callable[[str], bool]]:
    """`glob`'s predicates. Refuses a pattern that is empty, too long, or absolute."""
    patterns = [value] if isinstance(value, str) else value
    if not isinstance(patterns, list) or not patterns or \
            not all(isinstance(p, str) and p.strip() for p in patterns):
        raise FilterError("glob takes a path pattern (or a list of them), e.g. tenants/*.yaml.")
    if len(patterns) > MAX_GLOB_PATTERNS:
        raise FilterError(f"glob takes at most {MAX_GLOB_PATTERNS} patterns.")
    out = []
    for raw in patterns:
        pattern = raw.strip()
        if len(pattern) > MAX_GLOB_CHARS:
            raise FilterError(f"a glob is at most {MAX_GLOB_CHARS} characters.")
        if pattern.startswith("/"):
            raise FilterError("a glob is relative to the repository root (no leading /).")
        parts = pattern.split("/")
        if any(p == "" for p in parts):
            raise FilterError(f"not a path pattern: {pattern!r}.")
        out.append(_path_glob([None if p == "**" else _segment_matcher(p) for p in parts]))
    return out


def _path_glob(parts: list) -> Callable[[str], bool]:
    def match(path: str) -> bool:
        segments = path.split("/")
        # (pattern index, segment index) reachable: O(patterns × segments), no recursion.
        states = {(0, 0)}
        seen: set = set()
        while states:
            i, j = states.pop()
            if (i, j) in seen:
                continue
            seen.add((i, j))
            if i == len(parts):
                if j == len(segments):
                    return True
                continue
            part = parts[i]
            if part is None:                         # `**`: zero or more segments
                states.add((i + 1, j))
                if j < len(segments):
                    states.add((i, j + 1))
            elif j < len(segments) and part(segments[j]):
                states.add((i + 1, j + 1))
        return False

    return match


def validate(spec: Any) -> dict[str, Any]:
    """Compile and report {leaves, engine}; FilterError when it cannot be compiled."""
    compiled = compile_filter(spec)
    return {"leaves": compiled.leaves, "regex_engine": REGEX_ENGINE}


# ---------------------------------------------------------------------------------------------
# The per-trigger-version cache
# ---------------------------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, str, Compiled]] = {}
_CACHE_LOCK = threading.RLock()


def compiled_for(trigger_id: str, version: Any, spec: Any) -> Compiled:
    """The compiled filter of trigger `trigger_id` at `version` (its updated_at, or any token
    that changes when the config does): cached CACHE_TTL_SECONDS, re-compiled when the version
    moved, dropped by `invalidate`."""
    key = str(version)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(trigger_id)
        if hit and hit[1] == key and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[2]
    compiled = compile_filter(spec)
    with _CACHE_LOCK:
        _CACHE[trigger_id] = (now, key, compiled)
    return compiled


def invalidate(trigger_id: Optional[str] = None) -> None:
    with _CACHE_LOCK:
        if trigger_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(trigger_id, None)


def cache_size() -> int:
    with _CACHE_LOCK:
        return len(_CACHE)


__all__ = ["BOOL_OPS", "CACHE_TTL_SECONDS", "Compiled", "FilterError", "LEAF_OPS", "MAX_DEPTH",
           "MAX_LEAVES", "MAX_PATTERN_BYTES", "REGEX_ENGINE", "RegexTimeout", "cache_size",
           "compile_filter", "compile_globs", "compile_pattern", "compiled_for", "invalidate", "lookup",
           "validate"]
