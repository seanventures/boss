"""A PROJECT THAT EXPLAINS ITSELF (2026-11-19). The operator: "in the Overview page of the project
we need to display the PRD overview and the link to the PRD — if I want to know what this project is
and what it built, today I need to navigate to lots of places to find the PRD and its description.
Also per work details should display what was discussed and what was implemented by the Agent — we
don't need to display full chat but an explanation about what was implemented."

Two reads, both from what BOSS already stores, never a model call:

* :func:`about` — the Overview's "About this app" card: the founding PRD's one-paragraph summary,
  its acceptance criteria (3–7) with met / partial from the latest acceptance check, who approved
  the spec and when, a link to the full spec (the meeting that wrote it), and the later specs. A
  project with no PRD shows its brief and says so.
* :func:`for_work` / :func:`for_build` — "What was discussed / What was built" for a work item or
  one build: the panel's DECISIONS (never the transcript) and the build's own outputs — what
  changed with the files grouped by purpose, tests added, the review's outcome, follow-ups left.

STORED ONCE. A build's explanation is worked out from its record when the build completes
(:func:`store_build`, from run_terminal.on_status_written when the status turns completed) and kept on
``code_task_runs.finalize_ctx.explained``; a panel's on ``meeting_runs.finalize_ctx.explained``. A
record made before this existed is explained on its first read and stored then, so a page view
never pays twice. Only what changes after completion (the merge) is read live.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import as_json, href, iso

logger = logging.getLogger(__name__)

#: Bump when the stored shape changes: an older stored explanation is worked out again.
VERSION = 1
MAX_CRITERIA = 7
MAX_BULLETS = 6
_CLIP = 170


# ── text helpers ─────────────────────────────────────────────────────────────────────────────

def plain(value: Any) -> str:
    """Markdown → one line of plain words: no bold, code ticks, links or table pipes."""
    s = str(value or "")
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\*\*|__|`", "", s)
    s = re.sub(r"(?<!\w)[*_](\S[^*_]*?)[*_](?!\w)", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def clip(value: Any, limit: int = _CLIP) -> str:
    """At most `limit` characters, cut at a sentence or word boundary, never mid-word."""
    s = plain(value)
    if len(s) <= limit:
        return s
    cut = s[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    if stop >= limit * 0.55:
        return cut[:stop + 1].rstrip(";") if cut[stop] == "." else cut[:stop] + "…"
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.5 else cut).rstrip(",;:—- ") + "…"


def _section(markdown: str, pattern: str) -> str:
    """The body under the first heading matching `pattern` (any level), up to the next heading
    of the same or a higher level."""
    md = str(markdown or "")
    m = re.search(rf"^(#{{1,4}})\s*(?:\d+[.)]\s*)?[^\n]*?(?:{pattern})[^\n]*$", md, re.I | re.M)
    if not m:
        return ""
    level = len(m.group(1))
    body = md[m.end():]
    nxt = re.search(rf"^#{{1,{level}}}\s", body, re.M)
    return (body[:nxt.start()] if nxt else body).strip()


def _paragraph(body: str) -> str:
    """The first paragraph of prose in `body` (metadata lines and rules skipped)."""
    para: list[str] = []
    for line in str(body or "").splitlines():
        ln = line.strip()
        if not ln:
            if para:
                break
            continue
        if ln.startswith(("#", "---", "|", "```", ">")) or re.match(r"^\*\*[^*]+:\*\*", ln):
            if para:
                break
            continue
        para.append(ln)
    return plain(" ".join(para))


def _items(body: str) -> list[str]:
    """Top-level list items (numbered, bulleted or `- [ ]`), each joined onto one line."""
    out: list[str] = []
    for line in str(body or "").splitlines():
        m = re.match(r"^(?:\d+[.)]|[-*+])\s+(?:\[[ xX]\]\s+)?(.*)$", line)
        if m:
            out.append(m.group(1).strip())
        elif out and line.startswith((" ", "\t")) and line.strip() and not re.match(r"^\s+(?:\d+[.)]|[-*+])\s", line):
            out[-1] += " " + line.strip()
    return [plain(x) for x in out if plain(x)]


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", plain(value).lower()).strip()


# ── the PRD ───────────────────────────────────────────────────────────────────────────────────

def prd_title(markdown: str, fallback: Any = "") -> str:
    m = re.search(r"^#\s+(.+)$", str(markdown or ""), re.M)
    raw = m.group(1) if m else str(fallback or "")
    return plain(re.sub(r"^\s*PRD\s*[:·—-]\s*", "", raw, flags=re.I)) or "Spec"


def prd_summary(markdown: str) -> str:
    """The PRD's one paragraph: its Summary (or Overview / Problem) section's first paragraph,
    else the first prose paragraph of the document."""
    for pattern in (r"\bsummary\b", r"\boverview\b", r"\bproblem\b"):
        para = _paragraph(_section(markdown, pattern))
        if para:
            return clip(para, 600)
    return clip(_paragraph(re.sub(r"^#[^\n]*\n", "", str(markdown or ""), count=1)), 600)


def prd_criteria(markdown: str) -> list[str]:
    """The PRD's acceptance criteria, as written (at most seven)."""
    return [clip(c, 220) for c in _items(_section(markdown, r"acceptance"))][:MAX_CRITERIA]


def _prd_decisions(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """The project's approved meeting PRDs, oldest first."""
    from .. import prd_record

    rows = connection.execute(text(
        "SELECT id, recommendation, approved_by, approved_at, resolution_note, created_at "
        "FROM decisions WHERE project_id = :p AND status IN ('approved', 'executed') "
        "AND recommendation->>'tool' = 'publish_document' ORDER BY created_at"),
        {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        rec = as_json(r["recommendation"], {}) or {}
        if not prd_record._is_prd_decision(rec):  # noqa: SLF001 — the one PRD rule
            continue
        md = str((rec.get("args") or {}).get("markdown") or "").strip()
        if md:
            out.append({**dict(r), "markdown": md, "args": rec.get("args") or {}})
    return out


def approved_words(connection: Connection, decision: Mapping[str, Any]) -> str:
    """Who approved the spec, in words: "Approved by your Easy Mode start", "Approved by Sam"."""
    by = str(decision.get("approved_by") or "")
    note = str(decision.get("resolution_note") or "")
    if note.lower().startswith("approved by your easy mode start"):
        return "Approved by your Easy Mode start"
    if by in ("auto_approval", "auto", "system", "boss"):
        return "Approved automatically"
    if not by:
        return "Approved"
    from ..people_names import who

    return f"Approved by {who(by, connection)}"


def _meeting_of(connection: Connection, decision_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, project_id, title, directive, role_keys, state, finalize_ctx, status "
        "FROM meeting_runs WHERE decision_id = :d ORDER BY updated_at DESC LIMIT 1"),
        {"d": decision_id}).mappings().first()
    return dict(row) if row else None


def _spec_href(project_id: str, meeting: Optional[Mapping[str, Any]], decision_id: str) -> str:
    if meeting:
        return href(project_id, area="meetings", meeting=str(meeting["id"]))
    return href(project_id, area="decisions", decision=decision_id)


def _latest_status(connection: Connection, project_id: str, criteria: list[str]
                   ) -> tuple[list[Optional[dict[str, Any]]], Optional[dict[str, Any]]]:
    """Each criterion's verdict from the newest acceptance check that judged it (matched on the
    criterion's words), and that check's when / where."""
    rows = connection.execute(text(
        "SELECT id, code_task_run_id, verdict, items, created_at FROM acceptance_checks "
        "WHERE project_id = :p ORDER BY created_at DESC LIMIT 20"), {"p": project_id}).mappings().all()
    wanted = [_norm(c) for c in criteria]
    found: list[Optional[dict[str, Any]]] = [None] * len(criteria)
    newest: Optional[dict[str, Any]] = None
    for row in rows:
        for item in as_json(row["items"], []) or []:
            if not isinstance(item, dict):
                continue
            words = _norm(item.get("text"))
            if not words:
                continue
            for i, want in enumerate(wanted):
                if found[i] is not None or not want:
                    continue
                if words == want or words.startswith(want[:80]) or want.startswith(words[:80]):
                    found[i] = {"status": str(item.get("status") or "unjudged"),
                                "evidence": clip(item.get("evidence"), 220) or None}
                    if newest is None:
                        newest = {"at": iso(row["created_at"]), "build_id": row["code_task_run_id"]}
        if all(f is not None for f in found):
            break
    return found, newest


_STATUS = {"met": "met", "partial": "partial", "missing": "not_met", "not_met": "not_met",
           "unmet": "not_met", "failed": "not_met"}


def about(connection: Connection, project_id: str) -> dict[str, Any]:
    """The Overview's "About this app" card (see the module docstring)."""
    project = connection.execute(text(
        "SELECT id, name, description FROM projects WHERE id = :p"), {"p": project_id}
    ).mappings().first()
    specs = _prd_decisions(connection, project_id)
    if not specs:
        brief = plain((project or {}).get("description"))
        return {"source": "brief", "title": (project or {}).get("name") or "",
                "summary": brief or None, "criteria": [], "approved": None, "spec_href": None,
                "note": ("No spec yet — this is the project brief." if brief
                         else "No spec yet. The first build's panel writes one."),
                "later": []}
    first = specs[0]
    meeting = _meeting_of(connection, str(first["id"]))
    criteria = prd_criteria(first["markdown"])
    verdicts, checked = _latest_status(connection, project_id, criteria) if criteria else ([], None)
    rows = [{"n": i + 1, "text": c,
             "status": _STATUS.get(str((v or {}).get("status")), "unchecked") if v else "unchecked",
             "evidence": (v or {}).get("evidence")} for i, (c, v) in enumerate(zip(criteria, verdicts))]
    later = []
    for spec in reversed(specs[1:]):
        m = _meeting_of(connection, str(spec["id"]))
        later.append({"title": prd_title(spec["markdown"], spec["args"].get("title")),
                      "approved_at": iso(spec["approved_at"]),
                      "href": _spec_href(project_id, m, str(spec["id"]))})
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("met", "partial", "not_met", "unchecked")}
    return {
        "source": "prd",
        "title": prd_title(first["markdown"], first["args"].get("title")),
        "summary": prd_summary(first["markdown"]) or None,
        "criteria": rows,
        "counts": counts,
        "checked_at": (checked or {}).get("at"),
        "approved": {"words": approved_words(connection, first), "at": iso(first["approved_at"])},
        "spec_href": _spec_href(project_id, meeting, str(first["id"])),
        "note": None,
        "later": later[:3],
        "later_count": len(later),
    }


# ── what was discussed ────────────────────────────────────────────────────────────────────────

def _decision_lines(body: str) -> list[str]:
    """The decisions a synthesis records: a table's `Decision | Resolution` rows, else its
    top-level bullets (sub-bullets and bold-only lead-ins skipped)."""
    out: list[str] = []
    for line in body.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
        if len(cells) >= 2 and not re.match(r"^:?-{2,}", cells[0]) and _norm(cells[0]) not in ("decision", "topic", "question"):
            what, how = plain(cells[0]), plain(cells[1])
            how = re.sub(r"^(?:resolved|decided|agreed)\s*[:.-]\s*", "", how, flags=re.I)
            out.append(f"{what}: {how}" if what else how)
    if out:
        return out
    for line in body.splitlines():
        m = re.match(r"^(?:\d+[.)]|[-*+])\s+(.*)$", line)
        if m and plain(m.group(1)):
            out.append(plain(m.group(1)))
    return out


def discussed_from(synthesis: str, prd_markdown: str = "") -> dict[str, Any]:
    """The panel's decisions and the questions it left, from its synthesis (or the PRD)."""
    body = _section(synthesis, r"decision")
    decisions = _decision_lines(body)
    if not decisions and prd_markdown:
        decisions = _decision_lines(_section(prd_markdown, r"decision"))
    asks = _items(_section(synthesis, r"open question")) or _items(_section(prd_markdown, r"open question"))
    dissent = ""
    m = re.search(r"\*\*Dissent[^*]*\*\*\s*:?\s*(.+)", body, re.I)
    if m:
        dissent = clip(m.group(1), 200)
    return {"decisions": [clip(d) for d in decisions][:4],
            "open_questions": [clip(q, 150) for q in asks][:2],
            "dissent": dissent or None}


def _stored(fctx: Any) -> Optional[dict[str, Any]]:
    got = (as_json(fctx, {}) or {}).get("explained")
    return got if isinstance(got, dict) and got.get("version") == VERSION else None


def _keep(connection: Connection, table: str, row_id: str, value: dict[str, Any]) -> None:
    """Store an explanation on its row; a read that cannot write still answers."""
    assert table in ("code_task_runs", "meeting_runs")
    try:
        with connection.begin_nested():
            connection.execute(text(
                f"UPDATE {table} SET finalize_ctx = COALESCE(finalize_ctx, '{{}}'::jsonb) || "  # noqa: S608 — fixed table
                "jsonb_build_object('explained', CAST(:v AS jsonb)) WHERE id = :id"),
                {"v": json.dumps(value), "id": row_id})
    except Exception:  # noqa: BLE001 — never fail a page on a cache write
        logger.warning("explained: could not store on %s %s", table, row_id, exc_info=True)


def discussed(connection: Connection, project_id: str, meeting_run_id: Optional[str]
              ) -> Optional[dict[str, Any]]:
    """What the panel decided for this work (stored on the meeting run once it completed)."""
    if not meeting_run_id:
        return None
    row = connection.execute(text(
        "SELECT id, project_id, title, role_keys, state, finalize_ctx, status, decision_id "
        "FROM meeting_runs WHERE id = :m AND project_id = :p"),
        {"m": str(meeting_run_id), "p": project_id}).mappings().first()
    if not row:
        return None
    kept = _stored(row["finalize_ctx"])
    if kept is None:
        state = as_json(row["state"], {}) or {}
        prd_md = str(state.get("prd_markdown") or "")
        if not prd_md and row["decision_id"]:
            prd_md = str(connection.execute(text(
                "SELECT recommendation->'args'->>'markdown' FROM decisions WHERE id = :d"),
                {"d": row["decision_id"]}).scalar() or "")
        kept = {"version": VERSION, **discussed_from(str(state.get("synthesis") or ""), prd_md),
                "seats": len(as_json(row["role_keys"], []) or [])}
        if row["status"] == "completed":
            _keep(connection, "meeting_runs", str(row["id"]), kept)
    return {**kept, "href": href(project_id, area="meetings", meeting=str(row["id"]))}


# ── what was built ────────────────────────────────────────────────────────────────────────────

#: A file's purpose, first match wins (the order matters: a test under public/ is a test).
_PURPOSES: tuple[tuple[str, str], ...] = (
    ("Tests", r"(^|/)(tests?|__tests__|spec|e2e)/|\.(test|spec)\.[a-z]+$|(^|/)test_[^/]+$|_test\.[a-z]+$"),
    ("Docs", r"\.(md|mdx|rst|txt)$|(^|/)docs?/"),
    ("Dependencies", r"(^|/)(package(-lock)?\.json|pnpm-lock\.yaml|yarn\.lock|requirements[^/]*\.txt|"
                     r"pyproject\.toml|poetry\.lock|go\.(mod|sum)|pom\.xml|build\.gradle[^/]*|Cargo\.(toml|lock)|Gemfile(\.lock)?)$"),
    ("Deploy setup", r"(^|/)(Dockerfile[^/]*|docker-compose[^/]*|compose\.ya?ml|Procfile|Makefile|\.dockerignore|"
                     r"boss\.[^/]+|\.github/.*|\.gitlab-ci\.yml|\.env[^/]*|k8s/.*|helm/.*|terraform/.*)$"),
    ("Database", r"(^|/)(migrations?|db|schema)/|\.sql$|(^|/)schema\.prisma$"),
    ("Data", r"(^|/)(data|fixtures|seeds?)/|\.(geojson|csv|tsv|ndjson)$"),
    ("Styles", r"\.(css|scss|sass|less|styl)$|(^|/)style\.json$|tailwind\.config"),
    ("Screens", r"(^|/)(public|static|web|frontend|client|src/(components|pages|app|views)|components|pages|views)/"
                r"|\.(tsx|jsx|vue|svelte|html?)$"),
)


def purpose_of(path: str) -> str:
    for name, pattern in _PURPOSES:
        if re.search(pattern, path, re.I):
            return name
    return "Server code" if re.search(r"\.(js|mjs|cjs|ts|py|go|java|kt|rb|rs|php|cs)$", path, re.I) else "Other files"


def group_files(paths: Iterable[str]) -> list[dict[str, Any]]:
    """Files grouped by purpose, biggest group first (ties in a fixed reading order)."""
    order = [p for p, _ in _PURPOSES] + ["Server code", "Other files"]
    groups: dict[str, list[str]] = {}
    for p in dict.fromkeys(str(x) for x in paths if x):
        groups.setdefault(purpose_of(p), []).append(p)
    lead = ("Server code", "Screens", "Styles", "Data", "Database")
    return sorted(({"purpose": k, "files": v, "count": len(v)} for k, v in groups.items()),
                  key=lambda g: (g["purpose"] not in lead, -g["count"], order.index(g["purpose"])))


def _highlights(summary: str) -> list[str]:
    """What the builder says it did: its bullets ("**Server**: …" → "Server: …"), else its
    sentences — never the "All 15 tests pass" line (the tests bullet says that)."""
    lines = _items(summary)
    if not lines:
        lines = [s for s in re.split(r"(?<=[.!])\s+", plain(summary)) if s]
    skip = re.compile(r"^(all\s+)?\d*\s*(qa-authored\s+)?tests?\s+(all\s+)?pass|summary of (the )?implementation|^ITEM\s", re.I)
    out = []
    for ln in lines:
        ln = re.sub(r"^\s*(all\s+\d+\s+[\w-]*\s*tests?\s+pass[^.]*\.)\s*", "", ln, flags=re.I)
        if ln and not skip.search(ln) and len(ln) > 12:
            one = clip(ln, 150)
            out.append(one if re.search(r"[.!?)…]$", one) else one + "…")
    return out[:2]


def compose(highlights: list[str], facts: list[str], tail: Optional[str] = None) -> list[str]:
    """3–6 bullets: the facts (files, tests, review, follow-ups, the merge) always; what the
    builder says it did fills what room is left, first."""
    end = facts + ([tail] if tail else [])
    room = max(MAX_BULLETS - len(end), 0)
    return (highlights[:room] + end)[:MAX_BULLETS]


def _test_count(*texts: Any) -> Optional[int]:
    for t in texts:
        m = re.search(r"\b(\d+)\s+(?:[\w-]+\s+){0,2}tests?\b[^.\n]{0,20}\bpass", str(t or ""), re.I)
        if m:
            return int(m.group(1))
    return None


def _findings_raised(rounds: list[Any]) -> int:
    seen = set()
    for r in rounds:
        for seat in ((r or {}).get("seats") or {}).values():
            for m in re.finditer(r"^FINDING:\s*(\w+)\s+(.{0,80})", str((seat or {}).get("summary") or ""), re.M):
                seen.add(_norm(m.group(2)))
    return len(seen)


def built_from(state: Mapping[str, Any], fctx: Mapping[str, Any],
               check: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """A build's explanation from its own record (state, finalize_ctx, its acceptance check)."""
    impl = [str(x) for x in state.get("impl_files") or [] if x]
    tests = [str(x) for x in state.get("test_files") or [] if x]
    groups = group_files(impl + tests)
    files_n = state.get("files_changed") if isinstance(state.get("files_changed"), int) else len(impl) + len(tests)
    rounds = fctx.get("review_rounds") if isinstance(fctx.get("review_rounds"), list) else []
    deferred = [d for d in state.get("deferred_findings") or [] if isinstance(d, dict) and d.get("text")]
    passed = state.get("tests_passed")
    n_tests = _test_count(state.get("impl_summary"), state.get("qa_summary"))
    raised = _findings_raised(rounds)

    facts: list[str] = []
    if groups:
        parts = ", ".join(f"{g['purpose'].lower()} {g['count']}" for g in groups[:4])
        more = f", +{len(groups) - 4} more areas" if len(groups) > 4 else ""
        facts.append(f"Changed {files_n} file{'s' if files_n != 1 else ''}: {parts}{more}")
    t_bits = []
    if tests:
        t_bits.append(f"{len(tests)} test file{'s' if len(tests) != 1 else ''} added or updated")
    if passed is True:
        t_bits.append(f"all {n_tests} tests pass" if n_tests else "tests pass")
    elif passed is False:
        t_bits.append("tests were failing at the end")
    if t_bits:
        facts.append("Tests: " + ", ".join(t_bits))
    r_bits = []
    if rounds:
        r_bits.append(f"{len(rounds)} review round{'s' if len(rounds) != 1 else ''}")
    if raised:
        r_bits.append(f"{raised} finding{'s' if raised != 1 else ''} raised")
    counts = (check or {}).get("counts") if isinstance((check or {}).get("counts"), dict) else None
    if counts:
        total = sum(int(counts.get(k) or 0) for k in ("met", "partial", "missing", "unjudged"))
        if total:
            r_bits.append(f"{int(counts.get('met') or 0)} of {total} acceptance criteria met"
                          + (f", {int(counts['partial'])} partly" if counts.get("partial") else ""))
    if r_bits:
        facts.append("Review: " + ", ".join(r_bits))
    follow = [clip(re.sub(r"^(F-\d+\s*)?\[?(BLOCKER|MAJOR|MINOR|NIT)\]?\s*[:—-]?\s*", "",
                          str(d["text"]), flags=re.I), 100).rstrip(".")
              for d in deferred]
    if follow:
        facts.append(f"Left as follow-up{'s' if len(follow) != 1 else ''}: " + "; ".join(follow[:2])
                     + (f"; +{len(follow) - 2} more" if len(follow) > 2 else ""))
    highlights = _highlights(str(state.get("impl_summary") or ""))
    bullets = compose(highlights, facts)
    return {"version": VERSION, "bullets": bullets, "highlights": highlights, "facts": facts,
            "files_changed": files_n, "groups": groups,
            "tests": {"files": tests, "passed": passed, "count": n_tests},
            "review": {"rounds": len(rounds), "raised": raised},
            "acceptance": counts, "follow_ups": follow}


def _check_of(connection: Connection, run_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT verdict, counts, items FROM acceptance_checks WHERE code_task_run_id = :r "
        "ORDER BY created_at DESC LIMIT 1"), {"r": run_id}).mappings().first()
    return ({"verdict": row["verdict"], "counts": as_json(row["counts"], {}),
             "items": as_json(row["items"], [])} if row else None)


def acceptance_items(check: Optional[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """THE MUST-HAVES, EACH WITH ITS VERDICT (the Work panel's Asked column): the newest
    acceptance check's items as {n, text, verdict, evidence} — met | partial | not_met |
    unchecked — read from the check, never parsed out of "Must-have N (P1) partial: …" lines."""
    out = []
    for i, item in enumerate((check or {}).get("items") or []):
        if not isinstance(item, Mapping) or not plain(item.get("text")):
            continue
        status = str(item.get("status") or "")
        out.append({"n": int(item.get("n") or i + 1), "text": clip(item.get("text"), 220),
                    "verdict": _STATUS.get(status, "unchecked"),
                    "evidence": clip(item.get("evidence"), 220) or None})
    return out[:MAX_CRITERIA * 2]


# ── review comments, deduped ─────────────────────────────────────────────────────────────────

_GATE = re.compile(r"^Failing gate '([^']+)'\s*(?:\(([^)]+)\))?", re.I)
_MUST = re.compile(r"^Must-have\s+(\d+)\s*(?:\((P\d)\))?\s*(partial|missing|not met|unmet)?\s*:?\s*(.*)$",
                   re.I | re.S)
_AT = re.compile(r"^([\w./@-]+\.[A-Za-z0-9]+):(\d+)\s*[—–-]\s*(.*)$", re.S)
_SEV = {"blocker": "P1", "critical": "P1", "major": "P1", "high": "P1", "minor": "P2",
        "medium": "P2", "low": "P3", "nit": "P3", "finding": "P2"}


def review_comments(deferred: Iterable[Any], extra: Iterable[Any] = (), *,
                    must_haves_shown: bool = True) -> dict[str, Any]:
    """THE REVIEW'S COMMENTS, ONCE EACH (the Work panel): a build's deferred findings plus the
    merge card's follow-up items, deduped — a failing gate once however often it repeats ("2
    findings merged"), one comment per file and line, the same words once. "Must-have N partial"
    lines are the Asked column's meter (`must_haves_shown`), counted but not listed again.
    Each: {key, text, severity (gate | P1 | P2 | P3), source, path, line, merged}. Sorted gate →
    P1 → P2 → P3. Returns {items, raw}: `raw` is how many came in."""
    found: dict[str, dict[str, Any]] = {}
    raw = 0
    for entry in list(deferred or []) + list(extra or []):
        if not isinstance(entry, Mapping):
            continue
        words = str(entry.get("text") or "").strip()
        if not words:
            continue
        raw += 1
        sev_word = str(entry.get("severity") or entry.get("tier") or "").lower()
        severity = (str(entry.get("tier")).upper() if str(entry.get("tier") or "").upper()
                    in ("P1", "P2", "P3") else _SEV.get(sev_word, "P2"))
        path, line = entry.get("path"), entry.get("line")
        source = "review"
        if words.startswith("(") and ")" in words[:40]:
            source = words[1:words.index(")")]
            words = words[words.index(")") + 1:].strip()
            words = re.sub(r"\s*\n+\s*[A-Z ]+:\s*(YES|NO)\s*$", "", words)
        gate = _GATE.match(words)
        must = _MUST.match(words)
        at = _AT.match(words) if not path else None
        if at:
            path, line, words = at.group(1), int(at.group(2)), at.group(3)
        if gate:
            key = f"gate:{gate.group(1)}"
            name = gate.group(2) or gate.group(1).replace("_", " ")
            text_ = f"\u201c{name}\u201d check failed when the change opened"
            severity, source = "gate", gate.group(1)
        elif must:
            if must_haves_shown:
                continue
            key = f"must:{must.group(1)}"
            text_ = f"Must-have {must.group(1)} {(must.group(3) or 'partial').lower()}: {plain(must.group(4))}"
        elif path:
            key = f"at:{path}:{line or ''}"
            text_ = plain(words)
        else:
            key = "said:" + _norm(words)[:120]
            text_ = plain(words)
        if key in found:
            found[key]["merged"] += 1
            continue
        found[key] = {"key": key, "text": clip(text_, 240), "severity": severity,
                      "source": source, "path": str(path) if path else None,
                      "line": int(line) if isinstance(line, int) or str(line or "").isdigit() else None,
                      "merged": 1}
    order = {"gate": 0, "P1": 1, "P2": 2, "P3": 3}
    items = sorted(found.values(), key=lambda c: order.get(c["severity"], 4))
    return {"items": items, "raw": raw}


#: Tooling words that make a poor "what was built" line ("playwright devDependency was pinned…").
_NOISE = re.compile(r"devDependenc|lockfile|lock file|pinned|sandbox|npm install|cache|revision|"
                    r"node_modules|chromium|ci config|eslint", re.I)


def summary_of(built: Mapping[str, Any], prd_markdown: str = "") -> Optional[str]:
    """ONE PLAIN SENTENCE ABOUT THE USER-VISIBLE CHANGE (the Work panel's Built column), from
    what is stored: the PRD's first goal (what the person sees change), else the builder's first
    highlight that is not tooling noise. Never a model call; None when nothing says it."""
    goals = _items(_section(prd_markdown, r"\bgoals?\b")) if prd_markdown else []
    lead = next((g for g in goals if not re.match(r"^(no|not)\b", g, re.I)), "")
    if not lead:
        lead = next((h for h in built.get("highlights") or [] if not _NOISE.search(str(h))), "")
    if not lead:
        return None
    one = clip(lead, 160).rstrip(".…")
    tests = built.get("tests") if isinstance(built.get("tests"), Mapping) else {}
    tail = (" Tests pass." if tests.get("passed") is True else
            " Tests were failing at the end." if tests.get("passed") is False else "")
    return one[:1].upper() + one[1:] + "." + tail


def _merge_words(fctx: Mapping[str, Any], number: Any, status: str) -> Optional[str]:
    merge = fctx.get("merge") if isinstance(fctx.get("merge"), dict) else {}
    ref = f"change #{number}" if number else "the change"
    if merge.get("sha"):
        return f"Merged as {ref}"
    if status == "completed" and number:
        return f"Opened as {ref}, waiting on a merge"
    return None


def build(connection: Connection, run: Mapping[str, Any], *, store: bool = True) -> Optional[dict[str, Any]]:
    """One build's explanation: stored when it completed, else worked out (and stored once it
    is complete). A build still running is explained so far and not stored."""
    status = str(run.get("status") or "")
    fctx = as_json(run.get("finalize_ctx"), {}) or {}
    kept = _stored(fctx)
    if kept is None:
        state = as_json(run.get("state"), {}) or {}
        kept = built_from(state, fctx, _check_of(connection, str(run["id"])))
        if store and status == "completed":
            _keep(connection, "code_task_runs", str(run["id"]), kept)
    merged = _merge_words(fctx, run.get("pull_request_number"), status)
    bullets = compose(list(kept.get("highlights") or []), list(kept.get("facts") or []), merged)
    check = _check_of(connection, str(run["id"]))
    items = acceptance_items(check)
    counts = dict(kept.get("acceptance") or (check or {}).get("counts") or {})
    if items:
        counts = {**counts, "items": items}
    return {**kept, "bullets": bullets, "build_id": str(run["id"]), "status": status,
            "title": run.get("title"), "number": run.get("pull_request_number"),
            "acceptance": counts or None}


def store_build(connection: Connection, run_id: str) -> None:
    """Called when a build's status turns `completed` (run_terminal.on_status_written, whichever
    path wrote it): work its explanation out once and keep it.
    Never raises — a completion never fails on its explanation."""
    try:
        with connection.begin_nested():
            row = connection.execute(text(
                "SELECT id, status, title, state, finalize_ctx, pull_request_number FROM code_task_runs "
                "WHERE id = :r"), {"r": run_id}).mappings().first()
            if row and _stored(row["finalize_ctx"]) is None:
                build(connection, dict(row))
    except Exception:  # noqa: BLE001
        logger.warning("explained: completion store failed for %s", run_id, exc_info=True)


def _meeting_for_build(connection: Connection, project_id: str, run: Mapping[str, Any]) -> Optional[str]:
    from .. import approved_prd_design

    fctx = as_json(run.get("finalize_ctx"), {}) or {}
    wfr = fctx.get("workflow_run_id")
    if wfr:
        rows = connection.execute(text(
            "SELECT output FROM workflow_step_runs WHERE workflow_run_id = :r "
            "ORDER BY created_at DESC"), {"r": str(wfr)}).all()
        for (output,) in rows:
            mid = (as_json(output, {}) or {}).get("meeting_run_id")
            if mid:
                return str(mid)
    found = approved_prd_design.find_approved_prd(connection, project_id, fctx)
    if found and found.get("decision_id"):
        return connection.execute(text(
            "SELECT id FROM meeting_runs WHERE decision_id = :d ORDER BY updated_at DESC LIMIT 1"),
            {"d": found["decision_id"]}).scalar()
    return None


_RUN_COLS = "id, status, title, state, finalize_ctx, pull_request_url, pull_request_number, created_at"


def for_build(connection: Connection, project_id: str, run_id: str) -> dict[str, Any]:
    """The build page's "What was discussed / What was built"."""
    run = connection.execute(text(
        f"SELECT {_RUN_COLS} FROM code_task_runs WHERE id = :r AND project_id = :p"),  # noqa: S608
        {"r": run_id, "p": project_id}).mappings().first()
    if not run:
        raise LookupError(f"There is no build {run_id} in this project.")
    run = dict(run)
    return {"discussed": discussed(connection, project_id, _meeting_for_build(connection, project_id, run)),
            "built": build(connection, run), "later": []}


def main_build(builds: list[Mapping[str, Any]], merge_pr: Optional[Mapping[str, Any]] = None
               ) -> Optional[Mapping[str, Any]]:
    """THE BUILD "WHAT WAS BUILT" READS (park-bench-map 2026-11-19: the drawer read the running
    follow-up, whose record is empty, and showed nothing). The build that opened the change
    waiting on a merge (`merge_pr.build_id`), else the newest COMPLETED build with that change
    number, else the merged one, else the newest completed, else the newest."""
    if not builds:
        return None
    mp = merge_pr or {}
    if mp.get("build_id"):
        hit = next((b for b in builds if str(b["id"]) == str(mp["build_id"])), None)
        if hit is not None:
            return hit
    done = [b for b in builds if str(b.get("status") or "") == "completed"]
    if mp.get("number"):
        hit = next((b for b in reversed(done)
                    if str(b.get("pull_request_number") or "") == str(mp["number"])), None)
        if hit is not None:
            return hit
    merged = next((b for b in builds
                   if ((as_json(b.get("finalize_ctx"), {}) or {}).get("merge") or {}).get("sha")), None)
    return merged or (done[-1] if done else builds[-1])


def for_work(connection: Connection, project_id: str, *, meeting_run_id: Optional[str],
             builds: list[Mapping[str, Any]], merge_pr: Optional[Mapping[str, Any]] = None,
             prd_markdown: str = "") -> Optional[dict[str, Any]]:
    """A work item's: the panel's decisions, and the build that delivered it (main_build: the
    one that opened the change waiting on a merge, else the merged one, else the newest
    completed), with each later build named in one line, a one-sentence summary and the must-
    haves with their verdicts."""
    if not builds and not meeting_run_id:
        return None
    if not meeting_run_id and builds:
        meeting_run_id = _meeting_for_build(connection, project_id, builds[-1])
    main = main_build(builds, merge_pr)
    later = []
    for b in builds:
        if main is None or b["id"] == main["id"] or str(b.get("status")) != "completed":
            continue
        if b.get("created_at") and main.get("created_at") and b["created_at"] < main["created_at"]:
            continue
        one = build(connection, b)
        later.append({"build_id": str(b["id"]), "title": b.get("title"), "number": b.get("pull_request_number"),
                      "line": next(iter(one.get("bullets") or []), "") if one else ""})
    built = build(connection, main) if main is not None else None
    if built is not None:
        built["summary"] = summary_of(built, prd_markdown)
    return {"discussed": discussed(connection, project_id, meeting_run_id),
            "built": built, "later": later[:3]}


__all__ = ["VERSION", "about", "acceptance_items", "build", "built_from", "main_build",
           "review_comments", "summary_of", "clip", "discussed", "discussed_from",
           "for_build", "for_work", "group_files", "plain", "prd_criteria", "prd_summary",
           "prd_title", "purpose_of", "store_build"]
