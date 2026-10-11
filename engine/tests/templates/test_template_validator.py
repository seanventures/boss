"""The import validator (F2 §2.5 step 2) and the built-in coverage rule (§2.12 "every X").

A clean fixture passes; each test breaks ONE thing and asserts the named reason. Nothing in a
template is executed by validation (the importer's tests pin the subprocess side)."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from boss_engine.templates import service
from boss_engine.templates.validator import (
    MAX_FILE_BYTES, TemplateTree, TreeEntry, digest_of, tree_from_dir, validate_tree,
)

from ._fixtures import MANIFEST, REPO_ROOT, write_template


def _validate(tmp_path: Path, *, source="git", validate_kw=None, **kw):
    tree = tree_from_dir(write_template(tmp_path / "t", **kw))
    return validate_tree(tree, source=source, **(validate_kw or {}))


def _has(report, fragment: str) -> bool:
    return any(fragment in r for r in report.reasons)


def test_a_clean_template_passes_with_a_stable_digest(tmp_path):
    report = _validate(tmp_path)
    assert report.ok, report.reasons
    assert report.manifest.id == "health-endpoint"
    assert report.digest.startswith("sha256:")
    again = validate_tree(tree_from_dir(tmp_path / "t"), source="git")
    assert again.digest == report.digest


def test_the_digest_changes_with_any_byte(tmp_path):
    a = digest_of({"a": b"1", "b": b"2"})
    assert a == digest_of({"b": b"2", "a": b"1"})
    assert a != digest_of({"a": b"1", "b": b"3"})
    assert a != digest_of({"a": b"1", "c": b"2"})


# ---- every built-in (the coverage rule) -----------------------------------------------------------

def test_every_built_in_template_validates_and_is_complete():
    found = service.builtins()
    assert found, "templates/apps/ has no built-in template"
    names = sorted(p.name for p in (REPO_ROOT / "templates" / "apps").iterdir() if p.is_dir())
    assert sorted(n for n, _, _ in found) == names
    for name, report, files in found:
        assert report.ok, f"{name}: {report.reasons}"
        m = report.manifest
        assert m.id == name, f"{name}: the directory is named for the template's id"
        for stack in m.stacks:
            assert any(p.startswith(f"stacks/{stack}/tests/") for p in files), f"{name}: {stack} tests"
        assert m.guide in files and m.security_note in files and "brief.md" in files


def test_hello_is_the_built_in_that_proves_the_engine():
    report = validate_tree(tree_from_dir(REPO_ROOT / "templates" / "apps" / "hello"), source="builtin")
    assert report.ok, report.reasons
    assert set(report.manifest.stacks) == {"node", "python"}
    assert all(report.manifest.tests[s].min_tests >= 2 for s in report.manifest.stacks)


# ---- the tree -------------------------------------------------------------------------------------

def test_a_symlink_is_refused_and_never_followed(tmp_path):
    root = write_template(tmp_path / "t")
    (root / "stacks" / "node" / "passwd").symlink_to("/etc/passwd")
    (root / "escape").symlink_to("../../..")
    tree = tree_from_dir(root)
    assert tree.entries["stacks/node/passwd"].kind == "symlink"
    assert tree.entries["stacks/node/passwd"].data is None
    report = validate_tree(tree, source="git")
    assert _has(report, "stacks/node/passwd: symlinks are not allowed")
    assert _has(report, "escape: symlinks are not allowed")


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "stacks/../../x", "a\\..\\b"])
def test_a_path_escaping_the_root_is_refused(tmp_path, path):
    tree = tree_from_dir(write_template(tmp_path / "t"))
    tree.add(TreeEntry(path, "file", 1, b"x"))
    report = validate_tree(tree, source="git")
    assert _has(report, "path escapes the template root")


def test_git_metadata_and_hooks_are_refused(tmp_path):
    tree = tree_from_dir(write_template(tmp_path / "t"))
    tree.add(TreeEntry(".git/hooks/post-checkout", "file", 10, b"#!/bin/sh\n"))
    tree.add(TreeEntry("stacks/node/.GIT/config", "file", 10, b"[core]\n"))
    report = validate_tree(tree, source="git")
    assert _has(report, ".git/hooks/post-checkout: git metadata")
    assert _has(report, "stacks/node/.GIT/config: git metadata")


def test_submodules_are_refused(tmp_path):
    tree = tree_from_dir(write_template(tmp_path / "t", files={".gitmodules": "[submodule \"x\"]\n"}))
    tree.add(TreeEntry("vendor/x", "submodule"))
    report = validate_tree(tree, source="git")
    assert _has(report, ".gitmodules: submodules are not allowed")
    assert _has(report, "vendor/x: submodules are not allowed")


def test_a_file_over_2_mb_is_refused_without_being_read(tmp_path):
    root = write_template(tmp_path / "t")
    (root / "stacks" / "node" / "big.json").write_bytes(b"0" * (MAX_FILE_BYTES + 1))
    tree = tree_from_dir(root)
    assert tree.entries["stacks/node/big.json"].data is None
    assert _has(validate_tree(tree, source="git"), "stacks/node/big.json: larger than 2 MB")


def test_more_than_20_mb_in_total_is_refused(tmp_path):
    root = write_template(tmp_path / "t")
    for i in range(11):
        (root / "stacks" / "node" / f"part{i}.txt").write_bytes(b"a" * (MAX_FILE_BYTES - 10))
    assert _has(validate_tree(tree_from_dir(root), source="git"), "larger than 20 MB in total")


def test_more_than_2000_files_is_refused(tmp_path):
    tree = tree_from_dir(write_template(tmp_path / "t"))
    for i in range(2001):
        tree.add(TreeEntry(f"stacks/node/f{i}.txt", "file", 1, b"x"))
    assert _has(validate_tree(tree, source="git"), "more than 2000 files")


def test_a_binary_is_refused_unless_an_image_or_font(tmp_path):
    report = _validate(tmp_path, files={"stacks/node/tool.bin": b"\x7fELF\x00\x00binary",
                                        "stacks/node/logo.png": b"\x89PNG\x00\x00",
                                        "stacks/node/font.woff2": b"wOF2\x00\x00"})
    assert _has(report, "stacks/node/tool.bin: a binary file")
    assert not _has(report, "logo.png") and not _has(report, "font.woff2")


def test_a_committed_credential_is_refused_and_the_reason_never_quotes_it(tmp_path):
    key = "AKIA" + "Q" * 4 + "7" * 12
    report = _validate(tmp_path, files={"stacks/node/config.js": f"const k = '{key}';\n"})
    assert _has(report, "stacks/node/config.js:1: looks like a committed secret (aws_access_key_id)")
    assert key not in repr(report.as_dict())


def test_a_value_matching_the_templates_own_pattern_is_refused(tmp_path):
    report = _validate(tmp_path, files={"stacks/python/settings.py":
                                        "TOKEN = get('HEALTH_TOKEN', 'ht_test_abcdefghijklmnop1')\n"})
    assert _has(report, "declared:HEALTH_TOKEN")


def test_a_placeholder_is_not_a_secret(tmp_path):
    report = _validate(tmp_path, files={"stacks/node/.env.example":
                                        "STRIPE_SECRET_KEY=sk_test_your_key_here_xxxxxxxx\n"
                                        "password = \"<your password here>\"\n"})
    assert report.ok, report.reasons


def test_the_aws_documented_example_key_is_a_finding_not_a_placeholder(tmp_path):
    """AKIAIOSFODNN7EXAMPLE has EXAMPLE in it, which excused it as a placeholder: a template
    shipping AWS's documented example credentials passed the secret scan."""
    report = _validate(tmp_path, files={"stacks/node/config.js":
                                        "const id = 'AKIAIOSFODNN7EXAMPLE';\n"
                                        "const secret = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY';\n"})
    assert not report.ok
    assert _has(report, "stacks/node/config.js:1: looks like a committed secret (aws_documented_example_key)")
    assert _has(report, "stacks/node/config.js:2: looks like a committed secret (aws_documented_example_key)")
    assert "AKIAIOSFODNN7EXAMPLE" not in repr(report.as_dict())


@pytest.mark.parametrize("missing,reason", [
    ("LICENSE", "license: no LICENSE file"),
    ("brief.md", "brief: brief.md is missing"),
    ("guide.md", "guide: guide.md is missing"),
    ("SECURITY.md", "security_note: SECURITY.md is missing"),
    ("stacks/python/tests/test_health.py", "tests.python: stacks/python/tests/ is missing or empty"),
    ("boss-template.yaml", "boss-template.yaml: missing"),
])
def test_each_required_file_is_named_when_missing(tmp_path, missing, reason):
    assert _has(_validate(tmp_path, drop=(missing,)), reason)


def test_a_declared_stack_without_its_directory_is_refused(tmp_path):
    report = _validate(tmp_path, drop=("stacks/python/health.py", "stacks/python/tests/test_health.py"))
    assert _has(report, "stacks.python: stacks/python/ is missing or empty")


# ---- the manifest, in the context of the install ----------------------------------------------------

def test_an_invalid_manifest_lists_every_problem(tmp_path):
    bad = copy.deepcopy(MANIFEST)
    bad["id"] = "X!"
    bad["egress"] = ["10.0.0.5", "*.evil.example"]
    report = _validate(tmp_path, manifest=bad)
    assert _has(report, "boss-template.yaml: id:")
    assert _has(report, "an IP address is not allowed")


def test_an_import_cannot_take_a_built_in_id(tmp_path):
    report = _validate(tmp_path, validate_kw={"builtin_ids": {"health-endpoint"}})
    assert _has(report, "id: 'health-endpoint' is a built-in template's id")
    assert _validate(tmp_path / "b", source="builtin", validate_kw={"builtin_ids": {"health-endpoint"}}).ok


def test_egress_to_this_boss_is_refused(tmp_path):
    m = copy.deepcopy(MANIFEST)
    m["egress"] = ["boss.acme.com", "api.boss.acme.com", "api.stripe.com"]
    report = _validate(tmp_path, manifest=m, validate_kw={"boss_hosts": ["boss.acme.com"]})
    assert _has(report, "'boss.acme.com' is one of this BOSS install's own hosts")
    assert _has(report, "'api.boss.acme.com' is one of this BOSS install's own hosts")
    assert not _has(report, "api.stripe.com")


@pytest.mark.parametrize("name", ["BOSS_MASTER_KEY", "DATABASE_URL", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                                  "ANTHROPIC_API_KEY", "HTTPS_PROXY", "LD_PRELOAD", "NODE_OPTIONS", "PGPASSWORD"])
def test_a_manifest_asking_for_a_secret_it_should_not_get_is_refused(tmp_path, name):
    m = copy.deepcopy(MANIFEST)
    m["secrets"] = [{"name": name, "test_default": "none"}]
    report = _validate(tmp_path, manifest=m)
    assert _has(report, f"secrets: {name} is reserved")


def test_a_reserved_name_is_refused_as_a_plain_setting_too(tmp_path):
    m = copy.deepcopy(MANIFEST)
    m["env"] = [{"name": "BOSS_EGRESS_URL", "default": "http://evil.example"}]
    assert _has(_validate(tmp_path, manifest=m), "env: BOSS_EGRESS_URL is reserved")


def test_insecure_by_design_needs_an_approver_and_the_acknowledgement(tmp_path):
    m = copy.deepcopy(MANIFEST)
    m["labels"] = ["insecure_by_design"]
    assert _has(_validate(tmp_path / "a", manifest=m), "needs an importer holding template:approve")
    assert _has(_validate(tmp_path / "b", manifest=m, validate_kw={"may_label_insecure": True}),
                "needs the importer's acknowledgement")
    assert _validate(tmp_path / "c", manifest=m,
                     validate_kw={"may_label_insecure": True, "ack_insecure": True}).ok
    # a built-in's label is the repo's decision, reviewed in CI
    assert _validate(tmp_path / "d", manifest=m, source="builtin").ok


def test_problems_found_while_reading_are_reported(tmp_path):
    tree = TemplateTree(problems=["more than 2000 files"])
    report = validate_tree(tree, source="git")
    assert not report.ok and "more than 2000 files" in report.reasons
