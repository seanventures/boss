"""G3: the preview gate must find the merge decision for FOLLOW-UP runs too.

The gate first derived the branch as `boss/code/<run_id>`. That is wrong for any run that
continued an existing app: such a run keeps the branch it was started on, so on real data
run `crun_eedc13fc…` sat on `boss/code/crun_b91309db…`. The derived name matched no decision
and the console reported "no review_pull_request decision found for this run" for a task that
had one (PR #98) — a preview that could never be approved, with a misleading reason.

`app_previews.branch` already records the truth. Reading it beats deriving it, which is the
same lesson `lineage_impl_files` exists for.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.services import preview_approval as pa  # noqa: E402


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
    """Answers the gate's three queries; records which branches were looked up."""

    def __init__(self, preview, decisions_by_branch, targets=()):
        self.preview = preview
        self.decisions_by_branch = decisions_by_branch
        self.targets = list(targets)
        self.looked_up: list[str] = []

    def execute(self, stmt, params=None):
        text = str(stmt)
        params = params or {}
        if "FROM app_previews" in text:
            return _Result([self.preview] if self.preview else [])
        if "FROM decisions" in text:
            branch = params.get("b")
            self.looked_up.append(branch)
            found = self.decisions_by_branch.get(branch)
            return _Result([found] if found else [])
        if "FROM deployment_targets" in text:
            return _Result(self.targets)
        return _Result([])


def _preview(**kw):
    row = {"id": "prev_1", "run_id": "crun_child", "branch": "boss/code/crun_parent",
           "status": "running", "repo": "acme/repo", "url": "http://127.0.0.1:5173",
           "project_id": "prj_1"}
    row.update(kw)
    return row


def _decision(status="pending"):
    return {"id": "dec_1", "project_id": "prj_1", "task_id": "tsk_1", "status": status,
            "recommendation": {"action": "review_pull_request", "title": "BugBox",
                               "pull_request_url": "https://github.com/acme/repo/pull/98"}}


# ── the regression ───────────────────────────────────────────────────────────

def test_finds_decision_on_the_recorded_branch_not_the_derived_one() -> None:
    """run_id says crun_child; the decision lives on the parent's branch."""
    conn = _Conn(_preview(), {"boss/code/crun_parent": _decision()})
    gate = pa.describe_gate(conn, preview_id="prev_1")
    assert gate["decision_id"] == "dec_1", gate["blockers"]
    assert gate["can_approve"] is True
    # The recorded branch must be tried FIRST — deriving is only the fallback.
    assert conn.looked_up[0] == "boss/code/crun_parent"


def test_falls_back_to_derived_branch_when_none_recorded() -> None:
    """Older preview rows may have no branch; deriving must still work."""
    conn = _Conn(_preview(branch=None), {"boss/code/crun_child": _decision()})
    gate = pa.describe_gate(conn, preview_id="prev_1")
    assert gate["decision_id"] == "dec_1"
    assert conn.looked_up == ["boss/code/crun_child"]


# ── refusals, each with a reason rather than a dead button ───────────────────

def test_already_merged_is_reported_as_such() -> None:
    conn = _Conn(_preview(), {"boss/code/crun_parent": _decision(status="expired")})
    gate = pa.describe_gate(conn, preview_id="prev_1")
    assert gate["can_approve"] is False
    assert "already 'expired'" in gate["blockers"][0]
    # Still surfaces WHICH PR, so the console can link to what shipped.
    assert gate["pull_request_url"].endswith("/98")


def test_non_running_preview_cannot_be_approved() -> None:
    conn = _Conn(_preview(status="expired"), {"boss/code/crun_parent": _decision()})
    gate = pa.describe_gate(conn, preview_id="prev_1")
    assert gate["can_approve"] is False
    assert any("not 'running'" in b for b in gate["blockers"])


def test_missing_preview_returns_none() -> None:
    assert pa.describe_gate(_Conn(None, {}), preview_id="nope") is None


# ── what the click will do, stated before the click ──────────────────────────

def test_will_deploy_lists_only_auto_deploy_targets() -> None:
    targets = [
        {"app_name": "web", "environment": "staging", "auto_deploy": True, "paused": False,
         "target_ref": "root@2.29.2.202:22", "host_name": "hetzner-ci-01",
         "host_arch": "amd64", "host_status": "verified"},
        {"app_name": "api", "environment": "staging", "auto_deploy": False, "paused": False,
         "target_ref": "root@2.29.2.202:22", "host_name": "hetzner-ci-01",
         "host_arch": "amd64", "host_status": "verified"},
        {"app_name": "worker", "environment": "staging", "auto_deploy": True, "paused": True,
         "target_ref": "root@2.29.2.202:22", "host_name": "hetzner-ci-01",
         "host_arch": "amd64", "host_status": "verified"},
    ]
    conn = _Conn(_preview(), {"boss/code/crun_parent": _decision()}, targets=targets)
    gate = pa.describe_gate(conn, preview_id="prev_1")
    assert gate["will_deploy"] == ["web"], "paused and non-auto targets must not be promised"
    assert len(gate["app_targets"]) == 3, "but all targets are still shown"


def test_record_refuses_when_gate_is_closed() -> None:
    conn = _Conn(_preview(status="expired"), {"boss/code/crun_parent": _decision()})
    try:
        pa.record_preview_approval(conn, preview_id="prev_1", actor="param")
    except pa.PreviewGateError as exc:
        assert exc.status == 409
    else:
        raise AssertionError("a closed gate must refuse")


# ── repo spelling ────────────────────────────────────────────────────────────

def test_targets_match_bare_repo_name_too() -> None:
    """`app_previews.repo` is "owner/name"; `deployment_targets.repo` may be just "name".

    settings.target_github_repo is the bare name on this deployment, and the merge path keys
    on it — so matching only "owner/name" found nothing and the gate promised to deploy
    nothing while targets existed.
    """
    seen = {}

    class _RepoConn(_Conn):
        def execute(self, stmt, params=None):
            if "FROM deployment_targets" in str(stmt):
                seen.update(params or {})
            return super().execute(stmt, params)

    conn = _RepoConn(_preview(repo="paramg/boss-sandbox"),
                     {"boss/code/crun_parent": _decision()},
                     targets=[{"app_name": "BugBox", "environment": "staging",
                               "auto_deploy": True, "paused": False,
                               "target_ref": "root@2.29.2.202:22", "host_name": "hetzner-ci-01",
                               "host_arch": "amd64", "host_status": "verified"}])
    gate = pa.describe_gate(conn, preview_id="prev_1")
    assert seen.get("full") == "paramg/boss-sandbox"
    assert seen.get("bare") == "boss-sandbox", "the bare name must be queried too"
    assert gate["will_deploy"] == ["BugBox"]
