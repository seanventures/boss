"""GitHub Actions CI, governed: who may change it, how a change to it is reviewed, and waiting for it.

Three things a build needs from CI, and what used to happen instead:

  1. CHANGING THE WORKFLOWS. GitHub refuses any push that touches `.github/workflows/` from an App
     without `workflows: write`, and BOSS's App never asked for it, so a build could not add CI.
     Now it is an OPT-IN per repository ("Builds may change CI workflows", off by default). Turning
     it on needs an Owner or Admin with a fresh step-up (`github:connect`), is recorded, and makes
     the App manifest and the build's GitHub needs include `workflows: write`. An App created
     before the opt-in must accept the new permission at GitHub: `permissions_link` says where.

  2. A CHANGE TO CI IS A CHANGE TO WHAT GUARDS MAIN. Any build whose diff touches
     `.github/workflows/` is flagged "CI changed" on its pull request and in its review in BOSS,
     ALWAYS gets the adversarial review (whatever its risk tier), gets deterministic static checks
     (secret exfiltration, `pull_request_target` checking out the PR head, actions not pinned to a
     full commit SHA), and needs a PERSON's approval before it is merged or deployed. A policy, an
     agent, an API key or a run token cannot give that approval (`refuse_approval`).

  3. WAITING FOR CI. `wait_for_ci` waits for every check run and commit status on the PR head to
     finish, with a bounded timeout (BOSS_CI_WAIT_TIMEOUT_SECONDS, 30 min by default) and backoff
     polling, and says what happened in words: "CI passed: 3 checks", "CI failed: 'unit-tests'
     failed — <url>", "CI did not finish in 30 min". A failure or a timeout blocks the deploy
     approval (and the merge, unless a person forces it with a note) and links the failing check.

NO CHECKS AT ALL. A repository with CI configured (a `.github/workflows/` directory at the head,
or any check that ever reports) that reports nothing within the timeout has NOT passed: it times
out ("no checks reported"). A repository with no CI configured — no workflows and no check or
status reported within a short grace period — is recorded as `no_ci`, said plainly ("No CI is
configured ..."), and does not block: there is nothing to wait for, and BOSS's own gate already ran
the suite. The choice is per wait (`has_ci`), decided once when the wait starts.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from sqlalchemy import text

WORKFLOW_DIR = ".github/workflows/"
#: Where the per-repository opt-in lives: github_repository_bindings.metadata[SETTING_KEY].
SETTING_KEY = "ci_workflows_write"
SETTING_LABEL = "Builds may change CI workflows"
SETTING_HELP = ("Lets a build add or edit files under .github/workflows/. The GitHub App then needs "
                "the 'workflows: write' permission, and every such change is flagged 'CI changed', "
                "reviewed for secret leaks and unpinned actions, and needs a person's approval "
                "before merge or deploy.")

DEFAULT_TIMEOUT_SECONDS = 30 * 60
DEFAULT_POLL_SECONDS = 10
MAX_POLL_SECONDS = 120
#: How long a repository with no CI configured is given for an external CI to report a status.
NO_CI_GRACE_SECONDS = 60

TERMINAL = frozenset({"passed", "failed", "timed_out", "no_ci", "cancelled"})
#: Statuses a deploy (and, unforced, a merge) may go ahead on.
CLEARED = frozenset({"passed", "no_ci"})
_OK_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

#: Decisions that ship something. A CI change or a CI failure gates every one of them.
DEPLOY_ACTIONS = frozenset({"review_deployment", "deploy_host", "deploy_stack"})
MERGE_ACTIONS = frozenset({"review_pull_request"})


# ---- 2. what a change to CI is -------------------------------------------------------------------

def _norm(path: str) -> str:
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def is_workflow_path(path: str) -> bool:
    p = _norm(path)
    return p.startswith(WORKFLOW_DIR) and len(p) > len(WORKFLOW_DIR)


def workflow_paths(paths: Iterable[str]) -> list[str]:
    """The paths under .github/workflows/ among `paths`, sorted, each once."""
    return sorted({_norm(p) for p in paths or () if is_workflow_path(p)})


def workflow_changes(files: Any) -> dict[str, str]:
    """{path: content} of the workflow files in a change: a {path: content} mapping, or a list
    of FileChange-like objects / dicts with path and content."""
    out: dict[str, str] = {}
    if isinstance(files, Mapping):
        items = files.items()
    else:
        items = []
        for f in files or ():
            path = f.get("path") if isinstance(f, Mapping) else getattr(f, "path", None)
            content = f.get("content") if isinstance(f, Mapping) else getattr(f, "content", None)
            items.append((path, content))
    for path, content in items:
        if path and is_workflow_path(str(path)):
            out[_norm(str(path))] = "" if content is None else str(content)
    return dict(sorted(out.items()))


@dataclass(frozen=True)
class Finding:
    check: str          # secret_exfiltration | pull_request_target | unpinned_action | script_injection
    severity: str       # blocker | major | minor
    path: str
    line: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "severity": self.severity, "path": self.path,
                "line": self.line, "message": self.message}

    def finding_line(self) -> str:
        """The reviewer's own contract (review_comments), so the gate and the PR read it."""
        return (f"FINDING: {self.severity.upper()} {self.path}:{self.line} — "
                f"[ci:{self.check}] {self.message}")


_USES = re.compile(r"""^\s*-?\s*uses\s*:\s*['"]?([^'"\s#]+)""")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN = re.compile(r"^(\s*)-?\s*run\s*:\s*(.*)$")
_SECRET = re.compile(r"\$\{\{\s*(?:secrets\.[A-Za-z0-9_]+|secrets\[[^\]]+\]|toJSON\(\s*secrets\s*\))\s*\}\}",
                     re.IGNORECASE)
_SECRETS_DUMP = re.compile(r"toJSON\(\s*secrets\s*\)", re.IGNORECASE)
_NETWORK = re.compile(r"\b(curl|wget|nc|ncat|netcat|telnet|scp|ssh|ftp|Invoke-WebRequest|"
                      r"Invoke-RestMethod|iwr|irm|http|https)\b|/dev/tcp/|https?://", re.IGNORECASE)
_PRINT = re.compile(r"\b(echo|printf|print|cat|tee|Write-Output|Write-Host)\b", re.IGNORECASE)
_PR_HEAD = re.compile(r"github\.event\.pull_request\.head\.(sha|ref)|github\.head_ref|"
                      r"refs/pull/|github\.event\.pull_request\.merge_commit_sha", re.IGNORECASE)
_UNTRUSTED = re.compile(r"\$\{\{\s*github\.(event\.(pull_request\.(title|body|head\.ref|head\.label)|"
                        r"issue\.(title|body)|comment\.body|review\.body|review_comment\.body|"
                        r"head_commit\.message|commits)|head_ref)[^}]*\}\}", re.IGNORECASE)
_PRT = re.compile(r"\bpull_request_target\b")
_UNTRUSTED_TRIGGER = re.compile(r"\b(pull_request_target|workflow_run)\b")


def _strip_comment(line: str) -> str:
    # A YAML comment starts at a '#' preceded by whitespace (or the line start); good enough here.
    m = re.search(r"(^|\s)#", line)
    return line[:m.start()] if m else line


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def static_findings(path: str, content: str) -> list[Finding]:
    """Deterministic checks of one workflow file. Line-based so every finding has a line; it reads
    YAML shape (indentation of `run:` blocks and `steps:`), not YAML semantics.

    blocker  secret_exfiltration   a secret echoed, or sent over the network, in a `run:`; a secret
                                   used in `run:` under an untrusted trigger; `toJSON(secrets)`
    blocker  pull_request_target   a checkout of the PR head (head.sha/head.ref/refs/pull) in a
                                   workflow triggered by pull_request_target
    major    script_injection      an attacker-controlled context (PR title/body, head_ref, ...)
                                   expanded inside a `run:` script
    minor    unpinned_action       `uses:` not pinned to a full 40-character commit SHA (recorded
                                   and posted, not blocking: most published workflows pin tags)
    """
    path = _norm(path)
    lines = (content or "").splitlines()
    code = [_strip_comment(ln) for ln in lines]
    out: list[Finding] = []
    untrusted_trigger = any(_UNTRUSTED_TRIGGER.search(c) for c in code)
    prt = any(_PRT.search(c) for c in code)

    # Which lines belong to a `run:` script (the run line itself and its block scalar).
    in_run = [False] * len(code)
    i = 0
    while i < len(code):
        m = _RUN.match(code[i])
        if not m:
            i += 1
            continue
        in_run[i] = True
        base = _indent(lines[i])
        j = i + 1
        if m.group(2).strip()[:1] in ("|", ">") or not m.group(2).strip():
            while j < len(code) and (not code[j].strip() or _indent(lines[j]) > base):
                in_run[j] = True
                j += 1
        i = j

    for n, c in enumerate(code, start=1):
        if not c.strip():
            continue
        um = _USES.match(c)
        if um:
            ref = um.group(1)
            if not (ref.startswith("./") or ref.startswith("docker://")):
                at = ref.rsplit("@", 1)
                if len(at) != 2 or not _SHA.match(at[1]):
                    shown = at[1] if len(at) == 2 else "no ref"
                    out.append(Finding("unpinned_action", "minor", path, n,
                                       f"'{ref}' is not pinned to a full 40-character commit SHA "
                                       f"({shown}); a moved tag runs someone else's code with this "
                                       f"repository's token. Pin it: uses: {at[0]}@<sha> # {shown}"))
            elif ref.startswith("docker://") and "@sha256:" not in ref:
                out.append(Finding("unpinned_action", "minor", path, n,
                                   f"'{ref}' is not pinned to an image digest (@sha256:...)."))
        if prt and _PR_HEAD.search(c) and not in_run[n - 1]:
            out.append(Finding("pull_request_target", "blocker", path, n,
                               "checks out the pull request's head under pull_request_target: "
                               "code from a fork then runs with this repository's secrets and a "
                               "write token. Use pull_request, or never check out or run the "
                               "PR's code in this workflow."))
        if _SECRETS_DUMP.search(c):
            out.append(Finding("secret_exfiltration", "blocker", path, n,
                               "toJSON(secrets) exposes every secret of the repository at once."))
            continue
        if in_run[n - 1]:
            has_secret = bool(_SECRET.search(c))
            if has_secret and (_NETWORK.search(c) or _PRINT.search(c)):
                out.append(Finding("secret_exfiltration", "blocker", path, n,
                                   "a secret is printed or sent over the network from a run: "
                                   "script. Pass it through env: to the one tool that needs it, "
                                   "never to echo/curl/wget."))
            elif has_secret and untrusted_trigger:
                out.append(Finding("secret_exfiltration", "blocker", path, n,
                                   "a secret is used in a run: script of a workflow triggered by "
                                   "pull_request_target/workflow_run, where untrusted code can "
                                   "read it."))
            if _UNTRUSTED.search(c):
                out.append(Finding("script_injection", "major", path, n,
                                   "an attacker-controlled value (a PR title/body, a branch name) "
                                   "is expanded straight into a shell script. Pass it via env: and "
                                   "quote it."))
    return out


def static_review(files: Any) -> list[Finding]:
    """Static findings for every workflow file in a change."""
    found: list[Finding] = []
    for path, content in workflow_changes(files).items():
        found.extend(static_findings(path, content))
    return found


def findings_block(findings: Iterable[Finding]) -> str:
    items = list(findings)
    if not items:
        return ""
    return ("\n\nCI workflow static checks (deterministic, BOSS):\n"
            + "\n".join(f.finding_line() for f in items))


#: Handed to the adversarial reviewer whenever the change touches .github/workflows/.
REVIEW_CLAUSE = (
    "\n\nTHIS CHANGE EDITS CI WORKFLOWS (.github/workflows/). Review every workflow file and emit a "
    "FINDING for each of these, with its file:line:\n"
    "  1. SECRET EXFILTRATION: a secret (${{ secrets.* }}, or an env var holding one) echoed, "
    "printed, written to a file that is uploaded, or sent to the network (curl, wget, nc, an "
    "action that posts it); secrets used in run: steps of untrusted contexts "
    "(pull_request_target, workflow_run, issue_comment). BLOCKER.\n"
    "  2. pull_request_target MISUSE: a workflow triggered by pull_request_target that checks out "
    "or runs the pull request's head (github.event.pull_request.head.sha/ref, github.head_ref, "
    "refs/pull/*). BLOCKER.\n"
    "  3. UNPINNED ACTIONS: any uses: not pinned to a full 40-character commit SHA.\n"
    "  4. Over-broad permissions: (write-all, contents: write where read suffices) and script "
    "injection of attacker-controlled contexts into run: scripts.\n"
)


def review_clause_for(change: str) -> str:
    """The workflow checklist when the change text shows a workflow file, else ""."""
    return REVIEW_CLAUSE if WORKFLOW_DIR in (change or "") else ""


def ci_changed_comment(paths: list[str], findings: list[Finding]) -> str:
    lines = ["**CI changed** — this pull request edits GitHub Actions workflows:", ""]
    lines += [f"- `{p}`" for p in paths]
    lines += ["", "BOSS requires a person's approval before this is merged or deployed; no "
              "agent or automatic policy can approve it."]
    if findings:
        lines += ["", "Static checks:"]
        lines += [f"- {f.severity.upper()} `{f.path}:{f.line}` [{f.check}] {f.message}"
                  for f in findings]
    else:
        lines += ["", "Static checks (secret exfiltration, pull_request_target, unpinned "
                  "actions): nothing found."]
    return "\n".join(lines)


def ci_changed_evidence(files: Any) -> dict[str, Any]:
    """What a review/decision records about a CI change. {} when the change touches no workflow."""
    wf = workflow_changes(files)
    if not wf:
        return {}
    findings = static_review(wf)
    return {
        "ci_changed": True,
        "ci_changed_files": list(wf),
        "ci_findings": [f.as_dict() for f in findings],
        "requires_person_approval": True,
        "ci_changed_note": ("CI changed: this change edits " + ", ".join(list(wf)[:5])
                            + ". A person must approve it before merge or deploy."),
    }


# ---- 1. the opt-in -------------------------------------------------------------------------------

class CiSettingError(Exception):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


def _one(connection: Any, statement: str, params: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """One row, read in a SAVEPOINT when the connection has them: a refused read (an older
    schema) raises to the caller without poisoning the caller's transaction on Postgres."""
    nested = getattr(connection, "begin_nested", None)
    point = nested() if callable(nested) else None
    try:
        row = connection.execute(text(statement), dict(params)).mappings().first()
    except Exception:
        if point is not None:
            point.rollback()
        raise
    if point is not None:
        point.commit()
    return row


def _meta(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    return dict(value or {})


def _binding(connection: Any, project_id: str, binding_id: str) -> Mapping[str, Any]:
    row = connection.execute(text(
        "SELECT id, full_name, metadata FROM github_repository_bindings "
        "WHERE id = :b AND project_id = :p"), {"b": binding_id, "p": project_id}).mappings().first()
    if row is None:
        raise CiSettingError("repository_not_found", 404,
                             "That repository is not connected to this project.")
    return row


def workflows_write_allowed(connection: Any, project_id: str, full_name: str) -> bool:
    """True when the project's binding of `full_name` has the opt-in turned on."""
    try:
        row = _one(connection,
                   "SELECT metadata FROM github_repository_bindings WHERE project_id = :p "
                   "AND lower(full_name) = lower(:r) AND status = 'active'",
                   {"p": project_id, "r": full_name})
    except Exception:  # noqa: BLE001 — an older schema: never opted in
        return False
    return bool(row is not None and _meta(row["metadata"]).get(SETTING_KEY) is True)


def any_repo_allows_workflows(connection: Any) -> bool:
    """Any active binding opted in: then the (one, org-wide) App asks GitHub for workflows."""
    try:
        return _one(connection, "SELECT 1 AS x FROM github_repository_bindings WHERE status = "
                    "'active' AND (metadata->>:k) = 'true' LIMIT 1", {"k": SETTING_KEY}) is not None
    except Exception:  # noqa: BLE001
        return False


def permissions_link(app_cfg: Mapping[str, Any], web_base: str = "https://github.com") -> dict[str, Any]:
    """Where an operator accepts `workflows: write` for an App created before the opt-in.

    The App's permissions page (personal or organisation-owned); after saving there, GitHub asks
    every installation to accept the new permission on the installation's own settings page."""
    slug = app_cfg.get("slug")
    owner = app_cfg.get("account_login")
    base = web_base.rstrip("/")
    inst = app_cfg.get("installation_id")
    if not slug:
        return {"permissions_url": None, "installation_url": None,
                "note": ("No GitHub App was created from Setup here, so BOSS cannot link it. Open "
                         "your App's settings at GitHub > Settings > Developer settings > GitHub "
                         "Apps, add Repository permission 'Workflows: Read and write', save, then "
                         "accept the new permission on the installation.")}
    org_path = f"{base}/organizations/{owner}/settings/apps/{slug}/permissions"
    user_path = f"{base}/settings/apps/{slug}/permissions"
    perm_url = org_path if owner else user_path
    install_url = (f"{base}/organizations/{owner}/settings/installations/{inst}" if owner and inst
                   else f"{base}/settings/installations/{inst}" if inst else None)
    return {
        "permissions_url": perm_url,
        "permissions_url_personal": user_path,
        "installation_url": install_url,
        "note": ("Open the App's permissions page, set Repository permissions > Workflows to "
                 "'Read and write' and save. GitHub then asks the installation to accept it: open "
                 "the installation's settings page and press 'Accept new permissions'. Until both "
                 "are done GitHub refuses pushes that touch .github/workflows/."),
    }


def setting_view(connection: Any, project_id: str, binding_id: str,
                 app_cfg: Optional[Mapping[str, Any]] = None, web_base: str = "https://github.com"
                 ) -> dict[str, Any]:
    row = _binding(connection, project_id, binding_id)
    enabled = _meta(row["metadata"]).get(SETTING_KEY) is True
    out = {"binding_id": row["id"], "repository": row["full_name"], "key": SETTING_KEY,
           "label": SETTING_LABEL, "help": SETTING_HELP, "enabled": enabled,
           "requires": "Owner or Admin, with step-up",
           "app_permission": "workflows: write" if enabled else None}
    if enabled:
        out.update(permissions_link(app_cfg or {}, web_base))
    return out


def set_workflows_write(connection: Any, project_id: str, binding_id: str, enabled: bool, *,
                        actor: str) -> dict[str, Any]:
    """Turn the opt-in on or off. The caller has already checked github:connect (Owner/Admin,
    step-up) at the route; this records who changed it, in the project's events and, under
    identity, in the security audit chain."""
    from .core import emit_event

    row = _binding(connection, project_id, binding_id)
    meta = _meta(row["metadata"])
    before = meta.get(SETTING_KEY) is True
    meta[SETTING_KEY] = bool(enabled)
    connection.execute(text(
        "UPDATE github_repository_bindings SET metadata = CAST(:m AS JSONB), updated_at = now() "
        "WHERE id = :b AND project_id = :p"),
        {"m": json.dumps(meta), "b": binding_id, "p": project_id})
    data = {"repository": row["full_name"], "setting": SETTING_KEY, "before": before,
            "after": bool(enabled)}
    emit_event(connection, project_id, "repository.ci_workflows_write_changed", actor,
               "github_repository_binding", binding_id, data)
    _security_audit(connection, data, binding_id)
    return data


def _security_audit(connection: Any, data: dict[str, Any], binding_id: str) -> None:
    try:
        from ..identity import audit, lineage
    except Exception:  # noqa: BLE001
        return
    ctx = lineage.current_context()
    if ctx is None:
        return
    audit.record(audit.SecurityEvent(
        org_id=ctx.principal.org_id, event_type="policy.changed", outcome=audit.Outcome.SUCCESS,
        actor_principal_id=ctx.principal.id, actor_kind="user",
        target_type="github_repository_binding", target_id=binding_id,
        permission="github:connect", details=data), db=connection)


def build_github_needs(connection: Any, project_id: str, full_name: str,
                       base: Mapping[str, str]) -> dict[str, str]:
    """A build's GitHub needs: `base`, plus workflows: write when its repository opted in."""
    needs = dict(base)
    if full_name and workflows_write_allowed(connection, project_id, full_name):
        needs["workflows"] = "write"
    return needs


# ---- 3. waiting for CI ---------------------------------------------------------------------------

class CiReader(Protocol):
    def check_runs(self, sha: str) -> list[dict]: ...
    def statuses(self, sha: str) -> list[dict]: ...
    def has_workflows(self, sha: str) -> bool: ...


@dataclass
class CiOutcome:
    state: str                      # waiting | passed | failed | timed_out | no_ci
    summary: str
    checks: list[dict] = field(default_factory=list)
    failing_url: Optional[str] = None
    failing: list[str] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "summary": self.summary, "checks": self.checks,
                "failing_url": self.failing_url, "failing": self.failing}


def _minutes(seconds: float) -> str:
    m = max(1, int(round(seconds / 60.0)))
    return f"{m} min"


def normalise(check_runs: Iterable[Mapping[str, Any]], statuses: Iterable[Mapping[str, Any]]
              ) -> list[dict]:
    """Check runs (the latest per name) and commit statuses (the latest per context) as one list
    of {name, kind, status, conclusion, url}."""
    latest: dict[str, Mapping[str, Any]] = {}
    for run in check_runs or ():
        name = str(run.get("name") or "check")
        prev = latest.get(name)
        if prev is None or int(run.get("id") or 0) > int(prev.get("id") or 0):
            latest[name] = run
    out = [{"name": name, "kind": "check_run", "status": str(r.get("status") or "queued"),
            "conclusion": (str(r["conclusion"]) if r.get("conclusion") else None),
            "url": r.get("html_url") or r.get("details_url")}
           for name, r in latest.items()]
    seen: set[str] = set()
    for st in statuses or ():
        ctx = str(st.get("context") or "status")
        if ctx in seen:
            continue     # the combined-status API lists the newest first
        seen.add(ctx)
        state = str(st.get("state") or "pending")
        out.append({"name": ctx, "kind": "status",
                    "status": "in_progress" if state == "pending" else "completed",
                    "conclusion": None if state == "pending" else
                    ("success" if state == "success" else "failure"),
                    "url": st.get("target_url")})
    return sorted(out, key=lambda c: c["name"])


def evaluate(checks: list[dict]) -> CiOutcome:
    """One look at the checks: failed as soon as any finished check failed, waiting while any is
    unfinished, passed when all finished well. No checks at all -> waiting (the caller decides
    whether that becomes no_ci or a timeout)."""
    if not checks:
        return CiOutcome("waiting", "No CI checks have reported yet.", [])
    bad = [c for c in checks if c["status"] == "completed"
           and (c["conclusion"] or "").lower() not in _OK_CONCLUSIONS]
    if bad:
        first = bad[0]
        more = f" (and {len(bad) - 1} more)" if len(bad) > 1 else ""
        url = first.get("url")
        return CiOutcome("failed",
                         f"CI failed: '{first['name']}' {first.get('conclusion') or 'failed'}"
                         .replace("' failure", "' failed") + more + (f" — {url}" if url else ""),
                         checks, url, [c["name"] for c in bad])
    pending = [c for c in checks if c["status"] != "completed"]
    if pending:
        return CiOutcome("waiting",
                         f"CI running: {len(checks) - len(pending)} of {len(checks)} checks done; "
                         f"waiting on '{pending[0]['name']}'.", checks,
                         pending[0].get("url"), [])
    n = len(checks)
    return CiOutcome("passed", f"CI passed: {n} check{'s' if n != 1 else ''}", checks)


def look(reader: CiReader, sha: str) -> CiOutcome:
    return evaluate(normalise(reader.check_runs(sha), reader.statuses(sha)))


def settle(outcome: CiOutcome, *, elapsed: float, timeout: float, has_ci: bool,
           repository: str = "", grace: float = NO_CI_GRACE_SECONDS) -> CiOutcome:
    """Turn a non-terminal look into a terminal answer when time says so."""
    if outcome.terminal:
        return outcome
    if not outcome.checks and not has_ci and elapsed >= min(grace, timeout):
        where = f" for {repository}" if repository else ""
        return CiOutcome("no_ci", f"No CI is configured{where} (no .github/workflows and no "
                                  "check reported), so there was nothing to wait for.", [])
    if elapsed >= timeout:
        if not outcome.checks:
            return CiOutcome("timed_out", f"CI did not finish in {_minutes(timeout)}: no checks "
                                          "were reported for this commit.", [])
        pend = [c for c in outcome.checks if c["status"] != "completed"]
        head = pend[0] if pend else None
        url = head.get("url") if head else None
        return CiOutcome("timed_out",
                         f"CI did not finish in {_minutes(timeout)}"
                         + (f": '{head['name']}' still running" if head else "")
                         + (f" — {url}" if url else ""), outcome.checks, url,
                         [c["name"] for c in pend])
    return outcome


def next_interval(attempt: int, poll: float = DEFAULT_POLL_SECONDS,
                  cap: float = MAX_POLL_SECONDS) -> float:
    """Backoff: poll, x1.5 per attempt, capped."""
    return float(min(cap, poll * (1.5 ** max(0, attempt))))


def wait_for_ci(reader: CiReader, sha: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                poll_seconds: float = DEFAULT_POLL_SECONDS, has_ci: Optional[bool] = None,
                repository: str = "", sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic,
                on_look: Optional[Callable[[CiOutcome], None]] = None) -> CiOutcome:
    """BLOCK until CI on `sha` finishes or `timeout_seconds` pass, polling with backoff."""
    if has_ci is None:
        has_ci = _safe_has_workflows(reader, sha)
    start = clock()
    attempt = 0
    while True:
        try:
            outcome = look(reader, sha)
        except Exception as error:  # noqa: BLE001 — a flaky read is retried, never a pass
            outcome = CiOutcome("waiting", f"Could not read CI yet: {type(error).__name__}.", [])
        if outcome.checks:
            has_ci = True
        outcome = settle(outcome, elapsed=clock() - start, timeout=timeout_seconds,
                         has_ci=bool(has_ci), repository=repository)
        if on_look:
            on_look(outcome)
        if outcome.terminal:
            return outcome
        remaining = timeout_seconds - (clock() - start)
        sleep(max(0.0, min(next_interval(attempt, poll_seconds), max(remaining, 0.0))))
        attempt += 1


def _safe_has_workflows(reader: CiReader, sha: str) -> bool:
    try:
        return bool(reader.has_workflows(sha))
    except Exception:  # noqa: BLE001 — unknown: assume CI exists, so silence never passes
        return True


class HttpCiReader:
    """Reads a repository's CI over GitHub's REST API with a token."""

    def __init__(self, api_base: str, repository: str, token: str,
                 get: Optional[Callable[[str, str], Any]] = None):
        self.api_base = (api_base or "https://api.github.com").rstrip("/")
        self.repository = repository
        self.token = token
        if get is None:
            from .kit_connections import _http_get as get  # noqa: PLC0415
        self._get = get

    def _url(self, path: str) -> str:
        return f"{self.api_base}/repos/{self.repository}{path}"

    def check_runs(self, sha: str) -> list[dict]:
        return list((self._get(self._url(f"/commits/{sha}/check-runs?per_page=100"), self.token)
                     or {}).get("check_runs") or [])

    def statuses(self, sha: str) -> list[dict]:
        return list((self._get(self._url(f"/commits/{sha}/status"), self.token) or {})
                    .get("statuses") or [])

    def has_workflows(self, sha: str) -> bool:
        import httpx

        try:
            found = self._get(self._url(f"/contents/.github/workflows?ref={sha}"), self.token)
        except httpx.HTTPStatusError as error:
            if error.response is not None and error.response.status_code == 404:
                return False
            raise
        return bool(isinstance(found, list) and any(
            str(f.get("name", "")).endswith((".yml", ".yaml")) for f in found))

    def pull_request(self, number: int) -> dict:
        return self._get(self._url(f"/pulls/{number}"), self.token) or {}


class AdapterCiReader:
    """Reads CI through a GitHubAdapter (the build's own repository and credential)."""

    def __init__(self, github: Any):
        self.github = github

    def _get(self, path: str) -> Any:
        return self.github._request(  # noqa: SLF001 — the adapter's one HTTP edge
            "GET", f"/repos/{self.github.repository_full_name}{path}")

    def check_runs(self, sha: str) -> list[dict]:
        return list((self._get(f"/commits/{sha}/check-runs?per_page=100") or {})
                    .get("check_runs") or [])

    def statuses(self, sha: str) -> list[dict]:
        return list((self._get(f"/commits/{sha}/status") or {}).get("statuses") or [])

    def has_workflows(self, sha: str) -> bool:
        import httpx

        try:
            found = self._get(f"/contents/.github/workflows?ref={sha}")
        except httpx.HTTPStatusError as error:
            if error.response is not None and error.response.status_code == 404:
                return False
            raise
        return bool(isinstance(found, list) and any(
            str(f.get("name", "")).endswith((".yml", ".yaml")) for f in found))


class UnreadableCi:
    """A reader for when GitHub cannot be reached: every look fails, so a wait still times out."""

    def __init__(self, reason: str):
        self.reason = reason

    def check_runs(self, sha: str) -> list[dict]:
        raise RuntimeError(self.reason)

    statuses = check_runs

    def has_workflows(self, sha: str) -> bool:
        raise RuntimeError(self.reason)


def reader_for_project(connection: Any, settings: Any, project_id: str,
                       repository: Optional[str] = None) -> HttpCiReader:
    """The project's GitHub, as Setup connected it. Tests replace this function."""
    from .kit_connections import github_state, github_token

    state = github_state(connection, settings, project_id)
    if not state.get("connected") or not (repository or state.get("repo")):
        raise CiSettingError("github_not_connected", 409,
                             "This project has no GitHub repository connected. Connect it in Setup.")
    token = github_token(connection, settings, project_id)
    return HttpCiReader(state.get("api_base") or "https://api.github.com",
                        repository or state["repo"], token)


_PR_URL = re.compile(r"https?://[^/]+/([^/]+/[^/]+)/pull/(\d+)")


def parse_pr_url(url: str) -> Optional[tuple[str, int]]:
    m = _PR_URL.search(str(url or ""))
    return (m.group(1), int(m.group(2))) if m else None


# ---- the wait, recorded (ci_waits) ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def start_wait(connection: Any, *, project_id: str, repository: str, head_sha: str,
               pull_request_url: Optional[str] = None, timeout_seconds: Optional[int] = None,
               has_ci: Optional[bool] = None, code_task_run_id: Optional[str] = None,
               workflow_run_id: Optional[str] = None, actor: str = "boss",
               reader: Optional[CiReader] = None) -> dict[str, Any]:
    from .core import emit_event, make_id

    if not head_sha:
        raise CiSettingError("head_sha_required", 422, "There is no commit to wait for CI on.")
    timeout = int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)
    if timeout < 30 or timeout > 6 * 3600:
        raise CiSettingError("bad_timeout", 422, "The CI wait must be between 30 s and 6 h.")
    if has_ci is None:
        has_ci = _safe_has_workflows(reader, head_sha) if reader is not None else True
    wait_id = make_id("ciw")
    now = _now()
    connection.execute(text(
        "INSERT INTO ci_waits (id, project_id, repository, head_sha, pull_request_url, "
        "code_task_run_id, workflow_run_id, status, has_ci, timeout_seconds, deadline_at, "
        "next_poll_at, created_by, created_at, updated_at) VALUES (:id, :p, :r, :s, :u, :ct, "
        ":wf, 'waiting', :h, :t, :d, :n, :by, :n, :n)"),
        {"id": wait_id, "p": project_id, "r": repository, "s": head_sha, "u": pull_request_url,
         "ct": code_task_run_id, "wf": workflow_run_id, "h": bool(has_ci), "t": timeout,
         "d": now + timedelta(seconds=timeout), "n": now, "by": actor})
    emit_event(connection, project_id, "ci.wait_started", actor, "ci_wait", wait_id,
               {"repository": repository, "head_sha": head_sha,
                "pull_request_url": pull_request_url, "timeout_seconds": timeout,
                "has_ci": bool(has_ci)})
    return get_wait(connection, wait_id)


def get_wait(connection: Any, wait_id: str) -> dict[str, Any]:
    row = connection.execute(text("SELECT * FROM ci_waits WHERE id = :i"),
                             {"i": wait_id}).mappings().first()
    if row is None:
        raise CiSettingError("ci_wait_not_found", 404, "No such CI wait.")
    out = dict(row)
    if isinstance(out.get("checks"), str):
        out["checks"] = json.loads(out["checks"])
    for k in ("deadline_at", "next_poll_at", "finished_at", "created_at", "updated_at"):
        if isinstance(out.get(k), datetime):
            out[k] = out[k].isoformat()
    out["blocks_deploy"] = out["status"] not in CLEARED
    return out


def poll_wait(connection: Any, wait_id: str, reader: CiReader, *, force: bool = False,
              now: Optional[datetime] = None) -> dict[str, Any]:
    """One look at CI for a recorded wait, when it is due (or `force`). Never blocks."""
    from .core import emit_event

    row = connection.execute(text("SELECT * FROM ci_waits WHERE id = :i FOR UPDATE"),
                             {"i": wait_id}).mappings().first()
    if row is None:
        raise CiSettingError("ci_wait_not_found", 404, "No such CI wait.")
    if row["status"] in TERMINAL:
        return get_wait(connection, wait_id)
    now = now or _now()
    if not force and row["next_poll_at"] and row["next_poll_at"] > now:
        return get_wait(connection, wait_id)
    try:
        outcome = look(reader, row["head_sha"])
    except Exception as error:  # noqa: BLE001
        outcome = CiOutcome("waiting", f"Could not read CI yet: {type(error).__name__}.", [])
    has_ci = bool(row["has_ci"] or outcome.checks)
    elapsed = (now - row["created_at"]).total_seconds()
    outcome = settle(outcome, elapsed=elapsed, timeout=float(row["timeout_seconds"]),
                     has_ci=has_ci, repository=row["repository"])
    attempts = int(row["attempts"] or 0) + 1
    connection.execute(text(
        "UPDATE ci_waits SET status = :st, summary = :su, checks = CAST(:c AS JSONB), "
        "failing_url = :f, has_ci = :h, attempts = :a, next_poll_at = :np, "
        "finished_at = CASE WHEN :term THEN :now ELSE NULL END, updated_at = :now WHERE id = :i"),
        {"st": outcome.state, "su": outcome.summary[:1000], "c": json.dumps(outcome.checks),
         "f": outcome.failing_url, "h": has_ci, "a": attempts,
         "np": now + timedelta(seconds=next_interval(attempts)), "term": outcome.terminal,
         "now": now, "i": wait_id})
    if outcome.terminal:
        emit_event(connection, row["project_id"], f"ci.{outcome.state}", "boss", "ci_wait",
                   wait_id, {"summary": outcome.summary, "failing_url": outcome.failing_url,
                             "pull_request_url": row["pull_request_url"],
                             "head_sha": row["head_sha"], "checks": len(outcome.checks)})
    return get_wait(connection, wait_id)


def run_blocking(begin: Callable[[], Any], wait_id: str, reader: CiReader, *,
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Poll a recorded wait until it is terminal, each look in its own short transaction (a
    30-minute wait never holds a transaction open). Bounded by the wait's own deadline."""
    attempt = 0
    while True:
        with begin() as connection:
            got = poll_wait(connection, wait_id, reader, force=True)
        if got["status"] in TERMINAL:
            return got
        sleep(next_interval(attempt))
        attempt += 1


def latest_wait(connection: Any, project_id: str, *, pull_request_url: Optional[str] = None,
                shas: Iterable[str] = ()) -> Optional[dict[str, Any]]:
    shas = [s for s in shas if s]
    if not pull_request_url and not shas:
        return None
    try:
        row = _one(connection,
                   "SELECT id FROM ci_waits WHERE project_id = :p AND status <> 'cancelled' AND ("
                   "(CAST(:u AS TEXT) IS NOT NULL AND pull_request_url = :u) "
                   "OR head_sha = ANY(CAST(:s AS TEXT[]))) ORDER BY created_at DESC LIMIT 1",
                   {"p": project_id, "u": pull_request_url, "s": shas})
    except Exception:  # noqa: BLE001 — no ci_waits table yet: no CI gate
        return None
    return get_wait(connection, row["id"]) if row else None


# ---- the gates -----------------------------------------------------------------------------------

class CiGateRefused(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def _rec(row: Mapping[str, Any]) -> dict[str, Any]:
    rec = row.get("recommendation")
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = {}
    return dict(rec or {})


def _shas(rec: Mapping[str, Any]) -> list[str]:
    return [str(rec[k]) for k in ("head_sha", "merged_sha", "commit", "commit_ref")
            if rec.get(k) and re.fullmatch(r"[0-9a-f]{7,40}", str(rec[k]))]


def ci_changed_for(connection: Any, project_id: str, rec: Mapping[str, Any]) -> Optional[dict]:
    """The CI-changed review this decision ships, if any: its own flag, or (for a deploy) the
    review_pull_request decision of the same pull request / commit that carries the flag."""
    if rec.get("ci_changed") or rec.get("requires_person_approval"):
        return {"files": rec.get("ci_changed_files") or [], "source": "self"}
    url, shas = rec.get("pull_request_url"), _shas(rec)
    if not url and not shas:
        return None
    try:
        row = _one(connection,
                   "SELECT id, recommendation->'ci_changed_files' AS files FROM decisions "
                   "WHERE project_id = :p AND recommendation->>'ci_changed' = 'true' AND ("
                   "(CAST(:u AS TEXT) IS NOT NULL AND recommendation->>'pull_request_url' = :u) "
                   "OR recommendation->>'head_sha' = ANY(CAST(:s AS TEXT[]))) "
                   "ORDER BY created_at DESC LIMIT 1", {"p": project_id, "u": url, "s": shas})
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    files = row["files"]
    if isinstance(files, str):
        files = json.loads(files)
    return {"files": files or [], "source": row["id"]}


_AGENTISH = re.compile(r"^(agt_|agent[:_]|run_|svc_|sa_|key_|boss$|workflow|system|auto_approval$)")


def is_person(connection: Any, project_id: str, actor: str, identity_ctx: Any = None) -> bool:
    """Whether this approval is a PERSON's: never a policy, an agent, an API key or a run token.

    Identity first (a verified context must be a human session; an MCP confirmation counts as the
    person who confirmed it); then the actor itself (agent-shaped names and non-human members are
    refused)."""
    ctx = identity_ctx
    if ctx is None:
        try:
            from ..identity import lineage

            ctx = lineage.current_context()
            if ctx is None:
                from ..tools.registry import CURRENT_TOOL_AUTH

                ctx = CURRENT_TOOL_AUTH.get()
        except Exception:  # noqa: BLE001
            ctx = None
    if ctx is not None:
        try:
            from ..identity.approvals import MCP_REVIEWER

            if not MCP_REVIEWER.get():
                if getattr(ctx, "run_token", None) is not None:
                    return False            # an agent's run token
                if getattr(ctx, "credential", None) is not None and not ctx.is_human_session:
                    return False            # an API key / service account
                if getattr(ctx, "legacy", None) is None and not ctx.is_human_session:
                    return False            # verified, but not a person signed in
        except Exception:  # noqa: BLE001 — an unreadable context is not a person
            return False
    return actor_is_person(connection, project_id, actor)


def actor_is_person(connection: Any, project_id: str, actor: str) -> bool:
    """The recorded actor names a person: not agent-shaped, and not a non-human member."""
    name = str(actor or "").strip()
    if not name or _AGENTISH.match(name.lower()):
        return False
    try:
        row = _one(connection, "SELECT member_type FROM project_members WHERE project_id = :p "
                               "AND member_id = :m", {"p": project_id, "m": name})
        kind = row["member_type"] if row else None
    except Exception:  # noqa: BLE001
        kind = None
    return kind in (None, "human")


def refuse_approval(connection: Any, row: Mapping[str, Any], actor: str, *,
                    identity_ctx: Any = None, system_policy: bool = False,
                    force: bool = False) -> None:
    """Raise CiGateRefused when this approval may not be given. Called on every approve
    (decisions.transition) and on every policy approval (approve_by_policy, system_policy=True)."""
    rec = _rec(row)
    action = str(rec.get("action") or "")
    project_id = str(row.get("project_id") or "")
    if action not in DEPLOY_ACTIONS | MERGE_ACTIONS and not rec.get("requires_person_approval"):
        return
    changed = ci_changed_for(connection, project_id, rec)
    if changed is not None and (system_policy or not is_person(connection, project_id, actor,
                                                                identity_ctx)):
        raise CiGateRefused(
            "person_approval_required",
            "CI changed: this build edits " + (", ".join(changed["files"][:3]) or
                                               ".github/workflows/")
            + ". Only a person signed in to BOSS can approve it — not an agent, a policy, an API "
              "key or a run token.", ci_changed_files=changed["files"])
    if action not in DEPLOY_ACTIONS | MERGE_ACTIONS:
        return
    wait = latest_wait(connection, project_id, pull_request_url=rec.get("pull_request_url"),
                       shas=_shas(rec))
    if wait is None or wait["status"] in CLEARED:
        return
    if action in MERGE_ACTIONS and force:
        return       # a person overriding the merge gate with a note (decisions.transition)
    what = "deploy" if action in DEPLOY_ACTIONS else "merge"
    if wait["status"] == "waiting":
        msg = f"CI is still running on this change, so the {what} cannot be approved yet. {wait['summary']}"
    else:
        msg = f"{wait['summary']}. The {what} cannot be approved until CI passes."
    raise CiGateRefused("ci_not_passed", msg.replace("..", "."), ci_wait_id=wait["id"],
                        ci_status=wait["status"], failing_url=wait.get("failing_url"))


def refuse_effect(connection: Any, project_id: str, row: Mapping[str, Any]) -> None:
    """At the GitHub effect itself (merge, deployment): the approval that released it must have
    been a person's when CI changed, and a deploy needs CI cleared. Defence in depth for a
    decision that reached `approved` by any path."""
    rec = _rec(row)
    action = str(rec.get("action") or "")
    approved_by = str(row.get("approved_by") or "")
    changed = ci_changed_for(connection, project_id, rec)
    if changed is not None and approved_by and not actor_is_person(connection, project_id,
                                                                    approved_by):
        raise CiGateRefused("person_approval_required",
                            f"CI changed, and '{approved_by}' is not a person: a person must "
                            "approve this before it is merged or deployed.")
    if changed is not None and not approved_by:
        raise CiGateRefused("person_approval_required",
                            "CI changed: no person's approval is recorded for this change.")
    if action in DEPLOY_ACTIONS:
        wait = latest_wait(connection, project_id, pull_request_url=rec.get("pull_request_url"),
                           shas=_shas(rec))
        if wait is not None and wait["status"] not in CLEARED:
            raise CiGateRefused("ci_not_passed",
                                f"{wait['summary']}. Deploy refused until CI passes.",
                                ci_wait_id=wait["id"], failing_url=wait.get("failing_url"))
