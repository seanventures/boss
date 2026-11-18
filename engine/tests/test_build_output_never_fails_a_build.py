"""BUILD OUTPUT NEVER FAILS A BUILD.

THE CASE (2026-11-18, laptop, crun_332011ee, Easy Mode "Fixture clock", a Go API): the build
failed with "'fixtureclock': repository file exceeds 2097152 bytes". `go build` wrote the
compiled program into the repository root; the sandbox's diff (sandbox.EphemeralSandbox.diff,
which every stage, checkpoint and the PR read the changed files through) staged it with
`git add -A` and read it with RepoFiles.read_text, whose 2 MB cap refused it, and the refusal
failed the whole build.

  - a file the run ADDED that is a compiled program (ELF, Mach-O, or PE with a real PE header)
    is left out of the work with a note in words ("left out: fixtureclock, a 3 MB compiled
    binary — add it to .gitignore") and kept out of git until the next diff, which checks and
    notes it again (one BOSS block in .git/info/exclude, rewritten each time); the source ships;
  - any other file over the cap (new or tracked, text or data) refuses in words, unless
    .gitignore names it;
  - a new local project's .gitignore ignores its stack's build output (Go: /bin/), never bin/
    in other stacks, where it holds source (bin/cli.js).

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


def test_a_new_large_text_file_refuses_in_words_unless_it_is_ignored(tmp_path):
    """Only a compiled program is left out. A 3 MB text file the run made is work it cannot
    carry: said in words, never dropped silently."""
    box, _ = _repo(tmp_path)
    (tmp_path / "fixtures.json").write_text("[" + "1," * (3 * 1024 * 1024 // 2) + "1]")
    with pytest.raises(RepoFileRefused) as refused:
        box.diff()
    said = str(refused.value)
    assert said.startswith("fixtures.json is 3 MB") and "not a compiled program" in said
    assert "add it to .gitignore" in said
    # Once .gitignore names it, it is not staged and the diff goes on.
    (tmp_path / ".gitignore").write_text("fixtures.json\n")
    assert [c.path for c in box.diff()] == [".gitignore"]
    assert box.left_out == []


def test_a_large_untracked_data_blob_that_is_no_program_refuses_too(tmp_path):
    box, _ = _repo(tmp_path)
    (tmp_path / "dump.bin").write_bytes(b"\x00\x01" * (MAX_FILE_BYTES // 2 + 10))
    (tmp_path / "users.json").write_text('[{"id": 1}]')
    with pytest.raises(RepoFileRefused) as refused:
        box.diff()
    assert str(refused.value).startswith("dump.bin is ")


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


def test_a_node_projects_bin_cli_reaches_the_diff(tmp_path):
    """bin/ is source in a Node project: never ignored by the shared scaffold, never left out."""
    box, git = _repo(tmp_path)
    (tmp_path / ".gitignore").write_text(local_repo.initial_files("x", stack="Next.js")[".gitignore"])
    git("add", "-A")
    git("commit", "-qm", "scaffold")
    box.diff_base_sha = git("rev-parse", "HEAD").strip()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "cli.js").write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
    assert [c.path for c in box.diff()] == ["bin/cli.js"]
    assert box.left_out == []


def test_a_text_file_that_starts_with_mz_is_kept(tmp_path):
    box, _ = _repo(tmp_path)
    (tmp_path / "notes.md").write_text("MZ is how this file starts, and it is text.\n" * 20)
    assert [c.path for c in box.diff()] == ["notes.md"]
    assert box.left_out == []


def test_a_real_pe_header_is_a_compiled_program(tmp_path):
    box, _ = _repo(tmp_path)
    pe = bytearray(512)
    pe[0:2] = b"MZ"
    pe[0x3C:0x40] = (128).to_bytes(4, "little")
    pe[128:132] = b"PE\x00\x00"
    (tmp_path / "tool.bin").write_bytes(bytes(pe))
    (tmp_path / "time.go").write_text("package main\n")
    assert [c.path for c in box.diff()] == ["time.go"]
    assert box.left_out[0]["words"].startswith("left out: tool.bin, a 1 KB compiled binary")


def test_every_round_re_notes_a_left_out_binary_and_keeps_no_stale_exclude(tmp_path):
    box, git = _repo(tmp_path)
    (tmp_path / "fixtureclock").write_bytes(_elf(3 * 1024 * 1024))
    (tmp_path / "main.go").write_text("package main\n\nfunc main() { serve() }\n")
    exclude = tmp_path / ".git" / "info" / "exclude"

    box.diff()
    assert [n["path"] for n in box.left_out] == ["fixtureclock"]
    assert "/fixtureclock" in exclude.read_text()

    box.diff()                                                   # round two: still there
    assert [n["path"] for n in box.left_out] == ["fixtureclock"]
    assert exclude.read_text().count("/fixtureclock") == 1      # one block, rewritten

    (tmp_path / "fixtureclock").unlink()                         # the agent cleaned it up
    box.diff()
    assert box.left_out == []
    assert "fixtureclock" not in exclude.read_text()
    assert box._LEFT_OUT_BEGIN not in exclude.read_text()


def _ignored(tmp_path, paths, stack=None):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(local_repo.initial_files("x", stack=stack)[".gitignore"])
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


@pytest.mark.parametrize("stack", sorted(BUILD_OUTPUT))
def test_a_new_local_projects_gitignore_ignores_its_stacks_build_output(tmp_path, stack):
    wanted = BUILD_OUTPUT[stack]
    assert _ignored(tmp_path, wanted, stack=stack) == set(wanted)
    # ... and never a source file, bin/ included outside Go.
    sources = tmp_path / "sources"
    sources.mkdir()
    keep = ["main.go", "app/src/main/kotlin/Main.kt", "pages/index.tsx", "cmd/server/main.go",
            "a.out.md", "scripts/run.out.sh"] + ([] if stack == "Go" else ["bin/cli.js"])
    assert _ignored(sources, keep, stack=stack) == set()


def test_the_go_scaffold_ignores_its_binary_and_the_go_templates_build_there(tmp_path):
    go = []
    for path in sorted(TEMPLATES.glob("*.yaml")):
        item = yaml.safe_load(path.read_text(encoding="utf-8"))
        if str(item["stack"]).split()[0] == "Go":
            go.append(item["id"])
            assert "go build -o bin/" in item["prompt"], item["id"]
            assert local_repo.gitignore_for(item["stack"]).endswith("/bin/\n")
    assert {"fixture-clock", "license-shelf"} <= set(go)
    assert _ignored(tmp_path, ["bin/fixtureclock", "bin/licenseshelf"], stack="Go API") == {
        "bin/fixtureclock", "bin/licenseshelf"}
    assert "bin/" not in local_repo.gitignore_for("Next.js")


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
