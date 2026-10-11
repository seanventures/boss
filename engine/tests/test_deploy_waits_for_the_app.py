"""Three seconds is not a readiness check.

The operator's Shorty deployment failed twice, ~50 seconds apart, with the same message:

    health check returned 000; rolled back to previous build

`000` is curl's "nothing answered". The deploy did:

    time.sleep(3)
    curl -fsS -o /dev/null -w '%{http_code}' http://localhost:PORT/

One probe, three seconds after `docker run`, no retry. Shorty is a Node service that starts
node, runs migrations, seeds a database and only THEN calls `app.listen`. It could not possibly
have answered, so BOSS rolled back a deployment that was about to work — deterministically,
which is why both attempts failed identically.

The preview path already understood this and waits up to 180s for the port
(`_discover_working_port(timeout=180.0)`). Same requirement, expressed twice, correct once.

Worth recording what was NOT the cause: the agent asserted the app hard-coded
`app.listen(3000, "127.0.0.1")` in `src/server.ts`. That file does not exist, and the real
server reads `process.env.PORT` / `process.env.HOST` and defaults to `0.0.0.0` — exactly what
BOSS requires. Acting on that diagnosis would have "fixed" correct code.
"""

import inspect

from boss_engine.services import deploy_service as ds


def test_there_is_a_readiness_budget_at_all():
    assert ds.DEPLOY_HEALTH_TIMEOUT_SECONDS >= 60, (
        "a container that runs migrations and seeds a database needs more than a moment")


def test_the_budget_is_not_unbounded():
    """A deploy that hangs forever on a broken app is its own outage."""
    assert ds.DEPLOY_HEALTH_TIMEOUT_SECONDS <= 600


def test_it_polls_rather_than_sleeping_once():
    src = inspect.getsource(ds._await_healthy)
    assert "while True" in src
    assert "deadline" in src


def test_a_ready_app_is_not_made_to_wait():
    """Succeed on the first 2xx: only a slow or broken container should use the full budget."""
    src = inspect.getsource(ds._await_healthy)
    assert 'if code.startswith("2"):' in src
    assert "break" in src


def test_the_old_single_shot_sleep_is_gone():
    src = inspect.getsource(ds)
    assert "time.sleep(3)\n        hc = _ssh(" not in src


def test_the_health_gate_uses_the_waiter():
    src = inspect.getsource(ds)
    assert "_await_healthy(key_path, host, user, ssh_port, int(app_port))" in src


def test_nothing_listening_is_reported_differently_from_unhealthy():
    """`000` and `500` are different faults and need different fixes: one never bound the port,
    the other started and is broken. 'health check returned 000' said neither."""
    src = inspect.getsource(ds)
    assert 'never_answered = code in ("", "000")' in src
    assert "never bound it" in src
    assert "started and is\n                           \"unhealthy rather than absent." in src or \
           "unhealthy rather than absent" in src


def test_the_failure_says_how_long_it_waited():
    """'health check returned 000' with no duration hides whether it waited 3 seconds or 3
    minutes — which is the entire difference between this bug and a genuinely broken app."""
    src = inspect.getsource(ds)
    assert "after waiting " in src
    assert "{waited}s" in src


def test_the_preview_path_still_waits_too():
    """The twin. If either drifts back to a fixed sleep, this whole class of failure returns."""
    from boss_engine.services import preview_service as ps
    src = inspect.getsource(ps)
    # 180s, or more for a Java service that builds its jar first (preview_start_timeout).
    assert "def preview_start_timeout(workdir: Optional[Path], default: float = 180.0)" in src
    assert "timeout=preview_start_timeout(workdir)" in src
