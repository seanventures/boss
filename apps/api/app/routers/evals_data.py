"""Feedback on runs, datasets, the annotation inbox, experiments and the bench claim (AP-P4).

AGENT_PLATFORM §4.9 over HTTP (engine/boss_engine/services/evals.py, bench_results.py):

  POST/GET /v1/runs/{run_id}/feedback          a person's verdict on a run (or a span of it)
  GET/POST /v1/datasets                         datasets of a project; make one
  GET      /v1/datasets/{dataset_id}            one dataset with its examples
  POST     /v1/datasets/{dataset_id}/examples   an example by hand (confirmed)
  POST     /v1/datasets/import-battle           tests/agent-battle/battle.yaml as a dataset
  POST     /v1/spans/{span_id}/add-to-dataset   "Add to eval set" on a span (console A6)
  GET      /v1/eval-inbox                       PROPOSED examples (misses) waiting for a person
  POST     /v1/dataset-examples/{id}/review     confirm (optionally correcting the reference)
                                                or reject one
  POST/GET /v1/datasets/{dataset_id}/experiments  run the scripted battle over the dataset /
                                                list its experiments
  GET      /v1/dataset-experiments/{id}         one experiment with every result (and run id)
  GET      /v1/bench/governed-vs-bare           governed vs bare for one engine, noise bands,
                                                from RECORDED runs only

`/v1/feedback` and `/v1/experiments` already name person feedback (one-tap ratings) and the A/B
knob experiments; these paths sit beside them. Reads need `observability:read`; a person's
verdict on a run `feedback:submit` (as a one-tap rating does); datasets, the inbox and
experiments `observability:manage`, at the project. A real-engine experiment is refused here: real-money
battle runs are frozen, and an API request never starts paid work by accident.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class _Write(BaseModel):
    project_id: str
    actor: Optional[str] = None


class FeedbackBody(_Write):
    key: str = Field(min_length=1, max_length=200)
    span_id: Optional[str] = None
    score: Optional[float] = None
    value: Optional[Any] = None
    comment: Optional[str] = Field(default=None, max_length=4000)


class DatasetBody(_Write):
    name: str = Field(min_length=1, max_length=120)
    kind: str = "custom"
    description: str = Field(default="", max_length=2000)
    agent_key: Optional[str] = None


class ExampleBody(_Write):
    input: Any = None
    reference: Any = None
    split: str = "test"


class AddFromSpanBody(_Write):
    dataset_id: Optional[str] = None
    dataset_name: Optional[str] = None
    reference: Any = None
    split: str = "test"


class ReviewBody(_Write):
    verdict: str = Field(pattern="^(confirm|reject)$")
    note: str = Field(default="", max_length=2000)
    reference: Any = None


class ExperimentBody(_Write):
    mode: str = "scripted"
    agent_key: Optional[str] = None
    spec_version: Optional[int] = None
    #: Scripted mode only: {example_id: the answer the fixed agent gives}.
    fixes: Optional[dict[str, Any]] = None


def build_evals_router(*, engine, require_write_actor: Callable[..., str],
                       require_reader: Callable[..., None]) -> APIRouter:
    from boss_engine.services import bench_results, evals

    router = APIRouter(tags=["evaluation"])

    def _fail(error: Exception):
        if isinstance(error, evals.EvalError):
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        raise error

    def _writer(connection, body: _Write) -> str:
        return require_write_actor(connection, body.project_id, body.actor, "role_review")

    # ── feedback ──────────────────────────────────────────────────────────────────────────
    @router.post("/v1/runs/{run_id}/feedback", status_code=201)
    @permit(P.FEEDBACK_SUBMIT, scope=Scope.project(body="project_id"))
    def post_feedback(run_id: str, body: FeedbackBody) -> dict[str, Any]:
        """A person's verdict on a run (or one span of it): kept, and the run's payloads are
        kept for the extended retention."""
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return evals.record_feedback(
                    connection, body.project_id, key=body.key, source="human",
                    created_by=actor, run_id=run_id, span_id=body.span_id, score=body.score,
                    value=body.value, comment=body.comment)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/runs/{run_id}/feedback")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def get_feedback(run_id: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"run_id": run_id,
                    "items": evals.list_feedback(connection, project_id, run_id=run_id)}

    # ── datasets ──────────────────────────────────────────────────────────────────────────
    @router.get("/v1/datasets")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def list_datasets(project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"items": evals.list_datasets(connection, project_id)}

    @router.post("/v1/datasets", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def create_dataset(body: DatasetBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return evals.create_dataset(connection, body.project_id, name=body.name,
                                            created_by=actor, kind=body.kind,
                                            description=body.description,
                                            agent_key=body.agent_key)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/datasets/import-battle", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def import_battle(body: _Write) -> dict[str, Any]:
        """The agent battle-test set as the `agent-battle` dataset (idempotent)."""
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return evals.import_battle_yaml(connection, body.project_id, actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.get("/v1/datasets/{dataset_id}")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def get_dataset(dataset_id: str, project_id: str,
                    status: Optional[str] = None) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            found = evals.get_dataset(connection, project_id, dataset_id)
            if found is None:
                raise HTTPException(status_code=404, detail="Dataset not found.")
            found["items"] = evals.list_examples(connection, project_id, dataset_id,
                                                 status=status)
            return found

    @router.post("/v1/datasets/{dataset_id}/examples", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def add_example(dataset_id: str, body: ExampleBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return evals.add_example(connection, body.project_id, dataset_id,
                                         source="manual", proposed_by=actor, input=body.input,
                                         reference=body.reference, split=body.split)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    @router.post("/v1/spans/{span_id}/add-to-dataset", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def add_from_span(span_id: str, body: AddFromSpanBody) -> dict[str, Any]:
        """"Add to eval set": the span's redacted input and output become a confirmed example
        (a dataset named here is made when it does not exist)."""
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return evals.add_from_span(connection, body.project_id, span_id, actor=actor,
                                           dataset_id=body.dataset_id,
                                           dataset_name=body.dataset_name,
                                           reference=body.reference, split=body.split)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    # ── the annotation inbox ──────────────────────────────────────────────────────────────
    @router.get("/v1/eval-inbox")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def eval_inbox(project_id: str, status: str = "proposed") -> dict[str, Any]:
        """Misses BOSS captured (a waived gate, a delivery sent back), waiting for a person."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"status": status, "items": evals.inbox(connection, project_id, status=status)}

    @router.post("/v1/dataset-examples/{example_id}/review")
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def review_example(example_id: str, body: ReviewBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = _writer(connection, body)
            try:
                return evals.review_example(connection, body.project_id, example_id,
                                            verdict=body.verdict, actor=actor, note=body.note,
                                            reference=body.reference)
            except Exception as error:  # noqa: BLE001
                _fail(error)

    # ── experiments ───────────────────────────────────────────────────────────────────────
    @router.post("/v1/datasets/{dataset_id}/experiments", status_code=201)
    @permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
    def run_experiment(dataset_id: str, body: ExperimentBody) -> dict[str, Any]:
        """A battle run over the dataset's confirmed examples, recorded as an experiment (the
        scripted engine: no model spend)."""
        if body.mode != "scripted":
            raise HTTPException(status_code=422, detail=(
                "Only scripted experiments run from the API: a real-engine battle spends money "
                "(frozen for now) and is started by the battle runner, not a request."))
        with begin(engine) as connection:
            actor = _writer(connection, body)
            found = evals.get_dataset(connection, body.project_id, dataset_id)
        if found is None:
            raise HTTPException(status_code=404, detail="Dataset not found.")
        try:
            return evals.run_battle(engine, body.project_id, dataset=dataset_id, mode="scripted",
                                    fixes=body.fixes, agent_key=body.agent_key,
                                    spec_version=body.spec_version, created_by=actor,
                                    source="api")
        except Exception as error:  # noqa: BLE001
            _fail(error)

    @router.get("/v1/datasets/{dataset_id}/experiments")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def list_experiments(dataset_id: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return {"items": evals.list_experiments(connection, project_id,
                                                    dataset_id=dataset_id)}

    @router.get("/v1/dataset-experiments/{experiment_id}")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def get_experiment(experiment_id: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            found = evals.get_experiment(connection, project_id, experiment_id, results=True)
        if found is None:
            raise HTTPException(status_code=404, detail="Experiment not found.")
        return found

    # ── the bench claim ───────────────────────────────────────────────────────────────────
    @router.get("/v1/bench/governed-vs-bare")
    @permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
    def governed_vs_bare(project_id: str, engine_name: Optional[str] = None) -> dict[str, Any]:
        """BOSS's governed pipeline against the engine's bare loop, every metric with its noise
        band, from RECORDED runs (never a new one). Without `engine_name`: the engines recorded
        at both levels."""
        with begin(engine) as connection:
            require_reader(connection, project_id)
            if not engine_name:
                return {"engines": bench_results.engines()}
            got = bench_results.governed_vs_bare(engine_name, connection=connection)
        if got is None:
            raise HTTPException(status_code=404, detail=(
                f"{engine_name} has not been recorded both governed and bare."))
        return got

    return router


__all__ = ["build_evals_router"]
