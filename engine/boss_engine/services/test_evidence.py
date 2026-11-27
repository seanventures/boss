"""Structured test evidence for pull requests, console downloads, and GitHub check runs (BOSSAPP-1011).

Every BOSS-opened pull request or change set must end with a short ``### Test evidence`` section:
  - suites run, counts, pass/fail;
  - environment (substrate, cwd, verified commit);
  - what was deliberately not run;
  - links to full per-case results (console download, plus GitHub Check Run when connected to GitHub).

Local hosts publish download-only (never mentioning GitHub). GitHub-connected hosts also publish
the full per-case output as a native Check Run on the head SHA so the repository stays clean.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

SECTION_HEADING = "### Test evidence"
_SECTION_RE = re.compile(r"(?im)^#{2,3}\s+Test evidence\s*$")
_ANY_HEADING_RE = re.compile(r"(?m)^#{1,3}\s+\S")
_TRAILER_LINE_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9-]*:\s+\S.*|<!--.*?-->|\s*)$")

# Max bytes/chars GitHub Check Runs API accepts in output.text (65535 hard limit).
CHECK_RUN_TEXT_LIMIT = 64_000
MAX_STORED_CASES = 2000


class MissingTestEvidenceError(ValueError):
    """Raised when a factory pull request attempts to open without a Test evidence section."""

    code = "missing_test_evidence"


def _scrub(text: str) -> str:
    if not text:
        return ""
    try:
        from .run_secret_redaction import scrub

        return str(scrub(text))
    except Exception:  # noqa: BLE001 — redaction never drops evidence
        return str(text)


# ── per-case output parser ────────────────────────────────────────────────────────────────────

_UNITTEST_CASE_RE = re.compile(
    r"^(\S+(?:\s+\([^)]+\))?)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped(?:\s+.*)?|expected failure|unexpected success)\s*$"
)
_UNITTEST_RAN_RE = re.compile(r"^Ran\s+(\d+)\s+tests?\s+in\s+", re.MULTILINE)
_UNITTEST_FAILED_RE = re.compile(r"^FAILED\s+\(([^)]+)\)", re.MULTILINE)
_UNITTEST_OK_SKIP_RE = re.compile(r"^OK\s+\(skipped=(\d+)\)", re.MULTILINE)

_PYTEST_CASE_RE = re.compile(
    r"^([^\s]+::[^\s]+)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b(.*)$"
)
_PYTEST_SUMMARY_RE = re.compile(
    r"(?i)=+\s*([^=\n]*?\b(?:passed|failed|error|errors|skipped)\b[^=\n]*?)\s*=+"
)

_NODE_CASE_RE = re.compile(r"^\s*([✔✖✓×-])\s+(.+?)(?:\s+\(\d+(?:\.\d+)?m?s\))?\s*$")
_NODE_STAT_RE = re.compile(r"^\s*(?:ℹ|#)\s*(tests|pass|fail|skipped|cancelled|todo)\s+(\d+)\s*$", re.MULTILINE)

_GO_CASE_RE = re.compile(r"^---\s+(PASS|FAIL|SKIP):\s+(\S+)\s+\([^)]*\)\s*$")

_VITEST_TESTS_RE = re.compile(r"Tests\s+(.+)$", re.MULTILINE)


def parse_case_results(output: str) -> dict[str, Any]:
    """Deterministically extract per-case results and counts from test runner stdout/stderr."""
    raw = str(output or "")
    cases: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_case(name: str, status: str, detail: str = "") -> None:
        if len(cases) >= MAX_STORED_CASES:
            return
        clean = name.strip()[:300]
        if not clean:
            return
        key = (clean, status)
        if key in seen:
            return
        seen.add(key)
        entry: dict[str, str] = {"name": clean, "status": status}
        if detail.strip():
            entry["detail"] = detail.strip()[:300]
        cases.append(entry)

    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        m = _PYTEST_CASE_RE.match(s)
        if m:
            st_raw = m.group(2).upper()
            st = ("passed" if st_raw in ("PASSED", "XPASS")
                  else "skipped" if st_raw in ("SKIPPED", "XFAIL")
                  else "failed")
            add_case(m.group(1), st, m.group(3))
            continue
        m = _UNITTEST_CASE_RE.match(s)
        if m:
            st_raw = m.group(2).lower()
            st = ("passed" if st_raw == "ok"
                  else "skipped" if st_raw.startswith("skipped") or st_raw == "expected failure"
                  else "failed")
            add_case(m.group(1), st, m.group(2) if st != "passed" else "")
            continue
        m = _GO_CASE_RE.match(s)
        if m:
            st_raw = m.group(1).upper()
            st = "passed" if st_raw == "PASS" else "skipped" if st_raw == "SKIP" else "failed"
            add_case(m.group(2), st)
            continue
        m = _NODE_CASE_RE.match(line)
        if m and not s.startswith("- "):
            sym, name = m.group(1), m.group(2).strip()
            if name and not name.lower().startswith(("failing tests", "tests ", "suites ")):
                st = "passed" if sym in ("✔", "✓") else "failed" if sym in ("✖", "×") else "skipped"
                add_case(name, st)

    passed = sum(1 for c in cases if c["status"] == "passed")
    failed = sum(1 for c in cases if c["status"] == "failed")
    skipped = sum(1 for c in cases if c["status"] == "skipped")

    # Summary lines catch totals even when runners run non-verbosely (e.g. `pytest -q`, `unittest`).
    node_stats = {k.lower(): int(v) for k, v in _NODE_STAT_RE.findall(raw)}
    if node_stats.get("tests"):
        passed = max(passed, node_stats.get("pass", 0))
        failed = max(failed, node_stats.get("fail", 0))
        skipped = max(skipped, node_stats.get("skipped", 0) + node_stats.get("todo", 0))

    for m in _PYTEST_SUMMARY_RE.finditer(raw):
        chunk = m.group(1)
        p_m = re.search(r"(\d+)\s+passed", chunk, re.I)
        f_m = re.search(r"(\d+)\s+failed", chunk, re.I)
        e_m = re.search(r"(\d+)\s+errors?", chunk, re.I)
        s_m = re.search(r"(\d+)\s+skipped", chunk, re.I)
        if p_m or f_m or e_m or s_m:
            passed = max(passed, int(p_m.group(1)) if p_m else 0)
            failed = max(failed, (int(f_m.group(1)) if f_m else 0) + (int(e_m.group(1)) if e_m else 0))
            skipped = max(skipped, int(s_m.group(1)) if s_m else 0)

    for m in _VITEST_TESTS_RE.finditer(raw):
        chunk = m.group(1)
        p_m = re.search(r"(\d+)\s+passed", chunk, re.I)
        f_m = re.search(r"(\d+)\s+failed", chunk, re.I)
        s_m = re.search(r"(\d+)\s+skipped", chunk, re.I)
        if p_m or f_m or s_m:
            passed = max(passed, int(p_m.group(1)) if p_m else 0)
            failed = max(failed, int(f_m.group(1)) if f_m else 0)
            skipped = max(skipped, int(s_m.group(1)) if s_m else 0)

    ran_m = _UNITTEST_RAN_RE.search(raw)
    if ran_m:
        ran_total = int(ran_m.group(1))
        fail_m = _UNITTEST_FAILED_RE.search(raw)
        if fail_m:
            parts = fail_m.group(1)
            f_n = int(m.group(1)) if (m := re.search(r"failures=(\d+)", parts)) else 0
            e_n = int(m.group(1)) if (m := re.search(r"errors=(\d+)", parts)) else 0
            s_n = int(m.group(1)) if (m := re.search(r"skipped=(\d+)", parts)) else 0
            failed = max(failed, f_n + e_n)
            skipped = max(skipped, s_n)
            passed = max(passed, max(0, ran_total - failed - skipped))
        else:
            ok_skip_m = _UNITTEST_OK_SKIP_RE.search(raw)
            s_n = int(ok_skip_m.group(1)) if ok_skip_m else 0
            skipped = max(skipped, s_n)
            if re.search(r"^OK\b", raw, re.MULTILINE):
                passed = max(passed, max(0, ran_total - skipped))

    total = passed + failed + skipped
    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "cases": cases,
    }


def _collect_suites(
    evidence: Mapping[str, Any],
    local_validation: Optional[Mapping[str, Any]],
    checks: Optional[Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    suites: list[dict[str, Any]] = []
    seen_cmds: set[str] = set()

    def push(cmd: str, ok: Optional[bool], exit_code: Optional[int] = None, source: str = "boss") -> None:
        clean = str(cmd or "").strip()
        if not clean:
            return
        key = clean.lower()
        if key in seen_cmds:
            return
        seen_cmds.add(key)
        suites.append({
            "command": clean[:240],
            "ok": ok,
            "exit_code": exit_code,
            "source": source,
        })

    raw_cmds = evidence.get("test_commands") or evidence.get("tests") or []
    if isinstance(raw_cmds, list):
        for item in raw_cmds:
            if isinstance(item, Mapping):
                cmd = str(item.get("command") or item.get("name") or "")
                ok = item.get("ok") if "ok" in item else (
                    item.get("exit_code") == 0 if item.get("exit_code") is not None else None
                )
                ec = item.get("exit_code")
                push(cmd, bool(ok) if ok is not None else None,
                     int(ec) if isinstance(ec, int) else None, source="boss")
            elif isinstance(item, str):
                push(item, evidence.get("tests_passed") if isinstance(evidence.get("tests_passed"), bool) else None)

    single = evidence.get("test_cmd") or evidence.get("test_command")
    if single and not suites:
        tp = evidence.get("tests_passed")
        ec = evidence.get("test_exit_code")
        push(str(single), bool(tp) if isinstance(tp, bool) else None,
             int(ec) if isinstance(ec, int) else None, source="boss")

    if isinstance(local_validation, Mapping):
        for item in local_validation.get("commands") or []:
            if isinstance(item, Mapping):
                cmd = str(item.get("command") or "")
                ec = item.get("exit_code")
                ok = item.get("ok") if "ok" in item else (ec == 0 if ec is not None else None)
                push(cmd, bool(ok) if ok is not None else None,
                     int(ec) if isinstance(ec, int) else None, source="sandbox_validation")

    for chk in checks or ():
        if isinstance(chk, Mapping) and chk.get("name"):
            concl = str(chk.get("conclusion") or "").lower()
            st = str(chk.get("status") or "").lower()
            ok = True if concl in ("success", "neutral", "skipped") else (False if st == "completed" and concl else None)
            push(str(chk["name"]), ok, source="ci_check")

    return suites


def build_evidence_record(
    *,
    evidence: Optional[Mapping[str, Any]] = None,
    gates: Optional[Mapping[str, Any]] = None,
    local_validation: Optional[Mapping[str, Any]] = None,
    checks: Optional[Sequence[Mapping[str, Any]]] = None,
    host_kind: str = "local",
    head_sha: Optional[str] = None,
    artifact_id: Optional[str] = None,
    download_url: Optional[str] = None,
    check_run_url: Optional[str] = None,
    extra_not_run: Iterable[str] = (),
    raw_output_override: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble one canonical structured test-evidence record from run/validation data."""
    ev = dict(evidence or {})
    g = dict(gates or {})
    host = "local" if str(host_kind or "").lower() == "local" else "github"
    suites = _collect_suites(ev, local_validation, checks)

    output_parts: list[str] = []
    if raw_output_override:
        output_parts.append(str(raw_output_override))
    elif ev.get("last_verify_output"):
        output_parts.append(str(ev["last_verify_output"]))
    if isinstance(local_validation, Mapping):
        for cmd in local_validation.get("commands") or []:
            if isinstance(cmd, Mapping):
                chunk = "\n".join(
                    p for p in (str(cmd.get("stdout") or ""), str(cmd.get("stderr") or "")) if p.strip()
                )
                if chunk:
                    output_parts.append(f"$ {cmd.get('command', '')}\n{chunk}")
    raw_output = _scrub("\n\n".join(p for p in output_parts if p.strip()))
    parsed = parse_case_results(raw_output)

    suites_passed = sum(1 for s in suites if s.get("ok") is True)
    suites_failed = sum(1 for s in suites if s.get("ok") is False)
    suites_run = len(suites)

    if suites_failed > 0 or parsed["failed"] > 0 or ev.get("tests_passed") is False or g.get("written_tests_pass") is False:
        status = "failed"
    elif suites_passed > 0 or parsed["passed"] > 0 or ev.get("tests_passed") is True or g.get("written_tests_pass") is True:
        status = "passed"
    else:
        status = "not_run"

    # Environment summary — never mention GitHub on a local host.
    env: dict[str, Any] = {
        "substrate": str(ev.get("substrate") or ("local" if host == "local" else "sandbox")),
        "host": "local repository" if host == "local" else "GitHub",
    }
    if ev.get("verify_cwd"):
        env["cwd"] = str(ev["verify_cwd"])
    if ev.get("engine"):
        env["engine"] = str(ev["engine"])
    effective_head = str(head_sha or ev.get("verified_commit_sha") or ev.get("head_sha") or "").strip()
    if effective_head:
        env["head_sha"] = effective_head

    # What was deliberately not run.
    not_run: list[str] = []
    skipped_gates = ev.get("gates_skipped")
    if isinstance(skipped_gates, Mapping):
        for gate_name, reason in skipped_gates.items():
            not_run.append(f"{gate_name}: {reason}")
    app_serves = ev.get("app_serves")
    if isinstance(app_serves, Mapping) and app_serves.get("skipped"):
        not_run.append(f"app_serves: {app_serves['skipped']}")
    if not suites:
        not_run.append("No automated test suite was executed for this change")
    if host == "local":
        not_run.append("External hosted CI (local repository host)")
    for item in extra_not_run or ():
        text_item = str(item or "").strip()
        if text_item and text_item not in not_run:
            not_run.append(text_item)

    dl = download_url or (f"/v1/artifacts/{artifact_id}/download" if artifact_id else None)
    # Local host = console download only; never attach a GitHub check run link.
    cr_url = str(check_run_url).strip() if (host != "local" and check_run_url) else None

    return {
        "status": status,
        "suites": suites,
        "counts": {
            "suites_run": suites_run,
            "suites_passed": suites_passed,
            "suites_failed": suites_failed,
            "cases_passed": parsed["passed"],
            "cases_failed": parsed["failed"],
            "cases_skipped": parsed["skipped"],
            "cases_total": parsed["total"],
        },
        "cases": parsed["cases"],
        "environment": env,
        "not_run": not_run,
        "head_sha": effective_head or None,
        "artifact_id": artifact_id,
        "download_url": dl,
        "check_run_url": cr_url,
        "host_kind": host,
        "raw_output": raw_output,
    }


def render_pr_section(record: Mapping[str, Any]) -> str:
    """Render the brief trailing ``### Test evidence`` section for a PR/change body."""
    status = str(record.get("status") or "not_run")
    counts = record.get("counts") if isinstance(record.get("counts"), Mapping) else {}
    suites = record.get("suites") if isinstance(record.get("suites"), list) else []
    env = record.get("environment") if isinstance(record.get("environment"), Mapping) else {}
    not_run = record.get("not_run") if isinstance(record.get("not_run"), list) else []
    host = "local" if str(record.get("host_kind") or "").lower() == "local" else "github"
    dl = str(record.get("download_url") or "").strip()
    cr = str(record.get("check_run_url") or "").strip() if host != "local" else ""

    sr = int(counts.get("suites_run") or len(suites))
    sp = int(counts.get("suites_passed") or 0)
    sf = int(counts.get("suites_failed") or 0)
    cp = int(counts.get("cases_passed") or 0)
    cf = int(counts.get("cases_failed") or 0)
    cs = int(counts.get("cases_skipped") or 0)
    ct = int(counts.get("cases_total") or (cp + cf + cs))

    status_badge = "✅ Passed" if status == "passed" else ("❌ Failed" if status == "failed" else "⏭ Not run")
    count_bits = [f"{sr} suite{'s' if sr != 1 else ''} ({sp} passed, {sf} failed)"]
    if ct > 0:
        count_bits.append(f"{ct} case{'s' if ct != 1 else ''} ({cp} passed, {cf} failed, {cs} skipped)")

    lines = [SECTION_HEADING, f"- **Result:** {status_badge} — {'; '.join(count_bits)}"]

    if suites:
        suite_strs = []
        for s in suites[:8]:
            if not isinstance(s, Mapping):
                continue
            mark = "✅" if s.get("ok") is True else ("❌" if s.get("ok") is False else "•")
            suite_strs.append(f"{mark} `{str(s.get('command') or '')[:140]}`")
        if len(suites) > 8:
            suite_strs.append(f"… +{len(suites) - 8} more")
        lines.append("- **Suites:** " + ", ".join(suite_strs))
    else:
        lines.append("- **Suites:** None recorded.")

    env_bits: list[str] = []
    if env.get("substrate"):
        env_bits.append(f"substrate={env['substrate']}")
    if env.get("cwd"):
        env_bits.append(f"cwd={env['cwd']}")
    if env.get("host"):
        env_bits.append(f"host={env['host']}")
    if env.get("head_sha"):
        env_bits.append(f"commit=`{str(env['head_sha'])[:12]}`")
    lines.append("- **Environment:** " + (", ".join(env_bits) if env_bits else "BOSS verification sandbox"))

    if not_run:
        lines.append("- **Not run:** " + "; ".join(str(x) for x in not_run[:6]))
    else:
        lines.append("- **Not run:** Nothing deliberately skipped.")

    links: list[str] = []
    if dl:
        links.append(f"[Download full test results]({dl})")
    if cr:
        links.append(f"[Full results check run]({cr})")
    if links:
        lines.append("- **Full results:** " + " · ".join(links))
    elif host == "local":
        lines.append("- **Full results:** Downloadable from the change screen in BOSS.")
    else:
        lines.append("- **Full results:** Available in BOSS console and check run on the head commit.")

    return _scrub("\n".join(lines)) + "\n"


def render_full_report_text(content: Mapping[str, Any]) -> str:
    """Render the full per-case test report for console download and GitHub Check Run text."""
    title = str(content.get("title") or "Test evidence report")
    te = content.get("test_evidence") if isinstance(content.get("test_evidence"), Mapping) else content
    status = str(te.get("status") or content.get("status") or "unknown")
    head_sha = str(te.get("head_sha") or content.get("head_sha") or "")
    counts = te.get("counts") if isinstance(te.get("counts"), Mapping) else {}
    suites = te.get("suites") if isinstance(te.get("suites"), list) else []
    cases = te.get("cases") if isinstance(te.get("cases"), list) else []
    env = te.get("environment") if isinstance(te.get("environment"), Mapping) else {}
    not_run = te.get("not_run") if isinstance(te.get("not_run"), list) else []
    raw_output = str(te.get("raw_output") or content.get("raw_output") or "").strip()

    out = [f"# {title}", "", f"Status: {status}"]
    if head_sha:
        out.append(f"Commit: {head_sha}")
    if env:
        out.append("Environment: " + ", ".join(f"{k}={v}" for k, v in env.items() if v))
    if counts:
        out.append(
            "Counts: "
            f"suites={counts.get('suites_run', 0)} "
            f"(passed={counts.get('suites_passed', 0)}, failed={counts.get('suites_failed', 0)}), "
            f"cases={counts.get('cases_total', 0)} "
            f"(passed={counts.get('cases_passed', 0)}, failed={counts.get('cases_failed', 0)}, "
            f"skipped={counts.get('cases_skipped', 0)})"
        )
    out.append("")
    out.append("## Suites")
    if suites:
        for s in suites:
            if not isinstance(s, Mapping):
                continue
            mark = "PASS" if s.get("ok") is True else ("FAIL" if s.get("ok") is False else "INFO")
            ec = f" (exit {s['exit_code']})" if s.get("exit_code") is not None else ""
            out.append(f"- [{mark}] {s.get('command', '')}{ec}")
    else:
        out.append("- None recorded.")

    out.append("")
    out.append("## Per-case results")
    if cases:
        for c in cases:
            if not isinstance(c, Mapping):
                continue
            st = str(c.get("status") or "").upper()
            detail = f" — {c['detail']}" if c.get("detail") else ""
            out.append(f"- [{st}] {c.get('name', '')}{detail}")
    else:
        out.append("- Individual test cases were not separately enumerated in runner output.")

    out.append("")
    out.append("## Not run")
    if not_run:
        for item in not_run:
            out.append(f"- {item}")
    else:
        out.append("- Nothing deliberately skipped.")

    if raw_output:
        out += ["", "## Raw test output", "", "```text", raw_output, "```"]

    return _scrub("\n".join(out)).rstrip() + "\n"


def render_check_run_output(content: Mapping[str, Any], *, title: str) -> dict[str, str]:
    """Build GitHub Check Run ``output`` ({title, summary, text}), bounded to GitHub's limit."""
    te = content.get("test_evidence") if isinstance(content.get("test_evidence"), Mapping) else content
    summary = render_pr_section(te)
    full_text = render_full_report_text({**dict(content), "title": title})
    if len(full_text) > CHECK_RUN_TEXT_LIMIT:
        note = "\n\n... (output truncated to fit check-run size limit; download full artifact in BOSS)\n"
        full_text = full_text[: CHECK_RUN_TEXT_LIMIT - len(note)] + note
    return {
        "title": f"BOSS Test Evidence: {title}"[:250],
        "summary": summary[:60_000],
        "text": full_text,
    }


def check_run_conclusion(status: str) -> str:
    s = str(status or "").lower()
    if s == "passed":
        return "success"
    if s == "failed":
        return "failure"
    return "neutral"


# ── PR body section enforcement & placement ───────────────────────────────────────────────────

def _find_last_test_evidence_span(body: str) -> Optional[tuple[int, int]]:
    """Return (start, end) character offsets of the last ``Test evidence`` section, or None."""
    text = str(body or "")
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    start = last.start()
    rest = text[last.end():]
    next_heading = _ANY_HEADING_RE.search(rest)
    end = last.end() + next_heading.start() if next_heading else len(text)
    return start, end


def has_test_evidence_section(body: str) -> bool:
    """True when ``body`` carries a non-empty ``### Test evidence`` (or ``## Test evidence``) section."""
    span = _find_last_test_evidence_span(body)
    if span is None:
        return False
    block = str(body or "")[span[0]:span[1]].strip()
    lines = [ln.strip() for ln in block.splitlines()[1:] if ln.strip()]
    return bool(lines)


def ends_with_test_evidence_section(body: str) -> bool:
    """True when ``body`` has a non-empty Test evidence section as its final section
    (allowing only optional git trailers or hidden HTML comments after it)."""
    text = str(body or "")
    span = _find_last_test_evidence_span(text)
    if span is None:
        return False
    block = text[span[0]:span[1]].strip()
    if not [ln.strip() for ln in block.splitlines()[1:] if ln.strip()]:
        return False
    trailing = text[span[1]:].strip()
    if not trailing:
        return True
    if _ANY_HEADING_RE.search(trailing):
        return False
    return all(_TRAILER_LINE_RE.match(ln) for ln in trailing.splitlines())


def require_test_evidence_section(body: str) -> None:
    """Enforce that a factory PR body carries a trailing, non-empty ``### Test evidence`` section."""
    if not has_test_evidence_section(body):
        raise MissingTestEvidenceError(
            "Refused to open pull request: PR description is missing the required "
            "'### Test evidence' section (suites, counts, pass/fail, environment, not run)."
        )
    if not ends_with_test_evidence_section(body):
        raise MissingTestEvidenceError(
            "Refused to open pull request: '### Test evidence' must be the final section "
            "of the PR description."
        )


def strip_test_evidence_section(body: str) -> tuple[str, Optional[str]]:
    """Remove the last Test evidence section from ``body`` and return (remaining, extracted)."""
    text = str(body or "")
    span = _find_last_test_evidence_span(text)
    if span is None:
        return text.rstrip(), None
    extracted = text[span[0]:span[1]].strip()
    before = text[:span[0]].rstrip()
    after = text[span[1]:].strip()
    remaining = f"{before}\n\n{after}".strip() if (before and after) else (before or after)
    return remaining, extracted


def assemble_pr_body(
    summary: str,
    *,
    review_text: str = "",
    replacement_section: Optional[str] = None,
    fallback_section: Optional[str] = None,
) -> str:
    """Combine summary + optional Review packet so ``### Test evidence`` always sits at the end."""
    base, existing_section = strip_test_evidence_section(summary)
    parts: list[str] = []
    if base.strip():
        parts.append(base.strip())
    if review_text and review_text.strip():
        parts.append("## Review packet\n\n" + review_text.strip())
    final_section = (
        replacement_section.strip()
        if replacement_section and replacement_section.strip()
        else (existing_section or (fallback_section.strip() if fallback_section else None))
    )
    if final_section:
        parts.append(final_section)
    return "\n\n".join(parts).strip() + "\n"


def format_artifact_download(content: Mapping[str, Any], *, fmt: str = "text") -> tuple[bytes, str, str]:
    """Return (payload_bytes, media_type, extension) for ``/v1/artifacts/{id}/download``."""
    if str(fmt or "").lower() == "json":
        data = json.dumps(content, indent=2, default=str).encode("utf-8")
        return data, "application/json; charset=utf-8", "json"
    text_body = render_full_report_text(content)
    return text_body.encode("utf-8"), "text/plain; charset=utf-8", "txt"
