"""`exited -9` should be answerable in one number, not one hour.

BOSS can interrogate its own database in fine detail and can inspect the hosts it DEPLOYS to. It
had nothing at all for the container it runs in. So a SIGKILLed agent cost roughly an hour of
hypothesis-testing — OOM killer? deploy restart? something internal? — and was settled by reading
code rather than data (I20). The answer was BOSS's own process reaper, and an OOM count of zero
would have said so immediately.

It is also what makes experiment E1 provable. `/v1/code-tasks?limit=5` goes 0.37s -> 47s while a
build runs; that this is CPU starvation is an INFERENCE until load-per-core and cgroup throttling
are actually read.

No docker socket: /proc and the container's own cgroup files are sufficient, and the socket is
not something an application container should be handed.
"""

from pathlib import Path

import pytest

from boss_engine.services import self_observability as so


@pytest.fixture()
def fake_cgroup(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "CGROUP_V2", tmp_path)
    monkeypatch.setattr(so, "CGROUP_V1_MEM", tmp_path / "v1mem")
    return tmp_path


def test_nothing_raises_where_none_of_these_files_exist(tmp_path, monkeypatch):
    """Runs on macOS in development, where /proc and cgroup do not exist at all. A diagnostic
    that can fail the request it is diagnosing is worse than no diagnostic."""
    monkeypatch.setattr(so, "CGROUP_V2", tmp_path / "nope")
    monkeypatch.setattr(so, "CGROUP_V1_MEM", tmp_path / "nope")
    snap = so.self_snapshot(preview_root=str(tmp_path / "previews"))
    assert isinstance(snap["cpu"], dict) and isinstance(snap["memory"], dict)
    assert snap["notes"]


def test_an_oom_kill_is_reported_and_named(fake_cgroup):
    (fake_cgroup / "memory.events").write_text("low 0\nhigh 3\nmax 12\noom 2\noom_kill 1\n")
    (fake_cgroup / "memory.current").write_text("900000000")
    (fake_cgroup / "memory.max").write_text("1000000000")
    snap = so.self_snapshot()
    assert snap["memory"]["oom_kills"] == 1
    assert any("OOM killer has fired" in n for n in snap["notes"])


def test_zero_oom_kills_actively_exonerates_the_kernel(fake_cgroup):
    """The point of I20: 'not the OOM killer' is the finding that redirects the search."""
    (fake_cgroup / "memory.events").write_text("oom 0\noom_kill 0\n")
    snap = so.self_snapshot()
    assert snap["memory"]["oom_kills"] == 0
    assert any("look for something in BOSS that sends signals" in n for n in snap["notes"])


def test_an_uncapped_memory_limit_is_not_reported_as_a_number(fake_cgroup):
    (fake_cgroup / "memory.current").write_text("500000000")
    (fake_cgroup / "memory.max").write_text("max")
    assert "limit_mb" not in so.memory_snapshot()


def test_the_cgroup_v1_no_limit_sentinel_is_not_mistaken_for_a_real_ceiling(fake_cgroup):
    """v1 reports ~2^63 for 'unlimited'; rendering that as 8 exabytes would be nonsense."""
    v1 = fake_cgroup / "v1mem"
    v1.mkdir()
    (v1 / "memory.usage_in_bytes").write_text("500000000")
    (v1 / "memory.limit_in_bytes").write_text("9223372036854771712")
    assert "limit_mb" not in so.memory_snapshot()


def test_cpu_throttling_is_distinguished_from_being_busy(fake_cgroup):
    """Hitting your own quota and having too much to do need different fixes."""
    (fake_cgroup / "cpu.stat").write_text(
        "usage_usec 5000000\nnr_periods 100\nnr_throttled 40\nthrottled_usec 2000000\n")
    snap = so.self_snapshot()
    assert snap["cpu"]["throttled_periods"] == 40
    assert snap["cpu"]["throttled_seconds"] == 2.0
    assert any("own CPU ceiling" in n for n in snap["notes"])


def test_a_cpu_quota_is_converted_to_cores(fake_cgroup):
    (fake_cgroup / "cpu.max").write_text("50000 100000")
    assert so.cpu_snapshot()["cpu_limit_cores"] == 0.5


def test_an_uncapped_quota_is_explicitly_none_not_missing(fake_cgroup):
    """'Uncapped' and 'we could not read it' are different facts."""
    (fake_cgroup / "cpu.max").write_text("max 100000")
    cpu = so.cpu_snapshot()
    assert "cpu_limit_cores" in cpu and cpu["cpu_limit_cores"] is None


def test_oversubscribed_cpu_is_called_out_in_plain_language(monkeypatch):
    """This is the number E1 predicts: dozens of runnable build processes, not a busy API."""
    monkeypatch.setattr(so, "cpu_snapshot",
                        lambda: {"cores": 4, "load_1m": 12.0, "load_per_core": 3.0})
    monkeypatch.setattr(so, "memory_snapshot", lambda: {"used_mb": 100})
    monkeypatch.setattr(so, "disk_snapshot", lambda _r=None: {})
    monkeypatch.setattr(so, "process_snapshot", lambda *_a, **_k: {"available": False})
    notes = so.self_snapshot()["notes"]
    assert any("oversubscribed" in n and "waiting for a timeslice" in n for n in notes)


def test_preview_disk_is_measured_because_i39_could_only_be_guessed_at(tmp_path):
    root = tmp_path / "previews"
    (root / "prev_a-1" / "node_modules").mkdir(parents=True)
    (root / "prev_a-1" / "node_modules" / "big.bin").write_bytes(b"\0" * (3 * 1024 * 1024))
    disk = so.disk_snapshot(str(root))
    assert disk["preview_dirs"] == 1
    assert disk["preview_mb"] >= 3


def test_the_snapshot_always_says_something(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "CGROUP_V2", tmp_path / "none")
    assert so.self_snapshot()["notes"], "a diagnostic with no verdict makes the operator do the work"


def test_the_tool_is_registered_read_only():
    """Ask BOSS must be able to reach this without it counting as a write.

    Asserted against the built REGISTRY rather than the source text: a substring search proves
    the characters exist somewhere near each other, not that the tool was registered with that
    kind. Checking the shape of the code instead of its behaviour is the I26 mistake.
    """
    from boss_engine.tools.builtin import build_default_registry

    reg = build_default_registry()
    spec = next((s for s in reg.specs() if s.name == "inspect_self"), None)
    assert spec is not None, "inspect_self is not in the default registry"
    assert spec.kind == "read"


def test_ask_boss_can_reach_it_without_a_write_grant():
    """The discuss registry admits every read tool; a diagnostic behind a write grant is one
    nobody will have when they need it."""
    from boss_engine.tools.builtin import build_discuss_read_registry

    names = {s.name for s in build_discuss_read_registry().specs()}
    assert "inspect_self" in names
