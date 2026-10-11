"""A template larger than the brief's inline budget reaches the build WHOLE (F2 §2.3).

The brief inlined at most 48 KB of the reference, sorted by path: the banking starter (~84 KB)
lost its later sources and every tests/ file, the agent could not copy the tests, and
`template_tests` failed. Now (1) the brief inlines the tests first and names every reference
file, and (2) the build gets the whole pinned file set on disk, read-only, named by
BOSS_TEMPLATE_DIR / BOSS_TEMPLATE_STACK_DIR in every process of that build (templates.run_ref).
"""
from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

from boss_engine.services import agent_env, owned_process
from boss_engine.templates import apply, gate, run_ref
from boss_engine.templates.manifest import validate_manifest

MANIFEST = {
    "schema": "boss.template/v1", "id": "big-starter", "name": "Big starter", "version": "1.0.0",
    "summary": "A starter larger than the inline budget", "adds": ["routes"],
    "stacks": {"python": {"frameworks": ["fastapi"], "min_toolchain": "python311"}},
    "routes": ["GET /big"],
    "tests": {"python": {"command": "python3 -m pytest -q tests/big --junitxml=r.xml", "report": "junit",
                         "min_tests": 1}},
    "license": "Apache-2.0", "min_boss": "1.0.0",
}


def _files() -> dict[str, bytes]:
    files = {"BOSS-TEMPLATE.md": b"# Big starter\n", "boss-template.yaml": b"id: big-starter\n"}
    for i in range(12):                                         # ~96 KB of sources, sorted BEFORE tests/
        files[f"stacks/python/app/module_{i:02d}.py"] = (f"# module {i}\n" + "x = 1\n" * 1300).encode()
    files["stacks/python/tests/big/test_big.py"] = b"def test_big():\n    assert True\n"
    files["stacks/python/tests/big/test_more.py"] = b"def test_more():\n    assert 1 + 1 == 2\n"
    return files


def test_the_fixture_is_larger_than_the_inline_budget():
    assert sum(len(v) for v in _files().values()) > apply.MAX_INLINE_REFERENCE


def test_the_brief_inlines_the_tests_first_and_points_at_the_whole_set():
    manifest = validate_manifest(MANIFEST)
    brief = apply.build_directive(row={"source": "builtin"}, manifest=manifest, stack="python",
                                  framework=None, files=_files(), note="", app_name="shop")
    assert "### tests/big/test_big.py" in brief and "### tests/big/test_more.py" in brief
    assert brief.index("### tests/big/test_big.py") < brief.index("### app/module_00.py")
    assert "BOSS_TEMPLATE_DIR" in brief and "BOSS_TEMPLATE_STACK_DIR" in brief
    assert "app/module_11.py" in brief                         # every file is named
    assert "read them in $BOSS_TEMPLATE_STACK_DIR" in brief    # what was not inlined says where it is


class _Engine:
    def begin(self):
        return contextlib.nullcontext(None)


def test_the_build_gets_every_file_read_only_and_only_inside_its_own_scope(tmp_path, monkeypatch):
    files = _files()
    monkeypatch.setattr(gate, "applied_template",
                        lambda _c, _run: {"id": "tpl_big", "key": "big-starter", "version": "1.0.0",
                                          "stack": "python", "files": files})

    class Ident:
        run_dir = tmp_path

    with owned_process.owner_scope("code_run", "crun_big"):
        with run_ref.for_run(_Engine(), {"id": "crun_big"}, Ident()) as root:
            assert root == tmp_path / "template"
            for rel, content in files.items():
                assert (root / rel).read_bytes() == content
            env = agent_env.agent_environment(os.environ)
            assert env["BOSS_TEMPLATE_DIR"] == str(root)
            assert env["BOSS_TEMPLATE_STACK_DIR"] == str(root / "stacks" / "python")
            mode = stat.S_IMODE(os.stat(root / "stacks/python/tests/big/test_big.py").st_mode)
            assert mode == 0o444 and stat.S_IMODE(os.stat(root / "stacks/python").st_mode) == 0o555
        assert not (tmp_path / "template").exists()              # removed with the build
    assert "BOSS_TEMPLATE_DIR" not in agent_env.agent_environment(os.environ)


def test_a_build_that_applies_no_template_gets_nothing(monkeypatch):
    monkeypatch.setattr(gate, "applied_template", lambda _c, _run: None)
    with run_ref.for_run(_Engine(), {"id": "crun_plain"}) as root:
        assert root is None


def test_a_path_that_leaves_the_directory_is_never_written(tmp_path):
    run_ref.write_reference(tmp_path / "t", {"../escape.txt": b"x", "/abs.txt": b"y", "ok.txt": b"z"})
    assert (tmp_path / "t" / "ok.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_the_pipeline_gives_every_build_its_template_files():
    import inspect

    from boss_engine.services import governed_code_task_service as gov

    assert "_template_files(engine, run, _sbx)" in inspect.getsource(gov.run_governed_code_task)
    assert "_template_ref.current()" in inspect.getsource(agent_env.agent_environment)
