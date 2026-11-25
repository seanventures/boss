"""THE JOURNEYS GATE: the golden journeys' runner, proven on BOSS's real API with no model.

    python3 tests/e2e/gate/journeys.py --out results.json      (needs BOSS_E2E_GATE_PG_URL)
    scripts/e2e-gate.sh journeys                               (the same, in a throwaway container)

The daily runner (engine/boss_engine/services/golden_journeys) plays each journey against an
install's API and holds the outcome to the Acceptance Agent's verdict. This gate plays the
journeys that can run on the scripted conformance engine (`"modes": [..., "scripted"]`) against
BOSS's real API app, in process, on the engines gate's world (a throwaway Postgres with the full
schema and a local GitHub). There is no worker here, so the journey's `drain` step runs the
queued build on the scripted engine, as engines.py does.

Each journey must come out GREEN: every acceptance item MET on evidence the verdict module
verified against the record of calls. A red journey fails the gate with the verdict's words.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engines  # noqa: E402  (the world: Postgres, local GitHub, the scripted engine)
from engines import FIXTURE, OWNER, CheckFailed, World, check  # noqa: E402

ACTOR = "ceo"


class GateClient:
    """BOSS's API app in this process; `drain` runs the queued builds on the scripted engine."""

    mode = "scripted"

    def __init__(self, world: World, faults: str = ""):
        from fastapi.testclient import TestClient

        from apps.api.app import main

        self.world = world
        self.faults = faults
        self.http = TestClient(main.app, raise_server_exceptions=False)
        self.ran: set[str] = set()

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        r = self.http.request(method, path, json=body)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    def drain(self) -> str:
        from boss_engine.config import get_settings
        from boss_engine.services import code_task_service, scripted_engine
        from boss_engine.services.jobs import JobLeaseService
        from boss_engine.services.llm_telemetry import clear_llm_context

        t = self.world.text
        with self.world.engine.begin() as c:
            queued = [r for r in c.execute(t(
                "SELECT payload->>'run_id' AS run_id, payload FROM jobs WHERE project_id = :p "
                "AND kind = 'code.task' AND status = 'queued' ORDER BY created_at"),
                {"p": self.world.project}).mappings() if r["run_id"] not in self.ran]
            repos = {r["id"]: (r["owner"], r["repo"]) for r in c.execute(t(
                "SELECT id, spec->>'owner' AS owner, spec->>'repo' AS repo FROM code_task_runs "
                "WHERE project_id = :p"),
                {"p": self.world.project}).mappings()}
        done = []
        for row in queued:
            run_id = row["run_id"]
            self.ran.add(run_id)
            owner, repo = repos.get(run_id, (OWNER, ""))
            os.environ.update({"BOSS_SCRIPTED_ENGINE": "1", "BOSS_SCRIPTED_FAULTS": self.faults,
                               "BOSS_SCRIPTED_CAPS": "", "BOSS_SCRIPTED_FIXTURE": str(FIXTURE),
                               "BOSS_SCRIPTED_APP_DIR": ""})
            scripted_engine.reset()
            settings = get_settings().model_copy(update={
                "enable_code_task": True, "governed_coding_pipeline": True,
                "github_token": "gate-local-token", "code_agent_engine": "scripted",
                "code_review_engine": "scripted", "target_github_owner": owner,
                "target_github_repo": repo, "quality_bar": "standard"})
            try:
                code_task_service.run_code_task(self.world.engine, settings,
                                                JobLeaseService(settings), run_id)
            except Exception:  # noqa: BLE001 — the run's own record says what happened
                traceback.print_exc()
            finally:
                clear_llm_context()
            done.append(run_id)
        return f"ran {len(done)} queued build(s) on the scripted engine: {', '.join(done) or '-'}"


def seed_world(world: World) -> None:
    """The project's members (the API checks the actor is one), as an install seeds them."""
    from boss_engine.config import get_settings
    from boss_engine.services.members import MemberDirectoryService

    with world.engine.begin() as c:
        MemberDirectoryService(get_settings()).seed_default_members(c, world.project, "gate")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--journey", action="append", default=[])
    args = ap.parse_args()
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="boss-e2egate-journeys-"))
    world = None
    try:
        world = World(os.environ["BOSS_E2E_GATE_PG_URL"], work)
        seed_world(world)
        from boss_engine.services.golden_journeys import definitions, runner

        journeys = [j for j in definitions.load_all() if j.runnable_in("scripted")]
        if args.journey:
            journeys = [j for j in journeys if j.id in args.journey]
        if not journeys:
            raise CheckFailed("no journey is runnable on the scripted engine")
        for j in journeys:
            @check(f"{j.id} {j.title}")
            def one(j=j) -> str:
                repo = f"journey-{j.id.lower()}-{uuid.uuid4().hex[:6]}"
                world.seed(repo)
                scripted = (j.needs.get("scripted") or {})
                client = GateClient(world, faults=str(scripted.get("faults") or ""))
                result = runner.run_journey(j, client, variables={
                    "project_id": world.project, "owner": OWNER, "repo": repo,
                    "base_branch": "main", "actor": ACTOR, "model_config": {"default": "scripted"},
                    **(scripted.get("vars") or {})}, sleep=lambda s: time.sleep(min(s, 0.5)))
                (work / f"{j.id}.json").write_text(json.dumps(result, indent=1, default=str))
                if result["status"] != runner.GREEN:
                    raise CheckFailed(f"{result['status']}: {result['why']}")
                return f"green: {len(result['items'])} items MET, verdict {result['verdict']}"

            one()
            engines.RESULTS[-1]["bug"] = "1284"
    except Exception as exc:  # noqa: BLE001
        engines.RESULTS.append({"check": "gate set-up", "bug": "", "status": "FAIL",
                                "seconds": 0, "detail": f"{type(exc).__name__}: {exc}"[:500]})
        traceback.print_exc()
    finally:
        if world is not None:
            world.close()
        keep = Path(args.out).parent / "journeys"
        with contextlib.suppress(Exception):
            keep.mkdir(parents=True, exist_ok=True)
            for f in work.glob("*.json"):
                (keep / f.name).write_text(f.read_text())
    Path(args.out).write_text(json.dumps({"results": engines.RESULTS,
                                          "seconds": round(time.monotonic() - started, 1)},
                                         indent=1))
    return 1 if any(r["status"] != "PASS" for r in engines.RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
