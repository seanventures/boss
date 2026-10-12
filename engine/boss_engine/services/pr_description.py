"""A pull request's description, generated from what the run recorded — never agent chat.

PR #223's description opened with "Governed pipeline: tests by qa, impl by SE, adversarial
review SURVIVED" (the review had seen nothing) and then pasted the implementer's closing remark:
"The repo is clean (no uncommitted changes) and no leftover server process concerns since it was
killed." A reviewer opening the PR learned nothing about the change from either.

The description now says, from the run's own records: what changed, how it was tested, which
checks passed and failed, how the review went round by round, and where to look in BOSS.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional

from .pr_review_post import SEAT_LABELS, VERDICT_WORDS

_GATE_WORDS = {
    "code_written": "Code written", "written_tests_pass": "New tests pass",
    "existing_tests_intact": "Old tests pass", "tests_written_first": "Tests came first",
    "coder_did_not_edit_tests": "Tests untouched", "tests_strong_enough": "Tests catch bugs",
    "code_review_passed": "Review passed", "security_clean": "Security clean",
    "consistent": "Consistent", "nothing_broke": "Nothing broke", "app_serves": "App starts",
    "web_package_honest": "Package honest", "template_tests": "Template tests",
    "no_committed_secrets": "No secrets",
}


def _gate(name: str) -> str:
    return _GATE_WORDS.get(name) or name.replace("_", " ").capitalize()


def _changed(paths: list[str]) -> list[str]:
    top = Counter((p.split("/", 1)[0] + "/") if "/" in p else p for p in paths)
    lines = [f"- `{k}` — {v} file{'s' if v != 1 else ''}" for k, v in top.most_common(12)]
    if len(top) > 12:
        lines.append(f"- … and {len(top) - 12} more places")
    return lines


def render(*, title: str, evidence: dict, gates: dict, paths: Iterable[str] = (),
           additions: Optional[int] = None, deletions: Optional[int] = None,
           rounds: Iterable[dict] = (), comments: Iterable[dict] = (),
           deferred: Iterable[dict] = (), build_url: str = "", run_id: str = "",
           quality_gate_id: str = "") -> str:
    """The description. Every line comes from a record; nothing is written by a model."""
    paths = sorted({str(p) for p in paths if p})
    rounds = [r for r in rounds if isinstance(r, dict)]
    out = [f"## {title}".strip(), ""]
    # What changed.
    size = (f" (+{int(additions or 0)} −{int(deletions or 0)})"
            if additions is not None or deletions is not None else "")
    out.append(f"### What changed\n{len(paths)} file{'s' if len(paths) != 1 else ''}{size}.")
    out += _changed(paths)
    tests = [str(p) for p in (evidence.get("test_files") or []) if p]
    if tests:
        out.append(f"- Tests: {len(tests)} file{'s' if len(tests) != 1 else ''}")
    # Tests.
    out += ["", "### Tests"]
    # The run records its commands as `tests` (a list of {command, ok, exit_code}) and the last
    # one as `test_command`; reading only `test_commands`/`test_cmd` said "No test command was
    # recorded" over a run whose build, typecheck, lint and e2e had all passed.
    cmds = next((v for v in (evidence.get("test_commands"), evidence.get("tests"))
                 if isinstance(v, list) and v), [])
    single = evidence.get("test_cmd") or evidence.get("test_command")
    if cmds:
        for c in cmds[:6]:
            c = c if isinstance(c, dict) else {"command": str(c)}
            ok = c.get("ok") if "ok" in c else c.get("exit_code") == 0
            out.append(f"- {'✅' if ok else '❌'} `{str(c.get('command') or '')[:160]}`")
    elif single:
        passed = evidence.get("tests_passed")
        mark = "✅" if passed else ("❌" if passed is False else "•")
        out.append(f"- {mark} `{str(single)[:160]}`")
    else:
        out.append("- No test command was recorded.")
    # Checks — the review checks as the FINAL review round says, not as the first round left
    # them: "✅ Review passed" stood over a round that did not see the change.
    gates = dict(gates or {})
    final = next((r.get("seats") or {} for r in reversed(rounds) if r.get("seats")), {})
    for seat, gate, good in (("review_code", "code_review_passed", {"survived"}),
                             ("security_check", "security_clean", {"clean"}),
                             ("consistency_check", "consistent", {"consistent"})):
        verdict = str((final.get(seat) or {}).get("verdict") or "")
        # Only ever downgrades: a recorded failure stays a failure.
        if gate in gates and verdict:
            gates[gate] = bool(gates[gate]) and verdict in good
    passed = [k for k, v in gates.items() if v]
    failed = [k for k, v in gates.items() if not v]
    out += ["", "### Checks"]
    if gates:
        out.append(f"{len(passed)} passed, {len(failed)} failed.")
        out += [f"- ❌ {_gate(g)}" for g in failed]
        out += [f"- ✅ {_gate(g)}" for g in passed]
    else:
        out.append("- No quality gate was recorded.")
    # Review.
    rounds = [r for r in rounds if isinstance(r, dict)]
    comments = [c for c in comments if isinstance(c, dict)]
    out += ["", "### Review"]
    if rounds:
        out.append(f"{len(rounds)} review round{'s' if len(rounds) != 1 else ''}; each is "
                   "posted below as its own review, with the implementer's answers as replies.")
        for i, r in enumerate(rounds, start=1):
            seats = r.get("seats") or {}
            words = "; ".join(f"{SEAT_LABELS.get(s, s)}: "
                              f"{VERDICT_WORDS.get(str((v or {}).get('verdict')), (v or {}).get('verdict'))}"
                              for s, v in seats.items()) or "findings only"
            out.append(f"- Round {i}: {words}")
    else:
        out.append("- No review round was recorded.")
    if comments:
        sev = Counter(str(c.get("severity") or "") for c in comments)
        fixed = sum(1 for c in comments if c.get("response_kind") == "fixed")
        disputed = sum(1 for c in comments if c.get("response_kind") == "disputed")
        out.append("- Findings: " + ", ".join(f"{sev[s]} {s}" for s in
                                             ("blocker", "major", "minor", "nit") if sev[s])
                   + f" · {fixed} fixed, {disputed} disputed")
    deferred = list(deferred or [])
    if deferred:
        out.append(f"- Deferred as follow-up work: {len(deferred)}")
    hollow = evidence.get("review_hollow") or {}
    for stage, why in hollow.items():
        out.append(f"- ⚠ {SEAT_LABELS.get(stage, stage)} did not see the change ({why}).")
    # Links.
    out += ["", "### In BOSS"]
    if build_url:
        out.append(f"- Build: {build_url}")
    if run_id:
        out.append(f"- Run: `{run_id}`")
    if quality_gate_id:
        out.append(f"- Quality gate: `{quality_gate_id}`")
    return "\n".join(out).strip() + "\n"


def build_url(settings: Any, run_id: str) -> str:
    base = str(getattr(settings, "public_url", "") or "").rstrip("/")
    return f"{base}/?area=code_tasks&build={run_id}" if base else ""
