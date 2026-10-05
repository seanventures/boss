"""Every panel turn carries a gist: a plain headline and up to three key points.

The meeting room shows each seat's turn as a card — face, headline, three points, "Read all".
Seats reply in raw markdown (headings, bold, tables, numbered lists), and cutting the card out
of that in the browser put a table row in the headline and "**Priority** | **Why**" in the
points. So each seat writes its own gist beside its reply, the engine stores it with the turn,
and a meeting held before gists existed gets one cut deterministically.
"""
from __future__ import annotations

import contextlib
import json

from boss_engine.services import meeting_gist as mg
from boss_engine.services import meeting_service as ms

REPLY = """**UX Designer – Initial Take on the Lead-Triage Inbox**

### 1. Core priorities
| Priority | Why |
|---|---|
| Score at a glance | Teams scan 40 enquiries a morning |

1. **Speed of the mental model** — a rep must read a score in under a second.
- Every draft is editable before anything is sent, with one click to approve.
- Decline drafts are polite and never mention the score itself.

```mermaid
flowchart LR
  A --> B
```
"""


# ── the seat writes it ─────────────────────────────────────────────────────────────────────

def test_the_seats_gist_line_is_taken_off_the_reply_and_kept():
    reply = REPLY + '\nGIST: {"headline": "Make the **score** readable in a second", ' \
                    '"points": ["Editable drafts", "Polite declines", "Filter by score", "a 4th"]}\n'
    body, gist = mg.split_reply_gist(reply)
    assert "GIST" not in body
    assert body.rstrip().endswith("```")
    assert gist == {"headline": "Make the score readable in a second",
                    "points": ["Editable drafts", "Polite declines", "Filter by score"]}


def test_a_fenced_gist_line_is_still_read():
    body, gist = mg.split_reply_gist("Reply.\n```\nGIST: {\"headline\": \"Ship it\", \"points\": []}\n```")
    assert body == "Reply."
    assert gist == {"headline": "Ship it", "points": []}


def test_a_malformed_or_missing_gist_leaves_the_reply_untouched():
    for reply in (REPLY, REPLY + "\nGIST: {not json}", REPLY + '\nGIST: {"points": ["x"]}'):
        body, gist = mg.split_reply_gist(reply)
        assert gist is None
        assert body == reply


def test_only_the_closing_line_counts():
    """A reply that talks ABOUT a gist earlier keeps that text."""
    reply = 'GIST: {"headline": "early", "points": []}\n\nThe real reply goes on.'
    assert mg.split_reply_gist(reply) == (reply, None)


# ── or it is cut deterministically ─────────────────────────────────────────────────────────

def test_the_fallback_reads_the_first_heading_and_the_lines_that_say_something():
    gist = mg.extract_gist(REPLY)
    assert gist["headline"] == "UX Designer – Initial Take on the Lead-Triage Inbox"
    assert gist["points"] == [
        "Speed of the mental model — a rep must read a score in under a second.",
        "Every draft is editable before anything is sent, with one click to approve.",
        "Decline drafts are polite and never mention the score itself.",
    ]


def test_the_fallback_never_carries_markdown_tables_or_code():
    gist = mg.extract_gist(REPLY)
    text = json.dumps(gist)
    for mark in ("**", "|", "###", "```", "flowchart", "Score at a glance"):
        assert mark not in text


def test_long_lines_are_clipped_on_a_word():
    gist = mg.extract_gist("# " + "word " * 60 + "\n\n" + "point " * 80)
    assert len(gist["headline"]) <= mg.HEADLINE_MAX and gist["headline"].endswith("…")
    assert len(gist["points"][0]) <= mg.POINT_MAX and not gist["points"][0].endswith(" …")


def test_an_empty_reply_has_an_empty_gist():
    assert mg.extract_gist("") == {"headline": "", "points": []}
    assert mg.extract_gist(None) == {"headline": "", "points": []}


def test_a_round_serves_the_stored_gist_and_fills_the_rest():
    stored = {"UX Designer": {"headline": "Stored", "points": ["one"]}}
    got = mg.gists_for({"UX Designer": REPLY, "QA / Test": "## Test plan\n\nCover every path end to end."},
                       stored)
    assert got["UX Designer"] == {"headline": "Stored", "points": ["one"]}
    assert got["QA / Test"] == {"headline": "Test plan", "points": ["Cover every path end to end."]}


def test_a_room_record_carries_both_rounds_and_the_synthesis():
    out = mg.room_gists({"PM": "# Scope first\n\nThe first point that matters."}, {},
                        "## Agreed\n\nShip the slice.")
    assert out["round1_gists"]["PM"]["headline"] == "Scope first"
    assert out["round2_gists"] == {}
    assert out["synthesis_gist"] == {"headline": "Agreed", "points": ["Ship the slice."]}
    assert mg.room_gists(None, None, None)["synthesis_gist"] is None


# ── the engine asks for it and stores it with the turn ─────────────────────────────────────

class _Adapter:
    def __init__(self):
        self.prompts: list[str] = []

    def create_text_response(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        return "ref", REPLY + '\nGIST: {"headline": "Seat view", "points": ["p1", "p2"]}', \
            {"model": "fake", "input_tokens": 1, "output_tokens": 1}


def _advance(monkeypatch, phase: str, state: dict) -> tuple[dict, _Adapter]:
    adapter = _Adapter()
    saved: dict = {}
    personas = [{"key": "ux_designer", "title": "UX Designer", "lens": "users"},
                {"key": "qa", "title": "QA / Test", "lens": "tests"}]
    run = {"id": "mrun_1", "project_id": "p", "task_id": "t", "status": "running", "phase": phase,
           "directive": "Build a lead triage inbox", "url": "", "constraints": {},
           "role_keys": ["ux_designer", "qa"], "state": state, "usage": {}}

    @contextlib.contextmanager
    def _begin(_engine):
        yield object()

    from boss_engine.services import agent_persona, model_routing

    monkeypatch.setattr(ms, "begin", _begin)
    monkeypatch.setattr(ms, "load_meeting_run", lambda c, rid: run)
    monkeypatch.setattr(ms, "load_role_map", lambda c, p: {})
    monkeypatch.setattr(ms, "resolve_roles", lambda keys, role_map=None: personas)
    monkeypatch.setattr(agent_persona, "seat_voices", lambda c, p, ps: ps)
    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda c, s, p: adapter)
    monkeypatch.setattr(ms, "_save_phase",
                        lambda c, rid, st, cost, model, nxt: saved.update(state=st, next=nxt))

    class _Jobs:
        def create_job(self, *a, **k):
            return None

    ms.advance_meeting_run(None, None, _Jobs(), "mrun_1")
    return saved, adapter


def test_round_one_asks_every_seat_for_a_gist_and_stores_it(monkeypatch):
    saved, adapter = _advance(monkeypatch, "round1", {})
    assert saved["next"] == "round2"
    assert all(mg.GIST_INSTRUCTION in p for p in adapter.prompts)
    st = saved["state"]
    assert set(st["round1"]) == {"UX Designer", "QA / Test"}
    assert all("GIST" not in reply for reply in st["round1"].values())
    assert st["round1_gists"]["QA / Test"] == {"headline": "Seat view", "points": ["p1", "p2"]}


def test_round_two_debates_the_replies_not_the_gist_lines(monkeypatch):
    clean = {"UX Designer": "Round one reply.", "QA / Test": "Another reply."}
    saved, adapter = _advance(monkeypatch, "round2", {"round1": clean})
    assert saved["next"] == "synthesis"
    assert "GIST:" not in adapter.prompts[0].split("what the whole panel said in round 1")[1] \
        .split(mg.GIST_INSTRUCTION)[0]
    assert saved["state"]["round2_gists"]["UX Designer"]["headline"] == "Seat view"
