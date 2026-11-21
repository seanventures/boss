"""WHEN A PERSON MUST ACT, THE CARD SAYS EXACTLY WHAT TO DO (operator, 2026-11-21).

"... please fix the Agent being autonomously fixing the failure or if it's blocked worst case it
should provide the guidance for humans to fix the issue". A needs-person card that said "Check
the BOSS host is healthy (Infra › Doctor), then press Done — retry" about a contract case the
app could never pass (northgate-gateway, dec_40f3c93578a845cebd27) gave the person nothing to do.

Every failure-owner card now carries FOUR fields, built here and enforced by
engine/tests/test_needs_person_exits_are_justified.py:
  * what_failed  one line: what stopped, in its own words;
  * why          the evidence: why BOSS's agents could not do it themselves;
  * steps        the exact steps for the person — where to click, or the command to run;
  * after        what BOSS does once they have.
Generic advice ("check the host", "do what its page says") is refused by `problems()`.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

#: Phrases that tell a person nothing they can act on. A card carrying one fails the guard.
GENERIC = ("check the boss host is healthy", "do what its page says", "check what it names",
           "only a person can do what it needs", "something went wrong")

RETRY = "Press Done — retry on this card."
AFTER = ("BOSS runs it again as you and owns it until it works; if it fails the same way, this "
         "card comes back with what BOSS saw.")

#: cause code → (steps, after). `{cause}`, `{title}`, `{toolchain}`, `{receipt}` are filled in.
_STEPS: dict[str, tuple[tuple[str, ...], str]] = {
    "budget": (("Open Govern › Budget.",
                "Raise this project's spending cap above what it has spent (the card's cause "
                "names the cap it hit).", RETRY), AFTER),
    "no_repository": (("Open Setup › Repositories.",
                       "Connect the repository this project builds, or install the GitHub App "
                       "on it.", RETRY), AFTER),
    "repo_access": (("Open Setup › Repositories and press Test on the repository.",
                     "If it fails, give BOSS's GitHub account (or the App installation) access "
                     "to that repository in GitHub's settings.", RETRY), AFTER),
    "toolchain": (("On the BOSS host run `boss toolchains enable {toolchain}`.", RETRY), AFTER),
    "registry_access": (("Put the package registry's (or proxy's) credential on the BOSS host as "
                         "docs/community/ENGINE_SANDBOX.md \"Package registries\" describes.",
                         "Run `boss doctor` and confirm the registry check passes.", RETRY),
                        AFTER),
    "connector_login": (("Open Setup › Connectors (MCP) and press Reconnect on the connector "
                         "the cause names; sign in when it asks.", RETRY), AFTER),
    "model_login": (("Open Setup › Models.",
                     "On the model the cause names, sign in again (or paste a working API key) "
                     "and press Test.", RETRY), AFTER),
    "grant": (("Open Settings and set 'Agents fix failures with your authority' to 1 (or, if "
               "you turned it off on purpose, open My account › Agents acting for me and let "
               "the agent act for you on this project).", RETRY), AFTER),
    "approval_expired": ((RETRY + " The step asks for its approval again.",
                          "Open Approvals and answer it before it expires."),
                         "BOSS carries on from that step as soon as you approve."),
    "merge_conflict": (("Open the build that made this change and press Retry with the note "
                        "'Update from the base branch and resolve the conflict, keeping both "
                        "sides'.", "When it passes, merge it from its card."),
                       "BOSS's build agent pushes the updated branch to the same change and "
                       "asks you to merge again."),
    "host": (("Open Setup › Hosts and press Test on the host the cause names.",
              "Fix what it reports there (SSH access, sudo without a password, or free disk).",
              RETRY), AFTER),
    "held_proof": (("Open Setup › Models; on the held check for this model (receipt "
                    "{receipt}) press Review, then Release with a one-line reason.",),
                   "Connect works again for that model; BOSS re-checks it on its schedule."),
    "old": (("Read what it was for: '{title}'.",
             "If it is still wanted, " + RETRY[0].lower() + RETRY[1:],
             "If not, press Leave it."), "BOSS runs it once more now, as you, and owns it."),
    "platform_spent": (("On the BOSS host run `boss doctor` and fix any check it marks failed.",
                        "If a newer BOSS release is out, run `boss upgrade`: BOSS recorded this "
                        "failure as its own bug, so a release may carry the fix.", RETRY),
                       "BOSS runs it again on the code it has then; if it still fails the same "
                       "way, it stays on BOSS's own fix list and this card comes back."),
    "app_spent": (("Open the work and read the failure: {cause}",
                   "Retry it with a note saying what to change (or change the brief), then "
                   "press Done — retry here."), AFTER),
}


def _base(cause_code: str) -> str:
    return str(cause_code or "").split(":", 1)[0]


def build(*, cause_code: str, cls: str, title: str, cause: str, action: str, reason: str,
          hints: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The four fields for a card about this cause."""
    hints = dict(hints or {})
    base, tail = _base(cause_code), str(cause_code or "").rsplit(":", 1)[-1]
    if tail == "old":
        key = "old"
    elif tail == "spent" and base not in _STEPS:
        key = "platform_spent" if cls == "platform" else "app_spent"
    elif base == "platform" or base.startswith("platform"):
        key = "held_proof" if "held_proof" in cause_code else (
            "platform_spent" if tail == "spent" else "")
    else:
        key = base
    toolchain = (re.search(r"enable (\w+)", f"{cause} {action}") or [None, "<name>"])[1]
    fill = {"cause": cause.rstrip(".") + ".", "title": title, "toolchain": toolchain,
            "receipt": hints.get("receipt_id") or "named on the card"}
    if key in _STEPS:
        steps, after = _STEPS[key]
        steps = [s.format(**fill) for s in steps]
    else:
        # The step's own agent asked (asked_by_*), a retry was withheld or refused: the ACTION
        # it gave is the exact step, then the retry.
        first = str(action or "").strip()
        first = re.sub(r"\s*Then press Done — retry\.?$|\s*,? ?then press Done — retry\.?$", "",
                       first, flags=re.I).strip() or "Open the work this card names and read its failure."
        steps = [first if first.endswith((".", "?", "!")) else first + ".", RETRY]
        after = AFTER
    return {"what_failed": _one_line(f"{title}: {cause}"), "why": _one_line(reason, 600),
            "steps": steps, "after": after}


def _one_line(text: str, n: int = 280) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def problems(guidance: Mapping[str, Any]) -> list[str]:
    """What makes a card's guidance unusable for a person; [] when it is exact."""
    out = []
    for field in ("what_failed", "why", "after"):
        if not str(guidance.get(field) or "").strip():
            out.append(f"no {field}")
    steps = guidance.get("steps") or []
    if not isinstance(steps, list) or not steps or not all(str(s).strip() for s in steps):
        out.append("no steps")
    said = " ".join([str(guidance.get(k) or "") for k in ("why", "after")]
                    + [str(s) for s in steps]).lower()
    out += [f"generic: {g!r}" for g in GENERIC if g in said]
    return out


__all__ = ["AFTER", "GENERIC", "RETRY", "build", "problems"]
