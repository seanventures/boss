"""Helpers for plugin authors (Python SDK). Optional: a plugin needs only `contract`.

    from boss_engine.platform.sdk import free_port, docker_labels_args, wait_for_tcp, slug
"""
from __future__ import annotations

import re
import secrets as _random
import shlex
import shutil
import socket
import subprocess
import time
from typing import Iterable

from .contract import HookContext

#: Every container, volume and network a plugin creates carries these labels, so BOSS (and the
#: contract harness) can find and clean up what a resource owns.
LABEL_RESOURCE = "boss.infra.resource"
LABEL_PLUGIN = "boss.infra.plugin"


def free_port() -> int:
    """A TCP port free on this machine right now (bind to 0 and read it back)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                              timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def wait_for_tcp(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def slug(text: str, limit: int = 48) -> str:
    """A docker-safe name from a resource id: `deploy/helpdesk/db` -> `deploy-helpdesk-db`."""
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")[:limit] or "resource"


def resource_labels(ctx: HookContext) -> dict[str, str]:
    return {LABEL_RESOURCE: ctx.resource_id, LABEL_PLUGIN: ctx.plugin_id or "unknown", **ctx.labels}


def docker_labels_args(ctx: HookContext) -> str:
    """`--label k=v ...` for `docker run` / `docker volume create`, shell-quoted."""
    return " ".join(f"--label {shlex.quote(f'{k}={v}')}" for k, v in resource_labels(ctx).items())


def generate_password(nbytes: int = 24) -> str:
    return _random.token_urlsafe(nbytes)


def redact_values(text: str, values: Iterable[str]) -> str:
    """Replace every known secret value (longest first) with `***`, then BOSS's own patterns."""
    out = text or ""
    for v in sorted({v for v in values if v and len(v) >= 4}, key=len, reverse=True):
        out = out.replace(v, "***")
    try:
        from ..services.redaction import redact_secrets
        out = redact_secrets(out)
    except Exception:  # the SDK must work outside a full BOSS install
        pass
    return out


def bind_address(ctx: HookContext) -> str:
    """127.0.0.1 unless the request asked for (approved) public access."""
    return "0.0.0.0" if ctx.resource.public else "127.0.0.1"
