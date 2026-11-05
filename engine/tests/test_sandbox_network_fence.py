"""A usable IPv6 network never receives a fenced marker after a failed rule.

Execute the real entrypoint's fence and marker block with synthetic commands. No host
firewall is touched; actual kernel/network qualification is recorded separately.
"""
from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get('BOSS_TEST_FENCE_SOURCE', ROOT / 'scripts/sandbox/entrypoint.sh'))


def _block() -> str:
    source = SOURCE.read_text()
    start = source.find('ipv6_unavailable() {')
    if start < 0:
        start = source.index('fence_network() {')
    return source[start:source.index('# --- heal_ownership (begin)')]


def _run(tmp_path: Path, *, fail: str = '', fail2: str = '', missing: bool = False,
         unavailable: bool = False, mode: str = 'enforce', ipv4_fail: str = '',
         restore_missing: bool = False):
    commands = tmp_path / 'commands'
    commands.mkdir()
    marker_root = tmp_path / 'shim'
    marker_root.mkdir()
    # Verify stale markers are removed even when this boot cannot fence.
    (marker_root / '.network-fenced').touch()
    for tool in ('mkdir', 'rm', 'chmod'):
        (commands / tool).symlink_to('/bin/' + tool)
    (commands / 'chown').write_text('#!/bin/sh\nexit 0\n')
    (commands / 'python3').write_text('''#!/bin/sh
body=$(/bin/cat)
case "$body" in
 *AF_INET6*) [ "$IPV6_UNAVAILABLE" = 1 ]; exit $? ;;
 *) printf '%s\\n' '172.30.0.0/16' ;;
esac
''')
    command = '''#!/bin/sh
printf '%s %s\\n' "${0##*/}" "$*" >> "$CALLS"
[ "$*" != "$FAIL_RULE" ] && [ "$*" != "$FAIL_RULE_2" ]
'''
    (commands / 'iptables').write_text(command.replace('$FAIL_RULE', '$IPV4_FAIL_RULE'))
    # The preview rules (integration/next) load through *-restore, fed by preview_self_rules.
    (commands / 'iptables-restore').write_text('#!/bin/sh\n/bin/cat >/dev/null\n')
    if not missing:
        (commands / 'ip6tables').write_text(command)
        if not restore_missing:
            (commands / 'ip6tables-restore').write_text('#!/bin/sh\n/bin/cat >/dev/null\n')
    for tool in commands.iterdir():
        if not tool.is_symlink():
            tool.chmod(0o700)
    calls = tmp_path / 'calls'
    env = {'PATH': str(commands), 'SHIM_ROOT': str(marker_root), 'UID_RANGE': '61000-61999',
           'PORTS': '8000,8090,8091', 'LINK_PORTS': '18100:18199', 'PREVIEW_PORTS': '19000:19999', 'MODE': mode, 'CALLS': str(calls),
           'FAIL_RULE': fail, 'FAIL_RULE_2': fail2, 'IPV4_FAIL_RULE': ipv4_fail,
           'IPV4_FAIL_RULE_2': '', 'IPV6_UNAVAILABLE': '1' if unavailable else '0'}
    result = subprocess.run(['/bin/sh', '-c', 'set -eu\n' + _block()], env=env,
                            capture_output=True, text=True, timeout=5)
    return result, marker_root, calls.read_text() if calls.exists() else ''


@pytest.mark.parametrize(('fail', 'fail2', 'missing'), [
    ('', '', True),
    ('-L', '', False),
    ('-N BOSS_SANDBOX', '-F BOSS_SANDBOX', False),
    ('-A BOSS_SANDBOX -o lo -d ::1/128 -j ACCEPT', '', False),
    ('-A BOSS_SANDBOX -j REJECT', '', False),
    ('-A OUTPUT -m owner --uid-owner 61000-61999 -j BOSS_SANDBOX', '', False),
    ('-C BOSS_SANDBOX -o lo -d ::1/128 -j ACCEPT', '', False),
    ('-C BOSS_SANDBOX -j REJECT', '', False),
    ('-C OUTPUT -m owner --uid-owner 61000-61999 -j BOSS_SANDBOX', '', False),
])
def test_every_ipv6_failure_holds_builds_and_removes_old_fenced_marker(tmp_path, fail, fail2, missing):
    result, root, _ = _run(tmp_path, fail=fail, fail2=fail2, missing=missing)
    assert result.returncode == 0, result.stderr  # Console remains available in enforce mode.
    assert not (root / '.network-fenced').exists()
    assert (root / '.network-unfenced').is_file()
    assert 'builds are held' in result.stderr


def test_verified_ipv6_rules_allow_fenced_marker(tmp_path):
    result, root, calls = _run(tmp_path)
    assert result.returncode == 0 and (root / '.network-fenced').is_file()
    assert not (root / '.network-unfenced').exists()
    assert 'ip6tables -C OUTPUT -m owner --uid-owner 61000-61999 -j BOSS_SANDBOX' in calls


def test_existing_ipv6_chain_is_flushed_and_verified(tmp_path):
    result, root, calls = _run(tmp_path, fail='-N BOSS_SANDBOX')
    assert result.returncode == 0 and (root / '.network-fenced').exists()
    assert 'ip6tables -F BOSS_SANDBOX' in calls


def test_unavailable_ip6tables_restore_does_not_report_ready(tmp_path):
    """IPv6 filtering that cannot be installed must not leave the IPv4 fence marked ready."""
    result, root, calls = _run(tmp_path, restore_missing=True)
    assert result.returncode == 0, result.stderr
    assert not (root / '.network-fenced').exists()
    assert (root / '.network-unfenced').is_file()
    assert 'builds are held' in result.stderr
    assert 'iptables -A OUTPUT -m owner --uid-owner 61000-61999 -j BOSS_SANDBOX' in calls
    assert 'ip6tables -L' in calls


def test_positive_kernel_unavailability_can_skip_missing_ipv6_tools(tmp_path):
    result, root, calls = _run(tmp_path, missing=True, unavailable=True)
    assert result.returncode == 0 and (root / '.network-fenced').exists()
    assert 'ip6tables' not in calls


def test_required_mode_refuses_start_after_ipv6_failure(tmp_path):
    result, root, _ = _run(tmp_path, fail='-L', mode='required')
    assert result.returncode == 1 and not (root / '.network-fenced').exists()
    assert 'refusing to start' in result.stderr


def test_explicit_off_removes_markers_and_skips_both_families(tmp_path):
    result, root, calls = _run(tmp_path, missing=True, mode='off')
    assert result.returncode == 0 and calls == ''
    assert not (root / '.network-fenced').exists() and not (root / '.network-unfenced').exists()


def test_ipv4_failure_still_holds_builds(tmp_path):
    result, root, _ = _run(tmp_path, ipv4_fail='-A BOSS_SANDBOX -j REJECT')
    assert result.returncode == 0 and not (root / '.network-fenced').exists()
    assert (root / '.network-unfenced').is_file()


@pytest.mark.parametrize(('error', 'expected'), [
    (errno.EAFNOSUPPORT, 0), (errno.EPROTONOSUPPORT, 0), (errno.EPERM, 1),
    (errno.EINVAL, 1), (None, 1),
])
def test_actual_kernel_probe_accepts_only_unavailable_protocol_errors(error, expected):
    source = SOURCE.read_text()
    if 'ipv6_unavailable() {' not in source:
        pytest.fail('baseline has no positive kernel IPv6 unavailability probe')
    code = source.split("ipv6_unavailable() {", 1)[1].split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    fixture = ('import socket\n'
               f'fixture_error = {error!r}\n'
               'original_socket = socket.socket\n'
               'def controlled_socket(*args, **kwargs):\n'
               '    if fixture_error is not None: raise OSError(fixture_error, "owned probe")\n'
               '    return original_socket(*args, **kwargs)\n'
               'socket.socket = controlled_socket\n')
    result = subprocess.run([sys.executable, '-c', fixture + code], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == expected, result.stderr
