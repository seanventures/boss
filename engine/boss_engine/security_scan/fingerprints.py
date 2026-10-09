"""STABLE FINGERPRINTS (spec §4.4).

fingerprint = sha256(tool ‖ rule_id ‖ normalised location ‖ normalised snippet), where the
normalised location is

* for code: (file path, ENCLOSING FUNCTION name) — not the line, so a fix that moves lines does
  not make an untouched finding look "fixed" and a new one;
* for a dependency: (lockfile path, ecosystem, package) — not the version, so an upgrade that is
  still vulnerable stays the same finding, and one that is not makes it disappear;
* for DAST: (method, path template, parameter) — not the host or port (the in-sandbox app gets a
  fresh port every scan) and not ids in the path (/users/42 and /users/43 are one route).

The snippet is the REDACTED one: a hardcoded secret whose value is rotated is still the same
hardcoded secret, and no hash of a secret value is ever stored.

`snippet_key` = sha256(tool ‖ rule ‖ snippet) without the location: a finding gone from one place
whose snippet_key appears somewhere else was MOVED, not fixed (report.py).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .findings import Finding

MODULE = "<module>"
_SEP = "\x1f"

_JS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
_JAVA_EXT = {".java", ".kt", ".kts", ".scala", ".groovy"}
_PY_EXT = {".py", ".pyi"}
_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "with", "elif",
             "else", "do", "try", "new", "typeof", "await", "synchronized"}

_JS_HEADERS = (
    re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s+)?function\b"),
    re.compile(r"([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    # a route handler written inline: app.get("/users", (req, res) => {  ->  app.get "/users"
    re.compile(r"([A-Za-z_$][\w$.]*\.(?:get|post|put|patch|delete|all|use|route))\s*\(\s*"
               r"(['\"`][^'\"`]*['\"`])"),
    re.compile(r"^\s*(?:static\s+|async\s+|get\s+|set\s+|public\s+|private\s+|protected\s+)*"
               r"([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*(?::\s*[\w<>\[\]|, ]+)?\s*\{"),
    re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)"),
)
_JAVA_HEADERS = (
    re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|private|protected|static|final|"
               r"synchronized|abstract|default|native|override|suspend|fun)\s+)*"
               r"[\w<>\[\],.?]+(?:\s+[\w<>\[\],.?]+)*\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract|sealed|open|data)\s+)*"
               r"(?:class|interface|enum|record|object)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*fun\s+(?:<[^>]+>\s*)?([A-Za-z_]\w*)\s*\("),
)
_PY_HEADER = re.compile(r"^(\s*)(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")
_STRINGS = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`")
_LINE_COMMENT = re.compile(r"//.*$")


def language_of(path: str) -> str:
    ext = PurePosixPath(path).suffix.lower()
    if ext in _JS_EXT:
        return "js"
    if ext in _JAVA_EXT:
        return "java"
    if ext in _PY_EXT:
        return "python"
    return "other"


def _brace_depth(lines: list[str], start: int, end: int) -> int:
    depth = 0
    seen_open = False
    for raw in lines[start:end + 1]:
        code = _LINE_COMMENT.sub("", _STRINGS.sub('""', raw))
        for ch in code:
            if ch == "{":
                depth += 1
                seen_open = True
            elif ch == "}":
                depth -= 1
    return depth if seen_open else 0


def _brace_header(line: str, lang: str) -> Optional[str]:
    for pattern in (_JS_HEADERS if lang == "js" else _JAVA_HEADERS):
        m = pattern.search(line)
        if not m:
            continue
        name = m.group(1)
        if name in _KEYWORDS:
            continue
        if m.lastindex and m.lastindex >= 2 and pattern is _JS_HEADERS[3]:
            name = f"{name} {m.group(2)}"
        return name
    return None


def enclosing_function(text: str, line: int, lang: str) -> str:
    """The innermost function/method/class whose body holds 1-based `line`, else <module>."""
    lines = text.splitlines()
    if not lines or line < 1:
        return MODULE
    target = min(line, len(lines)) - 1
    if lang == "python":
        def indent(s: str) -> int:
            return len(s) - len(s.lstrip())
        t_indent = indent(lines[target]) if lines[target].strip() else None
        floor = t_indent
        for i in range(target, -1, -1):
            s = lines[i]
            if not s.strip():
                continue
            m = _PY_HEADER.match(s)
            ind = indent(s)
            if m and (i == target or (floor is not None and ind < floor)):
                return m.group(2)
            if floor is None or ind < floor:
                floor = ind
        return MODULE
    if lang in ("js", "java"):
        for i in range(target, -1, -1):
            name = _brace_header(lines[i], lang)
            if name is None:
                continue
            if i == target or _brace_depth(lines, i, target) > 0:
                return name
        return MODULE
    return MODULE


def normalise_snippet(snippet: str) -> str:
    return " ".join(str(snippet or "").split())


def path_template(url_or_path: str) -> str:
    """/users/42/orders/9f1c... -> /users/{id}/orders/{id}; no scheme, host, port or query."""
    path = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path.split("?", 1)[0]
    out = []
    for seg in (path or "/").split("/"):
        if re.fullmatch(r"\d+", seg) or re.fullmatch(r"[0-9a-fA-F-]{16,}", seg) \
                or re.fullmatch(r"[A-Za-z0-9_-]{24,}", seg):
            out.append("{id}")
        else:
            out.append(seg)
    joined = "/".join(out)
    return joined if joined.startswith("/") else "/" + joined


def _sha(*parts: str) -> str:
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()


def location_key(f: Finding) -> str:
    loc = f.location
    if f.tool == "zap":
        return _SEP.join(("dast", str(loc.get("method") or "GET").upper(),
                          str(loc.get("path") or "/"), str(loc.get("param") or "")))
    if f.tool == "osv":
        return _SEP.join(("dep", str(loc.get("lockfile") or ""), str(loc.get("ecosystem") or ""),
                          str(loc.get("package") or "")))
    return _SEP.join(("code", str(loc.get("file") or ""), str(loc.get("function") or MODULE)))


def snippet_of(f: Finding) -> str:
    return normalise_snippet(str((f.evidence or {}).get("snippet") or ""))


def fingerprint(f: Finding) -> str:
    return _sha(f.tool, f.rule_id, location_key(f), snippet_of(f))


def snippet_key(f: Finding) -> str:
    return _sha(f.tool, f.rule_id, snippet_of(f))


def annotate_functions(findings: Iterable[Finding], root: Optional[Path]) -> None:
    """Fill location.function for code findings from the checkout's source (read once per file)."""
    cache: dict[str, Optional[str]] = {}
    for f in findings:
        loc = f.location
        if f.tool == "osv" or f.tool == "zap" or loc.get("function"):
            continue
        rel = str(loc.get("file") or "")
        if rel not in cache:
            text = None
            if root is not None and rel:
                try:
                    p = (root / rel).resolve()
                    p.relative_to(root.resolve())           # never read outside the checkout
                    if p.is_file() and p.stat().st_size <= 2 * 1024 * 1024:
                        text = p.read_text(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    text = None
            cache[rel] = text
        text = cache[rel]
        loc["function"] = (enclosing_function(text, int(loc.get("line") or 0), language_of(rel))
                           if text is not None else MODULE)


def assign(findings: list[Finding]) -> list[Finding]:
    """Set every finding's fingerprint and snippet_key. Two findings that would share one (the same
    rule on the same code twice in one function) get an occurrence suffix, in line order, so the
    pair stays two findings and UNIQUE(scan_id, fingerprint) holds."""
    seen: dict[str, int] = {}
    for f in sorted(findings, key=lambda x: (int(x.location.get("line") or 0), x.title)):
        base = fingerprint(f)
        n = seen.get(base, 0)
        seen[base] = n + 1
        f.fingerprint = base if n == 0 else _sha(base, str(n))
        f.location["snippet_key"] = snippet_key(f)
    return findings
