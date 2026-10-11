"""THE support-level vocabulary, one place for every registry that grades what BOSS supports.

Two registries use it: the stack adapter registry (services/stacks.py: certified, experimental,
best effort, unsupported) and the infrastructure plugin registry (platform/contract.py:
certified, experimental, community). The words and their order (best first) are defined here
once; each registry takes the subset that applies to it.

    certified     every lifecycle stage proven by a recorded, repeatable run against pinned versions
    experimental  a documented subset works; some stages are not yet proven
    community     a third-party plugin; no BOSS-maintainer certification promise
    best_effort   BOSS tries; nothing is proven
    unsupported   BOSS does not do this
"""
from __future__ import annotations

LEVELS: tuple[str, ...] = ("certified", "experimental", "community", "best_effort", "unsupported")
WORDS: dict[str, str] = {"certified": "certified", "experimental": "experimental",
                         "community": "community", "best_effort": "best effort",
                         "unsupported": "not supported"}

#: The stack adapter registry's levels (services/stacks.py).
STACK_LEVELS: tuple[str, ...] = ("certified", "experimental", "best_effort", "unsupported")
#: The infrastructure plugin registry's levels (platform/contract.py SupportLevel).
PLUGIN_LEVELS: tuple[str, ...] = ("certified", "experimental", "community")


def rank(level: str) -> int:
    """Best first: a lower number is a stronger promise."""
    return LEVELS.index(level)
