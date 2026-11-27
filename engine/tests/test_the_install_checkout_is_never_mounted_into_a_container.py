"""THE DESIGN RULE BEHIND BOSS KNOWING WHERE IT WAS INSTALLED (ENT-REC-54, BOSS_FIXES_ITSELF §6.1).

BOSS learns its install path from the installer's record (.boss/selfsrc/install.json) and reads
its own source from the mirror beside it. The install checkout itself is NEVER mounted into a
container: a container that could write it could change the code BOSS runs outside every gate,
and one that could read `.git` would hold the operator's remotes and local history. The images
carry no `.git` either.

This pins it: every host path any Compose service mounts is one of a short, named list of
BOSS-owned files and folders inside the install (never the checkout's root, an ancestor of it,
`.git`, or anything outside it), and the generated overlay mounts nothing.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "scripts/install/community-compose.yml"
#: Every host path a service may mount, relative to the install, and why.
ALLOWED = {
    ".boss/selfsrc": "the self-source mirror and the install record, read-only",
    ".boss/import": "the one folder Import local code may read, read-only",
    ".boss/extra-ca.pem": "the company CA file, read-only",
    "scripts/install/initdb": "Postgres's first-start SQL",
    "scripts/install/monitoring/prometheus.yml": "the bundled Prometheus config",
    "scripts/install/monitoring/grafana/provisioning": "the bundled Grafana provisioning",
}


def _binds() -> list[tuple[str, str, str]]:
    """(service, host source as written, its target) for every bind mount in the file."""
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    out = []
    for name, service in (doc.get("services") or {}).items():
        for volume in service.get("volumes") or []:
            if isinstance(volume, str):
                source, _, rest = volume.partition(":")
                if source.startswith((".", "/", "~", "$")):
                    out.append((name, source, rest.split(":")[0]))
            elif volume.get("type") == "bind":
                out.append((name, str(volume["source"]), str(volume["target"])))
    return out


def _in_install(source: str) -> str:
    """The source as a path inside the install (the file sits in scripts/install/), or the raw
    source when it leaves the install."""
    if source.startswith(("/", "~", "$")):
        return source
    parts: list[str] = ["scripts", "install"]
    for part in PurePosixPath(source).parts:
        if part == "..":
            if not parts:
                return source
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def test_only_named_boss_paths_are_mounted_never_the_checkout():
    binds = _binds()
    assert binds, "the compose file was not read"
    for service, source, target in binds:
        inside = _in_install(source)
        assert inside in ALLOWED, (
            f"{service} mounts {source} ({inside or 'the install checkout itself'}) at {target}: "
            "the install checkout is never mounted into a container; BOSS reads where it was "
            "installed from .boss/selfsrc/install.json and its source from the mirror")
        assert ".git" not in PurePosixPath(inside).parts


def test_the_self_source_reaches_the_containers_read_only():
    for service, source, target in _binds():
        if _in_install(source) in (".boss/selfsrc", ".boss/import"):
            text = COMPOSE.read_text(encoding="utf-8")
            assert f"{source}:{target}:ro" in text, f"{service} must mount {source} read-only"


def test_the_engine_sandbox_overlay_mounts_nothing():
    generator = (ROOT / "scripts/install/compose_files.sh").read_text(encoding="utf-8")
    assert "volumes" not in generator


def test_the_images_carry_no_git_history():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".git" in [line.strip() for line in ignored]
    assert "COPY .git" not in (ROOT / "Dockerfile.community").read_text(encoding="utf-8")
