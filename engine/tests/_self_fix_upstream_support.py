"""Shared support for the upstream half of BOSS fixes itself (BOSSAPP-571 phase 3): a verified
self-fix row, and a fake GitHub that records every call and answers what a test scripts.

Nothing here is collected (no test_ names). Neutral names and RFC 5737 addresses only.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import text

DIFF = (
    "diff --git a/engine/boss_engine/services/stack_deploy.py b/engine/boss_engine/services/"
    "stack_deploy.py\n"
    "--- a/engine/boss_engine/services/stack_deploy.py\n"
    "+++ b/engine/boss_engine/services/stack_deploy.py\n"
    "@@ -1,3 +1,4 @@\n"
    " def plan_stack_deploy(target):\n"
    "+    retire = earlier_shared_projects(target)  # example-app on 203.0.113.10\n"
    "     return target\n"
    "diff --git a/engine/tests/test_a_release_retires_its_app.py b/engine/tests/"
    "test_a_release_retires_its_app.py\n"
    "--- /dev/null\n"
    "+++ b/engine/tests/test_a_release_retires_its_app.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def test_a_release_retires_its_app():\n"
    "+    assert True is not False\n")


def insert_verified(eng, project_id: str, *, diff: str = DIFF, status: str = "verified",
                    engine: str = "scripted", log_tail: str = "", symptom: str = "",
                    **extra: Any) -> str:
    """A self-fix as a finished, verified one looks: its local PR, tests, gates and review."""
    sfx_id = f"sfx_{uuid.uuid4().hex[:20]}"
    pr = {"title": "fix(deploy): a release that leaves the shared project retires its app",
          "branch": f"boss/self-fix/{sfx_id}-retire", "diff": diff,
          "labels": ["source:boss-agent", "boss:self-fix", "area:deploy", "engine:scripted",
                     "signature:deploy:port_held", "tier:3"],
          "trailers": {"BOSS-Self-Fix": sfx_id, "BOSS-Signature": "deploy:port_held"},
          "body": "local body", "delivery": "local"}
    row = {
        "id": sfx_id, "project_id": project_id, "run_id": f"wfr_{uuid.uuid4().hex[:20]}",
        "step_id": "deploy", "step_kind": "deploy", "signature": "deploy:port_held:8000",
        "class_signature": "deploy:port_held", "area": "deploy", "status": status,
        "confidence": "high", "base_sha": "a" * 40, "fix_commit_sha": "b" * 40,
        "fix_tree_sha": "c" * 40, "branch": pr["branch"], "tier": 3,
        "location": "engine/boss_engine/services/stack_deploy.py::plan_stack_deploy",
        "engine": engine, "created_by": "boss_maintainer"}
    row.update({k: v for k, v in extra.items() if k in row or k in (
        "upstream_state", "stack_state")})
    jsons = {
        "pr": pr, "flags": ["host_effect"],
        "paths": [{"path": "engine/boss_engine/services/stack_deploy.py", "tier": 3}],
        "diff_stat": {"files": ["engine/boss_engine/services/stack_deploy.py",
                                "engine/tests/test_a_release_retires_its_app.py"],
                      "added": 3, "removed": 0},
        "red_green": {"tests": ["engine/tests/test_a_release_retires_its_app.py"],
                      "base": {"exit_code": 1, "summary": "1 failed"},
                      "head": {"exit_code": 0, "summary": "1 passed"}},
        "gates": {"protected_paths": {"ok": True}, "tests_not_weakened": {"ok": True},
                  "affected_suites": {"ok": True, "detail": {"tests": 4}},
                  "p0_tier": {"ok": True, "detail": {"tests": 9}}},
        "review": {"seats": {"review_code": {"ok": True, "verdict": "PASS"},
                             "security_check": {"ok": True, "verdict": "PASS"}}},
        "diagnosis": {"hypothesis": "a release that leaves the shared project never retires "
                                    "its app container there",
                      "rejected": [{"hypothesis": "fix the single-container retire step",
                                    "why": "the old container lives in the shared project"}]},
        "evidence": {"symptom": symptom or "Port 8000 is held by the app's old container",
                     "log_tail": log_tail or "Bind for 0.0.0.0:8000 failed: port is already "
                                             "allocated"},
        "verification": {"outcome": "new_cause", "next_signature": "deploy:unwired:res-db"},
    }
    cols = list(row) + list(jsons)
    values = ", ".join(f":{k}" if k in row else f"CAST(:{k} AS JSONB)" for k in cols)
    params = {**row, **{k: json.dumps(v) for k, v in jsons.items()}}
    with eng.begin() as c:
        c.execute(text(f"INSERT INTO self_fixes ({', '.join(cols)}) VALUES ({values})"), params)
        c.execute(text("UPDATE self_fixes SET bundle = :b WHERE id = :i"),
                  {"b": b"bundle", "i": sfx_id})
    return sfx_id


@dataclass
class Resp:
    status: int
    data: Any = None

    def json(self) -> Any:
        return self.data


@dataclass
class FakeGitHub:
    """`github_app._call`'s shape: (method, path, token=, body=) -> Resp. Scripted by path."""
    login: Optional[str] = "pat-example"
    fork: Optional[str] = None
    commits: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    search_status: int = 200

    def __call__(self, method: str, path: str, *, token: str, body: Any = None) -> Resp:
        self.calls.append((method, path, body))
        if path == "/user":
            return Resp(200 if self.login else 401, {"login": self.login})
        if path.startswith("/search/commits"):
            return Resp(self.search_status, {"items": self.commits})
        if path.startswith("/search/issues"):
            return Resp(self.search_status, {"items": self.issues})
        if method == "GET" and self.login and path == f"/repos/{self.login}/boss":
            if self.fork:
                return Resp(200, {"full_name": self.fork, "fork": True,
                                  "parent": {"full_name": "seanventures/boss"}})
            return Resp(404, {})
        if method == "GET" and path.startswith("/repos/seanventures/boss/forks"):
            return Resp(200, [])
        if method == "POST" and path == "/repos/seanventures/boss/forks":
            self.fork = f"{self.login}/boss"
            return Resp(202, {"full_name": self.fork})
        if method == "GET" and "/git/commits/" in path:
            return Resp(200, {"sha": path.rsplit("/", 1)[1], "tree": {"sha": "t" * 40}})
        if method == "POST" and path.endswith("/git/blobs"):
            return Resp(201, {"sha": "1" * 40})
        if method == "POST" and path.endswith("/git/trees"):
            return Resp(201, {"sha": "2" * 40})
        if method == "POST" and path.endswith("/git/commits"):
            return Resp(201, {"sha": "3" * 40})
        if method == "POST" and path.endswith("/git/refs"):
            return Resp(201, {})
        if method == "POST" and path == "/repos/seanventures/boss/pulls":
            return Resp(201, {"html_url": "https://github.com/seanventures/boss/pull/412",
                              "number": 412})
        if method == "POST" and "/issues/" in path and path.endswith("/comments"):
            return Resp(201, {"html_url": "https://github.com/seanventures/boss/pull/300"
                                          "#issuecomment-1"})
        return Resp(404, {})


def settings(**update: Any):
    """BOSS's settings with the telemetry switches taken from the ENVIRONMENT (a cached
    Settings would carry whatever the first test of the session set)."""
    from boss_engine.config import get_settings

    return get_settings().model_copy(update={"telemetry": "", "do_not_track": "",
                                             "telemetry_url": "", **update})


def client_for(fake: FakeGitHub, token: str = "a-person-token"):
    from boss_engine.services import self_fix_upstream as up

    return up.GitHubUpstream(token, call=fake)
