"""PLAYWRIGHT BROWSERS: DOWNLOADED ONCE PER INSTALL, REUSED BY EVERY BUILD.

crun_966aa44e spent three 300-second timeouts on `npx --package=@playwright/test playwright install
chromium`, then failed. Every build ran that download again, inside its sandbox, into a cache
directory its sandbox uid could not write (/var/tmp/boss-tool-cache, 0755 boss) that is also lost
on every upgrade.

Now:

* the browsers live on the toolchain cache volume (`boss_community_toolchain_cache`, mounted at
  `toolchains.cache_root()` in the api and worker): `<cache root>/playwright`, owned by BOSS,
  0755 — a build's sandbox uid reads and runs them, and cannot change them (one build can never
  plant a browser another build runs);
* BOSS installs them itself, lazily, the first time a build needs that Playwright version, under
  an flock (concurrent builds wait for the one download instead of racing it), with a timeout long
  enough for a cold download (`BOSS_PLAYWRIGHT_INSTALL_TIMEOUT`, default 1200s). The CLI is
  `playwright@<version>` from the npm registry — never the project's own node_modules, which the
  agent wrote, run as BOSS;
* a version already installed is a marker check: later builds skip the install entirely.

`python -m boss_engine.services.playwright_cache prime <version>` installs one ahead of a build.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("boss.playwright_cache")

TIMEOUT_ENV = "BOSS_PLAYWRIGHT_INSTALL_TIMEOUT"
DEFAULT_TIMEOUT_SECONDS = 1200
SUBDIR = "playwright"
_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}(?:-[0-9A-Za-z.]{1,40})?$")


def install_timeout() -> int:
    try:
        return max(300, int(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def browsers_dir(*, create: bool = True) -> Optional[Path]:
    """The shared browsers directory, or None when this process has no writable toolchain cache
    (a developer's Mac, a test): callers then keep the old per-container path."""
    from . import toolchains

    root = toolchains.cache_root()
    try:
        if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
            return None
        path = root / SUBDIR
        if create and not path.is_dir():
            path.mkdir()
        if not path.is_dir():
            return None
        if os.stat(path).st_uid == os.geteuid():
            os.chmod(path, 0o755)
        # The root holds per-project caches (each 0700): a sandbox uid may pass through it to
        # reach the browsers, never list it.
        st = os.stat(root)
        if st.st_uid == os.geteuid() and (st.st_mode & 0o011) != 0o011:
            os.chmod(root, (st.st_mode & 0o7777) | 0o011)
        return path
    except OSError as exc:
        log.warning("playwright browser cache unavailable under %s: %s", root, exc)
        return None


def project_version(app_dir: Any) -> Optional[str]:
    """The Playwright version the app's tests run, from its installed packages (data only, never
    executed). None when it has none, or when the value is not a plain version."""
    base = Path(app_dir)
    for pkg in ("@playwright/test", "playwright"):
        try:
            raw = json.loads((base / "node_modules" / pkg / "package.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        version = raw.get("version") if isinstance(raw, dict) else None
        if isinstance(version, str) and _VERSION.match(version):
            return version
    return None


def _marker(base: Path, version: str) -> Path:
    return base / f".boss-chromium-{version}.ok"


def is_cached(version: str, base: Optional[Path] = None) -> bool:
    base = base or browsers_dir(create=False)
    return bool(base and _VERSION.match(version or "") and _marker(base, version).is_file())


def _open_modes(base: Path) -> None:
    """What BOSS downloaded must be readable and runnable by a build's uid (umask-proof)."""
    for dirpath, dirnames, filenames in os.walk(base):
        with contextlib.suppress(OSError):
            os.chmod(dirpath, os.stat(dirpath).st_mode & 0o7777 | 0o755)
        for name in filenames:
            p = os.path.join(dirpath, name)
            with contextlib.suppress(OSError):
                st = os.lstat(p)
                if not os.path.islink(p):
                    os.chmod(p, (st.st_mode & 0o7777) | 0o444 | (0o111 if st.st_mode & 0o100 else 0))


def _default_runner(argv: list[str], *, env: dict, cwd: str, timeout: int) -> tuple[int, str]:
    from . import owned_process

    try:
        proc = owned_process.run(argv, env=env, cwd=cwd, timeout=timeout, capture_output=True,
                                 text=True, sandboxed=False)
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "npx is not installed"


def ensure(version: Optional[str], *, base: Optional[Path] = None,
           timeout: Optional[int] = None,
           runner: Optional[Callable[..., tuple[int, str]]] = None) -> dict:
    """Make Chromium for Playwright `version` present in the shared cache. Returns
    {"ok", "cached", "seconds", "path", "detail"}; never raises."""
    started = time.monotonic()
    if not version or not _VERSION.match(version):
        return {"ok": False, "cached": False, "seconds": 0.0, "path": None,
                "detail": "no Playwright version found in the app's node_modules"}
    base = base or browsers_dir()
    if base is None:
        return {"ok": False, "cached": False, "seconds": 0.0, "path": None,
                "detail": "no shared browser cache on this install"}
    marker = _marker(base, version)
    if marker.is_file():
        return {"ok": True, "cached": True, "seconds": 0.0, "path": str(base),
                "detail": f"chromium for playwright {version} already cached"}
    limit = int(timeout or install_timeout())
    deadline = started + limit
    try:
        fd = os.open(str(base / ".install.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        return {"ok": False, "cached": False, "seconds": 0.0, "path": str(base),
                "detail": f"cannot lock the browser cache: {exc}"}
    try:
        while True:                      # wait for another build's download, bounded
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    return {"ok": False, "cached": False, "path": str(base),
                            "seconds": round(time.monotonic() - started, 1),
                            "detail": "another build is still downloading the browsers"}
                time.sleep(2)
        if marker.is_file():             # the build we waited for installed it
            return {"ok": True, "cached": True, "path": str(base),
                    "seconds": round(time.monotonic() - started, 1),
                    "detail": f"chromium for playwright {version} installed by another build"}
        npm_cache = base.parent / "playwright-npm"
        npm_cache.mkdir(exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(npm_cache, 0o700)
        env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(base),
               "npm_config_cache": str(npm_cache), "npm_config_update_notifier": "false"}
        remaining = max(60, int(deadline - time.monotonic()))
        work = tempfile.mkdtemp(prefix="boss-pw-")
        try:
            code, out = (runner or _default_runner)(
                ["npx", "--yes", f"playwright@{version}", "install", "chromium"],
                env=env, cwd=work, timeout=remaining)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        seconds = round(time.monotonic() - started, 1)
        if code != 0:
            log.warning("playwright %s browser install failed (%s): %s", version, code, out[-300:])
            return {"ok": False, "cached": False, "seconds": seconds, "path": str(base),
                    "detail": f"browser install failed (exit {code}): {out[-300:]}"}
        _open_modes(base)
        marker.write_text(f"{version}\n", encoding="utf-8")
        os.chmod(marker, 0o644)
        log.info("playwright %s chromium installed into %s in %.1fs", version, base, seconds)
        return {"ok": True, "cached": False, "seconds": seconds, "path": str(base),
                "detail": f"chromium for playwright {version} installed once for this BOSS"}
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "prime":
        print("usage: python -m boss_engine.services.playwright_cache prime <playwright version>")
        return 2
    logging.basicConfig(level=logging.INFO)
    result = ensure(argv[1])
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
