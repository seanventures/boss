"""A PREVIEW OF AN APP WHOSE BUILD HAD A DATABASE GETS ITS OWN — and gives it back.

HelpDesk built and passed its tests against the build's throwaway Postgres, then its preview
started the same app with no DATABASE_URL and exited on ECONNREFUSED 127.0.0.1:5432; the
preview step failed and the deploy step (which requires a reviewed preview) could never run.
preview_service.provision_preview_database leases a database keyed by the PREVIEW id, so the
preview's own processes get DATABASE_URL / PG*; ending the preview drops it; the reaper keeps it
while the preview lives.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from boss_engine.services import build_services as bs  # noqa: E402
from boss_engine.services import owned_process  # noqa: E402
from boss_engine.services import preview_service as ps  # noqa: E402

_PRINT_ENV = [sys.executable, "-c",
              "import json, os; print(json.dumps({k: os.environ.get(k) for k in "
              "('DATABASE_URL', 'PGPASSWORD')}))"]


def _lease(run_id: str) -> bs.PostgresLease:
    lease = bs.PostgresLease(run_id=run_id, name=f"boss_run_{run_id}", host="h", port=5433,
                             password=f"pw-{run_id}", ledger_handle=f"pg:h:5433/{run_id}")
    bs._LEASES[run_id] = lease
    return lease


def test_a_preview_needs_a_database_when_its_build_had_one(tmp_path):
    had = {"state": {"build_database": {"postgres": True, "mode": "auto", "why": ["pg in package.json"]}}}
    assert ps.preview_needs_database(had, tmp_path)[0] == "the build ran with a Postgres database"
    # Recorded as a JSON string (some loaders): same answer.
    assert ps.preview_needs_database({"state": json.dumps(had["state"])}, tmp_path)
    # The project's Never wins; a build that had none and code that says none: none.
    assert ps.preview_needs_database(
        {"state": {"build_database": {"postgres": False, "mode": "never"}}}, tmp_path) == []
    assert ps.preview_needs_database({"state": {}}, tmp_path) == []


def test_the_apps_own_code_is_enough_when_the_build_kept_no_record(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"pg": "^8"}}))
    assert ps.preview_needs_database({"state": {}}, tmp_path)


def test_the_preview_is_leased_by_its_own_id_and_its_processes_see_it(monkeypatch):
    made = []

    def fake_provision(run_id, *, install="", server_url=None, owner=None):
        made.append(run_id)
        return _lease(run_id)

    monkeypatch.setattr(bs, "provision", fake_provision)
    monkeypatch.setattr(ps, "begin", _no_db)
    run = {"id": "crun_x", "project_id": "p", "state": {"build_database": {"postgres": True}}}
    try:
        assert ps.provision_preview_database(None, run, "prev_db_a", None) is None
        assert made == ["prev_db_a"]
        with owned_process.owner_scope("preview", "prev_db_a"):
            seen = json.loads(owned_process.run(_PRINT_ENV, capture_output=True, text=True, timeout=30,
                                                env={"PATH": os.environ.get("PATH", "")}).stdout)
        assert seen["PGPASSWORD"] == "pw-prev_db_a"
        assert seen["DATABASE_URL"].startswith("postgresql://")
        # The build's own processes never see the preview's database, nor BOSS's own spawns.
        with owned_process.owner_scope("api", "prev_db_a"):
            seen = json.loads(owned_process.run(_PRINT_ENV, capture_output=True, text=True, timeout=30,
                                                env={"PATH": os.environ.get("PATH", "")}).stdout)
        assert seen["PGPASSWORD"] is None
    finally:
        bs._LEASES.pop("prev_db_a", None)


def test_ending_the_preview_drops_its_database(monkeypatch):
    dropped = []
    monkeypatch.setattr(bs, "teardown", lambda run_id, **k: dropped.append((run_id, k.get("reason"))) or True)
    ps._release_preview_sandbox("prev_db_b")
    assert dropped == [("prev_db_b", "preview_ended")]


def test_no_server_fails_the_preview_in_plain_words_not_with_a_crash(monkeypatch):
    def refuse(run_id, **k):
        raise bs.BuildServicesUnavailable("no build-services server is configured")

    monkeypatch.setattr(bs, "provision", refuse)
    why = ps.provision_preview_database(None, {"state": {"build_database": {"postgres": True}}},
                                        "prev_db_c", None)
    assert why and "needs PostgreSQL" in why and "no build-services server" in why


def test_the_reaper_keeps_a_live_previews_database(monkeypatch):
    from boss_engine.services import resource_reaper

    seen = {}
    monkeypatch.setattr(bs, "server", lambda url=None: object())
    monkeypatch.setattr(resource_reaper, "_live_sandbox_tasks", lambda engine: {"crun_live"})
    monkeypatch.setattr(bs, "reap", lambda live, **k: seen.setdefault("live", set(live)) and {})

    class _Rows:
        def all(self):
            return [("prev_live",)]

    class _Conn:
        def execute(self, *a, **k):
            return _Rows()

    class _Begin:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    class _Engine:
        def begin(self):
            return _Begin()

    bs.reap_tick(_Engine())
    assert {"crun_live", "prev_live"} <= seen["live"]


def test_both_preview_launch_paths_lease_before_launching():
    src = (ENGINE / "boss_engine/services/preview_service.py").read_text()
    for fn in ("def start_preview(", "def _respawn_process_preview("):
        body = src.split(fn, 1)[1].split("\ndef ", 1)[0]
        assert "provision_preview_database(" in body, fn
        assert body.index("provision_preview_database(") < body.index("launch_app("), fn


class _no_db:
    def __init__(self, *_a):
        pass

    def __enter__(self):
        raise RuntimeError("no database in this test")

    def __exit__(self, *a):
        return False
