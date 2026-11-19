"""APP-4 (#94) — a real visual/UX gate (deterministic rubric).

The one hook that could carry a design signal, `_build_browser_validation_review`, was a stub that
always returned `required_not_run` — it never rendered, screenshotted, or judged, and the release
go/no-go didn't block on it (spec 09 §2.5). And the preview "verify" passed on any 2xx even if the
body was blank or a React error overlay (§2.6). So the strongest "it works" signal was "a socket
answered."

This turns the stub into a **real, deterministic gate**. It judges *render evidence* against a rubric:

  - renders **without an error overlay** (React/Vite error boundary, framework 500 page, console errors),
  - has **real, non-placeholder content** (not a blank body / bare root div; no lorem / "Welcome to our
    platform" / "Your title here"),
  - has **visual hierarchy** (an `<h1>` and some sectioning),
  - **doesn't overflow at mobile width** (when a viewport measurement is available).

Decision (spec §6 #2): **deterministic checks now**; a vision-model rubric judge is the frontier-metered
follow-on (APP-6). The `render evidence` is captured however it can be — the served HTML from the
existing preview works today; a headless-browser (Playwright) capture with screenshots + a real mobile
viewport is the injectable follow-on (where that infra runs is the deferred sandbox decision). The
rubric here is pure and works on whatever evidence it's given.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_TAG = re.compile(r"<[^>]+>")
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)

# error-page / crash markers (framework overlays + generic error pages)
_ERROR_MARKERS = (
    "application error", "unhandled runtime error", "internal server error",
    "this page could not be found", "500: ", "react error", "vite error", "error boundary",
    "__next_error__", "runtimeerror", "cannot get /",
)
_PLACEHOLDER_MARKERS = (
    "lorem ipsum", "welcome to our platform", "your title here", "your headline here",
    "placeholder text", "insert text here", "todo: copy", "dolor sit amet", "product headline goes here",
)


def visible_text(html: str) -> str:
    """Approximate the text a user would see: drop script/style, strip tags, collapse whitespace."""
    without = _SCRIPT_STYLE.sub(" ", html or "")
    return " ".join(_TAG.sub(" ", without).split())


def has_real_content(html: str, *, min_chars: int = 40) -> bool:
    """Real content present — not a blank body or a bare SPA root div awaiting JS."""
    return len(visible_text(html)) >= min_chars


_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def rendered_markup(html: str) -> str:
    """The page's own markup — tags and text — without what the browser does not render:
    <script>/<style> bodies and HTML comments. Tags stay, so a structural marker such as
    `<html id="__next_error__">` is still seen."""
    return _COMMENT.sub(" ", _SCRIPT_STYLE.sub(" ", html or ""))


def error_page_marker(html: str) -> Optional[str]:
    """The error-page marker a page RENDERS, or None.

    ONLY WHAT IS RENDERED COUNTS. A Next.js App Router page inlines its whole flight payload in
    `<script>self.__next_f.push(...)</script>`, and that payload carries the framework's default
    not-found tree — `"404: This page could not be found."` — on EVERY page, working or not. The
    markers were matched against the raw HTML, so every working Next.js App Router page read as
    an error page: invoice-desk (crun_33379dfc, 2026-11-18) answered 200 at `/` with its real
    page and its app_serves gate failed with "the health check rejected the page it served",
    which held the build at "Checks failing". Script bodies, styles and comments are not the
    page; a real error page renders its error (or marks its markup, `__next_error__`)."""
    low = rendered_markup(html).lower()
    return next((m for m in _ERROR_MARKERS if m in low), None)


def is_error_page(html: str, console_errors: Optional[list[str]] = None) -> bool:
    if error_page_marker(html):
        return True
    return bool(console_errors)


def has_placeholder_copy(html: str) -> bool:
    low = (html or "").lower()
    return any(m in low for m in _PLACEHOLDER_MARKERS)


def has_visual_hierarchy(html: str) -> bool:
    """A legible page has a top-level heading and some sectioning — not a wall of undifferentiated divs."""
    low = (html or "").lower()
    has_h1 = "<h1" in low
    sectioning = low.count("<h2") + low.count("<section") + low.count("<header") + low.count("<nav")
    return has_h1 and sectioning >= 1


def mobile_overflows(viewport: Optional[dict[str, Any]], *, tolerance: int = 2) -> Optional[bool]:
    """True if the page scrolls horizontally at mobile width. None when no measurement was captured
    (served-HTML evidence can't know this — it needs a real headless viewport)."""
    m = (viewport or {}).get("mobile") if viewport else None
    if not m or "scrollWidth" not in m or "clientWidth" not in m:
        return None
    return m["scrollWidth"] > m["clientWidth"] + tolerance


def evaluate_visual(evidence: Optional[dict[str, Any]], *, require_hierarchy: bool = True) -> dict[str, Any]:
    """Judge render evidence against the rubric.

    ``evidence`` = ``{html, console_errors?, viewport?, route?, screenshot_ref?}`` or None. Returns
    ``{status: passed|failed|not_applicable, findings, checks, evidence_refs}``. Hard failures (error
    overlay, no real content, placeholder copy, mobile overflow) → ``failed``; missing hierarchy is a
    finding but not a hard block (design nit vs broken render — spec open-decision #3).
    """
    if not evidence or not isinstance(evidence, dict):
        return {"status": "not_applicable", "findings": [], "checks": {}, "evidence_refs": []}
    html = evidence.get("html", "")
    console_errors = evidence.get("console_errors") or []
    checks: dict[str, Any] = {
        "renders_without_error": not is_error_page(html, console_errors),
        "has_real_content": has_real_content(html),
        "no_placeholder_copy": not has_placeholder_copy(html),
        "has_visual_hierarchy": has_visual_hierarchy(html),
    }
    overflow = mobile_overflows(evidence.get("viewport"))
    if overflow is not None:
        checks["no_mobile_overflow"] = not overflow

    findings: list[str] = []
    if not checks["renders_without_error"]:
        findings.append("renders an error overlay / crash page (or console errors present)")
    if not checks["has_real_content"]:
        findings.append("no real content — blank body or bare root div")
    if not checks["no_placeholder_copy"]:
        findings.append("placeholder/lorem copy present — not on-domain")
    if checks.get("no_mobile_overflow") is False:
        findings.append("horizontal overflow at mobile width")
    if not checks["has_visual_hierarchy"]:
        findings.append("no clear visual hierarchy (missing h1/sectioning)")

    # hard blockers vs the hierarchy nit
    hard = (not checks["renders_without_error"] or not checks["has_real_content"]
            or not checks["no_placeholder_copy"] or checks.get("no_mobile_overflow") is False
            or (require_hierarchy and not checks["has_visual_hierarchy"]))
    return {
        "status": "failed" if hard else "passed",
        "findings": findings, "checks": checks,
        "evidence_refs": [r for r in [evidence.get("screenshot_ref")] if r],
        "route": evidence.get("route"),
    }
