"""A healthy Linux server must never be told it is out of memory, or to restart.

Live on boss-1, a 16 GB Ubuntu VM running BOSS in docker, every run card read:

    This computer is out of memory — BOSS is waiting on it. Restart the computer before the
    next run. Closing other applications helps, but a restart is what reliably clears it.

At that moment `free -m` said: total 15991, used 2211, free 834, buff/cache 13345,
available 13780. Containers held under 1.5 GB. Load 0.4.

The cause: the headline was one fixed string for EVERY severe verdict — a stall of BOSS's own
heartbeat, a full disk, a busy CPU — and memory was never consulted at all. The design now:
memory is only claimed when memory that cannot be reclaimed is short (MemAvailable; cgroup
anon+unevictable against a real limit), or under current pressure (PSI, OOM kills SINCE THE
LAST LOOK) — never page cache, never a cumulative counter. And "restart the computer" is
advice for a laptop's Docker VM, never for a Linux server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services import run_retrospective, self_observability as so

KB = 1024
MB = 1024 * 1024


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def boss_1(tmp_path, monkeypatch):
    """boss-1 exactly as measured: 15991 MB total, 834 free, 13780 available, a cgroup with no
    memory limit holding ~1.4 GB of which most is cache, and an OOM kill from long ago."""
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    _write(proc, "meminfo",
           f"MemTotal: {15991 * KB} kB\nMemFree: {834 * KB} kB\n"
           f"MemAvailable: {13780 * KB} kB\nBuffers: 0 kB\nCached: {13345 * KB} kB\n"
           f"SwapTotal: 0 kB\nSwapFree: 0 kB\n")
    _write(proc, "version", "Linux version 6.8.0-45-generic (buildd@lcy02) #45-Ubuntu SMP\n")
    _write(proc, "loadavg", "0.40 0.35 0.30 2/612 12345\n")
    _write(proc, "pressure/memory",
           "some avg10=0.00 avg60=0.00 avg300=0.00 total=1234\n"
           "full avg10=0.00 avg60=0.00 avg300=0.00 total=567\n")
    _write(cg, "memory.current", str(1400 * MB))
    _write(cg, "memory.max", "max")
    _write(cg, "memory.stat", f"anon {300 * MB}\nfile {1100 * MB}\nunevictable 0\n")
    _write(cg, "memory.events", "low 0\nhigh 0\nmax 0\noom 3\noom_kill 2\n")
    monkeypatch.setattr(so, "PROC", proc)
    monkeypatch.setattr(so, "CGROUP_V2", cg)
    monkeypatch.setattr(so, "_OOM_SEEN", {})
    monkeypatch.setattr(so, "disk_snapshot", lambda *_a, **_k: {"used_pct": 40.0})
    monkeypatch.setattr(so.os, "cpu_count", lambda: 4)
    return proc, cg


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Connection:
    """Stands in for the events table: the host.stalled rows of the last hour."""

    def __init__(self, stalls):
        self._rows = [{"data": {"stalled_seconds": s}} for s in stalls]

    def execute(self, *_a, **_k):
        return _Rows(self._rows)


def _advice(stalls=()):
    return run_retrospective.host_advice(_Connection(list(stalls)), "p1")


# ----------------------------------------------------------------- the reported host state


def test_boss_1_as_measured_shows_no_banner_at_all(boss_1):
    assert _advice() == {"show": False}


def test_page_cache_is_not_counted_as_used_memory(boss_1):
    reading = so.pressure_now()
    assert reading["bands"]["memory"]["band"] == "ok"
    assert reading["bands"]["memory"]["used_pct"] < 20  # 1 - 13780/15991 = 13.8%
    assert reading["memory_exhausted"] is False


def test_a_cgroup_with_no_limit_has_no_used_percentage(boss_1):
    memory = so.memory_snapshot()
    assert "limit_mb" not in memory and "used_pct" not in memory
    assert memory["unreclaimable_mb"] == 300.0


def test_a_historical_oom_count_is_not_an_alarm_today(boss_1):
    assert so.memory_snapshot()["oom_kills"] == 2
    for _ in range(3):  # however often the console polls, an unchanged count stays quiet
        assert so.pressure_now()["state"] == "ok"


def test_stalls_on_a_server_with_headroom_never_claim_memory_or_say_restart(boss_1):
    """Whatever held BOSS's heartbeat up on boss-1, the server measured 86% free."""
    advice = _advice(stalls=[400.0, 600.0, 300.0])  # critical by the stall rule alone
    assert advice["show"] is True
    assert "memory" not in advice["headline"].lower()
    assert "restart" not in advice["action"].lower()
    assert advice["cause"] == "boss_process"


def test_a_full_disk_on_a_server_says_disk_not_memory(boss_1, monkeypatch):
    monkeypatch.setattr(so, "disk_snapshot", lambda *_a, **_k: {"used_pct": 97.0})
    advice = _advice()
    assert "disk" in advice["headline"] and "memory" not in advice["headline"]
    assert "restart" not in advice["action"].lower()


# ----------------------------------------------------------------- real pressure still fires


def test_a_server_with_almost_nothing_available_is_out_of_memory(boss_1):
    proc, _ = boss_1
    _write(proc, "meminfo",
           f"MemTotal: {15991 * KB} kB\nMemFree: {120 * KB} kB\n"
           f"MemAvailable: {300 * KB} kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    advice = _advice()
    assert advice["severity"] == "critical"
    assert "out of memory" in advice["headline"]
    assert "server" in advice["headline"]
    assert "restart" not in advice["action"].lower().replace("a restart is not needed", "")


def test_a_container_at_its_own_limit_is_out_of_memory_even_with_host_room(boss_1):
    _, cg = boss_1
    _write(cg, "memory.max", str(2048 * MB))
    _write(cg, "memory.stat", f"anon {2000 * MB}\nfile {40 * MB}\nunevictable 0\n")
    assert "out of memory" in _advice()["headline"]


def test_a_container_full_of_cache_at_its_limit_is_not_out_of_memory(boss_1):
    _, cg = boss_1
    _write(cg, "memory.max", str(2048 * MB))
    _write(cg, "memory.current", str(2040 * MB))
    _write(cg, "memory.stat", f"anon {200 * MB}\nfile {1840 * MB}\nunevictable 0\n")
    assert _advice() == {"show": False}


def test_a_new_oom_kill_since_the_last_look_fires(boss_1):
    _, cg = boss_1
    assert so.pressure_now()["state"] == "ok"  # baseline: 2 historical kills
    _write(cg, "memory.events", "low 0\nhigh 0\nmax 4\noom 4\noom_kill 3\n")
    advice = _advice()
    assert advice["severity"] == "critical" and "out of memory" in advice["headline"]


def test_sustained_memory_stalls_fire_even_with_available_memory_reported(boss_1):
    proc, _ = boss_1
    _write(proc, "pressure/memory",
           "some avg10=60.00 avg60=40.00 avg300=20.00 total=99999\n"
           "full avg10=35.00 avg60=20.00 avg300=10.00 total=88888\n")
    assert "out of memory" in _advice()["headline"]


def test_restart_advice_is_kept_for_a_laptops_docker_vm(boss_1):
    proc, _ = boss_1
    _write(proc, "version", "Linux version 6.10.14-linuxkit (root@buildkitsandbox)\n")
    _write(proc, "meminfo",
           f"MemTotal: {7836 * KB} kB\nMemFree: {50 * KB} kB\n"
           f"MemAvailable: {100 * KB} kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    advice = _advice()
    assert "computer" in advice["headline"] and "out of memory" in advice["headline"]
    assert "Restart the computer" in advice["action"]
