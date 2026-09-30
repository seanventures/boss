"""model.generate_text and email.send_real were declared and never bound.

Every workflow wanting a step to draft or send anything found a skill in the catalogue that
could not run. That is the third instance of this shape today — a spec entry with no executor —
so these tests ask the registry and then call the functions.
"""
import inspect

import pytest

from boss_engine.services import skill_executors as se


def test_both_are_bound_at_all():
    ids = set(se.registered_executor_ids())
    assert "model.generate_text" in ids
    assert "email.send_real" in ids
    assert "email.send_real_guarded" in ids


def test_the_guarded_and_plain_email_skills_share_one_executor():
    """They are the same ACT. The difference is policy — the guarded spec requires an approval
    — and policy is enforced before an executor is reached, not inside it. Two copies would
    drift, and the one that drifted would be the one nobody tested."""
    source = inspect.getsource(se)
    guarded = source.index('@register_executor("email.send_real_guarded")')
    plain = source.index('@register_executor("email.send_real")')
    assert abs(guarded - plain) < 120, "the two ids no longer decorate the same function"


class _Model:
    def __init__(self):
        self.seen = None

    def create_text_response(self, prompt, system_prompt=""):
        self.seen = (prompt, system_prompt)
        return "ref", "a written thing", {"model": "fake"}


def _ctx(monkeypatch, model):
    """A context whose settings lookup is inert — this tests the executor, not the router.

    Patched on the modules the executor imports from INSIDE the function, which is where its
    local imports resolve. (An earlier version also tried to replace the module's __dict__,
    which is readonly and had nothing to do with anything.)
    """
    import boss_engine.services.claude_cli_adapter as cli
    import boss_engine.services.setup_service as setup

    monkeypatch.setattr(cli, "make_agent_chat_adapter", lambda _s: model)
    monkeypatch.setattr(setup, "chat_engine_for", lambda *a, **k: "")

    class Ctx:
        settings = object()
        connection = None
        project_id = "p"
        actor = "test"

    return Ctx()


def test_the_writer_returns_what_the_model_wrote(monkeypatch):
    model = _Model()
    out = se.execute_model_generate_text(_ctx(monkeypatch, model), {"prompt": "write a note"})
    assert out["text"] == "a written thing"
    assert out["characters"] == len("a written thing")


def test_material_from_an_earlier_step_reaches_the_prompt(monkeypatch):
    """A step wires whole VALUES and cannot build "write from: {dossier}" out of two of them.

    So the standing instruction is `prompt`, an earlier step's output is `context`, and the
    executor joins them once — rather than putting a template language in a JSON field.
    """
    model = _Model()
    se.execute_model_generate_text(
        _ctx(monkeypatch, model),
        {"prompt": "Write a newsletter.", "context": {"sector": "sports", "items": [1, 2]}})
    prompt, _system = model.seen
    assert "Write a newsletter." in prompt
    assert "sports" in prompt, "the earlier step's output never reached the model"
    assert "--- material ---" in prompt


def test_an_empty_prompt_is_refused_before_a_model_is_paid(monkeypatch):
    model = _Model()
    with pytest.raises(ValueError, match="prompt is required"):
        se.execute_model_generate_text(_ctx(monkeypatch, model), {"prompt": "  "})
    assert model.seen is None


def test_sending_refuses_rather_than_pretending(monkeypatch):
    """An install without SMTP gets the setting that is missing, not a success with nothing
    sent — which is the failure mode that matters for a thing nobody watches."""
    class Ctx:
        settings = object()
        connection = None
        project_id = "p"
        actor = "test"

    with pytest.raises(ValueError, match="to is required"):
        se.execute_email_send(Ctx(), {"body": "hello"})
    with pytest.raises(ValueError, match="body is required"):
        se.execute_email_send(Ctx(), {"to": "a@b.c"})


def test_a_plain_address_is_accepted_even_though_the_adapter_wants_a_channel():
    """EmailAdapter takes `email:someone@example.com` and refuses a bare address.

    That is a sensible rule for a channel abstraction and a trap for a workflow input called
    `to` — nobody types a scheme into a To field. Found on a live install: the test endpoint and
    this executor both failed with "Email channels must use the email:recipient@example.com
    format" on an address a person had typed correctly.
    """
    import inspect

    source = inspect.getsource(se.execute_email_send)
    assert 'to.startswith("email:")' in source
    assert 'f"email:{to}"' in source
