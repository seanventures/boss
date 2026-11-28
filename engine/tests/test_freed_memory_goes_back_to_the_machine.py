"""Freed memory goes back to the machine (BOSSAPP-1506).

The API kept every thread's malloc high-water mark and grew to 5.2 GB in a day. Two layers,
both held here: the image caps glibc's arenas (MALLOC_ARENA_MAX=2 in the base every Python
service builds from), and the API runs a thread that calls malloc_trim(0) every minute.
"""
from __future__ import annotations

import pathlib
import re
import sys
import threading

import pytest

from boss_engine.services import memory_trim, system_services

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_every_python_service_image_caps_the_malloc_arenas():
    dockerfile = (ROOT / "Dockerfile.community").read_text()
    base = dockerfile.split(" AS python-base", 1)[1].split("\nFROM ", 1)[0]
    assert re.search(r"^\s*MALLOC_ARENA_MAX=2\b", base, re.M), (
        "python-base (the api, worker and runner images) must set MALLOC_ARENA_MAX=2")


def test_the_trim_thread_is_one_of_the_api_system_services():
    source = (ROOT / "engine/boss_engine/services/system_threads.py").read_text()
    assert "memory_trim.start()" in source
    assert "memory_trim" in system_services.KNOWN


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc only")
def test_a_trim_pass_returns_what_free_alone_keeps():
    """The install's shape: a pool thread (its own arena) builds ~200 MB, keeps a few blocks
    (every 50th, as a response keeps a few strings of a big read) and frees the rest. free()
    alone hands back nothing: the holes sit below live blocks. malloc_trim(0) hands them back."""
    pinned: list[bytearray] = []

    def pool_call() -> None:
        blocks = [bytearray(64 * 1024) for _ in range(3200)]
        pinned.extend(blocks[::50])

    before = memory_trim.rss_kb()
    worker = threading.Thread(target=pool_call)
    worker.start()
    worker.join()
    after_free = memory_trim.rss_kb()
    assert after_free - before > 150_000, (before, after_free)   # free() alone kept it
    out = memory_trim.trim()
    assert out is not None and out["rss_after_kb"] is not None
    assert out["rss_after_kb"] < after_free - 120_000, (before, after_free, out)
    assert len(pinned) == 64


def test_off_or_without_glibc_nothing_starts(monkeypatch):
    monkeypatch.setenv("BOSS_MALLOC_TRIM_SECONDS", "0")
    assert memory_trim.start() is False
    monkeypatch.setattr(memory_trim, "_malloc_trim", lambda: None)
    monkeypatch.delenv("BOSS_MALLOC_TRIM_SECONDS")
    assert memory_trim.start() is False
    assert memory_trim.trim() is None


def test_a_bad_interval_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("BOSS_MALLOC_TRIM_SECONDS", "soon")
    assert memory_trim.interval_seconds() == memory_trim.DEFAULT_SECONDS
