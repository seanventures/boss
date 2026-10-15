"""The heartbeat's version survives the allow-list for every release spelling BOSS publishes.

`version` was dropped from every heartbeat once because the package spells it the Python way
(`1.0.0rc9`) and the allow-list only carries the published form (`1.0.0-rc.9`). A point release
of a candidate (`1.0.0rc10.post7`, published `1.0.0-rc.10.7`) is the next spelling to break it.
"""
import pytest

from boss_engine.services import improve_allowlist, improve_boss


@pytest.mark.parametrize("python_version,published", [
    ("1.0.0rc9", "1.0.0-rc.9"),
    ("1.0.0rc10.post7", "1.0.0-rc.10.7"),
    ("1.0.0b2", "1.0.0-beta.2"),
    ("1.0.0", "1.0.0"),
])
def test_the_package_version_is_published_in_the_allowed_form(monkeypatch, python_version,
                                                              published):
    import boss_engine

    monkeypatch.setattr(boss_engine, "__version__", python_version)
    assert improve_boss._version() == published
    assert improve_allowlist._VERSION.fullmatch(published)


def test_this_release_version_reaches_the_heartbeat():
    assert improve_allowlist._VERSION.fullmatch(improve_boss._version())


@pytest.mark.parametrize("junk", ["1.0.0-rc.10.7.1", "1.0.0-rc.x", "1.0.0-rc.10.abc",
                                  "1.0.0-rc.10.7; drop table", "v1.0-rc10.7"])
def test_anything_else_is_still_refused(junk):
    assert not improve_allowlist._VERSION.fullmatch(junk)
