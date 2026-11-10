"""A held tool call is bound to the exact arguments that were shown.

The digest is sha256 of a canonical payload, so key order cannot launder a
different call. A preview is safe to show: secret-named values are replaced,
and the arguments the model sent are left untouched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services.argument_binding import args_sha256, bind_held_call, redacted_preview

# sha256 of {"arguments":{"content":"x","path":"ran.py"},"tool":"write_file"}
_WRITE_FILE = "44410b06b5fa91f0ccf3545c8ea1edd18febb351ffe57acfe3d3e104147b0eed"
# sha256 of {"arguments":{"content":"x","meta":{"a":2,"b":1},"path":"ran.py"},"tool":"write_file"}
_WRITE_FILE_NESTED = "a4ec08a9d4e0fe1aa30a684374e6dd5b714b623f16c763bf5548f800f8987fb9"
# sha256 of {"arguments":{"text":"café"},"tool":"say"} — characters, not \u escapes
_SAY_CAFE = "3054014c667de598119bd65774e7c3f19b1d1168e3da6989d87654fa4fb100dd"


def test_args_sha256_is_the_digest_of_the_canonical_payload():
    assert args_sha256("write_file", {"path": "ran.py", "content": "x"}) == _WRITE_FILE


def test_dict_key_order_does_not_change_the_hash():
    forward = {"path": "ran.py", "content": "x", "meta": {"b": 1, "a": 2}}
    reverse = {"meta": {"a": 2, "b": 1}, "content": "x", "path": "ran.py"}
    assert args_sha256("write_file", forward) == args_sha256("write_file", reverse) == _WRITE_FILE_NESTED


def test_a_different_argument_value_changes_the_hash():
    original = args_sha256("write_file", {"path": "ran.py", "content": "x"})
    changed = args_sha256("write_file", {"path": "ran.py", "content": "y"})
    assert changed != original


def test_a_different_tool_name_changes_the_hash():
    assert args_sha256("read_file", {"path": "ran.py", "content": "x"}) != _WRITE_FILE


def test_non_ascii_arguments_are_hashed_as_characters():
    assert args_sha256("say", {"text": "café"}) == _SAY_CAFE


@pytest.mark.parametrize("arguments", [None, ["path"], "path", 1, True])
def test_non_dict_arguments_are_refused(arguments):
    with pytest.raises(ValueError):
        args_sha256("write_file", arguments)


def test_redacted_preview_replaces_secret_named_values_and_keeps_the_rest():
    preview = redacted_preview({
        "path": "a.py",
        "password": "hunter2",
        "Password": "still-secret",
        "TOKEN": "t",
        "api_key": "k",
        "ApiKey": "k2",
        "authorization": "Bearer x",
        "secret": {"inner": "nope"},
        "nested": {
            "ok": 1,
            "API_KEY": "sk",
            "Authorization": "Bearer y",
            "deeper": {"token": "z", "left": "stay"},
        },
        "items": [{"token": "abc", "name": "n"}, "plain", {"count": 2}],
        "password_hint": "keep",
        "token_count": 3,
        "note": None,
        "flag": False,
    })
    assert preview == {
        "path": "a.py",
        "password": "[redacted]",
        "Password": "[redacted]",
        "TOKEN": "[redacted]",
        "api_key": "[redacted]",
        "ApiKey": "[redacted]",
        "authorization": "[redacted]",
        "secret": "[redacted]",
        "nested": {
            "ok": 1,
            "API_KEY": "[redacted]",
            "Authorization": "[redacted]",
            "deeper": {"token": "[redacted]", "left": "stay"},
        },
        "items": [{"token": "[redacted]", "name": "n"}, "plain", {"count": 2}],
        "password_hint": "keep",
        "token_count": 3,
        "note": None,
        "flag": False,
    }


def test_redacted_preview_does_not_mutate_the_arguments():
    original = {
        "password": "hunter2",
        "nested": {"API_KEY": "sk", "deeper": {"token": "z"}},
        "items": [{"token": "abc"}],
        "secret": {"inner": "nope"},
    }
    preview = redacted_preview(original)
    assert original["password"] == "hunter2"
    assert original["nested"]["API_KEY"] == "sk"
    assert original["nested"]["deeper"]["token"] == "z"
    assert original["items"][0]["token"] == "abc"
    assert original["secret"] == {"inner": "nope"}
    assert preview is not original
    assert preview["nested"] is not original["nested"]
    assert preview["items"] is not original["items"]
    assert preview["items"][0] is not original["items"][0]


# sha256 of {"arguments":{"password":"hunter2","path":"a.py"},"tool":"write_file"}
_WRITE_FILE_SECRET = "520fdccd4b173d629f76cc8d2c29a597b897e4edfb2fdf09f3e7b4151094a589"


def test_a_held_call_stores_the_digest_and_the_redacted_preview():
    arguments = {"path": "a.py", "password": "hunter2"}
    held = bind_held_call("write_file", arguments)
    assert held["args_sha256"] == _WRITE_FILE_SECRET
    assert held["args_preview"]["path"] == "a.py"
    assert held["args_preview"]["password"] == "[redacted]"
    assert arguments["password"] == "hunter2"


def test_a_held_call_without_a_dict_adds_nothing():
    assert bind_held_call("write_file", "not-a-dict") == {}


def test_every_ask_decision_stores_the_argument_binding():
    source = (
        Path(__file__).resolve().parents[1] / "boss_engine" / "services" / "agent_loop.py"
    ).read_text()
    start = source.index("def _hold_for_untrusted_content")
    body = source[start:source.index("\ndef conversation_from_messages", start)]
    assert body.count("bind_held_call(") >= 4
