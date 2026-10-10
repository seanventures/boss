#!/usr/bin/env python3
"""Private recovery archive contract. Never print environment values or key material."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath

FORMAT = 1
REQUIRED = {'.env', 'source-commit', 'boss.dump', 'artifacts.tar', 'identity.tar'}
BLOCKED = {'CLAUDE_CODE_OAUTH_TOKEN', 'CODEX_AUTH_JSON'}


def fail(message):
    raise ValueError(message)


def private_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        fail(f'Expected a private regular file: {path.name}')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else stream_hash(stream)


def stream_hash(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(block)
    return result.hexdigest()


def dotenv_session_value(value):
    """Decode supported single-line Compose dotenv values; ambiguous forms fail closed."""
    value = value.strip()
    if value.startswith("'"):
        match = re.fullmatch(r"'([^']*)'\s*(?:#.*)?", value)
        if not match:
            fail('Multiline or escaped single-quoted CLI session values require a reviewed backup.')
        return match[1]
    if value.startswith('"'):
        match = re.fullmatch(r'"((?:[^"\\]|\\.)*)"\s*(?:#.*)?', value)
        if not match:
            fail('Multiline or malformed quoted CLI session values require a reviewed backup.')
        value = re.sub(r'\\([nrt"\\])', lambda m: {'n': '\n', 'r': '\r', 't': '\t',
                                                     '"': '"', '\\': '\\'}[m[1]], match[1])
    else:
        value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
    if '$' in value:
        fail('Interpolated CLI session values require a reviewed backup; automatic backup cannot safely resolve them.')
    return value


def session_values(value):
    """Check both dotenv quoting and nested Codex credential strings without logging them."""
    values = {value}
    if len(value) > 1 and value[0] == value[-1] and value[0] in ("'", '"'):
        values.add(value[1:-1])
    for candidate in tuple(values):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        def visit(item):
            if isinstance(item, dict):
                for nested in item.values():
                    visit(nested)
            elif isinstance(item, list):
                for nested in item:
                    visit(nested)
            elif isinstance(item, str) and len(item) >= 20:
                values.add(item)
        visit(parsed)
    # PostgreSQL COPY escapes backslashes and control characters. JSON/SQL may also
    # serialize a credential rather than storing its raw bytes.
    for candidate in tuple(values):
        values.add(candidate.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t'))
        values.add(json.dumps(candidate)[1:-1])
    return sorted(value for value in values if value)


def scan(stream, secrets):
    overlap = max((len(secret) for secret in secrets), default=1) - 1
    previous = b''
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        data = previous + block
        if any(secret in data for secret in secrets):
            fail('Excluded CLI session material was found; archive refused without modifying durable data.')
        previous = data[-overlap:] if overlap else b''


def archive_members(path, *, identity=False, secrets=()):
    seen = set()
    key_bytes = {}
    with tarfile.open(path, 'r:') as archive:
        for member in archive:
            name = member.name
            while name.startswith('./'):
                name = name[2:]
            if name in ('', '.') and member.isdir():
                continue
            pure = PurePosixPath(name)
            if pure.is_absolute() or '..' in pure.parts or name in seen or not (member.isdir() or member.isfile()):
                fail(f'Unsafe or duplicate archive member in {path.name}')
            seen.add(name)
            if identity and (member.mode & 0o077):
                fail('Identity archive contains a key or directory accessible to other users.')
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    fail(f'Unreadable archive member in {path.name}')
                with stream:
                    if identity:
                        if member.size > 1024 * 1024:
                            fail('Unexpected large identity file.')
                        key_bytes[name] = stream.read()
                        if any(secret in key_bytes[name] for secret in secrets):
                            fail('Excluded CLI session material in identity archive.')
                    else:
                        scan(stream, secrets)
    return key_bytes


def key_check(root, metadata, *, secrets=()):
    keys = archive_members(root / 'identity.tar', identity=True, secrets=secrets)
    name = metadata['master_key_relative']
    raw = keys.get(name)
    state = metadata['identity_state']
    kcvs = state.get('kcvs', [])
    if not isinstance(kcvs, list) or any(not re.fullmatch(r'[0-9a-f]{32}', k) for k in kcvs):
        fail('Invalid database identity metadata.')
    if raw is None:
        if kcvs or state.get('items', 0):
            fail('Identity master key missing for a populated database.')
        return None
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except ValueError:
        fail('Invalid identity master key encoding.')
    if len(key) != 32:
        fail('Invalid identity master key length.')
    kcv = hmac.new(key, b'BOSS-VAULT-KCV-v1', hashlib.sha256).digest()[:16].hex()
    if (kcvs or state.get('items', 0)) and kcv not in kcvs:
        fail('Identity master key does not match the database snapshot.')
    return kcv


def validate(root, manifest=None):
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        fail('Backup directory must be private and must not be a symlink.')
    if manifest is None:
        private_file(root / 'manifest.json')
        manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format') != FORMAT or manifest.get('complete') is not True:
        fail('Incomplete or legacy archive; preserve original data and use a reviewed recovery plan.')
    files = manifest.get('files', {})
    if not isinstance(files, dict):
        fail('Invalid archive file inventory.')
    state = manifest.get('identity_state')
    if not isinstance(state, dict) or not isinstance(state.get('items'), int) or state['items'] < 0:
        fail('Invalid database identity metadata.')
    if not REQUIRED <= set(files) or set(files) - REQUIRED - {'infisical.dump'}:
        fail('Archive manifest has missing or unsupported files.')
    for name, checksum in files.items():
        path = root / name
        private_file(path)
        if not path.stat().st_size or digest(path) != checksum:
            fail(f'Archive checksum mismatch: {name}')
    source = (root / 'source-commit').read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', source) or manifest.get('source_commit') != source:
        fail('Invalid or mismatched source revision.')
    for name in ('db_image_id', 'api_image_id'):
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest.get(name, '')):
            fail('Archive is missing an immutable local image identity.')
    path = manifest.get('master_key_relative', '')
    if not path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
        fail('Invalid master-key path.')
    archive_members(root / 'artifacts.tar')
    if key_check(root, manifest) != manifest.get('key_check_value'):
        fail('Archive key check value mismatch.')
    return manifest


def layout(config):
    result = {}
    services = config['services']
    for service, target, name in [('db', '/var/lib/postgresql/data', 'db_volume'),
                                  ('api', '/var/lib/boss/identity', 'identity_volume'),
                                  ('api', '/var/lib/boss/artifacts', 'artifacts_volume')]:
        mounts = [v for v in services[service].get('volumes', []) if v.get('target') == target]
        if len(mounts) != 1 or mounts[0].get('type') != 'volume':
            fail(f'Automatic recovery requires a named volume at {target}.')
        volume = config['volumes'][mounts[0]['source']]
        if volume.get('external'):
            fail('Automatic recovery refuses external volumes.')
        result[name] = volume['name']
    key_path = (services['api'].get('environment') or {}).get('BOSS_MASTER_KEY_FILE') or '/var/lib/boss/identity/master.key'
    pure = PurePosixPath(key_path)
    prefix = PurePosixPath('/var/lib/boss/identity')
    if '..' in pure.parts or not pure.is_relative_to(prefix) or pure == prefix:
        fail('Master key is outside the identity volume; a reviewed external-key backup is required.')
    result['master_key_relative'] = str(pure.relative_to(prefix))
    result['api_image'] = services['api']['image']
    result['all_volumes'] = [v['name'] for v in config.get('volumes', {}).values()]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'create', 'layout', 'env', 'scan'])
    parser.add_argument('path', nargs='?')
    parser.add_argument('extra', nargs='*')
    args = parser.parse_args()
    if args.command == 'layout':
        print(json.dumps(layout(json.load(sys.stdin))))
    elif args.command == 'env':
        source, target, excluded = Path(args.path), Path(args.extra[0]), Path(args.extra[1])
        lines, secrets = [], []
        for line in source.read_text().splitlines():
            match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
            if match and match[1] in BLOCKED:
                value = dotenv_session_value(match[2])
                if value:
                    secrets.extend(session_values(value))
                line = match[1] + '='
            elif re.match(r'^\s*(?:export\s+)?(?:CLAUDE_CODE_OAUTH_TOKEN|CODEX_AUTH_JSON)\b', line):
                fail('Unsupported CLI session assignment requires a reviewed backup.')
            lines.append(line)
        target.write_text('\n'.join(lines) + '\n')
        target.chmod(0o600)
        excluded.write_text(json.dumps(secrets))
        excluded.chmod(0o600)
    elif args.command == 'scan':
        secrets = [s.encode() for s in json.loads(Path(args.path).read_text()) if s]
        scan(sys.stdin.buffer, secrets)
    elif args.command == 'validate':
        validate(Path(args.path))
    else:
        root = Path(args.path)
        metadata = json.loads(Path(args.extra[0]).read_text())
        secrets = [s.encode() for s in json.loads(Path(args.extra[1]).read_text()) if s]
        archive_members(root / 'artifacts.tar', secrets=secrets)
        manifest = {**metadata, 'format': FORMAT, 'complete': True,
                    'source_commit': (root / 'source-commit').read_text().strip()}
        manifest['key_check_value'] = key_check(root, manifest, secrets=secrets)
        names = REQUIRED | ({'infisical.dump'} if (root / 'infisical.dump').exists() else set())
        manifest['files'] = {name: digest(root / name) for name in sorted(names)}
        validate(root, manifest)
        target = root / 'manifest.json'
        temporary = root / 'manifest.json.writing'
        temporary.write_text(json.dumps(manifest, indent=2) + '\n')
        temporary.chmod(0o600)
        os.replace(temporary, target)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError) as exc:
        # Do not echo parser values, SQL, key bytes, or environment content.
        message = str(exc) if type(exc) is ValueError else 'Archive input is missing or invalid.'
        print('Recovery archive refused: ' + message, file=sys.stderr)
        sys.exit(1)
