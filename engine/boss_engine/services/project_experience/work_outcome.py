"""WHAT A PIECE OF WORK IS, AND WHAT IT PRODUCED (the Work panel follows the work's kind).

The operator, on an incident investigation drawn as Spec → Build → Review & tests → Merge →
Staging → Production with "Nothing built yet" and "Pick a prod host": "why an investigation needs
code task pipeline?" (2026-11-20). The code-delivery stations fit only work that changes an app.
Every other workflow run is drawn as ITS OWN steps and told by what it produced:

  shape          code (builds, a pull request, a deploy) | investigation | job
  flow           the workflow's own steps — names, agents and status from the run's steps —
                 condensed to about six nodes (control steps and branches not taken left out)
  outcome        investigation: Asked (what triggered it, the data) · Found (the root cause and
                 its evidence) · Remediation (proposed, decided, done) · links
                 job: Asked · Produced (what it made, its counts, links)
                 plus `line`: the one-line outcome the header and the row say.

Read from the run's stored step outputs only: never a model call. Pure functions; work.py feeds
them the rows it already holds.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from .common import aware, face, iso

#: Capabilities that change an app: a workflow with any of them is code-delivery work.
CODE_CAPS = frozenset({"build", "retry_build", "fix_pr", "more_tests", "preview", "deploy",
                       "redeploy", "config_deploy", "promote_config", "wait_for_ci",
                       "release_approval", "smoke_test", "teardown_preview", "security_fix"})
#: Step kinds that route or wait, never work: left out of the drawn flow.
CONTROL_KINDS = frozenset({"condition", "timer", "for_each", "bounded_loop", "join", "parallel"})
#: Output fields that carry a finding (a judgement on a cause or a case).
FINDING_KEYS = ("candidates", "root_cause", "recommendation", "rca_verdict", "assessment")
#: Output fields that carry a proposed remediation.
PROPOSAL_KEYS = ("package", "change_content", "proposal", "remediation", "remediation_package_id")
#: Integer output fields that are bookkeeping, never what the work produced.
_NOT_A_COUNT = ("cost", "spend", "usd", "micros", "token", "model_call", "round", "iteration",
                "attempt", "budget", "_ms", "seconds", "population", "failed", "status")
_DONE = ("succeeded", "skipped")
_LIVE = ("running", "leased", "ready", "waiting")
_FAILED = ("failed", "deadlettered")
TARGET_NODES = 6
#: BOSS's own record ids (kgn_…, wfr_…): never a fact a person reads.
_INTERNAL_ID = re.compile(r"^[a-z]{2,6}_[0-9a-z]{12,}$")


def _cap(spec_step: Mapping[str, Any]) -> str:
    binding = spec_step.get("binding") if isinstance(spec_step.get("binding"), dict) else {}
    return str(binding.get("capability") or spec_step.get("capability") or "")


def delivers_code(spec: Mapping[str, Any], *, has_builds: bool = False, has_pr: bool = False) -> bool:
    """Work that changes an app: it ran a build, opened a change, or its workflow builds or
    deploys. Everything else is drawn as its workflow's own steps."""
    if has_builds or has_pr:
        return True
    return any(_cap(s) in CODE_CAPS for s in spec.get("steps") or [] if isinstance(s, dict))


def humanize(key: str) -> str:
    words = str(key or "").replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:]


def step_name(spec_step: Mapping[str, Any]) -> str:
    return str(spec_step.get("name") or spec_step.get("title") or "").strip() \
        or humanize(str(spec_step.get("id") or _cap(spec_step)))


def _words(out: Mapping[str, Any]) -> str:
    """A step's own sentence: its summary, else its note."""
    for k in ("summary", "note", "statement"):
        v = out.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def clip(text: Any, n: int = 160) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(",;:·—-")
    return cut + "…"


def _seconds(got: Mapping[str, Any], now: datetime) -> float:
    began, ended = aware(got.get("started_at")), aware(got.get("completed_at"))
    if began is None:
        return 0.0
    if ended is None and str(got.get("status") or "") in _LIVE:
        ended = now
    return max(0.0, (ended - began).total_seconds()) if ended else 0.0


def _is_person(spec_step: Mapping[str, Any], got: Mapping[str, Any]) -> bool:
    return str(spec_step.get("kind") or got.get("step_kind") or "") == "decision"


def worked_steps(spec: Mapping[str, Any], steps: Mapping[str, Mapping[str, Any]], *,
                 finished: bool) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The steps that did work, in the workflow's order: no routing/waiting steps, no branch the
    run did not take (skipped), and — once the run ended — no step that never started."""
    out = []
    for spec_step in spec.get("steps") or []:
        if not isinstance(spec_step, dict):
            continue
        if str(spec_step.get("kind") or "") in CONTROL_KINDS:
            continue
        got = steps.get(str(spec_step.get("id") or ""))
        if got is None:
            if finished:
                continue
            got = {"status": "pending", "output": {}}
        if str(got.get("status") or "") == "skipped":
            continue
        out.append((spec_step, dict(got)))
    return out


def _state(statuses: list[str], gate_open: bool) -> str:
    if any(s in _FAILED for s in statuses):
        return "failed"
    if gate_open:
        return "waiting"
    if statuses and all(s in _DONE for s in statuses):
        return "done"
    if any(s in _LIVE for s in statuses) or any(s in _DONE for s in statuses):
        return "now"
    return "todo"


def step_flow(spec: Mapping[str, Any], steps: Mapping[str, Mapping[str, Any]], *,
              roster: Mapping[str, Mapping[str, Any]], now: datetime, finished: bool,
              decisions: Optional[Mapping[str, Mapping[str, Any]]] = None,
              target: int = TARGET_NODES) -> list[dict[str, Any]]:
    """THE WORKFLOW'S OWN STEPS, condensed to about `target` nodes. Neighbours of the same sort
    (agent steps, or a person's decisions) merge, the pair that took the least time first; a
    node is named for the step in it that did the most work. Each node: key, label, state
    (done | now | waiting | todo | failed), agent, you (a person's decision), duration_s,
    started_at, sub (that step's own sentence), steps (every step name in it), and for a
    decision who decided and when."""
    from ..command_center import step_agent

    decisions = decisions or {}
    groups: list[dict[str, Any]] = []
    for spec_step, got in worked_steps(spec, steps, finished=finished):
        person = _is_person(spec_step, got)
        groups.append({"members": [(spec_step, got)], "person": person,
                       "weight": 0.0 if person else _seconds(got, now)})
    while len(groups) > target:
        best = None
        for i in range(len(groups) - 1):
            a, b = groups[i], groups[i + 1]
            if a["person"] != b["person"]:
                continue
            w = a["weight"] + b["weight"]
            if best is None or w < best[0]:
                best = (w, i)
        if best is None:
            break
        i = best[1]
        a, b = groups[i], groups[i + 1]
        groups[i:i + 2] = [{"members": a["members"] + b["members"], "person": a["person"],
                            "weight": a["weight"] + b["weight"]}]
    nodes = []
    for g in groups:
        members = g["members"]
        lead = max(members, key=lambda m: _seconds(m[1], now)) if not g["person"] else members[0]
        lead_spec, lead_got = lead
        agent = None
        if not g["person"]:
            for spec_step, got in [lead] + [m for m in members if m is not lead]:
                who = step_agent(spec_step, {**got, "output": got.get("output") or {}}, {})
                agent = face(roster, who.get("role_key"))
                if agent:
                    break
        starts = [aware(got.get("started_at")) for _s, got in members if aware(got.get("started_at"))]
        total = sum(_seconds(got, now) for _s, got in members)
        node: dict[str, Any] = {
            "key": str(members[0][0].get("id") or ""), "label": step_name(lead_spec),
            "state": _state([str(got.get("status") or "pending") for _s, got in members],
                            any(got.get("gate_open") for _s, got in members)),
            "agent": agent, "you": g["person"],
            "duration_s": int(total) if total >= 1 else None,
            "started_at": iso(min(starts)) if starts else None,
            "sub": clip(_words(lead_got.get("output") or {}), 140),
            "steps": [step_name(s) for s, _g in members],
        }
        if g["person"]:
            outs = [got.get("output") or {} for _s, got in members]
            outcome = next((str(o.get("outcome")) for o in outs if o.get("outcome")), "")
            dec = next((decisions.get(str(got.get("decision_id") or o.get("decision_id") or ""))
                        for (_s, got), o in zip(members, outs)
                        if decisions.get(str(got.get("decision_id") or o.get("decision_id") or ""))),
                       None)
            node["decided"] = outcome or None
            node["decided_by"] = (dec or {}).get("who") or None
            node["decided_at"] = (dec or {}).get("at") or None
        nodes.append(node)
    return nodes


# ── what it produced ──────────────────────────────────────────────────────────────────────────

def _finding(out: Mapping[str, Any]) -> bool:
    return any(out.get(k) not in (None, "", [], {}) for k in FINDING_KEYS)


def _proposal(out: Mapping[str, Any]) -> bool:
    return any(out.get(k) not in (None, "", [], {}) for k in PROPOSAL_KEYS)


def shape_of(kind: str, outputs: Iterable[Mapping[str, Any]]) -> str:
    """investigation: an incident, or a run whose steps judged a cause or a case; else a job."""
    if kind == "incident" or any(_finding(o) for o in outputs):
        return "investigation"
    return "job"


def _confidence(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if 0 <= f <= 1 else None


def _found(name: str, out: Mapping[str, Any]) -> dict[str, Any]:
    """The finding: the top candidate cause (with its evidence and precedents), else the
    recommendation and its rationale, else the step's own sentence."""
    cands = [c for c in out.get("candidates") or [] if isinstance(c, dict) and c.get("cause")]
    cands.sort(key=lambda c: -(_confidence(c.get("confidence")) or 0))
    refs: list[str] = []
    if cands:
        top = cands[0]
        for r in list(top.get("evidence") or []) + list(top.get("precedents") or []):
            if isinstance(r, str) and r and r not in refs:
                refs.append(r)
        headline = str(top["cause"])
        short = humanize(str(top.get("family") or "")) or clip(headline, 80)
        conf = _confidence(top.get("confidence"))
        others = [{"text": clip(c["cause"], 160), "confidence": _confidence(c.get("confidence"))}
                  for c in cands[1:3]]
    else:
        rec, verdict = out.get("recommendation"), out.get("assessment")
        if rec:
            headline = f"Recommends {str(rec).replace('_', ' ')}"
            short = headline[:1].lower() + headline[1:]
        elif verdict:
            headline = _words(out) or f"Assessed as {verdict}"
            short = f"assessed as {str(verdict).replace('_', ' ')}"
        else:
            headline = str(out.get("root_cause") or out.get("rca_verdict") or "") or _words(out)
            short = clip(headline, 80)
        conf = _confidence(out.get("confidence"))
        others = []
        for r in list(out.get("precedents_applied") or []):
            if isinstance(r, str) and r not in refs:
                refs.append(r)
    cited = out.get("cited_fact_ids") or out.get("cited") or []
    return {"step": name, "text": clip(headline, 400), "short": short, "confidence": conf,
            "rationale": clip(out.get("rationale"), 600) or None, "refs": refs[:12],
            "cited": len(cited) if isinstance(cited, list) else 0, "others": others}


def _links(out: Mapping[str, Any], name: str) -> list[dict[str, str]]:
    found = []
    for k, v in out.items():
        if isinstance(v, str) and (k.endswith("_link") or k.endswith("_href") or k == "href") \
                and (v.startswith("/") or v.startswith("http")):
            label = humanize(k.rsplit("_", 1)[0]) if k != "href" else name
            found.append({"label": label if label != "Review" else f"{name}: review", "href": v})
    return found


def _counts(out: Mapping[str, Any]) -> list[dict[str, Any]]:
    got = []
    for k, v in out.items():
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            continue
        if k.endswith("_id") or any(bad in k for bad in _NOT_A_COUNT):
            continue
        got.append({"label": humanize(k), "value": v})
    return got


def outcome(spec: Mapping[str, Any], steps: Mapping[str, Mapping[str, Any]], *, kind: str,
            title: str, directive: Optional[str], run_input: Mapping[str, Any],
            state: str, decisions: Optional[Mapping[str, Mapping[str, Any]]] = None,
            run_href: Optional[str] = None) -> dict[str, Any]:
    """What the run was asked, what it found or produced, and its one-line outcome."""
    decisions = decisions or {}
    worked = worked_steps(spec, steps, finished=True)
    outs = [(step_name(s), s, g, g.get("output") or {}) for s, g in worked]
    shape = shape_of(kind, (o for *_x, o in outs))
    agent_outs = [(n, s, g, o) for n, s, g, o in outs if not _is_person(s, g)]
    first = next(((n, o) for n, _s, g, o in agent_outs
                  if str(g.get("status")) == "succeeded" and _words(o)), None)
    facts = [{"label": humanize(k), "value": str(v)} for k, v in run_input.items()
             if k not in ("title", "directive", "short_title", "work_kind", "repository")
             and isinstance(v, (str, int, float)) and str(v).strip()
             and not _INTERNAL_ID.match(str(v))]
    asked = {"text": (directive or title or "").strip(), "trigger": None, "trigger_step": None,
             "facts": facts[:6]}
    if shape == "investigation" and first:
        asked["trigger"], asked["trigger_step"] = clip(_words(first[1]), 400), first[0]

    links: list[dict[str, str]] = []
    for n, _s, _g, o in outs:
        for link in _links(o, n):
            if link["href"] not in {x["href"] for x in links}:
                links.append(link)

    def decision_entry(n: str, g: Mapping[str, Any], o: Mapping[str, Any]) -> dict[str, Any]:
        dec = decisions.get(str(g.get("decision_id") or o.get("decision_id") or "")) or {}
        said = str(o.get("outcome") or dec.get("status") or "")
        if str(g.get("status")) in _LIVE and g.get("gate_open"):
            said = "waiting"
        return {"step": n, "person": True, "state": said or "waiting",
                "text": f"{n}: {said.replace('_', ' ') or 'waiting on you'}",
                "who": dec.get("who") or None, "at": dec.get("at") or None}

    base = {"shape": shape, "asked": asked, "links": links, "run_href": run_href,
            "found": None, "evidence": [], "remediation": [], "produced": [], "counts": []}
    if shape == "investigation":
        fi = next((i for i, (_n, s, g, o) in enumerate(outs)
                   if not _is_person(s, g) and _finding(o)), None)
        if fi is None:
            fi = next((i for i, (_n, s, g, o) in enumerate(outs)
                       if not _is_person(s, g) and o.get("rationale")), None)
        found = _found(outs[fi][0], outs[fi][3]) if fi is not None else None
        # Remediation starts at the first proposal or person's decision after the finding.
        start = None
        if fi is not None:
            start = next((i for i in range(fi + 1, len(outs))
                          if _is_person(outs[i][1], outs[i][2]) or _proposal(outs[i][3])), None)
        evidence, remediation = [], []
        trig = asked["trigger_step"]
        for i, (n, s, g, o) in enumerate(outs):
            if i == fi or (n == trig and i < (fi or 0)):
                continue
            if start is not None and i >= start:
                if _is_person(s, g):
                    remediation.append(decision_entry(n, g, o))
                elif _words(o) or _proposal(o):
                    pkg = o.get("package") if isinstance(o.get("package"), dict) else {}
                    text_ = pkg.get("fix") or _words(o)
                    remediation.append({
                        "step": n, "person": False, "text": clip(text_, 300),
                        "state": ("proposed" if _proposal(o) else
                                  "failed" if str(g.get("status")) in _FAILED else
                                  "running" if str(g.get("status")) in _LIVE else "done"),
                        "who": None, "at": iso(g.get("completed_at"))})
            elif _words(o):
                evidence.append({"step": n, "text": clip(_words(o), 260)})
        base.update(found=found, evidence=evidence[:6], remediation=remediation)
        decided = [r for r in remediation if r["person"]]
        # "remediation" when a fix was proposed; a run that only asked a person is a decision.
        what = ("remediation" if any(r["state"] == "proposed" for r in remediation) else
                "decision" if len(decided) == 1 else "decisions")
        if any(r["state"] in ("rejected", "declined", "denied") for r in decided):
            rem = f"{what} declined"
        elif any(r["state"] == "waiting" for r in decided):
            rem = f"{what} waits on you"
        elif decided and all(r["state"] == "approved" for r in decided):
            rem = f"{what} approved"
        elif any(r["state"] == "proposed" for r in remediation):
            rem = "remediation proposed"
        else:
            rem = ""
        if found:
            judged = outs[fi][3]
            lead = "Root cause" if judged.get("candidates") or judged.get("root_cause") else "Finding"
            conf = f" ({found['confidence']:.2f})" if found.get("confidence") is not None else ""
            line = " · ".join(x for x in (f"{lead}: {found['short']}{conf}", rem) if x)
        else:
            line = rem or (clip(_words(outs[-1][3]), 120) if outs else "")
        base["line"] = line
    else:
        produced, counts = [], []
        for n, s, g, o in outs:
            if _is_person(s, g):
                base["remediation"].append(decision_entry(n, g, o))
                continue
            words = _words(o)
            if words:
                produced.append({"step": n, "text": clip(words, 300)})
            elif o.get("approved_by") or o.get("outcome"):
                produced.append({"step": n, "text": " ".join(x for x in (
                    humanize(str(o.get("outcome") or "")), f"by {str(o['approved_by']).replace('_', ' ')}"
                    if o.get("approved_by") else "") if x)})
            counts.extend(c for c in _counts(o) if c["label"] not in {x["label"] for x in counts})
        # "Sections 136" beside "Sections read 136" is one number said twice.
        counts = [c for c in counts if not any(
            d is not c and d["value"] == c["value"] and d["label"].startswith(c["label"] + " ")
            for d in counts)]
        base.update(produced=produced, counts=counts[:6])
        lead = next((p["text"] for p in produced if any(ch.isdigit() for ch in p["text"])),
                    produced[0]["text"] if produced else "")
        base["line"] = clip(lead, 140)
    if state not in ("done", "failed"):
        base["line"] = base["line"] if base.get("found") or base.get("produced") else ""
    return base


__all__ = ["CODE_CAPS", "delivers_code", "outcome", "shape_of", "step_flow", "worked_steps"]
