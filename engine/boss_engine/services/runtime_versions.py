"""Runtime version windows and model fallback for CLI engines (BOSSAPP-858).

`RuntimeBinary` says an engine's CLI must be ON PATH. That is not enough: an image built months
ago carries a CLI that starts, answers `--version`, and then fails hours into a build because the
model the job was routed to needs a newer release, or because a flag BOSS passes was renamed. So
an engine may also DECLARE a version window:

    VersionWindow(binary="codex", min_version="0.150.0", max_version="0.200.0",
                  model_minimums=(("gpt-6*", "0.154.0"),),
                  model_fallbacks=("gpt-6-codex", "gpt-5.2-codex"))

  * min_version   the oldest release BOSS drives (inclusive);
  * max_version   the first release BOSS has NOT been checked against (exclusive) — a CLI past
                  it may have changed a flag, and the pre-check says so instead of a build
                  finding out;
  * model_minimums  (glob, min_version): a model matching the glob needs at least that release;
  * model_fallbacks the engine's models best-first. When the routed model needs a newer CLI than
                  the one installed, the nearest model after it in this list that the installed
                  CLI CAN run is used instead, and an `engine.model_fallback` event says so.

Nothing is declared by default: an engine without a window behaves exactly as before (no
`--version` probe at session start, no pre-check line). The check is a pure function of the
installed version string so tests and the pre-check share one implementation.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

_log = logging.getLogger(__name__)

STATUSES = ("ok", "unknown", "too_old", "too_new", "model_needs_newer", "model_fallback")
FALLBACK_EVENT = "engine.model_fallback"
_VERSION = re.compile(r"(\d+(?:\.\d+){1,3})")


def parse_version(text: Optional[str]) -> Optional[tuple[int, ...]]:
    """The first dotted version in `text` ("codex-cli 0.154.0" -> (0, 154, 0)), else None."""
    if not text:
        return None
    m = _VERSION.search(str(text))
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    a2, b2 = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a2 > b2) - (a2 < b2)


def _fmt(v: Optional[tuple[int, ...]]) -> str:
    return ".".join(str(p) for p in v) if v else "?"


@dataclass(frozen=True)
class VersionWindow:
    binary: str
    min_version: str = ""
    max_version: str = ""
    model_minimums: tuple[tuple[str, str], ...] = ()
    model_fallbacks: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)
    install_hint: str = ""

    def __post_init__(self) -> None:
        # Overrides arrive as JSON lists; hold them as tuples so the window stays hashable.
        object.__setattr__(self, "model_minimums",
                           tuple((str(p), str(v)) for p, v in (self.model_minimums or ())))
        object.__setattr__(self, "model_fallbacks", tuple(self.model_fallbacks or ()))
        object.__setattr__(self, "version_args", tuple(self.version_args or ("--version",)))
        for label, value in (("min_version", self.min_version),
                             ("max_version", self.max_version),
                             *(("model_minimums", v) for _, v in self.model_minimums)):
            if value and parse_version(value) is None:
                raise ValueError(f"{self.binary}: {label} {value!r} is not a version")
        lo, hi = parse_version(self.min_version), parse_version(self.max_version)
        if lo and hi and _cmp(lo, hi) >= 0:
            raise ValueError(f"{self.binary}: min_version must be below max_version")

    @property
    def declared(self) -> bool:
        return bool(self.min_version or self.max_version or self.model_minimums)

    def minimum_for(self, model: Optional[str]) -> Optional[tuple[int, ...]]:
        """The release `model` needs (the highest matching minimum), or None."""
        if not model:
            return None
        found: Optional[tuple[int, ...]] = None
        for pattern, version in self.model_minimums:
            if fnmatch.fnmatchcase(model.lower(), pattern.lower()):
                v = parse_version(version)
                if v and (found is None or _cmp(v, found) > 0):
                    found = v
        return found

    def view(self) -> dict[str, Any]:
        return {"binary": self.binary, "min_version": self.min_version or None,
                "max_version": self.max_version or None,
                "model_minimums": [list(p) for p in self.model_minimums],
                "model_fallbacks": list(self.model_fallbacks)}


@dataclass(frozen=True)
class VersionVerdict:
    status: str
    installed: Optional[str]
    detail: str
    hint: str = ""
    model: Optional[str] = None
    #: The model to run instead (status "model_fallback"), else None.
    fallback: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        return self.status in ("too_old", "too_new", "model_needs_newer")

    def view(self) -> dict[str, Any]:
        return {"status": self.status, "installed": self.installed, "detail": self.detail,
                "hint": self.hint or None, "model": self.model, "fallback": self.fallback}


def _hint(window: VersionWindow, wanted: str) -> str:
    base = window.install_hint or f"upgrade {window.binary}"
    return f"{base} (needs {window.binary} {wanted})"


def check(window: VersionWindow, installed: Optional[str],
          model: Optional[str] = None) -> VersionVerdict:
    """The verdict for `installed` (the CLI's `--version` output) against the window and the
    routed `model`. Pure: no process is started here."""
    have = parse_version(installed)
    shown = _fmt(have) if have else None
    if have is None:
        return VersionVerdict("unknown", None, f"{window.binary}: could not read its version",
                              model=model)
    lo, hi = parse_version(window.min_version), parse_version(window.max_version)
    if lo and _cmp(have, lo) < 0:
        return VersionVerdict("too_old", shown,
                              f"{window.binary} {shown} is older than {window.min_version}, the "
                              "oldest release BOSS drives",
                              hint=_hint(window, f">= {window.min_version}"), model=model)
    if hi and _cmp(have, hi) >= 0:
        return VersionVerdict("too_new", shown,
                              f"{window.binary} {shown} is at or past {window.max_version}, which "
                              "BOSS has not been checked against",
                              hint=(f"pin {window.binary} below {window.max_version}"
                                    + (f" ({window.install_hint})" if window.install_hint else "")),
                              model=model)
    need = window.minimum_for(model)
    if need and _cmp(have, need) < 0:
        fallback = nearest_fallback(window, model, have)
        if fallback:
            return VersionVerdict("model_fallback", shown,
                                  f"{model} needs {window.binary} {_fmt(need)}; {shown} is "
                                  f"installed, so {fallback} runs instead",
                                  hint=_hint(window, f">= {_fmt(need)}"), model=model,
                                  fallback=fallback)
        return VersionVerdict("model_needs_newer", shown,
                              f"{model} needs {window.binary} {_fmt(need)}; {shown} is installed "
                              "and no fallback model it can run is declared",
                              hint=_hint(window, f">= {_fmt(need)}"), model=model)
    return VersionVerdict("ok", shown, f"{window.binary} {shown} is inside its window",
                          model=model)


def nearest_fallback(window: VersionWindow, model: Optional[str],
                     have: tuple[int, ...]) -> Optional[str]:
    """The nearest model the installed CLI can run: the first in `model_fallbacks` AFTER the
    routed one (or the first in the list, when the routed model is not in it) whose minimum the
    installed release meets."""
    order = list(window.model_fallbacks)
    lowered = [m.lower() for m in order]
    start = lowered.index(model.lower()) + 1 if model and model.lower() in lowered else 0
    for candidate in order[start:]:
        need = window.minimum_for(candidate)
        if need is None or _cmp(have, need) >= 0:
            return candidate
    return None


# ── probing the installed CLI ─────────────────────────────────────────────────────────────────

_CACHE: dict[tuple, Optional[str]] = {}


def installed_version(window: VersionWindow, *,
                      run: Optional[Callable[[Sequence[str]], str]] = None,
                      which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """The CLI's `--version` output, cached per binary path and mtime. None when absent."""
    path = which(window.binary)
    if not path:
        return None
    try:
        key = (path, os.stat(path).st_mtime_ns, window.version_args)
    except OSError:
        key = (path, 0, window.version_args)
    if run is None and key in _CACHE:
        return _CACHE[key]
    try:
        if run is not None:
            out = run([path, *window.version_args])
        else:
            from . import owned_process

            done = owned_process.boss_run([path, *window.version_args], capture_output=True,
                                          text=True, timeout=20)
            out = (done.stdout or done.stderr or "") if done.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    value = (out or "").strip() or None
    if run is None:
        _CACHE[key] = value
    return value


def verdict_for(window: Optional[VersionWindow], model: Optional[str] = None, *,
                run: Optional[Callable[[Sequence[str]], str]] = None,
                which: Callable[[str], Optional[str]] = shutil.which
                ) -> Optional[VersionVerdict]:
    """None when nothing is declared (the default: byte-identical behaviour)."""
    if window is None or not window.declared:
        return None
    return check(window, installed_version(window, run=run, which=which), model)


def apply_fallback(engine: str, window: Optional[VersionWindow], model: Optional[str], *,
                   run: Optional[Callable[[Sequence[str]], str]] = None,
                   which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """At session start: the model to run. Unchanged unless a window is declared and the routed
    model needs a newer CLI; then the nearest fallback, with an `engine.model_fallback` event.
    A window the CLI is outside of raises (the pre-check said so first)."""
    verdict = verdict_for(window, model, run=run, which=which)
    if verdict is None or verdict.status in ("ok", "unknown"):
        return model
    if verdict.status == "model_fallback":
        _log.warning("%s: %s", engine, verdict.detail)
        record_fallback_event(engine, verdict)
        return verdict.fallback
    from .cli_harness import CliHarnessUnavailable

    raise CliHarnessUnavailable(f"{verdict.detail}. {verdict.hint}".strip())


def record_fallback_event(engine: str, verdict: VersionVerdict) -> None:
    """An event on the run (llm_telemetry's context names it); a log line when there is none."""
    try:
        from .llm_telemetry import get_llm_context

        ctx = get_llm_context() or {}
        db, project_id = ctx.get("db_engine"), ctx.get("project_id")
        if db is None or not project_id:
            return
        from .core import emit_event

        with db.begin() as conn:
            emit_event(conn, project_id, FALLBACK_EVENT, "boss", "run",
                       str(ctx.get("run_id") or ctx.get("workflow_run_id") or ""),
                       {"engine": engine, "stage": ctx.get("stage"), **verdict.view()})
    except Exception:  # noqa: BLE001 — telling about a fallback never fails the session
        _log.debug("model fallback event not recorded", exc_info=True)


def clear_cache_for_tests() -> None:
    _CACHE.clear()
