from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("install_deps", [0, 1])
@pytest.mark.parametrize("docker_present", [False, True])
def test_macos_missing_dependencies_explain_colima_and_compose(install_deps, docker_present):
    source = (ROOT / "install.sh").read_text()
    function = source.split("install_vm_deps() {", 1)[1].split("\n}\n", 1)[0]
    script = f"""
OS=Darwin
INSTALL_DEPS={install_deps}
need_cmd() {{
  if [ "$1" = docker ]; then return {0 if docker_present else 1}; fi
  return 0
}}
docker() {{ return 1; }}
ui_err() {{ printf '%s\\n' "$*"; }}
die() {{ printf '%s\\n' "$*"; exit 1; }}
install_vm_deps() {{{function}
}}
install_vm_deps
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert "Colima or Docker Desktop" in result.stdout
    assert "docs/community/COLIMA.md" in result.stdout
    if docker_present:
        assert "Docker Compose plugin is missing" in result.stdout
        assert "Docker CLI is missing" not in result.stdout
    else:
        assert "brew install colima docker docker-compose docker-buildx" in result.stdout


def test_stopped_macos_runtime_reports_start_instructions_before_sudo_fallback():
    source = (ROOT / "install.sh").read_text()
    hint = "boss_docker_unavailable_hint() {" + source.split("boss_docker_unavailable_hint() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    check = source.split("if ! docker info >/dev/null 2>&1; then", 1)[1]
    check = check.split('ui_ok "Docker daemon is running"', 1)[0]
    script = """
OS=Darwin
uname() { echo Darwin; }
docker() { if [ "$1" = context ]; then echo colima-boss; return 0; fi; return 1; }
die() { printf '%s\\n' "$*"; exit 1; }
boss_sudo_scope_safe() { echo unexpected-sudo-fallback; return 1; }
""" + hint + """
if ! docker info >/dev/null 2>&1; then
""" + check
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "colima start <profile>" in output and "Docker Desktop" in output
    assert "(colima-boss)" in output
    assert "docker context ls" in output
    assert "unexpected-sudo-fallback" not in output
