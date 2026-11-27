from boss_engine.services import local_repo, pushed_review


def test_pushed_documents_are_identified_from_both_commits(tmp_path):
    def git(*args):
        return local_repo.git(list(args), cwd=tmp_path)

    def commit(name, text):
        (tmp_path / name).write_text(text)
        git("add", "-A")
        git("-c", "user.name=QA", "-c", "user.email=qa@example.invalid",
            "commit", "-qm", "review fixture")
        return git("rev-parse", "HEAD")

    git("init", "-q")
    base = commit("README.md", "review fixture\n")
    head = commit("review.txt", "changes\n")
    db = tmp_path / ".git"
    reviewed = pushed_review.inspect(db, base, head)
    assert reviewed.documents_only and reviewed.paths == ["review.txt"]
    app = commit("run.py", "print('ready')\n")
    docs = commit("review.txt", "more changes\n")
    assert not pushed_review.inspect(db, app, docs).documents_only
    git("mv", "run.py", "implementation.txt")
    renamed = commit("README.md", "renamed\n")
    delta = pushed_review.inspect(db, docs, renamed)
    assert not delta.documents_only and "run.py" in delta.paths
    assert not pushed_review.inspect(db, base, base).documents_only


def test_executable_links_and_manifests_keep_the_code_path():
    for path, mode in [("guide.md", b"100755"), ("link.txt", b"120000"),
                       ("CMakeLists.txt", b"100644"), ("requirements/dev.txt", b"100644"),
                       ("requirements.txt", b"100644"), (".github/guide.md", b"100644")]:
        assert not pushed_review.prose_file(path, mode)
