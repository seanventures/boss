"""Two runs of one app must never be in flight at once.

Observed: at 19:14 the shepherd autopilot retried a failed build; at 19:16 an operator
restarted the same build by hand. Two full budgeted pipelines for one app, at two different
priorities, neither aware of the other, both queued behind an auto-repair nobody had asked for.

There WAS a guard. `work_fingerprint` hashes the title and instructions verbatim — and every
retry mutates BOTH by design: the title gains "· retry N", the instructions gain an ITERATION
FEEDBACK block. So a retry could never collide with its own original, which is the exact case
the guard exists to catch.
"""

import pytest

from boss_engine.services.code_task_service import active_same_app, work_fingerprint

BASE = dict(project_id="boss-dogfood", owner="paramg", repo="boss-sandbox",
            base_branch="main")
BRIEF = "Build Shorty, a small self-hosted URL shortener."
TITLE = "Shorty — self-hosted URL shortener"


def test_a_retry_has_the_same_identity_as_its_original():
    """The defect exactly: this returned two different fingerprints."""
    original = work_fingerprint(**BASE, title=TITLE, instructions=BRIEF)
    retry = work_fingerprint(
        **BASE, title=f"{TITLE} · retry 2",
        instructions=BRIEF + "\n\n=== ITERATION FEEDBACK (attempt 2) ===\nOOM-killed")
    assert original == retry


def test_operator_steering_does_not_fork_the_identity():
    original = work_fingerprint(**BASE, title=TITLE, instructions=BRIEF)
    steered = work_fingerprint(
        **BASE, title=TITLE,
        instructions=BRIEF + "\n\n=== OPERATOR STEERING (issued while this task was running) "
                             "===\nuse SQLite")
    assert original == steered


@pytest.mark.parametrize("attempt", [1, 2, 7, 12])
def test_any_retry_depth_collapses_to_the_same_work(attempt):
    original = work_fingerprint(**BASE, title=TITLE, instructions=BRIEF)
    assert work_fingerprint(**BASE, title=f"{TITLE} · retry {attempt}",
                            instructions=BRIEF) == original


def test_genuinely_different_work_still_differs():
    """The guard must not become so loose that it suppresses real work."""
    a = work_fingerprint(**BASE, title=TITLE, instructions=BRIEF)
    assert a != work_fingerprint(**BASE, title="HookWatch", instructions="A webhook inspector.")
    assert a != work_fingerprint(**{**BASE, "repo": "other-repo"}, title=TITLE,
                                 instructions=BRIEF)
    assert a != work_fingerprint(**{**BASE, "base_branch": "next"}, title=TITLE,
                                 instructions=BRIEF)


class _Rows:
    def __init__(self, rows): self._rows = rows
    def mappings(self): return self
    def all(self): return self._rows


class _Conn:
    def __init__(self, rows): self.rows = rows
    def execute(self, *_a, **_k): return _Rows(self.rows)


def test_the_same_app_in_flight_is_caught_even_with_a_reworded_brief():
    """The fingerprint is exact by design, so it cannot be the only guard.

    An operator who rephrases one sentence produces a different fingerprint — correctly — and
    would otherwise start a second pipeline for an app already building.
    """
    conn = _Conn([{"id": "crun_first", "title": f"{TITLE} · retry 2"}])
    assert active_same_app(conn, "boss-dogfood", "boss-sandbox", TITLE) == "crun_first"


def test_a_different_app_in_the_same_repo_is_not_blocked():
    conn = _Conn([{"id": "crun_other", "title": "HookWatch — webhook inspector"}])
    assert active_same_app(conn, "boss-dogfood", "boss-sandbox", TITLE) is None


def test_an_unnameable_title_does_not_block_everything():
    """Falling back to a generic name would make one junk run bar every future run."""
    conn = _Conn([{"id": "crun_x", "title": ""}])
    assert active_same_app(conn, "boss-dogfood", "boss-sandbox", "") is None


def test_both_guards_run_before_a_run_is_created():
    import inspect

    from boss_engine.services import code_task_service

    src = inspect.getsource(code_task_service.start_code_task)
    assert "active_duplicate(" in src and "active_same_app(" in src
    assert src.index("active_same_app(") < src.index('make_id("crun")')
