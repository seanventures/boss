from boss_engine.services.receipts import ReceiptService


class _Conn:
    def __init__(self, rows):
        self.rows = list(rows)
        self.seen = []

    def execute(self, stmt, params):
        sql = str(stmt)
        self.seen.append((sql, params))
        class Result:
            def __init__(self, row):
                self._row = row

            def mappings(self):
                return self

            def first(self):
                return self._row

        if "AND id =" in sql or "AND id=:" in sql.replace(" ", ""):
            wanted = params.get("receipt_id")
            for row in self.rows:
                if row["id"] == wanted:
                    return Result(row)
            return Result(None)
        wanted = params.get("dedupe_key")
        for row in self.rows:
            if row["dedupe_key"] == wanted:
                return Result(row)
        return Result(None)


def test_get_by_id_or_dedupe_prefers_receipt_id():
    svc = ReceiptService()
    row = {
        "id": "rcpt_abc",
        "dedupe_key": "code:task:crun_1",
        "effect_kind": "github.pull_request.create",
    }
    conn = _Conn([row])
    found = svc.get_by_id_or_dedupe_key(conn, "org", "rcpt_abc")
    assert found["id"] == "rcpt_abc"
    found2 = svc.get_by_id_or_dedupe_key(conn, "org", "code:task:crun_1")
    assert found2["dedupe_key"] == "code:task:crun_1"
    assert svc.get_by_id_or_dedupe_key(conn, "org", "") is None
    assert svc.get_by_id_or_dedupe_key(conn, "org", "missing") is None
