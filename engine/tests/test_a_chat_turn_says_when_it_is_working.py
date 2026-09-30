"""`working` on a thread read must describe THAT chat turn.

The console's landing poller asks GET /v1/agent/threads/{id}/messages whether a
turn is still running. The first version queried `agent_runs` — the old agent
loop / Metatron table. Console chat never writes that table. It writes
`agent_progress` (committed on its own connection so the poller can see it
mid-turn) and `investigations` (which outlive the HTTP request).

So after the 240s reporting bound the dock saw `working: false`, decided the
turn had died, and told the operator to send again. The real turn was still
inside IntentRouter.route_turn.
"""

from pathlib import Path

from boss_engine.services.agent_turn_status import thread_is_working

from ._source import code_only


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, *, progress=False, investigation=False, agent_run=False):
        self.progress = progress
        self.investigation = investigation
        self.agent_run = agent_run
        self.seen: list[str] = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        self.seen.append(sql)
        if "from agent_progress" in sql:
            return _Rows([{"ok": 1}] if self.progress else [])
        if "from investigations" in sql:
            return _Rows([{"ok": 1}] if self.investigation else [])
        if "from agent_runs" in sql:
            return _Rows([{"ok": 1}] if self.agent_run else [])
        raise AssertionError(f"unexpected query: {sql}")


def test_progress_on_this_thread_means_working():
    assert thread_is_working(FakeConn(progress=True), "p", "thr_1") is True


def test_a_live_investigation_means_working():
    assert thread_is_working(FakeConn(investigation=True), "p", "thr_1") is True


def test_the_old_agent_loop_alone_is_not_this_turn():
    # agent_runs is a different product. A Metatron row must not keep a dock
    # turn alive, and its absence must not declare a chat turn dead.
    assert thread_is_working(FakeConn(agent_run=True), "p", "thr_1") is False


def test_idle_is_idle():
    assert thread_is_working(FakeConn(), "p", "thr_1") is False


def test_the_messages_endpoint_uses_the_thread_signal():
    raw = Path("apps/api/app/main.py").read_text(encoding="utf-8")
    src = code_only(raw)
    chunk = src[src.find("def get_agent_thread_messages_endpoint") :]
    chunk = chunk[: chunk.find("\n@app.")]
    assert "thread_is_working" in chunk
    assert "from agent_runs" not in chunk
