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
# ...on a Mac, behind Docker Desktop. Restart advice is for THAT platform only; a Linux server
# is never told to restart (see test_a_healthy_server_is_never_told_it_is_out_of_memory.py).
LAPTOP = {"scope": "vm", "bands": {}, "reasons": []}


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
    assert "estart" in operator_advice(REAL, elapsed_minutes=242, working_minutes=63,
                                       gauges=LAPTOP)["action"]


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


# --------------------------------------------------------------------------------------- #
# Pressure bands: yellow at 85%, red at 95%.
# --------------------------------------------------------------------------------------- #

from boss_engine.services.self_observability import (  # noqa: E402
    PRESSURE_CRITICAL, PRESSURE_WARN, pressure,
)

_HEALTHY_CPU = {"load_per_core": 0.4}
_HEALTHY_DISK = {"used_pct": 40}


def _mem(total_mb: int, available_mb: int, swap_mb: float = 0.0) -> dict:
    return {"host_total_mb": total_mb, "host_available_mb": available_mb,
            "host_swap_used_mb": swap_mb}


def test_a_comfortable_machine_is_ok():
    assert pressure(_mem(16000, 9000), _HEALTHY_CPU, _HEALTHY_DISK)["state"] == "ok"


def test_eighty_five_percent_memory_is_the_yellow_line():
    """The line is inclusive: AT 85% it warns, just under it does not."""
    assert pressure(_mem(16000, 2560), _HEALTHY_CPU, _HEALTHY_DISK)["state"] == "ok"      # 84%
    assert pressure(_mem(16000, 2400), _HEALTHY_CPU, _HEALTHY_DISK)["state"] == "warning"  # 85%


def test_ninety_five_percent_memory_is_the_red_line():
    assert pressure(_mem(16000, 800), _HEALTHY_CPU, _HEALTHY_DISK)["state"] == "critical"


def test_cpu_is_load_per_core_not_a_utilisation_percent():
    """Load 12 on 12 cores is saturated; the same load on 48 cores is idle."""
    assert pressure(_mem(16000, 9000), {"load_per_core": 0.9}, _HEALTHY_DISK)["state"] == "warning"
    assert pressure(_mem(16000, 9000), {"load_per_core": 1.4}, _HEALTHY_DISK)["state"] == "critical"


def test_a_full_disk_raises_the_alarm_too():
    assert pressure(_mem(16000, 9000), _HEALTHY_CPU, {"used_pct": 96})["state"] == "critical"


def test_the_worst_gauge_decides():
    p = pressure(_mem(16000, 9000), _HEALTHY_CPU, {"used_pct": 97})
    assert p["state"] == "critical" and p["bands"]["memory"]["band"] == "ok"


def test_every_failing_gauge_is_named():
    p = pressure(_mem(16000, 300, swap_mb=15000), {"load_per_core": 1.2}, {"used_pct": 97})
    joined = ", ".join(p["reasons"])
    for expected in ("memory", "swapping", "cpu", "disk"):
        assert expected in joined


def test_missing_readings_degrade_to_ok_rather_than_guessing():
    """macOS has no /proc: absent numbers must not invent a verdict."""
    assert pressure({}, {}, {})["state"] == "ok"


def test_the_thresholds_are_the_ones_that_were_asked_for():
    assert (PRESSURE_WARN, PRESSURE_CRITICAL) == (0.85, 0.95)
