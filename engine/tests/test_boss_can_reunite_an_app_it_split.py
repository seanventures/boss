"""One product showed as two apps, and nothing in BOSS could put it back together.

The console showed "Incident Timeline Builder." — 17 attempts, PR #164 — beside
"Incident-Timeline Builder" — 2 attempts, PR #166, and the live deployment. One product, two
cards, two PRs, two bills, and the deployed one carrying the smaller half of the work.

`stable_app_id` hashes `identity_key(name)`, and identity_key was fixed to collapse punctuation
for exactly this reason: a trailing full stop and a U+2010 hyphen must not fork an app. Today
both titles hash the same. But app_id is STAMPED AT CREATION and never re-derived, so the fix
only helps work started after it. Everything split before stayed split, permanently, with no
control anywhere in the product to correct it — the customer keeps the consequences of our bug.

So BOSS gets to do what an engineer would: find the splits, and merge them.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from boss_engine.services.naming import identity_key, project_name, stable_app_id
from boss_engine.tools.builtin import build_default_registry

#: The two titles, verbatim from the runs that produced the two cards. The second one's hyphen
#: is U+2010, not ASCII — which is the whole reason they parted company.
TITLE_A = "Build an incident timeline builder.\nWho it is for: an on-call engineer"
TITLE_B = "Incident‐Timeline Builder"


class TestTheSplitCannotHappenAgain:
    def test_the_two_real_titles_now_share_one_identity(self):
        assert identity_key(project_name(TITLE_A)[0]) == identity_key(project_name(TITLE_B)[0])

    def test_and_therefore_one_app_id(self):
        assert (stable_app_id("boss-dogfood", "boss-sandbox", TITLE_A)
                == stable_app_id("boss-dogfood", "boss-sandbox", TITLE_B))

    def test_they_still_display_differently(self):
        """Identity is normalised; the DISPLAY name keeps whatever it had. Merging must not
        rename anybody's app."""
        assert project_name(TITLE_A)[0] != project_name(TITLE_B)[0]


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.updates: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if sql.strip().upper().startswith("UPDATE"):
            self.updates.append((sql, params))
            target = params.get("a")
            for r in self.rows:
                if (r["finalize_ctx"] or {}).get("app_id") == target:
                    r["finalize_ctx"] = {**(r["finalize_ctx"] or {}), "app_id": "KEPT"}
            return self
        if "SELECT id FROM code_task_runs" in sql:
            wanted = params.get("a")
            return _Rows([r for r in self.rows
                          if (r["finalize_ctx"] or {}).get("app_id") == wanted])
        return _Rows(self.rows)

    def mappings(self):
        return self


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeCtx:
    def __init__(self, rows):
        self.conn = FakeConn(rows)
        self.settings = object()

    @contextmanager
    def begin(self):
        yield self.conn


def _rows():
    """The shape the console actually had: 17 runs on one identity, 2 on the other."""
    out = []
    for i in range(17):
        out.append({"id": f"crun_a{i}", "title": TITLE_A, "status": "completed",
                    "pull_request_number": 164 if i == 0 else None,
                    "finalize_ctx": {"app_id": "app_4c55"}, "created_at": i})
    for i in range(2):
        out.append({"id": f"crun_b{i}", "title": TITLE_B, "status": "completed",
                    "pull_request_number": 166, "finalize_ctx": {"app_id": "app_b821"},
                    "created_at": 100 + i})
    return out


@pytest.fixture
def tools():
    reg = build_default_registry()
    return {spec.name: spec for spec in reg.specs()}


class TestFindingTheSplit:
    def test_it_reports_one_product_under_two_identities(self, tools):
        ctx = FakeCtx(_rows())
        out = tools["find_split_apps"].handler(
            ctx, project_id="boss-dogfood", actor="ceo", args={})
        assert out["count"] == 1
        found = out["split_apps"][0]
        assert {i["app_id"] for i in found["identities"]} == {"app_4c55", "app_b821"}

    def test_it_shows_both_display_names_and_both_prs(self, tools):
        """The operator has to recognise the two cards they are looking at."""
        ctx = FakeCtx(_rows())
        found = tools["find_split_apps"].handler(
            ctx, project_id="boss-dogfood", actor="ceo", args={})["split_apps"][0]
        prs = sorted(pr for i in found["identities"] for pr in i["pull_requests"])
        assert prs == [164, 166]
        names = {n for i in found["identities"] for n in i["display_names"]}
        assert len(names) == 2

    def test_it_suggests_keeping_the_identity_with_the_work(self, tools):
        ctx = FakeCtx(_rows())
        found = tools["find_split_apps"].handler(
            ctx, project_id="boss-dogfood", actor="ceo", args={})["split_apps"][0]
        assert found["suggested_keep"] == "app_4c55"

    def test_an_app_with_one_identity_is_not_reported(self, tools):
        rows = [r for r in _rows() if (r["finalize_ctx"] or {}).get("app_id") == "app_4c55"]
        ctx = FakeCtx(rows)
        out = tools["find_split_apps"].handler(
            ctx, project_id="boss-dogfood", actor="ceo", args={})
        assert out["count"] == 0


class TestMergingThem:
    def test_every_run_moves_to_the_kept_identity(self, tools):
        rows = _rows()
        ctx = FakeCtx(rows)
        out = tools["merge_apps"].handler(
            ctx, project_id="boss-dogfood", actor="ceo",
            args={"keep_app_id": "app_4c55", "merge_app_ids": ["app_b821"]})
        assert out["ok"] and out["runs_moved"] == 2

    def test_the_previous_identity_is_recorded_so_it_can_be_undone(self, tools):
        """A regrouping with no record of what it was before cannot be argued with later."""
        ctx = FakeCtx(_rows())
        tools["merge_apps"].handler(
            ctx, project_id="boss-dogfood", actor="ceo",
            args={"keep_app_id": "app_4c55", "merge_app_ids": ["app_b821"]})
        written = "".join(str(p.get("v", "")) for _sql, p in ctx.conn.updates)
        assert "merged_from_app_id" in written and "app_b821" in written

    def test_it_refuses_to_merge_an_app_into_itself(self, tools):
        ctx = FakeCtx(_rows())
        out = tools["merge_apps"].handler(
            ctx, project_id="boss-dogfood", actor="ceo",
            args={"keep_app_id": "app_4c55", "merge_app_ids": ["app_4c55"]})
        assert not out["ok"]

    def test_it_refuses_without_something_to_merge(self, tools):
        ctx = FakeCtx(_rows())
        out = tools["merge_apps"].handler(
            ctx, project_id="boss-dogfood", actor="ceo",
            args={"keep_app_id": "app_4c55", "merge_app_ids": []})
        assert not out["ok"]

    def test_it_is_a_write_so_the_console_shows_it_as_an_action(self, tools):
        assert tools["merge_apps"].kind == "write"
        assert tools["find_split_apps"].kind == "read"
