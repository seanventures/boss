"""THE SMALL HOST (R3, BOSSAPP-1431): a 4-core / 8 GiB machine, at rest and under pressure.

Every gate and dogfood run happens on a 12-core / 24 GB laptop or an 8 vCPU / 16 GB server, which
derive two to four build slots and never queue for long. An enterprise partner's 4-core / 8 GiB
laptop derived one slot for everything, admitted pinned builds at twice its room, admitted the
first build under high memory pressure, and queued work for 300+ minutes. Tests that reason
about capacity or time caps use these instead of a roomy machine.

Shared by engine/tests and apps/worker/tests (import it as `engine.tests._small_host` from the
repository root, or relatively from engine/tests).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from boss_engine.services.admission import Capacity

GIB = 1024.0

#: At rest: 8 GiB, 4 cores, ~5 GiB available, no pressure.
SMALL_HOST = Capacity(total_mb=8 * GIB, available_mb=5.2 * GIB, cores=4,
                      psi_full_avg10=0.0, psi_full_avg60=0.0, disk_free_gb=40.0,
                      disk_used_pct=55.0, load_per_core=0.3, scope="docker_vm")

#: The same box with one build and a preview running: memory nearly gone, the kernel stalling
#: on memory (PSI full avg10 18 %, avg60 9 %), every core busy.
SMALL_HOST_PRESSURED = Capacity(total_mb=8 * GIB, available_mb=1.4 * GIB, cores=4,
                                psi_full_avg10=18.0, psi_full_avg60=9.0, disk_free_gb=38.0,
                                disk_used_pct=57.0, load_per_core=1.8, swap_used_mb=900.0,
                                scope="docker_vm")

#: A queue as long as the partner's: the self-repair waited 311 minutes before it started.
LONG_QUEUE = timedelta(minutes=311)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def queued_for(minutes: float) -> datetime:
    """A created/enqueued time `minutes` ago."""
    return now_utc() - timedelta(minutes=minutes)
