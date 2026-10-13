"""BOSS splits a build's writing stages into parallel sessions itself (parallel_build).

A HelpDesk AI build had the sub-agent tool and a "use parallel sub-agents" rule and made zero
sub-agent calls; its time was output-token bound. So BOSS runs the design's WORK PACKAGES as
separate CLI sessions at the same time — scaffold first, then the parts, then one integration
session — and reverts any write a part makes outside the paths it owns.
"""
from __future__ import annotations

import inspect
import threading
from pathlib import Path

import pytest

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import parallel_build as pb

DESIGN = """=== WORK PACKAGES ===
```json
{"packages": [
  {"name": "api", "owns": ["app/server/**"], "tests": ["app/tests/api/**"], "brief": "routes"},
  {"name": "scaffold", "owns": ["app/package.json", "app/tsconfig.json"],
   "tests": ["app/vitest.config.ts"], "brief": "skeleton"},
  {"name": "ui", "owns": ["app/client/**"], "tests": ["app/tests/ui/**"], "brief": "screens"},
  {"name": "evil", "owns": ["../outside/**", "/etc/**"]}
]}
```"""


def test_the_design_session_asks_for_a_partition_and_keeps_it():
    assert "=== WORK PACKAGES ===" in g._design_prompt("t", "i", ["c"])
    parts = g._split_design("=== STANDARDS ===\ns\n=== SPEC ===\np\n=== PLAN ===\nq\n" + DESIGN)
    assert parts["plan"].strip() == "q"
    assert [p.name for p in pb.parse_packages(parts["packages"])] == ["scaffold", "api", "ui"]


def test_a_partition_never_reaches_outside_the_workspace_and_small_changes_do_not_split():
    names = [p.name for p in pb.parse_packages(DESIGN)]
    assert "evil" not in names                      # ../ and absolute globs are dropped
    assert pb.parse_packages('```json\n{"packages": []}\n```') == []
    assert pb.parse_packages("no json here") == []


def test_ownership_globs():
    assert pb.matches("app/server/routes/a.ts", "app/server/**")
    assert pb.matches("app/server", "app/server/**")
    assert pb.matches("app/package.json", "app/package.json")
    assert pb.matches("app/client/x.tsx", "app/client")
    assert not pb.matches("app/serverless/x.ts", "app/server/**")
    assert pb.matches("app/src/a.test.ts", "app/src/*.test.ts")


def _write(root: Path, rel: str, text: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_parts_run_at_the_same_time_and_a_write_outside_a_part_is_reverted(tmp_path):
    root = tmp_path
    _write(root, "README.md", "original")
    packages = pb.parse_packages(DESIGN)
    parts_seen = threading.Barrier(2, timeout=10)  # both parts must be in flight at once
    calls: list[str] = []

    def run_role(prompt: str) -> dict:
        if "=== FIRST: THE SCAFFOLD ===" in prompt:
            calls.append("scaffold")
            _write(root, "app/package.json", "{}")
            _write(root, "app/client/sneaky.tsx")        # outside the scaffold's paths
            return {"cost_usd_micros": 1}
        if "=== YOUR PART: api ===" in prompt:
            calls.append("api")
            parts_seen.wait()
            _write(root, "app/server/index.ts")
            _write(root, "README.md", "overwritten by a part")   # outside: reverted
            return {"cost_usd_micros": 2}
        if "=== YOUR PART: ui ===" in prompt:
            calls.append("ui")
            parts_seen.wait()
            _write(root, "app/client/App.tsx")
            return {"cost_usd_micros": 3}
        assert "=== INTEGRATE ===" in prompt
        calls.append("integration")
        return {"cost_usd_micros": 4, "summary": "wired", "session_id": "sess-int"}

    out = pb.run_split(run_role=run_role, root=root, base_prompt="BASE", packages=packages,
                       parallel=2, test_cmd="npm test", tests=False)
    assert out is not None
    assert calls[0] == "scaffold" and calls[-1] == "integration"
    assert sorted(calls[1:3]) == ["api", "ui"]                 # both ran (concurrently: barrier)
    assert (root / "README.md").read_text() == "original"      # the rogue write is undone
    assert not (root / "app/client/sneaky.tsx").exists()        # the scaffold's stray file too
    assert (root / "app/server/index.ts").exists() and (root / "app/client/App.tsx").exists()
    reverted = {v["path"] for v in out.violations}
    assert {"README.md", "app/client/sneaky.tsx"} <= reverted
    assert out.cost_usd_micros == 10 and out.session_id == "sess-int" and out.summary == "wired"


def test_a_part_cannot_touch_the_frozen_tests_even_inside_its_own_paths(tmp_path):
    root = tmp_path
    _write(root, "app/server/a.test.ts", "frozen")
    packages = pb.parse_packages(DESIGN)

    def run_role(prompt: str) -> dict:
        if "=== YOUR PART: api ===" in prompt:
            _write(root, "app/server/a.test.ts", "weakened")
            _write(root, "app/server/a.ts", "impl")
        return {}

    out = pb.run_split(run_role=run_role, root=root, base_prompt="B", packages=packages,
                       parallel=2, test_cmd="t", tests=False,
                       protect=lambda rel: rel == "app/server/a.test.ts")
    assert out is not None
    assert (root / "app/server/a.test.ts").read_text() == "frozen"
    assert (root / "app/server/a.ts").exists()


def test_qa_parts_write_only_tests(tmp_path):
    """Even a part whose test globs are broad writes only TEST files (test independence)."""
    root = tmp_path
    packages = [pb.Package("a", (), tests=("app/**",)), pb.Package("b", (), tests=("lib/**",))]

    def run_role(prompt: str) -> dict:
        if "=== YOUR PART: a ===" in prompt:
            _write(root, "app/tests/routes.test.ts")
            _write(root, "app/src/impl.ts")                    # not a test: reverted
        return {}

    out = pb.run_split(run_role=run_role, root=root, base_prompt="B", packages=packages,
                       parallel=2, test_cmd="t", tests=True)
    assert out is not None
    assert (root / "app/tests/routes.test.ts").exists()
    assert not (root / "app/src/impl.ts").exists()


def test_it_falls_back_when_there_is_nothing_to_split_or_every_part_failed(tmp_path):
    packages = pb.parse_packages(DESIGN)
    assert pb.run_split(run_role=lambda p: {}, root=tmp_path, base_prompt="B", packages=packages,
                        parallel=1, test_cmd="t", tests=False) is None
    one = [p for p in packages if p.name in ("scaffold", "api")]
    assert pb.run_split(run_role=lambda p: {}, root=tmp_path, base_prompt="B", packages=one,
                        parallel=3, test_cmd="t", tests=False) is None

    def boom(prompt: str) -> dict:
        if "=== YOUR PART" in prompt:
            raise RuntimeError("cli died")
        return {}

    assert pb.run_split(run_role=boom, root=tmp_path, base_prompt="B", packages=packages,
                        parallel=2, test_cmd="t", tests=False) is None


def test_the_pipeline_runs_both_writing_stages_through_the_split():
    src = inspect.getsource(g)
    assert src.count("_run_split(a, m,") == 2                   # write_tests and implement
    assert "parallel_agents=0" in src                           # a part is ONE agent
    assert 'model_max_concurrency' in inspect.getsource(g._build_fan_out)
