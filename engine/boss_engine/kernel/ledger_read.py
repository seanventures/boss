"""THE RUN LEDGER, READ SIDE: the one reader (design §3.5).

Services and tools ask these functions, never raw SQL, so "what did this run try and what did
it cost" has one answer wherever it is asked. Every query is filtered by the caller's project.

  spend(c, scope)            tokens, cached tokens and money of a run, an act, a lineage or a stage
  tried(c, scope)            what was attempted, deduplicated by (tool, arguments), newest first
  open_findings(c, scope)    findings no later event marked fixed
  timeline(c, scope)         the events in order, a page at a time
  compare(c, a, b, ...)      two policy versions side by side on one kind of event
  answer(c, scope, question) the read_run_ledger tool's reply: plain text, at most ~2k tokens
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

from sqlalchemy import text

from .types import Finding, Usage

ScopeKind = Literal["run", "act", "lineage", "stage"]

#: About 2k tokens of reply (chars/4).
ANSWER_CHARS = 8_000


@dataclass(frozen=True)
class Scope:
    """Which slice of the ledger: one run, one act, a whole retry lineage, or one stage of a
    run. `project_id` is the caller's; a scope never reaches another project's rows."""

    kind: ScopeKind
    id: str
    project_id: str
    stage: Optional[str] = None

    def where(self) -> tuple:
        column = {"run": "run_id", "act": "act_id", "lineage": "lineage_id",
                  "stage": "run_id"}[self.kind]
        clause = f"project_id = :p AND {column} = :scope_id"
        params = {"p": self.project_id, "scope_id": self.id}
        if self.kind == "stage" or self.stage:
            clause += " AND stage = :stage"
            params["stage"] = self.stage or ""
        return clause, params


@dataclass(frozen=True)
class Attempt:
    kind: str
    what: str
    args_sha256: Optional[str]
    result: str
    times: int
    last_seq: int
    run_id: str
    cost_usd_micros: int = 0


@dataclass(frozen=True)
class PolicyDelta:
    kind: str
    a: dict
    b: dict


def spend(c: Any, scope: Scope) -> Usage:
    where, params = scope.where()
    row = c.execute(text(f"""
        SELECT COALESCE(sum(input_tokens), 0), COALESCE(sum(output_tokens), 0),
               COALESCE(sum(cache_read_tokens), 0), COALESCE(sum(cache_write_tokens), 0),
               COALESCE(sum(cost_usd_micros) FILTER (WHERE NOT included_in_subscription), 0),
               max(model)
          FROM run_ledger WHERE {where} AND src IN ('model', 'event')
               AND kind NOT IN ('delegate.turn')
    """), params).first()
    return Usage(model=str(row[5] or ""), input_tokens=int(row[0]), output_tokens=int(row[1]),
                 cache_read_tokens=int(row[2]), cache_write_tokens=int(row[3]),
                 cost_usd_micros=int(row[4]))


_TRIED_KINDS = ("tool.result", "verify.result", "finding", "escalation")


def tried(c: Any, scope: Scope, *, kinds: tuple = _TRIED_KINDS, limit: int = 40) -> list:
    """Attempts deduplicated by (kind, tool, args digest): the same call made five times is one
    line saying five times, with its latest result."""
    where, params = scope.where()
    rows = c.execute(text(f"""
        SELECT kind, data, seq, run_id, cost_usd_micros
          FROM run_ledger WHERE {where} AND kind = ANY(:kinds)
         ORDER BY recorded_at DESC, seq DESC LIMIT 2000
    """), {**params, "kinds": list(kinds)}).all()
    seen: dict = {}
    order: list = []
    for kind, data, seq, run_id, cost in rows:
        data = data if isinstance(data, dict) else {}
        what = str(data.get("tool") or data.get("command") or data.get("message")
                   or data.get("question") or kind)[:160]
        digest = data.get("args_sha256")
        key = (kind, what, digest)
        if key in seen:
            seen[key]["times"] += 1
            continue
        result = str(data.get("outcome") or data.get("status") or data.get("result")
                     or ("passed" if data.get("passed") else "")
                     or data.get("severity") or "")[:120]
        seen[key] = {"kind": kind, "what": what, "args_sha256": digest, "result": result,
                     "times": 1, "last_seq": int(seq), "run_id": str(run_id),
                     "cost_usd_micros": int(cost or 0)}
        order.append(key)
        if len(order) >= limit:
            break
    return [Attempt(**seen[k]) for k in order]


def open_findings(c: Any, scope: Scope) -> list:
    where, params = scope.where()
    rows = c.execute(text(f"""
        SELECT data FROM run_ledger WHERE {where} AND kind = 'finding'
         ORDER BY seq
    """), params).all()
    out: list = []
    for (data,) in rows:
        data = data if isinstance(data, dict) else {}
        if data.get("fixed_in_seq"):
            continue
        out.append(Finding(source=data.get("source") or "tool",
                           severity=data.get("severity") or "info",
                           message=str(data.get("message") or ""), path=data.get("path"),
                           line=data.get("line"), quote=str(data.get("quote") or ""),
                           test_id=data.get("test_id"), assertion=data.get("assertion")))
    return out


def timeline(c: Any, scope: Scope, *, after_seq: int = 0, page: int = 200) -> list:
    where, params = scope.where()
    rows = c.execute(text(f"""
        SELECT src, kind, seq, step, stage, data, cost_usd_micros, input_tokens, output_tokens,
               cache_read_tokens, model, recorded_at, run_id, content_ref
          FROM run_ledger WHERE {where} AND seq > :after
         ORDER BY seq LIMIT :page
    """), {**params, "after": int(after_seq or 0), "page": max(1, min(int(page), 500))}
    ).mappings().all()
    return [dict(r) for r in rows]


def compare(c: Any, policy_a: str, policy_b: str, *, kind: str, since: datetime,
            project_id: str) -> PolicyDelta:
    """Events of one kind under two policy versions: how many, what they cost, how they ended.
    `policy_a` / `policy_b` are values such as "stop@1"; they match any policy slot."""
    def side(policy: str) -> dict:
        row = c.execute(text("""
            SELECT count(*), COALESCE(sum(cost_usd_micros), 0), COALESCE(avg(latency_ms), 0),
                   count(*) FILTER (WHERE data->>'result' IN ('pass', 'merged'))
              FROM run_ledger WHERE project_id = :p AND kind = :kind AND recorded_at >= :since
               AND EXISTS (SELECT 1 FROM jsonb_each_text(policy) v WHERE v.value = :policy)
        """), {"p": project_id, "kind": kind, "since": since, "policy": policy}).first()
        return {"events": int(row[0]), "cost_usd_micros": int(row[1]),
                "avg_latency_ms": float(row[2]), "passed": int(row[3])}

    return PolicyDelta(kind=kind, a={"policy": policy_a, **side(policy_a)},
                       b={"policy": policy_b, **side(policy_b)})


def _money(micros: int) -> str:
    return f"${micros / 1_000_000:.2f}"


def render_spend(usage: Usage) -> str:
    seen = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    cached = (100 * usage.cache_read_tokens // seen) if seen else 0
    return (f"spent {_money(usage.cost_usd_micros)}: {usage.input_tokens} new input tokens, "
            f"{usage.output_tokens} output, {cached}% of input cached")


def render_tried(attempts: list) -> str:
    if not attempts:
        return "nothing recorded as tried yet"
    lines = []
    for a in attempts:
        times = f" ×{a.times}" if a.times > 1 else ""
        lines.append(f"- {a.kind} {a.what}{times}: {a.result or 'no result recorded'}")
    return "\n".join(lines)


def answer(c: Any, scope: Scope, question: str, *, after_seq: int = 0) -> str:
    """read_run_ledger's reply, within ANSWER_CHARS."""
    if question == "spend":
        body = render_spend(spend(c, scope))
    elif question == "tried":
        body = render_tried(tried(c, scope)) + "\n" + render_spend(spend(c, scope))
    elif question == "open_findings":
        found = open_findings(c, scope)
        body = "\n".join(f"- [{f.severity}] {f.source}: {f.message}"
                         + (f" ({f.path}:{f.line})" if f.path else "") for f in found) \
            or "no open findings"
    elif question == "timeline":
        rows = timeline(c, scope, after_seq=after_seq, page=80)
        body = "\n".join(f"{r['seq']} step {r['step']} {r['kind']}"
                         + (f" {_money(int(r['cost_usd_micros']))}" if r["cost_usd_micros"] else "")
                         for r in rows) or "no events after that point"
    else:
        raise ValueError(f"unknown question {question!r}")
    if len(body) > ANSWER_CHARS:
        body = body[: ANSWER_CHARS - 60] + "\n…[cut; ask timeline with after_seq for the rest]"
    return body


__all__ = ["ANSWER_CHARS", "Attempt", "PolicyDelta", "Scope", "answer", "compare",
           "open_findings", "render_spend", "render_tried", "spend", "timeline", "tried"]
