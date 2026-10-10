"""Recovery archive negatives use explicit private fixtures, never operator data."""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('backup_archive', REPO / 'scripts/install/backup_archive.py')
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


def tar(path, members):
    with tarfile.open(path, 'w') as output:
        for name, data, mode, kind in members:
            item = tarfile.TarInfo(name)
            item.mode = mode
            item.type = kind
            item.size = len(data) if kind == tarfile.REGTYPE else 0
            output.addfile(item, io.BytesIO(data) if item.isfile() else None)
    path.chmod(0o600)


@pytest.fixture
def complete(tmp_path):
    root = tmp_path / 'backup'
    root.mkdir(mode=0o700)
    key = os.urandom(32)
    for name, data in {'.env': 'CODEX_AUTH_JSON=\n', 'source-commit': 'a' * 40,
                       'boss.dump': 'explicit-unit-fixture-not-a-real-dump'}.items():
        (root / name).write_text(data)
        (root / name).chmod(0o600)
    tar(root / 'identity.tar', [('master.key', base64.b64encode(key), 0o600, tarfile.REGTYPE)])
    tar(root / 'artifacts.tar', [('result.txt', b'result fixture', 0o600, tarfile.REGTYPE)])
    kcv = hmac.new(key, b'BOSS-VAULT-KCV-v1', hashlib.sha256).digest()[:16].hex()
    metadata = {'master_key_relative': 'master.key', 'identity_state': {'kcvs': [kcv], 'items': 1},
                'db_image_id': 'sha256:' + '1' * 64, 'api_image_id': 'sha256:' + '2' * 64}
    manifest = {**metadata, 'key_check_value': kcv, 'format': 1, 'complete': True,
                'source_commit': 'a' * 40,
                'files': {name: archive.digest(root / name) for name in archive.REQUIRED}}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'manifest.json').chmod(0o600)
    return root, manifest


def rewrite_manifest(root, manifest):
    (root / 'manifest.json').write_text(json.dumps(manifest))


def test_complete_private_archive_contract(complete):
    root, manifest = complete
    assert archive.validate(root)['source_commit'] == manifest['source_commit']


@pytest.mark.parametrize('name', sorted(archive.REQUIRED))
def test_missing_component_is_refused(complete, name):
    root, _ = complete
    (root / name).unlink()
    with pytest.raises((ValueError, OSError)):
        archive.validate(root)


@pytest.mark.parametrize('change', ['checksum', 'legacy', 'incomplete', 'wrong_key', 'public_key', 'symlink', 'traversal'])
def test_invalid_archive_is_refused_even_with_updated_hashes(complete, change):
    root, manifest = complete
    if change == 'checksum':
        (root / 'boss.dump').write_bytes(b'truncated')
    elif change == 'legacy':
        manifest.pop('format')
    elif change == 'incomplete':
        manifest['complete'] = False
    elif change in ('wrong_key', 'public_key'):
        key = base64.b64encode(os.urandom(32))
        tar(root / 'identity.tar', [('master.key', key, 0o644 if change == 'public_key' else 0o600, tarfile.REGTYPE)])
        manifest['files']['identity.tar'] = archive.digest(root / 'identity.tar')
    elif change in ('symlink', 'traversal'):
        tar(root / 'artifacts.tar', [('../outside' if change == 'traversal' else 'link', b'x', 0o600,
                                    tarfile.REGTYPE if change == 'traversal' else tarfile.SYMTYPE)])
        manifest['files']['artifacts.tar'] = archive.digest(root / 'artifacts.tar')
    rewrite_manifest(root, manifest)
    with pytest.raises((ValueError, OSError)):
        archive.validate(root)


def test_session_scan_detects_cross_chunk_secret_without_echo():
    secret = b'private-cli-fixture-session-value'
    stream = io.BytesIO(b'x' * (1024 * 1024 - 3) + secret + b'x')
    with pytest.raises(ValueError) as error:
        archive.scan(stream, [secret])
    assert secret.decode() not in str(error.value)


def test_tar_content_secret_is_refused_without_rewriting(tmp_path):
    path = tmp_path / 'artifacts.tar'
    secret = b'private-cli-fixture-session-value'
    tar(path, [('artifact.txt', secret, 0o600, tarfile.REGTYPE)])
    before = path.read_bytes()
    with pytest.raises(ValueError):
        archive.archive_members(path, secrets=[secret])
    assert path.read_bytes() == before


def test_missing_completion_never_contacts_docker_for_restore(tmp_path):
    root = tmp_path / 'incomplete'
    root.mkdir(mode=0o700)
    target = tmp_path / 'target'
    target.mkdir()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    marker = tmp_path / 'docker-contact'
    docker = bin_dir / 'docker'
    docker.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 99\n')
    docker.chmod(0o700)
    result = subprocess.run(['bash', str(REPO / 'scripts/install/restore.sh'), '--backup', str(root),
                             '--dir', str(target), '--project', 'recovery-negative'],
                            env={**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert not marker.exists()
    assert not (target / '.env').exists()


def test_purge_independently_refuses_an_incomplete_backup(tmp_path):
    install = tmp_path / 'install'
    helper_dir = install / 'scripts/install'
    helper_dir.mkdir(parents=True)
    (helper_dir / 'community-compose.yml').write_text('services: {}\n')
    (install / '.env').write_text('BOSS_DB_PASSWORD=explicit-fixture\n')
    backup = tmp_path / 'legacy'
    backup.mkdir(mode=0o700)
    (helper_dir / 'backup.sh').write_text('#!/bin/sh\nprintf "%s\\n" "' + str(backup) + '"\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    trace = tmp_path / 'docker-trace'
    docker = bin_dir / 'docker'
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "' + str(trace) + '"\nexit 0\n')
    docker.chmod(0o700)
    result = subprocess.run(['bash', str(REPO / 'scripts/install/uninstall.sh'), '--dir', str(install),
                             '--purge-data', '--confirm-purge', 'DELETE-BOSS-DATA'],
                            env={**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and 'data purge was cancelled' in result.stderr
    assert 'down' not in trace.read_text()
    assert (install / '.env').exists()


def test_quoted_codex_session_tokens_and_copy_escaping_are_checked():
    token = "explicit-fixture-access-token-1234"
    raw = "'" + json.dumps({"tokens": {"access_token": token, "refresh_token": token + "\\suffix"}}) + "'"
    values = archive.session_values(raw)
    assert token in values
    assert token + "\\\\suffix" in values
    with pytest.raises(ValueError):
        archive.scan(io.BytesIO((token + "\\\\suffix").encode()), [value.encode() for value in values])


@pytest.mark.parametrize('line', [
    "CLAUDE_CODE_OAUTH_TOKEN=fixture-secret-token-123456789",
    " export CLAUDE_CODE_OAUTH_TOKEN = 'fixture-secret-token-123456789' # note",
    ' CODEX_AUTH_JSON = \'{"tokens":{"access_token":"fixture-secret-token-123456789"}}\'',
    'CLAUDE_CODE_OAUTH_TOKEN="fixture-secret-token-123456789" # note',
])
def test_env_supported_compose_forms_are_blank_and_decoded_for_scan(tmp_path, line):
    source, target, values = [tmp_path / name for name in ('input.env', 'archive.env', 'excluded.json')]
    source.write_text('BOSS_DB_PASSWORD=explicit-fixture\n' + line + '\n')
    result = subprocess.run(['python3', str(REPO / 'scripts/install/backup_archive.py'), 'env',
                             str(source), str(target), str(values)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'fixture-secret-token-123456789' not in target.read_text()
    assert 'fixture-secret-token-123456789' in json.loads(values.read_text())
    assert 'fixture-secret-token-123456789' not in result.stdout + result.stderr
    assert target.stat().st_mode & 0o077 == values.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('line', [
    'CLAUDE_CODE_OAUTH_TOKEN=${TOKEN}',
    'CLAUDE_CODE_OAUTH_TOKEN="${TOKEN}"',
    'CODEX_AUTH_JSON=\'{"tokens":\n"unfinished"',
    'export CODEX_AUTH_JSON',
])
def test_env_ambiguous_compose_forms_fail_before_archive_output(tmp_path, line):
    source, target, values = [tmp_path / name for name in ('input.env', 'archive.env', 'excluded.json')]
    source.write_text(line)
    result = subprocess.run(['python3', str(REPO / 'scripts/install/backup_archive.py'), 'env',
                             str(source), str(target), str(values)], capture_output=True, text=True)
    assert result.returncode != 0
    assert not target.exists() and not values.exists()
