"""Real Postgres/new-volume recovery with real identity crypto; no paid/provider effects.

The Compose services are an explicit inert lifecycle fixture using a cached Postgres
image, not a claim that the ordinary installed BOSS application has been qualified.
"""
from __future__ import annotations

import base64
import hashlib
import json
import io
import tarfile
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.environ.get('BOSS_RUN_DOCKER_LIFECYCLE_TEST') != '1',
                                reason='requires cached local Postgres image and owned Docker volumes')


def run(*args, input=None, timeout=90):
    return subprocess.run([str(a) for a in args], input=input, capture_output=True,
                          text=True, timeout=timeout, check=False)


def ok(*args, input=None, timeout=90):
    result = run(*args, input=input, timeout=timeout)
    assert result.returncode == 0, result.stderr[-3000:]
    return result.stdout.strip()


def compose(install, project):
    return ['docker', 'compose', '-p', project, '-f', install / 'scripts/install/community-compose.yml',
            '--env-file', install / '.env']


def engine_for(cmd):
    port = ok(*cmd, 'port', 'db', '5432').rsplit(':', 1)[1]
    return create_engine(f'postgresql+psycopg://boss:fixture@127.0.0.1:{port}/boss')


def test_archive_restores_owner_totp_vault_artifact_and_history_into_new_volumes(tmp_path, monkeypatch):
    from boss_engine.identity import vault, passwords, users, totp, signin, authn
    from boss_engine.services.receipts import ReceiptService
    from boss_engine.services.core import emit_event

    if run('docker', 'image', 'inspect', 'postgres:16-alpine').returncode:
        pytest.skip('cached Postgres 16 image unavailable; no pull is allowed')
    tmp_path.chmod(0o700)
    install = tmp_path / 'source'
    recovered = tmp_path / 'recovered'
    compose_file = install / 'scripts/install/community-compose.yml'
    compose_file.parent.mkdir(parents=True)
    source_project = 'bossrecovery' + uuid.uuid4().hex[:10]
    target_project = source_project + 'restored'
    compose_file.write_text(f'name: {source_project}\n' + '''services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: boss
      POSTGRES_USER: boss
      POSTGRES_PASSWORD: fixture
    ports: ["127.0.0.1::5432"]
    volumes: ["pg:/var/lib/postgresql/data"]
  api:
    image: postgres:16-alpine
    entrypoint: ["sleep", "infinity"]
    volumes:
      - identity:/var/lib/boss/identity
      - artifacts:/var/lib/boss/artifacts
  egress:
    image: postgres:16-alpine
    entrypoint: ["sleep", "infinity"]
volumes:
  pg: {}
  identity: {}
  artifacts: {}
''')
    sha = ok('git', '-C', REPO, 'rev-parse', 'HEAD')
    (install / '.boss-shipped-commit').write_text(sha + '\n')
    (install / '.env').write_text('BOSS_DB_PASSWORD=fixture\n')
    (install / '.env').chmod(0o600)
    keyfile = tmp_path / 'fixture.key'
    keyfile.write_bytes(base64.b64encode(os.urandom(32)))
    keyfile.chmod(0o600)
    monkeypatch.setenv('BOSS_MASTER_KEY_FILE', str(keyfile))
    monkeypatch.delenv('BOSS_MASTER_KEY', raising=False)
    monkeypatch.delenv('BOSS_IDENTITY_SERVER_KEY', raising=False)
    monkeypatch.setenv('BOSS_PUBLIC_URL', 'http://localhost:3000')
    source_cmd = compose(install, source_project)
    target_cmd = None
    source_engine = target_engine = None
    try:
        ok(*source_cmd, 'up', '-d', '--pull', 'never')
        for _ in range(60):
            if run(*source_cmd, 'exec', '-T', 'db', 'pg_isready', '-h', '127.0.0.1', '-U', 'boss', '-d', 'boss').returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.fail('owned Postgres did not start')
        for sql in ['db/schema.sql', 'db/identity_seed.sql']:
            ok(*source_cmd, 'exec', '-T', 'db', 'psql', '-v', 'ON_ERROR_STOP=1', '-U', 'boss', '-d', 'boss',
               input=(REPO / sql).read_text(), timeout=120)
        ok(*source_cmd, 'exec', '-T', 'api', 'sh', '-c',
           'umask 077; cat > /var/lib/boss/identity/master.key', input=keyfile.read_text())
        artifact = 'A real restored artifact: recovery fixture, no provider effects.\n'
        ok(*source_cmd, 'exec', '-T', 'api', 'sh', '-c',
           'umask 077; cat > /var/lib/boss/artifacts/recovery.txt', input=artifact)
        source_engine = engine_for(source_cmd)
        vault.reset_master_key_cache()
        password = 'recovery-fixture-owner-2026-private'
        secret = totp.generate_secret()
        vault_secret = 'fixture-local-connection-value-with-no-provider-access'
        with source_engine.begin() as db:
            db.execute(text("INSERT INTO orgs(id,name) VALUES('org_recovery','Recovery fixture')"))
            db.execute(text("INSERT INTO workspaces(id,org_id,name) VALUES('ws_recovery','org_recovery','Fixture')"))
            db.execute(text("INSERT INTO projects(id,workspace_id,name,slug) VALUES('recovery','ws_recovery','Fixture','recovery')"))
            vault.boot(db=db)
            owner = users.create_user(db, org_id='org_recovery', email='owner@recovery.invalid',
                                      display_name='Recovery Owner', password_hash=passwords.hash_password(password),
                                      status='active', created_by='fixture')
            users.add_binding(db, org_id='org_recovery', subject_id=owner, role_key='owner', granted_by=owner,
                              reason='explicit recovery fixture')
            totp_item = vault.store(org_id='org_recovery', owner_principal_id=owner,
                                   kind=vault.VaultItemKind.TOTP_SECRET, provider='totp', secret=secret, db=db)
            db.execute(text('INSERT INTO identity_totp(principal_id,vault_item_id,last_step) VALUES(:p,:v,0)'),
                       {'p': owner, 'v': totp_item})
            connection = vault.store(org_id='org_recovery', owner_principal_id=None,
                                     kind=vault.VaultItemKind.LEGACY_PAT, provider='github', secret=vault_secret, db=db)
            receipt, _ = ReceiptService().create_or_reuse(db, 'org_recovery', 'ws_recovery', 'recovery',
                                                        'recovery-fixture-receipt', 'local.artifact', {}, {'fixture': True})
            emit_event(db, 'recovery', 'recovery.fixture', owner, 'receipt', receipt['id'], {'fixture': True})
        before_running = set(ok(*source_cmd, 'ps', '--status', 'running', '--services').splitlines())
        backup_result = run('bash', REPO / 'scripts/install/backup.sh', '--dir', install,
                            '--backup-dir', tmp_path / 'backups', timeout=120)
        assert backup_result.returncode == 0, backup_result.stderr[-3000:]
        backup = Path(backup_result.stdout.strip())
        manifest = json.loads((backup / 'manifest.json').read_text())
        assert manifest['complete'] and manifest['identity_state']['items'] == 2
        assert set(ok(*source_cmd, 'ps', '--status', 'running', '--services').splitlines()) == before_running
        assert 'egress' in before_running
        # Corruption/interruption/missing/mismatched key input must fail before target creation.
        for mode in ('missing', 'corrupt', 'mismatched', 'interrupted', 'invalid_dump'):
            broken = tmp_path / ('backup-' + mode)
            shutil.copytree(backup, broken)
            broken.chmod(0o700)
            spec = json.loads((broken / 'manifest.json').read_text())
            if mode == 'missing':
                (broken / 'identity.tar').unlink()
            elif mode == 'corrupt':
                (broken / 'artifacts.tar').write_bytes(b'corrupt')
            elif mode == 'mismatched':
                with tarfile.open(broken / 'identity.tar', 'w') as output:
                    data = base64.b64encode(os.urandom(32))
                    item = tarfile.TarInfo('master.key')
                    item.mode = 0o600
                    item.size = len(data)
                    output.addfile(item, io.BytesIO(data))
                spec['files']['identity.tar'] = hashlib.sha256((broken / 'identity.tar').read_bytes()).hexdigest()
            elif mode == 'invalid_dump':
                (broken / 'boss.dump').write_bytes(b'PGDMPtruncated')
                spec['files']['boss.dump'] = hashlib.sha256((broken / 'boss.dump').read_bytes()).hexdigest()
            else:
                spec['complete'] = False
            (broken / 'manifest.json').write_text(json.dumps(spec))
            negative_target = tmp_path / ('target-' + mode)
            shutil.copytree(install, negative_target, ignore=shutil.ignore_patterns('.env'))
            negative_project = source_project + mode
            refused = run('bash', REPO / 'scripts/install/restore.sh', '--backup', broken,
                          '--dir', negative_target, '--project', negative_project, timeout=120)
            assert refused.returncode != 0
            assert not (negative_target / '.env').exists()
            assert not ok('docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={negative_project}')
            assert not ok('docker', 'volume', 'ls', '-q', '--filter', f'label=com.docker.compose.project={negative_project}')
            shutil.rmtree(broken)
        shutil.copytree(install, recovered, ignore=shutil.ignore_patterns('.env'))
        target_cmd = compose(recovered, target_project)
        restored = run('bash', REPO / 'scripts/install/restore.sh', '--backup', backup,
                       '--dir', recovered, '--project', target_project, timeout=180)
        assert restored.returncode == 0, restored.stderr[-3000:]
        assert set(ok(*target_cmd, 'ps', '--status', 'running', '--services').splitlines()) == {'db'}
        target_engine = engine_for(target_cmd)
        # Read the actual restored volume's key into a new protected file, not the original key.
        restored_key = tmp_path / 'restored.key'
        raw = ok(*target_cmd, 'run', '--rm', '--no-deps', '--pull', 'never', '-T', '--entrypoint',
                 'cat', 'api', '/var/lib/boss/identity/master.key')
        restored_key.write_text(raw)
        restored_key.chmod(0o600)
        assert hashlib.sha256(restored_key.read_bytes()).digest() == hashlib.sha256(keyfile.read_bytes()).digest()
        monkeypatch.setenv('BOSS_MASTER_KEY_FILE', str(restored_key))
        vault.reset_master_key_cache()
        with target_engine.begin() as db:
            vault.boot(db=db)
            user = users.by_id(db, owner)
            assert user.email == 'owner@recovery.invalid' and passwords.verify_password(password, user.password_hash)
            assert db.execute(text("SELECT count(*) FROM identity_role_bindings WHERE subject_id=:p AND role_id='arl_owner'"), {'p': owner}).scalar_one() == 1
            assert vault.org_github_token(connection, db=db) == vault_secret
            facts = signin.RequestFacts(ip='127.0.0.1', user_agent='recovery-fixture',
                                         origin='http://localhost:3000', request_origin='http://localhost:3000')
            answer = signin.password_login(db, facts, email=user.email, password=password)
            assert answer.status == 200 and answer.body['next'] == 'mfa'
            preauth = next(cookie.split(';', 1)[0].split('=', 1)[1] for cookie in answer.cookies
                           if cookie.startswith(authn.PREAUTH_COOKIE + '='))
            facts.preauth = preauth
            signed_in = signin.mfa_totp(db, facts, code=totp.code_at(secret, totp.current_step()))
            assert signed_in.status == 200 and signed_in.body['next'] == 'done'
            assert any(cookie.startswith(authn.SESSION_COOKIE + '=') for cookie in signed_in.cookies)
            assert db.execute(text("SELECT count(*) FROM receipts WHERE dedupe_key='recovery-fixture-receipt'")).scalar_one() == 1
            assert db.execute(text("SELECT count(*) FROM events WHERE event_type='recovery.fixture'")).scalar_one() == 1
        assert ok(*target_cmd, 'run', '--rm', '--no-deps', '--pull', 'never', '-T', '--entrypoint',
                  'cat', 'api', '/var/lib/boss/artifacts/recovery.txt') == artifact.strip()
        # A second restore must refuse the occupied target before changing it.
        repeat = run('bash', REPO / 'scripts/install/restore.sh', '--backup', backup,
                     '--dir', recovered, '--project', target_project)
        assert repeat.returncode != 0 and 'Target .env already exists' in repeat.stderr
        # Actual purge must preserve the source when its current key cannot recover the DB.
        for helper in ('backup.sh', 'backup_archive.py'):
            shutil.copy2(REPO / 'scripts/install' / helper, compose_file.parent / helper)
        original_volumes = set(ok('docker', 'volume', 'ls', '-q', '--filter',
                                  f'label=com.docker.compose.project={source_project}').splitlines())
        assert len(original_volumes) == 3
        wrong_key = base64.b64encode(os.urandom(32)).decode()
        ok(*source_cmd, 'exec', '-T', 'api', 'sh', '-c',
           'umask 077; cat > /var/lib/boss/identity/master.key', input=wrong_key)
        try:
            refused_purge = run('bash', REPO / 'scripts/install/uninstall.sh', '--dir', install,
                               '--purge-data', '--confirm-purge', 'DELETE-BOSS-DATA',
                               '--backup-dir', tmp_path / 'purge-negative', timeout=120)
            assert refused_purge.returncode != 0 and 'data purge was cancelled' in refused_purge.stderr
            assert wrong_key not in refused_purge.stdout + refused_purge.stderr
            assert (install / '.env').is_file()
            assert set(ok('docker', 'volume', 'ls', '-q', '--filter',
                          f'label=com.docker.compose.project={source_project}').splitlines()) == original_volumes
            assert set(ok(*source_cmd, 'ps', '--status', 'running', '--services').splitlines()) == before_running
            assert not list((tmp_path / 'purge-negative').glob('boss-*'))
            with source_engine.begin() as db:
                assert db.execute(text("SELECT count(*) FROM receipts WHERE dedupe_key='recovery-fixture-receipt'")).scalar_one() == 1
        finally:
            ok(*source_cmd, 'exec', '-T', 'api', 'sh', '-c',
               'umask 077; cat > /var/lib/boss/identity/master.key', input=keyfile.read_text())
        # Compressed DB contents containing excluded CLI sessions are rejected, not byte-edited.
        token = 'explicit-fixture-cli-token-' + uuid.uuid4().hex
        (install / '.env').write_text('BOSS_DB_PASSWORD=fixture\nCLAUDE_CODE_OAUTH_TOKEN=' + token + '\n')
        with source_engine.begin() as db:
            db.execute(text('CREATE TABLE recovery_secret_negative(value text)'))
            db.execute(text('INSERT INTO recovery_secret_negative(value) VALUES(:v)'), {'v': token})
        negative = run('bash', REPO / 'scripts/install/backup.sh', '--dir', install,
                       '--backup-dir', tmp_path / 'negative', timeout=120)
        assert negative.returncode != 0 and token not in negative.stdout + negative.stderr
        assert not list((tmp_path / 'negative').glob('boss-*'))
        assert set(ok(*source_cmd, 'ps', '--status', 'running', '--services').splitlines()) == before_running
    finally:
        for engine in (source_engine, target_engine):
            if engine is not None:
                engine.dispose()
        if target_cmd:
            run(*target_cmd, 'down', '--volumes', '--remove-orphans')
        run(*source_cmd, 'down', '--volumes', '--remove-orphans')
        vault.reset_master_key_cache()
        keyfile.unlink(missing_ok=True)
        (tmp_path / 'restored.key').unlink(missing_ok=True)
