"""A failed code index must say why. The child's last stderr line was kept, and for a SQLAlchemy
error that line is only the "(Background on this error at: …)" link, so every failure read as a
URL (BOSS's own agent found it in the banking case study, BOSSAPP-767)."""
from boss_engine.services.knowledge.repo_index import _child_error

SQLA = """Traceback (most recent call last):
  File "x.py", line 1, in <module>
sqlalchemy.exc.OperationalError: (psycopg.errors.DeadlockDetected) deadlock detected
DETAIL:  Process 1 waits for ShareLock on transaction 2
[SQL: INSERT INTO kg_edges ...]
(Background on this error at: https://sqlalche.me/e/20/e3q8)
"""


def test_the_exception_line_is_kept_not_the_doc_link():
    said = _child_error(SQLA, 1)
    assert said.startswith("sqlalchemy.exc.OperationalError")
    assert "deadlock detected" in said and "sqlalche.me" not in said


def test_no_exception_line_falls_back_to_the_last_meaningful_line_then_the_exit_code():
    assert _child_error("something broke\n(Background on this error at: https://x)\n", 2) == "something broke"
    assert _child_error("", 137) == "exit 137"
