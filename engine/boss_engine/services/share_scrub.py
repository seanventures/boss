"""What "Share this run" masks before the person even sees the bundle (docs/product/RUN_SCORECARDS.md).

The Activity log's scrub (`redaction.redact`: credentials and PII) is the base. A shared run also
carries what that scrub never had to think about, because it never left the install:

  URL       anything with a scheme (https://…, git@…:…)
  HOST      a hostname (acme-prod-01.internal, db.acme.com)
  PATH      a file path (/srv/acme/ledger.py, src/app/main.ts, C:\\work\\x)
  FILE      a bare file name with a source extension (ledger.py)
  REPO      this install's repositories (owner/name, owner, name)
  NAME      this install's projects, orgs and workspaces, and its people (ids and display names)

Timestamps are kept as they are: the PII phone pattern used to turn `2026-10-05 18:20:11` into
`[PHONE]`, which made a run's own timeline unreadable. They are set aside before the scrub and
put back after it. The person still reads and edits the result, and is told plainly to check it.
"""
from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy import text

from .redaction import redact

_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b|\b\d{2}:\d{2}\b")
_URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s'\"<>)\]]+|\bgit@[\w.\-]+:[\w./\-]+", re.I)
_TLDS = ("com|net|org|io|dev|app|ai|co|cloud|sh|xyz|me|us|uk|de|fr|in|jp|cn|eu|biz|info|tech|"
         "site|online|local|internal|lan|corp|home|network|intra|test|example|localhost")
_HOST = re.compile(rf"\b(?:[a-z0-9](?:[a-z0-9\-]{{0,61}}[a-z0-9])?\.)+(?:{_TLDS})\b", re.I)
_PATH = re.compile(r"(?<![\w:/.\-])(?:~|\.{1,2})?/?(?:[\w.\-@]+/)+[\w.\-@]+|\b[A-Za-z]:\\[^\s'\"]+")
_FILE = re.compile(r"\b[\w\-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|go|rs|java|kt|rb|php|cs|cpp|cc|c|h|"
                   r"hpp|swift|scala|json|ya?ml|toml|ini|cfg|env|md|sql|sh|bash|css|scss|html|"
                   r"vue|svelte|lock|txt|csv|xml|proto|tf)\b", re.I)


def install_names(connection, project_id: str) -> list[tuple[str, str]]:
    """(label, name) for every name this install knows that must not leave in a shared run."""
    out: list[tuple[str, str]] = []

    def rows(sql: str, **p) -> Iterable:
        try:
            with connection.begin_nested():
                return connection.execute(text(sql), p).all()
        except Exception:  # noqa: BLE001 — a missing table in a partial schema hides nothing
            return []

    for owner, name, full in rows("SELECT owner, name, full_name FROM github_repository_bindings"):
        out += [("REPO", full), ("REPO", owner), ("REPO", name)]
    for (spec,) in rows("SELECT spec FROM code_task_runs WHERE project_id = :p "
                        "ORDER BY created_at DESC LIMIT 200", p=project_id):
        if isinstance(spec, dict):
            for key in ("repo", "owner"):
                if spec.get(key):
                    out.append(("REPO", str(spec[key])))
    for row in rows("SELECT id, name, slug FROM projects"):
        out += [("NAME", str(v)) for v in row if v]
    for row in rows("SELECT id, name FROM orgs"):
        out += [("NAME", str(v)) for v in row if v]
    for row in rows("SELECT id, name FROM workspaces"):
        out += [("NAME", str(v)) for v in row if v]
    for table in ("project_members", "org_members"):
        for mid, display in rows(f"SELECT member_id, display_name FROM {table}"):
            out += [("NAME", str(mid)), ("NAME", str(display))]
            out += [("NAME", part) for part in str(display).split() if len(part) >= 3]
    # Longest first, so "acme/payroll" goes before "acme"; nothing shorter than 3 characters.
    seen: set[str] = set()
    names = []
    for label, name in sorted(out, key=lambda x: -len(x[1])):
        n = name.strip()
        if len(n) < 3 or n.lower() in seen:
            continue
        seen.add(n.lower())
        names.append((label, n))
    return names


def scrub(raw: str, names: list[tuple[str, str]]) -> tuple[str, dict[str, int]]:
    """(masked text, {label: count})."""
    keep: list[str] = []

    def hold(m: re.Match) -> str:
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"

    work = _TIMESTAMP.sub(hold, raw or "")
    counts: dict[str, int] = {}

    def mask(pattern: re.Pattern, label: str) -> None:
        nonlocal work

        def sub(_m: re.Match) -> str:
            counts[label] = counts.get(label, 0) + 1
            return f"[{label}]"

        work = pattern.sub(sub, work)

    # Shapes first (a host or a path is masked whole), then this install's own names.
    mask(_URL, "URL")
    work, found = redact(work)
    for k, v in found.items():
        counts[k] = counts.get(k, 0) + v
    mask(_PATH, "PATH")
    mask(_FILE, "FILE")
    mask(_HOST, "HOST")
    for label, name in names:
        mask(re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])", re.I), label)
    work = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], work)
    return work, counts
