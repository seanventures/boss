"""Re-check 4 of tm/fixes (2026-10-05): a known id's hash is required, not merely compared (4);
uvicorn trusts forwarded headers only from BOSS_TRUSTED_PROXIES, and a trusted proxy's name is
resolved off the event loop with no cached failures (3)."""
from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

from sqlalchemy import text

from apps.api.tests.improve_fixtures import project, reset_state

ROOT = Path(__file__).resolve().parents[3]


def _collector_on(engine):
    from boss_engine.services.improve_boss import set_switch

    with engine.begin() as c:
        set_switch(c, "collector", True, actor="ceo", project_id=project())


def _post(client, path, body):
    return client.post(path, content=gzip.compress(json.dumps(body).encode()),
                       headers={"Content-Encoding": "gzip"})


def _heartbeat(iid, secret_hash=None):
    env = {"v": 1, "day": date.today().isoformat(), "install_id": iid,
           "heartbeat": {"install_id": iid, "version": "1.0.0", "os": "linux"}}
    if secret_hash is not None:
        env["secret_hash"] = secret_hash
    return env


# ------------------------------------------------------------------ 4
def test_a_known_id_refuses_a_heartbeat_without_its_hash(engine, client):
    from boss_engine.services.improve_collector import registered_hash

    reset_state(engine)
    _collector_on(engine)
    iid = "bi_" + "h" * 26
    assert _post(client, "/v1/telemetry/ingest", _heartbeat(iid, "a" * 64)).status_code == 200
    bare = _post(client, "/v1/telemetry/ingest", _heartbeat(iid))
    assert bare.status_code == 409 and bare.json()["detail"] == "install_id_taken"
    assert _post(client, "/v1/telemetry/ingest", _heartbeat(iid, "b" * 64)).status_code == 409
    with engine.begin() as c:
        assert registered_hash(c, iid) == "a" * 64
        assert c.execute(text("SELECT COUNT(*) FROM tm_heartbeats WHERE install_id = :i"),
                         {"i": iid}).scalar() == 1


def test_a_known_id_refuses_a_share_with_another_hash_or_none(engine, client):
    reset_state(engine)
    _collector_on(engine)
    iid = "bi_" + "j" * 26
    assert _post(client, "/v1/telemetry/ingest", _heartbeat(iid, "a" * 64)).status_code == 200
    for offered in ("f" * 64, None):
        share = {"v": 1, "install_id": iid, "bundle": "x"}
        if offered:
            share["secret_hash"] = offered
        r = _post(client, "/v1/telemetry/share", share)
        assert r.status_code == 409 and r.json()["detail"] == "install_id_taken", offered
    ok = _post(client, "/v1/telemetry/share", {"v": 1, "install_id": iid, "bundle": "x", "secret_hash": "a" * 64})
    assert ok.status_code == 200
    with engine.begin() as c:
        rows = c.execute(text("SELECT secret_hash FROM tm_shares WHERE install_id = :i"), {"i": iid}).scalars().all()
    assert rows == ["a" * 64]


def test_a_new_id_still_registers_without_a_hash_check(engine, client):
    reset_state(engine)
    _collector_on(engine)
    iid = "bi_" + "k" * 26
    assert _post(client, "/v1/telemetry/share",
                 {"v": 1, "install_id": iid, "bundle": "x", "secret_hash": "c" * 64}).status_code == 200
    assert _post(client, "/v1/telemetry/ingest", _heartbeat(iid, "c" * 64)).status_code == 200


# ------------------------------------------------------------------ 3
def test_uvicorn_is_given_the_trusted_proxies_and_nothing_else():
    from apps.api import serve

    assert serve.forwarded_allow_ips("127.0.0.0/8,::1/128,web") == "127.0.0.0/8,::1/128"
    assert serve.forwarded_allow_ips("") == ""
    assert serve.forwarded_allow_ips("10.0.0.5, 10.0.0.0/8 ,garbage") == "10.0.0.5/32,10.0.0.0/8"
    argv = serve.uvicorn_argv(["--host", "0.0.0.0", "--forwarded-allow-ips", "*", "--port", "8000"], "")
    assert argv == ["apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000",
                    "--forwarded-allow-ips", ""]


def test_with_nothing_trusted_uvicorn_believes_no_loopback_header():
    """The point of the launcher: with BOSS_TRUSTED_PROXIES empty, a loopback caller's
    X-Forwarded-For does not become the client address."""
    from apps.api import serve
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"]

    import asyncio

    for trusted, expect in (("", "127.0.0.1"), ("127.0.0.0/8", "198.51.100.9")):
        mw = ProxyHeadersMiddleware(app, trusted_hosts=serve.forwarded_allow_ips(trusted))
        scope = {"type": "http", "client": ("127.0.0.1", 5000), "scheme": "http",
                 "headers": [(b"x-forwarded-for", b"198.51.100.9")]}
        asyncio.run(mw(scope, None, None))
        assert seen["client"][0] == expect, trusted


def test_every_launch_goes_through_the_launcher():
    assert 'CMD ["python", "-m", "apps.api.serve"' in (ROOT / "Dockerfile.community").read_text()
    assert '"-m", "apps.api.serve"' in (ROOT / "tests/e2e/harness/stack.py").read_text()
    assert "-m apps.api.serve " in (ROOT / "package.json").read_text()


def test_a_failed_lookup_is_not_cached(monkeypatch):
    import socket

    from apps.api.app.routers import improve

    improve._RESOLVED.clear()
    answers = [OSError("not yet"), [(0, 0, 0, "", ("172.18.0.5", 0))]]

    def fake(name, port):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert improve._resolve("web") == []
    assert "web" not in improve._RESOLVED
    assert improve._resolve("web") == ["172.18.0.5"]
    assert improve._RESOLVED["web"][1] == ["172.18.0.5"]
    improve._RESOLVED.clear()


def test_the_client_address_is_worked_out_off_the_event_loop():
    src = (ROOT / "apps/api/app/routers/improve.py").read_text()
    assert "ip = await run_in_threadpool(\n            client_ip," in src


def test_compose_ships_no_proxy_hop():
    compose = (ROOT / "scripts/install/community-compose.yml").read_text()
    assert "BOSS_CONSOLE_PROXY_HOPS: ${BOSS_CONSOLE_PROXY_HOPS:-0}" in compose
    assert "BOSS_CONSOLE_PROXY_HOPS=" in (ROOT / ".env.example").read_text()


# ------------------------------------------------------------------ 6
# What `docker compose config` (v2) answered for ${K} with each .env below — None means Compose
# refused the file. Recorded 2026-10-05; checkup.sh must agree on every one.
COMPOSE_ANSWERS = [
    ('plain', 'K=off\n', 'off'),
    ('export prefix', 'export K=off\n', 'off'),
    ('double quotes', 'K="off"\n', 'off'),
    ('single quotes', "K='off'\n", 'off'),
    ('space then comment', 'K=off # note\n', 'off'),
    ('tab then comment', 'K=off\t# note\n', 'off\t# note'),
    ('hash inside a word', 'K=a#b\n', 'a#b'),
    ('spaces around =', 'K = off\n', 'off'),
    ('tabs around =', 'K\t=\toff\n', 'off'),
    ('last definition wins', 'K=on\nK=off\n', 'off'),
    ('indented', '   K=off\n', 'off'),
    ('quoted, then comment', 'K="a b" # c\n', 'a b'),
    ('hash inside quotes', "K='a # b' # c\n", 'a # b'),
    ('unterminated double quote', 'K="off\n', None),
    ('unterminated quote on another key', 'Z="x\nK=off\n', None),
    ('empty', 'K=\n', ''),
    ('double quotes across lines', 'K="a\nb"\n', 'a\nb'),
    ('escaped double quote', 'K="a\\"b"\n', 'a"b'),
    ('space before a leading hash', 'K= #x\n', '#x'),
    ('CRLF line ending', 'K=off\r\n', 'off'),
]


def _checkup_env(tmp_path, content):
    import subprocess

    src = (ROOT / "scripts/install/checkup.sh").read_text()
    start = src.index("_env_awk='")
    end = src.index("\n}\n", src.index("env_file_error() {")) + 3
    env = tmp_path / ".env"
    env.write_bytes(content.encode())
    script = src[start:end] + '\nprintf "%s" "$(env_value K)"; printf "|"; env_file_error'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={"ENV_FILE": str(env), "PATH": "/usr/bin:/bin"})
    value, _, error = out.stdout.partition("|")
    return value, error.strip()


def test_checkup_reads_env_exactly_as_compose_does(tmp_path):
    wrong = []
    for name, content, compose in COMPOSE_ANSWERS:
        value, error = _checkup_env(tmp_path, content)
        got = None if error else value
        if got != compose:
            wrong.append((name, compose, got))
    assert wrong == []


def test_checkup_names_the_line_compose_refuses(tmp_path):
    assert _checkup_env(tmp_path, 'A=1\nZ="x\nK=off\n') == ("", "2")
    assert "a quote never closes" in (ROOT / "scripts/install/checkup.sh").read_text()


# ------------------------------------------------------------------ 5
# The independent tester's adversarial set (scratchpad/tmfix4-test/b.py, 2026-10-05), seeded the
# same way and measured the same way: a name "leaks" when it still starts a word in the bundle.
KNOWN = ["orbitalfin", "kestrel-api", "kestrel_api", "kestrelApi", "KestrelApi", "kestrelapi",
         "tern-ledger", "heron-sync", "heron_sync", "heronSync", "MarlinDesk", "marlin-desk",
         "marlin_desk", "marlinDesk", "MARLIN_DESK", "TernLedger", "tern_ledger", "vps-ams-12",
         "kingfisher", "heron-box", "heron-box.fernhill.lan", "10.44.0.9", "tern.fernhill.pl",
         "Raj Patel", "rpatel", "Patel", "Li Wei", "lwei", "Zoë Müller", "Müller", "zmuller",
         "Siobhán", "O'Connor", "soconnor", "Bluewater", "BLUEWATER", "Nightjar", "Larkspur Holdings",
         # compounds (re-check 4)
         "KestrelApiService", "kestrel-api2", "NightjarSSO", "MarlinDeskService", "MarlinDesks",
         # every workflow-input string, and the target's domains and public URL (re-check 4)
         "Quillfeather Dental", "Quillfeather", "pg-west-3", "quillfeather.pl", "orbital-backups",
         "fernhill-ops", "Fernhill", "ledger.bluewater.pl", "ternledger.rs", "app.ternledger.zip"]
#: Hosts no table names, found by shape: a public suffix, or a host-taking tool.
UNKNOWN_HOSTS = ["get.acme.sh", "mail.zephyrco.pl", "intranet.zephyrco.rs", "docs.zephyrco.md",
                 "files.zephyrco.zip", "zephyrco.gg", "zephyrco.ly", "zephyrco.to", "zephyrco.fm",
                 "pg.prod.svc", "redis.service.consul", "nas.fritz.box", "pg.zephyrco.vpn",
                 "grafana.zephyrco.io", "api.zephyrco.co.uk", "status.zephyrco.app",
                 "zephyrco.ts.net", "pg.default.svc.cluster.local"]
#: One label before a suffix that is also a file extension or a code member: a file by the
#: heuristic, unless a tool names it. Measured and reported, not asserted masked.
EXT_SHAPED = ["acme.sh", "zephyrco.py", "zephyrco.run", "zephyrco.so", "zephyrco.page",
              "zephyrco.name", "zephyrco.am", "zephyrco.mk"]
BARE = ["db-zeta-04", "stork-ci", "egret-gpu", "\\\\filesrv01\\share\\zeta"]
BENIGN = ["README.md", "deploy.sh", "setup.py", "Cargo.rs", "user.name", "Array.prototype.map",
          "lib.so", "Makefile.am", "build.mk", "os.path.join", "self.items", "package.json",
          "api.ts", "next.config.js", "styles.css", "index.html", "main.go", "pytest -q",
          "TypeError", "e.g.", "v1.2.3", "node_modules", "ELIFECYCLE", "useEffect", "config.yaml",
          "release manager", "Chief executive", "localhost:3000"]
ADVERSARIAL = "\n".join([
    "clone orbitalfin/kestrel-api failed; module kestrel_api, class KestrelApi, var kestrelApi, pkg kestrelapi; KestrelApiService; kestrel-api2; kestrel down",
    "branch feature/heron-sync (heron_sync, heronSync); app MarlinDesk marlin-desk marlin_desk marlinDesk MARLIN_DESK MarlinDeskService MarlinDesks; TernLedger tern_ledger tern-ledger",
    "ssh vps-ams-12; ssh -i ~/.ssh/id_ed25519 ubuntu@kingfisher; ssh -o StrictHostKeyChecking=no root@kingfisher; scp build.tar deploy@heron-box:/srv; rsync -avz ./dist/ deploy@heron-box:/srv/app; ping -c 3 kingfisher",
    "deploy to heron-box.fernhill.lan via 10.44.0.9:22 and tern.fernhill.pl",
    "members: Raj Patel (rpatel), Raj said ok; Patel approved; Li Wei / lwei; Zoë Müller zmuller MÜLLER; Siobhán O'Connor soconnor",
    "codename Bluewater BLUEWATER bluewater-sync; Nightjar NightjarSSO; Larkspur Holdings",
    "unread: Quillfeather Dental quillfeather.pl pg-west-3 orbital-backups #fernhill-ops Fernhill ledger.bluewater.pl ternledger.rs app.ternledger.zip",
    "hosts: " + " ".join(UNKNOWN_HOSTS + EXT_SHAPED),
    "bare: psql -h db-zeta-04 -U app; getaddrinfo ENOTFOUND stork-ci; dial tcp: lookup egret-gpu on 127.0.0.11:53: no such host",
    "paths: \\\\filesrv01\\share\\zeta",
    "BENIGN: " + " ".join(BENIGN),
])


def _seed_adversarial(engine, client):
    from apps.api.tests.improve_fixtures import seed_build, seed_workflow_run

    client.put("/v1/telemetry/sharing", json={"project_id": project(), "actor": "ceo",
                                              "mask_words": "Nightjar, Larkspur Holdings, Bluewater"})
    with engine.begin() as x:
        x.execute(text("INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, status, created_by) "
                       "VALUES ('dh_r4a', :p, 'kingfisher', '10.44.0.9', 'rpatel', 'ready', 'ceo'), "
                       "('dh_r4b', :p, 'heron-box', 'heron-box.fernhill.lan', 'deploy', 'ready', 'ceo')"),
                  {"p": project()})
        x.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
                       "target_ref, status, created_by, domains, public_url) VALUES ('dt_r4', :p, 'TernLedger', "
                       "'orbitalfin/tern-ledger', 'prod', 'ssh_host', 'tern.fernhill.pl', 'active', 'ceo', "
                       "CAST(:d AS JSONB), 'https://app.ternledger.zip')"),
                  {"p": project(), "d": json.dumps(["ledger.bluewater.pl", "ternledger.rs"])})
        for mid, name in (("rpatel", "Raj Patel"), ("lwei", "Li Wei"), ("zmuller", "Zoë Müller"),
                          ("soconnor", "Siobhán O'Connor")):
            x.execute(text("INSERT INTO project_members (id, project_id, member_id, display_name) "
                           "VALUES (:i, :p, :m, :n)"), {"i": "pm_r4" + mid, "p": project(), "m": mid, "n": name})
        for inp in ({"repository": "orbitalfin/kestrel-api", "title": "Fernhill onboarding",
                     "notify_channel": "#fernhill-ops", "directive": "migrate pg-west-3 to the Bluewater cluster"},
                    {"repo": "orbitalfin/kestrel-api", "branch": "feature/heron-sync", "app_name": "MarlinDesk",
                     "host": "vps-ams-12", "customer": "Quillfeather Dental", "server": "pg-west-3",
                     "domain": "quillfeather.pl", "bucket": "orbital-backups"}):
            wf, _ = seed_workflow_run(x, status="failed")
            x.execute(text("UPDATE workflow_runs SET input = CAST(:i AS JSONB) WHERE id = :r"),
                      {"r": wf, "i": json.dumps(inp)})
        rid = seed_build(x, status="failed", title="Adversarial",
                         spec={"role_key": "software_engineer", "repo": "orbitalfin/kestrel-api", "base_branch": "main"},
                         finalize={"app_name": "MarlinDesk", "branch": "feature/heron-sync"})
        x.execute(text("UPDATE code_task_runs SET error = :e WHERE id = :i"), {"e": ADVERSARIAL, "i": rid})
    return rid


def _cleanup_adversarial(engine):
    with engine.begin() as x:
        x.execute(text("DELETE FROM deployment_targets WHERE id = 'dt_r4'"))
        x.execute(text("DELETE FROM deployment_hosts WHERE id IN ('dh_r4a', 'dh_r4b')"))
        x.execute(text("DELETE FROM project_members WHERE id LIKE 'pm_r4%'"))


def _present(bundle: str, word: str) -> bool:
    import re

    return re.search(rf"(?<![A-Za-z0-9]){re.escape(word.lower())}", bundle.lower()) is not None


def test_the_adversarial_set_measured(engine, client):
    from boss_engine.services import improve_boss

    reset_state(engine)
    rid = _seed_adversarial(engine, client)
    try:
        with engine.begin() as c:
            bundle = improve_boss.share_bundle(c, project(), "build", rid)["bundle"]
    finally:
        _cleanup_adversarial(engine)
    body, benign = bundle.split("BENIGN:", 1)
    leaks = [w for w in KNOWN if _present(body, w)]
    hosts_left = [h for h in UNKNOWN_HOSTS if _present(body, h)]
    ext_left = [h for h in EXT_SHAPED if _present(body, h)]
    bare_left = [h for h in BARE if h.lower() in body.lower()]
    lost = [w for w in BENIGN if w.lower() not in benign.lower()]
    print(f"known leaks {len(leaks)}/{len(KNOWN)} {leaks}; unknown hosts left {len(hosts_left)}/"
          f"{len(UNKNOWN_HOSTS)} {hosts_left}; ext-shaped left {len(ext_left)}/{len(EXT_SHAPED)}; "
          f"bare left {bare_left}; benign masked {len(lost)}/{len(BENIGN)} {lost}")
    assert leaks == []
    assert hosts_left == []
    assert bare_left == []
    assert lost == []
    # The over-masks the tester found: an ssh option and a file copied by scp stay as written.
    assert "ssh -o StrictHostKeyChecking=no root@[HOST]" in body
    assert "scp build.tar deploy@[HOST]:/srv" in body
    assert "ping -c 3 [HOST]" in body


def test_host_tools_name_their_machine_and_nothing_else():
    from boss_engine.services.share_scrub import scrub

    cases = {
        "ssh -p 2222 -i ~/.ssh/k admin@build07": "build07",
        "ping -c 3 -W 1 db-zeta-04": "db-zeta-04",
        "psql -h pg-main -U app -d x": "pg-main",
        "mysql -hdb-two -u root": "db-two",
        "pg_dump --host=pgx.lan2 db": "pgx.lan2",
        "Error: getaddrinfo ENOTFOUND stork-ci": "stork-ci",
        "dial tcp: lookup egret-gpu on 127.0.0.11:53": "egret-gpu",
        "rsync -az ./a/ backup@vault9:/data": "vault9",
        "scp notes.txt nas01:/share": "nas01",
        "telnet mailhub 25": "mailhub",
        "copy from \\\\filesrv01\\share\\zeta done": "filesrv01",
    }
    for line, host in cases.items():
        out, _ = scrub(line, [])
        assert host not in out, (line, out)
    for line in ("ssh -o StrictHostKeyChecking=no", "scp file.tar", "bash get.acme.sh",
                 "python setup.py install", "see README.md and deploy.sh"):
        out, _ = scrub(line, [])
        assert out == line, (line, out)


def test_the_full_public_suffix_list_is_bundled_with_its_licence():
    from boss_engine.services.share_scrub import _PSL_FILE, public_suffix

    head = _PSL_FILE.read_text(encoding="utf-8")[:400]
    assert "Mozilla Public" in head and "mozilla.org/MPL/2.0" in head
    assert "public_suffix_list.dat" in (ROOT / "THIRD-PARTY-NOTICES.md").read_text()
    assert public_suffix(["x", "co", "uk"]) == 2
    assert public_suffix(["x", "zephyrco", "ts", "net"]) == 2
    assert public_suffix(["a", "svc", "cluster", "local"]) == 2
    assert public_suffix(["self", "items"]) == 0
