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
OPTIONAL = {'infisical.dump', 'repos.tar'}
BLOCKED = {'CLAUDE_CODE_OAUTH_TOKEN', 'CODEX_AUTH_JSON'}
# LOCAL-FIRST PROJECTS (BOSSAPP-915): the bare git repositories of local projects (and the GitHub
# mirrors) live in their own named volume. Until a project is published it is the ONLY copy of
# that project's code, so an archive of an install that declares the volume must carry it.
REPOS_TARGET = '/var/lib/boss/repos'


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


def repo_inventory(path):
    """The bare repositories inside a repos archive: every `<...>.git` directory with a HEAD file."""
    found = set()
    with tarfile.open(path, 'r:') as archive:
        for member in archive:
            name = member.name
            while name.startswith('./'):
                name = name[2:]
            pure = PurePosixPath(name)
            if member.isfile() and pure.name == 'HEAD' and pure.parent.name.endswith('.git'):
                found.add(str(pure.parent))
    return sorted(found)


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
    if not REQUIRED <= set(files) or set(files) - REQUIRED - OPTIONAL:
        fail('Archive manifest has missing or unsupported files.')
    if manifest.get('repos_volume') and 'repos.tar' not in files:
        fail('The install keeps local repositories, but the archive has no repos.tar.')
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
    if 'repos.tar' in files:
        archive_members(root / 'repos.tar')
        if repo_inventory(root / 'repos.tar') != manifest.get('repositories'):
            fail('The repositories in repos.tar differ from the manifest.')
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
    # Optional: an install from before local-first projects declares no repos volume.
    mounts = [v for v in services['api'].get('volumes', []) if v.get('target') == REPOS_TARGET]
    if mounts:
        if len(mounts) != 1 or mounts[0].get('type') != 'volume':
            fail(f'Automatic recovery requires a named volume at {REPOS_TARGET}.')
        volume = config['volumes'][mounts[0]['source']]
        if volume.get('external'):
            fail('Automatic recovery refuses external volumes.')
        result['repos_volume'] = volume['name']
    key_path = (services['api'].get('environment') or {}).get('BOSS_MASTER_KEY_FILE') or '/var/lib/boss/identity/master.key'
    pure = PurePosixPath(key_path)
    prefix = PurePosixPath('/var/lib/boss/identity')
    if '..' in pure.parts or not pure.is_relative_to(prefix) or pure == prefix:
        fail('Master key is outside the identity volume; a reviewed external-key backup is required.')
    result['master_key_relative'] = str(pure.relative_to(prefix))
    result['api_image'] = services['api']['image']
    result['all_volumes'] = [v['name'] for v in config.get('volumes', {}).values()]
    return result


# ---- retention (storage hygiene layer 1, BOSSAPP-1146) -------------------------------------
#
# Every upgrade writes a ~1 GB backup under BOSS_BACKUP_DIR and nothing ever removed one: an
# operator's laptop held 358 of them (170 GB) after six weeks. The rule, applied after every
# successful backup and on demand (`backup_archive.py retention DIR`):
#
#   keep the newest BOSS_BACKUP_KEEP (10) and anything from the last BOSS_BACKUP_KEEP_DAYS (7)
#   days, within BOSS_BACKUP_CAP_GB (20) in total; the oldest go first; the backup just taken
#   (and, failing that, the newest one) is never removed.
#
# Only directories BOSS's backup.sh named (`boss-<UTC stamp>-<pid>`) are candidates; anything
# else in the directory (an operator's own copy, the legacy offline dumps) is never touched.
BACKUP_NAME = re.compile(r'^boss-(\d{8}T\d{6}Z)-\d+$')
RETENTION_DEFAULTS = {'keep': 10, 'keep_days': 7, 'cap_gb': 20.0}
#: The cap wins over "keep N" and "keep N days" — but never below this many of the newest
#: backups, so a rollback to the release before last stays possible (review of 02d79ce5e).
CAP_FLOOR = 3


def _tree_bytes(path):
    """Bytes under `path`, without following symlinks (a backup holds only regular files)."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _backup_time(path, match):
    """The backup's moment from its name; the directory's mtime when the name cannot be read."""
    import datetime as _dt
    try:
        return _dt.datetime.strptime(match[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime, tz=_dt.timezone.utc)


def list_backups(root):
    """[{path, time, bytes}] for every backup directory under `root`, newest first."""
    out = []
    root = Path(root)
    if not root.is_dir():
        return out
    for child in root.iterdir():
        match = BACKUP_NAME.match(child.name)
        if not match or child.is_symlink() or not child.is_dir():
            continue
        out.append({'path': child, 'time': _backup_time(child, match), 'bytes': _tree_bytes(child)})
    out.sort(key=lambda b: b['time'], reverse=True)
    return out


def retention_plan(backups, *, keep, keep_days, cap_bytes, protect=None, now=None):
    """Pure: which backups stay and which go, each with its reason. `backups` newest first.

    Returns (kept, removed): lists of {path, time, bytes, reason}."""
    import datetime as _dt
    now = now or _dt.datetime.now(tz=_dt.timezone.utc)
    keep = max(1, int(keep))
    protect = Path(protect).resolve() if protect else None
    horizon = now - _dt.timedelta(days=max(0, float(keep_days)))
    # A stamp from the future (a host whose clock was wrong) is not "the newest for ever":
    # it counts as taken now, and the one really just taken is still protected by --protect.
    backups = sorted(({**b, 'time': min(b['time'], now)} for b in backups),
                     key=lambda b: b['time'], reverse=True)
    kept, removed = [], []
    for index, backup in enumerate(backups):
        path = Path(backup['path'])
        if protect is not None and path.resolve() == protect:
            reason = 'the backup just taken'
        elif index == 0:
            reason = 'the newest backup'
        elif index < keep:
            reason = 'among the newest %d' % keep
        elif backup['time'] >= horizon:
            reason = 'less than %g days old' % float(keep_days)
        else:
            removed.append({**backup, 'reason': 'older than the newest %d and more than %g days old'
                            % (keep, float(keep_days))})
            continue
        kept.append({**backup, 'reason': reason})
    # THE CAP WINS over "keep N" and "keep N days": a cap the rules can exceed is not a cap.
    # Oldest first; never the protected one, and never below the CAP_FLOOR newest (a rollback
    # to the release before last must stay possible even on a small disk).
    if cap_bytes and cap_bytes > 0:
        total = sum(b['bytes'] for b in kept)
        floor = {id(b) for b in kept[:CAP_FLOOR]}
        for backup in reversed(kept[:]):
            if total <= cap_bytes:
                break
            if backup['reason'] == 'the backup just taken' or id(backup) in floor:
                continue
            kept.remove(backup)
            total -= backup['bytes']
            removed.append({**backup, 'reason': 'over the %.1f GB cap' % (cap_bytes / 1e9)})
    removed.sort(key=lambda b: b['time'])
    return kept, removed


def _gb(n):
    return '%.1f GB' % (n / 1e9)


def retention_main(argv):
    parser = argparse.ArgumentParser(
        prog='backup_archive.py retention',
        description='Remove old pre-upgrade backups: keep the newest N and the last D days, '
                    'within a size cap (which wins, but never below the newest %d). Logs one '
                    'line per removal to stderr; the backup named by --protect is never '
                    'removed.' % CAP_FLOOR)
    parser.add_argument('root', help='the backups directory (BOSS_BACKUP_DIR, ~/boss-backups)')
    parser.add_argument('--keep', type=int, default=RETENTION_DEFAULTS['keep'])
    parser.add_argument('--keep-days', type=float, default=RETENTION_DEFAULTS['keep_days'])
    parser.add_argument('--cap-gb', type=float, default=RETENTION_DEFAULTS['cap_gb'])
    parser.add_argument('--protect', default=None, help='the backup just taken')
    parser.add_argument('--dry-run', action='store_true', help='say what would go; remove nothing')
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error('--keep must be at least 1 (BOSS_BACKUP_KEEP)')
    if args.keep_days < 0 or args.cap_gb < 0:
        parser.error('--keep-days and --cap-gb cannot be negative')
    import datetime as _dt
    import shutil

    def log(line):
        print('backup retention: ' + line, file=sys.stderr)

    root = Path(args.root)
    if not root.is_dir():
        log('no backups directory at %s; nothing to do' % root)
        return 0
    backups = list_backups(root)
    kept, removed = retention_plan(backups, keep=args.keep, keep_days=args.keep_days,
                                   cap_bytes=args.cap_gb * 1e9, protect=args.protect)
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    freed = 0
    verb = 'would remove' if args.dry_run else 'removed'
    for backup in removed:
        age_days = (now - backup['time']).total_seconds() / 86400
        if not args.dry_run:
            try:
                shutil.rmtree(backup['path'])
            except OSError as exc:
                log('could not remove %s: %s' % (backup['path'], exc.strerror or 'error'))
                continue
        freed += backup['bytes']
        log('%s %s (%s, %.0f days old): %s' % (verb, backup['path'], _gb(backup['bytes']),
                                                age_days, backup['reason']))
    log('%s: kept %d (%s), %s %d (%s); rule: newest %d, last %g days, %.1f GB cap' % (
        root, len(kept), _gb(sum(b['bytes'] for b in kept)), verb, len(removed), _gb(freed),
        args.keep, args.keep_days, args.cap_gb))
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'retention':
        sys.exit(retention_main(sys.argv[2:]))
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
        if metadata.get('repos_volume'):
            if not (root / 'repos.tar').exists():
                fail('The install keeps local repositories, but they were not archived.')
            archive_members(root / 'repos.tar', secrets=secrets)
            manifest['repositories'] = repo_inventory(root / 'repos.tar')
        else:
            manifest.pop('repos_volume', None)
            (root / 'repos.tar').unlink(missing_ok=True)
        manifest['key_check_value'] = key_check(root, manifest, secrets=secrets)
        names = REQUIRED | {name for name in OPTIONAL if (root / name).exists()}
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
