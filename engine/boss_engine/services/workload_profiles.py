"""What each kind of work costs the machine (stability spec §4b).

`request` is the memory a job must find free before it starts (admission); `limit` is the most
its container may take (`docker run --memory`). Seeded from the per-phase footprints measured in
the api image (spec appendix C: agent ~300 MB, npm install 259 MB anon, vite build 159, tsc 166,
Chromium 235 — a small app build peaks at ~0.8-1.0 GB unreclaimable) and moved by the peaks real
runs record (EWMA of memory.peak), so a fleet of Next.js monorepos converges on its own numbers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text


@dataclass(frozen=True)
class Profile:
    kind: str
    request_mb: int
    limit_mb: int
    cpus: float = 1.0
    pids: int = 1024


#: Spec §4b.2 table. Job kinds map onto these through `profile_for_job`.
DEFAULTS: dict[str, Profile] = {
    "code.task": Profile("code.task", 1200, 3072, cpus=2.0, pids=2048),
    "preview_node": Profile("preview_node", 300, 1024, cpus=1.0),
    "preview_static": Profile("preview_static", 64, 256, cpus=0.5, pids=256),
    "review_seat": Profile("review_seat", 400, 1024, cpus=1.0),
    "discovery": Profile("discovery", 300, 1024, cpus=1.0),
    "chromium": Profile("chromium", 400, 1024, cpus=1.0),
    "sandbox": Profile("sandbox", 1200, 2048, cpus=2.0, pids=512),
}

#: Job kinds that start heavy local work. Everything else (meeting steps, decisions, tracker
#: sync, model-only steps) is light and is never held back for memory.
HEAVY_JOB_KINDS = {
    "code.task": "code.task",
    "governed.code_task": "code.task",
    "discovery.run": "discovery",
    "preview.start": "preview_node",
}

#: Weight of a new observation in the EWMA.
EWMA_ALPHA = 0.3
#: An observed peak never shrinks the request below this fraction of the seeded default.
FLOOR_FRACTION = 0.5


def profile_for_job(kind: str) -> Optional[str]:
    return HEAVY_JOB_KINDS.get(str(kind or ""))


def load(connection: Any, kind: str) -> Profile:
    base = DEFAULTS.get(kind) or Profile(kind, 300, 1024)
    try:
        row = connection.execute(text(
            "SELECT request_mb, limit_mb FROM workload_profiles WHERE kind = :k"),
            {"k": kind}).mappings().first()
    except Exception:  # noqa: BLE001 — an older schema: the seeded numbers
        row = None
    if not row:
        return base
    return Profile(kind, int(row["request_mb"]), int(row["limit_mb"]), base.cpus, base.pids)


def observe_peak(connection: Any, kind: str, peak_mb: float) -> Profile:
    """Move a kind's request toward what a real run peaked at. The limit follows with headroom
    but never drops below the seeded limit (a cap that kills a normal build is worse than none)."""
    base = DEFAULTS.get(kind) or Profile(kind, 300, 1024)
    current = load(connection, kind)
    request = (1 - EWMA_ALPHA) * current.request_mb + EWMA_ALPHA * float(peak_mb)
    request = max(int(base.request_mb * FLOOR_FRACTION), int(round(request)))
    limit = max(base.limit_mb, int(request * 2))
    connection.execute(text(
        "INSERT INTO workload_profiles (kind, request_mb, limit_mb, observed_peak_mb, samples, "
        "updated_at) VALUES (:k, :r, :l, :p, 1, now()) ON CONFLICT (kind) DO UPDATE SET "
        "request_mb = :r, limit_mb = :l, observed_peak_mb = :p, "
        "samples = workload_profiles.samples + 1, updated_at = now()"),
        {"k": kind, "r": request, "l": limit, "p": int(peak_mb)})
    return Profile(kind, request, limit, base.cpus, base.pids)


def workload_oom_score_adj() -> int:
    """Workload processes are the kernel's first choice under memory pressure, never the API or
    Postgres (spec §4b.1: +500 for builds, -500 api, -900 db)."""
    try:
        return max(-1000, min(1000, int(os.environ.get("BOSS_WORKLOAD_OOM_SCORE_ADJ") or 500)))
    except ValueError:
        return 500


def container_args(kind: str, *, profile: Optional[Profile] = None) -> list[str]:
    """The envelope a BOSS workload container runs in: a hard memory cap with no swap beyond
    it, a reservation, CPUs, a pids bound, and the OOM killer's first pick."""
    p = profile or DEFAULTS.get(kind) or Profile(kind, 300, 1024)
    return [
        "--memory", f"{p.limit_mb}m",
        "--memory-reservation", f"{min(p.request_mb, p.limit_mb)}m",
        "--memory-swap", f"{p.limit_mb}m",
        "--cpus", f"{p.cpus:g}",
        "--pids-limit", str(p.pids),
        "--oom-score-adj", str(workload_oom_score_adj()),
    ]
