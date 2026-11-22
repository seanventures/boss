"""HUMAN OVERRIDES of validation findings, and the automatic re-run (SPEC §3).

Per finding: accept · reject (that hunk back to the partner's exact lines) · edit (a hand-written
replacement) · remove (delete the finding's whole block — a route and its keys, a key's line; an
edit with EMPTY text is a remove) · waive (reason; a P0 needs a second person's approval) ·
reopen. Per file:
revert-all (the target becomes the partner's bytes). ``head_sha`` is a precondition — a stale
one is a 409 with the current packet. reject / edit / revert-all write ONE commit to the change
branch with ``BOSS-Override:`` / ``On-behalf-of:`` / ``Reason:`` trailers; accept and waive change
records only. Every override starts a re-run (job ``validation.rerun``: the validator on the
target at the new head) and the packet becomes v(n+1); the merge is refused while it runs
(review_packet.blocks_merge). An override never edits the validator's output: the packet applies
it on top, with its ref O-n.
"""
from __future__ import annotations

import json
import logging
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from sqlalchemy import text

from . import review_packet as rp
from .core import emit_event, make_id

log = logging.getLogger(__name__)

ACTIONS = ("accept", "reject", "edit", "remove", "waive", "reopen")
COMMITS = frozenset({"reject", "edit", "remove", "revert_all"})
WAIVE_ACTION = "waive_finding"
#: A P0 waive needs ONE named second person (SPEC §3), so its decision uses a single-approval tier
#: with no seat: tier 3 would ask for two people, and tier 2 for the CEO seat, while the waive names
#: exactly one approver (a person with `github:merge`). Tier 1 is still above nothing: it carries no
#: cost, so Full auto never approves it, and `on_waive_approved` refuses anyone but the named person.
WAIVE_RISK_TIER = 1
JOB_KIND = "validation.rerun"
REASON_MAX = 1000
#: A re-run still pending/running after this is failed with its reason (a dead worker, a lost
#: job) — never a merge held forever.
STALE_AFTER = rp.RERUN_STALE_AFTER
GITHUB_UNSUPPORTED = ("Overrides on GitHub pull requests are not supported yet; use the local "
                      "change.")


class OverrideError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _j(value: Any, default: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


# ── the change and its repository ──────────────────────────────────────────────────────────────

@dataclass
class Change:
    project_id: str
    number: int
    run_id: Optional[str]
    branch: str
    pr_url: Optional[str]
    repo: Any                    # a repo-host adapter: read_text_file / upsert_file
    head: Callable[[], str]
    local_git_dir: Optional[Path] = None
    owner: str = ""
    name: str = ""
    kind: str = "local"          # local | github


def change(c: Any, settings: Any, project_id: str, number: int) -> Change:
    """Change #n of the project: a local change set, else a build's pull request #n."""
    row = c.execute(text(
        "SELECT * FROM local_change_sets WHERE project_id = :p AND number = :n "
        "ORDER BY (state = 'open') DESC, created_at DESC LIMIT 1"),
        {"p": project_id, "n": int(number)}).mappings().first()
    if row is not None:
        from . import local_repo
        from .local_repo_host import LocalGitAdapter, change_set_url

        owner, _, name = str(row["repository"]).partition("/")
        adapter = LocalGitAdapter(settings, owner=owner, repo=name, project_id=project_id)
        git_dir = local_repo.bare_path(owner, name, settings)
        branch = str(row["branch"])
        return Change(project_id, int(number), row.get("run_id"), branch,
                      change_set_url(row["repository"], int(number)), adapter,
                      lambda: local_repo.branch_sha(git_dir, branch) or str(row.get("head_sha") or ""),
                      local_git_dir=git_dir, owner=owner, name=name)
    run = c.execute(text(
        "SELECT id, spec, finalize_ctx, pull_request_url FROM code_task_runs "
        "WHERE project_id = :p AND pull_request_number = :n ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "n": int(number)}).mappings().first()
    if run is None:
        raise OverrideError(404, "no_change", f"There is no change #{number}.")
    spec, fctx = _j(run["spec"], {}), _j(run["finalize_ctx"], {})
    owner, name = str(spec.get("owner") or ""), str(spec.get("repo") or "")
    branch = str(fctx.get("branch_name") or fctx.get("branch") or "")
    if not (owner and name and branch):
        raise OverrideError(409, "no_branch", f"Change #{number} has no branch BOSS can write.")
    from .github_actions import adapter_for_repository

    bound = adapter_for_repository(c, settings, project_id, f"{owner}/{name}")
    return Change(project_id, int(number), str(run["id"]), branch, run["pull_request_url"],
                  bound.adapter, lambda: str(bound.adapter.branch_snapshot(branch).get("sha") or ""),
                  owner=owner, name=name, kind="github")


# ── records ────────────────────────────────────────────────────────────────────────────────────

def overrides(c: Any, project_id: str, number: int) -> list[dict]:
    """The change's overrides, oldest first, each with its per-change ref O-n."""
    rows = c.execute(text(
        "SELECT * FROM finding_overrides WHERE project_id = :p AND change_number = :n "
        "ORDER BY created_at, id"), {"p": project_id, "n": int(number)}).mappings().all()
    names = _names(c, {str(r["actor"]) for r in rows} | {str(r["approver"]) for r in rows
                                                         if r.get("approver")})
    out = []
    for i, r in enumerate(rows, start=1):
        d = dict(r)
        for k in ("approved_at", "expires_at", "created_at"):
            if hasattr(d.get(k), "isoformat"):
                d[k] = d[k].isoformat()
        # WHO, BY NAME (dry run 2, gap 7): the card said "· a person".
        d["actor_name"] = names.get(str(d.get("actor")))
        if d.get("approver"):
            d["approver_name"] = names.get(str(d["approver"]))
        out.append({"ref": f"O-{i}", **d})
    return out


def _names(c: Any, ids: set) -> dict:
    """{principal id: display name} for the people named on overrides. {} when the identity
    tables are not there (never fails the read)."""
    ids = sorted(i for i in ids if i and i != "None")
    if not ids:
        return {}
    try:
        if c.execute(text("SELECT to_regclass('identity_principals')")).scalar() is None:
            return {}
        rows = c.execute(text("SELECT id, display_name FROM identity_principals "
                              "WHERE id = ANY(:a)"), {"a": ids}).all()
    except Exception:  # noqa: BLE001 — names are a convenience
        log.info("override actor names unavailable", exc_info=True)
        return {}
    return {str(i): str(n) for i, n in rows if n}


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def expire_stale(c: Any, project_id: str, number: int) -> int:
    """Fail re-runs pending/running past STALE_AFTER, with the reason."""
    return c.execute(text(
        "UPDATE validation_reruns SET state = 'failed', finished_at = now(), "
        "error = 'no result after ' || :m || ' minutes (the worker did not finish it)' "
        "WHERE project_id = :p AND change_number = :n AND state IN ('pending', 'running') "
        "AND created_at < now() - make_interval(mins => :mi)"),
        {"p": project_id, "n": int(number), "m": str(int(STALE_AFTER.total_seconds() // 60)),
         "mi": int(STALE_AFTER.total_seconds() // 60)}).rowcount or 0


def latest_rerun(c: Any, project_id: str, number: int) -> Optional[dict]:
    expire_stale(c, project_id, number)
    row = c.execute(text(
        "SELECT * FROM validation_reruns WHERE project_id = :p AND change_number = :n "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id, "n": int(number)}).mappings().first()
    if row is None:
        return None
    d = dict(row)
    d["validation"] = _j(d.get("validation"), None)
    return d


def _merge_decision(c: Any, ch: Change) -> Optional[dict]:
    row = c.execute(text(
        "SELECT id, recommendation FROM decisions WHERE project_id = :p "
        "AND status IN ('pending', 'delegated') AND recommendation->>'action' = 'review_pull_request' "
        "AND (recommendation->>'pull_request_url' = :u OR recommendation->>'run_id' = :r) "
        "ORDER BY created_at DESC LIMIT 1"),
        {"p": ch.project_id, "u": ch.pr_url or "", "r": ch.run_id or ""}).mappings().first()
    return {"id": row["id"], "recommendation": _j(row["recommendation"], {})} if row else None


def validation_of(c: Any, ch: Change) -> Optional[dict]:
    """The newest boss.validation/v1 for the change: the last finished re-run's, else the
    lineage's."""
    rr = c.execute(text(
        "SELECT validation, head_sha FROM validation_reruns WHERE project_id = :p "
        "AND change_number = :n AND state = 'done' ORDER BY finished_at DESC NULLS LAST LIMIT 1"),
        {"p": ch.project_id, "n": ch.number}).first()
    if rr is not None and _j(rr[0], None):
        rec = _j(rr[0], None)
        # The head the re-run checked (records before it stamped one: the head it was queued at).
        if isinstance(rec.get("after"), dict) and not rec["after"].get("head_sha") and rr[1]:
            rec["after"]["head_sha"] = rr[1]
        return rec
    if not ch.run_id:
        return None
    for r in reversed(rp._lineage(c, ch.run_id)):
        v = (r["state"] or {}).get("validation")
        if isinstance(v, Mapping) and v.get("schema") == "boss.validation/v1":
            return dict(v)
    return None


# ── applying overrides to the packet ───────────────────────────────────────────────────────────

def apply(findings: list[dict], ovrs: Sequence[Mapping[str, Any]], *, now: Optional[datetime] = None) -> list[dict]:
    """The packet's view of each finding after the people's overrides (records only; the
    validator's own output is untouched). The LAST override of a finding decides:
    accept/edit → as the re-run found it (``edit`` makes the fix ``human``); reject → ``reverted``
    while the re-run still reports it; waive → ``waived`` once approved (a P0's needs its
    approver) and not expired; reopen → ``open``; revert-all → every validator finding is as the
    re-run found it, ``reverted`` while reported."""
    now = now or _now()
    by_id = {str(f["id"]): f for f in findings}
    for o in ovrs:
        ref = {"id": o["ref"], "action": o["action"], "actor": o.get("actor"),
               "actor_name": o.get("actor_name"), "reason": o.get("reason"), "approver": o.get("approver"),
               "approved_at": o.get("approved_at"), "expires_at": o.get("expires_at"),
               "commit_sha": o.get("commit_sha"), "at": o.get("created_at")}
        targets = ([f for f in findings if (f.get("source") or {}).get("kind") == "validator"]
                   if o["action"] == "revert_all" else
                   [by_id[o["finding_id"]]] if o.get("finding_id") in by_id else [])
        for f in targets:
            f["override"] = ref
            action = o["action"]
            if action in ("reject", "revert_all") and f.get("status") == "open":
                f["status"] = "reverted"
            elif action in ("edit", "remove"):
                f["fix_kind"] = "human"
            elif action == "waive" and f.get("status") in ("open", "reverted"):
                approved = f.get("severity") != "P0" or bool(o.get("approved_at"))
                expires = _parse_ts(o.get("expires_at"))
                expired = expires is not None and expires < now
                if approved and not expired:
                    f["status"] = "waived"
            elif action == "reopen" and f.get("status") in ("waived", "fixed", "reverted",
                                                             "overridden"):
                f["status"] = "open"
            elif action == "accept" and (f.get("source") or {}).get("kind") == "guard" \
                    and f.get("status") == "open" \
                    and o.get("before_text") == f.get("original") \
                    and o.get("after_text") == f.get("fixed"):
                # A person accepted the security change BOSS could not explain.
                f["status"] = "overridden"
    # HELD AND BLOCKED FIXES ARE RELEASED BY PEOPLE (validators.record): a `human` / `never`
    # rule's fix counts once a person accepted (or wrote) it; a P0 whose hunk carried an
    # unexplained security change, once that change is accepted, rejected or gone.
    for f in findings:
        if f.get("status") != "open" or not f.get("fixed_if_released"):
            continue
        last = f.get("override") or {}
        if f.get("held") and last.get("action") not in ("accept", "edit", "remove"):
            continue
        if any((by_id.get(str(b)) or {}).get("status") in ("open", "reverted")
               for b in f.get("blocked_by") or []):
            continue
        f["status"] = "fixed"
        if f.get("held"):
            f["fix_kind"] = "human"
    return findings


def packet(c: Any, ch: Change, *, head_sha: Optional[str] = None) -> Optional[dict]:
    """The change's current packet, v(1 + overrides), overrides applied, re-run state carried."""
    ovrs = overrides(c, ch.project_id, ch.number)
    rr = latest_rerun(c, ch.project_id, ch.number)
    decision = _merge_decision(c, ch)
    run_id = ((decision or {}).get("recommendation") or {}).get("run_id") or ch.run_id
    if not run_id:
        return None
    followups = ((decision or {}).get("recommendation") or {}).get("followups")
    validation = validation_of(c, ch)
    p = rp.for_run(c, str(run_id), head_sha=head_sha or ch.head(), change=ch.number,
                   project_id=ch.project_id,
                   followups=followups, validation=validation,
                   packet_version=1 + len(ovrs),
                   rerun=({"id": rr["id"], "state": rr["state"],
                           "created_at": _iso(rr.get("created_at")),
                           **({"error": rr["error"]} if rr.get("error") else {})}
                          if rr else None),
                   apply_overrides=lambda fs: apply(fs, ovrs),
                   explained_hunks=override_hunks(validation, ovrs),
                   overrides=[{"id": o["ref"], "finding": o.get("finding_id"),
                               "action": o["action"], "actor": o.get("actor"),
                               "actor_name": o.get("actor_name"),
                               "reason": o.get("reason"), "approver": o.get("approver"),
                               "approved_at": o.get("approved_at"),
                               "expires_at": o.get("expires_at"),
                               "commit_sha": o.get("commit_sha"), "at": o.get("created_at")}
                              for o in ovrs])
    return p


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _override_lines(o: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """The lines an override's commit added and removed (its masked before → after text)."""
    import difflib

    a = str(o.get("before_text") or "").splitlines()
    b = str(o.get("after_text") or "").splitlines()
    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += a[i1:i2]
        if tag in ("replace", "insert"):
            added += b[j1:j2]
    return added, removed


def override_hunks(validation: Optional[Mapping[str, Any]], ovrs: Sequence[Mapping[str, Any]]
                   ) -> dict[str, list[str]]:
    """{hunk id: [O-n]}: the hunks of the change (input → target) that a person's override
    WROTE — every line the hunk adds is a line that override's commit added (or, for a hunk
    that only removes, every line it removes is one the override removed). A person's own edit
    is explained by that edit, never an "unexplained change" that blocks the merge (dry run 2,
    gap 6: O-1's edit of the CORS origin came back as unexplained_hunk:h-04)."""
    if not validation:
        return {}
    committed = [(o["ref"], *_override_lines(o)) for o in ovrs
                 if o.get("commit_sha") and (o.get("before_text") or o.get("after_text"))]
    if not committed:
        return {}
    out: dict[str, list[str]] = {}
    for x in validation.get("fixes") or []:
        for h in x.get("hunks") or []:
            hid, after, before = str(h.get("id")), list(h.get("after") or []), \
                list(h.get("before") or [])
            for ref, added, removed in committed:
                if (after and all(line in added for line in after)) or (
                        not after and before and all(line in removed for line in before)):
                    out.setdefault(hid, [])
                    if ref not in out[hid]:
                        out[hid].append(ref)
    return out


def explain_fixes(validation: Optional[dict], ovrs: Sequence[Mapping[str, Any]]) -> Optional[dict]:
    """The record as the Validation tab shows it: a fixes[] entry whose hunks a person's
    override wrote is that override's (``override``, kind ``human``) — the person's line, never
    an agent's and never ``unexplained``."""
    if not validation:
        return validation
    by_hunk = override_hunks(validation, ovrs)
    if not by_hunk:
        return validation
    fixes = []
    for x in validation.get("fixes") or []:
        refs = sorted({r for h in x.get("hunks") or [] for r in by_hunk.get(str(h.get("id")), [])})
        if refs:
            x = {**{k: v for k, v in x.items() if k != "unexplained"}, "kind": "human",
                 "override": refs[0], "overrides": refs}
        fixes.append(x)
    return {**validation, "fixes": fixes}


def refresh_decision(c: Any, ch: Change, p: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The merge card shows the new packet (and refuses while it re-runs)."""
    d = _merge_decision(c, ch)
    if d is None or p is None:
        return None
    c.execute(text(
        "UPDATE decisions SET recommendation = jsonb_set(recommendation, '{packet}', "
        "CAST(:p AS JSONB)), updated_at = now() WHERE id = :i"),
        {"p": json.dumps(p, default=str), "i": d["id"]})
    return str(d["id"])


def state(c: Any, settings: Any, project_id: str, number: int) -> dict:
    """GET …/validation: boss.validation/v1 + overrides + re-run state (+ the current packet)."""
    ch = change(c, settings, project_id, number)
    if ch.kind == "local":
        on_head_moved(c, settings, project_id, number, ch.head())
    rr = latest_rerun(c, project_id, number)
    ovrs = overrides(c, project_id, number)
    validation = explain_fixes(validation_of(c, ch), ovrs)
    if validation and (validation.get("input") or {}).get("file"):
        # THE PARTNER'S FILE, masked, for the line gutter: its bytes as the check received them
        # (the input at the branch head when it still hashes to the record's sha256).
        from .validators import mask, sha256

        try:
            body = ch.repo.read_text_file(ch.branch, validation["input"]["file"])
        except Exception:  # noqa: BLE001 — the gutter falls back to the quoted lines
            body = None
        if body is not None and sha256(body) == validation["input"].get("sha256"):
            validation["input"] = {**validation["input"], "text": mask(body)}
    return {"validation": validation, "overrides": ovrs,
            "rerun": ({k: (v.isoformat() if hasattr(v, "isoformat") else v)
                       for k, v in rr.items() if k != "validation"} if rr else None),
            "head_sha": ch.head(), "packet": packet(c, ch)}


# ── edits of the target ────────────────────────────────────────────────────────────────────────

def _lines(t: str) -> tuple[list[str], bool]:
    return t.splitlines(), t.endswith("\n")


def _join(lines: list[str], nl: bool) -> str:
    return "\n".join(lines) + ("\n" if nl and lines else "")


def reject_hunk(input_text: str, target_text: str, anchor: int) -> str:
    """The target with the hunk anchored at input line ``anchor`` (1-based; the line after which
    an insertion sits) replaced by the partner's exact lines."""
    from .validators import hunks

    a, nl = _lines(input_text)
    b, tnl = _lines(target_text)
    for h in hunks(input_text, target_text):
        if h["i1"] + 1 == anchor:
            return _join(b[: h["j1"]] + a[h["i1"]: h["i2"]] + b[h["j2"]:], tnl)
    raise OverrideError(409, "hunk_gone", "That change is no longer on the branch.")


def edit_target(input_text: str, target_text: str, anchor: Optional[int], line: Optional[int],
                replacement: str) -> str:
    """The hunk (anchored at input line ``anchor``) or else the target's ``line`` replaced by
    the person's text."""
    from .validators import hunks

    b, tnl = _lines(target_text)
    new = replacement.splitlines()
    if anchor is not None:
        for h in hunks(input_text, target_text):
            if h["i1"] + 1 == anchor:
                return _join(b[: h["j1"]] + new + b[h["j2"]:], tnl)
    if line and 0 < line <= len(b):
        return _join(b[: line - 1] + new + b[line:], tnl)
    raise OverrideError(409, "no_line", "There is no line to edit for that finding.")


def finding_target_line(target_text: str, finding: Mapping[str, Any]) -> Optional[int]:
    """The 1-based TARGET line a finding is on: its ``target_line`` when that line still reads
    its ``original`` (masked compare), else the one target line that does. None when there is
    no such line, or more than one and none recorded."""
    from .validators import mask

    b = target_text.splitlines()
    loc = finding.get("location") or {}
    orig = finding.get("original")
    at = loc.get("target_line", loc.get("line"))
    if orig is None:
        return at if isinstance(at, int) and 0 < at <= len(b) else None
    first = str(orig).splitlines()[0] if str(orig) else ""
    if isinstance(at, int) and 0 < at <= len(b) and mask(b[at - 1]) == first:
        return at
    hits = [n for n, x in enumerate(b, start=1) if mask(x) == first]
    return hits[0] if len(hits) == 1 else None


def remove_block(target_text: str, finding: Mapping[str, Any]) -> str:
    """The target without the finding's whole BLOCK (validators.block): a route's `- path:`
    line and every key under it, or a key's own line (and anything nested under it). A person
    removing `/v1/admin/accounts` deletes its 11 lines; edit_target only ever replaced one hunk
    or one line (dry run 2, gap 2). 409 when the finding's line is not on the branch, or its
    block would be the whole file."""
    from .validators import block

    b, nl = _lines(target_text)
    at = finding_target_line(target_text, finding)
    if at is None:
        raise OverrideError(409, "no_line", "The finding's line is not on the branch; use Edit.")
    lo, hi = block(b, at - 1)
    if (lo, hi) == (0, len(b)):
        raise OverrideError(409, "no_block", "That would remove the whole file; use Edit.")
    return _join(b[:lo] + b[hi:], nl)


def _trailers(ref: str, finding: str, action: str, actor: str, reason: Optional[str]) -> str:
    return (f"BOSS override {ref}: {action} {finding}\n\n"
            f"BOSS-Override: {ref} {finding} {action}\nOn-behalf-of: {actor}\n"
            f"Reason: {' '.join(str(reason or '-').split())[:300]}")


# ── the override ───────────────────────────────────────────────────────────────────────────────

def override(c: Any, settings: Any, project_id: str, number: int, *, action: str,
             finding_id: Optional[str], head_sha: str, actor: str, text_: Optional[str] = None,
             reason: Optional[str] = None, approver_id: Optional[str] = None,
             expires_at: Optional[str] = None) -> dict:
    """One person's override (or ``revert_all``). Returns ``{override, packet, rerun, decision}``.
    Raises OverrideError: 409 stale head (with the current packet), 403 a P0 waive without a
    second person, 422 a missing reason/text, 404 an unknown finding."""
    if action not in (*ACTIONS, "revert_all"):
        raise OverrideError(422, "bad_action", f"Unknown action {action!r}.")
    if action == "edit" and not (text_ or "").strip():
        # AN EDIT WITH NO TEXT REMOVES THE FINDING'S BLOCK (dry run 2, gap 2): recorded as
        # what it is, `remove`, a person's fix with a commit.
        action = "remove"
    ch = change(c, settings, project_id, number)
    if ch.kind != "local":
        raise OverrideError(422, "github_unsupported", GITHUB_UNSUPPORTED)
    # ONE OVERRIDE AT A TIME per change: the change row (and a lock on its key, for a change
    # with no row yet) is held to the end of this transaction.
    c.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
              {"k": f"finding_overrides:{project_id}:{int(number)}"})
    c.execute(text("SELECT id FROM local_change_sets WHERE project_id = :p AND number = :n "
                   "FOR UPDATE"), {"p": project_id, "n": int(number)})
    # IDEMPOTENT ON (finding, head, action): a replay (a retried request, the same click after a
    # step-up) returns the override already made, and starts no second re-run.
    replay = _replayed(c, ch, finding_id or "*", action, head_sha)
    if replay is not None:
        return replay
    head = ch.head()
    if not head_sha or head_sha != head:
        raise OverrideError(409, "stale_head", "The change moved on; review the newer packet.",
                            head_sha=head, packet=packet(c, ch, head_sha=head))
    rr = latest_rerun(c, project_id, number)
    if rr and rr["state"] in ("pending", "running"):
        raise OverrideError(409, "rerun_pending", "BOSS is re-checking the last change; wait for it.")
    validation = validation_of(c, ch)
    if not validation:
        raise OverrideError(409, "no_validation", f"Change #{number} has no validation to override.")
    current = packet(c, ch, head_sha=head) or {}
    finding = next((f for f in current.get("findings") or [] if f.get("id") == finding_id), None)
    if action != "revert_all" and finding is None:
        raise OverrideError(404, "no_finding", f"There is no finding {finding_id} on change #{number}.")
    reason = (reason or "").strip()[:REASON_MAX] or None
    if action in ("waive", "reject", "revert_all") and not reason:
        raise OverrideError(422, "reason_required", "Say why.")
    p0_waive = action == "waive" and (finding or {}).get("severity") == "P0"
    if p0_waive and (not approver_id or approver_id == actor):
        raise OverrideError(403, "approver_required",
                            "Waiving a P0 needs a second person to approve: name them.")
    if p0_waive and not approver_may_merge(c, project_id, str(approver_id)):
        raise OverrideError(403, "approver_cannot_merge",
                            "The approver must be a person on this project who can merge.")
    if action == "waive" and (finding or {}).get("source", {}).get("kind") == "validator" \
            and finding.get("status") == "fixed":
        raise OverrideError(409, "already_fixed", f"{finding_id} is already fixed.")

    ref = f"O-{len(overrides(c, project_id, number)) + 1}"
    oid = make_id("ovr")
    from .validators import mask

    before_text = after_text = commit_sha = None
    if action == "accept" and ((finding or {}).get("source") or {}).get("kind") == "guard":
        # A GUARD IS ACCEPTED FOR ITS CONTENT, not its id: U-nn are numbered per record, so a
        # later push's different security change may reuse U-01. apply() overrides the guard
        # only while its lines are exactly the ones the person saw.
        before_text, after_text = finding.get("original"), finding.get("fixed")
    # ACCEPTING A "NEEDS YOU" PROPOSAL: the agent's change BOSS refused for a `human` / `never`
    # rule (input_fidelity.enforce_autofix) is applied now, by this person, as one commit.
    proposal = (finding or {}).get("proposed") if action == "accept" else None
    if proposal and (finding or {}).get("status") != "open":
        proposal = None
    if proposal and not proposal.get("applicable"):
        raise OverrideError(409, "edit_required",
                            "The proposed line holds a value BOSS does not keep; write the line "
                            "with Edit.")
    if action in COMMITS or proposal:
        inp, target = (validation.get("input") or {}).get("file"), validation.get("target")
        input_text = ch.repo.read_text_file(ch.branch, inp)
        target_text = ch.repo.read_text_file(ch.branch, target)
        if input_text is None or target_text is None:
            raise OverrideError(409, "file_gone", f"`{inp}` or `{target}` is not on the branch.")
        hunk = next((h for h in validation.get("hunks") or []
                     if h.get("id") == (finding or {}).get("hunk_id")), None)
        anchor = int(hunk["line"]) if hunk else None
        if action == "revert_all":
            from .validators import sha256 as _sha

            if _sha(input_text) != (validation.get("input") or {}).get("sha256"):
                raise OverrideError(409, "input_changed",
                                    f"`{inp}` on the branch is not the file the check received.")
            new_text = input_text
        elif proposal:
            new_text = apply_proposal(target_text, proposal)
        elif action == "reject":
            if anchor is None:
                raise OverrideError(409, "no_fix", f"{finding_id} has no fix to reject.")
            new_text = reject_hunk(input_text, target_text, anchor)
        elif action == "remove":
            new_text = remove_block(target_text, finding or {})
        else:
            new_text = edit_target(input_text, target_text, anchor,
                                   finding_target_line(target_text, finding or {}), text_ or "")
        if new_text == target_text:
            raise OverrideError(409, "no_change", "That would change nothing on the branch.")
        before_text, after_text = mask(target_text), mask(new_text)
        commit_sha = ch.repo.upsert_file(ch.branch, target, new_text,
                                         _trailers(ref, finding_id or "*", action, actor, reason))
    c.execute(text(
        "INSERT INTO finding_overrides (id, project_id, change_number, finding_id, action, actor, "
        "reason, approver, expires_at, before_text, after_text, commit_sha, packet_version, "
        "head_sha) VALUES (:i, :p, :n, :f, :a, :actor, :r, :ap, CAST(:e AS timestamptz), :bt, "
        ":at, :cs, :v, :h)"),
        {"i": oid, "p": project_id, "n": int(number), "f": finding_id or "*", "a": action,
         "actor": actor, "r": reason, "ap": approver_id, "e": expires_at, "bt": before_text,
         "at": after_text, "cs": commit_sha, "v": int(current.get("packet_version") or 1) + 1,
         "h": head})
    decision_id = None
    if p0_waive:
        from .core import create_blocking_decision

        decision_id = create_blocking_decision(
            c, project_id, None,
            f"Waive P0 {finding_id} ({(finding.get('source') or {}).get('rule_id')}) on change "
            f"#{number}?",
            {"action": WAIVE_ACTION, "override_id": oid, "finding_id": finding_id,
             "change_number": int(number), "approver": approver_id, "asked_by": actor,
             "reason": reason, "expires_at": expires_at, "head_sha": head},
            {}, "p0_waive_needs_approval", risk_tier=WAIVE_RISK_TIER, default_action="hold")
    rerun_id = start_rerun(c, ch, head=commit_sha or head, finding_id=finding_id)
    p = packet(c, ch, head_sha=commit_sha or head)
    card = refresh_decision(c, ch, p)
    emit_event(c, project_id, "validation.override", actor, "finding_override", oid,
               {"ref": ref, "finding": finding_id, "action": action, "change": int(number),
                "commit_sha": commit_sha, "rerun_id": rerun_id, "waive_decision": decision_id})
    return {"override": {"id": oid, "ref": ref, "action": action, "finding_id": finding_id,
                         "commit_sha": commit_sha, "approval_decision": decision_id},
            "rerun": {"id": rerun_id, "state": "pending"}, "packet": p, "decision": card}


def _replayed(c: Any, ch: Change, finding_id: str, action: str, head_sha: Optional[str]
              ) -> Optional[dict]:
    """The response to a replayed override: the finding's LAST override, when it is this same
    action at this same head (an accept → reopen → accept is three overrides, not a replay)."""
    if not head_sha:
        return None
    rows = overrides(c, ch.project_id, ch.number)
    mine = [o for o in rows if o.get("finding_id") == finding_id]
    last = mine[-1] if mine else None
    if last is None or last.get("action") != action or last.get("head_sha") != head_sha:
        return None
    rr = None
    if last.get("rerun_id"):
        rr = c.execute(text("SELECT id, state FROM validation_reruns WHERE id = :i"),
                       {"i": last["rerun_id"]}).mappings().first()
    d = _merge_decision(c, ch)
    return {"override": {"id": last["id"], "ref": last["ref"], "action": action,
                         "finding_id": finding_id, "commit_sha": last.get("commit_sha"),
                         "approval_decision": None},
            "rerun": ({"id": rr["id"], "state": rr["state"]} if rr else None),
            "packet": packet(c, ch), "decision": (d or {}).get("id"), "replayed": True}


def apply_proposal(target_text: str, proposal: Mapping[str, Any]) -> str:
    """The target with a refused proposal applied: each piece's ``remove`` lines (found on the
    target as one unique run, compared masked) replaced by its ``after`` lines, or ``after``
    inserted after its ``anchor`` line. 409 when the lines are no longer there exactly once."""
    from .validators import mask

    b, nl = _lines(target_text)
    # THE PIECES ARE IN FILE ORDER: each is looked for AFTER the previous one, so a line every
    # route repeats (`upstream: lumen-pay-core`) is found in the right route. Matching each
    # piece across the whole file made the real run's V-07 proposal "gone" (dry run 2, gap 2):
    # its second piece's anchor appears once per route.
    cursor = 0
    for piece in proposal.get("pieces") or []:
        masked = [mask(x) for x in b]
        rem = [str(x) for x in piece.get("remove") or []]
        new = [str(x) for x in piece.get("after") or []]
        if rem:
            at = [k for k in range(cursor, len(b) - len(rem) + 1)
                  if masked[k:k + len(rem)] == rem]
            if not at:
                raise OverrideError(409, "proposal_gone", "The lines this proposal changes are "
                                    "no longer on the branch; use Edit.")
            b[at[0]:at[0] + len(rem)] = new
            cursor = at[0] + len(new)
        else:
            anchor = piece.get("anchor")
            at = ([k + 1 for k in range(cursor, len(b)) if masked[k] == anchor]
                  if anchor is not None else [0])
            if not at:
                raise OverrideError(409, "proposal_gone", "The line this proposal follows is not "
                                    "on the branch; use Edit.")
            b[at[0]:at[0]] = new
            cursor = at[0] + len(new)
    return _join(b, nl)


def approver_may_merge(c: Any, project_id: str, approver_id: str) -> bool:
    """The named approver is a PERSON (a user principal, not an agent, key or service) who holds
    `github:merge` on this project."""
    try:
        from ..identity import P
        from ..identity.authz import effective_permissions
        from ..identity.principal import PrincipalKind
        from ..identity.scope import ResolvedScope
        from ..identity.users import load_principal

        principal = load_principal(c, approver_id)
        if principal is None or principal.kind != PrincipalKind.USER:
            return False
        held = effective_permissions(principal, ResolvedScope.in_project(principal.org_id,
                                                                         project_id), db=c)
        return P.GITHUB_MERGE in held.permissions
    except Exception:  # noqa: BLE001 — an approver BOSS cannot check is not an approver
        log.info("approver %s not checked", approver_id, exc_info=True)
        return False


def rebuild(c: Any, settings: Any, project_id: str, number: int, *, actor: str,
            reason: Optional[str] = None) -> dict:
    """"Rebuild packet": a re-run of the recorded validator at the branch head (after a failed
    or overdue re-run, or an invalid packet). Same rules as an override: local changes only,
    one at a time."""
    ch = change(c, settings, project_id, number)
    if ch.kind != "local":
        raise OverrideError(422, "github_unsupported", GITHUB_UNSUPPORTED)
    c.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
              {"k": f"finding_overrides:{project_id}:{int(number)}"})
    rr = latest_rerun(c, project_id, number)
    if rr and rr["state"] in ("pending", "running"):
        raise OverrideError(409, "rerun_pending", "BOSS is re-checking already; wait for it.")
    if not validation_of(c, ch):
        raise OverrideError(409, "no_validation", f"Change #{number} has no validation to re-run.")
    head = ch.head()
    rid = start_rerun(c, ch, head=head, finding_id=None)
    p = packet(c, ch, head_sha=head)
    refresh_decision(c, ch, p)
    emit_event(c, project_id, "validation.rebuild", actor, "validation_rerun", rid,
               {"change": int(number), "reason": (reason or "")[:300]})
    return {"rerun": {"id": rid, "state": "pending"}, "packet": p}


def on_head_moved(c: Any, settings: Any, project_id: str, number: int, head: str) -> Optional[str]:
    """A push to the change branch, or a follow-up build's commit, moved change #n to ``head``.
    When its validation ran on another head, a re-run starts at this one — the packet says
    "stale → re-checking" (``stale``, ``stale_evidence``) until it is done. The re-run id, or
    None when nothing needed it. Never raises: a push is never refused for this."""
    try:
        with c.begin_nested():
            ch = change(c, settings, project_id, int(number))
            if ch.kind != "local" or not head:
                return None
            validation = validation_of(c, ch)
            if not validation:
                return None
            rr = latest_rerun(c, project_id, int(number))
            if rr and rr.get("head_sha") == head:
                return None                      # already re-checked (or re-checking) there
            ev = rp.evidence_head(validation)
            if ev == head:
                return None
            if ev is None and rr is None:
                # An older record with no stamped head: the merge card's head is its head; with
                # neither, BOSS does not know it is stale and claims nothing.
                d = _merge_decision(c, ch)
                ev = ((d or {}).get("recommendation") or {}).get("head_sha")
                if not ev or ev == head:
                    return None
            rid = start_rerun(c, ch, head=head, finding_id=None)
            refresh_decision(c, ch, packet(c, ch, head_sha=head))
            emit_event(c, project_id, "validation.stale_rerun", "boss", "validation_rerun", rid,
                       {"change": int(number), "head_sha": head, "evidence_head": ev})
            return rid
    except Exception:  # noqa: BLE001 — the push stands; the merge gate still sees the head
        log.info("stale re-run not started for %s #%s", project_id, number, exc_info=True)
        return None


def head_for_url(c: Any, url: Any) -> Optional[str]:
    """A local change's current head, from its URL (``http://boss-local/<o>/<r>/pull/<n>``)."""
    import re

    m = re.search(r"://[^/]+/([^/]+/[^/]+)/pull/(\d+)", str(url or ""))
    if not m:
        return None
    try:
        with c.begin_nested():
            return c.execute(text(
                "SELECT head_sha FROM local_change_sets WHERE repository = :r AND number = :n "
                "AND state = 'open' ORDER BY created_at DESC LIMIT 1"),
                {"r": m.group(1), "n": int(m.group(2))}).scalar()
    except Exception:  # noqa: BLE001 — no head known: the gate falls back to the packet's verdict
        return None


def start_rerun(c: Any, ch: Change, *, head: str, finding_id: Optional[str]) -> str:
    """Queue the re-run: the validator on the target at ``head`` (job ``validation.rerun``)."""
    from .jobs import enqueue_job

    rid = make_id("vrr")
    c.execute(text(
        "INSERT INTO validation_reruns (id, project_id, change_number, head_sha, state, finding_id) "
        "VALUES (:i, :p, :n, :h, 'pending', :f)"),
        {"i": rid, "p": ch.project_id, "n": ch.number, "h": head, "f": finding_id})
    c.execute(text("UPDATE finding_overrides SET rerun_id = :r WHERE project_id = :p "
                   "AND change_number = :n AND rerun_id IS NULL"),
              {"r": rid, "p": ch.project_id, "n": ch.number})
    enqueue_job(c, ch.project_id, JOB_KIND, {"rerun_id": rid}, priority=20,
                dedupe_key=f"{JOB_KIND}:{rid}")
    return rid


def on_waive_approved(c: Any, settings: Any, recommendation: Mapping[str, Any], actor: str) -> None:
    """The named approver approved a P0 waive: the override is approved, the packet rebuilt."""
    approver = str(recommendation.get("approver") or "")
    if actor and actor == str(recommendation.get("asked_by") or ""):
        raise OverrideError(403, "requester_cannot_approve",
                            "The person who asked for this waive cannot approve it.")
    if not approver or actor != approver:
        raise OverrideError(403, "not_the_approver",
                            "Only the person named on this waive can approve it.")
    row = c.execute(text("SELECT project_id, change_number FROM finding_overrides WHERE id = :i"),
                    {"i": recommendation.get("override_id")}).mappings().first()
    if row is None:
        raise OverrideError(404, "no_override", "That waive is gone.")
    c.execute(text("UPDATE finding_overrides SET approved_at = now() WHERE id = :i"),
              {"i": recommendation.get("override_id")})
    ch = change(c, settings, row["project_id"], int(row["change_number"]))
    refresh_decision(c, ch, packet(c, ch))


# ── the re-run (worker → API: the sandbox runs the validator) ──────────────────────────────────

def _checkout(ch: Change, workdir: Path) -> None:
    from . import owned_process

    if ch.local_git_dir is None:
        raise OverrideError(409, "not_local", "Re-runs check out local changes only for now.")
    proc = owned_process.boss_run(["git", "clone", "--quiet", "--branch", ch.branch,
                                   str(ch.local_git_dir), str(workdir)],
                                  capture_output=True, text=True, timeout=300, umask=0)
    if proc.returncode != 0:
        raise OverrideError(409, "checkout_failed", (proc.stderr or "git clone failed")[-300:])


def _sandbox_run(workdir: Path, env: Mapping[str, str]) -> Callable[[str], Any]:
    from types import SimpleNamespace

    from . import owned_process

    def run(cmd: str) -> Any:
        proc = owned_process.run(shlex.split(cmd), cwd=str(workdir), timeout=300,
                                 capture_output=True, text=True, env=dict(env))
        return SimpleNamespace(stdout=proc.stdout or "", exit_code=proc.returncode)
    return run


def _tree_head(tree: Path) -> Optional[str]:
    import subprocess

    if not (Path(tree) / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _safe_rel(path: Any) -> str:
    p = str(path or "")
    if not p or p.startswith("/") or "\\" in p or ".." in Path(p).parts:
        raise OverrideError(409, "bad_path", f"Refusing the path {p!r}: not inside the repository.")
    return p


def trusted_validator(rec: Mapping[str, Any]) -> tuple[dict, Optional[dict]]:
    """The validator and ruleset AS THE ORIGINAL CHECK TRUSTED THEM (its record), never what the
    change branch now declares: the agent's branch cannot swap the command or lower a P0."""
    v = rec.get("validator") or {}
    if not v.get("run"):
        raise OverrideError(409, "no_validator_command",
                            "The original check recorded no validator command to re-run.")
    rs = rec.get("ruleset")
    ruleset = ({"ruleset": rs.get("id"), "version": rs.get("version"), "sha": rs.get("sha"),
                "rules": rs.get("rules") or {}, "custom": rs.get("custom") or []}
               if isinstance(rs, Mapping) and rs.get("rules") is not None else None)
    return ({"name": v.get("id"), "check": v.get("run"), "fix": v.get("fix"),
             "adapter": None if v.get("adapter") in (None, "json") else v.get("adapter"),
             "rules_doc": v.get("rules_doc"), "version": v.get("version")}, ruleset)


def _record(engine: Any, rerun_id: str, state_: str, rec: Optional[dict], error: Optional[str],
            ch: Optional[Change], row: Mapping[str, Any]) -> dict:
    with engine.begin() as c:
        c.execute(text(
            "UPDATE validation_reruns SET state = :s, validation = CAST(:v AS JSONB), error = :e, "
            "finished_at = now() WHERE id = :i"),
            {"s": state_, "v": json.dumps(rec) if rec is not None else None,
             "e": (error or (rec or {}).get("error") or None), "i": rerun_id})
        p = None
        if ch is not None:
            try:
                with c.begin_nested():
                    p = packet(c, ch, head_sha=row["head_sha"])
                    refresh_decision(c, ch, p)
            except Exception:  # noqa: BLE001 — the re-run's result stands; the card catches up
                log.warning("packet not refreshed after re-run %s", rerun_id, exc_info=True)
        emit_event(c, row["project_id"], f"validation.rerun_{state_}", "system", "validation_rerun",
                   rerun_id, {"change": int(row["change_number"]), "error": error,
                              "packet_version": (p or {}).get("packet_version")})
    return {"ok": state_ == "done", "state": state_, "error": error}


def execute_rerun(engine: Any, settings: Any, rerun_id: str, *,
                  do_checkout: Optional[Callable[[Change, Path], None]] = None,
                  make_run: Optional[Callable[[Path], Callable[[str], Any]]] = None) -> dict:
    """Run one queued re-run to its end. Idempotent: a finished one is returned as is. Every
    way it can fail — the change gone, the checkout, the command, a bad path — is RECORDED as
    `failed` with the reason (the card then lets a person force the merge). The validator and
    ruleset come from the original check's record (``trusted_validator``).
    ``do_checkout`` / ``make_run`` are seams for tests (the real ones need the toolchain)."""
    from . import input_fidelity, owned_process, sandbox_identity

    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM validation_reruns WHERE id = :i"),
                        {"i": rerun_id}).mappings().first()
        if row is None:
            return {"ok": False, "error": f"no such re-run: {rerun_id}"}
        row = dict(row)
        if row["state"] in ("done", "failed"):
            return {"ok": row["state"] == "done", "state": row["state"]}
        c.execute(text("UPDATE validation_reruns SET state = 'running', started_at = now() "
                       "WHERE id = :i"), {"i": rerun_id})
    ch: Optional[Change] = None
    rec, error = None, None
    try:
        with engine.begin() as c:
            ch = change(c, settings, row["project_id"], int(row["change_number"]))
            before = validation_of(c, ch) or {}
        inp = _safe_rel((before.get("input") or {}).get("file"))
        target = _safe_rel(before.get("target"))
        v, ruleset = trusted_validator(before)
        owner_id = f"vrerun_{rerun_id}"
        with owned_process.owner_scope("sandbox", owner_id, project_id=row["project_id"], fresh=True):
            with sandbox_identity.bind(owner_id, project_id=row["project_id"]) as ident:
                base = ident.workspace("vrr-") if ident is not None else Path(
                    tempfile.mkdtemp(prefix="boss-vrr-"))
                try:
                    tree = base / "src"
                    (do_checkout or _checkout)(ch, tree)
                    input_text = (tree / inp).read_text(errors="replace")
                    contract = {"input": inp, "output": target, "answer_files": [],
                                "validator": v, "source": "rerun", "_input_text": input_text,
                                "_output_base": None, "_answer_texts": {}, "ruleset": ruleset}
                    if make_run is not None:
                        run = make_run(tree)
                    else:
                        from ..security_scan.runners import tool_env

                        run = _sandbox_run(tree, tool_env(ident.home if ident else base))
                    rec = input_fidelity.public_validation(
                        input_fidelity.run_validation(tree, contract, run, ruleset=ruleset))
                    # THE HEAD THIS EVIDENCE RAN ON (the packet claims no other).
                    if isinstance(rec.get("after"), dict):
                        rec["after"]["head_sha"] = _tree_head(tree) or row.get("head_sha")
                finally:
                    if ident is None:
                        shutil.rmtree(base, ignore_errors=True)
    except OverrideError as exc:
        error = exc.message
    except Exception as exc:  # noqa: BLE001 — a re-run that cannot run is FAILED, with why
        log.warning("validation re-run %s failed", rerun_id, exc_info=True)
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    state_ = "failed" if error or rec is None or rec.get("error") else "done"
    return _record(engine, rerun_id, state_, rec, error, ch, row)
