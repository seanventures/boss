"""Is there a model that can do this project's work? One answer, one sentence, every start path.

THE CASE (2026-11-18, stranger test r3, fresh install, no model connected): Easy Mode reserved the
project's whole $10 daily cap for a build that had no model to run on. The person was told
"Waiting for budget: the project's cap is reached" at $0.00 spent. Try a sample and Start a new
app read as running ("Waiting at meeting", "BOSS is on it"). The real need was a model.

  usable_model(connection, settings, project_id) -> bool
      False only when Setup knows of no usable model for the project (none connected, or every
      one failing). Unreadable means True: this never stops work on a guess.
  NO_MODEL_SENTENCE / SETUP_MODELS_HREF
      What every surface says, and where it links.
"""
from __future__ import annotations

import time
from typing import Any

NO_MODEL_SENTENCE = "No model is connected. Add one in Setup › Models, then press Retry."
#: The same, where the start is a launch button (Easy Mode) rather than a run with a Retry.
NO_MODEL_LAUNCH = ("No model is connected, so nothing was started or reserved. Add one in "
                   "Setup › Models, then press Easy Mode again.")
#: Said on a build or workflow run that waits for one (stuck_work's wait reasons).
WAITING_FOR_MODEL = "Waiting for a model: no model is connected. Add one in Setup › Models, then press Retry."
SETUP_MODELS_HREF = "?area=setup&focus=models"

#: A short memory per project, so a list of runs asks Setup once, not once per run.
_TTL_SECONDS = 10.0
_seen: dict[str, tuple[float, bool]] = {}


def usable_model(connection: Any, settings: Any, project_id: str) -> bool:
    """Whether a model Setup counts as usable is connected for the project. Never raises."""
    key = str(project_id or "")
    hit = _seen.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    try:
        from . import setup_view

        if hasattr(connection, "begin_nested"):
            with connection.begin_nested():   # a failed read never poisons the caller's tx
                basics = setup_view.build_basics(connection, settings, key)
        else:
            basics = setup_view.build_basics(connection, settings, key)
        models = list((basics or {}).get("models") or [])
        answer = any(setup_view.model_working(m) for m in models)
    except Exception:  # noqa: BLE001 — unknown is usable: never hold work on a failed read
        return True
    _seen[key] = (now, answer)
    return answer


def forget() -> None:
    """Drop the short memory (a model was connected or removed; tests)."""
    _seen.clear()
