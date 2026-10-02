"""The reasoning process must not try to call BOSS's tools itself.

BOSS drives the tool loop: the model is asked to NAME the next tool in a JSON object, BOSS
executes it and hands back the result. But this backend runs inside Claude Code, which has a
tool system of its own — so a long "AVAILABLE TOOLS" list reads to the model like a toolbelt it
should reach for. It did: it stopped emitting JSON, tried to invoke those names natively, was
denied, retried until the turn ceiling, and the process exited 1 with
`stop_reason: "tool_use"`. Every tool-using question in the console came back "I could not
answer that".

Two properties made it nasty to diagnose:

  * It scaled with the SIZE of the tool list. 52 registered tools answered fine and the 53rd
    tipped it over, so it presented as "the tool added last broke Ask BOSS" — a control run
    with that tool removed and a different one removed instead is what separated the two.
  * The model narrated a confusion that pointed at the wrong system entirely, reporting that
    `read_spec` and `ToolSearch` were unreachable. `ToolSearch` is a Claude Code concept and
    has nothing to do with BOSS; the tool was registered and exposed the whole time.

So the fix is a contract stated where it cannot be missed — before the list, and again after
it — plus enough turn budget to survive one stray attempt.
"""

from __future__ import annotations

import json

from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter


class _Settings:
    repo_root = __import__("pathlib").Path("/tmp")
    claude_cli_timeout = 240
    claude_cli_model = None
    claude_cli_path = "claude"


def _adapter() -> ClaudeCliAdapter:
    return ClaudeCliAdapter(_Settings())


def _prompt_with_tools(n: int = 53) -> str:
    tools = [
        {"type": "function", "function": {
            "name": f"tool_{i}", "description": "does a thing",
            "parameters": {"type": "object", "properties": {}}}}
        for i in range(n)
    ]
    return _adapter()._build_prompt([{"role": "user", "content": "hi"}], tools)


def test_the_tool_list_is_introduced_as_boss_s_not_the_model_s():
    """Stated BEFORE the list — after it, the list has already done the priming."""
    prompt = _prompt_with_tools()
    intro = prompt.index("BOSS's tools, not yours")
    assert intro < prompt.index("AVAILABLE TOOLS:"), "the disclaimer trails the list it qualifies"
    assert "cannot invoke them" in prompt


def test_the_last_word_is_output_json_yourself():
    """The final instruction is the one the model acts on."""
    prompt = _prompt_with_tools()
    tail = prompt[-400:]
    assert "exactly one JSON object" in tail
    assert "do not attempt to use any tool of your own" in tail


def test_the_rules_say_the_model_has_no_tools_of_its_own():
    prompt = _prompt_with_tools()
    assert "You have NO tools of your own here" in prompt
    assert "executed by BOSS after you name them" in prompt


def test_one_stray_tool_attempt_does_not_end_the_turn():
    """`--max-turns 2` meant a single denied attempt was fatal.

    Denying Claude Code's own tools does not stop the model from trying; it costs a turn. With
    two turns there was nothing left to recover into.
    """
    cmd = _build_cmd()
    turns = int(cmd[cmd.index("--max-turns") + 1])
    assert turns >= 4, f"--max-turns {turns} leaves no room to recover from a denied attempt"


def test_the_model_still_gets_no_tools_of_its_own():
    """The turn budget is headroom, NOT permission. BOSS controls all data access."""
    cmd = _build_cmd()
    blocked = cmd[cmd.index("--disallowedTools") + 1]
    for tool in ("Bash", "Read", "Write", "Edit", "WebFetch", "Task"):
        assert tool in blocked, f"{tool} is no longer denied"


def _build_cmd() -> list[str]:
    """The argv the adapter would run, captured without running it."""
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps({"result": "{}"})
        stderr = ""

    import subprocess

    real = subprocess.run

    def fake(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _Proc()

    subprocess.run = fake
    try:
        try:
            _adapter()._invoke("hello")
        except Exception:
            pass                                   # parsing the stub result is not the point
    finally:
        subprocess.run = real
    assert "cmd" in captured, "the adapter never shelled out"
    return captured["cmd"]


def test_a_tool_list_that_grows_does_not_change_the_contract():
    """The failure appeared at 53 tools. Nothing here may depend on the count."""
    for n in (5, 53, 120):
        prompt = _prompt_with_tools(n)
        assert "BOSS's tools, not yours" in prompt
        assert "do not attempt to use any tool of your own" in prompt[-400:]
