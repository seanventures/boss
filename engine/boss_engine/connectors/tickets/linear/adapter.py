"""Linear: the existing adapter (create + test), unchanged, behind the v2 contract."""

from __future__ import annotations

from ....services.work_tracker_adapters import LinearWorkTrackerAdapter
from ..base import TicketAdapterBase


class LinearTicketAdapter(LinearWorkTrackerAdapter, TicketAdapterBase):
    """Read/browse/transition are not built yet; they raise CapabilityNotSupported."""
