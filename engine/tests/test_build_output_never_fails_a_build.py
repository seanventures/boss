"""BUILD OUTPUT NEVER FAILS A BUILD.

THE CASE (2026-11-18, laptop, crun_332011ee, Easy Mode "Fixture clock", a Go API): the build
failed with "'fixtureclock': repository file exceeds 2097152 bytes". `go build` wrote the
compiled program into the repository root; the sandbox's diff (sandbox.EphemeralSandbox.diff,
which every stage, checkpoint and the PR read the changed files through) staged it with
`git add -A` and read it with RepoFiles.read_text, whose 2 MB cap refused it, and the refusal
failed the whole build.

  - a file the run ADDED that is a compiled program, or too large to read, is left out of the
    work with a note in words ("left out: fixtureclock, a 3 MB compiled binary — add it to
    .gitignore"), unstaged and kept out of git for the rest of the run; the source change ships;
  - a file git already TRACKS that is over the cap still refuses, in words;
  - a new local project's .gitignore ignores the build output of every compiled stack the
    factory templates name, and the Go templates build into that ignored bin/.

Real git in a temporary repository; no model, no database.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from boss_engine.services import local_repo, sandbox
from boss_engine.services.repo_files import MAX_FILE_BYTES, RepoFileRefused

TEMPLATES = Path(sandbox.__file__).resolve().parents[1] / "data" / "factory_templates"


def _repo(tmp_path):
    def git(*a):
        return subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, text=True,
                              check=True).stdout

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    box = sandbox.EphemeralSandbox.__new__(sandbox.EphemeralSandbox)
    box.path = tmp_path
    box._repo_files = None
    box.diff_base_sha = git("rev-parse", "HEAD").strip()
    box._git = lambda args, raw=False, **kw: git(*args)
    return box, git


def _elf(size: int) -> bytes:
    return b"\x7fELF\x02\x01\x01" + b"\x00" * (size - 7)


def test_an_untracked_binary_is_left_out_and_the_source_change_ships(tmp_path):
    box, git = _repo(tmp_path)
    (tmp_path / "main.go").write_text("package main\n\nfunc main() { serve() }\n")
    (tmp_path / "fixtureclock").write_bytes(_elf(3 * 1024 * 1024))

    changes = box.diff()

    assert [c.path for c in changes] == ["main.go"]
    assert "serve()" in changes[0].content
    assert [n["path"] for n in box.left_out] == ["fixtureclock"]
    words = box.left_out[0]["words"]
    assert words == "left out: fixtureclock, a 3 MB compiled binary — add it to .gitignore"
    # Kept out of git for the rest of the run: the next stage's `git add -A` never stages it.
    assert "fixtureclock" not in git("diff", "--cached", "--name-only")
    assert box.diff_summary().paths == ["main.go"]
    assert [c.path for c in box.diff()] == ["main.go"]
    assert len(box.left_out) == 1                                  # said once


def test_a_small_compiled_binary_is_build_output_too(tmp_path):
    box, _ = _repo(tmp_path)
    (tmp_path / "fixtureclock").write_bytes(_elf(64 * 1024))
    (tmp_path / "time.go").write_text("package main\n")
    assert [c.path for c in box.diff()] == ["time.go"]
    assert box.left_out[0]["path"] == "fixtureclock"


def test_an_untracked_large_data_file_is_left_out_not_a_failure(tmp_path):
    box, _ = _repo(tmp_path)
    (tmp_path / "dump.bin").write_bytes(b"\x00\x01" * (MAX_FILE_BYTES // 2 + 10))
    (tmp_path / "users.json").write_text('[{"id": 1}]')
    assert [c.path for c in box.diff()] == ["users.json"]
    assert "dump.bin" in box.left_out[0]["words"]


def test_a_tracked_file_over_the_cap_still_refuses_in_words(tmp_path):
    box, git = _repo(tmp_path)
    (tmp_path / "data.csv").write_text("a,b\n")
    git("add", "-A")
    git("commit", "-qm", "data")
    box.diff_base_sha = git("rev-parse", "HEAD").strip()
    (tmp_path / "data.csv").write_text("a,b\n" + "1,2\n" * (MAX_FILE_BYTES // 4 + 10))
    with pytest.raises(RepoFileRefused) as refused:
        box.diff()
    said = str(refused.value)
    assert said.startswith("data.csv is ") and "Git already tracks it" in said


def _ignored(tmp_path, paths):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(local_repo.initial_files("x")[".gitignore"])
    out = subprocess.run(["git", "check-ignore", "--no-index", *paths], cwd=tmp_path,
                         capture_output=True, text=True)
    return set(out.stdout.split())


#: The build output of each compiled stack the factory templates name.
BUILD_OUTPUT = {
    "Go": ["bin/fixtureclock", "fixtureclock.test", "app.exe"],
    "Jetpack Compose": ["app/build/outputs/apk/debug/app-debug.apk", ".gradle/8.7/x.lock",
                        "release.aab"],
    "Next.js": [".next/server/app.js"],
    "Godot 4": [".godot/imported/x.ctex", "game.pck"],
    "Java": ["target/app.jar", "Main.class"],
}


def test_a_new_local_projects_gitignore_ignores_every_compiled_stacks_output(tmp_path):
    wanted = [p for paths in BUILD_OUTPUT.values() for p in paths]
    assert _ignored(tmp_path, wanted) == set(wanted)
    # ... and never a source file.
    sources = tmp_path / "sources"
    sources.mkdir()
    assert _ignored(sources, ["main.go", "app/src/main/kotlin/Main.kt", "pages/index.tsx",
                              "cmd/server/main.go"]) == set()


def test_the_go_templates_build_into_the_ignored_bin(tmp_path):
    go = []
    for path in sorted(TEMPLATES.glob("*.yaml")):
        item = yaml.safe_load(path.read_text(encoding="utf-8"))
        if str(item["stack"]).split()[0] == "Go":
            go.append(item["id"])
            assert "go build -o bin/" in item["prompt"], item["id"]
    assert go, "no Go template found"
    assert {"fixture-clock", "license-shelf"} <= set(go)
    assert _ignored(tmp_path, ["bin/fixtureclock", "bin/licenseshelf"]) == {
        "bin/fixtureclock", "bin/licenseshelf"}


def test_the_run_records_what_was_left_out():
    """Each stage's checkpoint reads the diff; what it left out is written on the run
    (finalize_ctx.left_out) and as code_task.files_left_out, once per file."""
    import inspect

    from boss_engine.services import governed_code_task_service as governed

    src = inspect.getsource(governed)
    at = src.index("            files = sandbox.diff()\n        except Exception:")
    assert "_note_left_out(sandbox)" in src[at:at + 400]
    note = src[src.index("    def _note_left_out(box)"):]
    assert "'left_out'" in note[:1500] and "code_task.files_left_out" in note[:1500]
