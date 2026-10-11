"""Source/inert-process recovery contracts; no Docker/provider execution here."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "scripts/install"


def load(name):
    spec = importlib.util.spec_from_file_location(name, INSTALL / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load("recovery_guard")
recovery = load("recovery_compose")


def child(root, sentinel, *args):
    # An isolated copy keeps the production fixed path intact. Runtime CLI has
    # no environment/path override; only this test's source copy changes it.
    path = root.parent / "guard-copy.py"
    path.write_text((INSTALL / "recovery_guard.py").read_text().replace(
        'Path("/var/lib/boss/recovery")', "Path(" + repr(str(root)) + ")"))
    return subprocess.run([sys.executable, str(path), sys.executable, "-c",
                           "from pathlib import Path; Path(" + repr(str(sentinel)) + ").touch()", *args],
                          capture_output=True, text=True, timeout=10)


def test_fresh_empty_root_executes_child(tmp_path):
    root = tmp_path / "recovery"
    root.mkdir()
    sentinel = tmp_path / "ran"
    assert child(root, sentinel).returncode == 0
    assert sentinel.exists()


@pytest.mark.parametrize("state", ["held", "malformed", "clear", "duplicate", "oversize", "unreadable", "marker_symlink", "marker_dir", "root_symlink", "root_file", "missing_root", "root_unreadable", "unexpected_file"])
def test_unsafe_or_held_state_never_executes_child_on_restarts(tmp_path, state):
    root = tmp_path / "recovery"
    root.mkdir()
    marker = root / "hold.json"
    marker.write_text(json.dumps({"interface": guard.INTERFACE, "state": "held"}))
    if state == "malformed":
        marker.write_text("{secret-do-not-print")
    elif state == "clear":
        marker.write_text(json.dumps({"interface": guard.INTERFACE, "state": "clear"}))
    elif state == "duplicate":
        marker.write_text('{"interface":"boss-recovery-hold/v1","state":"clear","state":"held"}')
    elif state == "oversize":
        marker.write_text("x" * 4097)
    elif state == "unreadable":
        marker.chmod(0)
    elif state in ("marker_symlink", "marker_dir"):
        marker.unlink()
        if state == "marker_symlink":
            marker.symlink_to(tmp_path / "missing")
        else:
            marker.mkdir()
    elif state == "root_symlink":
        root.rename(tmp_path / "other")
        root.symlink_to(tmp_path / "other", target_is_directory=True)
    elif state in ("root_file", "missing_root"):
        marker.unlink()
        root.rmdir()
        if state == "root_file":
            root.write_text("not-a-directory")
    elif state == "root_unreadable":
        root.chmod(0)
    elif state == "unexpected_file":
        marker.rename(root / "unknown.json")
    sentinel = tmp_path / "ran"
    for _ in range(2):
        result = child(root, sentinel)
        assert result.returncode == 78
        assert "startup refused" in result.stderr
        assert "secret-do-not-print" not in result.stderr
        assert not sentinel.exists()
    if state == "root_unreadable":
        root.chmod(0o700)
    if state == "unreadable":
        marker.chmod(0o600)


def test_guard_has_no_environment_resume_or_path_switch(tmp_path, monkeypatch):
    root = tmp_path / "recovery"
    root.mkdir()
    (root / "hold.json").write_text('{"interface":"boss-recovery-hold/v1","state":"held"}')
    monkeypatch.setenv("BOSS_RECOVERY_ROOT", str(tmp_path / "empty"))
    monkeypatch.setenv("BOSS_RECOVERY_RESUME", "1")
    assert child(root, tmp_path / "ran", "--resume").returncode == 78


def test_marker_write_is_exclusive_across_two_racing_restores(tmp_path):
    marker = tmp_path / "hold.json"
    command = ['sh', '-c', 'set -euC; test ! -L "$1"; umask 077; cat > "$1"', 'writer', str(marker)]
    first = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first.communicate('{"state":"held","attempt":"first"}', timeout=10)
    second.communicate('{"state":"held","attempt":"second"}', timeout=10)
    assert sorted([first.returncode, second.returncode])[0] == 0
    assert (first.returncode == 0) != (second.returncode == 0)
    saved = json.loads(marker.read_text())
    assert saved['state'] == 'held'
    assert saved['attempt'] == ('first' if first.returncode == 0 else 'second')


@pytest.mark.parametrize("role", list(recovery.ROLES))
def test_held_actual_role_launch_never_calls_child_or_privileged_initializers(tmp_path, role):
    root = tmp_path / "recovery"
    root.mkdir()
    (root / "hold.json").write_text('{"interface":"boss-recovery-hold/v1","state":"held"}')
    sentinel = tmp_path / "unexpected-child-or-init"
    guard_copy = tmp_path / "guard.py"
    guard_copy.write_text((INSTALL / "recovery_guard.py").read_text().replace(
        'Path("/var/lib/boss/recovery")', "Path(" + repr(str(root)) + ")"))
    config = yaml.safe_load((INSTALL / "community-compose.yml").read_text())
    command = config["services"][role]["command"]
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    for name in ("python", "id", "iptables", "chown", "setpriv"):
        path = binary_dir / name
        path.write_text("#!/bin/sh\ntouch " + str(sentinel) + "\nexit 99\n")
        path.chmod(0o700)
    if role == "api":
        entry = tmp_path / "entrypoint.sh"
        entry.write_text((REPO / "scripts/sandbox/entrypoint.sh").read_text().replace(recovery.GUARD, str(guard_copy)))
        args = ["sh", str(entry), *command]
    else:
        args = [sys.executable, str(guard_copy), *command]
    result = subprocess.run(args, env={**os.environ, "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"]}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 78
    assert not sentinel.exists()


def test_actual_role_contracts_guard_before_application_or_api_initialization():
    config = yaml.safe_load((INSTALL / "community-compose.yml").read_text())
    dockerfile = (REPO / "Dockerfile.community").read_text()
    entrypoint = (REPO / "scripts/sandbox/entrypoint.sh").read_text()
    check = "python3 " + recovery.GUARD + " --check"
    assert entrypoint.index(check) < entrypoint.index('UID_RANGE=')
    assert entrypoint.index(check) < entrypoint.index('id -u')
    assert dockerfile.index('ENTRYPOINT ["python3",') < dockerfile.index('FROM python-runtime AS api')
    assert 'ENTRYPOINT ["/usr/local/bin/boss-entrypoint"]' in dockerfile
    for role, command in recovery.ROLES.items():
        service = config["services"][role]
        assert "boss_community_recovery:" + recovery.ROOT + ":ro" in service["volumes"]
        assert "BOSS_RECOVERY_API_IMAGE_ID" in service["image"]
        assert service["command"] == command
        if role != "api":
            assert service["entrypoint"] == ["python3", recovery.GUARD]
    assert not any("identity" in v for v in config["services"]["mcp-tools"]["volumes"])
    assert config["services"]["api"]["user"] == "0:0"
    assert config["services"]["api"]["cap_add"] == ["NET_ADMIN"]


def resolved_config(project):
    volumes = {"boss_community_" + k: {"name": project + "_boss_community_" + k} for k in ("pg", "identity", "artifacts", "recovery")}
    mount = {"type": "volume", "source": "boss_community_recovery", "target": recovery.ROOT, "read_only": True}
    services = {role: {"image": "fixture-" + role, "volumes": [copy.deepcopy(mount)], "command": command} for role, command in recovery.ROLES.items()}
    for role in ("worker", "egress", "mcp-tools"):
        services[role]["entrypoint"] = ["python3", recovery.GUARD]
    services["api"]["volumes"] += [{"type": "volume", "source": "boss_community_" + k, "target": "/var/lib/boss/" + k} for k in ("identity", "artifacts")]
    services["db"] = {"image": "postgres:16-alpine", "volumes": [{"type": "volume", "source": "boss_community_pg", "target": "/var/lib/postgresql/data"}]}
    return {"services": services, "volumes": volumes}


@pytest.mark.parametrize("changed", ["writable", "different_volume", "external", "mcp_identity", "wrong_entrypoint", "wrong_command"])
def test_unsafe_compose_refused(changed):
    config = resolved_config("fixture")
    layout = load("backup_archive").layout(config)
    worker = config["services"]["worker"]
    if changed == "writable":
        worker["volumes"][0]["read_only"] = False
    elif changed == "different_volume":
        worker["volumes"][0]["source"] = "boss_community_artifacts"
    elif changed == "external":
        config["volumes"]["boss_community_recovery"]["external"] = True
    elif changed == "mcp_identity":
        config["services"]["mcp-tools"]["volumes"].append({"target": "/var/lib/boss/identity", "source": "boss_community_identity"})
    elif changed == "wrong_entrypoint":
        worker["entrypoint"] = ["/usr/local/bin/boss-entrypoint"]
    else:
        worker["command"] = recovery.ROLES["api"]
    with pytest.raises(SystemExit):
        recovery.recovery_layout(config, layout, "fixture", False)


@pytest.mark.parametrize("offline", [False, True])
def test_volume_plan_uses_reviewed_logical_names_and_physical_target_mapping(tmp_path, offline):
    config = resolved_config("fixture")
    names = {"boss_community_pg": "database", "boss_community_identity": "vault", "boss_community_artifacts": "results", "boss_community_recovery": "hold_state"}
    config["volumes"] = {names[key]: value for key, value in config["volumes"].items()}
    for service in config["services"].values():
        for mount in service["volumes"]:
            mount["source"] = names[mount["source"]]
    manifest = {"source_commit": "a" * 40, "db_image_id": "sha256:" + "1" * 64, "api_image_id": "sha256:" + "2" * 64}
    recovery.prepare(config, load("backup_archive").layout(config), manifest, "fixture", tmp_path, offline)
    plan = json.loads((tmp_path / "volume-plan.json").read_text())
    assert plan["identity"] == {"logical": "boss_community_identity" if offline else "vault", "name": "fixture_boss_community_identity"}
    assert plan["artifacts"] == {"logical": "boss_community_artifacts" if offline else "results", "name": "fixture_boss_community_artifacts"}
    assert plan["recovery"] == {"logical": "boss_community_recovery" if offline else "hold_state", "name": "fixture_boss_community_recovery"}
    if offline:
        base = json.loads((tmp_path / "offline-compose.json").read_text())
        assert all(base["volumes"][value["logical"]]["name"] == value["name"] for value in plan.values())


@pytest.fixture
def restore_fixture(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.mkdir()
    for name in ("scripts/install/community-compose.yml", "scripts/install/recovery_guard.py", "scripts/sandbox/entrypoint.sh", "Dockerfile.community"):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / name, path)
    source = "a" * 40
    (target / ".boss-shipped-commit").write_text(source)
    for name, data in {".env": "BOSS_DB_PASSWORD=private-fixture-never-print\n", "source-commit": source, "boss.dump": "explicit inert dump"}.items():
        (backup / name).write_text(data)
        (backup / name).chmod(0o600)
    for name in ("identity.tar", "artifacts.tar"):
        with tarfile.open(backup / name, "w"):
            pass
        (backup / name).chmod(0o600)
    manifest = {"format": 1, "complete": True, "source_commit": source, "master_key_relative": "master.key", "identity_state": {"items": 0, "kcvs": []}, "key_check_value": None,
                "db_image_id": "sha256:" + "1" * 64, "api_image_id": "sha256:" + "2" * 64,
                "files": {name: hashlib.sha256((backup / name).read_bytes()).hexdigest() for name in load("backup_archive").REQUIRED}}
    recovery.write_json(backup / "manifest.json", manifest)
    (backup / "manifest.json").chmod(0o600)
    config = resolved_config("owned-fixture")
    recovery.write_json(tmp_path / "config.json", config)
    recovery.write_json(tmp_path / "hashes.json", recovery.source_hashes(target))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('''#!/usr/bin/env python3
import importlib.util, json, os, subprocess, sys
from pathlib import Path
a = sys.argv[1:]
root = Path(os.environ['HOLD_FIXTURE'])
target = root / 'target'
if (target / '.env').exists():
    # Model an ordinary base-only compose launch at the first usable config.
    # Docker creates a fresh empty volume before the guard runs when no hold
    # exists. Use the actual stdlib guard against that isolated volume.
    volume = root / 'actual-recovery-volume'
    volume.mkdir(exist_ok=True)
    held = (volume / 'hold.json').exists()
    base = target / 'scripts/install/community-compose.yml'
    db_only = base.read_text().startswith('{') and set(json.loads(base.read_text())['services']) == {'db'}
    application_started = False
    if not db_only:
        path = target / 'scripts/install/recovery_guard.py'
        if path.is_file():
            spec = importlib.util.spec_from_file_location('actual_guard', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            try: module.check_recovery(volume)
            except module.RecoveryHeld: pass
            else: application_started = True
        else: application_started = True
    observation = {'held_marker_present': held, 'db_only': db_only, 'application_started': application_started}
    with (root / 'interleaving.jsonl').open('a') as log: log.write(json.dumps(observation)+'\\n')
    if application_started:
        # Actual inert child process; no product import or provider operation.
        subprocess.run([sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).touch()', str(root / 'unsafe-application-started')], check=True)
with (root / 'trace.jsonl').open('a') as log:
    log.write(json.dumps(a) + '\\n')
mode = os.environ.get('HOLD_FAULT', '')
if a[0] == 'info': pass
elif a[:2] == ['image', 'inspect']:
    if '--format' in a:
        print('sha256:'+'2'*64+'|boss-recovery-hold/v1|'+'a'*40+'|["/usr/local/bin/boss-entrypoint"]|boss')
elif a[0] == 'ps': pass
elif a[:2] == ['volume', 'inspect']:
    if '--format' in a:
        volume = json.loads((root / 'created-volumes.json').read_text())[a[-1]]
        print(('other-project' if mode == 'volume_label' else volume['project'])+'|'+volume['logical'])
        sys.exit(0)
    sys.exit(0 if mode == 'occupied' else 1)
elif a[:2] == ['volume', 'create']:
    if mode == 'volume_create' or (mode == 'artifact_create' and a[-1].endswith('_artifacts')): sys.exit(19)
    path = root / 'created-volumes.json'
    volumes = json.loads(path.read_text()) if path.exists() else {}
    labels = [a[i+1] for i,v in enumerate(a) if v == '--label']
    volumes[a[-1]] = {'project': labels[0].split('=',1)[1], 'logical': labels[1].split('=',1)[1]}
    path.write_text(json.dumps(volumes))
elif a[0] == 'run':
    if 'python3' in a:
        h = json.loads((root / 'hashes.json').read_text())
        if mode == 'hash': h['/usr/local/libexec/boss/recovery_guard.py'] = 'wrong'
        print(json.dumps(h))
    elif 'sh' in a:
        if mode == 'marker_collision': sys.exit(19)
        data = sys.stdin.read()
        (root / 'persistent-hold.json').write_text(data)
        volume = root / 'actual-recovery-volume'
        volume.mkdir(exist_ok=True)
        (volume / 'hold.json').write_text(data)
    else: sys.stdin.read()
elif a[0] == 'compose':
    if 'config' in a:
        files = [a[i+1] for i,v in enumerate(a) if v == '-f']
        env = Path(a[a.index('--env-file')+1])
        if Path(files[0]).read_text().startswith('{'):
            c = json.loads(Path(files[0]).read_text())
        else: c = json.loads((root / 'config.json').read_text())
        if 'BOSS_RECOVERY_API_IMAGE_ID=' in env.read_text():
            for role,s in c['services'].items():
                s['image'] = 'sha256:' + ('1' if role == 'db' else '2')*64
        print(json.dumps(c))
    elif 'exec' in a:
        if 'pg_restore' in a:
            sys.stdin.read()
            if mode == 'restore': sys.exit(19)
        elif 'psql' in a: print('f')
    elif 'up' in a or 'stop' in a: pass
    else: sys.exit(91)
else: sys.exit(92)
''')
    docker.chmod(0o700)
    env = {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "HOLD_FIXTURE": str(tmp_path), "LC_ALL": "C"}
    return tmp_path, target, backup, env


def restore(fixture, *, offline=False, fault="", script=None):
    root, target, backup, env = fixture
    args = ["bash", str(script or INSTALL / "restore.sh"), "--backup", str(backup), "--dir", str(target), "--project", "owned-fixture"]
    if offline:
        args.append("--offline-data-only")
    result = subprocess.run(args, env={**env, "HOLD_FAULT": fault}, capture_output=True, text=True, timeout=20)
    assert "private-fixture-never-print" not in result.stdout + result.stderr
    trace = [json.loads(line) for line in (root / "trace.jsonl").read_text().splitlines()] if (root / "trace.jsonl").exists() else []
    return result, trace


def test_preguard_normal_restore_refuses_before_docker_or_target_resources(restore_fixture):
    root, target, _backup, _env = restore_fixture
    (target / "scripts/install/recovery_guard.py").unlink()
    result, trace = restore(restore_fixture)
    assert result.returncode != 0 and "--offline-data-only" in result.stderr
    assert trace == []
    assert not (target / ".env").exists()
    assert not (root / "persistent-hold.json").exists()


def test_offline_restore_refuses_linked_compose_and_preserves_original(restore_fixture):
    root, target, _backup, _env = restore_fixture
    source = target / "scripts/install/community-compose.yml"
    original = root / "original-compose.yml"
    source.rename(original)
    before = original.read_bytes()
    source.symlink_to(original)
    result, trace = restore(restore_fixture, offline=True)
    assert result.returncode != 0 and "linked source" in result.stderr
    assert trace == []
    assert original.read_bytes() == before
    assert not (target / ".env").exists()


@pytest.mark.parametrize("fault", ["hash", "occupied"])
def test_guard_image_or_namespace_refusal_precedes_target_resources(restore_fixture, fault):
    root, target, _backup, _env = restore_fixture
    result, trace = restore(restore_fixture, fault=fault)
    assert result.returncode != 0
    assert not (target / ".env").exists()
    assert not (root / "persistent-hold.json").exists()
    assert not any("up" in call for call in trace)


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("archive_env_mode", [0o600, 0o400])
def test_restore_persists_hold_before_data_and_base_contract_without_override(restore_fixture, offline, archive_env_mode):
    root, target, backup, _env = restore_fixture
    (backup / ".env").chmod(archive_env_mode)
    if offline:
        (target / "scripts/install/recovery_guard.py").unlink()
    result, trace = restore(restore_fixture, offline=offline)
    assert result.returncode == 0, result.stderr
    assert "Application startup remains HELD" in result.stdout
    assert "up -d" not in result.stdout
    marker_call = next(i for i,a in enumerate(trace) if a[0] == "run" and "sh" in a)
    data_call = next(i for i,a in enumerate(trace) if a[0] == "run" and "tar" in a)
    db_call = next(i for i,a in enumerate(trace) if a[0] == "compose" and "up" in a)
    assert marker_call < data_call < db_call
    creates = [(i,a) for i,a in enumerate(trace) if a[:2] == ["volume", "create"]]
    assert len(creates) == 3
    assert creates[0][0] < marker_call < creates[1][0] < data_call
    for _index, call in creates:
        assert "com.docker.compose.project=owned-fixture" in call
        assert any(label.startswith("com.docker.compose.volume=") for label in call)
    assert trace[db_call][-1] == "db"
    assert not any("build" in a or "pull" == a[0] for a in trace)
    observations = [json.loads(line) for line in (root / "interleaving.jsonl").read_text().splitlines()]
    assert observations and all(item["held_marker_present"] and not item["application_started"] for item in observations)
    assert json.loads((root / "persistent-hold.json").read_text())["state"] == "held"
    manifest = json.loads((backup / "manifest.json").read_text())
    env = (target / ".env").read_text()
    assert (target / ".env").stat().st_mode & 0o777 == 0o600
    assert "BOSS_RECOVERY_API_IMAGE_ID=" + manifest["api_image_id"] in env
    assert "BOSS_RECOVERY_DB_IMAGE_ID=" + manifest["db_image_id"] in env
    assert "COMPOSE_PROJECT_NAME=owned-fixture" in env
    base = yaml.safe_load((target / "scripts/install/community-compose.yml").read_text())
    if offline:
        assert set(base["services"]) == {"db"}
        assert "private-fixture-never-print" not in json.dumps(base)
        assert (target / ".boss-archived-compose.yml").exists()
        assert "BOSS_RECOVERY_DB_IMAGE_ID" in base["services"]["db"]["image"]
    else:
        override = json.loads((target / ".boss-restore-images.json").read_text())
        for role in recovery.ROLES:
            assert "BOSS_RECOVERY_API_IMAGE_ID" in base["services"][role]["image"]
            assert "boss_community_recovery:" + recovery.ROOT + ":ro" in base["services"][role]["volumes"]
            mount = override["services"][role]["volumes"][0]
            assert mount["read_only"] and mount["target"] == recovery.ROOT
    # Re-running refuses occupied private environment without touching the hold.
    before = (root / "persistent-hold.json").read_bytes()
    again, _ = restore(restore_fixture, offline=offline)
    assert again.returncode != 0
    assert (root / "persistent-hold.json").read_bytes() == before


def test_data_restore_failure_keeps_independent_hold_and_stops_database(restore_fixture):
    root, _target, _backup, _env = restore_fixture
    result, trace = restore(restore_fixture, fault="restore")
    assert result.returncode != 0 and "Recovery incomplete" in result.stderr
    assert json.loads((root / "persistent-hold.json").read_text())["state"] == "held"
    assert any(a[0] == "compose" and a[-1] == "stop" for a in trace)
    assert not any(a[-1] == "api" and "up" in a for a in trace)


@pytest.mark.parametrize("fault", ["volume_create", "volume_label", "marker_collision"])
def test_volume_or_hold_ownership_failure_never_restores_data_or_stops_other_stack(restore_fixture, fault):
    root, target, _backup, _env = restore_fixture
    if fault == "marker_collision":
        (root / "persistent-hold.json").write_text('{"state":"held","attempt":"other"}')
    result, trace = restore(restore_fixture, fault=fault)
    assert result.returncode != 0
    assert not (target / ".env").exists()
    assert not any(a[0] == "run" and "tar" in a for a in trace)
    assert not any(a[0] == "compose" and ("stop" in a or "up" in a) for a in trace)
    if fault == "marker_collision":
        assert json.loads((root / "persistent-hold.json").read_text())["attempt"] == "other"


def test_artifact_volume_creation_failure_retains_owned_hold(restore_fixture):
    root, _target, _backup, _env = restore_fixture
    result, trace = restore(restore_fixture, fault="artifact_create")
    assert result.returncode != 0
    assert json.loads((root / "persistent-hold.json").read_text())["state"] == "held"
    assert not any(a[0] == "compose" and a[-1] == "stop" for a in trace)
    assert not any(a[0] == "compose" and "up" in a for a in trace)


@pytest.mark.parametrize("order", ["current", "publish_before_marker_negative_control"])
def test_base_only_startup_interleaving_at_first_usable_target_env(restore_fixture, order):
    root, _target, _backup, _env = restore_fixture
    script = None
    if order != "current":
        tools = root / "negative-tools"
        tools.mkdir()
        for name in ("backup_archive.py", "recovery_compose.py"):
            shutil.copy2(INSTALL / name, tools / name)
        text = (INSTALL / "restore.sh").read_text()
        start = text.index("# Only now publish startup configuration.")
        end = text.index("for kind in identity artifacts;", start)
        publication = text[start:end]
        text = text[:start] + text[end:]
        # Change only publication order: the same guard/marker/image behavior.
        text = text.replace('RECOVERY_VOLUME="', publication + 'RECOVERY_VOLUME="', 1)
        script = tools / "restore.sh"
        script.write_text(text)
    result, _trace = restore(restore_fixture, script=script)
    assert result.returncode == 0, result.stderr
    observations = [json.loads(line) for line in (root / "interleaving.jsonl").read_text().splitlines()]
    assert observations
    if order == "current":
        assert observations[0]["held_marker_present"]
        assert not any(item["application_started"] for item in observations)
        assert not (root / "unsafe-application-started").exists()
    else:
        assert not observations[0]["held_marker_present"]
        assert observations[0]["application_started"]
        assert (root / "unsafe-application-started").exists()


@pytest.mark.parametrize("field", ["image_id", "source_commit", "interface", "entrypoint", "user"])
def test_image_identity_interface_and_revision_must_match(field):
    manifest = {"api_image_id": "sha256:" + "2" * 64, "source_commit": "a" * 40}
    metadata = {"image_id": manifest["api_image_id"], "source_commit": manifest["source_commit"], "interface": recovery.INTERFACE,
                "entrypoint": ["/usr/local/bin/boss-entrypoint"], "user": "boss"}
    metadata[field] = "wrong"
    with pytest.raises(SystemExit):
        recovery.qualify_image(REPO, manifest, metadata, recovery.source_hashes(REPO))
