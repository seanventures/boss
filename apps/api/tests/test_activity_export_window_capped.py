"""A retention-cut export says so on its own header, not via the row cap.

`X-Truncated` stays the 50,000-row cap. `X-Window-Capped` is 1 only when
`export_params` put `window_capped` true on the filter values.
"""

from __future__ import annotations

from apps.api.app.routers.activity import window_capped_header


def test_window_capped_true_is_1_and_anything_else_is_0():
    """A cut window under the row cap is still capped; an open window is not."""
    assert window_capped_header(
        {"since": "2026-09-01T00:00:00+00:00", "window_capped": True}
    ) == "1"
    assert window_capped_header({}) == "0"
    assert window_capped_header({"window_capped": False, "level": "error"}) == "0"
    assert window_capped_header({"window_capped": None}) == "0"
