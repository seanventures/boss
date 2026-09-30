from boss_engine.services.jobs import JobLeaseService


class _Conn:
    def __init__(self):
        self.sql = ""

    def execute(self, stmt, params):
        self.sql = str(stmt)

        class Result:
            def mappings(self):
                return self

            def all(self):
                return []

        return Result()


def test_list_jobs_sort_updated_uses_updated_at():
    svc = object.__new__(JobLeaseService)
    conn = _Conn()
    svc.list_jobs(conn, "boss-dogfood", sort="updated", include_terminal=False)
    assert "j.updated_at DESC" in conn.sql
    assert "deadlettered" not in conn.sql.split("ORDER BY", 1)[1][:80]


def test_list_jobs_default_keeps_deadletter_priority():
    svc = object.__new__(JobLeaseService)
    conn = _Conn()
    svc.list_jobs(conn, "boss-dogfood")
    assert "WHEN 'deadlettered' THEN 0" in conn.sql
