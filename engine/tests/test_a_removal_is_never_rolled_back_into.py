"""A removal is never run again by Roll back or Redeploy.

THE CASE (HelpDesk AI, 2026-10-17). The target's newest delivery run was a failed manual deploy;
the run before it was the governed REMOVAL of the app (delete_data). Roll back picks "the
earlier succeeded run" — the removal — and redeploy() planned its packet again: the app and its
database volume were removed from a Roll back press, with no new approval
(deprun_a2a265b71ce145a08249). A removal is its own governed act; going back never repeats it."""
from __future__ import annotations

import contextlib

from boss_engine.services import delivery_pipeline, infra_view


def _h(i, status, kind="deploy"):
    return {"id": i, "status": status, "kind": kind}


def test_roll_back_never_picks_a_removal_or_anything_older():
    history = [_h("failed_manual", "failed"), _h("removal", "succeeded", "remove"),
               _h("old_release", "succeeded")]
    assert infra_view.delivery_rollback_source("failed_manual", history) is None
    ok = [_h("failed_manual", "failed"), _h("good", "succeeded"), _h("removal", "succeeded", "remove")]
    assert infra_view.delivery_rollback_source("failed_manual", ok) == "good"
    assert infra_view.is_removal_run({"release_packet": {"kind": "remove"}})
    assert not infra_view.is_removal_run({"release_packet": {}})


def test_redeploy_refuses_a_removal(monkeypatch):
    class Rows:
        def execute(self, *a, **k):
            class R:
                def mappings(self):
                    return self

                def first(self):
                    return {"id": "deprun_r", "project_id": "p",
                            "release_packet": {"kind": "remove", "removal": {"delete_data": True}}}
            return R()

    @contextlib.contextmanager
    def begin(_engine):
        yield Rows()

    import boss_engine.db as db
    monkeypatch.setattr(db, "begin", begin)
    planned = []
    from boss_engine.services import resource_plane
    monkeypatch.setattr(resource_plane.ResourcePlaneService, "plan_deployment_run",
                        lambda *a, **k: planned.append(k) or {})
    out = delivery_pipeline.redeploy(object(), None, "deprun_r", actor="a", execute=False)
    assert out["status"] == "blocked" and "removed the app" in out["reason"]
    assert planned == [], "nothing is planned"
