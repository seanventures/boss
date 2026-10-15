"""The image that runs builds has a C/C++ toolchain (BOSSAPP-515).

Agents picked npm packages that compile on install (better-sqlite3, bcrypt: node-gyp needs make and
g++) and the build failed, so they were told to avoid them. The runtime stage now installs
build-essential (test_every_engine_binary_is_in_the_image), and what an agent is told follows
what the sandbox actually has: the "no C/C++ compiler" fact is said only where no compiler is on
the PATH.
"""
from __future__ import annotations


def test_an_agent_is_told_there_is_no_compiler_only_where_there_is_none(monkeypatch):
    from boss_engine.services import code_agent

    for have, says_none in (({"cc", "make", "g++"}, False), (set(), True)):
        monkeypatch.setattr(code_agent, "_SANDBOX_FACTS", {})
        monkeypatch.setattr("shutil.which", lambda tool, _h=have: f"/usr/bin/{tool}"
                            if tool in _h else None)
        assert (code_agent.NO_COMPILER_FACT in code_agent.sandbox_facts()) is says_none
