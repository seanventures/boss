"""BOSS code never runs as root in its own container (round 3, R3-STACK S1; boss_engine/_runtime_user).

The real-stack build walk found every fresh install's FIRST build failing at its end with
`Permission denied` on /var/lib/boss/artifacts/<project>/art_….json: install.sh seeds through a
`docker compose exec api` — root by default, since the api service is `user: "0:0"` for its
entrypoint's network fence — so the seed's artifact directory was root's. The fix is the class:
importing boss_engine as root inside the image becomes the image's runtime user first, and the
entrypoint heals what an older root exec left behind.
"""
from __future__ import annotations

import pwd
import re
from pathlib import Path

import pytest

from boss_engine import _runtime_user as ru

ROOT = Path(__file__).resolve().parents[2]


class _Calls:
    def __init__(self):
        self.done: list[tuple[str, object]] = []


@pytest.fixture
def fake_root(monkeypatch):
    calls = _Calls()
    monkeypatch.setattr(ru.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: pwd.struct_passwd(
        (name, "x", 4242, 4343, "", f"/home/{name}", "/bin/sh")))
    monkeypatch.setattr(ru.os, "getgrouplist", lambda name, gid: [gid])
    monkeypatch.setattr(ru.os, "setgroups", lambda g: calls.done.append(("setgroups", tuple(g))))
    monkeypatch.setattr(ru.os, "setgid", lambda g: calls.done.append(("setgid", g)))
    monkeypatch.setattr(ru.os, "setuid", lambda u: calls.done.append(("setuid", u)))
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.delenv(ru.KEEP_ROOT_ENV, raising=False)
    return calls


def test_root_in_the_image_becomes_the_runtime_user(fake_root, monkeypatch):
    monkeypatch.setenv(ru.RUNTIME_USER_ENV, "boss")
    assert ru.become_runtime_user() is True
    # groups, then gid, then uid: dropping the uid first would forbid the rest
    assert [c[0] for c in fake_root.done] == ["setgroups", "setgid", "setuid"]
    assert ("setuid", 4242) in fake_root.done and ("setgid", 4343) in fake_root.done
    assert ru.os.environ["HOME"] == "/home/boss" and ru.os.environ["USER"] == "boss"


def test_outside_the_image_nothing_changes(fake_root, monkeypatch):
    monkeypatch.delenv(ru.RUNTIME_USER_ENV, raising=False)
    assert ru.become_runtime_user() is False and not fake_root.done


def test_not_root_nothing_changes(fake_root, monkeypatch):
    monkeypatch.setenv(ru.RUNTIME_USER_ENV, "boss")
    monkeypatch.setattr(ru.os, "geteuid", lambda: 100)
    assert ru.become_runtime_user() is False and not fake_root.done


def test_keep_root_is_an_explicit_escape(fake_root, monkeypatch):
    monkeypatch.setenv(ru.RUNTIME_USER_ENV, "boss")
    monkeypatch.setenv(ru.KEEP_ROOT_ENV, "1")
    assert ru.become_runtime_user() is False and not fake_root.done


def test_a_failed_drop_refuses_instead_of_running_as_root(fake_root, monkeypatch):
    monkeypatch.setenv(ru.RUNTIME_USER_ENV, "boss")

    def nope(_uid):
        raise PermissionError("no CAP_SETUID")
    monkeypatch.setattr(ru.os, "setuid", nope)
    with pytest.raises(SystemExit):
        ru.become_runtime_user()


def test_the_package_drops_on_import_and_the_image_names_its_user():
    init = (ROOT / "engine" / "boss_engine" / "__init__.py").read_text()
    assert "_become_runtime_user()" in init
    docker = (ROOT / "Dockerfile.community").read_text()
    runtime = docker.split("AS python-runtime", 1)[1].split("\nFROM ", 1)[0]
    assert re.search(r"BOSS_RUNTIME_USER=boss\b", runtime), "the runtime stage must name its user"


def test_the_entrypoint_heals_root_owned_files_in_the_service_volumes():
    ep = (ROOT / "scripts" / "sandbox" / "entrypoint.sh").read_text()
    assert re.search(r'heal_ownership "\$d" boss', ep)


def _heal_function() -> str:
    ep = (ROOT / "scripts" / "sandbox" / "entrypoint.sh").read_text()
    return ep.split("# --- heal_ownership (begin)", 1)[1].split("# --- heal_ownership (end)", 1)[0]


def test_the_ownership_heal_never_follows_a_symlink(tmp_path):
    """D2 (id-sec-final2): the heal runs as root over volumes other containers write. A link planted
    there (-> /etc/shadow, -> a directory of root's) must never have its TARGET chowned, and no
    path is ever handed to chown without -h. Run the entrypoint's own function with a stub chown
    that records what it was asked to do (the real one needs root)."""
    import os
    import shutil
    import subprocess

    if shutil.which("sh") is None or shutil.which("find") is None:
        import pytest
        pytest.skip("no POSIX shell")
    vol = tmp_path / "artifacts"
    (vol / "proj" / "sub").mkdir(parents=True)
    (vol / "proj" / "file.txt").write_text("x")
    outside = tmp_path / "outside"
    (outside / "secret_dir").mkdir(parents=True)
    (outside / "shadow").write_text("root only")
    os.symlink(outside / "shadow", vol / "proj" / "evil")
    os.symlink(outside / "secret_dir", vol / "proj" / "evil_dir")
    os.symlink(outside, vol / "linked_tree")
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "chown.log"
    (stub / "chown").write_text(f'#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" >> "{log}"\n')
    (stub / "chown").chmod(0o755)
    script = tmp_path / "heal.sh"
    # "nobody" owns nothing here, so every real entry is a candidate, as root's are in a volume.
    script.write_text(_heal_function() + f'\nheal_ownership "{vol}" nobody\n')
    subprocess.run(["sh", str(script)], check=True,
                   env={**os.environ, "PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin"})
    calls = log.read_text().splitlines()
    assert calls, "the heal chowned nothing"
    named = []
    for line in calls:
        cwd, args = line.split("|", 1)
        parts = args.split()
        assert parts[0] == "-h", line                    # never dereference
        for name in parts[2:]:
            path = os.path.normpath(os.path.join(cwd, name))
            assert not os.path.islink(path), f"a symlink was handed to chown: {path}"
            assert path.startswith(str(vol.resolve())) or path.startswith(str(vol)), path
            named.append(os.path.basename(path))
    assert {"proj", "sub", "file.txt"} <= set(named)
    assert not {"evil", "evil_dir", "linked_tree", "shadow", "secret_dir"} & set(named)
