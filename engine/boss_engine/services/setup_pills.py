"""Every state Repo and models can show, and the one way each is said.

The page used to have five verdicts for one question — a header chip, step ticks, card badges,
table dots and a sentence under each form — and they disagreed, because each card judged for
itself. Now the server judges once (`setup_view`) and every reader renders a PILL from this
catalogue: the console, the Command Center, workflow readiness and the agent's diagnose_setup.

A state the checks can produce and this catalogue does not name is a defect
(`test_every_setup_state_has_one_pill`): label at most three words, tip at most fourteen.

Labels and tips may carry `{n}` (days), `{time}` (HH:MM UTC), `{repo}`, `{model}`, `{host}`,
`{date}` and `{provider_fix}`; `pill()` fills them.
"""
from __future__ import annotations

from typing import Any, Optional

#: tone -> what it means. "busy" is the spinner.
TONES = ("green", "amber", "red", "grey", "busy")

#: Actions a pill may offer. The console maps each to one control.
ACTIONS = ("connect", "test", "replace", "fix_link", "budget", "default_model")

#: GitHub pages a fix links to. The token list is where a fine-grained token's permissions and
#: repositories are edited; the org page is where an owner approves a pending token request.
#: Both come from identity/github_urls (the one builder); the org's requests page needs the
#: repository's owner, so it is built when the pill is filled (`{repo}`).
from ..identity import github_urls as _gh

GITHUB_TOKENS_URL = _gh.fine_grained_tokens()


def _org_requests(values: dict[str, Any]) -> str:
    owner = str(values.get("repo") or "").split("/", 1)[0] if "/" in str(values.get("repo") or "") else None
    return _gh.token_requests(owner)

REPOSITORY: dict[str, dict[str, Any]] = {
    "not_connected": {"label": "Not connected", "tone": "grey",
                      "tip": "BOSS needs a repository to open pull requests.",
                      "action": "connect"},
    "checking": {"label": "Checking…", "tone": "busy",
                 "tip": "Asking GitHub what this token can do."},
    "working": {"label": "Working", "tone": "green",
                "tip": "Can read, write a branch and open a pull request."},
    "cant_write": {"label": "Can't write", "tone": "red",
                   "tip": "Token needs Contents: Read and write.",
                   "action": "fix_link", "fix_url": GITHUB_TOKENS_URL},
    "cant_open_prs": {"label": "Can't open PRs", "tone": "red",
                      "tip": "Token needs Pull requests: Read and write.",
                      "action": "fix_link", "fix_url": GITHUB_TOKENS_URL},
    "no_access": {"label": "No access", "tone": "red",
                  "tip": "This token can't see {repo}. Add it to the token's repositories.",
                  "action": "fix_link", "fix_url": GITHUB_TOKENS_URL},
    "token_rejected": {"label": "Token rejected", "tone": "red",
                       "tip": "GitHub refused this token. It was revoked or mistyped.",
                       "action": "replace"},
    "expiring": {"label": "Expires in {n}d", "tone": "amber",
                 "tip": "Builds stop when it expires. Make a new token now.",
                 "action": "replace"},
    "expired": {"label": "Expired", "tone": "red",
                "tip": "Token expired on {date}. Paste a new one.",
                "action": "replace"},
    "awaiting_org_approval": {"label": "Awaiting org approval", "tone": "amber",
                              "tip": "Your org must approve this token on GitHub.",
                              "action": "fix_link", "fix_url": _org_requests},
    "rate_limited": {"label": "GitHub busy · {time}", "tone": "amber",
                     "tip": "GitHub rate limit. Checks resume at {time}."},
    "unreachable": {"label": "Can't reach GitHub", "tone": "amber",
                    "tip": "The server can't reach api.github.com.",
                    "action": "test"},
}

MODEL: dict[str, dict[str, Any]] = {
    "working": {"label": "Working", "tone": "green",
                "tip": "Answered a test question {age} ago."},
    "checking": {"label": "Checking…", "tone": "busy",
                 "tip": "Asking one tiny question. Budgeted."},
    "not_checked": {"label": "Not checked", "tone": "grey",
                    "tip": "Saved before checks existed. Press Test.",
                    "action": "test"},
    "key_rejected": {"label": "Key rejected", "tone": "red",
                     "tip": "The provider refused this key.",
                     "action": "replace"},
    "login_expired": {"label": "Login expired", "tone": "red",
                      "tip": "{provider_fix}",
                      "action": "replace"},
    # BOSS's own failure, never the provider's: the stored key could not be decrypted (a changed
    # master key). The provider was not asked, so it is not "Key rejected".
    "key_unreadable": {"label": "Can't read key", "tone": "red",
                       "tip": "BOSS could not decrypt the stored key. Save it again.",
                       "action": "replace"},
    "no_credit": {"label": "No credit", "tone": "red",
                  "tip": "Add credit at the provider, then Test.",
                  "action": "fix_link"},
    "rate_limited": {"label": "Limited · {time}", "tone": "amber",
                     "tip": "Plan limit reached. Auto uses another model until {time}."},
    "model_not_found": {"label": "Model not found", "tone": "red",
                        "tip": "{model} isn't offered by this key. Pick another.",
                        "action": "default_model"},
    "unreachable": {"label": "Can't reach", "tone": "amber",
                    "tip": "The server can't reach {host}.",
                    "action": "test"},
    # {cap_name} is the cap that is actually binding (Daily cap / Monthly cap / Burst cap) —
    # never hardcoded to "Monthly": a daily or burst cap can refuse a call while the monthly
    # one has room left, and a tip naming the wrong cap sends the operator to raise the wrong
    # one (operator report, boss-1 rc10.9: budget was fine, the pill still said "Monthly").
    "paused_budget": {"label": "Paused · budget", "tone": "amber",
                      "tip": "{cap_name} reached. Raise it or wait for reset.",
                      "action": "budget"},
    "not_on_server": {"label": "Not on server", "tone": "grey",
                      "tip": "Install and sign in on the BOSS server first."},
    # A check that failed for a reason none of the above names. Still one pill, still a fix.
    "failed": {"label": "Check failed", "tone": "red",
               "tip": "The test question failed. Press Test to retry.",
               "action": "test"},
}

#: Per-provider fix for an expired subscription login (the `login_expired` tip).
LOGIN_FIX = {
    "claude_code": "Make a new token with claude setup-token.",
    "codex": "Run codex login on your computer, then drop auth.json.",
    "grok": "Sign in to Grok on the BOSS server again.",
    "cursor": "Sign in to Cursor on the BOSS server again.",
}
DEFAULT_LOGIN_FIX = "Sign in again, then press Test."

BUDGET: dict[str, dict[str, Any]] = {
    "ok": {"label": "OK", "tone": "green", "tip": "{spent} of {cap}. Metered work runs."},
    "near": {"label": "Near cap", "tone": "amber",
             "tip": "{spent} of {cap}. Metered work pauses at the cap."},
    "spent": {"label": "Spent", "tone": "red",
              "tip": "Cap reached. Metered models paused until {date}."},
}

#: The header chip.
VERDICT: dict[str, dict[str, Any]] = {
    "ready": {"label": "Ready", "tone": "green",
              "tip": "Repository and model checked. Ready to build."},
    "to_go": {"label": "{n} to go", "tone": "grey",
              "tip": "Connect {missing} to start."},
    "problem": {"label": "{n} problem", "tone": "red", "tip": "{headline}"},
    "problems": {"label": "{n} problems", "tone": "red", "tip": "{headline}"},
    "budget_spent": {"label": "Budget spent", "tone": "red",
                     "tip": "Cap reached. Metered models paused until {date}."},
}

#: Try it: one sentence becomes a pull request.
TRY: dict[str, dict[str, Any]] = {
    "planning": {"label": "Planning…", "tone": "busy", "tip": "Reading the repository."},
    "building": {"label": "Building…", "tone": "busy", "tip": "Writing the change and its tests."},
    "testing": {"label": "Testing…", "tone": "busy", "tip": "Running the tests and the review."},
    "pr_open": {"label": "PR #{n} ↗", "tone": "green", "tip": "Your first PR is open."},
    "failed": {"label": "Failed", "tone": "red", "tip": "{headline}"},
}

CATALOGUES = {"repository": REPOSITORY, "model": MODEL, "budget": BUDGET, "verdict": VERDICT,
              "try": TRY}


class _Blank(dict):
    def __missing__(self, key: str) -> str:  # an unfilled slot reads as nothing, never "{x}"
        return ""


def _untimed(template: str, values: dict[str, Any]) -> str:
    """A tip that names a time nobody knows drops the phrase ("until ." never reaches a page)."""
    if values.get("time") or "{time}" not in template:
        return template
    import re

    return re.sub(r"\s+(?:until|at)\s+\{time\}", "", template)


def pill(kind: str, state: str, **values: Any) -> dict[str, Any]:
    """The pill for `state` in catalogue `kind`, with its slots filled. Unknown state: KeyError,
    so a new state cannot reach the page without a label and a tip."""
    entry = CATALOGUES[kind][state]
    fill = _Blank({k: ("" if v is None else str(v)) for k, v in values.items()})
    out: dict[str, Any] = {
        "state": state,
        "tone": entry["tone"],
        "label": entry["label"].format_map(fill).strip(),
        "tip": " ".join(_untimed(entry["tip"], values).format_map(fill).split()),
    }
    if entry.get("action"):
        out["action"] = entry["action"]
    fix_url = values.get("fix_url") or entry.get("fix_url")
    if callable(fix_url):
        fix_url = fix_url(values)
    if fix_url:
        out["fix_url"] = fix_url
    # A TIME IN A PILL IS THE VIEWER'S CLOCK. The label carries the server's HH:MM (UTC in a
    # container); `at` is the instant, so the console rewrites it in the browser's own zone.
    if values.get("at") and "{time}" in entry["label"] + entry["tip"]:
        out["at"] = str(values["at"])
    if "{time}" in entry["label"] and not values.get("time"):
        out["label"] = out["label"].replace(" · ", "").strip(" ·")
    return out
