"""BOSSAPP-856: the app's runtime services run beside a build, supervised, and agents manage them.

A preview is one process with a TTL; an app with an API and a worker could not be run mid-build.
boss.verify.yaml may now declare `runtime:` (services with ports, dependencies and health, plus
provision/teardown). These tests start real processes: an API that answers /healthz and reports
the worker's port it was given, and a worker."""
from __future__ import annotations

import json
import sys
import textwrap
import time
import urllib.request

import pytest

from boss_engine.services import runtime_services as rs
from boss_engine.services.verify_manifest import VerifyManifestError, parse_verify_manifest

BASE = "version: 1\nsuites:\n  - {name: unit, paths: ['**'], run: ['true']}\n"

API = textwrap.dedent("""
    import http.server, os, json
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"path": self.path,
                               "worker": os.environ.get("BOSS_SERVICE_WORKER_PORT"),
                               "mode": os.environ.get("MODE")}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a):
            print("hit", self.path, flush=True)
    http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
""")
WORKER = "import time\nprint('worker up', flush=True)\nwhile True: time.sleep(1)\n"
CRASH = "import sys\nprint('boom', flush=True)\nsys.exit(3)\n"


def _manifest(runtime: str):
    return parse_verify_manifest(BASE + runtime)


def test_no_runtime_block_is_unchanged():
    m = parse_verify_manifest(BASE)
    assert m.runtime is None
    assert rs.for_run("crun_none") is None


@pytest.mark.parametrize("runtime,said", [
    ("runtime: {services: [{name: API, run: x}]}", "name must be"),
    ("runtime: {services: [{name: a, run: 'x && y'}]}", "shell syntax"),
    ("runtime: {services: [{name: a, run: x, depends_on: [b]}]}", "not declared"),
    ("runtime: {services: [{name: a, run: x, depends_on: [b]}, {name: b, run: y, depends_on: [a]}]}",
     "cycle"),
    ("runtime: {services: [{name: a, run: x, port: 80}]}", "port"),
    ("runtime: {services: [{name: a, run: x, health: {http: healthz}}]}", "starting with /"),
    ("runtime: {services: [{name: a, run: x, colour: red}]}", "unknown key"),
    ("runtime: {services: [], extra: 1}", "unknown key"),
])
def test_a_bad_declaration_names_the_field(runtime, said):
    with pytest.raises(VerifyManifestError, match=said):
        _manifest(runtime)


def test_the_declaration_parses_in_dependency_order():
    m = _manifest(textwrap.dedent("""\
        runtime:
          provision: ["echo hi"]
          services:
            - {name: api, run: "node a.js", depends_on: [worker], health: {http: /healthz},
               env: {MODE: test}, port: 18123}
            - {name: worker, run: "node w.js", start: on_demand}
        """))
    spec = m.runtime
    assert [s.name for s in spec.order()] == ["worker", "api"]
    api = spec.services[0]
    assert api.run == ("node", "a.js") and api.port == 18123 and api.env == (("MODE", "test"),)
    assert spec.services[1].on_demand and spec.provision == ("echo hi",)


def _spec(tmp_path, extra_api: str = ""):
    (tmp_path / "api.py").write_text(API)
    (tmp_path / "worker.py").write_text(WORKER)
    (tmp_path / "crash.py").write_text(CRASH)
    (tmp_path / "provision.py").write_text("open('provisioned', 'w').write('1')\n")
    (tmp_path / "teardown.py").write_text("open('torn', 'w').write('1')\n")
    py = sys.executable
    return _manifest(textwrap.dedent(f"""\
        runtime:
          provision: ["{py} provision.py"]
          teardown: ["{py} teardown.py"]
          services:
            - name: api
              run: "{py} api.py"
              depends_on: [worker]
              health: {{http: /healthz}}
              env: {{MODE: test}}
              ready_seconds: 20
            - name: worker
              run: "{py} worker.py"
            - name: crash
              run: "{py} crash.py"
              start: on_demand
              ready_seconds: 5
        """)).runtime


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_services_start_in_order_see_each_other_and_stop_with_the_build(tmp_path):
    events = []
    sup = rs.RuntimeSupervisor(run_id="crun_rt1", root=tmp_path, spec=_spec(tmp_path),
                               emit=events.append, log_dir=str(tmp_path / "logs"))
    (tmp_path / "logs").mkdir()
    try:
        sup.provision()
        assert (tmp_path / "provisioned").exists()
        sup.start_all()
        assert sup.alive("api") and sup.alive("worker") and not sup.alive("crash")
        said = _get(f"http://127.0.0.1:{sup.ports['api']}/ping")
        assert said == {"path": "/ping", "worker": str(sup.ports["worker"]), "mode": "test"}
        started = [e["service"] for e in events if e["op"] == "start" and e["ok"]]
        assert started == ["worker", "api"]
        assert "worker up" in sup.act("logs", "worker")["logs"]

        # stopping the worker stops what depends on it first; starting the api brings it back
        sup.act("stop", "worker")
        assert not sup.alive("api") and not sup.alive("worker")
        sup.act("start", "api")
        assert sup.alive("worker") and sup.alive("api")
        before = sup.running["api"].proc.pid
        sup.act("restart", "api")
        assert sup.running["api"].proc.pid != before

        with pytest.raises(rs.RuntimeServicesError, match="exited 3 before it was ready"):
            sup.act("start", "crash")
        with pytest.raises(rs.RuntimeServicesError, match="no runtime service"):
            sup.act("start", "nope")
        status = sup.act("status")
        assert {s["name"]: s["running"] for s in status["services"]} == {
            "api": True, "worker": True, "crash": False}
    finally:
        procs = [r.proc for r in sup.running.values()]
        sup.teardown()
    assert (tmp_path / "torn").exists()
    assert all(p.poll() is not None for p in procs)
    assert [e["op"] for e in events][-3:] == ["teardown", "stop", "stop"]


def test_the_run_registry_and_the_agent_tool(tmp_path):
    from boss_engine.services import mcp_gateway as gw

    spec = _spec(tmp_path)
    manifest = type("M", (), {"runtime": spec})()
    sup = rs.start_for_run(None, "crun_rt2", "p_x", manifest, tmp_path)
    try:
        assert rs.for_run("crun_rt2") is sup
        gateway = gw.McpGateway(db=lambda: None)
        grant = gw.GatewayGrant(token_hash="h", project_id="p_x", expires_at=time.time() + 60,
                                run_id="crun_rt2")
        other = gw.GatewayGrant(token_hash="h2", project_id="p_x", expires_at=time.time() + 60,
                                run_id="crun_other")
        assert gw.RUNTIME_TOOL in gateway._keyed_tools(grant)
        assert gateway._keyed_tools(other) == []
        out = gateway.call_run_tool(grant, "runtime_service", {"action": "status"})
        names = [s["name"] for s in json.loads(out["content"][0]["text"])["services"]]
        assert names == ["api", "worker", "crash"]
        refused = gateway.call_run_tool(other, "runtime_service", {"action": "status"})
        assert refused.get("isError")
        bad = gateway.call_run_tool(grant, "runtime_service", {"action": "explode"})
        assert bad.get("isError")
    finally:
        rs.teardown_run("crun_rt2")
    assert rs.for_run("crun_rt2") is None
    assert not sup.alive("api")


def test_a_microvm_build_is_told_why():
    spec = rs.RuntimeSpec(services=(rs.RuntimeService(name="a", run=("x",)),))
    with pytest.raises(rs.RuntimeServicesError, match="microVM"):
        rs.start_for_run(None, "crun_rt3", "p", type("M", (), {"runtime": spec})(), ".",
                         use_microvm=True)
