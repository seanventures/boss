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
#: The e2e overlays, layered on the same file (so their relative paths resolve the same way).
OVERLAYS = sorted((ROOT / "tests/e2e").rglob("*.yml"))
#: Every host path a service may mount, relative to the install, and why. All read-only.
ALLOWED = {
    ".boss/selfsrc": "the self-source mirror and the install record",
    ".boss/import": "the one folder Import local code may read",
    ".boss/extra-ca.pem": "the company CA file",
    "scripts/install/initdb": "Postgres's first-start SQL",
    "scripts/install/monitoring/prometheus.yml": "the bundled Prometheus config",
    "scripts/install/monitoring/grafana/provisioning": "the bundled Grafana provisioning",
}
#: The e2e overlays' own fixtures (test stacks only).
ALLOWED_E2E = {"tests/e2e/stubs", "tests/e2e/compose"}


class _Loader(yaml.SafeLoader):
    """Compose's merge tags (`!reset`, `!override`) read as plain values."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: (
    loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else
    loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else
    loader.construct_scalar(node)))


def _binds(path: Path = COMPOSE) -> list[tuple[str, str, str, bool]]:
    """(service, host source as written, its target, read-only) for every bind mount."""
    doc = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader) or {}
    out = []
    for name, service in (doc.get("services") or {}).items():
        for volume in (service or {}).get("volumes") or []:
            if isinstance(volume, str):
                source, _, rest = volume.partition(":")
                if source.startswith((".", "/", "~", "$")):
                    target, _, mode = rest.partition(":")
                    out.append((name, source, target, "ro" in mode.split(",")))
            elif isinstance(volume, dict) and volume.get("type") == "bind":
                out.append((name, str(volume["source"]), str(volume["target"]),
                            bool(volume.get("read_only"))))
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
    for service, source, target, _ in binds:
        inside = _in_install(source)
        assert inside in ALLOWED, (
            f"{service} mounts {source} ({inside or 'the install checkout itself'}) at {target}: "
            "the install checkout is never mounted into a container; BOSS reads where it was "
            "installed from .boss/selfsrc/install.json and its source from the mirror")
        assert ".git" not in PurePosixPath(inside).parts


def test_every_host_mount_is_read_only():
    """A container that could write the mirror, the record or the installer's SQL could change
    what the host later builds or trusts."""
    for path in [COMPOSE, *OVERLAYS]:
        for service, source, target, read_only in _binds(path):
            assert read_only, f"{path.name}: {service} mounts {source} at {target} read-write"


def test_the_e2e_overlays_mount_only_their_fixtures():
    assert OVERLAYS, "the e2e overlays were not found"
    for path in OVERLAYS:
        for service, source, target, _ in _binds(path):
            inside = _in_install(source)
            assert inside in set(ALLOWED) | ALLOWED_E2E, f"{path.name}: {service} mounts {source}"


def test_the_guard_catches_the_checkout_and_a_writable_mount(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("services:\n  api:\n    volumes:\n      - ../..:/src:ro\n"
                   "      - ../../.boss/selfsrc:/var/lib/boss/private/selfsrc\n")
    found = _binds(bad)
    assert _in_install(found[0][1]) not in ALLOWED
    assert found[1][3] is False


def test_the_engine_sandbox_overlay_mounts_nothing():
    generator = (ROOT / "scripts/install/compose_files.sh").read_text(encoding="utf-8")
    assert "volumes" not in generator


def test_the_images_carry_no_git_history():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".git" in [line.strip() for line in ignored]
    assert "COPY .git" not in (ROOT / "Dockerfile.community").read_text(encoding="utf-8")
