"""G1: one merge must deploy EVERY app it changed, not whichever target was registered last.

`target_for` returned a single row via `ORDER BY created_at DESC LIMIT 1`, while the table is
keyed `(project_id, app_name, environment)`. A repo holding two apps therefore deployed one of
them and silently never shipped the other — silently, because "no target" is a legitimately
quiet outcome, so nothing distinguished "nothing to deploy" from "we dropped an app".

These tests pin the resolver only. Execution is covered by test_delivery_execute_verdict.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.services import delivery_pipeline as dp  # noqa: E402


class _Maps:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Maps(self._rows)


class _Conn:
    """Returns the given target rows for the resolver's SELECT."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, stmt, params=None):
        if "FROM deployment_targets" in str(stmt):
            return _Result(self.rows)
        return _Result([])


def _target(app, globs, **kw):
    row = {"id": f"dt_{app}", "app_name": app, "environment": "staging",
           "repo": "acme/monorepo", "path_globs": globs, "auto_deploy": True,
           "paused": False, "status": "active"}
    row.update(kw)
    return row


def _resolve(rows, changed):
    return dp.targets_for(_Conn(rows), "prj_1", "acme/monorepo", "staging",
                          changed_paths=changed)


# ── the defect itself ────────────────────────────────────────────────────────

def test_two_apps_both_resolve() -> None:
    """The G1 regression: both apps must come back, not just the newest."""
    rows = [_target("web", ["apps/web/**"]), _target("api", ["services/api/**"])]
    found = _resolve(rows, ["apps/web/src/main.tsx", "services/api/handler.py"])
    assert {t["app_name"] for t in found} == {"web", "api"}


def test_only_the_changed_app_resolves() -> None:
    rows = [_target("web", ["apps/web/**"]), _target("api", ["services/api/**"])]
    found = _resolve(rows, ["apps/web/src/main.tsx"])
    assert [t["app_name"] for t in found] == ["web"]


# ── the existing single-app world must not move ──────────────────────────────

def test_empty_globs_is_whole_repo() -> None:
    """Every target registered before path_globs existed has [] and must still deploy."""
    rows = [_target("solo", [])]
    assert len(_resolve(rows, ["anything/at/all.py"])) == 1
    assert len(_resolve(rows, None)) == 1


def test_globs_as_json_string() -> None:
    """Some drivers hand JSONB back as text; the resolver must not care."""
    rows = [_target("web", '["apps/web/**"]')]
    assert [t["app_name"] for t in _resolve(rows, ["apps/web/x.ts"])] == ["web"]


# ── failing open, deliberately ───────────────────────────────────────────────

def test_unknown_changed_paths_deploys_scoped_targets_anyway() -> None:
    """If we cannot tell what changed, deploy rather than skip.

    An extra deploy of unchanged code is cheap and visible. An app that quietly stops
    shipping is the bug this whole change exists to remove.
    """
    rows = [_target("web", ["apps/web/**"]), _target("api", ["services/api/**"])]
    found = _resolve(rows, None)
    assert {t["app_name"] for t in found} == {"web", "api"}
    assert all(t.get("_globs_unevaluated") for t in found), "must be flagged, not silent"


def test_no_match_yields_nothing() -> None:
    rows = [_target("web", ["apps/web/**"])]
    assert _resolve(rows, ["docs/README.md"]) == []


# ── glob shapes an operator will actually type ───────────────────────────────

def test_directory_prefix_forms_all_match() -> None:
    for glob in ("apps/web/**", "apps/web/*", "apps/web/"):
        rows = [_target("web", [glob])]
        found = _resolve(rows, ["apps/web/src/deep/nested/main.tsx"])
        assert [t["app_name"] for t in found] == ["web"], f"glob {glob!r} failed to match"


def test_leading_dot_slash_is_ignored_on_both_sides() -> None:
    rows = [_target("web", ["./apps/web/**"])]
    assert len(_resolve(rows, ["./apps/web/src/main.tsx"])) == 1


# ── back-compat shim ─────────────────────────────────────────────────────────

def test_target_for_still_returns_one_or_none() -> None:
    assert dp.target_for(_Conn([]), "prj_1", "acme/monorepo", "staging") is None
    rows = [_target("web", [])]
    assert dp.target_for(_Conn(rows), "prj_1", "acme/monorepo", "staging")["app_name"] == "web"
