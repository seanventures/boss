"""Go as an opt-in toolchain (BOSSAPP-1210), and a build that stops instead of switching language
(BOSSAPP-1209). docs/community/TOOLCHAINS.md.

The Northgate brief asked for a Go API gateway on an install with no Go; the panel "converged" on
Node and BOSS built Node (2026-11-15). Go is now a toolchain an install turns on, mirroring Java:
a pinned, checksum-verified release in its own Dockerfile stage, a gate of `go vet` and
`go test`, module downloads only through boss-egress, per-project caches. And a build that needs
a language this install does not have stops and says how to turn it on — never proceeds in
another language, and never gates a go.mod tree with the npm or Python default.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NORTHGATE = Path(__file__).resolve().parent / "fixtures" / "northgate_go_brief.txt"

from boss_engine.services import stacks, toolchains, verification_target  # noqa: E402


def _manifest(tmp_path, monkeypatch, data: dict) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(data))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    monkeypatch.setenv(toolchains.CACHE_ROOT_ENV, str(tmp_path / "cache"))
    return manifest


@pytest.fixture
def go_on(tmp_path, monkeypatch):
    return _manifest(tmp_path, monkeypatch, {"go": {"go": "1.27.4"}})


@pytest.fixture
def go_off(tmp_path, monkeypatch):
    return _manifest(tmp_path, monkeypatch, {})


def _repo(tmp_path: Path, *files: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    for name in files:
        (repo / name).write_text("module example.com/x\n\ngo 1.27\n" if name == "go.mod" else "x\n")
    return repo


# ---- the image: a pinned, checksum-verified Go in its own stage ----------------------------------

def _dockerfile() -> str:
    return (ROOT / "Dockerfile.community").read_text()


def _stage(name: str) -> str:
    return _dockerfile().split(f"AS {name}\n", 1)[1].split("\nFROM ", 1)[0]


def test_the_go_stage_pins_a_release_and_checks_it_per_architecture():
    go = _stage("toolchains-go")
    version = re.search(r"^ARG GO_VERSION=(\d+\.\d+\.\d+)$", go, re.M)
    assert version and version.group(1).startswith("1.2")
    for arch in ("AMD64", "ARM64"):
        assert re.search(rf"^ARG GO_SHA256_{arch}=[0-9a-f]{{64}}$", go, re.M), arch
    assert 'echo "$go_sum  go.tgz" | sha256sum -c -' in go
    # Checked before it is unpacked, and never piped into a shell.
    assert go.index("sha256sum -c") < go.index("tar -xzf go.tgz")
    assert not re.search(r"\|\s*(ba)?sh\b", go)
    assert "https://dl.google.com/go/go$GO_VERSION.linux-${TARGETARCH:-amd64}.tar.gz" in go
    assert "/opt/toolchains/go" in go
    assert "ln -s ../go/bin/go /opt/toolchains/bin/go" in go
    assert "ln -s ../go/bin/gofmt /opt/toolchains/bin/gofmt" in go
    assert '"go$GO_VERSION "' in go                       # the binary is the pinned one
    # The release's own go.env (GOPROXY=…,direct, GOTOOLCHAIN=auto) is rewritten: `go` is safe
    # without the per-run environment.
    goenv = re.search(r"printf '([^']*)' \\\n\s*> /opt/toolchains/go/go.env", go)
    assert goenv, "the stage rewrites $GOROOT/go.env"
    assert goenv.group(1).split("\\n") == [
        "GOPROXY=https://proxy.golang.org", "GOSUMDB=sum.golang.org", "GOTOOLCHAIN=local", ""]
    assert """printf '{"go": {"go": "%s"}}\\n' "$GO_VERSION" > /opt/toolchains/manifest.json""" in go


def test_toolchains_are_selected_per_name_and_their_manifests_merged():
    dockerfile = _dockerfile()
    assert "ARG BOSS_TOOLCHAINS=java,go" in dockerfile
    assert 'ARG _TC_LIST=",${BOSS_TOOLCHAINS},"' in dockerfile
    for name in ("java", "go", "security"):
        upper = name.upper()
        assert f"ARG _TC_{upper}_HIT=${{_TC_LIST/*,{name},*/{name}}}" in dockerfile
        assert f"ARG TC_{upper}=${{_TC_{upper}_HIT/,*/none}}" in dockerfile
        assert f"FROM toolchains-${{TC_{upper}}} AS tc-{name}" in dockerfile
        assert f"COPY --from=tc-{name} /opt/toolchains /opt/toolchains" in dockerfile
        assert f"COPY --from=tc-{name} /opt/toolchains/manifest.json /tmp/tc-manifests/{name}.json" \
            in dockerfile
    merge = _stage("toolchains")
    assert 'known = {"none", "java", "go", "security"}' in merge
    assert "manifest.json" in merge and "merged.update" in merge
    # The frontend that understands ${var/pattern/replacement} is pinned.
    assert dockerfile.startswith("# syntax=docker/dockerfile:1.7\n")


def _merge_script() -> str:
    return _stage("toolchains").split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]


@pytest.mark.parametrize("value,ok", [("none", True), ("go", True), ("java,go", True),
                                      ("go,java,security", True), ("jav", False),
                                      ("java, go", False), ("none,go", False), ("golang", False)])
def test_the_image_build_refuses_an_unknown_selection_in_words(tmp_path, value, ok):
    """The toolchains stage's own script, run against stub manifests: a list it cannot honour
    fails the build with the accepted names, never a silent default."""
    manifests = tmp_path / "tc-manifests"
    manifests.mkdir()
    selected = [n for n in value.split(",") if n in ("java", "go", "security")]
    for name in ("java", "go", "security"):
        content = {name: {"v": "1"}} if name in selected else {}
        if name == "security" and name in selected:
            content["java"] = {"v": "1"}
        (manifests / f"{name}.json").write_text(json.dumps(content))
    out = tmp_path / "manifest.json"
    script = _merge_script().replace("/tmp/tc-manifests", str(manifests)) \
        .replace("/opt/toolchains/manifest.json", str(out))
    result = subprocess.run([sys.executable, "-c", script, value], capture_output=True, text=True)
    assert (result.returncode == 0) == ok, result.stderr
    if ok:
        assert set(json.loads(out.read_text())) >= set(selected)
    else:
        assert "java, go, security" in result.stderr


def test_compose_passes_the_list_through_and_restore_keeps_it():
    compose = (ROOT / "scripts" / "install" / "community-compose.yml").read_text()
    assert "BOSS_TOOLCHAINS: ${BOSS_TOOLCHAINS:-java,go}" in compose
    assert "${BOSS_TOOLCHAINS:-none}" not in compose
    restore = (ROOT / "scripts" / "install" / "restore_preserved_env.sh").read_text()
    assert "\nBOSS_TOOLCHAINS\n" in restore


# ---- boss.sh: go is known, and the setting is a list -------------------------------------------

def _boss_sh_functions() -> str:
    text = (ROOT / "scripts" / "install" / "boss.sh").read_text()
    start = text.index("KNOWN_TOOLCHAINS=(")
    return text[start:text.index("cmd_toolchains() {", start)]


def _bash(snippet: str) -> str:
    return subprocess.run(["bash", "-c", _boss_sh_functions() + "\n" + snippet],
                          capture_output=True, text=True, check=True).stdout.split()


def test_boss_sh_knows_go_and_lists_it():
    text = (ROOT / "scripts" / "install" / "boss.sh").read_text()
    assert "KNOWN_TOOLCHAINS=(java go security)" in text
    assert "toolchains enable java|go|security | disable java|go|security" in text
    assert "printf 'Known toolchains:   %s\\n' \"${KNOWN_TOOLCHAINS[*]}\"" in text
    assert 'wanted="$(toolchains_with "$current" "$name")"' in text
    assert 'wanted="$(toolchains_without "$current" "$name")"' in text


@pytest.mark.parametrize("call,expected", [
    ("toolchains_with none go", "go"),
    ("toolchains_with java go", "java,go"),
    ("toolchains_with java,go go", "java,go"),           # enabling twice changes nothing
    ("toolchains_with go java", "java,go"),              # one order, whatever was typed
    ("toolchains_without java,go java", "go"),           # disabling one keeps the other
    ("toolchains_without go go", "none"),
    ("toolchains_without none go", "none"),
    ("toolchains_normalise 'Go, JAVA'", "java,go"),
    ("toolchains_normalise golang", "none"),
])
def test_enable_and_disable_add_to_and_remove_from_the_list(call, expected):
    assert _bash(call) == [expected]



# ---- the default is java,go; an explicit value always wins -------------------------------------

def _boss_sh_toolchains_command() -> str:
    """cmd_toolchains as boss.sh has it, with its helpers, and the real env_value/set_env_value.
    Only the image build and the restart are stubbed (no Docker)."""
    text = (ROOT / "scripts" / "install" / "boss.sh").read_text()
    start = text.index("KNOWN_TOOLCHAINS=(")
    end = text.index("\n}\n", text.index("cmd_toolchains() {", start)) + 3
    env_value = text[text.index("env_value() {"):].split("\n", 1)[0]
    setter = text[text.index("set_env_value() {"):]
    setter = setter[:setter.index("\n}\n") + 3]
    stubs = ("need_install() { :; }\ndie() { echo \"$*\" >&2; exit 1; }\nhint_command() { echo boss; }\n"
             "compose() { [[ \"$1\" == build ]] && echo \"BUILT $*\"; return 0; }\n"
             "apply_env() { echo \"APPLIED $*\"; }\n")
    return stubs + env_value + "\n" + setter + text[start:end]


def _toolchains(env_file: Path, *args: str) -> subprocess.CompletedProcess:
    script = _boss_sh_toolchains_command() + '\ncmd_toolchains "$@"\n'
    return subprocess.run(["bash", "-c", script, "boss", *args], capture_output=True, text=True,
                          env={**os.environ, "ENV_FILE": str(env_file)})


def _setting(env_file: Path) -> list[str]:
    return [line for line in env_file.read_text().splitlines() if line.startswith("BOSS_TOOLCHAINS=")]


def test_java_and_go_are_the_default_everywhere_a_default_lives():
    text = (ROOT / "scripts" / "install" / "boss.sh").read_text()
    assert 'DEFAULT_TOOLCHAINS="java,go"' in text
    assert '"${current:-$DEFAULT_TOOLCHAINS}"' in text and '"${current:-none}"' not in text
    assert "ARG BOSS_TOOLCHAINS=java,go\n" in _dockerfile()


def test_no_line_in_env_means_java_and_go(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BOSS_EDITION=community\n")
    out = _toolchains(env, "list")
    assert out.returncode == 0, out.stderr
    assert "BOSS_TOOLCHAINS=java,go (the default" in out.stdout
    assert _setting(env) == []           # listing writes nothing: the install keeps the default


def test_disable_go_from_the_default_persists_java_only(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BOSS_EDITION=community\n")
    out = _toolchains(env, "disable", "go")
    assert out.returncode == 0, out.stderr
    assert _setting(env) == ["BOSS_TOOLCHAINS=java"]
    assert "BUILT build api worker" in out.stdout
    out = _toolchains(env, "disable", "java")
    assert out.returncode == 0, out.stderr
    assert _setting(env) == ["BOSS_TOOLCHAINS=none"]
    # turned off stays off: listing reads the explicit none, not the default
    out = _toolchains(env, "list")
    assert "Configured in .env: BOSS_TOOLCHAINS=none" in out.stdout


def test_an_explicit_none_is_kept_and_enable_starts_from_it(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BOSS_TOOLCHAINS=none\n")
    out = _toolchains(env, "disable", "go")
    assert out.returncode == 0 and "already none" in out.stdout
    assert _setting(env) == ["BOSS_TOOLCHAINS=none"]
    out = _toolchains(env, "enable", "go")
    assert out.returncode == 0, out.stderr
    assert _setting(env) == ["BOSS_TOOLCHAINS=go"]


def test_enabling_security_on_the_default_keeps_java_and_go(tmp_path):
    env = tmp_path / ".env"
    env.write_text("")
    out = _toolchains(env, "enable", "security")
    assert out.returncode == 0, out.stderr
    assert _setting(env) == ["BOSS_TOOLCHAINS=java,go,security"]

def test_disabling_java_under_security_says_security_includes_java():
    text = (ROOT / "scripts" / "install" / "boss.sh").read_text()
    body = text[text.index("cmd_toolchains() {"):]
    guard = body[body.index('if [[ "$action" == disable && "$name" == java'):]
    guard = guard[:guard.index("  fi\n") + 5]
    assert "the security scanners include Java" in guard
    assert "boss toolchains disable security" in guard and "return 1" in guard
    out = subprocess.run(["bash", "-c", "f() { local action=disable name=java current=java,security\n"
                          + guard + "\nreturn 0; }; f"], capture_output=True, text=True)
    assert out.returncode == 1
    assert "boss toolchains disable security" in out.stdout
    assert "boss toolchains disable java" in out.stdout


def test_the_python_reading_of_the_setting_matches(go_on):
    assert toolchains.parse_selection("java,go") == ["java", "go"]
    assert toolchains.parse_selection("none") == [] == toolchains.parse_selection("")
    assert toolchains.parse_selection(" Go , go ") == ["go"]


# ---- the manifest, Setup and the missing-toolchain words ----------------------------------------

def test_setup_lists_go_with_its_version(go_on):
    rows = {r["name"]: r for r in toolchains.setup_summary()}
    assert rows["go"]["installed"] and rows["go"]["versions"] == {"go": "1.27.4"}
    assert rows["go"]["enable"] is None and not rows["java"]["installed"]


def test_setup_says_how_to_turn_go_on(go_off):
    go = {r["name"]: r for r in toolchains.setup_summary()}["go"]
    assert not go["installed"] and go["enable"] == "boss toolchains enable go"


def test_missing_go_is_said_in_words(go_off):
    message = toolchains.missing_message("go", "go.mod")
    assert message.startswith("This project needs Go (it has a go.mod)")
    assert "`boss toolchains enable go`" in message and "`boss update`" in message
    assert "BOSS_TOOLCHAINS=go" in message and "java,go" in message
    assert "another language" in message and "not found" not in message


# ---- the gate plan ---------------------------------------------------------------------------------

def test_a_go_module_gates_on_go_vet_and_go_test(tmp_path, go_on):
    repo = _repo(tmp_path, "go.mod", "main.go")
    plan = verification_target.resolve_gate_plan({}, "add a route", repo)
    assert plan.source == "toolchain" and plan.runs_steps and not plan.error
    assert plan.commands == ["go vet ./...", "go test ./..."]
    assert plan.description == "go vet ./... && go test ./..."
    assert plan.steps == [("go vet ./...", repo), ("go test ./...", repo)]
    assert plan.timeout_seconds == toolchains.GATE_TIMEOUT_SECONDS


def test_go_gate_commands_are_sandbox_runnable():
    from boss_engine.config import Settings
    from boss_engine.services.sandbox import assert_allowed_executable, looks_like_shell

    for command in toolchains.GO_GATE:
        assert not looks_like_shell(command)
        assert_allowed_executable(command.split()[0], Settings())


def test_a_go_module_without_go_is_toolchain_missing_never_npm(tmp_path, go_off):
    repo = _repo(tmp_path, "go.mod", "main.go")
    plan = verification_target.resolve_gate_plan({}, "", repo)
    assert plan.source == "toolchain_missing" and plan.runs_steps and plan.commands == []
    assert plan.error == plan.description
    assert plan.error.startswith("This project needs Go (it has a go.mod)")
    assert "npm" not in " ".join(plan.commands) and "unittest" not in plan.description


@pytest.mark.parametrize("manifest,label", [("Cargo.toml", "Rust"), ("Gemfile", "Ruby"),
                                            ("composer.json", "PHP"), ("mix.exs", "Elixir"),
                                            ("build.sbt", "Scala")])
def test_a_tree_of_a_language_boss_cannot_build_is_not_gated_as_node(tmp_path, go_on, manifest,
                                                                      label):
    repo = _repo(tmp_path, manifest)
    plan = verification_target.resolve_gate_plan({}, "", repo)
    assert plan.source == "toolchain_missing" and plan.commands == []
    assert plan.error.startswith(f"This project is {label} (it has a {manifest}), which BOSS "
                                 "cannot build")


def test_toolchain_missing_is_a_persons_to_fix(tmp_path, go_off):
    plan = verification_target.resolve_gate_plan({}, "", _repo(tmp_path, "go.mod"))
    assert plan.needs_human
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not verification_target.resolve_gate_plan({}, "", empty).needs_human


@pytest.mark.parametrize("extra", ["package.json", "requirements.txt"])
def test_a_repair_cannot_flip_a_go_tree_to_npm_or_python_while_go_is_off(tmp_path, go_off, extra):
    """A repair that adds a package.json (or a Python manifest) beside go.mod does not turn the
    gate into npm's: the tree still needs Go, and the gate still says so."""
    plan = verification_target.resolve_gate_plan({}, "", _repo(tmp_path, "go.mod", "main.go", extra))
    assert plan.source == "toolchain_missing" and plan.needs_human
    assert plan.error.startswith("This project needs Go")


def test_a_repair_cannot_flip_a_java_tree_to_npm_while_java_is_off(tmp_path, go_off):
    plan = verification_target.resolve_gate_plan({}, "", _repo(tmp_path, "pom.xml", "package.json"))
    assert plan.source == "toolchain_missing" and plan.needs_human
    assert plan.error.startswith("This project needs Java")


def test_a_stray_go_mod_does_not_make_a_maven_project_go(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, {"java": {"jdk": "21"}, "go": {"go": "1.27.4"}})
    repo = _repo(tmp_path, "pom.xml", "go.mod")
    src = repo / "src" / "main" / "java"
    src.mkdir(parents=True)
    for i in range(3):
        (src / f"A{i}.java").write_text("class A {}\n")
    (repo / "tools.go").write_text("package tools\n")
    assert stacks.compiled_stack(repo) == "java"
    assert stacks._service_in(repo, ".").language == "java"
    assert verification_target.resolve_gate_plan({}, "", repo).commands == ["mvn -B -ntp verify"]
    # Mostly Go: the go.mod is the build.
    for i in range(5):
        (repo / f"g{i}.go").write_text("package main\n")
    assert stacks.compiled_stack(repo) == "go"
    assert stacks._service_in(repo, ".").language == "go"


def test_node_and_python_beside_a_go_mod_keep_their_plans(tmp_path, go_on):
    node = _repo(tmp_path, "package.json", "go.mod")
    assert verification_target.resolve_gate_plan({}, "", node).source == "default"
    py = tmp_path / "py"
    py.mkdir()
    (py / "go.mod").write_text("module x\n")
    (py / "requirements.txt").write_text("flask\n")
    plan = verification_target.resolve_gate_plan({}, "", py)
    assert plan.source == "default" and plan.commands == [verification_target.PYTHON_TEST_CMD]


def test_explicit_test_commands_still_win_over_go(tmp_path, go_on):
    repo = _repo(tmp_path, "go.mod")
    plan = verification_target.resolve_gate_plan({"test_commands": ["go test ./pkg/..."]}, "", repo)
    assert plan.source == "task" and plan.commands == ["go test ./pkg/..."]


# ---- the registry: Go is a language BOSS builds --------------------------------------------------

def test_go_is_in_the_registry_and_not_unsupported(go_on):
    assert "go" in stacks.LANGUAGES and stacks.LANGUAGES["go"].toolchain == "go"
    assert all(u.key != "go" for u in stacks.UNSUPPORTED)
    assert stacks.language_ready("go")
    assert "go.mod" not in stacks._UNSUPPORTED_MANIFESTS


def test_a_go_service_is_detected_planned_and_started(tmp_path, go_on):
    app = tmp_path / "app"
    (app / "api" / "cmd" / "server").mkdir(parents=True)
    (app / "api" / "go.mod").write_text("module x\n\nrequire github.com/go-chi/chi/v5 v5.0.12\n")
    (app / "api" / "cmd" / "server" / "main.go").write_text("package main\n\nfunc main() {}\n")
    (app / "web").mkdir()
    (app / "web" / "package.json").write_text(json.dumps({"dependencies": {"react": "18"}}))
    services = {s.rel: s for s in stacks.detect_services(app)}
    assert services["api"].language == "go" and services["api"].framework == "chi"
    assert services["api"].role == "backend" and services["api"].supported
    plan = stacks.stack_plan(app)
    assert not plan.error
    assert [(c, d.name) for c, d in plan.steps if d.name == "api"] == [
        ("go vet ./...", "api"), ("go test ./...", "api")]
    script = plan.backend_command[2]
    assert plan.backend_command[:2] == ["sh", "-lc"]
    assert 'go build -o "$out" ./cmd/server' in script and 'exec "$out"' in script
    assert "TMPDIR" in script                             # never into the checkout
    assert stacks.adapter_for(stacks.detect_services(app / "api")).id == "go-service"


def test_a_root_main_package_runs_from_the_module_root(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "main.go").write_text("// Package main.\npackage main\n")
    (tmp_path / "main_test.go").write_text("package main\n")
    assert toolchains.go_main_package(tmp_path) == "."


# ---- the sandbox: proxy, checksum database and per-project caches ---------------------------------

def test_egress_allows_the_go_module_proxy_and_checksum_database_only(monkeypatch, go_on):
    sys.path.insert(0, str(ROOT))
    from apps.egress.boss_egress import connect

    monkeypatch.delenv("BOSS_EGRESS_CONNECT_ALLOW", raising=False)
    for host in ("proxy.golang.org", "sum.golang.org"):
        assert host in connect.TOOLCHAIN_ALLOW["go"] and connect.host_allowed(host)
    for host in ("storage.googleapis.com", "golang.org", "go.dev", "dl.google.com",
                 "gopkg.in", "go.googlesource.com"):
        assert not connect.host_allowed(host), host


def test_the_go_hosts_are_closed_on_an_install_without_go(monkeypatch, go_off):
    """The module proxy fetches any public path on request: it is a way out as well as in, so
    an install without Go does not open it."""
    sys.path.insert(0, str(ROOT))
    from apps.egress.boss_egress import connect

    monkeypatch.delenv("BOSS_EGRESS_CONNECT_ALLOW", raising=False)
    for host in ("proxy.golang.org", "sum.golang.org"):
        assert host not in connect.DEFAULT_ALLOW and not connect.host_allowed(host)
    assert "exfil" in (ROOT / "apps" / "egress" / "boss_egress" / "connect.py").read_text() \
        or "a way\n#: OUT" in (ROOT / "apps" / "egress" / "boss_egress" / "connect.py").read_text()


def test_a_run_reaches_modules_only_through_the_proxy_with_caches_under_home(tmp_path, go_on):
    env = toolchains.sandbox_env(tmp_path / "home")
    assert env["GOPROXY"] == "https://proxy.golang.org"          # no `,direct`
    assert env["GOSUMDB"] == "sum.golang.org"
    assert env["GOTOOLCHAIN"] == "local" and env["GOFLAGS"] == "-modcacherw"
    for name in ("GOPRIVATE", "GONOPROXY", "GONOSUMDB", "GONOSUMCHECK", "GOINSECURE"):
        assert env[name] == "", name                          # nothing skips proxy or sumdb
    home = str(tmp_path / "home")
    assert env["GOMODCACHE"] == f"{home}/go/pkg/mod" and env["GOCACHE"] == f"{home}/.cache/go-build"
    cached = {name: rel for name, rel, tool in toolchains.CACHE_DIRS if tool == "go"}
    assert env["GOMODCACHE"].endswith(cached["gomod"]) and env["GOCACHE"].endswith(cached["gobuild"])


def test_no_go_environment_without_go(tmp_path, go_off):
    assert toolchains.sandbox_env(tmp_path) == {}


def test_the_sandbox_identity_hands_the_go_environment_to_every_process():
    text = (ROOT / "engine" / "boss_engine" / "services" / "sandbox_identity.py").read_text()
    block = text[text.index("out.update(toolchains.sandbox_env(home))") - 200:][:500]
    assert "except Exception as exc" in block and "log.warning(" in block   # never silent


def test_go_caches_persist_per_project_and_never_cross(tmp_path, go_on):
    run1 = tmp_path / "run1"
    mod = run1 / "go" / "pkg" / "mod" / "github.com" / "go-chi" / "chi@v5" / "mux.go"
    mod.parent.mkdir(parents=True)
    mod.write_bytes(b"package chi")
    build = run1 / ".cache" / "go-build" / "ab" / "abcd-d"
    build.parent.mkdir(parents=True)
    build.write_bytes(b"obj")
    (run1 / ".m2" / "repository").mkdir(parents=True)        # Java is off: not kept
    assert sorted(toolchains.save_cache(run1, "proj-a")) == ["gobuild", "gomod"]
    run2 = tmp_path / "run2"
    run2.mkdir()
    assert sorted(toolchains.restore_cache(run2, "proj-a")) == ["gobuild", "gomod"]
    copied = run2 / "go" / "pkg" / "mod" / "github.com" / "go-chi" / "chi@v5" / "mux.go"
    assert copied.read_bytes() == b"package chi"
    assert copied.stat().st_mode & 0o777 == 0o666
    other = tmp_path / "run3"
    other.mkdir()
    assert toolchains.restore_cache(other, "proj-b") == []


# ---- RCA-C: the build stops instead of switching language ---------------------------------------

@pytest.mark.parametrize("text", [
    "reverse-proxy API gateway (Go) with host/path/method routing",
    "### 1. `northgate-gateway` (Go, medium complexity)",
    "- Clean, idiomatic Go; tests pass",
    "`go vet` must be clean",
    "and `go test ./...` clean",
    "A golang microservice", "Build it in Go", "a Go service", "written in Go.", "a go.mod must be at the root",
])
def test_go_is_read_from_the_phrasings_a_brief_uses(text):
    assert stacks.languages_named(text) == ["go"], text


@pytest.mark.parametrize("text", [
    "Users log in, go to the dashboard and go back", "We go live on Monday",
    "the go-to-market plan", "Let's go, team", "go ahead and merge", "not in Go — use FastAPI",
    "has no node, npm, pip3, java, go, ruby, php, make",
])
def test_ordinary_go_words_are_not_the_language(text):
    assert "go" not in stacks.languages_named(text), text


@pytest.mark.parametrize("brief,source,target", [
    ("Rewrite our Go codebase in Node", "go", "node"),
    ("Port our Go service to Python (FastAPI)", "go", "python"),
    ("Migrate the API from Go to Python", "go", "python"),
    ("Port our Rails app to Python", "ruby", "python"),
])
def test_the_language_code_moves_away_from_is_not_requested(brief, source, target):
    named = stacks.languages_named(brief) + stacks.unsupported_named(brief)
    assert source not in named and target in named, named


@pytest.mark.parametrize("brief", ["Rewrite it in Go", "Port the Node service to Go",
                                   "Migrate the billing service to Go"])
def test_the_language_code_moves_to_is_requested(brief):
    assert "go" in stacks.languages_named(brief), brief


#: Re-review of #135: a later language in the sentence is not a migration target, and "to"
#: alone is no migration. Each of these asks for Go; with Go off, each must stop the build.
GO_IS_WHAT_IS_BUILT = [
    "Build the backend in Go to serve the React app",
    "Build a Go service to replace the legacy Java one",
    "Build a Go API to replace our Python one",
    "A Go proxy to the Express backend",
    "Build a Go worker that moves jobs to the Node queue",
    "Move from Python to Go for the gateway",
    "Migrate from Java to Go",
]


@pytest.mark.parametrize("brief", GO_IS_WHAT_IS_BUILT)
def test_go_is_detected_whatever_else_the_sentence_names(brief, go_off):
    assert "go" in stacks.languages_named(brief), stacks.languages_named(brief)
    verdict = stacks.check_build(brief + "\n\n---\n\nspec")
    assert not verdict.ok and "go" in verdict.off, brief


@pytest.mark.parametrize("brief,expected", [
    ("Rewrite our Go codebase in Node", ["node"]),
    ("Port our Go service to Python (FastAPI)", ["python"]),
    ("We use Go internally; build the dashboard in React", ["node"]),
    ("Build a Go service to replace the legacy Java one", ["go"]),
    ("Build a Go API to replace our Python one", ["go"]),
    ("Move from Python to Go for the gateway", ["go"]),
    ("Migrate from Java to Go", ["go"]),
])
def test_exactly_the_built_language_is_named(brief, expected):
    assert stacks.languages_named(brief) == expected, brief


@pytest.mark.parametrize("text", ["go vet ./... must pass", "`go vet ./...` must pass",
                                  "go test ./... and go vet ./... must be clean"])
def test_a_path_or_code_span_does_not_end_the_sentence(text):
    assert "go" in stacks.languages_named(text), text


def test_a_bare_go_in_a_stack_array_is_go():
    assert "go" in stacks.languages_named('{"stack": ["go", "react", "postgres"]}')
    assert "go" in stacks.languages_named('languages: ["golang"]')
    assert "go" not in stacks.languages_named('allowed: ["go", "make", "npm"]')


@pytest.mark.parametrize("brief", [
    "Rewrite our Go codebase in Node",
    "Build a Python service. Our CLI runs `go test ./...` separately.",
    "Port our Go service to Python (FastAPI)",
])
def test_go_as_a_source_or_incidental_tooling_does_not_stop_a_build(brief, go_off):
    assert stacks.check_build(brief + "\n\n---\n\nspec").ok, brief
    declared = (brief + "\n\n---\n\nTHE APPROVED ARCHITECTURE BELOW IS BINDING.\n"
                "## Components\nStack: backend=python/fastapi\n")
    assert stacks.check_build(declared).ok, brief


@pytest.mark.parametrize("text,asks", [
    ("Our CLI runs `go test ./...` separately.", False),
    ("We keep a go.mod for the tools.", False),
    ("`go vet` and `go test ./...` must pass", True),
    ("- Clean; `go vet` and `go test ./...` clean", True),
])
def test_go_tooling_is_a_request_only_as_a_requirement(text, asks):
    assert ("go" in stacks.languages_named(text)) == asks, text


def test_a_stack_array_in_a_brief_is_read(go_on):
    assert stacks.unsupported_named('{"stack": ["ruby", "rails", "postgres"]}') == ["ruby"]
    assert stacks.unsupported_named('allowed: ["ruby", "rake", "bundle"]') == []


def test_the_northgate_brief_stops_while_go_is_off(go_off):
    brief = NORTHGATE.read_text()
    verdict = stacks.check_build(brief)
    assert not verdict.ok and verdict.off == ["go"] and verdict.unsupported == []
    assert "This build needs Go, which is not turned on" in verdict.message
    assert "`boss toolchains enable go`" in verdict.message and "`boss update`" in verdict.message
    assert "another language" in verdict.message
    assert verdict.as_dict()["code"] == "stack_unsupported"


def test_the_northgate_brief_builds_once_go_is_on(go_on):
    assert stacks.check_build(NORTHGATE.read_text()).ok


def test_the_northgate_spec_names_no_ruby_or_php(go_on):
    """BOSS's own deploy-machine sentence and the spec's allowed-command list name Ruby, PHP and
    .NET; neither asks for them."""
    brief = NORTHGATE.read_text()
    assert stacks.unsupported_named(brief) == []
    assert "go" in stacks.languages_named(brief)
    allowed = brief.split("Allowed commands (the spec's dump): ", 1)[1]
    assert stacks.unsupported_named(allowed) == [] and stacks.languages_named(allowed) == []
    # A real request is still read.
    assert stacks.unsupported_named("Rewrite it in Ruby on Rails.\n" + brief) == ["ruby"]


def test_an_architecture_that_swaps_the_asked_language_does_not_proceed(go_off):
    """The directive asks for Go; an approved architecture that declares Node is the swap
    Northgate shipped. The build stops on the language the operator asked for."""
    brief = ("Build the gateway in Go.\n\n---\n\nTHE APPROVED ARCHITECTURE BELOW IS BINDING.\n"
             "## Components\nStack: backend=node/fastify\n")
    verdict = stacks.check_build(brief)
    assert not verdict.ok and verdict.off == ["go"]
    assert "boss toolchains enable go" in verdict.message


def test_an_unsupported_language_stops_the_build_as_stack_unsupported(go_on):
    verdict = stacks.check_build("Build the gateway in Rust.\n\n---\n\nspec")
    assert not verdict.ok and verdict.unsupported == ["rust"]
    assert "Rust, which BOSS cannot build" in verdict.message
    assert verdict.as_dict()["code"] == "stack_unsupported"


# ---- docs ------------------------------------------------------------------------------------------

def test_the_docs_describe_go():
    doc = (ROOT / "docs" / "community" / "TOOLCHAINS.md").read_text()
    for needle in ("`go`", "boss toolchains enable go", "BOSS_TOOLCHAINS=java,go", "go vet ./...",
                   "proxy.golang.org", "sum.golang.org", "GOTOOLCHAIN=local", "toolchains-go"):
        assert needle in doc, needle
    stacks_doc = (ROOT / "docs" / "community" / "SUPPORTED_STACKS.md").read_text()
    assert stacks_doc == stacks.render_supported_stacks_md()
    assert "`go-service`" in stacks_doc and "| `go` — Go | not supported" not in stacks_doc
