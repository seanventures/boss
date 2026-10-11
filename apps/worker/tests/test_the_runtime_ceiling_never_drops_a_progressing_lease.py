"""The worker's runtime ceiling stops renewing HUNG work, never work that is progressing.

crun_1839849d: a build whose implement turn may run 5400s outlived the fixed 5400s job ceiling
while it was streaming agent turns. The worker stopped renewing, the job was reclaimed, and the
redelivery's cleanup SIGTERMed the live agent. The control plane now says, on every heartbeat,
whether a code task's run is progressing, and the ceiling yields to it.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boss_worker import runner  # noqa: E402


def test_only_an_explicit_progress_report_counts():
    assert runner.job_progressing({"progressing": True}) is True
    assert runner.job_progressing({"progressing": False}) is False
    assert runner.job_progressing({"progressing": None}) is False
    assert runner.job_progressing({}) is False          # an older control plane
    assert runner.job_progressing(None) is False


def test_the_ceiling_yields_to_progress_and_still_bounds_hung_work():
    body = inspect.getsource(runner)
    assert "progressing = job_progressing(beat)" in body
    assert "elapsed > max_runtime and progressing" in body
    # The hung-work branch is still there, after the progressing one.
    assert body.index("lease_renewal_past_ceiling_progressing") \
        < body.index("lease_renewal_stopped_runtime_exceeded")
