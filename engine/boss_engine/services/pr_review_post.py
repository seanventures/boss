"""The review conversation, on the pull request where people read it.

Why
---
Pull request #223 (77 files) showed "Conversation 0 · No reviews". The agents had reviewed it,
argued about it and fixed things across several rounds, and none of that reached the place a
human reviews code. One COMMENT review was posted, once, holding a reviewer's summary that said
there was nothing to review.

What is posted
--------------
* One GitHub review per review ROUND: each seat's verdict and summary, and whether it saw the
  change (review_input); findings that point at a line as inline comments; the rest in the body.
* The implementer's answer to each finding (fixed in round N, changing these files / disputed
  because …) as a REPLY under the comment it answers.
* The deferred findings (below the blocking bar, recorded as follow-up work), on the last round.

Every review is a COMMENT, never APPROVE or REQUEST_CHANGES: BOSS's gate decides whether the
change ships, and a second verdict living on GitHub would be a second place that decision lives.

Idempotent: what was posted is recorded (`state`: review id per round, GitHub comment id per
stored comment, reply id per answered comment). A re-run updates a round's body instead of
posting it again, and never replies twice.

A connection that may not write reviews is said in words (``permission_words``), never skipped
silently: the caller puts the sentence on the build page.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional

import httpx

from . import review_comments
from .review_comments import priority_label, priority_of

SEAT_LABELS = {"review_code": "Code review (adversarial)", "security_check": "Security review",
               "consistency_check": "Consistency review"}
VERDICT_WORDS = {"survived": "passed — no blocking defect found",
                 "refuted": "found a blocking defect", "clean": "clean",
                 "issues": "found security issues", "consistent": "consistent with the design",
                 "drift": "drifted from the design", "not_run": "did not reach a verdict",
                 "hollow": "did not see the change", "skipped": "not convened"}


class ReviewPostRefused(Exception):
    """GitHub refused the write; the message says what to grant."""


def permission_words(exc: BaseException, repository: str) -> Optional[str]:
    """The sentence for a refused write, or None when it is not a permissions problem."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code in (401, 403, 404):
        return (f"BOSS's GitHub connection cannot write pull request reviews on {repository} "
                f"(GitHub answered {code}). Give its token \"Pull requests: Read and write\" "
                "access (a fine-grained token: Repository permissions → Pull requests), then "
                "use \"Review this PR again\" to post the review.")
    return None


def round_name(round_i: int, ordinal: int, total: int, kind: str = "") -> str:
    if kind == "review_again":
        return f"Round {ordinal} of {total} — reviewed again on request, on the PR's diff"
    if int(round_i) >= 100:
        return f"Re-check {ordinal} of {total} (the check was run again)"
    if int(round_i) == 0:
        return f"Round {ordinal} of {total} — first review"
    return f"Round {ordinal} of {total} — after fix round {int(round_i)}"


def _fixed_words(c: dict) -> str:
    files = [str(f) for f in (c.get("response_files") or [])][:6]
    where = f" (changed {', '.join(f'`{f}`' for f in files)})" if files else ""
    kind = str(c.get("response_kind") or "")
    head = (f"**Implementer — fixed in fix round {c.get('response_round')}**{where}"
            if kind == "fixed" else
            f"**Implementer — disputed in fix round {c.get('response_round')}**")
    return f"{head}: {str(c.get('response') or '').strip()}"


def round_body(entry: dict, round_comments: list[dict], *, ordinal: int, total: int,
               deferred: Iterable[dict] = (), last: bool = False,
               not_inline: Iterable[dict] = ()) -> str:
    """The review body for one round."""
    lines = [f"### BOSS review — "
             f"{round_name(entry.get('round', 0), ordinal, total, str(entry.get('kind') or ''))}",
             ""]
    for stage, seat in (entry.get("seats") or {}).items():
        verdict = str((seat or {}).get("verdict") or "not_run")
        label = SEAT_LABELS.get(stage, stage)
        lines.append(f"**{label}:** {VERDICT_WORDS.get(verdict, verdict)}")
        if (seat or {}).get("hollow"):
            lines.append(f"> ⚠ This review did not see the change ({seat['hollow']}). Its gate "
                         "failed and the review was run again; it was not counted as a pass.")
        summary = str((seat or {}).get("summary") or "").strip()
        if summary:
            excerpt = summary if len(summary) <= 1800 else summary[:1800] + " …"
            lines += ["<details><summary>What the reviewer wrote</summary>", "",
                      excerpt, "", "</details>"]
        lines.append("")
    pri = Counter(priority_of(str(c.get("severity") or "")) for c in round_comments)
    if round_comments:
        counts = ", ".join(f"{pri[p]} {p}" for p in ("P1", "P2", "P3") if pri[p])
        lines.append(f"**Findings this round:** {counts} (P1 must be fixed before merge, "
                     "P2 should be, P3 is optional).")
    loose = [c for c in round_comments if not (c.get("path") and c.get("line"))]
    loose += list(not_inline)
    if loose:
        lines += ["", "**Findings not tied to a line:**"]
        for c in loose:
            where = (f" `{c.get('path')}:{c.get('line')}`" if c.get("path") and c.get("line")
                     else "")
            lines.append(f"- **{priority_label(str(c.get('severity') or ''))}**{where} — "
                         f"{c.get('body')}")
            if c.get("response_kind"):
                lines.append(f"  - {_fixed_words(c)}")
    deferred = list(deferred or [])
    if last and deferred:
        lines += ["", "#### Deferred — below the blocking bar, recorded as follow-up work"]
        for f in deferred:
            where = f" `{f.get('path')}:{f.get('line')}`" if f.get("path") and f.get("line") else ""
            lines.append(f"- **{priority_label(str(f.get('severity') or ''))}**{where} — "
                         f"{str(f.get('text') or f.get('body') or '').strip()}")
    lines += ["", "_Posted by BOSS. This is a comment, not an approval: BOSS's quality gate "
                  "decides whether the change ships._"]
    return "\n".join(lines).strip()


def _inline(comments: list[dict]) -> list[dict]:
    out = []
    for c in comments:
        if not (c.get("path") and c.get("line")):
            continue
        item = {"path": c["path"], "line": int(c.get("end_line") or c["line"]),
                "body": f"**{priority_label(str(c.get('severity') or ''))}** — {c.get('body')}"}
        if c.get("end_line") and c.get("end_line") != c.get("line"):
            item["start_line"] = int(c["line"])
        out.append(item)
    return out


def _rounds_of(rounds: list[dict], comments: list[dict]) -> list[dict]:
    """Every round: those recorded, plus any a comment names that was not (older runs)."""
    by = {int(r.get("round") or 0): r for r in rounds if isinstance(r, dict)}
    for c in comments:
        r = int(c.get("round") or 0)
        by.setdefault(r, {"round": r, "seats": {}})
    return [by[k] for k in sorted(by)]


def post(github: Any, number: int, *, comments: list[dict], rounds: list[dict],
         deferred: Iterable[dict] = (), state: Optional[dict] = None,
         repository: str = "") -> dict:
    """Post (or update) every round's review and every implementer reply. Returns
    {posted, reviews_created, reviews_updated, inline, replies, state, [reason], [url]}."""
    state = {"rounds": dict((state or {}).get("rounds") or {}),
             "replies": dict((state or {}).get("replies") or {})}
    comments = [c for c in comments if isinstance(c, dict)]
    ordered = _rounds_of(rounds, comments)
    if not ordered:
        return {"posted": False, "reason": "no review round to post", "state": state}
    created = updated = inline_n = replies = 0
    url = None
    deferred = list(deferred or [])
    try:
        for i, entry in enumerate(ordered):
            r = int(entry.get("round") or 0)
            mine = [c for c in comments if int(c.get("round") or 0) == r]
            key = str(r)
            last = i == len(ordered) - 1
            if key in state["rounds"]:
                rec = state["rounds"][key]
                body = round_body(entry, mine, ordinal=i + 1, total=len(ordered),
                                  deferred=deferred, last=last,
                                  not_inline=[c for c in mine if c.get("path") and c.get("line")
                                              and c.get("id") not in (rec.get("comments") or {})])
                github.update_review_body(number, int(rec["review_id"]), body)
                updated += 1
                continue
            anchored = [c for c in mine if c.get("path") and c.get("line")]
            body = round_body(entry, mine, ordinal=i + 1, total=len(ordered),
                              deferred=deferred, last=last)
            try:
                result = github.create_review(number, body=body, event="COMMENT",
                                              comments=_inline(anchored) or None)
                placed = anchored
            except httpx.HTTPStatusError as exc:
                if permission_words(exc, repository) or not anchored:
                    raise
                # 422: a line outside the diff. Post the round anyway, every finding in the body.
                body = round_body(entry, mine, ordinal=i + 1, total=len(ordered),
                                  deferred=deferred, last=last, not_inline=anchored)
                result = github.create_review(number, body=body, event="COMMENT", comments=None)
                placed = []
            created += 1
            url = getattr(result, "url", None) or url
            rec = {"review_id": int(result.id), "comments": {}}
            if placed:
                posted_comments = github.review_comments_of(number, int(result.id))
                pool = list(posted_comments)
                for c in placed:
                    line = int(c.get("end_line") or c["line"])
                    hit = next((p for p in pool if p.get("path") == c["path"]
                                and int(p.get("line") or 0) == line), None)
                    if hit is not None:
                        pool.remove(hit)
                        rec["comments"][str(c.get("id"))] = int(hit["id"])
                inline_n += len(rec["comments"])
            state["rounds"][key] = rec
        # The implementer's answers, under the comments they answer.
        gh_ids = {cid: gid for rec in state["rounds"].values()
                  for cid, gid in (rec.get("comments") or {}).items()}
        for c in comments:
            cid = str(c.get("id") or "")
            if not c.get("response_kind") or not cid or cid in state["replies"]:
                continue
            gid = gh_ids.get(cid)
            if gid is None:
                continue   # unanchored: its answer is in the round's body
            state["replies"][cid] = github.reply_to_review_comment(number, int(gid),
                                                                   _fixed_words(c))
            replies += 1
    except httpx.HTTPStatusError as exc:
        words = permission_words(exc, repository)
        return {"posted": created + updated > 0, "reason": words or
                f"GitHub refused the review: {exc.response.status_code}",
                "permission": bool(words), "reviews_created": created,
                "reviews_updated": updated, "inline": inline_n, "replies": replies,
                "state": state}
    return {"posted": True, "reviews_created": created, "reviews_updated": updated,
            "inline": inline_n, "replies": replies, "state": state,
            **({"review_url": url} if url else {})}


def legacy_round(evidence: dict, changed_paths: Iterable[str] = ()) -> dict:
    """Round 0 for a run recorded before rounds were: the evidence's own summaries. With the
    change's paths, a summary that never saw it is said to be hollow (review_input) — PR #223's
    "nothing to review" is not replayed as a pass."""
    from .review_input import hollow_reason

    hollow = dict(evidence.get("review_hollow") or {})
    paths = list(changed_paths or ())
    if paths:
        for stage, key in (("review_code", "review_summary"),
                           ("security_check", "security_summary"),
                           ("consistency_check", "consistency_summary")):
            if stage not in hollow and str(evidence.get(key) or "").strip():
                why = hollow_reason(str(evidence.get(key)), paths)
                if why:
                    hollow[stage] = why

    def verdict(stage: str, key: str) -> str:
        return "hollow" if hollow.get(stage) else (evidence.get(key) or "not_run")
    return {"round": 0, "seats": {
        "review_code": {"verdict": verdict("review_code", "adversarial_verdict"),
                        "summary": evidence.get("review_summary") or "",
                        "hollow": hollow.get("review_code")},
        "security_check": {"verdict": verdict("security_check", "security_verdict"),
                           "summary": evidence.get("security_summary") or "",
                           "hollow": hollow.get("security_check")},
        "consistency_check": {"verdict": verdict("consistency_check", "consistency_verdict"),
                              "summary": evidence.get("consistency_summary") or "",
                              "hollow": hollow.get("consistency_check")}}}


def comment_dicts(comments: Iterable[Any]) -> list[dict]:
    """Stored comments (dicts or ReviewComment) as dicts."""
    out = []
    for c in comments:
        if isinstance(c, review_comments.ReviewComment):
            out.append(c.to_dict())
        elif isinstance(c, dict):
            out.append(c)
    return out
