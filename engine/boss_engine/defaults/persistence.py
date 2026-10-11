from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from typing import Any, Iterator, Optional


class InMemoryPersistence:
    """Process-local OSS persistence for code-task state and lineage."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    @contextmanager
    def transaction(self, engine: Any = None) -> Iterator["InMemoryPersistence"]:
        del engine
        with self._lock:
            yield self

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            run = self._runs.get(run_id)
            return deepcopy(run) if run is not None else None

    def list_runs(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            matching = [
                deepcopy(run)
                for run in reversed(list(self._runs.values()))
                if run.get("project_id") == project_id
            ]
        return matching[: int(limit)]

    def save_run(self, run_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            current = self._runs.setdefault(run_id, {"id": run_id})
            current.update(deepcopy(state))

    def first_code_task_job(self, run_id: str) -> Optional[dict[str, Any]]:
        """This run-only adapter stores no job authority to carry to a retry.

        Run metadata is not a delegated grant. The caller must still perform its
        normal consent check when no original job authority is available.
        """
        del run_id
        return None

    def execute(self, statement: Any, parameters: Any = None) -> Any:
        del statement, parameters
        raise RuntimeError(
            "raw SQL is unavailable in the OSS in-memory persistence adapter"
        )
