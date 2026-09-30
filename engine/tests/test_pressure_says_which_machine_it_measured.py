"""A green memory band must not be read as "my machine is fine".

Measured on the operator's Mac while a workflow ran, with the Mac's swap at 23,708 MB of
24,576 used -- 96.5%, minutes from unusable:

    BOSS reported   memory 42.4% ok    swap 2.0 MB ok    state: ok

Both numbers were correct about the Docker Desktop VM and said nothing at all about the Mac.
The operator had asked for exactly these thresholds -- "yellow warning if the system is
running really low on mem and cpu like 85% and system red warning if it above 95%" -- and on
a Mac or a Windows box they can never fire, because /proc/meminfo behind Docker Desktop
belongs to a VM with its own 8 GB.

The thresholds were never wrong. The reading did not say what it had measured.
"""

from __future__ import annotations

from boss_engine.services.self_observability import (
    measures_the_real_machine,
    pressure,
)

HEALTHY_CPU = {"load_per_core": 0.2}
HEALTHY_DISK = {"used_pct": 17.0}

#: The exact reading taken from the container while the Mac was at 96.5% swap.
THE_MISLEADING_READING = {
    "host_total_mb": 7836.4,
    "host_available_mb": 4833.5,
    "host_swap_used_mb": 2.0,
    "host_is_the_machine": False,
}


def test_a_vm_reading_says_it_is_a_vm():
    result = pressure(THE_MISLEADING_READING, HEALTHY_CPU, HEALTHY_DISK)
    assert result["scope"] == "vm"


def test_a_vm_reading_explains_itself_in_the_operators_terms():
    """The note has to be usable by someone who did not build this: it must say that a green
    band here is not a statement about their computer."""
    note = pressure(THE_MISLEADING_READING, HEALTHY_CPU, HEALTHY_DISK)["scope_note"]
    assert "not this computer" in note
    assert "Docker Desktop" in note


def test_a_server_reading_is_not_hedged():
    """On a Linux server /proc/meminfo IS the host. Those installs -- reference host, any Hetzner
    box -- must keep an unqualified reading, or the hedge becomes noise everywhere."""
    server = dict(THE_MISLEADING_READING, host_is_the_machine=True)
    result = pressure(server, HEALTHY_CPU, HEALTHY_DISK)
    assert result["scope"] == "machine"
    assert "scope_note" not in result


def test_an_older_snapshot_without_the_flag_is_treated_as_a_machine():
    """Snapshots are persisted and read back. One taken before this flag existed must not
    suddenly be reported as a VM."""
    legacy = {k: v for k, v in THE_MISLEADING_READING.items() if k != "host_is_the_machine"}
    assert pressure(legacy, HEALTHY_CPU, HEALTHY_DISK)["scope"] == "machine"


def test_the_bands_themselves_are_unchanged():
    """This changes what the reading SAYS ABOUT ITSELF, not where the thresholds sit. 85/95
    were asked for by name and must not drift."""
    from boss_engine.services.self_observability import PRESSURE_CRITICAL, PRESSURE_WARN

    assert PRESSURE_WARN == 0.85
    assert PRESSURE_CRITICAL == 0.95

    strained = {"host_total_mb": 1000.0, "host_available_mb": 100.0,
                "host_is_the_machine": True}
    result = pressure(strained, HEALTHY_CPU, HEALTHY_DISK)
    assert result["bands"]["memory"]["band"] == "warning"
    assert result["state"] == "warning"


def test_the_detector_answers_for_this_machine_without_raising():
    """It reads /proc/version, which does not exist on macOS outside a container. It must
    still return a bool rather than throwing inside a monitoring path."""
    assert isinstance(measures_the_real_machine(), bool)
