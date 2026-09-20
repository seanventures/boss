"""When the machine is the problem, say so to the person who can fix it.

"I got the system issue from you but there will be no one to give that info to customer."

Two runs were read as BOSS degrading while the host was thrashing, and both diagnoses came from
a person reading container logs by hand. A customer has nobody to do that. The verdict has to
arrive as an instruction, in words that do not assume they know what swap is.
"""

from __future__ import annotations

from boss_engine.services.self_observability import operator_advice

# The real run: 242 minutes elapsed, 63 of them model work.
REAL = {"state": "critical", "stalled_seconds": 10740.0, "share_of_elapsed": 0.74}


def test_a_healthy_machine_says_nothing_at_all():
    """An indicator that is always on is an indicator nobody reads."""
    assert operator_advice({"state": "ok"}) == {"show": False}


def test_a_starved_machine_is_surfaced():
    assert operator_advice(REAL, elapsed_minutes=242, working_minutes=63)["show"] is True


def test_it_names_the_computer_not_the_product():
    """Read as 'BOSS is slow', the customer files a bug. Read as 'this machine is out of
    memory', they fix it in five minutes."""
    advice = operator_advice(REAL, elapsed_minutes=242, working_minutes=63)
    assert "computer" in advice["headline"].lower()
    assert advice["not_a_boss_fault"] is True


def test_it_tells_them_what_to_do():
    assert "estart" in operator_advice(REAL, elapsed_minutes=242, working_minutes=63)["action"]


def test_it_gives_the_split_they_can_check_themselves():
    detail = operator_advice(REAL, elapsed_minutes=242, working_minutes=63)["detail"]
    assert "242" in detail and "63" in detail


def test_a_mild_stall_warns_rather_than_demanding_a_restart():
    advice = operator_advice({"state": "degraded", "stalled_seconds": 90.0,
                              "share_of_elapsed": 0.02})
    assert advice["severity"] == "warning"
    assert "struggling" in advice["headline"]


def test_a_large_share_is_critical_even_when_the_verdict_was_only_degraded():
    """Half a run lost to the machine is not a 'warning' whatever the state string says."""
    advice = operator_advice({"state": "degraded", "stalled_seconds": 3600.0,
                              "share_of_elapsed": 0.6})
    assert advice["severity"] == "critical"


def test_it_works_without_the_per_run_numbers():
    advice = operator_advice({"state": "critical", "stalled_seconds": 600.0})
    assert advice["show"] is True and "10 minutes" in advice["detail"]
