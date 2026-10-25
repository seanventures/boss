"""ONE search over what agents and people WROTE into the graph: rules and learnings, and the runs
and conversations that produced them.

The console's graph search (view.search) and every agent tool (kg_search, kg_precedent, kg_rules,
kg_why, and the "Learned before" pack section) find written items through ``find_written``, so
they cannot disagree. Before it, the console found INC-2041's root cause and the agent tools did
not: they read names and ``search_tsv`` (name + body) only, while INC-2041 sat in the root cause's
evidence attributes and in the brief of the conversation that produced it (an SRE agent answered
"zero hits for INC-2041", then guessed the wrong strategy).

What it matches, best first:

0. an exact id in the query — an incident or ticket key (``INC-2041``, ``BOSSAPP-571``) or a BOSS
   id (``kgn_…``, ``athr_…``, ``crun_…``) — as the node's id, or in its title, body or attributes;
1. the learnings PRODUCED by a run or conversation whose title, body, attributes or brief (the
   person's messages in its thread) carries that id;
2. the query as a phrase (a short query) in a title, body or attributes;
3. the learnings produced by a run or conversation matching the phrase;
4. the query's words (any of them) in a title, body or attributes, by text rank — a symptom
   described in other words than the precedent ("books more than the cash received");
5. the matching runs and conversations themselves, when the caller reads anchors.

Inside a tier an incident-like query (an incident or ticket key, or words like incident, outage,
root cause, symptom) ranks root causes, precedents and investigations first; otherwise learned
items, then rules, then anchors. Provenance (who wrote it, when) never matches. Meaning (vector)
search is not here: callers that have it fuse it with this list.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .view import ANCHOR_KINDS, LEARNED_KINDS

#: Kinds people and agents write (learned items, rules) and the anchors they hang on.
WRITTEN_KINDS = frozenset({"rule", *LEARNED_KINDS, *ANCHOR_KINDS})

#: An incident or ticket key (INC-2041, BOSSAPP-571) or a BOSS id (kgn_…, athr_…, crun_…).
ID_TERM = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z][A-Za-z0-9]{1,15}-\d{2,}|[a-z]{2,8}_[0-9a-f]{8,})(?![A-Za-z0-9_])")
_INCIDENT_WORDS = re.compile(r"\b(incident|outage|root[ -]?cause|post-?mortem|regression|symptom|sev(erity)?\s*\d)\b",
                             re.IGNORECASE)
_TICKET = re.compile(r"\b[A-Za-z][A-Za-z0-9]{1,15}-\d{2,}\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")
#: Words too common in a brief to say what it is about (English function words only).
_COMMON = frozenset("""
this that with from into when then than they them their there these those which what will would
should could have has had been being were also only must make made does done each very just more
most some such your yours ours about over under after before while where here other same like
""".split())
#: A phrase longer than this is a question or a brief, matched by its ids and words, not whole.
PHRASE_MAX = 120

#: What find_written reads, in words an agent can repeat when nothing matched.
FIELDS = ("node id", "title", "body", "attributes (evidence, detail, symptom, scope; not provenance)",
          "the brief of the run or conversation that produced a learning")

_MATCH = ("id", "run", "text", "run", "words", "anchor")


def id_terms(query: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in ID_TERM.finditer(query or "")))


def incident_like(query: str) -> bool:
    return bool(_TICKET.search(query or "") or _INCIDENT_WORDS.search(query or ""))


def or_query(query: str, limit: int = 24) -> Optional[str]:
    """A to_tsquery of the query's words, OR-ed: a long brief ANDed matches nothing."""
    words: list[str] = []
    for word in _WORD.findall(query or ""):
        low = word.lower()
        if low not in _COMMON and low not in words:
            words.append(low)
        if len(words) >= limit:
            break
    return " | ".join(words) if words else None


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _kind_rank(incident: bool) -> dict[str, int]:
    if incident:
        order = ["root_cause", "precedent", "investigation", "fix", "workaround", "test_failure",
                 "review_finding", "decision", "fact"]
        rank = {k: i for i, k in enumerate(order)}
        rank["rule"] = len(order)
    else:
        rank = {k: 0 for k in LEARNED_KINDS}
        rank["rule"] = 1
    for k in ANCHOR_KINDS:
        rank[k] = 20
    return rank


def find_written(connection: Connection, project_id: str, query: str, *, kinds: Iterable[str] = WRITTEN_KINDS,
                 layers: Optional[Iterable[str]] = None, approved_rules_only: bool = False,
                 limit: int = 20) -> list[dict[str, Any]]:
    """Written items matching ``query``, best first: ``[{id, kind, match}]`` where match is
    ``id`` | ``run`` | ``text`` | ``words`` | ``anchor`` (see the module docstring).

    ``kinds`` limits what is returned (a console layer chip, a caller without decision:read);
    ``layers`` limits it to these ``knowledge_layer`` values (a stage's scope);
    ``approved_rules_only`` drops rules no person approved (what a stage may read)."""
    q = (query or "").strip()
    wanted = sorted(set(kinds) & WRITTEN_KINDS)
    if not q or not wanted:
        return []
    ids = id_terms(q)
    phrases = [q] if len(q) <= PHRASE_MAX and q not in ids else []
    words = or_query(q)
    items = [k for k in wanted if k not in ANCHOR_KINDS]
    anchors_back = [k for k in wanted if k in ANCHOR_KINDS]
    params = {
        "p": project_id, "ids": ids, "idp": [_like(i) for i in ids],
        "allp": [_like(t) for t in [*ids, *phrases]], "words": words,
        "items": items, "anchors": list(ANCHOR_KINDS), "anchors_back": anchors_back,
        "layers": sorted(set(layers)) if layers is not None else None,
        "approved": bool(approved_rules_only), "rank": json.dumps(_kind_rank(incident_like(q))),
        "limit": max(1, min(int(limit), 200)),
    }
    rows = connection.execute(text(
        r"""
        WITH anchor AS (
          -- A conversation's brief is the person's FIRST message in its thread: a later question
          -- that names INC-2041 ("which strategy did INC-2041 affect?") does not make what that
          -- conversation produced INC-2041's.
          SELECT r.id, r.kind, r.last_seen,
                 r.name || ' ' || coalesce(r.body, '') || ' ' || coalesce((r.attrs - 'provenance')::text, '') || ' ' ||
                 coalesce((SELECT m.content FROM agent_messages m
                           WHERE r.attrs->>'thread_id' IS NOT NULL AND m.thread_id = r.attrs->>'thread_id'
                             AND m.role = 'user' ORDER BY m.created_at, m.id LIMIT 1), '') AS doc
          FROM kg_nodes r
          WHERE r.project_id = :p AND r.valid_to IS NULL AND r.kind = ANY(CAST(:anchors AS text[]))
        ), runs AS (
          SELECT a.id, a.kind, a.last_seen,
                 CASE WHEN a.id = ANY(CAST(:ids AS text[])) OR a.doc ILIKE ANY(CAST(:idp AS text[])) THEN 1 ELSE 3 END AS via
          FROM anchor a
          WHERE a.id = ANY(CAST(:ids AS text[])) OR a.doc ILIKE ANY(CAST(:allp AS text[]))
        ), cand AS (
          SELECT n.id, n.kind, n.last_seen, n.name, n.body, n.attrs, n.knowledge_layer, n.trust_tier
          FROM kg_nodes n
          WHERE n.project_id = :p AND n.valid_to IS NULL AND n.kind = ANY(CAST(:items AS text[]))
            AND (CAST(:layers AS text[]) IS NULL OR n.knowledge_layer = ANY(CAST(:layers AS text[])))
            AND (NOT :approved OR n.kind <> 'rule' OR n.trust_tier = 'approved')
        ), direct AS (
          SELECT c.id, c.kind, c.last_seen,
                 CASE WHEN c.id = ANY(CAST(:ids AS text[])) OR c.name ILIKE ANY(CAST(:idp AS text[])) OR c.body ILIKE ANY(CAST(:idp AS text[]))
                           OR (c.attrs - 'provenance')::text ILIKE ANY(CAST(:idp AS text[])) THEN 0 ELSE 2 END AS via,
                 0.0::float8 AS rank
          FROM cand c
          WHERE c.id = ANY(CAST(:ids AS text[])) OR c.name ILIKE ANY(CAST(:allp AS text[])) OR c.body ILIKE ANY(CAST(:allp AS text[]))
                OR (c.attrs - 'provenance')::text ILIKE ANY(CAST(:allp AS text[]))
        ), produced AS (
          SELECT c.id, c.kind, c.last_seen, min(runs.via) AS via, 0.0::float8 AS rank
          FROM kg_edges e JOIN runs ON runs.id = e.src_id JOIN cand c ON c.id = e.dst_id
          WHERE e.project_id = :p AND e.valid_to IS NULL AND e.kind = 'produced'
          GROUP BY c.id, c.kind, c.last_seen
        ), worded AS (
          SELECT c.id, c.kind, c.last_seen, 4 AS via,
                 ts_rank_cd(d.doc, to_tsquery('english', CAST(:words AS text)))::float8 AS rank
          FROM cand c,
               LATERAL (SELECT to_tsvector('english', regexp_replace(
                   c.name || ' ' || coalesce(c.body, '') || ' ' || coalesce((c.attrs - 'provenance')::text, ''),
                   '[^A-Za-z0-9]+', ' ', 'g')) AS doc) d
          WHERE CAST(:words AS text) IS NOT NULL AND d.doc @@ to_tsquery('english', CAST(:words AS text))
        ), anchored AS (
          SELECT runs.id, runs.kind, runs.last_seen, 5 AS via, 0.0::float8 AS rank
          FROM runs WHERE runs.kind = ANY(CAST(:anchors_back AS text[]))
        ), u AS (
          SELECT * FROM direct UNION ALL SELECT * FROM produced UNION ALL SELECT * FROM worded
          UNION ALL SELECT * FROM anchored
        ), best AS (
          SELECT DISTINCT ON (id) id, kind, last_seen, via, rank FROM u ORDER BY id, via, rank DESC
        )
        SELECT id, kind, via FROM best
        ORDER BY via, CASE WHEN via = 4 THEN -rank ELSE 0 END,
                 coalesce((CAST(:rank AS jsonb)->>kind)::int, 9), last_seen DESC NULLS LAST, id
        LIMIT :limit
        """), params).all()
    return [{"id": r[0], "kind": r[1], "match": _MATCH[int(r[2])]} for r in rows]


def pinned(found: Sequence[dict]) -> list[str]:
    """The ids an exact id led to (the node itself, or what its run produced): these lead any
    ranking, since the caller named them."""
    return [f["id"] for f in found if f["match"] in ("id", "run")]


def searched(*, layers: Iterable[str], meaning: Optional[bool], query: str, tool: str) -> dict[str, Any]:
    """What a tool read when it found nothing, in words an agent can repeat — so "zero hits" is a
    statement about these layers and fields, never about the whole graph."""
    ids = id_terms(query)
    how = ("exact ids " + ", ".join(ids) + "; " if ids else "") + "the words of the query"
    return {
        "layers": sorted(set(layers)),
        "fields": list(FIELDS),
        "meaning": ("on" if meaning else "off") if meaning is not None else "not used",
        "note": (f"{tool} found nothing for {query!r}. It searched {how} in the "
                 f"{', '.join(sorted(set(layers)))} layers: {'; '.join(FIELDS)}. Nothing here does not prove "
                 "the graph lacks it: say what was searched, then try other words or ids, kg_search, "
                 "kg_schema or kg_cypher before concluding."),
    }
