"""Freed memory goes back to the machine (BOSSAPP-1506).

The API runs its sync routes on a pool of threads that come and go (anyio retires an idle one
after 10 s) and its daemons on threads of their own. glibc gives threads their own malloc
arenas, up to 8 per core (96 on the laptop's 12-core Docker VM), and returns a freed block to the
kernel only when it sits at the very top of its heap. Everything else freed stays resident. So a
request that briefly builds 90 MB of strings leaves ~64 MB behind in whatever arena served it, and
over a day every arena holds its own high-water mark: the laptop's API reached 5.2 GB in 18 h
while a walk of its heaps (read from /proc, the process untouched) found 458 MB of the first
945 MB in FREE chunks and only ~40 MB of thread-arena memory in use.

Two settings, one per layer:

* the image sets ``MALLOC_ARENA_MAX=2`` for every Python service (Dockerfile.community
  python-base), so threads share two arenas instead of up to 96;
* this thread calls ``malloc_trim(0)`` every BOSS_MALLOC_TRIM_SECONDS (60; 0 turns it off),
  which hands every arena's free pages back with madvise, whatever the allocator settings.

Not glibc (macOS, musl): nothing to do; `trim()` answers None and the thread is not started.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

_log = logging.getLogger("boss.memory_trim")

DEFAULT_SECONDS = 60.0

_THREAD: Optional[threading.Thread] = None
_TRIM: Optional[Callable[[int], int]] = None
_LOOKED = False
#: The last pass, for the Health screen and the tests: {"at", "rss_before_kb", "rss_after_kb"}.
LAST: dict[str, Any] = {}


def _malloc_trim() -> Optional[Callable[[int], int]]:
    global _TRIM, _LOOKED
    if not _LOOKED:
        _LOOKED = True
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
            fn = libc.malloc_trim
            fn.argtypes, fn.restype = [ctypes.c_size_t], ctypes.c_int
            _TRIM = fn
        except (OSError, AttributeError):   # not glibc
            _TRIM = None
    return _TRIM


def rss_kb() -> Optional[int]:
    """This process's resident set (Linux), in KB."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def trim() -> Optional[dict[str, Any]]:
    """One pass: give every arena's free pages back. None when the allocator has no trim."""
    fn = _malloc_trim()
    if fn is None:
        return None
    before = rss_kb()
    fn(0)
    LAST.update(at=time.time(), rss_before_kb=before, rss_after_kb=rss_kb())
    return dict(LAST)


def interval_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("BOSS_MALLOC_TRIM_SECONDS", DEFAULT_SECONDS)))
    except ValueError:
        return DEFAULT_SECONDS


def start(interval: Optional[float] = None) -> bool:
    """The trim thread, once per process. False when there is nothing to trim or it is off."""
    global _THREAD
    every = interval_seconds() if interval is None else interval
    if every <= 0 or _malloc_trim() is None:
        return False
    if _THREAD is not None and _THREAD.is_alive():
        return True

    def _loop() -> None:
        while True:
            time.sleep(every)
            try:
                trim()
            except Exception:  # noqa: BLE001 — housekeeping never takes the API down
                _log.debug("malloc_trim pass failed", exc_info=True)

    _THREAD = threading.Thread(target=_loop, name="boss-memory-trim", daemon=True)
    _THREAD.start()
    return True


__all__ = ["DEFAULT_SECONDS", "LAST", "interval_seconds", "rss_kb", "start", "trim"]
