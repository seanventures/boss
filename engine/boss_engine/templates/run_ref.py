"""THE TEMPLATE'S FILES, ON DISK AND READ-ONLY, FOR THE BUILD THAT APPLIES IT (F2 §2.3).

The build's brief inlines at most `apply.MAX_INLINE_REFERENCE` of a template's code. A larger
template (the banking starter is ~84 KB) lost its later sources and every test file, so the agent
could not copy the tests and `template_tests` failed. Now the whole pinned file set (every stack,
the brief, the guide, the tests) is written for the build, read-only, and named to every process
of THAT build by `BOSS_TEMPLATE_DIR` (and `BOSS_TEMPLATE_STACK_DIR`, the applied stack's
reference): the spec's `/opt/boss/template/` mount, as a directory of the run. The brief points
there.

Where: inside the run's sandbox directory when the build is isolated (BOSS creates it, 0555/0444:
the run's uid reads it and cannot change it), else a private temp directory. Removed when the
build ends. A micro-VM build does not see it (its brief still inlines the tests first).
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

DIR_ENV = "BOSS_TEMPLATE_DIR"
STACK_DIR_ENV = "BOSS_TEMPLATE_STACK_DIR"

_ENV: dict[str, dict[str, str]] = {}
_LOCK = threading.Lock()


def current() -> dict[str, str]:
    """The template environment of the build whose owner scope this process is in ({} if none)."""
    from ..services import owned_process

    owner = owned_process.current_owner()
    if owner is None:
        return {}
    with _LOCK:
        for oid in reversed(owner.ids()):
            if oid in _ENV:
                return dict(_ENV[oid])
    return {}


def write_reference(root: Path, files: dict[str, bytes]) -> int:
    """Write `files` under `root` read-only (dirs 0555, files 0444). Paths that would leave `root`
    are skipped (the import already refuses them; this never trusts that). Returns the bytes written."""
    written = 0
    base = root.resolve()
    for rel, content in sorted(files.items()):
        parts = PurePosixPath(rel).parts
        if not parts or rel.startswith("/") or ".." in parts:
            continue
        target = base.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(content if isinstance(content, bytes) else str(content).encode())
        os.chmod(target, 0o444)
        written += len(content)
    for dirpath, _dirs, _files in os.walk(base, topdown=False):
        os.chmod(dirpath, 0o555)
    return written


def _remove(root: Path) -> None:
    for dirpath, _dirs, _files in os.walk(root):
        with contextlib.suppress(OSError):
            os.chmod(dirpath, 0o755)
    shutil.rmtree(root, ignore_errors=True)


@contextlib.contextmanager
def for_run(engine: Any, run: Optional[dict], ident: Any = None) -> Iterator[Optional[Path]]:
    """Give the build `run` its applied template's files for the block. Never fails the build:
    a template that cannot be read is logged and the gate fails closed on its own."""
    run_id = str((run or {}).get("id") or "")
    applied = None
    if run_id:
        try:
            from . import gate

            with engine.begin() as c:
                applied = gate.applied_template(c, run or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("templates: build %s runs without the template's files (%s)", run_id, type(exc).__name__)
    if not applied or applied.get("error") or not applied.get("files"):
        yield None
        return
    if ident is not None:
        root = Path(ident.run_dir) / "template"
        root.mkdir(mode=0o755)
    else:
        root = Path(tempfile.mkdtemp(prefix="boss-template-"))
    try:
        size = write_reference(root, applied["files"])
        stack = str(applied.get("stack") or "")
        env = {DIR_ENV: str(root)}
        if stack and (root / "stacks" / stack).is_dir():
            env[STACK_DIR_ENV] = str(root / "stacks" / stack)
        with _LOCK:
            _ENV[run_id] = env
        log.info("templates: build %s reads %s %s (%d bytes) at %s", run_id, applied.get("key") or applied.get("id"),
                 applied.get("version"), size, root)
        yield root
    finally:
        with _LOCK:
            _ENV.pop(run_id, None)
        _remove(root)
