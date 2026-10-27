"""Build L: the one helper every BOSS-generated console link goes through.

A link without `project_id` renders against whichever project happens to be selected, which is
the render-then-flip bug this build fixes at the console. Every surface that builds a console
URL (notifications, escalations, decision cards, PR comments, Plane/Slack messages) calls
`build_console_link` instead of formatting its own query string, so `project_id` can never be
left off by omission.
"""
from __future__ import annotations

import collections
from urllib.parse import urlencode

#: `link_generated_total{surface,has_project_id}` (spec §7 N6 / acceptance #2). `has_project_id`
#: is only ever "true" because this helper raises rather than emit a link without one — a
#: "false" bucket appearing would mean some surface bypassed it.
LINK_GENERATED_METRIC: "collections.Counter[tuple[str, str]]" = collections.Counter()


def link_generated_metric_value(surface: str, has_project_id: bool) -> int:
    return LINK_GENERATED_METRIC[(surface, "true" if has_project_id else "false")]


def build_console_link(area: str, *, project_id: str, base: str = "", **params: str) -> str:
    """A console URL for `area`, always carrying `project_id` (F7)."""
    if not project_id:
        raise ValueError("build_console_link requires project_id")
    LINK_GENERATED_METRIC[(area, "true")] += 1
    query = {"area": area, "project_id": project_id, **params}
    return f"{base}/?{urlencode(query)}"
