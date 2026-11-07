"""What the app reads from its environment, BY NAME ONLY (the onboarding agent's Secrets check,
screen 10 "App secret · OPENAI_API_KEY — only you").

A repository says which settings its app needs in several places, and the onboarding agent reads
all of them from the checkout, never a value:

  boss.deploy.yaml `env:`        declared for BOSS: generated, a demo login, a literal, a secret
  .env.example / .env.sample     `NAME=` lines (an empty value is one the app expects to be set)
  docker-compose `environment:`  `${NAME}` (no default: required) or `${NAME:-x}` (has a default)
  the source                     process.env.NAME, os.environ["NAME"], os.getenv("NAME"),
                                 System.getenv("NAME"), ENV["NAME"], Deno.env.get("NAME") …

Each name is then classed by WHO SETS IT: BOSS generates it (a session key, a demo password),
the manifest sets it (a literal), it has a default (optional), or nobody does — and a name nobody
sets that the app needs is the one item only a person can supply. Values are never read: a
`.env` file itself is skipped, and only the left of `=` in an example file is kept.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

#: Source files read for env reads, at most this many, each at most this big.
MAX_FILES = 4000
MAX_BYTES = 256_000
_NAME = r"([A-Z][A-Z0-9_]{1,63})"
#: How languages read one variable (syntax, not a word list): the captured group is the name.
_READS = (
    re.compile(r"process\.env\.%s\b" % _NAME),
    re.compile(r"process\.env\[\s*['\"]%s['\"]\s*\]" % _NAME),
    re.compile(r"import\.meta\.env\.%s\b" % _NAME),
    re.compile(r"os\.environ\[\s*['\"]%s['\"]\s*\]" % _NAME),
    re.compile(r"os\.(?:environ\.get|getenv)\(\s*['\"]%s['\"]" % _NAME),
    re.compile(r"System\.getenv\(\s*\"%s\"" % _NAME),
    re.compile(r"\bENV\[\s*['\"]%s['\"]\s*\]" % _NAME),
    re.compile(r"\bENV\.fetch\(\s*['\"]%s['\"]" % _NAME),
    re.compile(r"Deno\.env\.get\(\s*['\"]%s['\"]" % _NAME),
    re.compile(r"os\.Getenv\(\s*\"%s\"" % _NAME),
    re.compile(r"env::var\(\s*\"%s\"" % _NAME),
)
_SOURCE = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java",
           ".kt", ".php", ".cs", ".ex", ".exs", ".vue", ".svelte"}
_SKIP = {".git", "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__",
         "target", ".next", "coverage", "tests", "test", "__tests__", "spec"}
_COMPOSE = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
_EXAMPLES = (".env.example", ".env.sample", ".env.template", "example.env", ".env.dist")
#: Names every process has, or a platform sets: never asked of anyone.
_PLATFORM = {"NODE_ENV", "PORT", "HOST", "HOSTNAME", "PATH", "HOME", "PWD", "TZ", "LANG", "CI",
             "USER", "SHELL", "TERM", "DEBUG", "PYTHONPATH", "NODE_OPTIONS"}


def _files(root: Path) -> Iterable[Path]:
    n = 0
    for path in sorted(root.rglob("*")):
        if n >= MAX_FILES:
            return
        rel = path.relative_to(root).parts
        if any(p in _SKIP for p in rel[:-1]) or not path.is_file():
            continue
        if path.suffix in _SOURCE:
            n += 1
            yield path


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_BYTES:
            return ""
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def _compose(root: Path) -> dict[str, dict[str, Any]]:
    """{name: {required, default?}} from the first compose file's services' `environment:`."""
    out: dict[str, dict[str, Any]] = {}
    for name in _COMPOSE:
        path = root / name
        if not path.is_file():
            continue
        body = _read(path)
        for m in re.finditer(r"\$\{%s(:?-([^}]*))?\}" % _NAME, body):
            has_default = m.group(2) is not None
            seen = out.setdefault(m.group(1), {"required": not has_default, "where": name})
            seen["required"] = seen["required"] and not has_default
        break
    return out


def _examples(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in _EXAMPLES:
        path = root / name
        if not path.is_file():
            continue
        for line in _read(path).splitlines():
            m = re.match(r"\s*(?:export\s+)?%s\s*=\s*(.*)$" % _NAME, line)
            if m:
                # Only whether a value is there, never the value.
                out.setdefault(m.group(1), {"required": not m.group(2).strip().strip("'\""),
                                            "where": name})
    return out


def _code(root: Path) -> dict[str, str]:
    """{name: first file that reads it}."""
    out: dict[str, str] = {}
    for path in _files(root):
        body = _read(path)
        if "env" not in body:
            continue
        for pattern in _READS:
            for m in pattern.finditer(body):
                out.setdefault(m.group(1), str(path.relative_to(root)))
    return out


def scan(root: Optional[Path]) -> dict[str, Any]:
    """{names: [...], generated, demo, set, optional, yours, read_in_code} — names only.

    `yours` is what only a person can supply: a manifest `{secret}`, or a name the app needs (a
    compose `${NAME}` with no default, an example line with no value) that nothing sets."""
    from ...templates.run_env import needs_from_app_dir

    empty = {"names": [], "generated": [], "demo": [], "set": [], "optional": [], "yours": [],
             "read_in_code": [], "sources": []}
    if root is None or not Path(root).is_dir():
        return empty
    root = Path(root)
    try:
        needs = needs_from_app_dir(root)
    except Exception:  # noqa: BLE001 — an unreadable manifest declares nothing
        needs = {}
    compose, examples, code = _compose(root), _examples(root), _code(root)
    generated = {n for n, d in needs.items() if d.kind == "generated"}
    demo = {n for n, d in needs.items() if d.kind == "demo"}
    literal = {n for n, d in needs.items() if d.kind in ("literal", "stub", "listed")}
    secret = {n for n, d in needs.items() if d.kind == "secret"}
    names = (set(needs) | set(compose) | set(examples) | set(code)) - _PLATFORM
    handled = generated | demo | literal
    yours = set(secret) - handled
    for n in names - handled - secret:
        required = (compose.get(n) or {}).get("required") or (examples.get(n) or {}).get("required")
        if required:
            yours.add(n)
    optional = names - handled - yours
    sources = [s for s, has in (("boss.deploy.yaml", (root / "boss.deploy.yaml").is_file()),
                                ("compose", bool(compose)), (".env example", bool(examples)),
                                ("code", bool(code))) if has]
    return {"names": sorted(names), "generated": sorted(generated & names),
            "demo": sorted(demo & names), "set": sorted(literal & names),
            "optional": sorted(optional), "yours": sorted(yours),
            "read_in_code": sorted(set(code) - _PLATFORM), "sources": sources}


__all__ = ["scan"]
