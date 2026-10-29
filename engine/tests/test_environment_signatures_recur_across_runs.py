"""L1 follow-up (acceptance criteria 7-8): an `environment`-classified failure's signature is
counted per project across runs, and a recurring signature (seen twice or more) is raised as
its own event — a platform gap, not noise buried inside each run's own baseline record.
"""
import os
import unittest
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import baseline_verify, governed_code_task_service as svc

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")


def _env_record(test_id: str, evidence: str) -> baseline_verify.BaselineRecord:
    return baseline_verify.BaselineRecord(
        environment=(baseline_verify.Classified(
            test_id=test_id, klass="environment", reason="ModuleNotFoundError",
            evidence=evidence),))


class TestEnvironmentSignature(unittest.TestCase):
    def test_signature_is_the_signal_not_the_test_id(self):
        a = _env_record("tests/x.py::test_a",
                        "ModuleNotFoundError: No module named 'driver'").environment[0]
        b = _env_record("tests/y.py::test_b",
                        "ModuleNotFoundError: No module named 'driver'").environment[0]
        self.assertEqual(baseline_verify.environment_signature(a),
                         baseline_verify.environment_signature(b))


@needs_db
class TestEnvironmentSignatureRecursAcrossRuns(unittest.TestCase):
    def setUp(self):
        from ._obs_seed import pg_engine

        self.engine = pg_engine()
        self._orig_transaction = svc._transaction
        svc._transaction = lambda e: e.begin()
        self.project_id = "p_envsig_" + uuid.uuid4().hex[:8]

    def tearDown(self):
        svc._transaction = self._orig_transaction
        with self.engine.begin() as c:
            c.execute(text(
                "DELETE FROM code_task_environment_signatures WHERE project_id = :p"),
                {"p": self.project_id})

    def test_second_occurrence_raises_the_recurring_event(self):
        record = _env_record("tests/x.py::test_a",
                             "ModuleNotFoundError: No module named 'driver'")

        events = []

        def fake_emit(connection, project_id, event_type, actor, entity_type, entity_id, data):
            if event_type == "code_task.environment_signature_recurring":
                events.append(data)
            else:
                orig_emit(connection, project_id, event_type, actor, entity_type, entity_id, data)

        orig_emit = svc.emit_event
        svc.emit_event = fake_emit
        try:
            svc._record_environment_signatures(self.engine, self.project_id, "crun_1", record)
            self.assertEqual(events, [])  # first sighting: not yet "recurring"
            svc._record_environment_signatures(self.engine, self.project_id, "crun_2", record)
        finally:
            svc.emit_event = orig_emit

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["seen_count"], 2)

        with self.engine.begin() as c:
            count = c.execute(text(
                "SELECT seen_count FROM code_task_environment_signatures "
                "WHERE project_id = :p"), {"p": self.project_id}).scalar()
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
