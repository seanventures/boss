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
    (tmp_path / ".gitignore").write_text("*.log\n")
    base = commit("README.md", "review fixture\n")
    head = commit("review.txt", "changes\n")
    db = tmp_path / ".git"
    reviewed = pushed_review.inspect(db, base, head)
    assert reviewed.documents_only and reviewed.paths == ["review.txt"]
    new_ignore = commit(".gitignore", "*.tmp\n")
    assert not pushed_review.inspect(db, head, new_ignore).documents_only
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


def test_review_checkpoints_never_modify_the_submitted_branch():
    from boss_engine.services.change_recheck import push_hold_reason

    for stage in ("checkpoint:write_tests", "checkpoint:cancelled"):
        assert push_hold_reason(False, None, submitted_review=True, where=stage)
        assert push_hold_reason(False, None, where=stage) is None
    assert push_hold_reason(False, "template", submitted_review=True, where="pull_request") == "template"


def test_pushed_document_acceptance_requires_both_receipts():
    from types import SimpleNamespace
    from boss_engine.services.pr_description import render

    items = [{"n": n, "text": text} for n, text in enumerate(pushed_review.REVIEW_CRITERIA, 1)]
    args = dict(stack={}, source={"root": True}, dropped=0, head="a"*40, paths=["guide.md"],
                check=SimpleNamespace(exit_code=0, timed_out=False), verdict="survived",
                votes=[({"summary": "REVIEWED: guide.md. No issue."}, "survived")], suspect={})
    def result(**changes):
        return pushed_review.documentation_acceptance(items, **{**args, **changes})
    assert result()["met"] == [1, 2]
    assert result()["verdict"] == "met"
    assert result(votes=[])["verdict"] == "not_run"
    assert result(votes=[({"summary": "Okay"}, "survived")])["verdict"] == "not_run"
    assert result(suspect={"review_code": "moved"})["verdict"] == "not_run"
    assert result(check=SimpleNamespace(exit_code=1, timed_out=False))["verdict"] == "not_met"
    assert result(verdict="refuted", votes=[({"summary": "Defect in guide.md"}, "refuted")])["verdict"] == "not_met"
    assert result(stack={"missing": ["go.mod"]})["verdict"] == "not_met"
    assert result(source={"root": False})["verdict"] == "not_run"
    assert result(dropped=1) is None
    assert pushed_review.documentation_acceptance(items + [{"n": 3, "text": "Ship app"}], **args) is None
    body = render(title="review", evidence={"documentation_review": {
        "submitted_paths": ["guide.md"], "exit_code": 0}}, gates={"written_tests_pass": True})
    assert "guide.md" in body and "✅ New tests pass" not in body
    assert "git diff --check" in body and "Application tests are inapplicable" in body
