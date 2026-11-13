"""Previews at their own HTTPS origin through the boss-expose helper (BOSSAPP-860).

Previews are served by BOSS's preview origin (preview_origin.py), one host per preview under a
base BOSS controls. An app that needs a REAL origin of its own — an OAuth callback registered at
an https URL, a service worker, hot reload over a websocket on a tailnet — still breaks there.
With BOSS_PREVIEW_EXPOSE=on, a preview whose port is up is handed to the boss-expose helper
(scripts/expose/boss-expose: the privilege split boss-sandbox-exec uses — BOSS asks by argv, the
helper alone may change what the host serves), which maps it to a tailnet HTTPS port
(`tailscale serve`) or a Caddy subdomain.

FAIL CLOSED. The preview is marked running only after BOSS has fetched the exposed URL over HTTPS
with certificate verification and got an answer; a helper that refuses, a backend that fails, or
a URL that does not answer within the window unmaps whatever was mapped and fails the preview
(failure_kind 'expose_failed'). It never falls back to "exposed but unchecked".

Off (the default): nothing is called, the preview row and response are exactly as before.
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

ENABLE_ENV = "BOSS_PREVIEW_EXPOSE"
HELPER_ENV = "BOSS_EXPOSE_HELPER"
SUDO_ENV = "BOSS_EXPOSE_SUDO"
PROBE_ENV = "BOSS_EXPOSE_PROBE_SECONDS"
DEFAULT_HELPER = "/usr/local/libexec/boss/boss-expose"
DEFAULT_PROBE_SECONDS = 60
FAILURE_KIND = "expose_failed"


class ExposeFailed(RuntimeError):
    """The preview could not be given its own verified origin; it must not be marked ready."""


def enabled() -> bool:
    return (os.environ.get(ENABLE_ENV) or "").strip().lower() in ("1", "on", "true", "yes")


def helper_argv(*args: str) -> list[str]:
    helper = (os.environ.get(HELPER_ENV) or DEFAULT_HELPER).strip()
    base = ["sudo", "-n", helper] if (os.environ.get(SUDO_ENV) or "").strip() in ("1", "on") \
        else [helper]
    return [*base, *args]


def label_for(preview_id: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", str(preview_id).lower())[:40].strip("-") or "preview"


def _call(*args: str) -> dict[str, Any]:
    from . import owned_process

    try:
        done = owned_process.boss_run(helper_argv(*args), capture_output=True, text=True,
                                      timeout=90)
    except FileNotFoundError:
        raise ExposeFailed(f"the boss-expose helper is not installed "
                           f"({os.environ.get(HELPER_ENV) or DEFAULT_HELPER})") from None
    except subprocess.TimeoutExpired:
        raise ExposeFailed("the boss-expose helper did not answer in 90s") from None
    try:
        said = json.loads((done.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        said = {}
    if done.returncode != 0 or not said.get("ok"):
        raise ExposeFailed(str(said.get("error") or (done.stderr or "").strip()[-300:]
                               or f"boss-expose exited {done.returncode}"))
    return said


def probe(url: str, *, timeout: float = DEFAULT_PROBE_SECONDS,
          opener: Optional[Callable[..., Any]] = None, sleep: Callable[[float], None] = time.sleep
          ) -> Optional[int]:
    """GET `url` over verified HTTPS until it answers below 500; its status, else None."""
    if not url.startswith("https://"):
        return None
    context = ssl.create_default_context()
    deadline = time.monotonic() + timeout
    while True:
        try:
            if opener is not None:
                resp = opener(url, timeout=10)
            else:
                resp = urllib.request.urlopen(url, timeout=10, context=context)
            with resp:
                status = int(getattr(resp, "status", 200))
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (urllib.error.URLError, OSError, ssl.SSLError):
            status = None
        if status is not None and status < 500:
            return status
        if time.monotonic() >= deadline:
            return None
        sleep(1.0)


def attach(preview_id: str, port: int, *, probe_fn: Callable[[str], Optional[int]] = None
           ) -> Optional[dict[str, Any]]:
    """Map the preview's port and verify it from outside. None when exposing is off.
    Raises ExposeFailed (after unmapping) when it cannot be verified."""
    if not enabled():
        return None
    label = label_for(preview_id)
    mapped = _call("map", label, str(int(port)))
    url = str(mapped.get("url") or "")
    window = float(os.environ.get(PROBE_ENV) or DEFAULT_PROBE_SECONDS)
    status = (probe_fn or (lambda u: probe(u, timeout=window)))(url)
    if status is None:
        detach(preview_id)
        raise ExposeFailed(f"{url or 'the exposed URL'} did not answer over HTTPS within "
                           f"{int(window)}s, so the preview is not marked ready")
    return {"url": url, "label": label, "status": status}


def detach(preview_id: str) -> None:
    """Remove the preview's mapping. Never raises; a no-op when exposing is off."""
    if not enabled() or not preview_id:
        return
    try:
        _call("unmap", label_for(preview_id))
    except ExposeFailed as exc:
        _log.warning("preview %s: could not unexpose: %s", preview_id, exc)
