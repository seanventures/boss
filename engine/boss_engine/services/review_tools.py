"""A reviewer whose tools could not run did not review (BOSSAPP-1350).

crun_9f190ed1ede845c09cdc: Codex's read-only sandbox needs user namespaces the container does
not grant, so every command a reviewer seat ran died with `bwrap: No permissions to create a new
namespace`. The seats then judged what they could not read: the acceptance judge called 30 items
"partial: could not be verified" and the adversarial review "refuted" code that had met 29/30.

A seat whose tools failed says nothing about the code, so it is NOT RUN, with the reason — the
same shape as a review that never saw the change (review_input.hollow_reason).

THE SIGNAL IS THE ENGINE'S, NEVER THE MODEL'S PROSE. A real review of a sandbox or namespace
feature talks about seccomp and namespaces for pages. `blocked_by_transport` reads only what the
CLI itself printed (its stderr, and the output of the commands the agent ran: a Codex
`command_execution` event) and the engine hands the finding to its result as `tools_blocked`.
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any, Mapping, Optional

#: The words a seat's record starts with when its tools could not run.
NOT_RUN = "NOT RUN"

#: How a sandbox/permission failure reads when a CLI or a command prints it. Conservative: only
#: the signatures this failure class is known to print.
_BLOCKED = re.compile(
    r"(?i)(bwrap:[^\n]{0,120}"
    r"|no permissions? to create a new (?:user )?namespace"
    r"|landlock\w*[^\n]{0,60}(?:not supported|not enabled|unavailable|failed|denied)"
    r"|sandbox\((?:landlockrestrict|seccompinstall|seccompbackend)\)"
    r"|(?:unshare|clone)\(\)[^\n]{0,40}operation not permitted)")

#: A file and line in an item's reason: evidence something was read.
_CITES = re.compile(r"[\w./-]+\.\w{1,6}:\d+")


def _hit(text: Optional[str]) -> Optional[str]:
    found = _BLOCKED.search(str(text or ""))
    return found.group(0).strip()[:120] if found else None


def is_tooling_failure(text: Optional[str], path: Optional[str] = None) -> bool:
    """Whether a finding's words are BOSS's own sandbox failing (BOSSAPP-1360, F-42: "every
    command failed before execution because the BOSS command sandbox could not create a
    namespace (`bwrap: ...`)"): no file anchors it and it carries the sandbox's signature.

    ONLY A SIGNATURE: never the reason to drop a finding by itself. Callers apply it to what a
    seat recorded hollow / not_run / tools_blocked said (`drop_blind`, `ledger.stale_rows`) —
    a security finding, or a seat that ran, is never judged by its prose."""
    return not path and bool(_hit(text))


def drop_blind(comments: Any, hollow: Any = None) -> list:
    """`comments` (ReviewComment objects or mappings) without those a BLIND seat raised
    (`hollow`: {stage: reason}, the seats recorded not_run / hollow / tools_blocked)."""
    blind = set(hollow or {})
    out = []
    for c in comments or []:
        get = c.get if isinstance(c, dict) else (lambda k, d=None, _c=c: getattr(_c, k, d))
        if str(get("stage") or "") not in blind:
            out.append(c)
    return out


def blocked_by_transport(stderr: Optional[str], stdout: Optional[str] = None) -> Optional[str]:
    """Why the engine's tools could not run, from the CLI's own output only, or None.

    `stderr` is the CLI's own and is read whole. `stdout` is its JSON event stream: only the
    recorded output of commands (`command_execution` events) is read — never an
    `agent_message`, which is the model talking — and only when EVERY command failed with the
    signature. One command printing it is the app's own test output (`runner_test.go:40: bwrap:
    setting up uid map`), not a sandbox that cannot start."""
    said = _hit(stderr)
    if not said:
        outputs = []
        for line in str(stdout or "").splitlines():
            if "command_execution" not in line:
                continue
            try:
                item = (json.loads(line) or {}).get("item") or {}
            except ValueError:
                continue
            if item.get("type") == "command_execution":
                outputs.append(item.get("aggregated_output") or item.get("output"))
        hits = [_hit(o) for o in outputs]
        if hits and all(hits):
            said = hits[0]
    return f"the reviewer's tools could not run ({said})" if said else None


def reads_nothing(items: list[dict], root: Any = None) -> bool:
    """A judge that cited nothing it could have read, and claimed nothing missing or met: every
    item partial/unjudged, none citing a file:line or a path that is in the tree (`root`). Never
    when any item is MISSING (a finding)."""
    if not items:
        return False
    if any(str((i or {}).get("status")) not in ("partial", "unjudged") for i in items):
        return False
    for i in items:
        why = str((i or {}).get("evidence") or "")
        if _CITES.search(why):
            return False
        if root is not None:
            from .acceptance.verdict import evidence_paths

            if evidence_paths(root, why)[0]:
                return False
    return True


def unseen_reason(result: dict, hollow: Optional[str]) -> Optional[str]:
    """Why a review seat's session did not review: its tools could not run (the ENGINE's
    report, `tools_blocked`) unless the seat still anchored findings, else `hollow`
    (review_input.hollow_reason). NOT RUN — never a refuted or failed verdict."""
    from . import review_comments

    blocked = (result or {}).get("tools_blocked")
    if blocked and not review_comments.parse_findings(str((result or {}).get("summary") or "")):
        return not_run(str(blocked))
    return hollow


def not_run(reason: str) -> str:
    return f"{NOT_RUN}: {reason}"


def _norm_path(path: Any) -> str:
    return str(path or "").strip().replace("\\", "/").lstrip("./")


def changed_hunks(
    prev_files: Mapping[str, str],
    curr_files: Mapping[str, str],
) -> dict[str, Optional[list[tuple[int, int]]]]:
    """Map each file in `curr_files` to:
    - None if it is new (absent in `prev_files`, all lines changed),
    - [] if it is byte-identical to `prev_files`,
    - [(start, end), ...] 1-based inclusive line spans in `curr_files` that changed.
    """
    prev_norm = {_norm_path(k): v for k, v in prev_files.items()}
    out: dict[str, Optional[list[tuple[int, int]]]] = {}
    for raw_path, new_text in curr_files.items():
        path = _norm_path(raw_path)
        if path not in prev_norm:
            out[path] = None
            continue
        old_text = prev_norm[path]
        if old_text == new_text:
            out[path] = []
            continue
        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()
        spans: list[tuple[int, int]] = []
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
        for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag in ("replace", "insert"):
                spans.append((j1 + 1, max(j1 + 1, j2)))
            elif tag == "delete":
                spans.append((max(1, j1), max(1, j1 + 1)))
        out[path] = spans
    return out


def _resolve_hunk_path(
    finding_path: Any,
    hunks: Mapping[str, Optional[list[tuple[int, int]]]],
) -> Optional[str]:
    norm = _norm_path(finding_path)
    if not norm:
        return None
    if norm in hunks:
        return norm
    matches = [
        k for k in hunks
        if k.endswith("/" + norm) or norm.endswith("/" + k)
    ]
    return matches[0] if len(matches) == 1 else None


def finding_touches_changed_lines(
    finding: Mapping[str, Any],
    hunks: Mapping[str, Optional[list[tuple[int, int]]]],
    curr_files: Mapping[str, str],  # noqa: ARG001 — retained in public signature
) -> bool:
    """True when `finding` is unanchored, unresolvable (fail closed), in a new file, or
    overlaps a changed hunk; False when it sits on unchanged lines of a previously reviewed file."""
    resolved = _resolve_hunk_path(finding.get("path"), hunks)
    if resolved is None:
        return True
    spans = hunks[resolved]
    if spans is None:
        return True
    if not spans:
        return False
    line = finding.get("line")
    if line is None:
        return True
    try:
        start = int(line)
        end = int(finding.get("end_line") or start)
    except (TypeError, ValueError):
        return True
    if end < start:
        start, end = end, start
    return any(not (end < h_start or start > h_end) for h_start, h_end in spans)


def prior_finding_fingerprints(prior_summary: str, *, stage: Optional[str] = None) -> set[str]:
    """Fingerprints (`rv:...`) of every FINDING raised in `prior_summary`."""
    from . import review_comments
    from .acceptance.ledger import review_fingerprint

    return {
        review_fingerprint(c.path, c.body)
        for c in review_comments.parse_findings(prior_summary or "", stage=stage)
    }


def classify_unchanged_findings(
    findings: list[dict],
    *,
    prev_files: Optional[Mapping[str, str]],
    curr_files: Optional[Mapping[str, str]],
    prior_summary: str = "",
    stage: Optional[str] = None,
) -> list[dict]:
    """Tag brand-new findings sitting on unchanged code as `newly_noticed` (BOSSAPP-1387).
    Findings that were already in `prior_summary` or that touch changed lines keep their
    original status so regressions and unresolved blockers are never demoted."""
    if prev_files is None or curr_files is None:
        return [dict(f) for f in findings]
    from .acceptance.ledger import review_fingerprint

    hunks = changed_hunks(prev_files, curr_files)
    prior_fps = prior_finding_fingerprints(prior_summary, stage=stage)
    out: list[dict] = []
    for f in findings:
        item = dict(f)
        fp = review_fingerprint(item.get("path"), item.get("text") or "")
        if fp not in prior_fps and not finding_touches_changed_lines(item, hunks, curr_files):
            item["newly_noticed"] = True
            item["label"] = "newly noticed"
        out.append(item)
    return out
