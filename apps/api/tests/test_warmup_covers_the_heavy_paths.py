"""A warm-up that skips the heavy queries is cosmetic.

Cold-start eviction (I3) recurred THREE times in one session. The mechanism each time: a
deploy, then the first real query reads a large table off disk, `def` endpoints saturate the
bounded threadpool, /health can no longer be SCHEDULED, Docker marks the container unhealthy,
Traefik evicts it — and the console reports "API unreachable" about an API that is fine.

The first warm-up covered dashboard/code_tasks/events — the paths I happened to be looking at.
The next two evictions came from /v1/jobs and /v1/code-tasks, which were not in it.
/v1/code-tasks alone returns 1.41 MB across the widest table in the schema.
"""

import inspect

from apps.api.app import main


def test_the_warmup_covers_the_tables_the_first_requests_actually_read():
    src = inspect.getsource(main._warm_read_paths)
    for table in ("jobs", "code_task_runs", "deployment_hosts", "meeting_runs", "events"):
        assert table in src, f"{table} is read by a first request and is not warmed"


def test_it_warms_the_widest_read_in_the_system():
    """/v1/code-tasks reads spec/state/finalize_ctx across 200 rows — the 1.41 MB response."""
    src = inspect.getsource(main._warm_read_paths)
    assert "finalize_ctx" in src and "LIMIT 200" in src


def test_warming_runs_off_the_boot_path():
    """Warming must never delay readiness — the container has to answer /health immediately."""
    src = inspect.getsource(main._warm_read_paths)
    assert "threading.Thread" in src
    assert "daemon=True" in src


def test_a_failed_warmup_cannot_break_a_boot():
    src = inspect.getsource(main._warm_read_paths)
    assert src.count("except Exception") >= 2, (
        "an optimisation must never be able to fail a boot")
