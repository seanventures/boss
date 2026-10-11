"""Actual shell selection blocks with inert Docker and sudo env-reset shims only."""
import json,os,pathlib,subprocess,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[2]
SCRIPTS=['install.sh',*[f'scripts/install/{n}.sh' for n in ('boss','backup','uninstall','restore','bootstrap_infisical')]]
BEGIN='# ---- scoped sudo Docker fallback '
END='# ---- end scoped sudo Docker fallback '
def guard(source):
 a=source.index(BEGIN);b=source.index('\n',source.index(END,a));return source[a:b]+'\n'
def block(name):
 s=(ROOT/name).read_text();g=guard(s)
 if name.endswith('boss.sh'):
  return g+s[s.index('docker_cmd() {'):s.index('\ncompose() {')]+'\ndocker_cmd\n"${DOCKER[@]}" compose config\n'
 if name.endswith('bootstrap_infisical.sh'):
  return g+s[s.index('case "${BOSS_DOCKER_USE_SUDO:-auto}"'):s.index('# Generated if you did not supply one')]+ '\ndocker_compose config\n'
 start=s.index('if ! docker info >/dev/null 2>&1; then',s.index(END))
 stop=s.index('ui_ok "Docker daemon is running"',start) if name=='install.sh' else s.index('# A directory lock',start) if name.endswith('restore.sh') else s.index('# The images are tagged',start)
 return g+'DOCKER=(docker)\n'+s[start:stop]+('\n"${DOCKER[@]}" compose -p "$PROJECT" config\n' if name.endswith('restore.sh') else '\n"${DOCKER[@]}" compose config\n')

class SudoSelection(unittest.TestCase):
 def run_case(self,name,extra=None,dotenv='',case=None,picker=False):
  with tempfile.TemporaryDirectory(prefix='boss-inert-selection-') as temp:
   root=pathlib.Path(temp);bindir=root/'bin';bindir.mkdir();envfile=root/'.env';envfile.write_text(dotenv);log=root/'events.jsonl';casefile=root/'case.json';settings={'direct':False,'context':'default','endpoint':'unix:///var/run/docker.sock','context_exit':0,'inspect_exit':0,'sudo_info':True,'os':'Linux','plugin':True,**(case or {})};casefile.write_text(json.dumps(settings))
   docker=bindir/'docker';docker.write_text('#!'+sys.executable+'\n'+'''import os,sys,json
case=json.load(open(os.environ['CASE_FILE']));args=sys.argv[1:];priv=os.environ.get('PRIVILEGED')=='1'
host=None
if args[:1]==['--host']:host=args[1];args=args[2:]
row={'args':sys.argv[1:],'privileged':priv,'selected_host':host or ('root-saved-context' if priv else os.environ.get('DOCKER_HOST') or case['endpoint']),'project':os.environ.get('COMPOSE_PROJECT_NAME'),'tag':os.environ.get('BOSS_COMMUNITY_IMAGE_TAG'),'private_sentinel':bool(os.environ.get('PRIVATE_SENTINEL'))}
with open(os.environ['EVENT_LOG'],'a') as out:out.write(json.dumps(row)+'\\n')
if args==['info']:sys.exit(0 if (case['sudo_info'] if priv else case['direct']) else 1)
if args==['context','show']:print(case['context']);sys.exit(case['context_exit'])
if args[:3]==['context','inspect','default']:print(case['endpoint']);sys.exit(case['inspect_exit'])
if args==['compose','version']:sys.exit(0 if case['plugin'] else 1)
if args==['version']:print('inert standalone');sys.exit(0)
if args==['config']:print(json.dumps(row));sys.exit(0)
if args[:1]==['compose']:print(json.dumps(row));sys.exit(0)
sys.exit(91)
''');docker.chmod(0o755);standalone=bindir/'docker-compose';standalone.write_bytes(docker.read_bytes());standalone.chmod(0o755)
   uname=bindir/'uname';uname.write_text('#!'+sys.executable+'\nimport json,os;print(json.load(open(os.environ["CASE_FILE"]))["os"])\n');uname.chmod(0o755)
   pre='''set -eu
OS=Linux;NONINTERACTIVE=1;DOCKER=(docker)
id() { if [ "$1" = -u ]; then printf '1001\\n'; else /usr/bin/id "$@"; fi; }
need_cmd() { command -v "$1" >/dev/null 2>&1; }
die() { printf '%s\\n' "$*" >&2; exit 92; }
ui_info() { :; }
sudo() { [ "$1" = -n ] && shift; /usr/bin/env -i PATH="$PATH" CASE_FILE="$CASE_FILE" EVENT_LOG="$EVENT_LOG" PRIVILEGED=1 "$@"; }
'''
   env={'PATH':str(bindir)+':/usr/bin:/bin','CASE_FILE':str(casefile),'EVENT_LOG':str(log),'ENV_FILE':str(envfile),'INSTALL_DIR':str(root),'BACKUP':str(root),'PROJECT':'inert-recovery','PRIVATE_SENTINEL':'inert-not-a-credential',**(extra or {})}
   body=block(name)
   if picker:
    src=(ROOT/'install.sh').read_text();part=src[src.index('if "${DOCKER[@]}" compose version'):src.index('ui_ok "Docker Compose:')];body=body.rsplit('\n"${DOCKER[@]}" compose config',1)[0]+'\n'+part+'\n"${COMPOSE[@]}" config\n'
   result=subprocess.run(['/bin/bash','-c',pre+body],env=env,capture_output=True,timeout=10);events=[json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
   return result,events,(root/'unexpected').exists()
 def all_refused(self,extra=None,dotenv='',case=None,selection=False):
  for name in SCRIPTS:
   if selection and name.endswith('restore.sh'):continue
   with self.subTest(script=name):
    r,events,_=self.run_case(name,extra,dotenv,case);self.assertNotEqual(r.returncode,0);self.assertFalse(any(e['privileged'] for e in events));self.assertFalse(any('compose' in e['args'] for e in events));self.assertNotIn('inert-not-a-credential',r.stderr.decode())
 def test_same_bounded_guard_in_all_six_scripts(self):self.assertEqual(len({guard((ROOT/n).read_text()) for n in SCRIPTS}),1)
 def test_default_linux_sudo_targets_fixed_socket_not_root_context(self):
  for name in SCRIPTS:
   with self.subTest(script=name):
    r,events,_=self.run_case(name);self.assertEqual(r.returncode,0,r.stderr.decode());priv=[e for e in events if e['privileged']];self.assertTrue(priv);self.assertTrue(all(e['args'][:2]==['--host','unix:///var/run/docker.sock'] for e in priv));self.assertTrue(all(not e['private_sentinel'] for e in priv));self.assertEqual(priv[-1]['args'][2:4],['compose','-p'] if name.endswith('restore.sh') else ['compose','config'])
 def test_direct_custom_project_tag_and_host_preserved(self):
  env={'COMPOSE_PROJECT_NAME':'second','BOSS_COMMUNITY_IMAGE_TAG':'second-v2','DOCKER_HOST':'tcp://inert.invalid:2376','DOCKER_CONTEXT':'chosen'}
  for name in SCRIPTS:
   with self.subTest(script=name):
    r,events,_=self.run_case(name,env,case={'direct':True,'context':'chosen'});self.assertEqual(r.returncode,0,r.stderr.decode());self.assertFalse(any(e['privileged'] for e in events));self.assertEqual(events[-1]['project'],'second');self.assertEqual(events[-1]['tag'],'second-v2');self.assertEqual(events[-1]['selected_host'],env['DOCKER_HOST'])
 def test_custom_project_rejected(self):self.all_refused({'COMPOSE_PROJECT_NAME':'second'},selection=True)
 def test_custom_tag_rejected(self):self.all_refused({'BOSS_COMMUNITY_IMAGE_TAG':'second-v2'},selection=True)
 def test_explicit_host_even_local_refuses_sudo(self):self.all_refused({'DOCKER_HOST':'unix:///var/run/docker.sock'})
 def test_explicit_context_refuses_sudo(self):self.all_refused({'DOCKER_CONTEXT':'default'})
 def test_explicit_config_refuses_sudo(self):self.all_refused({'DOCKER_CONFIG':'/inert/config'})
 def test_tls_selections_refuse_sudo(self):
  for key in ['DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH']:
   with self.subTest(key=key):self.all_refused({key:'inert'})
 def test_saved_nondefault_context_refuses_sudo(self):self.all_refused(case={'context':'other'})
 def test_saved_context_endpoint_not_local_refuses_sudo(self):self.all_refused(case={'endpoint':'tcp://inert.invalid:2376'})
 def test_context_inspect_failure_refuses_sudo(self):self.all_refused(case={'inspect_exit':1})
 def test_context_show_failure_refuses_sudo(self):self.all_refused(case={'context_exit':1})
 def test_darwin_sudo_refused(self):self.all_refused(case={'os':'Darwin'})
 def test_dotenv_custom_selection_refused_without_contents_in_error(self):self.all_refused(selection=True,dotenv='COMPOSE_PROJECT_NAME=second\nPRIVATE_SECRET=inert-not-a-credential\n')
 def test_dotenv_custom_image_refused(self):self.all_refused(selection=True,dotenv='BOSS_COMMUNITY_IMAGE_TAG=second-v2\n')
 def test_dotenv_duplicate_default_refused(self):self.all_refused(selection=True,dotenv='COMPOSE_PROJECT_NAME=boss-community\nCOMPOSE_PROJECT_NAME=boss-community\n')
 def test_dotenv_interpolation_refused(self):self.all_refused(selection=True,dotenv='BOSS_COMMUNITY_IMAGE_TAG=${OTHER}\n')
 def test_dotenv_export_spaces_or_quotes_refused(self):
  for line in ['export COMPOSE_PROJECT_NAME=boss-community',' COMPOSE_PROJECT_NAME=boss-community','COMPOSE_PROJECT_NAME =boss-community','COMPOSE_PROJECT_NAME="boss-community"']:
   with self.subTest(line=line):self.all_refused(selection=True,dotenv=line+'\n')
 def test_dotenv_only_two_literal_fields_inspected_no_eval(self):
  for name in SCRIPTS:
   with self.subTest(script=name):
    r,events,sideeffect=self.run_case(name,dotenv='COMPOSE_PROJECT_NAME=boss-community\nBOSS_COMMUNITY_IMAGE_TAG=\nIGNORED=$(touch "$INSTALL_DIR/unexpected")\n');self.assertEqual(r.returncode,0,r.stderr.decode());self.assertFalse(sideeffect)
 def test_denied_sudo_does_not_attempt_compose(self):
  for name in SCRIPTS:
   with self.subTest(script=name):
    r,events,_=self.run_case(name,case={'sudo_info':False});self.assertNotEqual(r.returncode,0);self.assertFalse(any('compose' in e['args'] for e in events))
 def test_forced_infisical_sudo_checks_selection(self):
  r,events,_=self.run_case(SCRIPTS[-1],{'BOSS_DOCKER_USE_SUDO':'1','COMPOSE_PROJECT_NAME':'second'});self.assertNotEqual(r.returncode,0);self.assertFalse(any(e['privileged'] for e in events))
 def test_forced_infisical_direct_keeps_custom_selection(self):
  r,events,_=self.run_case(SCRIPTS[-1],{'BOSS_DOCKER_USE_SUDO':'0','COMPOSE_PROJECT_NAME':'second'},case={'direct':True});self.assertEqual(r.returncode,0);self.assertEqual(events[-1]['project'],'second');self.assertFalse(any(e['privileged'] for e in events))

 def test_colon_dotenv_selectors_are_refused(self):
  for line in ['COMPOSE_PROJECT_NAME:second','BOSS_COMMUNITY_IMAGE_TAG:second-v2']:
   with self.subTest(line=line):self.all_refused(dotenv=line+'\n',selection=True)
 def test_sudo_plugin_failure_cannot_fall_back_to_standalone(self):
  r,events,_=self.run_case('install.sh',case={'plugin':False},picker=True);self.assertNotEqual(r.returncode,0);self.assertFalse(any(e['args']==['version'] for e in events));self.assertFalse(any(e['args'][-1:] == ['config'] for e in events))
 def test_direct_plugin_failure_retains_existing_standalone_choice(self):
  r,events,_=self.run_case('install.sh',case={'plugin':False,'direct':True},picker=True);self.assertEqual(r.returncode,0,r.stderr.decode());self.assertTrue(any(e['args']==['config'] for e in events));self.assertFalse(any(e['privileged'] for e in events))
 def test_restore_custom_explicit_target_and_archive_fields_do_not_select_daemon(self):
  r,events,_=self.run_case('scripts/install/restore.sh',{'COMPOSE_PROJECT_NAME':'old','BOSS_COMMUNITY_IMAGE_TAG':'old-image'},'COMPOSE_PROJECT_NAME=archived\nBOSS_COMMUNITY_IMAGE_TAG=archived-image\n');self.assertEqual(r.returncode,0,r.stderr.decode());self.assertEqual(events[-1]['args'],['--host','unix:///var/run/docker.sock','compose','-p','inert-recovery','config'])
  src=(ROOT/'scripts/install/restore.sh').read_text();self.assertIn('boss_sudo_scope_safe "$BACKUP/.env" restore',src);self.assertIn('compose -p "$PROJECT"',src)

 def test_bom_prefixed_custom_selection_refused(self):
  self.all_refused(dotenv='\ufeffCOMPOSE_PROJECT_NAME=second\n',selection=True)
 def test_bom_prefixed_default_selection_is_checked(self):
  r,events,_=self.run_case('install.sh',dotenv='\ufeffCOMPOSE_PROJECT_NAME=boss-community\n');self.assertEqual(r.returncode,0,r.stderr.decode());self.assertTrue(events[-1]['privileged'])

if __name__=='__main__':unittest.main()
