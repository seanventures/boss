"""Two agent-tool gaps found while shepherding (2026-10-24).

* An agent concluded a platform gap "does not exist": list_platform_gaps returned 14 full rows,
  the tool result was clipped to the agent's budget, and 8 rows and a broken tail arrived as if
  they were the list. Now every clip says it cut (`clip_for_model`), and the tool pages honestly
  — compact rows, `total`, `next_offset`, and one gap in full by `id`.
* propose_action refused project='boss-dogfood' as "invented": the marker `foo` matched inside
  `dogfood`. Word markers now match whole words, and a value naming a record that exists is
  never invented.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services.tool_result_text import clip_for_model
from boss_engine.tools import build_default_registry, builtin

ENGINE = Path(builtin.__file__).resolve().parents[1]


# ── a clip says it cut ───────────────────────────────────────────────────────────────────────

def test_a_result_that_fits_is_untouched():
    assert clip_for_model('{"a": 1}', 100) == '{"a": 1}'


def test_a_cut_result_says_it_is_incomplete_and_stays_in_budget():
    body = json.dumps({"gaps": [{"id": f"brec_{i}", "symptom": "x" * 900} for i in range(14)]})
    cut = clip_for_model(body, 8_000)
    assert len(cut) <= 8_000
    assert "INCOMPLETE" in cut and f"{len(body)} characters" in cut
    assert cut.startswith(body[:1_000])


def test_every_agent_path_clips_through_the_one_function():
    """No agent path hands a model a bare slice of a tool result."""
    bare = re.compile(r"json\.dumps\(result[^\n]*\)\)?\[:")
    offenders = []
    for path in [ENGINE / "agent" / "engine_agent.py", *sorted((ENGINE / "services").glob("*.py"))]:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if bare.search(line) and ("content" in line or "body =" in line or "encoded =" in line):
                offenders.append(f"{path.name}:{n}")
    assert offenders == []


# ── list_platform_gaps pages honestly ─────────────────────────────────────────────────────────

def _gaps(n):
    return [{"id": f"brec_{i:02d}", "signature": f"platform_gap:x:{i}", "status": "open",
             "title": f"gap {i}", "symptom": "s" * 800, "root_cause": "r" * 800,
             "proposed_change": "p" * 800, "acceptance_tests": ["t" * 300], "files": [],
             "occurrences": 1, "self_fix_id": None, "created_by": "agent",
             "updated_at": f"2026-10-{i + 1:02d}"} for i in range(n)]


@pytest.fixture()
def gaps(monkeypatch):
    from boss_engine.services import boss_recommendations

    rows = _gaps(14)
    monkeypatch.setattr(boss_recommendations, "list_recommendations",
                        lambda c, p, status=None: [r for r in rows
                                                   if status is None or r["status"] == status])

    @contextmanager
    def begin():
        yield None

    spec = build_default_registry().get("list_platform_gaps")
    return lambda **args: spec.handler(SimpleNamespace(begin=begin), project_id="p",
                                       actor="ceo", args=args)


def test_a_page_says_how_many_there_are_and_where_the_next_page_starts(gaps):
    first = gaps(limit=8)
    assert first["total"] == 14 and first["returned"] == 8 and first["next_offset"] == 8
    assert first["complete"] is False
    assert first["gaps"][0]["id"] == "brec_13", "newest first"
    rest = gaps(limit=8, offset=8)
    assert rest["returned"] == 6 and rest["next_offset"] is None
    assert {g["id"] for g in first["gaps"] + rest["gaps"]} == {f"brec_{i:02d}" for i in range(14)}


def test_the_default_page_is_compact_and_fits_an_agents_budget(gaps):
    page = gaps()
    assert page["total"] == 14 and page["returned"] == 14 and page["complete"] is True
    assert "symptom" not in page["gaps"][0]
    assert len(json.dumps(page)) < 8_000, "the whole list must fit the smallest tool budget"
    assert "symptom" in gaps(detail=True, limit=2)["gaps"][0]


def test_one_gap_by_id_in_full_and_a_missing_one_says_so(gaps):
    one = gaps(id="brec_03")
    assert one["found"] is True and one["gap"]["id"] == "brec_03"
    assert one["gap"]["root_cause"] == "r" * 800
    missing = gaps(id="brec_99")
    assert missing["found"] is False and missing["ok"] is False


# ── a real id is never "invented" ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [{"project": "boss-dogfood"}, {"repository": "acme/barcode"},
                                  {"app": "awesome-api"}, {"repo": "paramg/todolist-api"}])
def test_a_word_inside_a_real_name_is_not_a_placeholder(args):
    assert builtin._placeholder_argument(args) == ""


@pytest.mark.parametrize("args", [{"run_id": "crun_placeholder"}, {"project": "foo"},
                                  {"app": "foo-app"}, {"repository": "owner/name"},
                                  {"domain": "x.example.com"}, {"repo": "some-repo"},
                                  {"target": "<target>"}, {"run_id": "TODO"}])
def test_an_invented_identifier_is_still_refused(args):
    assert builtin._placeholder_argument(args)


class _Rows:
    def __init__(self, hit):
        self._hit = hit

    def first(self):
        return (1,) if self._hit else None


class _Conn:
    def __init__(self, real):
        self.real, self.asked = real, []

    def execute(self, statement, params):
        self.asked.append((str(statement), params["v"]))
        return _Rows(params["v"] in self.real)

    @contextmanager
    def begin_nested(self):
        yield


def _ctx(real):
    conn = _Conn(real)

    @contextmanager
    def begin():
        yield conn

    return SimpleNamespace(begin=begin, settings=None), conn


def test_a_value_that_names_a_record_that_exists_is_never_invented():
    ctx, conn = _ctx({"todo-tracker"})
    assert builtin._placeholder_argument({"project": "todo-tracker"}, ctx) == ""
    assert "FROM projects" in conn.asked[0][0]
    # the same spelling with no such record is still refused
    ctx, _ = _ctx(set())
    assert builtin._placeholder_argument({"project": "todo-tracker"}, ctx)


def test_propose_action_offers_a_button_for_a_real_project(monkeypatch):
    ctx, _ = _ctx({"boss-dogfood"})
    out = build_default_registry().get("propose_action").handler(
        ctx, project_id="boss-dogfood", actor="ceo",
        args={"tool": "set_project_setting",
              "args": {"project": "boss-dogfood", "key": "code_agent_max_turns", "value": "90"},
              "label": "Raise turns", "why": "the build ran out"})
    assert out.get("proposed") is True, out
