"""A PLAN COVERS ITS BRIEF before a person sees it or code is written (AR-24).

THE CASE (northgate-gateway, follow-up crun_b14450fc2b034508a756, 2026-11-16): the brief said
"fix every open finding below, security first" and listed a HIGH dot-segment path traversal in
internal/proxy/router.go first, then two MEDIUMs in handler.go. The plan the design session
wrote fixed a request-id bug and added two test files — nothing else — and went to a person on a
plan-approval card (dec_dcd91e76a6254f9a9939, "touches auth, secret, token") that offered only
Approve or Reject. The plan had quietly narrowed its brief, and nothing compared the two.

THE RULE. Every plan the design phase produces is checked against the brief it was written for,
before the plan-approval gate and before any code. That is every build that runs the design
phase (risk tier 2 and up, not a web-app scaffold, a seeded build or the fix lane), follow-ups
included when they run it. A build that skips the design phase has no plan to check: its brief
is held to by the acceptance check after the code is written.

  * THE ITEMS. Every finding the brief lists in BOSS's own `- [SEVERITY] text` form whose
    severity requires it (a security finding, a blocker, anything HIGH/CRITICAL/MAJOR — a
    security block's own HIGH/MEDIUM paragraphs are split out, each its own item), and every
    acceptance criterion (the must-haves) that is not a restatement of one of those findings.
  * THE DETERMINISTIC MATCH, where the item has an identity. A finding anchored to a file (and
    the functions it names in its location) is covered only when the plan names that file and,
    if it names functions, one of them. No model can wave that through.
  * THE JUDGEMENT. One typed decision (typed_decisions: the answers are fixed, anything else is
    refused) reads the brief and the plan and says, for each item, covered / skipped with a
    stated reason / missing. A model is needed: "fix the CORS handler" covers an applyCORS
    finding however it is worded, and a plan can skip an item for a reason a person accepts.
  * Uncovered → back to the planner with the missing items named (bounded, MAX_REPLANS), and
    only then to a person, with the gap on the card. The plan-approval card also offers
    Request changes (on_plan_sent_back): the run re-plans with the person's note.

A model that cannot answer never blocks an item the deterministic match could not judge: the
check fails open there and says so (`unjudged`); an anchored item it could judge stays
deterministic.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

#: Planner rounds after the first plan when items are uncovered; then a person decides.
MAX_REPLANS = 2
#: The most items one check judges (the rest are listed as not checked, never as covered).
MAX_ITEMS = 30

_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "ADVISORY", "INFO", "BLOCKER", "MAJOR",
               "MINOR", "NIT", "SECURITY")
#: An item at one of these severities must be covered by the plan (or skipped with a reason).
_REQUIRED = frozenset({"CRITICAL", "HIGH", "BLOCKER", "MAJOR", "SECURITY"})
#: Inside a security finding, these are still security findings to fix; LOW/ADVISORY are not.
_SECURITY_REQUIRED = frozenset({"CRITICAL", "HIGH", "MEDIUM", "SECURITY", "BLOCKER", "MAJOR"})

_BULLET = re.compile(r"^\s*-\s*\[([A-Z][A-Z /_-]{1,30})\]\s*(.*)$")
_LEAD = re.compile(r"^\s*(" + "|".join(_SEVERITIES) + r")(?:/[A-Z]+)?\b[:\s]")
_FILE = re.compile(
    r"(?<![\w/.\-])((?:[\w.\-]+/)*[\w\-]+\.(?:go|py|pyi|ts|tsx|js|jsx|mjs|java|kt|scala|rb|rs|"
    r"cs|php|swift|c|cc|cpp|h|hpp|sql|sh|tf|ya?ml|json|toml))(?::[\d\-]+)?(?![\w/])")
_TICKED_NAME = re.compile(r"`([A-Za-z_][\w.]*)(?:\(\))?`")
#: The planner's explicit "this item does not apply" (BOSSAPP-1360): `ITEM F-42: NOT APPLICABLE —
#: <reason>` (stale, a false positive, already done), one line per item, the reason required.
_NOT_APPLICABLE = re.compile(
    r"(?im)^[\s>*\-\u2022`]*ITEM\s+`?([A-Za-z][\w.\-]*\d[\w.\-]*|bi_[0-9a-f]{6,})`?\s*[:\-]\s*"
    r"\**NOT[\s_]+APPLICABLE\**\s*(?:[\u2014\u2013:\-]+\s*)?(.*)$")
#: The id a brief item leads with (a ledger finding `F-42`, a contract item `AC-3`).
_ITEM_KEY = re.compile(r"^\W*(?:\[[A-Za-z /_-]{2,30}\]\s*)?(?:\(\w+\)\s*)?([A-Z]{1,6}-\d+)\b")
_RESTATED = re.compile(r"^\s*(?:Fixed|Deferred finding addressed)\s*:\s*", re.I)


@dataclass
class BriefItem:
    id: str
    severity: str
    text: str
    kind: str                       # "finding" | "must_have"
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    #: A security finding (a `[SECURITY]` block's item, or raised by the security seat).
    security: bool = False

    def label(self) -> str:
        words = " ".join(self.text.split())
        lead = _LEAD.match(_unsourced(words))
        return (words if lead else f"[{self.severity}] {words}")[:240]


@dataclass
class Coverage:
    ok: bool
    items: int
    missing: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    unjudged: int = 0
    not_checked: int = 0
    by: str = "deterministic"

    def as_dict(self) -> dict:
        return {"ok": self.ok, "items": self.items, "missing": self.missing,
                "skipped": self.skipped, "unjudged": self.unjudged,
                "not_checked": self.not_checked, "by": self.by}


def _item_id(severity: str, text: str) -> str:
    return "bi_" + hashlib.sha1(f"{severity}|{' '.join(text.split())}".encode()).hexdigest()[:10]


def _anchors(text: str) -> tuple[list[str], list[str]]:
    """The files a finding is LOCATED in and the functions its location names — only when the
    finding starts with its location (`internal/proxy/router.go (`Match`) — …`, as BOSS's
    reviewers write them), from the head before the ' — ' explanation. A file a finding merely
    mentions (a must-have naming the YAML it is about) is not where the fix goes."""
    loc = _LEAD.sub("", _unsourced(text), count=1).lstrip()
    if not _FILE.match(loc):
        return [], []
    head = loc.split(" — ", 1)[0] if " — " in loc[:400] else loc[:200]
    files = list(dict.fromkeys(m.group(1) for m in _FILE.finditer(head)))
    if not files:
        return [], []
    symbols = list(dict.fromkeys(
        m.group(1).split(".")[-1] for m in _TICKED_NAME.finditer(head)
        if not _FILE.fullmatch(m.group(1))))
    return files, [s for s in symbols if len(s) >= 3]


def _unsourced(text: str) -> str:
    """The finding without the seat that raised it (`(security_check) HIGH …` → `HIGH …`)."""
    return re.sub(r"^\s*\(\w+\)\s*", "", text or "")


def _findings(instructions: str) -> list[tuple[str, str]]:
    """BOSS's `- [SEVERITY] text` findings, each with the lines and severity-led paragraphs
    that continue it (a security block's own HIGH / MEDIUM paragraphs)."""
    out: list[list[str]] = []
    current: Optional[list[str]] = None
    blank = False
    for line in (instructions or "").splitlines():
        m = _BULLET.match(line)
        if m:
            current = [m.group(1).strip().upper(), m.group(2)]
            out.append(current)
            blank = False
            continue
        if current is None:
            continue
        if not line.strip():
            blank = True
            continue
        if blank and not _LEAD.match(line):
            current = None              # the paragraph after the list ("Fix ONLY these.")
            continue
        current[1] += ("\n\n" if blank else "\n") + line
        blank = False
    return [(sev, text) for sev, text in out]


def _split_security(sev: str, text: str) -> list[tuple[str, str]]:
    """One finding per severity-led paragraph of a finding block; the block's own severity
    for a paragraph that names none."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[tuple[str, str]] = []
    for p in paras:
        m = _LEAD.match(_unsourced(p))
        if m:
            out.append((m.group(1), p))
        elif out:
            out[-1] = (out[-1][0], out[-1][1] + "\n\n" + p)
        else:
            out.append((sev, p))
    return out


#: The seat that raised a finding, as BOSS writes it at the head of the finding.
_SECURITY_SEAT = re.compile(r"^\s*\(security_check\)", re.I)


def _security_block(block_sev: str, text: str) -> bool:
    """A finding the security seat raised: a `[SECURITY]` block, or any bullet whose finding
    starts `(security_check)` (`- [MEDIUM] (security_check) …`)."""
    return block_sev == "SECURITY" or bool(_SECURITY_SEAT.match(text or ""))


def _required(block_sev: str, sev: str, security: bool = False) -> bool:
    """A SECURITY MEDIUM IS REQUIRED (AMEX, AR-24 review): any finding of the security seat at
    MEDIUM or above, whether it came in a `[SECURITY]` block or as its own bullet."""
    if block_sev == "SECURITY" or security:
        return sev in _SECURITY_REQUIRED
    return sev in _REQUIRED or block_sev in _REQUIRED and sev not in {
        "LOW", "ADVISORY", "INFO", "NIT", "MINOR"}


def brief_items(instructions: str, criteria: Any = None) -> list[BriefItem]:
    """What the plan must cover: the required findings, then the must-haves not restating
    one of them. Deterministic: the same brief gives the same items and ids."""
    items: list[BriefItem] = []
    finding_keys: list[str] = []
    for block_sev, text in _findings(instructions):
        finding_keys.append(_norm(text)[:60])
        security = _security_block(block_sev, text)
        for sev, part in _split_security(block_sev, text):
            part_security = security or sev == "SECURITY" or bool(_SECURITY_SEAT.match(part))
            if not _required(block_sev, sev, part_security):
                continue
            files, symbols = _anchors(part)
            items.append(BriefItem(_item_id(sev, part), sev if sev != "SECURITY" else block_sev,
                                   part.strip(), "finding", files, symbols,
                                   security=part_security))
    for crit in criteria or []:
        crit = str(crit or "").strip()
        if not crit:
            continue
        body = _norm(_RESTATED.sub("", crit))
        if _RESTATED.match(crit) or any(k and (k in body or body[:60] in k)
                                         for k in finding_keys):
            continue                    # a follow-up's "Fixed: - [HIGH] …" restates a finding
        items.append(BriefItem(_item_id("MUST-HAVE", crit), "MUST-HAVE", crit, "must_have"))
    seen: set[str] = set()
    unique = []
    for it in items:
        if it.id not in seen:
            seen.add(it.id)
            unique.append(it)
    return unique


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _mentions(plan: str, name: str) -> bool:
    """Whether the plan names a function / identifier `name` (a whole word)."""
    return re.search(r"(?<![\w.\-])" + re.escape(name) + r"(?![\w\-])", plan) is not None


#: A path-shaped token in a plan (`internal/proxy/router.go`, `./proxy/router.go`, `router.go`).
_PATH_TOKEN = re.compile(r"[\w.\-/]*[\w\-]\.[A-Za-z]\w{0,8}")


def _path_key(path: str) -> str:
    return re.sub(r"^(?:\.{1,2}/|/)+", "", str(path or "").strip("`'\"()[],;:"))


def _tail_of(longer: str, shorter: str) -> bool:
    """Whether `shorter` is `longer` or a `/`-bounded tail of it."""
    return bool(shorter) and (longer == shorter or longer.endswith("/" + shorter))


def names_file(plan: str, path: str) -> bool:
    """Whether the plan names the file at `path`: the same path, or one a `/`-bounded tail of
    the other — `internal/proxy/router.go`, `./proxy/router.go` and `router.go` all name a
    finding anchored at `proxy/router.go` (AR-24 review: the old look-behind refused the first
    two)."""
    want = _path_key(path)
    if not want:
        return False
    for m in _PATH_TOKEN.finditer(plan or ""):
        got = _path_key(m.group(0))
        if got and (_tail_of(got, want) or _tail_of(want, got)):
            return True
    return False


def anchored_verdict(item: BriefItem, plan: str) -> Optional[str]:
    """'covered' / 'missing' by the item's identity, or None when it has none to match.
    A file is named by its path, a longer or shorter `/`-bounded form of it, or its file name
    (`names_file`); a function by its own name."""
    if not item.files:
        return None
    named_file = any(names_file(plan, f) for f in item.files)
    if not named_file:
        return "missing"
    if item.symbols and not any(_mentions(plan, s) for s in item.symbols):
        return "missing"
    return "covered"


def item_key(item: BriefItem) -> str:
    """The id the brief gave the item (`F-42`, `AC-3`), or ''."""
    m = _ITEM_KEY.match(item.text or "")
    return m.group(1).upper() if m else ""


def declared_not_applicable(*texts: str) -> dict[str, str]:
    """{item key or id (upper-cased): reason} for every `ITEM <key>: NOT APPLICABLE — <reason>`
    the planner wrote in its PLAN (never its self-review). A line with no reason is not a
    declaration: the plan still owes the item."""
    out: dict[str, str] = {}
    for text in texts:
        for m in _NOT_APPLICABLE.finditer(str(text or "")):
            reason = " ".join(m.group(2).split())
            if len(reason) >= 6:
                out.setdefault(m.group(1).upper(), reason[:300])
    return out


def _questions(items: list[BriefItem]):
    from .typed_decisions import Question

    return [Question(
        key=f"i{k}",
        instructions=(f"Does the PLAN deliver this item the BRIEF requires? ITEM ({it.severity}): "
                      f"{' '.join(it.text.split())[:700]}"),
        options={
            "covered": "The plan's changes fix or deliver this item (its wording may differ).",
            "skipped": "The plan does not do it but says so explicitly, naming the item and "
                       "giving a reason.",
            "missing": "The plan neither does it nor explicitly says why it is left out.",
        }) for k, it in enumerate(items, start=1)]


def check(design: Mapping[str, Any], *, instructions: str, criteria: Any = None,
          decider: Any = None) -> Coverage:
    """Whether the design's plan covers its brief. Never raises: a decider that fails leaves
    the deterministic verdicts standing and counts the rest as unjudged."""
    plan = str((design or {}).get("plan") or "")
    review = str((design or {}).get("plan_review") or "")
    every = brief_items(instructions, criteria)
    items, not_checked = every[:MAX_ITEMS], max(0, len(every) - MAX_ITEMS)
    if not items:
        return Coverage(ok=True, items=0)
    anchored = [anchored_verdict(it, plan) for it in items]
    declared = declared_not_applicable(plan)
    answers: dict[str, Any] = {}
    by = "deterministic"
    if decider is not None:
        context = ("BRIEF (what the person asked for):\n" + str(instructions or "")[:9000]
                   + "\n\nPLAN:\n" + plan[:9000]
                   + ("\n\nPLAN REVIEW (the planner's own notes):\n" + review[:3000]
                      if review else ""))
        try:
            batch = decider.decide(context, _questions(items))
            answers = dict(batch.decisions or {})
            by = f"{getattr(batch, 'backend', 'model')}+deterministic"
        except Exception:  # noqa: BLE001 — the deterministic verdicts stand on their own
            logger.warning("plan coverage judgement failed", exc_info=True)
    missing, skipped, unjudged = [], [], 0
    for k, (it, det) in enumerate(zip(items, anchored), start=1):
        d = answers.get(f"i{k}")
        said = getattr(d, "answer", None)
        reason = str(getattr(d, "reason", "") or "")
        na = declared.get(item_key(it)) or declared.get(it.id.upper())
        # A SECURITY finding or a BLOCKER is never waved off by the planner: it stays missing,
        # and so goes to the human plan gate.
        if na and not (it.security or it.severity in SERIOUS):
            # THE PLANNER SAID IT DOES NOT APPLY (BOSSAPP-1360): stale, a false positive, or
            # already done. Not sent back, not escalated: it goes on to review and acceptance
            # as an item to VERIFY (`not_applicable`), where a reader can refute the reason.
            skipped.append({"id": it.id, "severity": it.severity, "kind": it.kind,
                            "item": it.label(), "why": na, "security": it.security,
                            "not_applicable": True, "key": item_key(it) or it.id})
            continue
        if det == "missing":
            # The plan does not name where the finding is: covered is not an answer a model can
            # give it; an explicit skip still is.
            verdict = "skipped" if said == "skipped" else "missing"
            why = reason if said == "skipped" else (
                f"the plan does not name {', '.join(it.files[:2])}"
                + (f" ({', '.join(it.symbols[:3])})" if it.symbols else ""))
        elif said in ("covered", "skipped", "missing"):
            verdict, why = said, reason
        else:
            verdict, why = "covered", ""
            if det is None:
                unjudged += 1
        row = {"id": it.id, "severity": it.severity, "kind": it.kind, "item": it.label(),
               "why": why[:300], "security": it.security}
        if verdict == "missing":
            missing.append(row)
        elif verdict == "skipped":
            skipped.append(row)
    return Coverage(ok=not missing, items=len(items), missing=missing, skipped=skipped,
                    unjudged=unjudged, not_checked=not_checked, by=by)


def replan_note(coverage: Coverage) -> str:
    """What the planner is told when its plan left brief items out."""
    lines = "\n".join(f"- {m['item']}" + (f" (why: {m['why']})" if m.get("why") else "")
                      for m in coverage.missing)
    return ("YOUR PLAN DOES NOT COVER EVERYTHING THE BRIEF REQUIRES. These items are missing "
            "from it:\n" + lines + "\n\nRewrite the PLAN so it covers every item above — "
            "security and blocker findings first, naming the files and functions you will "
            "change — or, for an item you will not do (stale, a false positive, already done; never a security, critical, high or blocker finding), "
            "write one line in the PLAN: `ITEM <key>: NOT APPLICABLE — <reason>` (the key as the "
            "item names it, e.g. F-42 or AC-3). Review will verify each such reason. Do not drop "
            "anything the previous plan already covered.")


def verify_block(coverage: Optional[Mapping[str, Any]]) -> str:
    """The items the planner declared not applicable, as a block for the review and acceptance
    that follow: each reviewing seat answers each one, positively (silence confirms nothing)."""
    rows = [r for r in (coverage or {}).get("skipped") or []
            if isinstance(r, Mapping) and r.get("not_applicable")]
    if not rows:
        return ""
    return ("\n\nPLAN ITEMS DECLARED NOT APPLICABLE. Check each reason against the code and "
            "answer EACH on its own line: `NA <key>: CONFIRMED` (the reason holds) or "
            "`NA <key>: REFUTED — <why>` (it is still owed). No answer leaves it open:\n"
            + "\n".join(f"- {r.get('key') or r['id']}: {r['item']} — planner: "
                         f"{r.get('why') or 'no reason'}" for r in rows[:20]))


#: A gap at one of these severities is never approved by a delegate (approval_delegations).
SERIOUS = frozenset({"CRITICAL", "HIGH", "BLOCKER", "SECURITY"})


def serious_gaps(coverage: Optional[Mapping[str, Any]]) -> list[dict]:
    """The missing items a person must see: security findings and HIGH / CRITICAL / BLOCKER."""
    return [m for m in (coverage or {}).get("missing") or []
            if isinstance(m, Mapping) and (m.get("security")
                                           or str(m.get("severity") or "").upper() in SERIOUS)]


def gap_words(coverage: Mapping[str, Any]) -> str:
    """The gap as a person reads it on the plan card."""
    missing = list((coverage or {}).get("missing") or [])
    if not missing:
        return ""
    head = (f"The plan still leaves {len(missing)} item{'s' if len(missing) != 1 else ''} "
            "of its brief out after BOSS sent it back to the planner:")
    return head + "\n" + "\n".join(f"- {m['item']}" for m in missing[:12])


def ensure_covers(design: dict, *, instructions: str, criteria: Any,
                  decider: Any,
                  replan: Callable[[str, Mapping[str, Any]], Optional[Mapping[str, Any]]],
                  on_round: Optional[Callable[[int, Coverage], None]] = None,
                  max_replans: int = MAX_REPLANS) -> tuple[dict, Coverage]:
    """Check the plan; while it leaves items out, hand the planner the missing items and check
    its new plan — at most `max_replans` times. Returns the last design (with its coverage
    recorded under `coverage`) and the last verdict. `replan(note, design)` (the plan as it
    stands) returns the new PLAN / PLAN REVIEW (and plan_ok), or None when the planner could not
    answer."""
    coverage = check(design, instructions=instructions, criteria=criteria, decider=decider)
    rounds = 0
    if on_round:
        on_round(rounds, coverage)
    while not coverage.ok and rounds < max_replans:
        rounds += 1
        try:
            new = replan(replan_note(coverage), design)
        except Exception:  # noqa: BLE001 — a planner that fails leaves the gap for a person
            logger.warning("re-plan for coverage failed", exc_info=True)
            new = None
        if not new or not str(new.get("plan") or "").strip():
            break
        design = {**design, **{k: v for k, v in new.items()
                               if k in ("plan", "plan_review", "plan_ok")}}
        coverage = check(design, instructions=instructions, criteria=criteria, decider=decider)
        if on_round:
            on_round(rounds, coverage)
    design = {**design, "coverage": {**coverage.as_dict(), "replans": rounds}}
    return design, coverage


# ---------------------------------------------------------------------------------------------
# Request changes on the plan card
# ---------------------------------------------------------------------------------------------

def on_plan_sent_back(connection: Any, row: Mapping[str, Any], *, actor: str,
                      note: Optional[str], jobs: Any) -> bool:
    """A person pressed Request changes on a plan-approval card: the paused build designs
    again with the person's note (and the plan they sent back) in its brief, is checked against
    its brief again, and asks again. Returns False when the build is no longer waiting on this
    plan (nothing to re-plan)."""
    from sqlalchemy import text

    from .core import emit_event
    from .jobs import JOB_PRIORITY_INTERACTIVE

    rec = row.get("recommendation") or {}
    run_id = str(rec.get("run_id") or "")
    if not run_id:
        return False
    run = connection.execute(text(
        "SELECT id, project_id, task_id, status, phase, state FROM code_task_runs WHERE id = :id "
        "FOR UPDATE"), {"id": run_id}).mappings().first()
    if not run or run["status"] != "blocked" or run["phase"] != "awaiting_plan_approval":
        emit_event(connection, row["project_id"], "code_task.plan_change_skipped", actor,
                   "code_task_run", run_id or str(row["id"]),
                   {"decision_id": row["id"], "why": "the build is no longer waiting on this plan"})
        return False
    state = run["state"] if isinstance(run["state"], dict) else json.loads(run["state"] or "{}")
    previous = str(((state or {}).get("design") or {}).get("plan") or "")
    change = {"note": str(note or "").strip()[:4000], "by": actor, "decision_id": row["id"],
              "previous_plan": previous[:6000],
              "rounds": int(((state or {}).get("plan_change_request") or {}).get("rounds") or 0) + 1}
    # THE PERSON'S NOTE IS KEPT ON THE RUN the way a rejection's is (BOSSAPP-1321,
    # `plan_rejected_note`): one line, bounded, under `plan_change_note`, beside the request.
    connection.execute(text(
        "UPDATE code_task_runs SET state = (COALESCE(state, '{}'::jsonb) - 'design' "
        "- 'awaiting_plan_approval' - 'plan_interrupt_id') "
        "|| jsonb_build_object('plan_change_request', CAST(:c AS JSONB)), "
        "finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
        "|| jsonb_build_object('plan_change_note', CAST(:n AS TEXT)), "
        "status = 'running', phase = 'design', updated_at = now() WHERE id = :id"),
        {"c": json.dumps(change), "n": " ".join(change["note"].split())[:1000], "id": run_id})
    jobs.create_job(connection, project_id=run["project_id"], task_id=run["task_id"],
                    kind="code.task", priority=JOB_PRIORITY_INTERACTIVE,
                    payload={"run_id": run_id})
    emit_event(connection, run["project_id"], "code_task.plan_sent_back", actor,
               "code_task_run", run_id,
               {"decision_id": row["id"], "note": change["note"][:500],
                "round": change["rounds"]})
    return True


def change_request_brief(state: Mapping[str, Any]) -> str:
    """What the PLANNER is told on a re-plan after Request changes, or "" when there is none.
    For the design session only: never joined to the brief later stages read, and never checked
    as brief items (the old plan's bullets are not the task)."""
    change = (state or {}).get("plan_change_request") or {}
    if not isinstance(change, Mapping) or not str(change.get("note") or "").strip():
        return ""
    prev = str(change.get("previous_plan") or "").strip()
    return ("\n\nA PERSON SENT THE PLAN BACK WITH CHANGES. Their note, which this plan must "
            f"address:\n{str(change['note']).strip()}"
            + (f"\n\nTHE PLAN THEY SENT BACK (for reference; do not repeat what they asked to "
               f"change):\n{prev}" if prev else ""))


__all__ = ["BriefItem", "Coverage", "MAX_REPLANS", "SERIOUS", "anchored_verdict", "brief_items",
           "names_file", "serious_gaps",
           "change_request_brief", "check", "ensure_covers", "gap_words", "on_plan_sent_back",
           "replan_note"]
