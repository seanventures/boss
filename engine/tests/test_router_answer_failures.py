"""A failed answer must not look like a considered one.

Every failure in the router's answer path — a budget cap, a model outage, a tool raising —
collapsed into `reply = ""` and then into a friendly sentence about telling BOSS what to do.
So an agent that was BROKEN and an agent that was working produced identical text, and the
reasonable conclusion from outside was "this agent is useless" rather than "something failed".

These pin that the reason survives to the operator.
"""

from __future__ import annotations

from boss_engine.services import intent_router as ir


class _Budget:
    def __init__(self, status="reserved"):
        self.status = status

    def reserve(self, *a, **kw):
        return {"status": self.status, "reservation_id": "res_1"}

    def release(self, *a, **kw):
        return None


class _Turn:
    def __init__(self, reply):
        self.reply = reply


class _Answerer:
    def __init__(self, reply=None, boom=None):
        self.reply = reply
        self.boom = boom
        self.called = False

    def chat(self, **kwargs):
        self.called = True
        if self.boom:
            raise self.boom
        return _Turn(self.reply)


def _router(monkeypatch, *, budget_status="reserved", reply=None, boom=None):
    router = ir.IntentRouter.__new__(ir.IntentRouter)
    router.budget = _Budget(budget_status)
    router.settings = type("S", (), {"agent_router_budget_usd_micros": 1000})()
    answerer = _Answerer(reply=reply, boom=boom)
    monkeypatch.setattr(router, "_answer_agent", lambda: answerer, raising=False)
    monkeypatch.setattr(ir, "emit_event", lambda *a, **kw: None)
    return router, answerer


def _ask(router):
    return router._answer(
        None, "p1", [{"role": "user", "content": "why did the last build fail?"}]
    )


def test_a_real_answer_is_returned_unchanged(monkeypatch):
    router, _ = _router(monkeypatch, reply="It failed on npm test: 3 assertions.")
    assert "npm test" in _ask(router).reply


def test_a_refused_budget_says_the_model_was_never_called(monkeypatch):
    """The most likely cause of a vague answer, and previously invisible."""
    router, answerer = _router(monkeypatch, budget_status="denied")
    reply = _ask(router).reply
    assert "budget" in reply.lower()
    assert answerer.called is False


def test_a_model_error_is_reported_rather_than_disguised(monkeypatch):
    router, _ = _router(monkeypatch, boom=RuntimeError("upstream 529"))
    assert "529" in _ask(router).reply


def test_an_empty_model_reply_is_named_as_such(monkeypatch):
    router, _ = _router(monkeypatch, reply="   ")
    assert "empty answer" in _ask(router).reply


def test_a_failure_is_never_dressed_up_as_the_conversational_reply(monkeypatch):
    """The whole defect: broken and working produced the same sentence."""
    router, _ = _router(monkeypatch, boom=RuntimeError("boom"))
    reply = _ask(router).reply
    assert "I can help discuss this." not in reply
    assert "failure on my side" in reply
