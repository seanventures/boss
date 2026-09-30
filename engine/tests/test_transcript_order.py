"""A chat has to read in the order it happened.

An operator asked a follow-up and watched it appear ABOVE the answer to the previous question.
It was not a UI race: the question and its reply are written in one transaction, and Postgres
now() is TRANSACTION START time, so both rows carry an identical created_at. The query broke
that tie with `id` — a random hex — so the order of a conversation was decided by chance. In a
real thread, four of five answers sorted above the question they answered.
"""

import re

from boss_engine.services import agent_service


def _order_by() -> str:
    src = agent_service.AgentService.list_messages.__doc__ or ""
    code = agent_service.__dict__["AgentService"]
    import inspect
    body = inspect.getsource(code.list_messages)
    m = re.search(r"ORDER BY(.+?)\"\"\"", body, re.S)
    assert m, "list_messages no longer has an ORDER BY"
    return " ".join(m.group(1).split())


def test_the_tiebreak_is_not_a_random_id():
    order = _order_by()
    assert "CASE role" in order, f"ties still broken arbitrarily: {order}"


def test_a_question_sorts_before_its_own_answer():
    order = _order_by()
    u, a = order.index("'user'"), order.index("'assistant'")
    assert u < a, "the answer would still sort above the question"


def test_tool_work_sits_between_the_question_and_the_reply():
    order = _order_by()
    u, t, a = order.index("'user'"), order.index("'tool'"), order.index("'assistant'")
    assert u < t < a


def test_created_at_still_leads():
    """Role must only break TIES — ordering by role first would interleave whole turns."""
    order = _order_by()
    assert order.strip().startswith("created_at")


def test_the_ordering_is_total():
    """id stays as the final tiebreak so the result is stable across identical rows."""
    assert _order_by().rstrip().endswith("id")
