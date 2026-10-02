"""Every query that reads code_task_runs names columns the table actually has.

The incident (boss-1, 2026-10-02): read_pull_request — the tool an agent uses to read the PR a
build opened — selected `repo, owner FROM code_task_runs`. Those live inside the `spec` JSON, not
as columns, so every call with a crun_ id died with UndefinedColumn 'repo' before reading
anything, and "why did this preview fail" could not get as far as the pull request.

The fix reads spec->>'repo'. This test enumerates every SELECT ... FROM code_task_runs in the
engine and the API and checks each bare column against schema.sql, so the next one fails here
instead of on a live install.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _schema_columns(table: str) -> set[str]:
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", schema, re.S)
    assert body, f"{table} not found in schema.sql"
    cols = {m.group(1) for m in re.finditer(r"^\s*([a-z_][a-z0-9_]*)\s+[A-Z]", body.group(1), re.M)}
    cols |= set(re.findall(rf"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS ([a-z_][a-z0-9_]*)",
                           schema))
    return cols


def _split_top_level(select_list: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in select_list:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


_BARE = re.compile(r"^\s*(?:([a-z_]+)\.)?([a-z_][a-z0-9_]*)\s*(?:AS\s+[a-z_]+)?\s*$", re.I)
_KEYWORDS = {"distinct", "true", "false", "null"}
_NOT_ALIASES = {"where", "join", "left", "right", "inner", "order", "group", "limit", "on",
                "for", "union", "having", "cross", "full", "offset", "returning"}


def _selected_bare_columns(path: Path) -> list[tuple[int, str]]:
    """(line, column) for each bare column a `SELECT ... FROM code_task_runs` names."""
    src = path.read_text(encoding="utf-8")
    # Join adjacent string literals the way Python does, so a query split over lines reads whole.
    flat = re.sub(r'"\s*\n\s*"', "", src)
    flat = re.sub(r"'\s*\n\s*'", "", flat)
    out = []
    for m in re.finditer(r"\bFROM\s+code_task_runs\b(?:\s+(?:AS\s+)?([a-z]\w*))?", flat, re.I):
        alias = (m.group(1) or "").lower()
        if alias in _NOT_ALIASES:
            alias = ""
        head = flat[max(0, m.start() - 800):m.start()]
        sel = head.rfind("SELECT")
        if sel < 0:
            continue
        select_list = head[sel + len("SELECT"):]
        # A nested FROM between SELECT and here means this SELECT is not the one for this FROM.
        if re.search(r"\bFROM\b", select_list):
            continue
        line = flat[: m.start()].count("\n") + 1
        for item in _split_top_level(select_list):
            item = item.replace('"', " ").replace("\\n", " ").strip()
            bare = _BARE.match(item)
            if not bare or bare.group(2).lower() in _KEYWORDS:
                continue
            qualifier = (bare.group(1) or "").lower()
            # `p.url` in a query that joins app_previews AS p is not a code_task_runs column.
            if qualifier and qualifier != alias:
                continue
            out.append((line, bare.group(2)))
    return out


def test_every_select_from_code_task_runs_names_real_columns():
    columns = _schema_columns("code_task_runs")
    assert {"id", "spec", "pull_request_url", "title"} <= columns
    checked, offenders = 0, []
    for base in ("engine/boss_engine", "apps/api/app"):
        for path in sorted((ROOT / base).rglob("*.py")):
            for line, col in _selected_bare_columns(path):
                checked += 1
                if col not in columns and col != "*":
                    offenders.append(f"{path.relative_to(ROOT)}:~{line} selects {col!r}")
    assert checked > 20, "the scan found too few selects to be a real check"
    assert offenders == [], "\n".join(offenders)


def test_read_pull_request_reads_the_repo_out_of_the_spec():
    src = (ROOT / "engine/boss_engine/tools/builtin.py").read_text(encoding="utf-8")
    body = src[src.index("def read_pull_request("):]
    body = body[: body.index("\n    @reg.tool(")] if "\n    @reg.tool(" in body else body
    assert "spec->>'repo' AS repo" in body
    assert "spec->>'owner' AS owner" in body
