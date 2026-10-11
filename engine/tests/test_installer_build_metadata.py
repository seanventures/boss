"""Local Git/archive and shell controls; no Docker daemon, network, or full install."""
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
BEGIN = '# ---- build metadata '
END = '# ---- end build metadata '


class BuildMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = {
            'PATH': '/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin',
            'HOME': str(self.home), 'LC_ALL': 'C',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_ALLOW_PROTOCOL': 'file',
        }
        self.seed = self.root / 'seed'
        self.seed.mkdir()
        self.git('init', '-q', str(self.seed))
        self.git('-C', str(self.seed), 'config', 'user.name', 'Local fixture')
        self.git('-C', str(self.seed), 'config', 'user.email', 'fixture@example.invalid')
        (self.seed / 'source.txt').write_text('original source\n')
        (self.seed / '.gitignore').write_text('.env\n.boss-release\nboss-community.env.sh\n')
        self.git('-C', str(self.seed), 'add', '.')
        self.git('-C', str(self.seed), 'commit', '-qm', 'local fixture')
        self.sha = self.git('-C', str(self.seed), 'rev-parse', 'HEAD').strip()
        self.tree = self.root / 'install'
        self.git('clone', '-q', '--no-local', str(self.seed), str(self.tree))

    def git(self, *args):
        result = subprocess.run(['/usr/bin/git', *args], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, 'local Git fixture failed')
        return result.stdout

    def block(self):
        source = (ROOT / 'install.sh').read_text()
        start = source.index(BEGIN)
        end = source.index('\n', source.index(END, start))
        return source[start:end]

    def run_metadata(self, *, tree=None, extra_env=None, prefix='', suffix=''):
        tree = tree or self.tree
        script = 'set -euo pipefail\nINSTALL_DIR=' + shlex.quote(str(tree)) + '\n'
        script += 'ui_warn() { printf "warning\\n" >&2; };\n' + prefix + '\n'
        script += self.block() + '\n' + suffix
        script += '\nprintf "%s\\n%s\\n" "$BOSS_SOURCE_SHA" "$BOSS_BUILT_AT"\n'
        result = subprocess.run(['/bin/bash', '-c', script],
                                env={**self.env, **(extra_env or {})},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, 'metadata selection unexpectedly failed')
        rows = result.stdout.splitlines()
        self.assertEqual(len(rows), 2)
        return rows, result.stderr

    def test_fresh_real_local_clone_sets_its_actual_commit_and_utc_time(self):
        before = datetime.now(timezone.utc)
        (source, stamp), warnings = self.run_metadata()
        after = datetime.now(timezone.utc)
        self.assertEqual(source, self.sha)
        observed = datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        self.assertLessEqual(before.replace(microsecond=0), observed)
        self.assertLessEqual(observed, after)
        self.assertEqual(warnings, '')

    def test_detached_checkout_uses_commit_not_requested_branch_name(self):
        self.git('-C', str(self.tree), 'checkout', '-q', '--detach', self.sha)
        (source, _), _ = self.run_metadata(extra_env={'BOSS_REPO_REF': 'fictional-release'})
        self.assertEqual(source, self.sha)

    def test_real_git_worktree_file_supported(self):
        work = self.root / 'worktree'
        self.git('-C', str(self.seed), 'worktree', 'add', '-q', '--detach', str(work), self.sha)
        self.assertTrue((work / '.git').is_file())
        self.assertEqual(self.run_metadata(tree=work)[0][0], self.sha)

    def test_dirty_tracked_bytes_are_unknown(self):
        (self.tree / 'source.txt').write_text('local changes\n')
        self.assertEqual(self.run_metadata()[0][0], 'unknown')

    def test_staged_changes_are_unknown(self):
        (self.tree / 'source.txt').write_text('staged changes\n')
        self.git('-C', str(self.tree), 'add', 'source.txt')
        self.assertEqual(self.run_metadata()[0][0], 'unknown')

    def test_untracked_source_is_unknown(self):
        (self.tree / 'extra.py').write_text('untracked source\n')
        self.assertEqual(self.run_metadata()[0][0], 'unknown')

    def test_ignored_installer_state_does_not_obscure_clean_revision(self):
        for name in ['.env', '.boss-release', 'boss-community.env.sh']:
            (self.tree / name).write_text('synthetic fixture state\n')
        self.assertEqual(self.run_metadata()[0][0], self.sha)

    def test_real_git_archive_has_no_invented_revision_or_marker_trust(self):
        archive = subprocess.run(['/usr/bin/git', '-C', str(self.seed), 'archive', 'HEAD'],
                                 env=self.env, capture_output=True, check=True).stdout
        packaged = self.root / 'packaged'
        packaged.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as content:
            for member in content:
                self.assertTrue(member.isfile())
                (packaged / member.name).write_bytes(content.extractfile(member).read())
        (packaged / '.boss-shipped-commit').write_text(self.sha + '\n')
        (source, stamp), warnings = self.run_metadata(tree=packaged,
                                                    extra_env={'BOSS_SOURCE_SHA': self.sha})
        self.assertEqual(source, 'unknown')
        self.assertNotEqual(stamp, 'unknown')
        self.assertEqual(warnings, 'warning\n')

    def test_packaged_directory_cannot_inherit_parent_git_revision(self):
        nested = self.tree / 'package'
        nested.mkdir()
        (nested / 'source.txt').write_text('unrelated package\n')
        self.assertEqual(self.run_metadata(tree=nested)[0][0], 'unknown')

    def test_stale_caller_metadata_is_replaced_by_observation(self):
        values, _ = self.run_metadata(extra_env={'BOSS_SOURCE_SHA': 'f' * 40,
                                                'BOSS_BUILT_AT': '2001-01-01T00:00:00Z'})
        self.assertEqual(values[0], self.sha)
        self.assertNotEqual(values[1], '2001-01-01T00:00:00Z')

    def test_git_read_error_stays_unknown_and_does_not_fail_build(self):
        self.assertEqual(self.run_metadata(prefix='git() { return 2; };')[0][0], 'unknown')

    def test_noncommit_git_output_is_not_exported(self):
        prefix = '''git() {
 if [[ "$*" == *"rev-parse --verify HEAD"* ]]; then printf '%s\\n' not-a-commit; else /usr/bin/git "$@"; fi
};'''
        self.assertEqual(self.run_metadata(prefix=prefix)[0][0], 'unknown')

    def test_date_failure_keeps_known_revision_and_unknown_time(self):
        self.assertEqual(self.run_metadata(prefix='date() { return 1; };')[0],
                         [self.sha, 'unknown'])

    def test_malformed_clock_output_is_not_exported(self):
        self.assertEqual(self.run_metadata(prefix='date() { printf "local time\\n"; };')[0],
                         [self.sha, 'unknown'])

    def test_metadata_uses_only_local_read_commands_no_auth_or_requests(self):
        calls = self.root / 'calls'
        prefix = '''git() {
 printf '%s\\n' "$*" >> "$CALLS"
 case "$*" in
  *"rev-parse --show-toplevel"|*"rev-parse --verify HEAD"|*"status --porcelain --untracked-files=normal") /usr/bin/git "$@";;
  *) return 91;;
 esac
};
curl() { return 92; }; ssh() { return 92; }; docker() { return 92; };
'''
        values, _ = self.run_metadata(prefix=prefix, extra_env={'CALLS': str(calls)})
        self.assertEqual(values[0], self.sha)
        self.assertEqual(len(calls.read_text().splitlines()), 3)
        self.assertNotIn('fetch', calls.read_text())
        self.assertNotIn('remote', calls.read_text())

    def test_one_timestamp_and_revision_reaches_every_existing_serial_build(self):
        source = (ROOT / 'install.sh').read_text()
        serial = source[source.index('# ---- serial image builds '):source.index('# ---- end serial image builds ')]
        capture = self.root / 'build-env.jsonl'
        count = self.root / 'date-count'
        prefix = '''date() { printf 'called\\n' >> "$DATE_COUNT"; printf '2026-10-11T12:34:56Z\\n'; };
'''
        suffix = '''
compose() {
 if [[ "$*" == *"config --format json"* ]]; then
  printf '%s\\n' '{"services":{"api":{"build":{}},"worker":{"build":{}},"mcp-tools":{"build":{}},"egress":{"build":{}},"web":{"build":{},"profiles":["web"]}}}'
 else
  python3 -c 'import json,os,sys; f=open(os.environ["BUILD_CAPTURE"],"a"); f.write(json.dumps({"role":sys.argv[-1],"revision":os.environ["BOSS_SOURCE_SHA"],"at":os.environ["BOSS_BUILT_AT"]})+"\\n")' "$@"
 fi
};
ui_info() { :; }; die() { exit 93; }; COMPOSE=(compose); COMPOSE_PROFILES='--profile web'; ENV_FILE=unused
''' + serial
        self.run_metadata(prefix=prefix, suffix=suffix,
                          extra_env={'BUILD_CAPTURE': str(capture), 'DATE_COUNT': str(count)})
        rows = [json.loads(line) for line in capture.read_text().splitlines()]
        self.assertEqual([r['role'] for r in rows], ['api', 'worker', 'mcp-tools', 'egress', 'web'])
        self.assertEqual({r['revision'] for r in rows}, {self.sha})
        self.assertEqual({r['at'] for r in rows}, {'2026-10-11T12:34:56Z'})
        self.assertEqual(count.read_text(), 'called\n')

    def test_source_and_index_bytes_remain_unchanged(self):
        index = self.tree / '.git/index'
        before = index.read_bytes()
        files = {name: (self.tree / name).read_bytes() for name in ['source.txt', '.gitignore']}
        self.run_metadata()
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual({name: (self.tree / name).read_bytes() for name in files}, files)


class SudoBuildMetadata(unittest.TestCase):
    # Reuse only fixture helpers, not the parent's seventeen tests.
    setUp = BuildMetadata.setUp
    git = BuildMetadata.git
    block = BuildMetadata.block
    run_metadata = BuildMetadata.run_metadata

    def build_child(self, *, tree=None, prefix='', reset=True):
        capture = self.root / 'sudo-build-child.jsonl'
        docker = self.root / 'inert-docker'
        shim = self.root / 'inert-sudo-env-reset'
        python = __import__('sys').executable
        docker.write_text('#!' + python + '\n' + '''import json,os,sys
args=sys.argv[1:]
if args[0]!='compose':raise SystemExit(90)
if args[-3:]==['config','--format','json']:
 print(json.dumps({'services':{k:{'build':{}} for k in ['api','egress','mcp-tools','web','worker']}}))
else:
 if 'build' not in args:raise SystemExit(91)
 tail=args[args.index('build')+1:];values={};role=tail[-1];rest=tail[:-1]
 if len(rest)%2:raise SystemExit(92)
 for flag,assignment in zip(rest[::2],rest[1::2]):
  if flag!='--build-arg' or '=' not in assignment:raise SystemExit(93)
  key,value=assignment.split('=',1)
  if key not in ['BOSS_SOURCE_SHA','BOSS_BUILT_AT'] or key in values:raise SystemExit(94)
  values[key]=value
 row={'role':role,'explicit':values,'revision':values.get('BOSS_SOURCE_SHA',os.environ.get('BOSS_SOURCE_SHA','unknown')),'at':values.get('BOSS_BUILT_AT',os.environ.get('BOSS_BUILT_AT','unknown')),'metadata_env_present':any(k in os.environ for k in ['BOSS_SOURCE_SHA','BOSS_BUILT_AT']),'credential_marker_present':'OPENAI_API_KEY' in os.environ}
 with open(''' + repr(str(capture)) + ''','a') as f:f.write(json.dumps(row)+'\\n')
''')
        docker.chmod(0o700)
        shim.write_text('#!' + python + '\n' + '''import os,sys
if sys.argv[1:3]!=['-n',''' + repr(str(docker)) + ''']:raise SystemExit(95)
# A documented env_reset model, not sudo or a privilege transition.
os.execve(sys.argv[2],sys.argv[2:],{'PATH':'/usr/bin:/bin:/opt/homebrew/bin','LC_ALL':'C','HOME':''' + repr(str(self.home)) + '''})
''')
        shim.chmod(0o700)
        source = (ROOT / 'install.sh').read_text()
        serial = source[source.index('# ---- serial image builds '):source.index('# ---- end serial image builds ')]
        command = [str(shim), '-n', str(docker), 'compose'] if reset else [str(docker), 'compose']
        suffix = 'ui_info() { :; }; die() { exit 93; }; COMPOSE=(' + ' '.join(shlex.quote(v) for v in command) + "); COMPOSE_PROFILES='--profile web'; ENV_FILE=unused\n" + serial
        self.run_metadata(tree=tree,prefix=prefix,suffix=suffix,extra_env={'OPENAI_API_KEY':'synthetic-marker-never-forward'})
        return [json.loads(line) for line in capture.read_text().splitlines()]

    def test_real_clone_metadata_crosses_env_reset_as_two_explicit_build_args(self):
        rows = self.build_child(prefix="date() { printf '2026-10-11T12:34:56Z\\n'; };")
        self.assertEqual([r['role'] for r in rows], ['api','egress','mcp-tools','web','worker'])
        for row in rows:
            self.assertEqual(row['revision'],self.sha)
            self.assertEqual(row['at'],'2026-10-11T12:34:56Z')
            self.assertEqual(set(row['explicit']),{'BOSS_SOURCE_SHA','BOSS_BUILT_AT'})
            self.assertFalse(row['metadata_env_present'])
            self.assertFalse(row['credential_marker_present'])

    def test_packaged_source_unknown_crosses_reset_without_inventing_revision(self):
        package = self.root/'package';package.mkdir();(package/'source.txt').write_text('packaged source')
        rows=self.build_child(tree=package)
        self.assertEqual({r['revision'] for r in rows},{'unknown'})
        self.assertTrue(all(set(r['explicit'])=={'BOSS_SOURCE_SHA','BOSS_BUILT_AT'} for r in rows))
        self.assertTrue(all(not r['metadata_env_present'] and not r['credential_marker_present'] for r in rows))

    def test_dirty_checkout_and_unavailable_clock_remain_explicitly_unknown(self):
        (self.tree/'source.txt').write_text('changed')
        rows=self.build_child(prefix='date() { return 1; };')
        self.assertTrue(all(r['explicit']=={'BOSS_SOURCE_SHA':'unknown','BOSS_BUILT_AT':'unknown'} for r in rows))

    def test_non_sudo_build_also_gets_only_two_explicit_metadata_args(self):
        rows=self.build_child(reset=False)
        self.assertTrue(all(r['revision']==self.sha and set(r['explicit'])=={'BOSS_SOURCE_SHA','BOSS_BUILT_AT'} for r in rows))
        # Existing direct-shell inheritance is unchanged, never promoted into build args.
        self.assertTrue(all(r['credential_marker_present'] for r in rows))

    def test_sudo_policy_and_unrelated_daemon_project_configuration_unchanged(self):
        source=(ROOT/'install.sh').read_text()
        self.assertEqual(source.count('DOCKER=(sudo -n docker)'),2)
        self.assertIn('COMPOSE=("${DOCKER[@]}" compose)',source)
        serial=source[source.index('# ---- serial image builds '):source.index('# ---- end serial image builds ')]
        self.assertNotIn('--preserve-env',serial);self.assertNotIn('sudo -E',serial)
        self.assertNotIn('--host',serial);self.assertNotIn('--project-name',serial)
        self.assertEqual(serial.count('--build-arg'),2)


if __name__ == '__main__':
    unittest.main()
