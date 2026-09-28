"""Work moves from BOSS's fork to the repository it was forked from, with a person in between.

"We should have a mechanism to build merge to the main branch of sean/boss, I hope it should be
easy because we have access to boss repo of both using my account right?"

Access is not symmetric, and the asymmetry is permanent rather than a gap to close:

    example-org/boss       private
    paramg on upstream    push: true  (collaborator)
    BOSS's own token      404 on example-org/boss

A fine-grained token's resource owner is the person who issues it, so it cannot be scoped to a
repository owned by another user account — and the upstream repository being private means
"tokens can always read public repositories" does not rescue it either. What can reach
upstream is the operator's own credential.

So promotion is a thing a person runs, which is also the right shape: a pull request BOSS
opened on the fork becomes a pull request a human opens upstream. Nothing in here merges.
"""
from __future__ import annotations

import inspect
import pathlib

SCRIPT = pathlib.Path("scripts/promote_fork.py")


def _source() -> str:
    return SCRIPT.read_text()


def _code() -> str:
    """The script with comments and docstrings removed.

    Asserting against raw source keeps finding the PROSE that explains a thing and concluding
    the thing is there: "Nothing here merges anything. `gh pr merge` remains a deliberate
    act" makes a search for "pr merge" succeed. That has now happened four times in one
    session, so this strips them rather than dodging each phrase.
    """
    import io
    import tokenize

    kept = []
    for token in tokenize.generate_tokens(io.StringIO(_source()).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(token.string)
    return " ".join(kept)


def test_the_script_exists_and_is_runnable():
    assert SCRIPT.is_file()
    assert SCRIPT.read_text().startswith("#!/usr/bin/env python3")


def test_it_never_merges():
    """The decision to put agent-written code into the upstream main branch belongs to whoever
    owns that repository, and a script that could take it would eventually take it by
    accident."""
    code = _code()
    assert "pr merge" not in code
    assert "--merge" not in code
    assert "--squash" not in code


def test_it_opens_a_draft_unless_told_otherwise():
    """What BOSS wrote has been reviewed once on the fork; a second pair of eyes upstream is
    the point of promoting rather than pushing."""
    source = _source()
    assert 'create.append("--draft")' in source
    assert '"--ready"' in source


def test_it_checks_the_credential_before_it_pushes():
    """Failing at the push means a branch half-promoted and an error about git rather than
    about access."""
    source = _source()
    assert "def require_push_upstream()" in source
    assert ".permissions.push" in source
    at = source.index("def cmd_promote")
    assert "require_push_upstream()" in source[at:at + 200]


def test_it_syncs_the_fork_the_other_way_too():
    """A fork drifts in both directions. Branching the next piece of work from the day the fork
    was made is how an agent writes a fix against code that has already changed."""
    source = _source()
    assert "def cmd_sync" in source
    assert "repo\", \"sync\"" in source or '"sync", FORK' in source


def test_and_says_which_credential_it_is_using_when_gh_is_missing():
    """"gh: command not found" sends somebody to install a CLI without saying why BOSS's own
    token is not the answer."""
    source = _source()
    assert "BOSS's own token cannot" in source


def test_the_fork_and_upstream_are_named_once():
    """Two spellings of a repository name is how a promotion lands in the wrong place."""
    source = _source()
    assert source.count('FORK = "paramg/boss"') == 1
    assert source.count('UPSTREAM = "example-org/boss"') == 1
