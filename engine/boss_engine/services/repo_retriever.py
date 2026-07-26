"""APP-5 (#95) — whole-repo coherence via retrieval.

The agent's repo context was "the first 40 files under 20 KB, by sorted path" (spec 09 §2.7). For a
multi-file frontend — design tokens in one file, layout in another, a dozen components — the agent
never sees the whole design system at once, so coherence (one type scale, one spacing rhythm, one
component vocabulary applied everywhere) is exactly what the fixed window prevents.

This replaces path-order truncation with **retrieval**:

  - **Always pin the coherence source of truth** — for a UI task the design tokens, tailwind config,
    global CSS, `components.json`, and the component-kit surface are in context *regardless* of file
    count. They define coherence; the agent can't be consistent with what it can't see.
  - **Rank the rest by relevance** to the task (lexical overlap on path + content), not by path order.
  - The 40-file / 20 KB caps become a **large safety ceiling**, not the selection mechanism.

Model-free (lexical) — a cheap, deterministic first cut; a semantic/embedding backend is the follow-on.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

# Files that define a project's coherence — always in context for a UI task, whatever the count.
_DESIGN_BASENAMES = {
    "tailwind.config.ts", "tailwind.config.js", "components.json", "postcss.config.js",
    "globals.css", "index.css", "theme.css", "tokens.css", "app.css",
}
_DESIGN_PATH_HINTS = ("components/ui/", "/lib/utils", "design-system", "design_system",
                      "/theme/", "/tokens/", "/styles/")


def is_design_file(rel: str) -> bool:
    """Is this part of the design-system source of truth / component kit?"""
    low = rel.lower().replace("\\", "/")
    base = low.rsplit("/", 1)[-1]
    if base in _DESIGN_BASENAMES:
        return True
    return any(h in low for h in _DESIGN_PATH_HINTS)


_WORD = re.compile(r"[a-z0-9]{3,}")


def terms(text: str) -> set[str]:
    """Lowercased word tokens (≥3 chars) — the lexical unit for relevance."""
    return set(_WORD.findall((text or "").lower()))


def score_relevance(rel: str, content: str, task_terms: set[str]) -> float:
    """Relevance of a file to the task: overlap on the PATH weighs heavily (a filename that matches
    the task is a strong signal), content overlap fills in, saturating so one huge file can't dominate."""
    if not task_terms:
        return 0.0
    path_overlap = len(terms(rel) & task_terms)
    content_overlap = len(terms(content) & task_terms)
    return path_overlap * 5.0 + min(content_overlap, 12)


def select_files(
    entries: list[dict[str, Any]], task: str, *, ui: bool = False,
    pin: Optional[Callable[[str], bool]] = None,
    max_files: int = 120, max_bytes: int = 400_000, head_bytes: int = 20_000,
) -> dict[str, Any]:
    """Choose which files' contents to put in context.

    ``entries`` = ``[{rel, content, size}]``. Pinned files (design files when ``ui``, plus anything the
    optional ``pin`` predicate matches) are always included; the rest are ranked by relevance to the
    task and filled until the file/byte ceiling. Oversized files are head-truncated to ``head_bytes``.
    Returns ``{selected: [{rel, content, pinned, score}], pinned_count, considered, dropped}``.
    """
    task_terms = terms(task)

    def _pinned(rel: str) -> bool:
        return bool((pin and pin(rel)) or (ui and is_design_file(rel)))

    pinned = [e for e in entries if _pinned(e["rel"])]
    rest = [e for e in entries if not _pinned(e["rel"])]
    rest.sort(key=lambda e: (-score_relevance(e["rel"], e.get("content", ""), task_terms), e["rel"]))

    selected: list[dict[str, Any]] = []
    used_bytes = 0
    # pinned first (they get in even if it means blowing slightly past the soft byte ceiling),
    # then relevance-ranked until a ceiling is hit.
    for e in pinned + rest:
        is_pin = _pinned(e["rel"])
        if not is_pin and (len(selected) >= max_files or used_bytes >= max_bytes):
            continue
        body = e.get("content", "")
        truncated = len(body.encode("utf-8", "ignore")) > head_bytes
        if truncated:
            body = body[:head_bytes] + "\n# ... [truncated: exceeds context head cap] ..."
        selected.append({
            "rel": e["rel"], "content": body, "pinned": is_pin, "truncated": truncated,
            "score": None if is_pin else round(score_relevance(e["rel"], e.get("content", ""), task_terms), 1),
        })
        used_bytes += len(body.encode("utf-8", "ignore"))

    return {
        "selected": selected,
        "pinned_count": sum(1 for s in selected if s["pinned"]),
        "considered": len(entries),
        "dropped": len(entries) - len(selected),
    }


def render_context(listing: list[str], selection: dict[str, Any]) -> str:
    """Format the selected files into the agent's REPO context block (design/pinned files first)."""
    files = "\n".join(listing) or "(empty repo)"
    parts = []
    for s in selection["selected"]:
        tag = " (design)" if s["pinned"] else (f" (score {s['score']})" if s.get("score") else "")
        parts.append(f"--- {s['rel']}{tag} ---\n{s['content']}")
    return f"REPO FILES:\n{files}\n\nFILE CONTENTS:\n" + "\n\n".join(parts)
