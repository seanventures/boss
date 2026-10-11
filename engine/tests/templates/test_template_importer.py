"""The git importer (F2 §2.5 step 1, §2.9 S2.5): adversarial repositories, real git, file://.

The guarantees under test: only `git` runs; nothing is checked out (a symlink is never created, a
hook never runs, no template file lands on disk); the URL and ref cannot smuggle an option, a
protocol or a credential; limits hold while reading; the result is pinned to a commit SHA."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from boss_engine.templates import importer
from boss_engine.templates.importer import ImportRefused, check_ref, check_url, fetch
from boss_engine.templates.validator import MAX_FILE_BYTES, validate_tree

from ._fixtures import commit_all, git, git_repo


def _fetch(repo: Path, ref=None, **kw):
    return fetch(f"file://{repo}", ref, allow_local=True, proxy="", **kw)


def test_a_clean_repository_imports_pinned_to_its_commit(tmp_path):
    repo, sha = git_repo(tmp_path / "src")
    got = _fetch(repo, "main")
    assert got.commit_sha == sha
    report = validate_tree(got.tree, source="git")
    assert report.ok, report.reasons
    assert got.tree.files()["brief.md"] == b"Add GET /healthz.\n"


def test_a_sha_ref_must_be_the_commit_fetched(tmp_path):
    repo, sha = git_repo(tmp_path / "src")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    assert _fetch(repo, sha).commit_sha == sha
    with pytest.raises(ImportRefused):
        _fetch(repo, "0" * 40)


def test_only_git_is_executed_and_nothing_is_checked_out(tmp_path, monkeypatch):
    repo, _ = git_repo(tmp_path / "src")
    (repo / "stacks" / "node" / "package.json").write_text(
        '{"scripts": {"preinstall": "touch /tmp/boss-s2-pwned", "postinstall": "touch /tmp/boss-s2-pwned"}}')
    commit_all(repo)
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(argv, *a, **kw):
        seen.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(importer.subprocess, "run", spy)
    work = tmp_path / "work"
    work.mkdir()
    got = _fetch(repo, workdir=work)
    assert seen and all(argv[0] == "git" for argv in seen)
    subcommands = {next(a for a in argv[1:] if not a.startswith("-") and "=" not in a) for argv in seen}
    assert subcommands <= {"init", "fetch", "rev-parse", "ls-tree", "cat-file"}, subcommands
    assert not any("checkout" in argv or "clone" in argv for argv in seen)
    assert list(work.iterdir()) == []                     # the quarantine is gone
    assert not Path("/tmp/boss-s2-pwned").exists()
    assert "stacks/node/package.json" in got.tree.files()


def test_every_git_call_disables_hooks_submodules_and_foreign_protocols(tmp_path, monkeypatch):
    repo, _ = git_repo(tmp_path / "src")
    seen: list[tuple[list[str], dict]] = []
    real = subprocess.run
    monkeypatch.setattr(importer.subprocess, "run",
                        lambda argv, *a, **kw: (seen.append((list(argv), dict(kw.get("env") or {}))),
                                                real(argv, *a, **kw))[1])
    _fetch(repo)
    for argv, env in seen:
        joined = " ".join(argv)
        assert "core.hooksPath=/dev/null" in joined and "protocol.allow=never" in joined
        assert "submodule.recurse=false" in joined and "transfer.fsckObjects=true" in joined
        assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ALLOW_PROTOCOL"] == "https:file"
        assert "DATABASE_URL" not in env and not any(k.startswith("BOSS_") for k in env)


def test_hooks_in_the_source_repository_never_run(tmp_path):
    marker = tmp_path / "hook-ran"
    repo, _ = git_repo(tmp_path / "src")
    # a committed hooks directory a careless clone might point core.hooksPath at
    (repo / ".githooks").mkdir()
    (repo / ".githooks" / "post-checkout").write_text(f"#!/bin/sh\ntouch {marker}\n")
    commit_all(repo)
    # and the source repository's own hooks, armed after its last commit (the server side of a
    # local fetch runs in that repository)
    hooks = repo / ".git" / "hooks"
    for name in ("post-checkout", "post-merge", "pre-push", "reference-transaction", "post-index-change",
                 "pre-receive", "post-update", "fsmonitor-watchman"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch {marker}\n")
        (hooks / name).chmod(0o755)
    got = _fetch(repo)
    assert not marker.exists()
    assert ".githooks/post-checkout" in got.tree.files()     # data, reviewed like any file


def test_a_symlink_escape_is_read_as_a_link_never_followed(tmp_path):
    repo, _ = git_repo(tmp_path / "src")
    (repo / "stacks" / "node" / "secrets").symlink_to("/etc/passwd")
    (repo / "up").symlink_to("../../../../")
    commit_all(repo)
    got = _fetch(repo)
    assert got.tree.entries["stacks/node/secrets"].kind == "symlink"
    assert got.tree.entries["stacks/node/secrets"].data is None
    report = validate_tree(got.tree, source="git")
    assert any("stacks/node/secrets: symlinks are not allowed" in r for r in report.reasons)
    assert any("up: symlinks are not allowed" in r for r in report.reasons)


def test_a_huge_file_is_listed_but_never_read(tmp_path):
    repo, _ = git_repo(tmp_path / "src")
    (repo / "stacks" / "node" / "blob.bin").write_bytes(os.urandom(MAX_FILE_BYTES + 4096))
    commit_all(repo)
    got = _fetch(repo)
    entry = got.tree.entries["stacks/node/blob.bin"]
    assert entry.size == MAX_FILE_BYTES + 4096 and entry.data is None
    assert any("stacks/node/blob.bin: larger than 2 MB" in r for r in validate_tree(got.tree).reasons)


def test_a_repository_over_the_quarantine_limit_is_refused(tmp_path, monkeypatch):
    repo, _ = git_repo(tmp_path / "src")
    monkeypatch.setattr(importer, "MAX_REPO_BYTES", 1024)
    (repo / "stacks" / "node" / "pad.txt").write_bytes(os.urandom(64 * 1024))
    commit_all(repo)
    with pytest.raises(ImportRefused, match="larger than"):
        _fetch(repo)


def test_a_submodule_is_recorded_and_refused(tmp_path):
    repo, sha = git_repo(tmp_path / "src")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/evil")
    (repo / ".gitmodules").write_text('[submodule "vendor/evil"]\n\tpath = vendor/evil\n\turl = https://evil.example/x\n')
    git(repo, "add", ".gitmodules")
    git(repo, "commit", "-q", "-m", "submodule")
    got = _fetch(repo)
    assert got.tree.entries["vendor/evil"].kind == "submodule"
    reasons = validate_tree(got.tree).reasons
    assert any("vendor/evil: submodules are not allowed" in r for r in reasons)
    assert any(".gitmodules: submodules are not allowed" in r for r in reasons)


def test_a_manifest_asking_for_secrets_it_should_not_get_is_rejected_at_import(tmp_path):
    from ._fixtures import MANIFEST
    import copy

    m = copy.deepcopy(MANIFEST)
    m["secrets"] = [{"name": "BOSS_MASTER_KEY"}, {"name": "GITHUB_TOKEN"}]
    m["env"] = [{"name": "HTTPS_PROXY", "default": "http://attacker.example:8080"}]
    repo, _ = git_repo(tmp_path / "src", manifest=m)
    reasons = validate_tree(_fetch(repo).tree).reasons
    for name in ("BOSS_MASTER_KEY", "GITHUB_TOKEN", "HTTPS_PROXY"):
        assert any(f"{name} is reserved" in r for r in reasons), reasons


# ---- the URL and the ref ----------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://github.com/acme/t.git", "ssh://git@github.com/acme/t.git", "git://github.com/acme/t.git",
    "ext::sh -c touch% /tmp/pwned", "https://user:pass@github.com/acme/t.git",
    "https://token@github.com/acme/t.git", "https://127.0.0.1/acme/t.git", "https://[::1]/t.git",
    "https://10.0.0.8/t.git", "https://localhost/t.git", "https://db/t.git", "https://git.corp/t.git",
    "https://github.com:8443/acme/t.git", "https://github.com/acme/t.git?x=1", "file:///etc",
    "-uhttps://github.com/acme/t.git",
])
def test_a_url_that_is_not_public_https_is_refused(url):
    with pytest.raises(ImportRefused):
        check_url(url, allow_local=False)


def test_a_refusal_never_echoes_a_credential():
    with pytest.raises(ImportRefused) as exc:
        check_url("https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/a/b.git")
    assert "ghp_" not in exc.value.reason


def test_a_public_https_url_is_accepted_and_normalised():
    assert check_url("https://GitHub.com/acme/t.git") == "https://github.com/acme/t.git"


@pytest.mark.parametrize("ref", ["--upload-pack=touch /tmp/pwned", "-c", "a..b", "x.lock", "HEAD@{1}",
                                 "a b", "$(id)", "x;y", ""])
def test_a_ref_that_could_be_an_option_or_expression_is_refused(ref):
    if ref == "":
        assert check_ref(ref) == "HEAD"
        return
    with pytest.raises(ImportRefused):
        check_ref(ref)


def test_a_failed_fetch_says_why_without_leaking(tmp_path):
    with pytest.raises(ImportRefused, match="no repository at file://") as e:
        fetch(f"file://{tmp_path}/missing", None, allow_local=True, proxy="")
    assert "fatal" not in e.value.reason


def test_the_fetch_goes_through_the_egress_proxy_when_the_install_has_one(tmp_path, monkeypatch):
    repo, _ = git_repo(tmp_path / "src")
    seen: list[list[str]] = []
    real = subprocess.run
    monkeypatch.setattr(importer.subprocess, "run",
                        lambda argv, *a, **kw: (seen.append((list(argv), kw.get("env") or {})), real(argv, *a, **kw))[1])
    monkeypatch.setenv("BOSS_EGRESS_CONNECT_PROXY", "http://egress:8091")
    fetch(f"file://{repo}", None, allow_local=True)
    fetch_calls = [(argv, env) for argv, env in seen if "fetch" in argv]
    assert fetch_calls and all("http.proxy=http://egress:8091" in " ".join(a) for a, _ in fetch_calls)
    assert all(env.get("HTTPS_PROXY") == "http://egress:8091" for _, env in fetch_calls)
