"""The Archivist's facts: write, resolve, supersede, promote, forget, retain (spec §3.4, §3.8; Phase 2).

**Trust tiers come from the origin, never from the payload.** A write names its `Origin`, which only
BOSS code builds. The tier follows from the origin's kind:

| Origin kind | Tier | Who writes it |
|---|---|---|
| `deterministic` | observed | the code graph, CI and test results (no model) |
| `decision` | governed | a person's recorded decision or sign-off, copied verbatim (no model) |
| `distilled` | inferred | the Archivist's extraction call |
| `agent` | inferred | any agent, through `kg_propose_fact` |

`approved` is reached only by `promote()`, which needs a person. Any `tier`, `trust` or similar
key in a candidate is ignored: only the known fields are read.

**Rank:** governed > approved > observed > inferred. A fact never outranks one of a higher rank:
- a lower-ranked fact that contradicts a higher-ranked one is recorded, but closed at once and
  linked to the fact that refused it;
- a higher- or equal-ranked contradiction closes the older fact (`valid_to`) and links
  `superseded_by`.

Nothing is deleted except by retention or `forget_source`.

**Resolve** (in order):
1. The subject is matched to a code or lifecycle node by exact key or qualified name.
2. A fact whose normalised statement is already current merges into it, adding the new source.
3. The current facts about the same subject (and the nearest by embedding, when there are
   embeddings) go to a model judgement: same, contradicts, or unrelated. Without a model (in
   structure-only mode) only exact duplicates merge.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .store import Scope

KINDS = ("invariant", "decision", "lesson", "pattern", "coupling", "ownership", "risk")
TIER_OF_ORIGIN = {"deterministic": "observed", "decision": "governed", "distilled": "inferred", "agent": "inferred"}
RANK = {"inferred": 0, "observed": 1, "approved": 2, "governed": 3}
FIELDS = ("subject", "predicate", "object", "statement", "kind", "confidence", "valid_from", "evidence")

#: judge(new_fact, candidates) -> {candidate_id: "same" | "contradicts" | "unrelated"}
Judge = Callable[[dict, list[dict]], dict[str, str]]


@dataclass(frozen=True)
class Origin:
    """Where a fact came from. Built by BOSS code only; it decides the tier."""

    kind: str                   # deterministic | decision | distilled | agent
    source_kind: str            # run | incident | review | decision | ci | agent
    source_id: str
    actor: str = "archivist"

    def __post_init__(self) -> None:
        if self.kind not in TIER_OF_ORIGIN:
            raise ValueError(f"unknown origin kind {self.kind!r}")

    @property
    def tier(self) -> str:
        return TIER_OF_ORIGIN[self.kind]


class InvalidFact(ValueError):
    pass


def normalise(statement: str) -> str:
    return re.sub(r"\s+", " ", statement.strip().lower()).rstrip(".")


def content_hash(subject: str, statement: str) -> str:
    return hashlib.sha256(f"{normalise(subject)}\n{normalise(statement)}".encode()).hexdigest()


def clean(candidate: dict) -> dict:
    """Only the known fields, validated. Anything else in the payload (a claimed tier) is dropped."""
    fact = {k: candidate.get(k) for k in FIELDS}
    for k in ("subject", "predicate", "statement"):
        if not isinstance(fact[k], str) or not fact[k].strip():
            raise InvalidFact(f"a fact needs a non-empty {k}")
        fact[k] = fact[k].strip()[:2000]
    fact["object"] = str(fact["object"] or "").strip()[:2000]
    if fact["kind"] not in KINDS:
        raise InvalidFact(f"kind must be one of {', '.join(KINDS)}")
    try:
        fact["confidence"] = min(1.0, max(0.0, float(fact["confidence"] if fact["confidence"] is not None else 0.5)))
    except (TypeError, ValueError):
        fact["confidence"] = 0.5
    vf = fact["valid_from"]
    try:
        fact["valid_from"] = datetime.fromisoformat(str(vf).replace("Z", "+00:00")) if vf else None
    except ValueError:
        fact["valid_from"] = None
    ev = fact["evidence"]
    fact["evidence"] = [str(e)[:500] for e in ev][:10] if isinstance(ev, list) else ([str(ev)[:500]] if ev else [])
    return fact


class FactStore:
    def __init__(self, judge: Optional[Judge] = None, neighbours: Optional[Callable[..., list[str]]] = None):
        self.judge = judge
        self.neighbours = neighbours        # (connection, scope, fact) -> fact ids near it by embedding

    # -- write -----------------------------------------------------------------------------------
    def write(self, c: Connection, scope: Scope, candidate: dict, origin: Origin) -> dict:
        """Store one fact; returns {"id", "action": created|merged|superseded|refused, ...}."""
        fact = clean(candidate)
        tier = origin.tier
        h = content_hash(fact["subject"], fact["statement"])
        node_id = self._resolve_subject(c, scope, fact["subject"])
        same = c.execute(text("SELECT id, tier FROM kg_facts WHERE project_id = :p AND content_hash = :h "
                              "AND valid_to IS NULL"), {"p": scope.project_id, "h": h}).mappings().first()
        if same:
            self._merge(c, scope, same["id"], fact, origin)
            if RANK[tier] > RANK[same["tier"]]:          # a person's decision restating it: raise, never lower
                c.execute(text("UPDATE kg_facts SET tier = :t WHERE id = :i"), {"t": tier, "i": same["id"]})
                self._event(c, scope, same["id"], "tier_raised", origin.actor, {"from": same["tier"], "to": tier})
                return {"id": same["id"], "action": "merged", "tier": tier}
            return {"id": same["id"], "action": "merged", "tier": same["tier"]}
        fid = "fact_" + uuid.uuid4().hex[:20]
        c.execute(text(
            """INSERT INTO kg_facts (id, project_id, subject, subject_node_id, predicate, object, statement, kind,
                                     tier, confidence, content_hash, valid_from)
               VALUES (:id, :p, :s, :n, :pr, :o, :st, :k, :t, :cf, :h, COALESCE(:vf, now()))"""),
            {"id": fid, "p": scope.project_id, "s": fact["subject"], "n": node_id, "pr": fact["predicate"],
             "o": fact["object"], "st": fact["statement"], "k": fact["kind"], "t": tier,
             "cf": fact["confidence"], "h": h, "vf": fact["valid_from"]})
        self._source(c, scope, fid, origin, fact["evidence"])
        self._event(c, scope, fid, "created", origin.actor, {"tier": tier, "origin": origin.kind,
                                                             "source": f"{origin.source_kind}:{origin.source_id}"})
        result = {"id": fid, "action": "created", "tier": tier}
        verdicts = self._judge(c, scope, fid, fact, node_id)
        for other_id, verdict in verdicts.items():
            other = c.execute(text("SELECT id, tier FROM kg_facts WHERE id = :i AND project_id = :p "
                                   "AND valid_to IS NULL"), {"i": other_id, "p": scope.project_id}).mappings().first()
            if other is None:
                continue
            if verdict == "same" and RANK[other["tier"]] >= RANK[tier]:
                # A paraphrase of a fact at least as trusted: fold into it.
                self._close(c, scope, fid, other["id"], "merged_into", origin.actor)
                self._merge(c, scope, other["id"], fact, origin)
                return {"id": other["id"], "action": "merged", "tier": other["tier"]}
            if verdict in ("same", "contradicts"):
                if RANK[tier] >= RANK[other["tier"]]:
                    self._close(c, scope, other["id"], fid, "superseded", origin.actor)
                    result = {**result, "action": "superseded", "supersedes": other["id"]}
                else:
                    self._close(c, scope, fid, other["id"], "refused_by_higher_tier", origin.actor)
                    return {"id": fid, "action": "refused", "tier": tier, "kept": other["id"]}
        return result

    def _resolve_subject(self, c, scope, subject: str) -> Optional[str]:
        rows = c.execute(text(
            "SELECT id FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL AND "
            "(node_key = :s OR (kind = 'symbol' AND (name = :s OR node_key LIKE :tail))) LIMIT 2"),
            {"p": scope.project_id, "s": subject, "tail": "%::" + subject.replace("%", "").replace("_", r"\_")}).all()
        return rows[0][0] if len(rows) == 1 else None

    def _judge(self, c, scope, fid: str, fact: dict, node_id: Optional[str]) -> dict[str, str]:
        if self.judge is None:
            return {}
        rows = c.execute(text(
            "SELECT id, subject, predicate, object, statement, kind, tier FROM kg_facts WHERE project_id = :p "
            "AND valid_to IS NULL AND id <> :f AND (lower(subject) = lower(:s) OR (CAST(:n AS TEXT) IS NOT NULL "
            "AND subject_node_id = :n)) ORDER BY created_at DESC LIMIT 8"),
            {"p": scope.project_id, "f": fid, "s": fact["subject"], "n": node_id}).mappings().all()
        cands = {r["id"]: dict(r) for r in rows}
        if self.neighbours is not None:
            extra = [i for i in self.neighbours(c, scope, {**fact, "id": fid}) if i not in cands and i != fid]
            if extra:
                for r in c.execute(text(
                        "SELECT id, subject, predicate, object, statement, kind, tier FROM kg_facts "
                        "WHERE project_id = :p AND valid_to IS NULL AND id = ANY(:ids)"),
                        {"p": scope.project_id, "ids": extra[:5]}).mappings():
                    cands[r["id"]] = dict(r)
        if not cands:
            return {}
        verdicts = self.judge({k: fact[k] for k in ("subject", "predicate", "object", "statement", "kind")},
                              list(cands.values())) or {}
        return {k: v for k, v in verdicts.items() if k in cands and v in ("same", "contradicts", "unrelated")}

    def _merge(self, c, scope, fid: str, fact: dict, origin: Origin) -> None:
        c.execute(text("UPDATE kg_facts SET confidence = GREATEST(confidence, :cf), updated_at = now() WHERE id = :i"),
                  {"cf": fact["confidence"], "i": fid})
        self._source(c, scope, fid, origin, fact["evidence"])
        self._event(c, scope, fid, "merged", origin.actor, {"source": f"{origin.source_kind}:{origin.source_id}"})

    def _close(self, c, scope, fid: str, by: str, action: str, actor: str) -> None:
        c.execute(text("UPDATE kg_facts SET valid_to = now(), superseded_by = :b, updated_at = now() "
                       "WHERE id = :i AND valid_to IS NULL"), {"i": fid, "b": by})
        self._event(c, scope, fid, action, actor, {"by": by})

    @staticmethod
    def _source(c, scope, fid: str, origin: Origin, evidence: list[str]) -> None:
        c.execute(text(
            "INSERT INTO kg_fact_sources (fact_id, project_id, source_kind, source_id, evidence) "
            "VALUES (:f, :p, :k, :s, :e) ON CONFLICT DO NOTHING"),
            {"f": fid, "p": scope.project_id, "k": origin.source_kind, "s": origin.source_id,
             "e": "; ".join(evidence)[:2000]})
        # A FACT CARRIES THE SCOPE OF ITS MOST RESTRICTIVE SOURCE (spec §3.5): a reader needs every
        # permission any of its sources needs (retrieval.SOURCE_PERMISSION).
        from .retrieval import SOURCE_PERMISSION

        need = SOURCE_PERMISSION.get(origin.source_kind, "repository:read")
        c.execute(text("UPDATE kg_facts SET read_permissions = ARRAY(SELECT DISTINCT unnest(read_permissions || "
                       "CAST(:n AS TEXT[])) ORDER BY 1) WHERE id = :f AND NOT (:need = ANY(read_permissions))"),
                  {"f": fid, "n": [need], "need": need})

    @staticmethod
    def _event(c, scope, fid: Optional[str], action: str, actor: str, detail: dict) -> None:
        c.execute(text("INSERT INTO kg_fact_events (project_id, fact_id, action, actor, detail) "
                       "VALUES (:p, :f, :a, :ac, CAST(:d AS JSONB))"),
                  {"p": scope.project_id, "f": fid, "a": action, "ac": actor, "d": json.dumps(detail, default=str)})

    # -- read ------------------------------------------------------------------------------------
    def current(self, c: Connection, scope: Scope, *, subject: Optional[str] = None, kind: Optional[str] = None,
                include_inferred: bool = True, limit: int = 50) -> list[dict]:
        """Current facts, highest rank first, so an inferred fact never outranks a governed one."""
        rows = c.execute(text(
            "SELECT id, subject, predicate, object, statement, kind, tier, confidence, valid_from, uses "
            "FROM kg_facts WHERE project_id = :p AND valid_to IS NULL "
            "AND (CAST(:s AS TEXT) IS NULL OR lower(subject) = lower(:s)) AND (CAST(:k AS TEXT) IS NULL OR kind = :k) "
            "AND (:inf OR tier <> 'inferred') "
            "ORDER BY CASE tier WHEN 'governed' THEN 3 WHEN 'approved' THEN 2 WHEN 'observed' THEN 1 ELSE 0 END DESC, "
            "confidence DESC, valid_from DESC LIMIT :l"),
            {"p": scope.project_id, "s": subject, "k": kind, "inf": include_inferred, "l": limit}).mappings().all()
        return [dict(r) for r in rows]

    # -- promotion (a person only) -----------------------------------------------------------------
    def promote(self, c: Connection, scope: Scope, fact_id: str, *, person: str, actor_kind: str) -> dict:
        """An inferred fact a person confirmed becomes `approved`. Agents and services cannot promote."""
        if actor_kind != "human":
            raise PermissionError("only a person can approve a fact")
        n = c.execute(text("UPDATE kg_facts SET tier = 'approved', updated_at = now() WHERE id = :i AND project_id = :p "
                           "AND tier = 'inferred' AND valid_to IS NULL"), {"i": fact_id, "p": scope.project_id}).rowcount
        if n:
            self._event(c, scope, fact_id, "approved", person, {})
        return {"id": fact_id, "approved": bool(n)}

    def reject(self, c: Connection, scope: Scope, fact_id: str, *, person: str, actor_kind: str) -> dict:
        """A person rejects a current fact: it is closed (kept for audit, never retrieved again)."""
        if actor_kind != "human":
            raise PermissionError("only a person can reject a fact")
        n = c.execute(text("UPDATE kg_facts SET valid_to = now(), updated_at = now() WHERE id = :i AND project_id = :p "
                           "AND valid_to IS NULL AND tier IN ('inferred', 'observed')"),
                      {"i": fact_id, "p": scope.project_id}).rowcount
        if n:
            self._event(c, scope, fact_id, "rejected", person, {})
        return {"id": fact_id, "rejected": bool(n)}

    def forget_fact(self, c: Connection, scope: Scope, fact_id: str, *, actor: str) -> dict:
        """Delete one fact, its sources and its embeddings. Logged, without the content."""
        exists = c.execute(text("SELECT 1 FROM kg_facts WHERE id = :i AND project_id = :p"),
                           {"i": fact_id, "p": scope.project_id}).first()
        if exists:
            self._delete(c, scope, [fact_id])
            self._event(c, scope, None, "forgot_fact", actor, {"fact": fact_id})
        return {"id": fact_id, "forgotten": bool(exists)}

    # -- forget and retention -------------------------------------------------------------------
    def forget_source(self, c: Connection, scope: Scope, source_kind: str, source_id: str, *, actor: str) -> dict:
        """Right to forget: every fact derived only from this source is deleted with its embeddings;
        a fact other sources also support loses this source and stays. Logged, without the content."""
        fids = [r[0] for r in c.execute(text(
            "SELECT fact_id FROM kg_fact_sources WHERE project_id = :p AND source_kind = :k AND source_id = :s"),
            {"p": scope.project_id, "k": source_kind, "s": source_id})]
        c.execute(text("DELETE FROM kg_fact_sources WHERE project_id = :p AND source_kind = :k AND source_id = :s"),
                  {"p": scope.project_id, "k": source_kind, "s": source_id})
        orphans = [r[0] for r in c.execute(text(
            "SELECT f.id FROM kg_facts f WHERE f.id = ANY(:ids) AND NOT EXISTS "
            "(SELECT 1 FROM kg_fact_sources s WHERE s.fact_id = f.id)"), {"ids": fids})]
        self._delete(c, scope, orphans)
        c.execute(text("DELETE FROM kg_ingest_log WHERE project_id = :p AND source_kind = :k AND source_id = :s"),
                  {"p": scope.project_id, "k": source_kind, "s": source_id})
        self._event(c, scope, None, "forgot_source", actor,
                    {"source": f"{source_kind}:{source_id}", "deleted": len(orphans), "kept": len(fids) - len(orphans)})
        return {"deleted": len(orphans), "kept": len(fids) - len(orphans)}

    def apply_retention(self, c: Connection, scope: Scope, *, now: Optional[datetime] = None) -> int:
        """Facts closed (superseded) longer ago than the org's retention are deleted."""
        days = c.execute(text(
            "SELECT COALESCE(s.retention_days, 365) FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            "LEFT JOIN kg_knowledge_settings s ON s.org_id = w.org_id WHERE p.id = :p"),
            {"p": scope.project_id}).scalar() or 365
        now = now or datetime.now(timezone.utc)
        old = [r[0] for r in c.execute(text(
            "SELECT id FROM kg_facts WHERE project_id = :p AND valid_to IS NOT NULL "
            "AND valid_to < CAST(:now AS TIMESTAMPTZ) - make_interval(days => :d)"),
            {"p": scope.project_id, "now": now, "d": int(days)})]
        self._delete(c, scope, old)
        if old:
            self._event(c, scope, None, "retention_purge", "archivist", {"deleted": len(old), "days": int(days)})
        return len(old)

    def _delete(self, c, scope, ids: list[str]) -> None:
        if not ids:
            return
        if c.execute(text("SELECT to_regclass('kg_embeddings')")).scalar():
            c.execute(text("DELETE FROM kg_embeddings WHERE project_id = :p AND owner_kind = 'fact' "
                           "AND owner_id = ANY(:ids)"), {"p": scope.project_id, "ids": ids})
        c.execute(text("UPDATE kg_facts SET superseded_by = NULL WHERE superseded_by = ANY(:ids)"), {"ids": ids})
        c.execute(text("DELETE FROM kg_facts WHERE project_id = :p AND id = ANY(:ids)"),
                  {"p": scope.project_id, "ids": ids})
