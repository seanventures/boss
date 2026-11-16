"""THE ACCEPTANCE CONTRACT (BOSSAPP-1214, epic BOSSAPP-1213 AA-A).

What was asked, frozen per build lineage, word for word: the ticket's acceptance criteria, the
brief's must-haves and the PRD's tested-and-gating list. Version 1 is written the first time the
lineage is checked and never rewritten; a changed ticket is a new version. Every check of the
lineage — the build, its repairs, its follow-ups — is held to it, never to its own short spec
(Northgate's follow-up "met" two MINOR findings while fifteen must-haves stayed missing).

The item extraction moved here from acceptance_gate.py; the gate's own tests
(test_acceptance_gate_holds_the_build_to_its_must_haves.py) prove its behaviour did not change.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from boss_engine.services import acceptance_gate as ag
from boss_engine.services.acceptance import contract as ac

from ._acceptance_fixtures import load_northgate_v1
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

PROSE = Path(__file__).resolve().parent / "fixtures" / "acceptance_prose"


@pytest.fixture(autouse=True)
def strict_transaction_guard(monkeypatch):
    """A model call inside an open transaction is an error in these tests (BOSSAPP-1232)."""
    monkeypatch.setenv(ac.STRICT_GUARD_ENV, "1")


@pytest.fixture(scope="module")
def ng():
    return load_northgate_v1()


# ── extraction (no database) ────────────────────────────────────────────────────────────────

def test_contract_extraction_on_the_northgate_spec_is_todays_23_items(ng):
    sources = ac.gather_sources(ng.spec())
    items = ac.build_items(sources)
    assert len(items) == 23
    assert [i["id"] for i in items] == [f"AC-{n}" for n in range(1, 24)]
    assert [s["kind"] for s in sources] == ["brief", "prd", "criteria"]
    # Exactly the list the in-build gate judges, in the same order.
    assert [i["text"] for i in items] == [i["text"] for i in ag.acceptance_items(ng.spec())]
    assert all(i["priority"] == "must" and i["source_ref"] for i in items)


def test_the_contract_keeps_every_word_the_gate_caps_only_what_it_reads():
    long_item = "gateway must " + "do the thing exactly as written " * 60
    assert len(long_item) > ag.MAX_ITEM_CHARS
    spec = {"instructions": "Must-haves:\n- " + long_item + "\n- short one\n"}
    items = ac.build_items(ac.gather_sources(spec))
    assert items[0]["text"] == " ".join(long_item.split())          # verbatim, never cut
    assert len(ag.acceptance_items(spec)[0]["text"]) == ag.MAX_ITEM_CHARS


def test_the_moved_extraction_is_the_gates_own():
    for name in ("brief_must_haves", "prd_gating_criteria", "_sections", "_items_of",
                 "acceptance_items", "items_dropped", "lineage_build_rows", "union_spec",
                 "item_key", "UNION_KEY", "MAX_TOTAL_ITEMS", "MAX_ITEM_CHARS"):
        assert getattr(ag, name) is getattr(ac, name), name


def test_ticket_items_come_from_the_connector_field_and_the_whole_description(ng):
    assert ac.ticket_items({"acceptance_criteria": ["  a  b ", "", "c"]}) == ["a b", "c"]
    from_body = ac.ticket_items(ng.ticket())
    # BOSSAPP-1233: every requirement the ticket states, its "Acceptance" list first (was 5).
    assert len(from_body) == 26
    assert from_body[4] == "All tests pass; the README quick start works as written."
    assert ac.ticket_items({"body": "no acceptance here"}) == []
    assert ac.ticket_items(None) == []
    # The connector's field and the description together, deduplicated, the field first.
    both = ac.ticket_items({**ng.ticket(), "acceptance_criteria": [from_body[1], "Extra"]})
    assert both[:2] == [from_body[1], "Extra"] and len(both) == 27


def test_the_northgate_ticket_yields_an_item_for_each_must_have_the_run_never_checked(ng):
    """THE NORTHGATE MISSES (crun_5f8a, 2026-11-15): judged on 8 items (the ticket's 5
    "Acceptance" lines + 3 the panel added), the build passed 7 while gwlint enforced five
    hard-coded checks (no >=20-rule rules/api-guidelines.yaml), JWT was an HS256 shared secret
    (no JWKS), and the JWT-audience rule existed only on paper. Each is now an item, word for
    word from the ticket."""
    body = ng.ticket_text
    items = ac.requirement_items(body)
    assert len(items) == 26
    whole = ac.ticket_items(ng.ticket())
    flat = ac._norm(body)
    rulebook = [i for i in whole if "`rules/api-guidelines.yaml`" in i]
    parent = rulebook[0].split(" **Security:**")[0]
    for it in whole:                                     # every word is the ticket's own
        if it in rulebook:                               # the parent's words, then the sub-bullet's
            assert it.startswith(parent + " ") and parent in flat and it[len(parent) + 1:] in flat
        else:
            assert it in flat, it
    assert len(rulebook) == 2 and all("At least 20 rules:" in i for i in rulebook)
    assert any("JWT `audience` set" in i for i in rulebook)        # the audience rule
    assert any("**API guidelines:** versioned base path" in i for i in rulebook)
    jwks = [i for i in whole if "JWKS URL with caching" in i]
    assert len(jwks) == 1 and jwks[0].startswith("**Authentication per route:** JWT/OAuth2 (issuer, "
                                                 "audience, required scopes, JWKS URL with caching)")
    assert "Stack: Go. (gateway and gwlint: one Go module, static binaries)" in whole
    # Each "What to build" capability, the gwlint CLI, the examples (prose), the quality bar.
    for lead in ("**Routing:**", "**Multi-tenant config:**", "**Rate limiting:**", "**CORS**",
                 "**Resilience:**", "**Headers:**", "**Observability:**",
                 "**Two environments:**", "**Tests:**", "**JSON Schema**", "**`gwlint` CLI**",
                 "`examples/`: `lumen-pay.good.yaml`", "Clean, idiomatic Go;", "A README with:",
                 "Plain, concise naming.", "No external services required"):
        assert sum(1 for i in whole if i.startswith(lead)) == 1, lead
    # Within the gate's batching: one pass of 40 holds the whole ticket.
    assert len(whole) <= ag.MAX_ITEMS


def test_requirement_items_read_structure_not_keywords():
    doc = """Intro prose about why we do this. It is context, not an item.

Stack: Python

# Title

## Background

Some history in prose, never an item.

## Build

- Plain bullet requirement
- Parent with short parts:
  - id
  - owner
- Parent with requirements of its own:
  - First sub-requirement that is long enough to stand as its own checkable requirement
  - Second sub-requirement that is also long enough to stand on its own as a requirement
    and wraps onto a second line

### Deliverable in prose

A script `bin/run` that prints the report.

```
- not a bullet: code
```

> - a quoted bullet: someone's words

## Stretch (not gating)

- Optional thing

## Acceptance

1. It runs
"""
    got = ac.requirement_items(doc)
    assert got == [
        "Stack: Python",
        "Plain bullet requirement",
        "Parent with short parts: id owner",
        "Parent with requirements of its own: First sub-requirement that is long enough to "
        "stand as its own checkable requirement",
        "Parent with requirements of its own: Second sub-requirement that is also long enough "
        "to stand on its own as a requirement and wraps onto a second line",
        "A script `bin/run` that prints the report.",
        "It runs"]


def test_ticket_items_dedupe_with_the_brief_and_prd_keeping_every_source(ng):
    """Multi-source: a ticket requirement the PRD also states word for word is ONE item that
    names both sources; the rest of the brief and PRD keep their own items."""
    spec = {"instructions": ng.instructions,
            "acceptance_criteria": ["`gwlint validate examples/lumen-pay.good.yaml` exits 0.",
                                    "No external services required to run the demo."]}
    items = ac.build_items(ac.gather_sources(spec, ticket=ng.ticket()))
    shared = [i for i in items if i["text"] in spec["acceptance_criteria"]]
    assert len(shared) == 2
    for i in shared:
        srcs = [x["source"] for x in i["sources"]]
        assert srcs[0] == "ticket" and "criterion" in srcs and len(set(srcs)) == len(srcs)
    assert len([i for i in items if i["source"] == "ticket"]) == 26
    assert len({ac._key(i["text"]) for i in items}) == len(items)


def test_how_an_item_is_checked_is_the_plans_answer_not_its_words():
    item = {"text": "the video reads clearly"}
    assert ac.check_hint(item, runnable=True) == "run"
    assert ac.check_hint(item, runnable=False) == "inspect"
    assert ac.check_hint({**item, "check_hint": "human"}, runnable=True) == "human"


def test_without_a_database_the_gate_judges_what_it_always_did():
    own = {"instructions": "Must-haves:\n- one\n", "acceptance_criteria": ["two"]}
    spec, source = ag.items_spec(None, run_id="crun_x", fctx={}, own_spec=own)
    assert spec == own and source == {"run_id": "crun_x", "root": True}


# ── frozen, versioned, held (database) ──────────────────────────────────────────────────────

def _run(c, project_id, *, spec, fctx, rid=None):
    from sqlalchemy import text

    rid = rid or f"crun_{uuid.uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, title, spec, finalize_ctx, "
        "created_at) VALUES (:i, :p, 'completed', 'Northgate', CAST(:s AS JSONB), "
        "CAST(:f AS JSONB), clock_timestamp())"),
        {"i": rid, "p": project_id, "s": json.dumps(spec), "f": json.dumps(fctx)})
    return rid


def _texts(spec):
    return [i["text"] for i in ag.acceptance_items(spec)]


@pytest.fixture()
def lineage(eng, project, ng):
    """A Northgate build started from its ticket: the root run carries the ticket snapshot."""
    with eng.begin() as c:
        root = _run(c, project, spec=ng.spec(), fctx={"ticket": ng.ticket()})
    yield root
    from sqlalchemy import text

    with eng.begin() as c:
        c.execute(text("DELETE FROM acceptance_contracts WHERE project_id = :p"), {"p": project})
        c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": project})


def test_the_contract_freezes_once_and_the_build_its_repairs_and_follow_ups_are_held_to_it(
        eng, project, ng, lineage):
    root = lineage
    with eng.begin() as c:
        spec, source = ag.items_spec(c, run_id=root, fctx={"ticket": ng.ticket()},
                                     own_spec=ng.spec())
    held = _texts(spec)
    assert len(held) == 48           # the ticket's 26 requirements + the 23, one shared
    assert held[0].startswith("`make run-staging` starts the gateway")      # the ticket first
    assert source["root"] is True and source["contract"]["version"] == 1
    with eng.begin() as c:
        v1 = ac.latest(c, root_run_id=root)
        # A REPAIR child carries its own (here: shortened) spec; it is held to the contract.
        repair = _run(c, project, spec={"instructions": "Repair: one test"},
                      fctx={"root_run_id": root})
        # A FOLLOW-UP's spec is the two findings it was sent to fix.
        follow = _run(c, project, spec={"instructions": "Fix the two MINOR findings",
                                        "acceptance_criteria": ["Fixed: [MINOR] a",
                                                                "Fixed: [MINOR] b"]},
                      fctx={"is_followup": True, "root_run_id": root})
    for run_id, fctx, own in (
            (repair, {"root_run_id": root}, {"instructions": "Repair: one test"}),
            (follow, {"is_followup": True, "root_run_id": root},
             {"instructions": "Fix the two MINOR findings",
              "acceptance_criteria": ["Fixed: [MINOR] a", "Fixed: [MINOR] b"]})):
        with eng.begin() as c:
            spec, source = ag.items_spec(c, run_id=run_id, fctx=fctx, own_spec=own)
        texts = _texts(spec)
        assert texts[:48] == held, run_id               # the whole contract, first, in order
        assert source["contract"] == {"id": v1["id"], "version": 1, "items": 48}
        # What a run adds beyond the contract still counts after it (never fewer items).
        if run_id == repair:
            assert texts[48:] == []
    with eng.begin() as c:
        assert [v["version"] for v in ac.versions(c, root_run_id=root)] == [1]
        # freeze is first-time-only: other sources later never rewrite version 1.
        again = ac.freeze(c, project_id=project, root_run_id=root,
                          sources=ac.gather_sources({"acceptance_criteria": ["other"]}))
        assert again["id"] == v1["id"] and len(again["items"]) == 48
        from sqlalchemy import text

        assert c.execute(text(
            "SELECT count(*) FROM events WHERE project_id = :p "
            "AND event_type = 'acceptance.contract_frozen'"), {"p": project}).scalar() == 1


def test_a_ticket_change_is_a_new_version_that_keeps_every_id(eng, project, ng, lineage):
    root = lineage
    with eng.begin() as c:
        ag.items_spec(c, run_id=root, fctx={"ticket": ng.ticket()}, own_spec=ng.spec())
        v1 = ac.latest(c, root_run_id=root)
    by_text = {i["text"]: i["id"] for i in v1["items"]}
    first_ticket = v1["items"][0]
    assert first_ticket["source"] == "ticket" and first_ticket["id"] == "AC-1"
    # A person includes a mid-run change (ticket_sync.on_card_answered writes the snapshot):
    # the first criterion is dropped and one is added.
    changed = dict(ng.ticket())
    criteria = ac.ticket_items(changed)[1:] + ["`make token` mints a demo JWT for the quick start"]
    # (The connector's field now carries the whole list; the description is emptied so the
    # dropped criterion is not read back from it.)
    changed.update(acceptance_criteria=criteria, body="", updated_at="2026-11-15")
    from sqlalchemy import text

    with eng.begin() as c:
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = CAST(:f AS JSONB) WHERE id = :i"),
                  {"f": json.dumps({"ticket": changed}), "i": root})
        follow = _run(c, project, spec={"instructions": "Fix"},
                      fctx={"is_followup": True, "root_run_id": root})
    with eng.begin() as c:
        spec, source = ag.items_spec(c, run_id=follow,
                                     fctx={"is_followup": True, "root_run_id": root},
                                     own_spec={"instructions": "Fix"})
        v2 = ac.latest(c, root_run_id=root)
    assert v2["version"] == 2 and v2["supersedes_id"] == v1["id"]
    assert source["contract"]["version"] == 2
    assert v2["reason"].startswith(f"{ng.ticket()['key']} changed: 1 item added, 1 removed")
    ids2 = {i["text"]: i["id"] for i in v2["items"]}
    assert first_ticket["text"] not in ids2                       # dropped
    assert all(ids2[t] == by_text[t] for t in ids2 if t in by_text)  # every kept id unchanged
    new = [i for i in v2["items"] if i["text"].startswith("`make token`")]
    assert new and new[0]["id"] == "AC-49" and new[0]["source"] == "ticket"
    assert "`make token` mints a demo JWT for the quick start" in _texts(spec)
    with eng.begin() as c:
        # The same ticket again changes nothing; a person's revision is version 3, and a removed
        # id is never handed to a different item.
        ag.items_spec(c, run_id=follow, fctx={"is_followup": True, "root_run_id": root},
                      own_spec={"instructions": "Fix"})
        assert [v["version"] for v in ac.versions(c, root_run_id=root)] == [1, 2]
        # Only a PERSON revises; BOSS, an agent or a job is refused, and nothing is written.
        for who in ("boss", "acceptance_agent", "agt_assay", "sva_ci", ""):
            with pytest.raises(ac.ContractRevisionRefused):
                ac.revise(c, v2["id"], removed=["AC-2"], reason="x", actor=who)
        # `removed` names item IDS; an item's words never match (they could name another item).
        with pytest.raises(ValueError):
            ac.revise(c, v2["id"], removed=[v2["items"][1]["text"]], reason="x",
                      actor="usr_test_person")
        assert [v["version"] for v in ac.versions(c, root_run_id=root)] == [1, 2]
        v3 = ac.revise(c, v2["id"], added=["Operator: the demo runs offline"], removed=["AC-2"],
                       reason="operator note", actor="usr_test_person")
        assert v3["version"] == 3 and v3["created_by"] == "usr_test_person"
        assert v3["items"][-1]["sources"] == [{"source": "operator", "source_kind": "operator",
                                                "source_ref": "usr_test_person"}]
        assert [i["id"] for i in v3["items"]][-1] == "AC-50"
        assert "AC-2" not in {i["id"] for i in v3["items"]}
        assert c.execute(text(
            "SELECT count(*) FROM events WHERE project_id = :p "
            "AND event_type = 'acceptance.contract_revised'"), {"p": project}).scalar() == 2


def test_a_follow_up_whose_builds_are_gone_is_held_to_the_contract_not_to_nothing(
        eng, project, ng, lineage):
    root = lineage
    from sqlalchemy import text

    with eng.begin() as c:
        ag.items_spec(c, run_id=root, fctx={"ticket": ng.ticket()}, own_spec=ng.spec())
        follow = _run(c, project, spec={"instructions": "Fix"},
                      fctx={"is_followup": True, "root_run_id": root})
        c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": root})
    with eng.begin() as c:
        spec, source = ag.items_spec(c, run_id=follow,
                                     fctx={"is_followup": True, "root_run_id": root},
                                     own_spec={"instructions": "Fix"})
    assert source["root"] is True and source["contract"]["version"] == 1
    assert len(_texts(spec)) == 48


def test_a_run_with_no_ticket_is_judged_on_exactly_the_items_it_was_before(eng, project, ng):
    from sqlalchemy import text

    with eng.begin() as c:
        root = _run(c, project, spec=ng.spec(), fctx={})
    try:
        with eng.begin() as c:
            spec, source = ag.items_spec(c, run_id=root, fctx={}, own_spec=ng.spec())
        assert _texts(spec) == _texts(ng.spec())
        assert source["contract"]["items"] == 23
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM acceptance_contracts WHERE lineage_root_run_id = :r"),
                      {"r": root})
            c.execute(text("DELETE FROM code_task_runs WHERE id = :r"), {"r": root})


def test_a_ticket_dropping_a_criterion_never_removes_what_the_brief_also_asked(eng, project):
    """THE REVIEWER'S REPRO (PR #139, F1): "Written in Go" is both a brief must-have and a ticket
    criterion; deduplicated, it was labelled with its first source (the ticket), so the ticket
    dropping it removed the brief's must-have. An item keeps every source, and leaves only when
    the ticket was its ONLY source."""
    from sqlalchemy import text

    ticket = {"provider": "jira", "key": "NG-2", "id": "NG-2", "title": "Gateway",
              "acceptance_criteria": ["Written in Go", "Rate limits answer 429"]}
    spec = {"instructions": "Must-haves:\n- Written in Go\n- A YAML rulebook of 20 rules\n"}
    with eng.begin() as c:
        root = _run(c, project, spec=spec, fctx={"ticket": ticket})
    try:
        with eng.begin() as c:
            ag.items_spec(c, run_id=root, fctx={"ticket": ticket}, own_spec=spec)
            v1 = ac.latest(c, root_run_id=root)
        go = next(i for i in v1["items"] if i["text"] == "Written in Go")
        assert go["source"] == "ticket"                                   # the first source...
        assert [x["source"] for x in go["sources"]] == ["ticket", "must_have"]  # ...not the only
        assert len(v1["items"]) == 3
        # The ticket drops "Written in Go" (and its 429 line), adds one.
        changed = {**ticket, "acceptance_criteria": ["Hot reload rejects a config without auth"]}
        with eng.begin() as c:
            c.execute(text("UPDATE code_task_runs SET finalize_ctx = CAST(:f AS JSONB) "
                           "WHERE id = :i"), {"f": json.dumps({"ticket": changed}), "i": root})
        with eng.begin() as c:
            spec2, _ = ag.items_spec(c, run_id=root, fctx={"ticket": changed}, own_spec=spec)
            v2 = ac.latest(c, root_run_id=root)
        assert v2["version"] == 2
        still = next(i for i in v2["items"] if i["text"] == "Written in Go")
        assert still["id"] == go["id"]                                    # same item, same id
        assert [x["source"] for x in still["sources"]] == ["must_have"]
        assert still["source"] == "must_have" and still["source_kind"] == "brief"
        texts = [i["text"] for i in v2["items"]]
        assert "Rate limits answer 429" not in texts                      # ticket-only: leaves
        assert "Hot reload rejects a config without auth" in texts
        assert "Written in Go" in _texts(spec2)
        assert v2["reason"] == "NG-2 changed: 1 item added, 1 removed, 1 re-sourced"
        # And back: the ticket lists it again, it gains the ticket as a source — no new item.
        again = {**ticket, "acceptance_criteria": ["Written in Go",
                                                   "Hot reload rejects a config without auth"]}
        with eng.begin() as c:
            c.execute(text("UPDATE code_task_runs SET finalize_ctx = CAST(:f AS JSONB) "
                           "WHERE id = :i"), {"f": json.dumps({"ticket": again}), "i": root})
        with eng.begin() as c:
            ag.items_spec(c, run_id=root, fctx={"ticket": again}, own_spec=spec)
            v3 = ac.latest(c, root_run_id=root)
        back = next(i for i in v3["items"] if i["text"] == "Written in Go")
        assert back["id"] == go["id"] and len(v3["items"]) == len(v2["items"])
        assert {x["source"] for x in back["sources"]} == {"must_have", "ticket"}
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM acceptance_contracts WHERE lineage_root_run_id = :r"),
                      {"r": root})
            c.execute(text("DELETE FROM code_task_runs WHERE id = :r"), {"r": root})


def test_a_contract_frozen_from_the_acceptance_list_alone_takes_in_the_whole_ticket(
        eng, project, ng, lineage):
    """A lineage frozen before BOSSAPP-1233 held only the ticket's 5 "Acceptance" lines. The same
    ticket snapshot, read in full, is a new version that ADDS the rest (every id kept), and says
    the ticket was read in full, not that it changed."""
    root = lineage
    old_reader = {**ng.ticket(), "acceptance_criteria": ac.ticket_items(ng.ticket())[:5]}
    with eng.begin() as c:
        v1 = ac.freeze(c, project_id=project, root_run_id=root,
                       sources=ac.gather_sources(ng.spec(), ticket={**old_reader, "body": ""}))
        assert len(v1["items"]) == 27
        v2 = ac.revise_from_ticket(c, v1, ng.ticket())
    assert v2["version"] == 2
    assert v2["reason"].startswith(f"{ng.ticket()['key']} read in full: 21 items added")
    assert len(v2["items"]) == 48
    assert [i["id"] for i in v2["items"][:27]] == [i["id"] for i in v1["items"]]
    assert any("JWKS URL with caching" in i["text"] for i in v2["items"])


# ---------------------------------------------------------------------------------------------
# BOSSAPP-1233 review: prose the structure cannot decide is read by a model, word for word
# ---------------------------------------------------------------------------------------------

class ProseReader:
    """A scripted prose reader: answers the given items (copied or not) and counts its calls."""

    def __init__(self, *texts, judgement=(), needs_loader=()):
        self.texts, self.calls, self.prompts = list(texts), 0, []
        self.judgement, self.needs_loader = list(judgement), list(needs_loader)

    def __call__(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        body = json.dumps({"items": [{"text": t, "why": "stated"} for t in self.texts],
                           "judgement": self.judgement, "needs_loader": self.needs_loader})
        return f"Reading.\n{ac._PROSE_OPEN}\n{body}\n{ac._PROSE_CLOSE}\n"


def _ticket(name, **kw):
    return {"provider": "jira", "key": "NG-9", "id": "NG-9", "title": name, "url": None,
            "body": (PROSE / name).read_text(encoding="utf-8"), "acceptance_criteria": [],
            "updated_at": "2026-11-15", **kw}


def test_a_blank_line_ends_a_bullet():
    """A bug ticket's "Environment:" paragraph after its last repro step was glued onto it."""
    body = (PROSE / "bug_short.md").read_text(encoding="utf-8")
    steps = ac.requirement_items(body)
    assert steps[-1] == "Save the file."
    assert not any("Environment" in s or "Expected" in s for s in steps)
    # An INDENTED line right under a bullet still continues it; an unindented one does not.
    assert ac.requirement_items("- one\n  more\nnot it\n") == ["one more"]


def test_a_bug_ticket_is_held_to_its_expected_behaviour_not_its_repro_steps():
    ticket = _ticket("bug_short.md", type="Bug")
    expected = ("Expected: the reload accepts the URL and requests to `/v1/payments` keep "
                "reaching the upstream.")
    reader = ProseReader(expected, "the gateway logs a panic and stops serving, paraphrased")
    prose = ac.ticket_prose(ticket, ask=reader)
    assert reader.calls == 1
    assert "steps to reproduce and the actual behaviour are context" in reader.prompts[0]
    assert "Environment: staging" in reader.prompts[0]          # the WHOLE report was read
    items = ac.ticket_items(ticket, prose=prose["prose_items"])
    assert items == [expected]                  # the paraphrase is not the ticket's words: dropped
    # Without a reader, BOSS still holds the run to what structure gives (never nothing).
    assert ac.ticket_items(ticket) == ac.requirement_items(ticket["body"])


def test_a_description_only_story_yields_its_requirements_word_for_word():
    ticket = _ticket("story_description_only.md", type="Story")
    assert ac.requirement_items(ticket["body"]) == []           # structure finds nothing
    said = ["Add a `GET /v1/usage` endpoint that returns, for the calling tenant only, the "
            "requests made in the last 24 hours per route and the share of the rate limit used.",
            "The response must be JSON and must never include another tenant's numbers.",
            "The endpoint needs the same authentication as the tenant's routes."]
    # A reader that drops a ticket's ** or ` marks still cites word for word.
    reader = ProseReader(*said, said[0].replace("`", ""), "Charts in a console")
    prose = ac.ticket_prose(ticket, ask=reader)
    assert prose["prose_items"][:3] == said and len(prose["prose_items"]) == 3
    assert "## Proposal" in reader.prompts[0] and "out of scope or for later" in reader.prompts[0]
    assert ac.ticket_items(ticket, prose=prose["prose_items"]) == said


def test_the_prose_reader_is_bounded_and_unreadable_answers_are_nothing():
    ticket = _ticket("story_description_only.md", body="Do it. " * 5000)
    reader = ProseReader()
    ac.ticket_prose(ticket, ask=reader)
    assert len(reader.prompts[0]) < ac.MAX_PROSE_CHARS + 4000
    assert ac.parse_prose("no json here", "text") == []
    many = json.dumps({"items": [{"text": f"word {i} here"} for i in range(80)]})
    text = " ".join(f"word {i} here." for i in range(80))
    assert len(ac.parse_prose(many, text)) == ac.MAX_PROSE_ITEMS


def test_northgate_sends_only_its_left_over_prose_to_the_reader(ng):
    """KAN-9's structure gives 26 items; only its intro prose is left for the reader."""
    left = ac.prose_input(ng.ticket())
    assert left.startswith("Northgate API Gateway: automate tenant API onboarding")
    assert "Routing" not in left and "At least 20 rules" not in left


class NoTransactionReader(ProseReader):
    """A prose reader that fails the test if it is called while a transaction is open."""

    def __call__(self, prompt):
        from boss_engine.db import open_transactions

        assert not open_transactions(), "the prose reader ran inside an open transaction"
        return super().__call__(prompt)


INTRO = ("We want BOSS to run it: every tenant config change is validated (format, security, "
         "API guidelines), rejected with a report and a suggested fix when it doesn't conform, "
         "then deployed to staging, contract-tested, approved, and promoted to production.")


def _cleanup_project(eng, project):
    from sqlalchemy import text

    with eng.begin() as c:
        c.execute(text("DELETE FROM acceptance_contracts WHERE project_id = :p"), {"p": project})
        c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": project})


def test_the_prose_is_read_before_the_check_and_asked_once_per_ticket_text(eng, project, ng):
    """prepare_for_run reads the prose OUTSIDE any transaction and stores it with the contract;
    every later check (items_spec, in its own transaction) reuses it — no second model call, no
    contract flapping on a second opinion."""
    reader = NoTransactionReader(INTRO)
    with eng.begin() as c:
        root = _run(c, project, spec=ng.spec(), fctx={"ticket": ng.ticket()})
    try:
        for _ in range(2):
            ac.prepare_for_run(eng, run_id=root, fctx={"ticket": ng.ticket()},
                               own_spec=ng.spec(), ask=reader)
            for _ in range(2):
                with eng.begin() as c:
                    ag.items_spec(c, run_id=root, fctx={"ticket": ng.ticket()},
                                  own_spec=ng.spec())
        assert reader.calls == 1
        with eng.begin() as c:
            v = ac.latest(c, root_run_id=root)
        assert v["version"] == 1
        assert any(i["text"] == INTRO for i in v["items"])
        src = next(x for x in v["sources"] if x["kind"] == "ticket")
        assert src["prose_digest"] and src["prose_items"] == [INTRO]
    finally:
        _cleanup_project(eng, project)


def test_a_model_call_inside_an_open_transaction_is_refused(eng, project, ng, monkeypatch):
    """The class that crashed Northgate (BOSSAPP-1232): strict, it raises; in production it is
    skipped with a warning and what the contract holds stands."""
    from boss_engine.db import begin

    reader = ProseReader(INTRO)
    with begin(eng):
        with pytest.raises(ac.ProseReadInTransaction):
            ac.ticket_prose(ng.ticket(), ask=reader)
        monkeypatch.setenv(ac.STRICT_GUARD_ENV, "0")
        held = {"prose_digest": "old", "prose_items": ["kept"]}
        assert ac.ticket_prose(ng.ticket(), ask=reader, held=held)["prose_items"] == ["kept"]
    assert reader.calls == 0


def test_the_gates_prepare_step_asks_its_agent_outside_any_transaction(eng, project, ng):
    """acceptance_gate.prepare_contract (what the pipeline calls before the check's
    transaction): the reviewer's agent answers through run_verdict, in no transaction."""
    class Agent:
        calls = 0

        def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
            from boss_engine.db import open_transactions

            assert not open_transactions()
            Agent.calls += 1
            return {"summary": ProseReader(INTRO)(prompt)}

    with eng.begin() as c:
        root = _run(c, project, spec=ng.spec(), fctx={"ticket": ng.ticket()})
    try:
        ag.prepare_contract(eng, run_id=root, fctx={"ticket": ng.ticket()}, own_spec=ng.spec(),
                            agent=Agent())
        assert Agent.calls == 1
        with eng.begin() as c:
            assert any(i["text"] == INTRO for i in ac.latest(c, root_run_id=root)["items"])
    finally:
        _cleanup_project(eng, project)


# ---------------------------------------------------------------------------------------------
# Re-review of #143: tags are set at extraction; bug layout; contained items; reader version
# ---------------------------------------------------------------------------------------------

NAMING = "Plain, concise naming. Someone at the customer will read this code."


def _rulebook_items(ng):
    return [i for i in ac.ticket_items(ng.ticket()) if "`rules/api-guidelines.yaml`" in i]


def test_judgement_and_loader_tags_are_set_at_extraction_and_reach_the_gate(eng, project, ng):
    """The reader tags items when the contract is made; the tags ride on the contract items
    into the gate's items (acceptance_items), where only they decide advisory / loader."""
    rulebook = _rulebook_items(ng)
    reader = NoTransactionReader(judgement=[NAMING, "not an item of this ticket"],
                                 needs_loader=rulebook[:1])
    with eng.begin() as c:
        root = _run(c, project, spec=ng.spec(), fctx={"ticket": ng.ticket()})
    try:
        ac.prepare_for_run(eng, run_id=root, fctx={"ticket": ng.ticket()}, own_spec=ng.spec(),
                           ask=reader)
        with eng.begin() as c:
            v = ac.latest(c, root_run_id=root)
            spec, _ = ag.items_spec(c, run_id=root, fctx={"ticket": ng.ticket()},
                                    own_spec=ng.spec())
        tagged = {i["text"]: i for i in v["items"]}
        assert tagged[NAMING].get("judgement") is True
        assert tagged[rulebook[0]].get("needs_loader") is True
        assert sum(1 for i in v["items"] if i.get("judgement")) == 1     # the stray one ignored
        gate = {i["text"]: i for i in ag.acceptance_items(spec)}
        assert gate[NAMING]["judgement"] and gate[rulebook[0]]["needs_loader"]
        # The floor adds it on the other rulebook item too (it names the rules YAML).
        assert gate[rulebook[1]].get("needs_loader")
    finally:
        _cleanup_project(eng, project)


def test_a_new_reader_reads_again_and_a_reading_that_changes_nothing_is_no_new_version(
        eng, project, ng):
    with eng.begin() as c:
        root = _run(c, project, spec=ng.spec(), fctx={"ticket": ng.ticket()})
    try:
        first = NoTransactionReader(judgement=[NAMING])
        ac.prepare_for_run(eng, run_id=root, fctx={"ticket": ng.ticket()}, own_spec=ng.spec(),
                           ask=first, model="model-a")
        same = NoTransactionReader(judgement=[NAMING])
        ac.prepare_for_run(eng, run_id=root, fctx={"ticket": ng.ticket()}, own_spec=ng.spec(),
                           ask=same, model="model-a")
        assert (first.calls, same.calls) == (1, 0)
        # Another model: read again; the same answer is bookkeeping, not a version.
        other = NoTransactionReader(judgement=[NAMING])
        ac.prepare_for_run(eng, run_id=root, fctx={"ticket": ng.ticket()}, own_spec=ng.spec(),
                           ask=other, model="model-b")
        assert other.calls == 1
        with eng.begin() as c:
            v = ac.latest(c, root_run_id=root)
        assert v["version"] == 1
        src = next(x for x in v["sources"] if x["kind"] == "ticket")
        assert src["prose_digest"] == ac.prose_digest(ng.ticket(), model="model-b")
        # A reader that tags differently re-tags the items: a new version, every id kept.
        retag = NoTransactionReader(judgement=[])
        ac.prepare_for_run(eng, run_id=root, fctx={"ticket": ng.ticket()}, own_spec=ng.spec(),
                           ask=retag, model="model-c")
        with eng.begin() as c:
            v2 = ac.latest(c, root_run_id=root)
        assert v2["version"] == 2 and not any(i.get("judgement") for i in v2["items"])
        assert [i["id"] for i in v2["items"]] == [i["id"] for i in v["items"]]
    finally:
        _cleanup_project(eng, project)


def test_the_digest_holds_the_readers_prompt_and_model(ng):
    t = ng.ticket()
    assert ac.prose_digest(t, model="a") != ac.prose_digest(t, model="b")
    assert ac.prose_digest(t) == ac.prose_digest(dict(t))
    assert ac.reader_version("a") != ac.reader_version("b")


def test_a_bug_is_known_by_its_layout_not_only_its_type():
    body = (PROSE / "bug_short.md").read_text(encoding="utf-8")
    assert ac._is_bug({"body": body})                                  # Steps / Expected / Actual
    assert ac._is_bug({"body": "## Steps to reproduce\n1. x\n\n## Expected result\nY\n"})
    assert not ac._is_bug({"body": "## Expected load\nWe expect a lot of traffic.\n"})
    assert not ac._is_bug({"body": (PROSE / "story_description_only.md").read_text()})


def test_an_item_wholly_inside_another_is_held_once():
    ticket = {"body": "", "acceptance_criteria": [
        "The response must be JSON.",
        "The response must be JSON and must never include another tenant's numbers.",
        "Tests pass."]}
    assert ac.ticket_items(ticket) == [
        "The response must be JSON and must never include another tenant's numbers.",
        "Tests pass."]
