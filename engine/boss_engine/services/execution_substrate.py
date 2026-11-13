"""Canonical execution-substrate vocabulary for code runs and previews.

The configured provider is an input. The normalized value stored on a run or
preview is the audit record of where that particular operation actually ran.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class ExecutionSubstrate(str, Enum):
    LOCAL_SUBPROCESS = "local_subprocess"
    MICROVM = "microvm"
    PREVIEW_HOST = "preview_host"
    PREVIEW_DOCKER = "preview_docker"
    PREVIEW_ROOTFS = "preview_rootfs"
    # Pluggable execution environments (BOSSAPP-824). 'ssh' runs in this phase;
    # the others are recognized vocabulary whose drivers land in phase 2 — the
    # factory refuses them in words, but a run that DID happen on one must be
    # able to record it.
    SSH = "ssh"
    KUBERNETES = "kubernetes"
    E2B = "e2b"
    DAYTONA = "daytona"


_LOCAL = {"local", "subprocess", "local_subprocess"}
_MICROVM = {"microvm", "firecracker", "gvisor"}
_ENVIRONMENT = {
    "ssh": ExecutionSubstrate.SSH,
    "kubernetes": ExecutionSubstrate.KUBERNETES,
    "e2b": ExecutionSubstrate.E2B,
    "daytona": ExecutionSubstrate.DAYTONA,
}


def normalize_build_substrate(value: Optional[str]) -> ExecutionSubstrate:
    normalized = (value or "local").strip().lower()
    if normalized in _LOCAL:
        return ExecutionSubstrate.LOCAL_SUBPROCESS
    if normalized in _MICROVM:
        return ExecutionSubstrate.MICROVM
    if normalized in _ENVIRONMENT:
        return _ENVIRONMENT[normalized]
    raise ValueError(f"Unsupported execution substrate: {value!r}")


def effective_substrates(settings) -> dict[str, str]:
    """Return the validated native/governed startup configuration."""
    return {
        "native": normalize_build_substrate(settings.sandbox_provider).value,
        "governed": normalize_build_substrate(settings.governed_sandbox_provider).value,
    }
