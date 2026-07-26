"""APP-2 (#92) — design/product role + design-aware prompts.

BOSS's generation prompts are correctness-only: the implementer is told to make "the smallest correct
change," and the adversarial reviewer is told that anything out of the correctness scope is *not* a
defect (spec 09 §2.2, §2.3). So even a perfectly correct page comes out bare — placeholder copy,
no visual hierarchy — and the gate structurally cannot register "this isn't coherent/finished."

This adds the missing **design & product-coherence axis** to the prompts, but only where it applies:

- `is_ui_bearing` decides whether a task touches the surface a user sees (so Python/back-end tasks are
  untouched — this is additive).
- `product_design_prompt` is the new first-class **product_design** role: visual system, component-kit
  usage, responsive-by-default, and — the load-bearing move — **on-domain copy** (specific audience,
  price points, value prop, process narrative), with placeholder/lorem explicitly forbidden.
- `design_impl_clause` amends the implement prompt so "smallest change" is scoped to *behavior*: the
  design system and copy requirements are **not strippable** to a minimal diff.
- `design_review_clause` amends the reviewer so, for UI changes, **incoherence is in scope** —
  placeholder copy, inconsistent type/spacing, missing responsive, broken hierarchy become defects.
"""
from __future__ import annotations

from typing import Any, Optional

# Signals that a task targets the user-facing surface. Kept broad but frontend-specific so a
# back-end task mentioning "page" in passing doesn't trip it (we also weight file extensions).
_UI_KEYWORDS = (
    "frontend", "front-end", "landing page", "landing-page", "web app", "web-app", "website",
    "dashboard", "ui/ux", " ux ", "responsive", "tailwind", "shadcn", "component", "react",
    "next.js", "nextjs", "vite", "css", "design", "visual", "layout", "hero section", "marketing page",
)
_UI_EXTS = (".tsx", ".jsx", ".css", ".scss", ".html", ".vue", ".svelte")
_WEB_KINDS = {"web", "spa", "frontend", "site", "landing"}


def is_ui_bearing(spec: dict[str, Any], title: str = "", test_files: Optional[list[str]] = None) -> bool:
    """True when a task touches the user-facing surface — an explicit ``project_kind`` of web/spa, any
    UI file extension in the test/target files, or UI vocabulary in the title/instructions."""
    if str(spec.get("project_kind", "")).lower() in _WEB_KINDS:
        return True
    for f in (test_files or []) + list(spec.get("target_files", []) or []):
        if str(f).lower().endswith(_UI_EXTS):
            return True
    hay = f"{title} {spec.get('instructions', '')} {' '.join(spec.get('acceptance_criteria', []))}".lower()
    return any(k in hay for k in _UI_KEYWORDS)


_ON_DOMAIN_COPY = (
    "ON-DOMAIN COPY (the load-bearing requirement): write specific, audience-aware copy — a concrete "
    "audience, a real value proposition, a legible process narrative, and realistic figures where the "
    "domain implies them. NEVER ship lorem ipsum, 'Welcome to our platform', 'Lorem'/'placeholder', "
    "'Your title here', or generic filler. If a real figure (price, stat) isn't supplied, use a "
    "plausible illustrative value and mark it clearly (e.g. a TODO comment) rather than leaving filler."
)


def product_design_prompt(title: str, instructions: str, criteria: list[str],
                          design_system: Optional[str] = None) -> str:
    """The new product_design role: turn a scaffolded baseline into a coherent, on-domain first pass."""
    crit = "\n".join(f"- {c}" for c in criteria) or "- (design to the task description)"
    ds = (f"\n\nDESIGN SYSTEM (theme these tokens — do not invent a parallel one):\n{design_system}"
          if design_system else "")
    return (
        "You are a senior product designer + front-end engineer. Your job is COHERENCE, not features: "
        "make the first pass read as a designed, finished product on the surface the user sees first.\n\n"
        f"TASK: {title}\n\n{instructions}\n\nACCEPTANCE CRITERIA:\n{crit}{ds}\n\n"
        "DELIVER:\n"
        "- Visual system: apply ONE type scale, ONE spacing rhythm, ONE accent consistently; establish "
        "a clear hierarchy (hero → sections → CTA); responsive/mobile-first by default.\n"
        "- Component kit: compose from the scaffolded primitives (e.g. shadcn Button/Card) rather than "
        "hand-rolling divs — this keeps it coherent for free.\n"
        f"- {_ON_DOMAIN_COPY}\n"
        "- End with a one-line summary of the design decisions."
    )


def design_impl_clause() -> str:
    """Appended to the implement prompt for UI-bearing tasks: presentation is not strippable."""
    return (
        "\n\nDESIGN AXIS (this is a UI-bearing task): the 'smallest correct change' rule applies to "
        "BEHAVIOR only — you may NOT strip presentation to a minimal diff. Satisfy the design system "
        "(consistent type scale, spacing, accent; clear visual hierarchy; responsive by default), "
        "compose the existing component kit, and keep copy on-domain and specific. "
        + _ON_DOMAIN_COPY
    )


def design_review_clause() -> str:
    """Appended to the review prompt for UI-bearing changes: incoherence IS a defect here."""
    return (
        "\n\nDESIGN AXIS (this is a UI-bearing change): incoherence is IN SCOPE and reportable — do NOT "
        "suppress it as out-of-scope. Treat these as concrete defects (VERDICT: REFUTED): placeholder / "
        "lorem / generic copy ('Welcome to our platform'), inconsistent type scale or spacing, a missing "
        "responsive/mobile layout, or broken visual hierarchy (no clear hero/sections/CTA). Correctness "
        "defects still count too; a bare-but-correct page is not a pass for a UI task."
    )
