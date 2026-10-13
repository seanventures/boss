"""A GitHub refusal says which permission is missing and where to grant it (rc10 tester).

The PR #223 approval card showed "Client error '403 Forbidden' for url 'https://api.github.com/
repos/…/check-runs' For more information check: https://developer.mozilla.org/…".
"""
from __future__ import annotations

import inspect

from boss_engine.services import software_factory
from boss_engine.services.github_words import WHERE, github_error_in_words

RAW_403 = ("Client error '403 Forbidden' for url 'https://api.github.com/repos/paramg/"
           "boss-sandbox/commits/8a009a6ca249/check-runs'\nFor more information check: "
           "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403")


def test_a_403_on_checks_names_the_permission_and_where():
    words = github_error_in_words(RAW_403, doing="Couldn't read the pull request's checks")
    assert "Checks: Read" in words and WHERE in words
    assert "api.github.com" not in words and "mozilla" not in words


def test_other_statuses_say_what_to_do():
    assert "expired" in github_error_in_words("Client error '401 Unauthorized' for url "
                                              "'https://api.github.com/user'")
    assert "cannot see this repository" in github_error_in_words(
        "Client error '404 Not Found' for url 'https://api.github.com/repos/a/b/pulls/1'")


def test_a_non_github_error_keeps_its_own_first_line():
    assert github_error_in_words("timed out after 30s\ntrace…") == "timed out after 30s"


def test_the_merge_card_summary_uses_it():
    src = inspect.getsource(software_factory)
    assert 'f"Unable to capture GitHub validation evidence: {error}"' not in src
    assert "_github_words(error" in src
