"""A sandboxed session reads the tree it was sent, never an empty home (BOSSAPP-1323/1324).

THE CASE (northgate-gateway, change #2, 2026-11-16). claude_code's review seats run on a copy
of the tree (engine_contract._isolated_copy). The copy was made in the system temp directory,
outside the run's sandbox directory, and a sandboxed process may only stand inside its own run
directory, so sandbox_identity.cwd silently swapped it for the run's HOME. Every reviewer,
acceptance check and planner of the lineage started with no tree (agent_turns: "your current
working directory is /var/lib/boss/sandboxes/<run>/home"):

  * crun_95aca9c7's acceptance re-check found nothing and called all 30 must-haves MISSING
    ("no go.mod exists"), replacing a judged 25 met / 5 partial;
  * the follow-up crun_bbf93235's planner searched the disk, found an old Node checkout of the
    same project under /tmp/boss-kg-index, and planned the Go must-haves against it.

Now the copy is made inside the run's sandbox directory, and a session whose tree is outside it
is refused in words instead of starting in an empty home.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import engine_contract as ec
from boss_engine.services import sandbox_identity
from boss_engine.services.engine_contract import ContractedAgent, EngineCapabilities


class _Seat:
    """An engine like claude_code: its read-only seat keeps a shell, so it reviews a copy."""

    engine = "claude_code"
    capabilities = EngineCapabilities(read_only=True, read_only_shell=True)

    def __init__(self):
        self.ran_in: Path = Path(".")
        self.saw: list[str] = []

    def run_role(self, prompt, cwd, *, read_only=False, model=None, resume_session_id=None,
                 max_turns=None, parallel_agents=0, **_):
        self.ran_in = Path(cwd)
        self.saw = sorted(p.relative_to(cwd).as_posix() for p in Path(cwd).rglob("*.go"))
        return {"summary": "ACCEPTANCE: MET", "usage": {}, "cost_usd_micros": 0, "turns": 1}

    def run(self, spec, repo, sandbox):  # pragma: no cover
        raise NotImplementedError


def _identity(run_dir: Path):
    def workspace(prefix: str) -> Path:
        import tempfile

        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(run_dir)))

    return SimpleNamespace(run_dir=run_dir, home=run_dir / "home", workspace=workspace)


def _change_branch(root: Path) -> Path:
    (root / "cmd/northgate-gateway").mkdir(parents=True)
    (root / "schema").mkdir()
    (root / "go.mod").write_text("module northgate\n\ngo 1.23\n")
    (root / "cmd/northgate-gateway/main.go").write_text("package main\n\nfunc main() {}\n")
    (root / "schema/schema.go").write_text("package schema\n")
    return root


def test_a_review_seat_reads_a_copy_inside_the_runs_sandbox(tmp_path, monkeypatch):
    run_dir = tmp_path / "sandboxes" / "65f8864b"
    (run_dir / "home").mkdir(parents=True)
    checkout = _change_branch(run_dir / "crun_95aca9c7-v5g74pco")
    monkeypatch.setattr(sandbox_identity, "current", lambda: _identity(run_dir))
    seat = _Seat()
    ContractedAgent(seat).run_role("judge the must-haves", checkout, read_only=True)
    assert seat.ran_in != checkout
    seat.ran_in.resolve().relative_to(run_dir.resolve())       # inside the run's directory
    assert seat.saw == ["cmd/northgate-gateway/main.go", "schema/schema.go"]
    assert not any(p.name.startswith("boss-review-copy-") for p in run_dir.iterdir()), \
        "the copy is removed after the session"


def test_a_session_outside_its_sandbox_is_refused_not_sent_home(tmp_path, monkeypatch):
    run_dir = tmp_path / "sandboxes" / "65f8864b"
    (run_dir / "home").mkdir(parents=True)
    elsewhere = _change_branch(tmp_path / "boss-kg-index" / "ghrepo_1c433a0d")
    monkeypatch.setattr(sandbox_identity, "current", lambda: _identity(run_dir))
    seat = _Seat()
    with pytest.raises(ec.EngineSessionFailed, match="outside the run's sandbox directory"):
        ContractedAgent(seat).run_role("plan this", elsewhere, read_only=False)
    assert seat.ran_in == Path("."), "the session never started"


def test_without_a_sandbox_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_identity, "current", lambda: None)
    checkout = _change_branch(tmp_path / "repo")
    seat = _Seat()
    ContractedAgent(seat).run_role("judge", checkout, read_only=True)
    assert seat.saw == ["cmd/northgate-gateway/main.go", "schema/schema.go"]
    assert ec.session_tree_problem(checkout) is None
