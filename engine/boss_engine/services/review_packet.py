"""THE REVIEW PACKET: ``boss.review_packet/v2`` (SPEC docs/design/validation-review §1.4).

What the merge card and the change page render, built by the engine from step outputs — the
Validate step's ``boss.validation/v1`` record, the build's test runs, the review seats'
follow-ups, the people's overrides — never written by a model. ``validate_packet`` enforces the
anti-slop rules (one test each); a packet that breaks one is ``verdict.state = invalid`` and the
merge is refused (ci_gate) until it is rebuilt. The engine never falls back to prose.

Rules (``validate_packet``):
1. every field is present;
2. free text only in ``claims[].text`` / ``risks[].text``, each ≤ 120 chars with ≥ 1 ref that
   resolves to a finding, evidence, override, test or contract id in this packet;
3. ``counts`` equal what ``findings`` recompute to;
4. ``status: fixed`` needs after-evidence (``absent: true``) and a ``hunk_id`` (a test finding:
   a passing run at the packet's head);
5. no build prose (``impl_summary``, review markdown, …) anywhere in it;
6. any failure → ``verdict = {state: invalid, reasons: ["rule<n>:<what>"]}``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

SCHEMA = "boss.review_packet/v2"
KEY = "packet"
PLEVELS = ("P0", "P1", "P2", "P3")
REQUIRED = ("schema", "packet_version", "head_sha", "built_at", "work", "verdict", "counts",
            "findings", "vs_input", "validators", "tests", "contract", "risks", "claims")
TEXT_MAX = 120
#: Build prose that never reaches the packet (rule 5): it stays in the build's log.
PROSE_KEYS = frozenset({"impl_summary", "qa_summary", "review_summary", "security_summary",
                        "consistency_summary", "summary_of", "review_markdown", "summary",
                        "rationale", "explanation"})
STATUSES = ("open", "fixed", "overridden", "reverted", "waived", "suppressed")
_ORDER = {"open": 0, "overridden": 2, "reverted": 2, "waived": 3, "suppressed": 4, "fixed": 5}
_LOC = re.compile(r"^\s*`?([\w./-]+\.\w+)(?::(\d+))?`?\s*[—–-]\s*")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(value: Any, default: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


# ── counts and verdict (computed, never written) ───────────────────────────────────────────────

def count(findings: Sequence[Mapping[str, Any]]) -> dict:
    out: dict[str, Any] = {p: {"found": 0, "open": 0, "waived": 0} for p in PLEVELS}
    out["unmapped"] = 0
    for f in findings:
        p = f.get("severity") if f.get("severity") in PLEVELS else "P2"
        out[p]["found"] += 1
        if f.get("status") == "open":
            out[p]["open"] += 1
        elif f.get("status") == "waived":
            out[p]["waived"] += 1
        if f.get("unmapped"):
            out["unmapped"] += 1
    return out


def _sort_key(f: Mapping[str, Any]) -> tuple:
    status = str(f.get("status") or "open")
    rank = _ORDER.get(status, 1)
    if status == "open":
        rank = {"P0": 0, "P1": 1}.get(str(f.get("severity")), 1.5)
    return (rank, PLEVELS.index(f["severity"]) if f.get("severity") in PLEVELS else 9,
            str(f.get("id")))


def verdict(packet: Mapping[str, Any]) -> dict:
    """ready, or blocked with typed reasons: an open P0/P1, a substituted input, a validator
    that could not report, a failing test, an unexplained change to the partner's file, a re-run
    still pending."""
    reasons: list[str] = []
    for f in packet.get("findings") or []:
        # Only a validator, test or contract finding blocks; a review follow-up the quality bar
        # let through is listed, open, with where it went (deferred_to) — the light lane. A
        # REJECTED fix puts the finding back (status `reverted`): it blocks like an open one.
        if f.get("status") in ("open", "reverted") and f.get("severity") in ("P0", "P1") \
                and (f.get("source") or {}).get("kind") in ("validator", "test", "contract") \
                and not f.get("deferred_to"):
            reasons.append(f"open_{str(f['severity']).lower()}:{f['id']}")
    for v in packet.get("validators") or []:
        if v.get("guard"):
            reasons.append(f"input_substituted:{v['guard'].get('source')}")
        elif v.get("error"):
            reasons.append(f"validator_error:{v.get('id')}")
    for t in packet.get("tests") or []:
        if t.get("ok") is False:
            reasons.append(f"tests_failed:{t['id']}")
    for c in packet.get("contract") or []:
        if c.get("got") != c.get("expect"):
            reasons.append(f"contract_failed:{c['id']}")
    for h in (packet.get("vs_input") or {}).get("unexplained_hunks") or []:
        reasons.append(f"unexplained_hunk:{h}")
    if (packet.get("rerun") or {}).get("state") in ("pending", "running"):
        reasons.append("rerun_pending")
    elif (packet.get("rerun") or {}).get("state") == "failed":
        reasons.append("rerun_failed")
    return {"state": "blocked" if reasons else "ready", "reasons": reasons}


# ── the anti-slop rules ────────────────────────────────────────────────────────────────────────

def known_ids(packet: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for f in packet.get("findings") or []:
        ids.add(str(f.get("id")))
        for e in f.get("evidence") or []:
            if isinstance(e, Mapping) and e.get("id"):
                ids.add(str(e["id"]))
        if isinstance(f.get("override"), Mapping) and f["override"].get("id"):
            ids.add(str(f["override"]["id"]))
    for v in packet.get("validators") or []:
        ids.update(str(e) for e in v.get("evidence_ids") or [])
    for t in packet.get("tests") or []:
        ids.update({str(t.get("id")), str(t.get("evidence"))})
    for c in packet.get("contract") or []:
        ids.add(str(c.get("id")))
    for o in packet.get("overrides") or []:
        ids.add(str(o.get("id")))
    ids.discard("None")
    return ids


def _prose_paths(value: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(value, Mapping):
        for k, v in value.items():
            if k in PROSE_KEYS:
                out.append(f"{path}.{k}" if path else str(k))
            out += _prose_paths(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out += _prose_paths(v, f"{path}[{i}]")
    return out


def validate_packet(packet: Mapping[str, Any]) -> list[dict]:
    """The rules a packet breaks: ``[{rule: 1..5, at, what}]``; [] when it is valid."""
    errors: list[dict] = []
    for key in REQUIRED:
        if key not in packet:
            errors.append({"rule": 1, "at": key, "what": "missing"})
    ids = known_ids(packet)
    for kind in ("claims", "risks"):
        for i, item in enumerate(packet.get(kind) or []):
            at = f"{kind}[{i}]"
            text = str((item or {}).get("text") or "")
            refs = list((item or {}).get("refs") or [])
            if not text or len(text) > TEXT_MAX:
                errors.append({"rule": 2, "at": at, "what": f"text over {TEXT_MAX} chars"
                               if text else "no text"})
            if not refs:
                errors.append({"rule": 2, "at": at, "what": "no evidence ref"})
            for r in refs:
                if str(r) not in ids:
                    errors.append({"rule": 2, "at": at, "what": f"unknown ref {r}"})
            extra = set(item or {}) - {"text", "refs"}
            if extra:
                errors.append({"rule": 2, "at": at, "what": f"fields {sorted(extra)}"})
    if "counts" in packet and packet.get("counts") != count(packet.get("findings") or []):
        errors.append({"rule": 3, "at": "counts", "what": "do not match the findings"})
    for f in packet.get("findings") or []:
        if f.get("status") not in STATUSES:
            errors.append({"rule": 1, "at": str(f.get("id")), "what": "bad status"})
        if f.get("status") != "fixed":
            continue
        kind = (f.get("source") or {}).get("kind")
        after = any(isinstance(e, Mapping) and e.get("absent") for e in f.get("evidence") or [])
        passed = any(isinstance(e, Mapping) and e.get("kind") == "test" and e.get("ok")
                     and e.get("sha") == packet.get("head_sha") for e in f.get("evidence") or [])
        if kind == "test" and not passed:
            errors.append({"rule": 4, "at": str(f.get("id")), "what": "fixed without a pass"})
        elif kind != "test" and not (after and f.get("hunk_id")):
            errors.append({"rule": 4, "at": str(f.get("id")),
                           "what": "fixed without after-evidence and a hunk"})
    for p in _prose_paths(packet):
        errors.append({"rule": 5, "at": p, "what": "build prose"})
    return errors


def finalize(packet: dict) -> dict:
    """Counts and verdict computed; the rules checked; a broken packet is ``invalid``."""
    packet["findings"] = sorted(packet.get("findings") or [], key=_sort_key)
    packet["counts"] = count(packet["findings"]) if "counts" not in packet else packet["counts"]
    packet["verdict"] = {"state": "ready", "reasons": []}
    errors = validate_packet(packet)
    packet["verdict"] = ({"state": "invalid",
                          "reasons": [f"rule{e['rule']}:{e['at']}:{e['what']}" for e in errors]}
                         if errors else verdict(packet))
    return packet


# ── building it from step outputs ──────────────────────────────────────────────────────────────

def _ns(prefix: str, value: str) -> str:
    return f"{prefix}{value}" if prefix else value


def from_validation(rec: Mapping[str, Any], prefix: str = "") -> tuple[list[dict], dict, dict]:
    """(findings, validators row, vs_input) from one ``boss.validation/v1`` record."""
    findings: list[dict] = []
    for f in rec.get("findings") or []:
        g = json.loads(json.dumps(f))
        g["id"] = _ns(prefix, str(f["id"]))
        g["evidence"] = [{**e, "id": _ns(prefix, str(e.get("id")))} if isinstance(e, Mapping)
                         else e for e in f.get("evidence") or []]
        findings.append(g)
    after = rec.get("after") or {}
    ruleset = rec.get("ruleset") or None
    row = {"id": (rec.get("validator") or {}).get("id"),
           "ruleset": (f"{ruleset.get('id')} v{ruleset.get('version')}" if ruleset else None),
           "before": len((rec.get("before") or {}).get("findings") or []),
           "after_open": sum(1 for f in findings if f.get("status") == "open"),
           "after_waived": sum(1 for f in findings if f.get("status") == "waived"),
           "exit": after.get("exit") if after else None,
           "evidence": _ns(prefix, "E-3") if after else None,
           "evidence_ids": [_ns(prefix, str(e["id"])) for e in rec.get("evidence") or []],
           "guard": rec.get("guard"), "error": rec.get("error")}
    by_hunk: dict[str, list[str]] = {}
    for x in rec.get("fixes") or []:
        ids = by_hunk.setdefault(str(x.get("hunk_id")), [])
        if x.get("finding") and _ns(prefix, str(x["finding"])) not in ids:
            ids.append(_ns(prefix, str(x["finding"])))
    hunks = rec.get("hunks") or []
    vs = {"file": rec.get("target"), "hunks": len(hunks),
          "added": sum(int(h.get("added") or 0) for h in hunks),
          "removed": sum(int(h.get("removed") or 0) for h in hunks),
          "hunks_by_finding": {h: ids for h, ids in by_hunk.items() if ids},
          "unexplained_hunks": [str(h["id"]) for h in hunks if not by_hunk.get(str(h["id"]))]}
    return findings, row, vs


def from_followups(items: Iterable[Mapping[str, Any]], bar: Optional[str]) -> list[dict]:
    """Review seats' findings R-n. The quality bar's own severity mapping is the ``basis``; an
    item without one is P2 and ``unmapped`` (§1.1), so an unsupported P0 cannot block and an
    unsupported P3 cannot hide."""
    out = []
    for n, it in enumerate(items, start=1):
        text = str(it.get("text") or "")
        m = _LOC.match(text)
        tier, sev = it.get("tier"), it.get("severity")
        mapped = tier in PLEVELS and bool(sev)
        out.append({
            "id": f"R-{n}",
            "source": {"kind": "reviewer", "name": it.get("seat") or "review",
                       "rule_id": None, "rule_doc": None},
            "severity": tier if mapped else "P2",
            "basis": f"quality bar {bar or 'standard'}: {sev}" if mapped else None,
            "unmapped": not mapped, "native_severity": sev or None,
            "title": (text[m.end():] if m else text)[:TEXT_MAX] or None,
            "location": {"file": m.group(1) if m else None,
                         "line": int(m.group(2)) if m and m.group(2) else None,
                         "partner_line": None},
            "original": None, "fixed": None, "hunk_id": None, "fix_kind": "agent",
            "status": "open", "evidence": [], "override": None,
            **({"deferred_to": it["deferred_to"]} if it.get("deferred_to") else {})})
    return out


def from_tests(runs: Iterable[Mapping[str, Any]], head_sha: Optional[str]) -> list[dict]:
    out = []
    for n, t in enumerate(runs, start=1):
        out.append({"id": f"T-{n}", "suite": str(t.get("command") or "")[:200],
                    "passed": t.get("passed"), "failed": t.get("failed"),
                    "exit": t.get("exit_code"), "ok": bool(t.get("ok")),
                    "evidence": f"E-T{n}", "sha": head_sha})
    return out


def _claims(findings: Sequence[Mapping[str, Any]], validators: Sequence[Mapping[str, Any]],
            tests: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The packet's claims, from templates over its own records (never a model's words)."""
    out: list[dict] = []
    for v in validators:
        if v.get("evidence"):
            prefix = str(v["evidence"])[: -len("E-3")]
            fixed = [f["id"] for f in findings if f.get("status") == "fixed"
                     and str(f.get("id")).startswith(prefix + "V-")]
            out.append({"text": f"{v['id']}: {v['before']} found, {len(fixed)} fixed, "
                                f"{v['after_open']} open", "refs": [v["evidence"], *fixed][:20]})
    for p in ("P0", "P1"):
        mine = [f for f in findings if f.get("severity") == p]
        closed = [f["id"] for f in mine if f.get("status") in ("fixed", "waived")]
        if mine and len(closed) == len(mine):
            out.append({"text": f"All {len(mine)} {p} findings closed", "refs": closed})
    ok = [t["id"] for t in tests if t.get("ok")]
    if tests and len(ok) == len(tests):
        out.append({"text": f"{len(ok)} test run(s) pass at the head", "refs": ok})
    return out


def build_packet(*, work: Mapping[str, Any], head_sha: Optional[str],
                 validations: Sequence[Mapping[str, Any]] = (),
                 tests: Sequence[Mapping[str, Any]] = (),
                 contract: Sequence[Mapping[str, Any]] = (),
                 reviews: Sequence[Mapping[str, Any]] = (),
                 overrides: Sequence[Mapping[str, Any]] = (),
                 risks: Sequence[Mapping[str, Any]] = (),
                 packet_version: int = 1, rerun: Optional[Mapping[str, Any]] = None,
                 built_at: Optional[str] = None,
                 apply_overrides: Optional[Callable[[list], list]] = None) -> dict:
    """The packet from step outputs (records only). ``validations`` are boss.validation/v1
    records; ``tests`` the build's test runs; ``reviews`` boss.finding/v1 reviewer findings."""
    findings: list[dict] = []
    validators: list[dict] = []
    vs_input: dict = {"file": None, "hunks": 0, "added": 0, "removed": 0,
                      "hunks_by_finding": {}, "unexplained_hunks": []}
    many = len(validations) > 1
    for rec in validations:
        prefix = f"{(rec.get('validator') or {}).get('id')}:" if many else ""
        fs, row, vs = from_validation(rec, prefix)
        findings += fs
        validators.append(row)
        if vs_input["file"] is None:
            vs_input = vs
    findings += [dict(r) for r in reviews]
    if apply_overrides is not None:          # finding_overrides.apply: the people's overrides
        findings = apply_overrides(findings)
        for v in validators:
            mine = [f for f in findings if (f.get("source") or {}).get("kind") == "validator"]
            v["after_open"] = sum(1 for f in mine if f.get("status") in ("open", "reverted"))
            v["after_waived"] = sum(1 for f in mine if f.get("status") == "waived")
    test_rows = from_tests(tests, head_sha)
    packet = {"schema": SCHEMA, "packet_version": int(packet_version), "head_sha": head_sha,
              "built_at": built_at or _now(), "work": dict(work),
              "findings": findings, "vs_input": vs_input, "validators": validators,
              "tests": test_rows, "contract": [dict(c) for c in contract],
              "overrides": [dict(o) for o in overrides],
              "risks": [dict(r) for r in risks],
              "claims": _claims(findings, validators, test_rows),
              **({"rerun": dict(rerun)} if rerun else {})}
    return finalize(packet)


# ── from the database ──────────────────────────────────────────────────────────────────────────

def _lineage(connection: Any, run_id: str) -> list[dict]:
    """The run's lineage, root first: [{id, title, finalize_ctx, state, created_at}]."""
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT id, finalize_ctx->>'root_run_id' AS root FROM code_task_runs WHERE id = :r"),
        {"r": run_id}).mappings().first()
    if row is None:
        return []
    root = str(row["root"] or row["id"])
    rows = connection.execute(text(
        "SELECT id, title, finalize_ctx, state, created_at FROM code_task_runs "
        "WHERE id = :root OR finalize_ctx->>'root_run_id' = :root ORDER BY (id = :root) DESC, "
        "created_at"), {"root": root}).mappings().all()
    return [{**dict(r), "finalize_ctx": _j(r["finalize_ctx"], {}), "state": _j(r["state"], {})}
            for r in rows]


def work_title(title: Any) -> str:
    """The root build's own name: no "Follow-up: … —" leads, no retry tails."""
    from .pr_follow_up import work_name

    return work_name(title) or str(title or "")


def _run_no(connection: Any, workflow_run_id: Optional[str]) -> Optional[int]:
    if not workflow_run_id:
        return None
    from sqlalchemy import text

    try:
        with connection.begin_nested():
            n = connection.execute(text(
                "SELECT count(*) FROM workflow_runs w, workflow_runs me WHERE me.id = :w "
                "AND w.project_id = me.project_id "
                "AND w.workflow_definition_id = me.workflow_definition_id "
                "AND w.created_at <= me.created_at"), {"w": workflow_run_id}).scalar()
    except Exception:  # noqa: BLE001 — the number is a label
        return None
    return int(n) if n else None


def for_run(connection: Any, run_id: str, *, head_sha: Optional[str],
            change: Optional[int] = None, followups: Optional[Mapping[str, Any]] = None,
            overrides: Sequence[Mapping[str, Any]] = (), packet_version: int = 1,
            rerun: Optional[Mapping[str, Any]] = None,
            validation: Optional[Mapping[str, Any]] = None,
            apply_overrides: Optional[Callable[[list], list]] = None,
            project_id: Optional[str] = None) -> Optional[dict]:
    """The packet for a build's merge card: the work is the lineage's ROOT build (its title
    without follow-up prefixes), the validation is the newest one in the lineage, the tests are
    this build's. None when the run is unknown."""
    lineage = _lineage(connection, run_id)
    if not lineage:
        return None
    root = lineage[0]
    me = next((r for r in lineage if r["id"] == run_id), lineage[-1])
    wfr = (me["finalize_ctx"] or {}).get("workflow_run_id") or \
        (root["finalize_ctx"] or {}).get("workflow_run_id")
    validation = validation or next((r["state"].get("validation") for r in reversed(lineage)
                       if isinstance(r["state"].get("validation"), Mapping)
                       and r["state"]["validation"].get("schema") == "boss.validation/v1"), None)
    work = {"title": work_title(root["title"]), "workflow_run": wfr,
            "run_no": _run_no(connection, wfr), "build": root["id"],
            "later_builds": [r["id"] for r in lineage[1:]], "change": change}
    reviews = from_followups((followups or {}).get("items") or [], (followups or {}).get("bar"))
    # THE QUALITY BAR DECIDES WHAT A REVIEW FINDING BLOCKS (operator 2026-11-18, light lane):
    # what reaches the card as a follow-up already passed the bar — queued, for the next pass, or
    # listed for later — so it is listed, open, and DEFERRED, never a packet block. Only a
    # validator, test or contract finding blocks the packet; a review that blocks at the bar
    # (production) is the checks-failing card's, not this one's.
    fu = followups or {}
    by = fu.get("handled_by") or ("queued" if fu.get("queued") else "later")
    # Where it went: a follow-up build on THIS change's branch (queued, the next pass, the build
    # already on it) lands on the same change; "later" has no change yet.
    on_change = by != "later" and change is not None
    for r in reviews:
        r["deferred_to"] = {"by": by, "number": change if on_change else None,
                            "href": (f"/changes/{project_id}/{change}" if on_change and project_id
                                     else None)}
    return build_packet(work=work, head_sha=head_sha,
                        validations=[validation] if validation else [],
                        tests=me["state"].get("tests") or [], reviews=reviews,
                        overrides=overrides, packet_version=packet_version, rerun=rerun,
                        apply_overrides=apply_overrides)


#: A re-run with no result after this is overdue: a person's forced merge gets past it
#: (finding_overrides fails it with the reason when it next reads it).
RERUN_STALE_AFTER = timedelta(minutes=15)


def _when(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def blocks_merge(rec: Mapping[str, Any], *, force: bool) -> Optional[tuple[str, str]]:
    """(code, message) when the card's packet refuses this merge: an invalid packet or a
    re-run in progress always; a blocked verdict (open P0/P1, a substituted input, a failing
    test…) unless a person forces it with a reason."""
    packet = rec.get(KEY) if isinstance(rec, Mapping) else None
    if not isinstance(packet, Mapping):
        return None
    v = packet.get("verdict") or {}
    reasons = [str(r) for r in v.get("reasons") or []]
    if v.get("state") == "invalid":
        return ("packet_invalid", f"Packet invalid: {len(reasons)} rule(s) failed. Rebuild the "
                                  "packet before merging.")
    if "rerun_pending" in reasons:
        started = _when((packet.get("rerun") or {}).get("created_at"))
        stale = started is not None and datetime.now(timezone.utc) - started > RERUN_STALE_AFTER
        if not (stale and force):
            return ("rerun_pending", "Re-checking after an override; merge when the re-run is "
                                     "done." + (" It is overdue: a person may merge anyway with "
                                                "a stated reason." if stale else ""))
    if v.get("state") == "blocked" and not force:
        return ("packet_blocked", "Blocked: " + ", ".join(reasons[:6])
                + ". A person may merge anyway with a stated reason.")
    return None
