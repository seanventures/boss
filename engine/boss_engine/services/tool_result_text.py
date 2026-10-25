"""A TOOL RESULT CUT TO FIT A MODEL'S CONTEXT SAYS IT WAS CUT.

Every agent path hands a tool's JSON result to the model as text, clipped to a budget
(8,000-16,000 characters). The clip was a bare slice: a list of 14 platform gaps arrived as 8
entries and a broken tail, and the agent concluded the gap it was looking for "does not exist".
`clip_for_model` is the one clip: when it cuts, the text ends with a marker that says the result
is INCOMPLETE, how much was dropped, and how to get the rest (a page, an id, a filter).
"""
from __future__ import annotations

MARKER = ("\n…[CUT: this tool result was {total} characters and only the first {shown} are "
          "shown. It is INCOMPLETE — anything not above may still exist. Ask the tool for "
          "less: a page (offset/limit), a single id, or a narrower filter.]")


def clip_for_model(text: str, limit: int) -> str:
    """`text` whole when it fits; otherwise its head plus the INCOMPLETE marker, within `limit`
    characters overall."""
    text = text if isinstance(text, str) else str(text)
    limit = int(limit)
    if len(text) <= limit:
        return text
    marker = MARKER.format(total=len(text), shown="{shown}")
    shown = max(0, limit - len(marker) - 8)
    return text[:shown] + marker.format(shown=shown)
