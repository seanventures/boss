"""Per-stack toolchains, starting with Java (docs/community/TOOLCHAINS.md).

The build loop recognises a Java build, runs Maven or Gradle, and says in words when the install
has no Java; a preview builds and runs the jar on $PORT; the sandbox reaches Maven Central and
the Gradle plugin portal only through boss-egress; dependency caches persist per project.
"""
from __future__ import annotations

import json
import re
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

from boss_engine.services import preview_service, toolchains, verification_target  # noqa: E402


@pytest.fixture
def java_on(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"java": {"jdk": "Temurin 21.0.12.1+1", "maven": "3.9.16",
                                             "gradle": "8.14.6"}}))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    monkeypatch.setenv(toolchains.CACHE_ROOT_ENV, str(tmp_path / "cache"))
    return manifest


@pytest.fixture
def java_off(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest-none.json"
    manifest.write_text("{}\n")
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    return manifest


def _repo(tmp_path: Path, *files: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    for name in files:
        (repo / name).write_text("x\n")
    return repo


# ---- stack detection -----------------------------------------------------------------------------

@pytest.mark.parametrize("files,expected", [
    (("pom.xml",), "maven"),
    (("build.gradle",), "gradle"),
    (("build.gradle.kts",), "gradle"),
    (("settings.gradle.kts",), "gradle"),
    (("pom.xml", "build.gradle"), "maven"),
    (("package.json",), None),
    (("pyproject.toml",), None),
])
def test_a_java_build_is_recognised_by_its_build_file(tmp_path, files, expected):
    assert toolchains.detect_java(_repo(tmp_path, *files)) == expected


def test_maven_project_gates_on_mvn_verify(tmp_path, java_on):
    repo = _repo(tmp_path, "pom.xml")
    plan = verification_target.resolve_gate_plan({}, "add a hello endpoint", repo)
    assert plan.source == "toolchain" and plan.runs_steps and not plan.error
    assert plan.commands == ["mvn -B -ntp verify"]
    assert plan.steps == [("mvn -B -ntp verify", repo)]
    assert plan.timeout_seconds == toolchains.GATE_TIMEOUT_SECONDS


def test_gradle_project_gates_on_the_images_gradle_not_the_wrapper(tmp_path, java_on):
    repo = _repo(tmp_path, "build.gradle.kts", "gradlew")
    plan = verification_target.resolve_gate_plan({}, "", repo)
    assert plan.commands == ["gradle --no-daemon --console=plain build"]


def test_gate_commands_are_sandbox_runnable(tmp_path, java_on):
    from boss_engine.config import Settings
    from boss_engine.services.sandbox import assert_allowed_executable, looks_like_shell

    for command in toolchains.MAVEN_GATE + toolchains.GRADLE_GATE:
        assert not looks_like_shell(command)
        assert_allowed_executable(command.split()[0], Settings())


def test_missing_java_is_said_in_words(tmp_path, java_off):
    repo = _repo(tmp_path, "pom.xml")
    plan = verification_target.resolve_gate_plan({}, "", repo)
    assert plan.source == "toolchain_missing" and plan.runs_steps
    assert plan.commands == []
    assert plan.error == plan.description
    assert plan.error.startswith("This project needs Java (it has a pom.xml)")
    assert "boss toolchains enable java" in plan.error
    assert "BOSS_TOOLCHAINS=java" in plan.error
    assert "not found" not in plan.error


def test_node_and_python_plans_are_unchanged(tmp_path, java_on):
    node = _repo(tmp_path, "package.json", "pom.xml")      # a Node app at the root wins
    assert verification_target.resolve_gate_plan({}, "", node).source == "default"
    py = tmp_path / "py"
    py.mkdir()
    plan = verification_target.resolve_gate_plan({}, "", py)
    assert plan.source == "default" and plan.commands == [verification_target.PYTHON_TEST_CMD]
    web = tmp_path / "web"
    web.mkdir()
    (web / "pom.xml").write_text("x")
    plan = verification_target.resolve_gate_plan({"project_kind": "web"}, "", web)
    assert plan.commands == verification_target.WEB_TEST_COMMANDS


def test_explicit_test_commands_still_win(tmp_path, java_on):
    repo = _repo(tmp_path, "pom.xml")
    plan = verification_target.resolve_gate_plan({"test_commands": ["mvn -q test"]}, "", repo)
    assert plan.source == "task" and plan.commands == ["mvn -q test"]


# ---- previews --------------------------------------------------------------------------------------

def test_a_java_service_previews_as_its_jar_on_port(tmp_path, java_on):
    repo = _repo(tmp_path, "pom.xml", "config.yml")
    assert preview_service.resolve_app_entry(repo) == ("java", ".")
    run = preview_service.how_app_runs(repo)
    assert run is not None and run.entry == ("java", ".")
    script = run.command[2]
    assert run.command[:2] == ["sh", "-lc"]
    assert script.startswith("mvn -B -ntp -q -DskipTests package && ")
    assert 'exec java -Dserver.port="$PORT" -jar "$jar" server config.yml' in script
    assert "target/*.jar" in script


def test_a_gradle_service_without_config_runs_plain_jar(tmp_path, java_on):
    repo = _repo(tmp_path, "build.gradle")
    script = preview_service.command_for_entry(repo, "java", ".")[2]
    assert script.startswith("gradle --no-daemon -q -x test assemble && ")
    assert script.endswith('-jar "$jar"') and "build/libs/*.jar" in script


def test_a_declared_preview_command_still_wins(tmp_path, java_on):
    repo = _repo(tmp_path, "pom.xml")
    (repo / preview_service.RUN_DECLARATION_FILE).write_text("java -jar app.jar\n")
    run = preview_service.how_app_runs(repo)
    assert run.source == "declared"


def test_previewing_java_without_the_toolchain_says_how_to_turn_it_on(tmp_path, java_off):
    repo = _repo(tmp_path, "pom.xml")
    script = preview_service.command_for_entry(repo, "java", ".")[2]
    assert "This project needs Java" in script and "exit 78" in script


def test_health_path_is_declarable_and_java_gets_build_time(tmp_path, java_on):
    repo = _repo(tmp_path, "pom.xml")
    assert preview_service.preview_health_path(repo) == "/"
    (repo / preview_service.HEALTH_DECLARATION_FILE).write_text("/hello\n")
    assert preview_service.preview_health_path(repo) == "/hello"
    (repo / preview_service.HEALTH_DECLARATION_FILE).write_text("http://evil/\n")
    assert preview_service.preview_health_path(repo) == "/"
    assert preview_service.preview_start_timeout(repo) == 600.0
    assert preview_service.preview_start_timeout(tmp_path / "nothing") == 180.0


# ---- egress ------------------------------------------------------------------------------------------

def test_egress_allows_maven_central_and_the_gradle_portal_and_nothing_broader(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from apps.egress.boss_egress import connect

    monkeypatch.delenv("BOSS_EGRESS_CONNECT_ALLOW", raising=False)
    for host in ("repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org",
                 "plugins-artifacts.gradle.org"):
        assert host in connect.DEFAULT_ALLOW and connect.host_allowed(host)
    for host in ("services.gradle.org", "downloads.gradle.org", "maven.org", "evil.maven.org",
                 "jitpack.io", "oss.sonatype.org"):
        assert not connect.host_allowed(host), host
    public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.0.215", 443))
    monkeypatch.setattr(connect.socket, "getaddrinfo", lambda h, p, type=0: [public])
    assert connect.decide(b"CONNECT repo.maven.apache.org:443 HTTP/1.1\r\n\r\n")[1] == ""
    assert connect.decide(b"CONNECT repo.maven.apache.org:80 HTTP/1.1\r\n\r\n")[1] == "port_not_allowed"


# ---- the sandbox: proxy config and per-project caches ---------------------------------------------

def test_maven_and_gradle_get_the_egress_proxy_in_their_own_config(tmp_path, java_on):
    home = tmp_path / "home"
    home.mkdir()
    assert toolchains.write_proxy_config(home, "http://egress:8091")
    settings = (home / ".m2" / "settings.xml").read_text()
    assert "<host>egress</host><port>8091</port>" in settings and "<active>true</active>" in settings
    props = (home / ".gradle" / "gradle.properties").read_text()
    assert "systemProp.https.proxyHost=egress" in props and "systemProp.https.proxyPort=8091" in props
    assert (home / ".m2").stat().st_mode & 0o7777 == 0o2777


def test_no_proxy_config_without_java(tmp_path, java_off):
    home = tmp_path / "home"
    home.mkdir()
    assert not toolchains.write_proxy_config(home, "http://egress:8091")
    assert not (home / ".m2").exists()


def test_caches_persist_per_project_and_never_cross_projects(tmp_path, java_on):
    run1 = tmp_path / "run1"
    jar = run1 / ".m2" / "repository" / "io" / "dropwizard" / "core.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"jar")
    gmod = run1 / ".gradle" / "caches" / "modules-2" / "files-2.1" / "x.jar"
    gmod.parent.mkdir(parents=True)
    gmod.write_bytes(b"g")
    assert sorted(toolchains.save_cache(run1, "proj-a")) == ["gradle", "m2"]

    run2 = tmp_path / "run2"
    run2.mkdir()
    assert sorted(toolchains.restore_cache(run2, "proj-a")) == ["gradle", "m2"]
    copied = run2 / ".m2" / "repository" / "io" / "dropwizard" / "core.jar"
    assert copied.read_bytes() == b"jar"
    # The next run's uid is not the copier: what it gets is writable to it.
    assert copied.stat().st_mode & 0o777 == 0o666
    assert copied.parent.stat().st_mode & 0o7777 == 0o2777

    other = tmp_path / "run3"
    other.mkdir()
    assert toolchains.restore_cache(other, "proj-b") == []
    assert not (other / ".m2").exists()
    assert toolchains.project_cache_dir("proj-a") != toolchains.project_cache_dir("proj-b")
    assert "proj-a" not in str(toolchains.project_cache_dir("proj-a"))


def test_no_cache_work_without_a_project_or_java(tmp_path, java_off):
    home = tmp_path / "h"
    (home / ".m2" / "repository").mkdir(parents=True)
    assert toolchains.save_cache(home, "p") == []
    assert toolchains.restore_cache(home, "p") == []


def test_setup_lists_toolchains(java_on):
    rows = {r["name"]: r for r in toolchains.setup_summary()}
    assert rows["python"]["installed"] and rows["node"]["installed"]
    assert rows["java"]["installed"] and rows["java"]["versions"]["maven"] == "3.9.16"
    assert rows["java"]["enable"] is None


def test_setup_says_how_to_turn_java_on(java_off):
    java = {r["name"]: r for r in toolchains.setup_summary()}["java"]
    assert not java["installed"] and java["enable"] == "boss toolchains enable java"


# ---- the install surface ------------------------------------------------------------------------------

def test_toolchains_are_opt_in_in_the_image_and_compose():
    dockerfile = (ROOT / "Dockerfile.community").read_text()
    assert "ARG BOSS_TOOLCHAINS=none" in dockerfile
    assert "FROM toolchains-${BOSS_TOOLCHAINS} AS toolchains" in dockerfile
    assert "AS toolchains-none" in dockerfile and "AS toolchains-java" in dockerfile
    java = dockerfile.split("AS toolchains-java", 1)[1].split("FROM toolchains-", 1)[0]
    assert "sha256sum -c" in java and "sha512sum -c" in java
    assert not re.search(r"\|\s*(ba)?sh\b", java), "a download is never piped into a shell"
    compose = (ROOT / "scripts" / "install" / "community-compose.yml").read_text()
    assert compose.count("BOSS_TOOLCHAINS: ${BOSS_TOOLCHAINS:-none}") == 4
    assert compose.count("boss_community_toolchain_cache:/var/lib/boss/toolchain-cache") == 2
    boss_sh = (ROOT / "scripts" / "install" / "boss.sh").read_text()
    assert "toolchains) cmd_toolchains" in boss_sh
