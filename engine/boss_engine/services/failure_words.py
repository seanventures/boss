"""EVERY FAILURE SAYS WHAT HAPPENED AND WHAT TO DO (BOSSAPP-1428).

Failures reached people as a symptom with no cause and no next step: "BOSS couldn't save this to
its database. Try again." for a full disk, "The cause is not established: inspect the
test-author tool output" for a test author that wrote nothing, "GitHub did not list the App's
repositories. Try again." for an App that was not installed. Each was true and none was usable.

THE CONTRACT, one function every person-facing failure sentence is checked with
(engine/tests/test_every_failure_says_what_and_what_to_do.py):

  problems(what=..., action=..., where=...) -> []   when the sentence is usable, else why not.

  * `what`    says what happened in plain words — not a bare "Failed." / "Something went wrong",
              and never a deflection ("the cause is not established", "inspect the … output");
  * `action`  names the next action and WHERE: a console area ("Setup › Models", "Govern",
              "Infra"), a backticked `boss …` command, this card / this step / Details, or a
              link (`where`). BOSS acting by itself ("Nothing to do — it resumes by itself") is
              an action too. A bare "Try again." is not;
  * neither   names an internal field (`extends_run_id`, `failure_class`) outside backticks;
  * neither   carries a secret (the text equals its own redaction).

`VAGUE` is the one list of phrases that tell a person nothing; failure_guidance (the failure-owner
cards) and the source lint read the same list.
"""
from __future__ import annotations

import re
from typing import Optional

#: Phrases that tell a person nothing they can act on, wherever they appear.
VAGUE_PHRASES: tuple[str, ...] = (
    "something went wrong",
    "couldn't do that",
    "could not do that",
    "that did not work",
    "that didn't work",
    "the action failed",
    "an error occurred",
    "an unknown error",
    "unknown error",
    "cause is not established",
    "check the boss host is healthy",
    "do what its page says",
    "check what it names",
    "only a person can do what it needs",
)

#: The same phrases as one pattern, plus "inspect the … output" (a deflection: the sentence
#: should say what the output shows). Exported for the source lint.
VAGUE = re.compile(
    r"(?:" + "|".join(re.escape(p) for p in VAGUE_PHRASES if not p.startswith("that did"))
    + r")"
    # "That did not work." as its own sentence — not "an approach that did not work".
    + r"|(?:^|[.!?]\s+)that did(?: not|n't) work\b"
    + r"|\binspect the [\w -]{1,40} output\b",
    re.I,
)

#: A `what` that is nothing but a status word.
_BARE_WHAT = re.compile(r"^\W*(?:it )?(?:failed|error|failure|oops|not ok|did not work)\W*$", re.I)

#: The console's own places (apps/web/app/ConsoleShell.tsx nav labels, and the names engine
#: sentences already use for them). A next step that names one says where to act.
AREAS: tuple[str, ...] = (
    "Command Center", "Projects", "Project", "Code Tasks", "Agents at work", "Workflows",
    "Workflow", "Agent Studio", "Knowledge Graph", "Approvals", "Decisions", "Infra",
    "Observability", "Activity log", "Activity Log", "Activity", "Self-fixes", "Self-fix",
    "Research", "Panels", "Meetings", "BOSS Agent", "Task Board", "Setup", "Get started",
    "Skills & MCP", "Govern", "Operations", "Assurance", "Access and packs", "GitHub access",
    "Capabilities", "Sandboxes", "My account", "Identity", "Passport", "Inbox",
)
_AREA = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in AREAS) + r")\b")

#: Places on the very thing the person is looking at.
_HERE = re.compile(
    r"\bthis (?:card|step|build|run|page|form|row)\b|\bDetails\b|\bDone — retry\b|"
    r"\bRetry on\b|\bpress (?:Retry|Sign in|Reconnect|Test|Re-check)\b|\bthe build's card\b|"
    r"\bits card\b|\breload\b|\bthe run's activity\b|\bon the host\b|\bthe BOSS host\b",
    re.I,
)

#: A command to run, in backticks: `boss doctor`, `boss toolchains enable go`.
_COMMAND = re.compile(r"`(?:boss|docker|git|gh) [^`]+`")

#: BOSS acts by itself: no person needed, and it says so.
_BY_ITSELF = re.compile(r"\bnothing to do\b|\bnothing is needed\b|\bby itself\b|"
                        r"\bon (?:its|their) own\b", re.I)

#: A next step that is only "try again".
_BARE_RETRY = re.compile(r"^\W*(?:please )?(?:try again|retry)(?: (?:later|shortly|now))?\W*$",
                         re.I)

#: An internal identifier: lower snake_case (`extends_run_id`, `failure_class`).
#: (A record id such as `crun_5f8a…` carries digits and is something a person can search for.)
_SNAKE = re.compile(r"(?<![\w./=&?-])[a-z][a-z]*(?:_[a-z]+)+(?![\w/-]|\.\w)")
_BACKTICKED = re.compile(r"`[^`]*`")
_URL = re.compile(r"(?:https?://|/\?|\?area=)\S*")


def _internal_names(text: str) -> list[str]:
    plain = _URL.sub(" ", _BACKTICKED.sub(" ", text or ""))
    return _SNAKE.findall(plain)


def _leaks(text: str) -> bool:
    from .redaction import redact_secrets

    return bool(text) and redact_secrets(text) != text


def vague_in(text: str) -> list[str]:
    """The vague phrases a text carries (empty when none)."""
    return [m.group(0) for m in VAGUE.finditer(text or "")]


def problems(*, what: str, action: str, where: Optional[str] = None) -> list[str]:
    """Why this failure sentence is not usable by a person; [] when it is."""
    out: list[str] = []
    what_s, action_s = " ".join(str(what or "").split()), " ".join(str(action or "").split())
    if not what_s:
        out.append("what: empty — say what happened")
    elif _BARE_WHAT.match(what_s):
        out.append(f"what: bare status {what_s!r} — say what happened")
    if not action_s:
        out.append("action: empty — name the next action and where")
    elif _BARE_RETRY.match(action_s):
        out.append(f"action: bare {action_s!r} — say why, and where to act")
    elif not (_AREA.search(action_s) or _HERE.search(action_s) or _COMMAND.search(action_s)
              or _BY_ITSELF.search(action_s) or str(where or "").strip()):
        out.append(f"action: no place — {action_s[:120]!r} names no console area, command, "
                   "card or link")
    for field, text in (("what", what_s), ("action", action_s)):
        for phrase in vague_in(text):
            out.append(f"{field}: vague {phrase!r}")
        for name in _internal_names(text):
            out.append(f"{field}: internal name {name!r} (say it in words, or backtick it)")
        if _leaks(text):
            out.append(f"{field}: carries a secret")
    return out


# ── shared wordings ─────────────────────────────────────────────────────────────────────────

def short(text: object, n: int = 160) -> str:
    """One line of someone else's words, redacted and cut: safe to quote to a person."""
    from .redaction import redact_secrets

    line = " ".join(str(text or "").split())
    try:
        line = redact_secrets(line)
    except Exception:  # noqa: BLE001 — never quote what could not be redacted
        return ""
    return line if len(line) <= n else line[: n - 1].rstrip() + "…"


def http_status_of(error: object) -> Optional[int]:
    """The HTTP status a failure carries — GitHub's answer, not BOSS's own reply code: an
    `extra['status']` (GitHubSetupError), `.status_code`/`.code` (requests, urllib), or a
    "status 403"/"HTTP 401" in its words."""
    extra = getattr(error, "extra", None)
    if isinstance(extra, dict) and str(extra.get("status") or "").isdigit():
        return int(extra["status"])
    for attr in ("status_code", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None) or getattr(response, "status", None)
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    found = re.search(r"\b(?:status|HTTP)[ =:]*([1-5]\d\d)\b", str(error or ""), re.I)
    return int(found.group(1)) if found else None


def _timed_out(error: object) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    name = type(error).__name__.lower()
    return "timeout" in name or "connecterror" in name or bool(
        re.search(r"timed? ?out|connection (?:refused|reset)|unreachable|name or service not "
                  r"known", str(error or ""), re.I))


def github_refusal(what: str, error: object = None, *, status: Optional[int] = None,
                   account: str = "") -> str:
    """"GitHub did not list <what>" WITH GitHub's answer and the way out — the one wording for
    every GitHub listing that failed (Setup's pickers, the repository registry, an account's
    repositories). `error` is the exception in scope; `status` GitHub's HTTP status when the
    caller already has it."""
    code = status if status is not None else (http_status_of(error) if error is not None else None)
    who = f" for {account}" if account else ""
    lead = f"GitHub did not list {what}{who}"
    if code == 401:
        return (f"{lead}: it refused BOSS's sign-in (HTTP 401). Reconnect the GitHub account in "
                "Setup › Repositories, then open the list again.")
    if code == 403:
        return (f"{lead}: the GitHub App is not installed on that account or lacks access "
                "(HTTP 403). Install the App on it (Setup › Add a GitHub account), then "
                "open the list again.")
    if code == 404:
        return (f"{lead}: GitHub does not know that installation any more (HTTP 404) — the App "
                "was probably uninstalled. Install it again from Setup › Repositories.")
    if code == 429:
        return (f"{lead}: GitHub rate-limited BOSS (HTTP 429). Nothing to change: open the "
                "list in Setup › Repositories again in a few minutes.")
    if (code is not None and code >= 500) or (error is not None and _timed_out(error)):
        said = f"HTTP {code}" if code else "no answer"
        return (f"{lead}: GitHub is unavailable right now ({said}). Nothing to change: open "
                "the list in Setup › Repositories again in a minute; if it lasts, check "
                "githubstatus.com.")
    if code is not None:
        return (f"{lead}: GitHub answered HTTP {code}. Press Test on the account in Setup › "
                "Repositories to see what it reports.")
    return (f"{lead}: BOSS got no usable answer from GitHub. Press Test on the account in "
            "Setup › Repositories to see what it reports.")


__all__ = ["AREAS", "VAGUE", "VAGUE_PHRASES", "github_refusal", "http_status_of", "problems",
           "short", "vague_in"]
